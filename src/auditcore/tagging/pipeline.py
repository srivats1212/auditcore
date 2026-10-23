"""Documents -> observations -> citations -> proposed tags.

Every step is idempotent: re-running on the same document version adds
nothing. Published counts only ever use tags with ``status="accepted"``.
"""

from __future__ import annotations

import logging
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
from .llm import LlmRefusal, LlmTagger
from .rules import tag_text

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
    for hit in tag_text(citation.text, taxonomy):
        if _has_tag(session, citation.id, hit.mode_id, "rules"):
            continue
        session.add(FailureMode(citation_id=citation.id, mode_id=hit.mode_id, taxonomy_version=taxonomy.version,
                                provenance="rules", confidence=RULE_CONFIDENCE, evidence=hit.evidence))
        added += 1
    return added


def llm_tag_citation(session: Session, citation: Citation, tagger: LlmTagger) -> int:
    already = session.scalars(
        select(FailureMode.id).where(FailureMode.citation_id == citation.id, FailureMode.provenance == "llm")
    ).first()
    if already is not None:
        return 0
    obs = citation.observation
    cfr = citation.cfr_reference.canonical if citation.cfr_reference else None
    tags = tagger.tag(citation.text, doc_kind=obs.kind, cfr=cfr)
    for t in tags:
        session.add(FailureMode(citation_id=citation.id, mode_id=t.mode_id, taxonomy_version=tagger.taxonomy.version,
                                provenance="llm", confidence=t.confidence, evidence=t.evidence, model=tagger.model))
    if not tags:
        # Record that the LLM looked and proposed nothing, so re-runs skip it.
        session.add(FailureMode(citation_id=citation.id, mode_id="NONE", taxonomy_version=tagger.taxonomy.version,
                                provenance="llm", confidence=None, status="rejected", model=tagger.model,
                                reviewer="system", reviewed_at=utcnow()))
    return len(tags)


def run_pipeline(session: Session, taxonomy: Taxonomy, *, tagger: LlmTagger | None = None,
                 limit: int | None = None, llm_max: int | None = None) -> dict:
    """``limit`` caps documents; ``llm_max`` caps LLM calls (one per citation)."""
    stats = {"documents": 0, "observations": 0, "citations": 0, "rule_tags": 0, "llm_tags": 0, "llm_errors": 0}
    docs = session.scalars(
        select(Document).where(Document.is_current.is_(True), Document.source.in_(["form_483", "warning_letter"]))
        .order_by(Document.id)
    ).all()
    for doc in docs[:limit] if limit else docs:
        stats["documents"] += 1
        for obs in segment_document(session, doc):
            stats["observations"] += 1
            for citation in obs.citations:
                stats["citations"] += 1
                stats["rule_tags"] += rules_tag_citation(session, citation, taxonomy)
                if tagger is not None and (llm_max is None or tagger.usage.calls < llm_max):
                    try:
                        stats["llm_tags"] += llm_tag_citation(session, citation, tagger)
                    except (LlmRefusal, RuntimeError, ValueError) as exc:
                        stats["llm_errors"] += 1
                        log.warning("LLM tagging failed for citation %s: %s", citation.id, exc)
                    except Exception as exc:
                        # Rate limits and overloads (after the SDK's own retries) skip this
                        # citation; it is retried next run. Auth and request errors abort.
                        if getattr(exc, "status_code", None) in (408, 409, 429, 500, 502, 503, 504, 529):
                            stats["llm_errors"] += 1
                            log.warning("LLM call failed for citation %s, will retry next run: %s", citation.id, exc)
                        else:
                            raise
        session.flush()
    if tagger is not None:
        stats["llm_usage"] = tagger.usage.as_dict()
    return stats
