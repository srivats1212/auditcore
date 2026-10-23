"""Documents -> observations -> citations -> proposed tags.

Every step is idempotent: re-running on the same document version adds
nothing. Published counts only ever use tags with ``status="accepted"``.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    CfrReference,
    Citation,
    Document,
    FailureMode,
    Form483,
    Observation,
    WarningLetter,
    utcnow,
)
from ..taxonomy.loader import Taxonomy
from ..text.cfr import CfrRef, regime_for
from ..text.ocr_cleanup import clean_text
from ..text.segment import split_483, split_citations, split_warning_letter
from .llm import LlmRefusal, LlmTagger, strip_request_tail
from .rules import has_electronic_cue, tag_text

log = logging.getLogger(__name__)

# Rules are high precision by design; a rule hit enters review with this prior.
RULE_CONFIDENCE = 0.9


def get_or_create_cfr(session: Session, ref: CfrRef) -> CfrReference:
    # Part 820 rows carry "QSR"; whether a given citation is QSR or QMSR depends
    # on its date, so analytics resolves that per citation with regime_for().
    row = session.scalars(select(CfrReference).where(CfrReference.canonical == ref.canonical)).first()
    if row is None:
        row = CfrReference(
            canonical=ref.canonical,
            part=ref.part,
            section=ref.section or f"Part {ref.part}",
            paragraph=ref.paragraph,
            regime=regime_for(ref.part, None),
        )
        session.add(row)
        session.flush()
    return row


def segment_document(session: Session, doc: Document) -> list[Observation]:
    """Create observation and citation rows for a current document version."""
    existing = session.scalars(select(Observation).where(Observation.document_id == doc.id)).all()
    if existing:
        return list(existing)
    if doc.text_clean is None:
        if doc.text_raw is None:
            return []
        doc.text_clean = clean_text(doc.text_raw)

    if doc.source == "form_483":
        if "response" in str((doc.meta or {}).get("record_type", "")).lower():
            return []  # the firm's reply quotes each observation; not FDA's findings
        kind, segments = "483", split_483(doc.text_clean)
        parent = session.scalars(select(Form483).where(Form483.document_id == doc.id)).first()
        links = dict(form_483_id=parent.id if parent else None,
                     facility_id=parent.facility_id if parent else None,
                     inspection_id=parent.inspection_id if parent else None,
                     observed_date=parent.issue_date if parent else None)
    elif doc.source == "warning_letter":
        kind, segments = "wl", split_warning_letter(doc.text_clean)
        parent = session.scalars(select(WarningLetter).where(WarningLetter.document_id == doc.id)).first()
        links = dict(warning_letter_id=parent.id if parent else None,
                     facility_id=parent.facility_id if parent else None,
                     observed_date=parent.issue_date if parent else None)
    else:
        return []

    observations = []
    for seg in segments:
        obs = Observation(document_id=doc.id, kind=kind, number=seg.number, text=seg.text, **links)
        session.add(obs)
        session.flush()
        for draft in split_citations(seg.text):
            cfr = get_or_create_cfr(session, draft.cfr) if draft.cfr else None
            session.add(Citation(observation_id=obs.id, ordinal=draft.ordinal, text=draft.text,
                                 cfr_reference_id=cfr.id if cfr else None, created_by="rules"))
        observations.append(obs)
    session.flush()
    return observations


def _has_tag(session: Session, citation_id: int, mode_id: str, provenance: str) -> bool:
    return session.scalars(
        select(FailureMode.id).where(
            FailureMode.citation_id == citation_id,
            FailureMode.mode_id == mode_id,
            FailureMode.provenance == provenance,
        )
    ).first() is not None


def rules_tag_citation(session: Session, citation: Citation, taxonomy: Taxonomy) -> int:
    added = 0
    # Only the findings decide: FDA's request list mentions electronic records in most letters.
    electronic = has_electronic_cue(strip_request_tail(citation.text))
    for hit in tag_text(citation.text, taxonomy):
        if _has_tag(session, citation.id, hit.mode_id, "rules"):
            continue
        paper_only = taxonomy.modes[hit.mode_id].paper_records and not electronic
        session.add(FailureMode(citation_id=citation.id, mode_id=hit.mode_id, taxonomy_version=taxonomy.version,
                                provenance="rules", confidence=RULE_CONFIDENCE, evidence=hit.evidence,
                                paper_only=paper_only))
        added += 1
    return added


def _sibling_rows(session: Session, citation: Citation) -> list[FailureMode]:
    siblings = [c.id for c in citation.observation.citations if c.text == citation.text and c.id != citation.id]
    if not siblings:
        return []
    return list(session.scalars(select(FailureMode).where(
        FailureMode.citation_id.in_(siblings), FailureMode.provenance == "llm")))


def _llm_request(citation: Citation) -> tuple[str, str, str | None]:
    """(text, document kind, CFR sections) sent to the model. The CFR line
    lists every section the violation cites, since one call covers all
    sibling citations."""
    obs = citation.observation
    siblings = sorted((c for c in obs.citations if c.text == citation.text), key=lambda c: c.ordinal)
    cfrs = [c.cfr_reference.canonical for c in siblings if c.cfr_reference]
    # FDA's request list is not findings; leaving it out saves tokens and false positives.
    return strip_request_tail(citation.text), obs.kind, "; ".join(dict.fromkeys(cfrs)) or None


def _write_llm_tags(session: Session, citation: Citation, tags, tagger: LlmTagger) -> int:
    for t in tags:
        session.add(FailureMode(citation_id=citation.id, mode_id=t.mode_id, taxonomy_version=tagger.taxonomy.version,
                                provenance="llm", confidence=t.confidence, evidence=t.evidence, model=tagger.model,
                                paper_only=t.paper_only))
    if not tags:
        # Record that the LLM looked and proposed nothing, so re-runs skip it.
        session.add(FailureMode(citation_id=citation.id, mode_id="NONE", taxonomy_version=tagger.taxonomy.version,
                                provenance="llm", confidence=None, status="rejected", model=tagger.model,
                                reviewer="system", reviewed_at=utcnow()))
    session.flush()
    return len(tags)


def _copy_from_sibling(session: Session, citation: Citation, done: list[FailureMode]) -> int:
    source = min(f.citation_id for f in done)
    copied = 0
    for f in done:
        if f.citation_id != source:
            continue
        none = f.mode_id == "NONE"
        session.add(FailureMode(citation_id=citation.id, mode_id=f.mode_id, taxonomy_version=f.taxonomy_version,
                                provenance="llm", confidence=f.confidence, evidence=f.evidence, model=f.model,
                                paper_only=f.paper_only, status=f.status if none else "proposed",
                                reviewer=f.reviewer if none else None, reviewed_at=f.reviewed_at if none else None))
        copied += not none
    session.flush()
    return copied


def _has_llm(session: Session, citation: Citation) -> bool:
    return session.scalars(select(FailureMode.id).where(
        FailureMode.citation_id == citation.id, FailureMode.provenance == "llm")).first() is not None


def llm_tag_citation(session: Session, citation: Citation, tagger: LlmTagger) -> int:
    """Propose LLM tags for one citation. Sibling citations (same observation,
    same text, one per cited CFR section) share one model call: the first is
    sent with every section the violation cites, and later siblings copy its
    proposals instead of calling again."""
    if _has_llm(session, citation):
        return 0
    done = _sibling_rows(session, citation)
    if done:
        return _copy_from_sibling(session, citation, done)
    text, kind, cfr = _llm_request(citation)
    return _write_llm_tags(session, citation, tagger.tag(text, doc_kind=kind, cfr=cfr), tagger)


_RETRYABLE = (408, 409, 429, 500, 502, 503, 504, 529)


def llm_tag_all(session: Session, citations: list[Citation], tagger: LlmTagger, *, llm_max: int | None = None,
                workers: int = 1, stats: dict | None = None) -> dict:
    """LLM stage over many citations: one call per distinct violation text,
    up to ``llm_max`` calls, ``workers`` calls in flight at once. Model calls
    run in threads; every database write stays on this thread. Proposals are
    committed every 25 results so an interrupted paid run keeps them."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stats = stats if stats is not None else {"llm_tags": 0, "llm_errors": 0}
    stats.setdefault("llm_calls_skipped_siblings", 0)
    jobs: list[Citation] = []
    later: list[Citation] = []
    claimed: set[tuple[int, str]] = set()
    for c in citations:
        if _has_llm(session, c):
            continue
        key = (c.observation_id, c.text)
        if key in claimed or _sibling_rows(session, c):
            later.append(c)  # a sibling carries (or will carry) the call
            continue
        claimed.add(key)
        jobs.append(c)
    if llm_max is not None:
        jobs = jobs[:max(0, llm_max - tagger.usage.calls)]
    requests = {c.id: _llm_request(c) for c in jobs}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(tagger.tag, requests[c.id][0], doc_kind=requests[c.id][1], cfr=requests[c.id][2]): c
                   for c in jobs}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                stats["llm_tags"] += _write_llm_tags(session, c, fut.result(), tagger)
            except (LlmRefusal, RuntimeError, ValueError) as exc:
                stats["llm_errors"] += 1
                log.warning("LLM tagging failed for citation %s: %s", c.id, exc)
            except Exception as exc:
                # Rate limits and overloads (after the SDK's own retries) skip this
                # citation; it is retried next run. Auth and request errors abort.
                if getattr(exc, "status_code", None) in _RETRYABLE:
                    stats["llm_errors"] += 1
                    log.warning("LLM call failed for citation %s, will retry next run: %s", c.id, exc)
                else:
                    for f in futures:
                        f.cancel()
                    session.commit()
                    raise
            done += 1
            if done % 25 == 0:
                session.commit()  # paid proposals survive an interrupted run
                log.info("LLM: %d of %d calls done, $%.2f so far", done, len(jobs), tagger.usage.cost_usd)
    for c in later:
        if not _has_llm(session, c) and _sibling_rows(session, c):
            stats["llm_tags"] += _copy_from_sibling(session, c, _sibling_rows(session, c))
            stats["llm_calls_skipped_siblings"] += 1
    session.commit()
    return stats


