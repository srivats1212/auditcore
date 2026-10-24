"""Stage 3: human review. Proposed tags become accepted or rejected.

A citation-mode pair may have both a rules and an LLM proposal; review
decides the pair once, and the decision applies to every proposal for it.
An accepted decision is also stored as a ``human`` provenance row so the
published dataset has one unambiguous record per (citation, mode).

Decisions are not final. Deciding a pair again (accept to reject, reject to
accept, or flipping ``paper_only``) updates the same rows, so every count
reads the current decision. Each change, including the first decision, is
appended to ``review_event`` with the previous and new decision, reviewer,
time, and an optional reason.

The CLI (``auditcore review``) and the local web review page both go through
``review_items`` and ``apply_decision``, so either interface sees and writes
the same state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Citation, FailureMode, Observation, ReviewEvent, utcnow

# "Low confidence" filter: the most confident proposal is below this.
LOW_CONFIDENCE = 0.7


def _state(rows: list[FailureMode]) -> tuple[str, bool | None, bool]:
    """(decision, paper_only, pending) for one citation's rows of one mode.
    decision is proposed, accepted or rejected; paper_only is set only when
    accepted; pending means a machine proposal still awaits a decision."""
    human = next((r for r in rows if r.provenance == "human"), None)
    machine = [r for r in rows if r.provenance != "human"]
    pending = any(r.status == "proposed" for r in machine)
    if human is not None and human.status in ("accepted", "rejected"):
        return human.status, (human.paper_only if human.status == "accepted" else None), pending
    if machine and not pending:
        return "rejected", None, False  # an accept always writes a human row
    return "proposed", None, pending


@dataclass
class ReviewItem:
    citation_id: int
    mode_id: str
    text: str
    proposals: list[FailureMode]  # machine (rules, LLM) rows for this pair, any status
    human: FailureMode | None = None
    llm_seen: bool = False  # the LLM stage has looked at this citation
    year: int | None = None  # issue year of the letter or 483
    _rows: list[FailureMode] = field(default_factory=list, repr=False)

    @property
    def best_confidence(self) -> float:
        return max((p.confidence or 0.0) for p in self.proposals)

    @property
    def paper_only(self) -> bool:
        """Proposed as paper-only if the most confident proposal says so."""
        best = max(self.proposals, key=lambda p: p.confidence or 0.0)
        return best.paper_only

    @property
    def sources(self) -> str:
        return "+".join(sorted({p.provenance for p in self.proposals}))

    @property
    def pending(self) -> bool:
        return _state(self._rows)[2]

    @property
    def decision(self) -> str:
        return _state(self._rows)[0]

    @property
    def decided_paper_only(self) -> bool | None:
        return _state(self._rows)[1]

    @property
    def effective_paper_only(self) -> bool:
        """The flag that counts: the reviewer's if accepted, else the proposal's."""
        decided = self.decided_paper_only
        return self.paper_only if decided is None else decided

    @property
    def disagreement(self) -> bool:
        """Rules and LLM disagree: the LLM looked at the citation and only one
        stage proposed this mode, or both did with different paper_only flags."""
        if not self.llm_seen:
            return False
        provs = {p.provenance for p in self.proposals}
        return provs != {"rules", "llm"} or len({p.paper_only for p in self.proposals}) > 1

    @property
    def reviewer(self) -> str | None:
        last = self._last_reviewed()
        return last.reviewer if last else None

    @property
    def reviewed_at(self) -> datetime | None:
        last = self._last_reviewed()
        return last.reviewed_at if last else None

    def _last_reviewed(self) -> FailureMode | None:
        done = [r for r in self._rows if r.reviewed_at is not None and r.reviewer not in (None, "system")]
        return max(done, key=lambda r: _aware(r.reviewed_at), default=None)


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; compare everything as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def review_items(session: Session, *, state: str = "pending", mode_id: str | None = None,
                 proposer: str | None = None, disagreements: bool = False, low_confidence: bool = False,
                 paper: str | None = None, year: int | None = None) -> list[ReviewItem]:
    """Review items, one per finding: sibling citations (same observation and
    text, one per cited CFR section) collapse into one item because a
    decision covers them all.

    state: pending (a proposal awaits a decision), decided, or all.
    proposer: rules, llm, or both. paper: paper or electronic (the flag that
    would count: the reviewer's if accepted, else the proposal's). year:
    issue year of the letter or 483."""
    q = (select(FailureMode, Citation.observation_id, Citation.text, Observation.observed_date)
         .join(Citation, FailureMode.citation_id == Citation.id)
         .join(Observation, Citation.observation_id == Observation.id)
         .where(FailureMode.mode_id != "NONE"))
    if mode_id:
        q = q.where(FailureMode.mode_id == mode_id)
    if year:
        q = q.where(Observation.observed_date >= date(year, 1, 1), Observation.observed_date < date(year + 1, 1, 1))
    groups: dict[tuple[int, str, str], dict[int, list[FailureMode]]] = {}
    texts: dict[int, str] = {}
    years: dict[int, int | None] = {}
    for fm, obs_id, text, observed in session.execute(q.order_by(FailureMode.citation_id, FailureMode.id)):
        groups.setdefault((obs_id, text, fm.mode_id), {}).setdefault(fm.citation_id, []).append(fm)
        texts[fm.citation_id] = text
        years[fm.citation_id] = observed.year if observed else None
    llm_seen = set(session.scalars(select(FailureMode.citation_id).where(FailureMode.provenance == "llm").distinct()))

    items: list[ReviewItem] = []
    for (_, _, mid), by_cit in groups.items():
        with_machine = [cid for cid, rows in by_cit.items() if any(r.provenance != "human" for r in rows)]
        if not with_machine:
            continue  # nothing was proposed; not a review item
        # Prefer a sibling that still has a pending proposal, so it surfaces.
        cid = next((c for c in with_machine if _state(by_cit[c])[2]), with_machine[0])
        rows = by_cit[cid]
        items.append(ReviewItem(cid, mid, texts[cid], [r for r in rows if r.provenance != "human"],
                                human=next((r for r in rows if r.provenance == "human"), None),
                                llm_seen=cid in llm_seen, year=years.get(cid), _rows=rows))

    if state == "pending":
        items = [i for i in items if i.pending]
    elif state == "decided":
        items = [i for i in items if not i.pending]
    if proposer in ("rules", "llm"):
        items = [i for i in items if i.sources == proposer]
    elif proposer == "both":
        items = [i for i in items if i.sources == "llm+rules"]
    if disagreements:
        items = [i for i in items if i.disagreement]
    if low_confidence:
        items = [i for i in items if i.best_confidence < LOW_CONFIDENCE]
    if paper in ("paper", "electronic"):
        items = [i for i in items if i.effective_paper_only == (paper == "paper")]

    if state == "decided":
        # Most recent decision first.
        items.sort(key=lambda i: (i.reviewed_at is None, -(_aware(i.reviewed_at).timestamp() if i.reviewed_at else 0)))
    else:
        # Agreement between rules and LLM first: fastest to clear, most likely correct.
        items.sort(key=lambda i: (not i.pending, -len({p.provenance for p in i.proposals}), -i.best_confidence))
    return items


def review_queue(session: Session, *, mode_id: str | None = None, limit: int = 50) -> list[ReviewItem]:
    """Pending items, in review order."""
    return review_items(session, state="pending", mode_id=mode_id)[:limit]


@dataclass
class Progress:
    total: int
    reviewed: int
    accepted: int
    rejected: int

    @property
    def precision(self) -> float | None:
        """Running precision of the proposals: accepted / decided."""
        decided = self.accepted + self.rejected
        return self.accepted / decided if decided else None


def progress(session: Session) -> Progress:
    items = review_items(session, state="all")
    done = [i for i in items if not i.pending]
    return Progress(total=len(items), reviewed=len(done),
                    accepted=sum(i.decision == "accepted" for i in done),
                    rejected=sum(i.decision == "rejected" for i in done))


def history(session: Session, citation_id: int, mode_id: str) -> list[ReviewEvent]:
    return list(session.scalars(select(ReviewEvent).where(
        ReviewEvent.citation_id == citation_id, ReviewEvent.mode_id == mode_id).order_by(ReviewEvent.id)))


def decide(session: Session, citation_id: int, mode_id: str, *, accept: bool, reviewer: str,
           evidence: str | None = None, paper_only: bool | None = None, reason: str | None = None,
           interface: str = "cli", _siblings: bool = True) -> bool:
    """Accept or reject one (citation, mode), or change an earlier decision.
    The same decision applies to sibling citations of the same observation
    with identical text (one per cited CFR section), so a reviewer decides
    each finding once.

    ``paper_only`` sets the flag on the accepted tag; None keeps what the
    most confident proposal said. Returns True if anything changed; a
    decision identical to the current one is a no-op and is not logged."""
    changed = False
    if _siblings:
        citation = session.get(Citation, citation_id)
        if citation is not None:
            siblings = session.scalars(select(Citation.id).where(
                Citation.observation_id == citation.observation_id, Citation.text == citation.text,
                Citation.id != citation_id)).all()
            for sid in siblings:
                has = session.scalars(select(FailureMode.id).where(
                    FailureMode.citation_id == sid, FailureMode.mode_id == mode_id)).first()
                if has is not None or accept:
                    changed |= decide(session, sid, mode_id, accept=accept, reviewer=reviewer, evidence=evidence,
                                      paper_only=paper_only, reason=reason, interface=interface, _siblings=False)
    now = utcnow()
    proposals = session.scalars(
        select(FailureMode).where(FailureMode.citation_id == citation_id, FailureMode.mode_id == mode_id)
    ).all()
    if not proposals and not accept:
        return changed
    before, before_paper, pending = _state(list(proposals))
    human = next((p for p in proposals if p.provenance == "human"), None)
    machine = [p for p in proposals if p.provenance != "human"]
    best = max(machine, key=lambda p: p.confidence or 0.0, default=None)
    if accept:
        flag = paper_only if paper_only is not None else (
            best.paper_only if best is not None else (human.paper_only if human is not None else False))
        after, after_paper = "accepted", flag
    else:
        after, after_paper = "rejected", None
    if (before, before_paper) == (after, after_paper) and not pending:
        return changed

    version = proposals[0].taxonomy_version if proposals else None
    for p in machine:
        p.status = after
        p.reviewer, p.reviewed_at = reviewer, now
    if accept:
        if human is None:
            if version is None:
                from ..taxonomy.loader import load_taxonomy

                version = load_taxonomy().version
            human = FailureMode(citation_id=citation_id, mode_id=mode_id, taxonomy_version=version,
                                provenance="human", confidence=1.0,
                                evidence=evidence or (best.evidence if best else None))
            session.add(human)
        human.paper_only = after_paper
        human.status, human.reviewer, human.reviewed_at = "accepted", reviewer, now
    elif human is not None:
        human.status, human.reviewer, human.reviewed_at = "rejected", reviewer, now
    session.add(ReviewEvent(citation_id=citation_id, mode_id=mode_id, previous_decision=before,
                            previous_paper_only=before_paper, new_decision=after, new_paper_only=after_paper,
                            reviewer=reviewer, reason=(reason or "").strip() or None, interface=interface,
                            created_at=now))
    session.flush()
    return True


def apply_decision(session: Session, citation_id: int, mode_id: str, *, accept: bool, reviewer: str,
                   paper_only: bool | None = None, reason: str | None = None, interface: str = "cli",
                   taxonomy_version: str | None = None) -> bool:
    """``decide`` plus a rebuild of the annual statistics, so trend views and
    the home page reflect the decision at once. Both review interfaces use this."""
    from ..analytics.stats import compute_annual_stats

    changed = decide(session, citation_id, mode_id, accept=accept, reviewer=reviewer, paper_only=paper_only,
                     reason=reason, interface=interface)
    if changed:
        if taxonomy_version is None:
            from ..taxonomy.loader import load_taxonomy

            taxonomy_version = load_taxonomy().version
        compute_annual_stats(session, taxonomy_version)
    return changed


def pending_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(FailureMode)
        .where(FailureMode.status == "proposed", FailureMode.mode_id != "NONE")
    ) or 0
