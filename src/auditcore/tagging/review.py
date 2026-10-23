"""Stage 3: human review. Proposed tags become accepted or rejected.

A citation-mode pair may have both a rules and an LLM proposal; review
decides the pair once, and the decision applies to every proposal for it.
An accepted decision is also stored as a ``human`` provenance row so the
published dataset has one unambiguous record per (citation, mode).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Citation, FailureMode, utcnow


@dataclass
class ReviewItem:
    citation_id: int
    mode_id: str
    text: str
    proposals: list[FailureMode]

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


def review_queue(session: Session, *, mode_id: str | None = None, limit: int = 50) -> list[ReviewItem]:
    q = select(FailureMode).where(FailureMode.status == "proposed", FailureMode.mode_id != "NONE")
    if mode_id:
        q = q.where(FailureMode.mode_id == mode_id)
    q = q.order_by(FailureMode.citation_id, FailureMode.mode_id)
    grouped: dict[tuple[int, str], list[FailureMode]] = {}
    for fm in session.scalars(q):
        grouped.setdefault((fm.citation_id, fm.mode_id), []).append(fm)
        if len(grouped) > limit:
            grouped.popitem()
            break
    items = []
    seen: set[tuple[int, str, str]] = set()
    for (cid, mid), props in grouped.items():
        citation = session.get(Citation, cid)
        if citation is None:
            continue
        key = (citation.observation_id, citation.text, mid)
        if key in seen:  # sibling citation; decide() covers it
            continue
        seen.add(key)
        items.append(ReviewItem(cid, mid, citation.text, props))
    # Agreement between rules and LLM first: fastest to clear, most likely correct.
    items.sort(key=lambda i: (-len({p.provenance for p in i.proposals}), -i.best_confidence))
    return items


def decide(session: Session, citation_id: int, mode_id: str, *, accept: bool, reviewer: str,
           evidence: str | None = None, paper_only: bool | None = None, _siblings: bool = True) -> None:
    """Accept or reject one (citation, mode). The same decision applies to
    sibling citations of the same observation with identical text (one per
    cited CFR section), so a reviewer decides each finding once.

    ``paper_only`` sets the flag on the accepted tag; None keeps what the
    most confident proposal said."""
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
                    decide(session, sid, mode_id, accept=accept, reviewer=reviewer, evidence=evidence,
                           paper_only=paper_only, _siblings=False)
    now = utcnow()
    proposals = session.scalars(
        select(FailureMode).where(FailureMode.citation_id == citation_id, FailureMode.mode_id == mode_id)
    ).all()
    if not proposals and not accept:
        return
    version = proposals[0].taxonomy_version if proposals else None
    for p in proposals:
        if p.provenance == "human":
            continue
        p.status = "accepted" if accept else "rejected"
        p.reviewer, p.reviewed_at = reviewer, now

    human = next((p for p in proposals if p.provenance == "human"), None)
    if accept:
        machine = [p for p in proposals if p.provenance != "human"]
        best = max(machine, key=lambda p: p.confidence or 0.0, default=None)
        if human is None:
            if version is None:
                from ..taxonomy.loader import load_taxonomy

                version = load_taxonomy().version
            human = FailureMode(citation_id=citation_id, mode_id=mode_id, taxonomy_version=version,
                                provenance="human", confidence=1.0,
                                evidence=evidence or (best.evidence if best else None))
            session.add(human)
        if paper_only is not None:
            human.paper_only = paper_only
        elif best is not None:
            human.paper_only = best.paper_only
        human.status, human.reviewer, human.reviewed_at = "accepted", reviewer, now
    elif human is not None:
        human.status, human.reviewer, human.reviewed_at = "rejected", reviewer, now
    session.flush()


def pending_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(FailureMode)
        .where(FailureMode.status == "proposed", FailureMode.mode_id != "NONE")
    ) or 0