class ResegmentRefused(RuntimeError):
    pass


def clear_segmentation(session: Session) -> int:
    """Drop observations, citations and machine-proposed tags so documents
    are re-segmented with the current code. Refused once any tag has been
    reviewed or any gold label exists: those attach to citation rows, and
    re-segmenting would orphan them."""
    from sqlalchemy import delete, func

    from ..models import GoldLabel

    reviewed = session.scalar(select(func.count()).select_from(FailureMode).where(
        (FailureMode.provenance == "human") | (FailureMode.reviewer.is_not(None)) & (FailureMode.reviewer != "system")))
    gold = session.scalar(select(func.count()).select_from(GoldLabel))
    if reviewed or gold:
        raise ResegmentRefused(f"{reviewed} reviewed tags and {gold} gold labels would be orphaned; not re-segmenting")
    n = session.scalar(select(func.count()).select_from(Observation)) or 0
    session.execute(delete(FailureMode))
    session.execute(delete(Citation))
    session.execute(delete(Observation))
    session.flush()
    return n


def run_pipeline(session: Session, taxonomy: Taxonomy, *, tagger: LlmTagger | None = None,
                 limit: int | None = None, llm_max: int | None = None, llm_workers: int = 1,
                 llm_since: date | None = None) -> dict:
    """``limit`` caps documents; ``llm_max`` caps LLM calls (one per distinct
    violation text); ``llm_workers`` sets how many calls run at once;
    ``llm_since`` sends only citations issued on or after that date to the
    LLM (rules still tag everything)."""
    stats = {"documents": 0, "observations": 0, "citations": 0, "rule_tags": 0, "llm_tags": 0, "llm_errors": 0}
    docs = session.scalars(
        select(Document).where(Document.is_current.is_(True), Document.source.in_(["form_483", "warning_letter"]))
        .order_by(Document.id)
    ).all()
    citations: list[Citation] = []
    for doc in docs[:limit] if limit else docs:
        stats["documents"] += 1
        for obs in segment_document(session, doc):
            stats["observations"] += 1
            for citation in obs.citations:
                stats["citations"] += 1
                stats["rule_tags"] += rules_tag_citation(session, citation, taxonomy)
                if llm_since is None or (obs.observed_date is not None and obs.observed_date >= llm_since):
                    citations.append(citation)
        session.flush()
    if tagger is not None:
        session.commit()
        llm_tag_all(session, citations, tagger, llm_max=llm_max, workers=llm_workers, stats=stats)
        stats["llm_usage"] = tagger.usage.as_dict()
    return stats
