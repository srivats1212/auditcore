"""Trend analytics (PRD section 8).

Counting rules, stated on every published statistic:
- the unit is the citation, not the document;
- failure-mode totals count each violation once; the CFR breakdown counts
  each section cited. A violation is one numbered warning-letter violation or
  one numbered 483 observation (an ``observation`` row);
- only human-accepted tags count;
- paper-only data-integrity tags (``paper_only``) never count; they stay
  searchable but are not computerized-systems findings;
- only current document versions count (superseded versions are excluded);
- a citation is counted once per mode even if rules and LLM both proposed it;
- year = calendar year of the 483 issue date or warning-letter issue date.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    CfrReference,
    Citation,
    Document,
    FailureMode,
    ModeEvaluation,
    Observation,
    ObservationStatAnnual,
    utcnow,
)
from ..text.cfr import regime_for


def _accepted_rows(session: Session, taxonomy_version: str):
    return session.execute(
        select(
            FailureMode.mode_id,
            Citation.id.label("citation_id"),
            Observation.id.label("observation_id"),
            Observation.kind,
            Observation.observed_date,
            Observation.document_id,
            Observation.facility_id,
            CfrReference.section,
            CfrReference.part,
        )
        .join(Citation, FailureMode.citation_id == Citation.id)
        .join(Observation, Citation.observation_id == Observation.id)
        .join(Document, Observation.document_id == Document.id)
        .outerjoin(CfrReference, Citation.cfr_reference_id == CfrReference.id)
        .where(
            FailureMode.provenance == "human",
            FailureMode.status == "accepted",
            FailureMode.paper_only.is_(False),
            FailureMode.taxonomy_version == taxonomy_version,
            Document.is_current.is_(True),
            Observation.observed_date.is_not(None),
        )
    ).all()


def compute_annual_stats(session: Session, taxonomy_version: str) -> int:
    groups: dict[tuple, dict[str, set]] = defaultdict(lambda: {"c": set(), "d": set(), "f": set()})
    for r in _accepted_rows(session, taxonomy_version):
        sections = ["", r.section or ""] if r.section else [""]  # "" row = all CFR sections combined
        for sec in sections:
            g = groups[(r.observed_date.year, r.mode_id, sec, r.kind)]
            g["c"].add(r.observation_id if sec == "" else r.citation_id)
            g["d"].add(r.document_id)
            if r.facility_id:
                g["f"].add(r.facility_id)
    session.execute(delete(ObservationStatAnnual).where(ObservationStatAnnual.taxonomy_version == taxonomy_version))
    now = utcnow()
    for (year, mode_id, sec, kind), g in groups.items():
        session.add(ObservationStatAnnual(
            year=year, mode_id=mode_id, cfr_section=sec, kind=kind, taxonomy_version=taxonomy_version,
            citation_count=len(g["c"]), document_count=len(g["d"]), facility_count=len(g["f"]), computed_at=now))
    session.flush()
    return len(groups)


def mode_trend(session: Session, mode_id: str, taxonomy_version: str) -> dict[int, dict[str, int]]:
    rows = session.scalars(select(ObservationStatAnnual).where(
        ObservationStatAnnual.mode_id == mode_id, ObservationStatAnnual.cfr_section == "",
        ObservationStatAnnual.taxonomy_version == taxonomy_version))
    out: dict[int, dict[str, int]] = defaultdict(lambda: {"483": 0, "wl": 0})
    for r in rows:
        out[r.year][r.kind] = r.citation_count
    return dict(sorted(out.items()))


def cfr_breakdown(session: Session, mode_id: str, taxonomy_version: str) -> list[tuple[str, int]]:
    totals: dict[str, int] = defaultdict(int)
    for r in session.scalars(select(ObservationStatAnnual).where(
            ObservationStatAnnual.mode_id == mode_id, ObservationStatAnnual.cfr_section != "",
            ObservationStatAnnual.taxonomy_version == taxonomy_version)):
        totals[r.cfr_section] += r.citation_count
    return sorted(totals.items(), key=lambda kv: -kv[1])


def top_modes(session: Session, taxonomy_version: str, *, since_year: int, allowed: set[str] | None = None,
              limit: int = 10) -> list[tuple[str, int]]:
    totals: dict[str, int] = defaultdict(int)
    for r in session.scalars(select(ObservationStatAnnual).where(
            ObservationStatAnnual.cfr_section == "", ObservationStatAnnual.year >= since_year,
            ObservationStatAnnual.taxonomy_version == taxonomy_version)):
        if allowed is None or r.mode_id in allowed:
            totals[r.mode_id] += r.citation_count
    return sorted(totals.items(), key=lambda kv: -kv[1])[:limit]


@dataclass
class Escalation:
    facility_id: int
    mode_id: str
    first_483: date
    warning_letter: date
    days: int


def escalations(session: Session, taxonomy_version: str, *, window_days: int = 730) -> list[Escalation]:
    """A 483 finding escalates when the same facility (matched by FEI-backed
    facility row) receives a warning letter citing the same failure mode
    within ``window_days`` after the 483."""
    first_483: dict[tuple[int, str], date] = {}
    wls: dict[tuple[int, str], list[date]] = defaultdict(list)
    for r in _accepted_rows(session, taxonomy_version):
        if r.facility_id is None:
            continue
        key = (r.facility_id, r.mode_id)
        if r.kind == "483":
            if key not in first_483 or r.observed_date < first_483[key]:
                first_483[key] = r.observed_date
        else:
            wls[key].append(r.observed_date)
    out = []
    for key, d483 in first_483.items():
        later = sorted(d for d in wls.get(key, []) if 0 <= (d - d483).days <= window_days)
        if later:
            out.append(Escalation(key[0], key[1], d483, later[0], (later[0] - d483).days))
    return sorted(out, key=lambda e: (e.mode_id, e.days))


def escalation_rates(session: Session, taxonomy_version: str, *, window_days: int = 730) -> dict[str, dict]:
    """Per mode: facilities with a 483 citing it, and the share that later got
    a warning letter citing it. Only meaningful for facilities whose 483 is
    published, which the methodology note must say."""
    base: dict[str, set[int]] = defaultdict(set)
    for r in _accepted_rows(session, taxonomy_version):
        if r.kind == "483" and r.facility_id is not None:
            base[r.mode_id].add(r.facility_id)
    esc: dict[str, set[int]] = defaultdict(set)
    for e in escalations(session, taxonomy_version, window_days=window_days):
        esc[e.mode_id].add(e.facility_id)
    return {m: {"facilities_483": len(f), "escalated": len(esc[m]), "rate": len(esc[m]) / len(f)}
            for m, f in base.items() if f}


def regime_split(session: Session, taxonomy_version: str) -> dict[str, int]:
    """Device citations split by QSR vs QMSR (date-resolved)."""
    out: dict[str, int] = defaultdict(int)
    for r in _accepted_rows(session, taxonomy_version):
        if r.part == "820":
            out[regime_for("820", r.observed_date) or "QSR"] += 1
    return dict(out)


@dataclass
class MethodologyNote:
    corpus: str
    date_range: tuple[int | None, int | None]
    counting: str
    evaluation: dict[str, dict]

    def render(self) -> str:
        lo, hi = self.date_range
        evals = "; ".join(
            f"{m}: precision {e['precision']:.2f}, recall {e['recall']:.2f} (n={e['support']})"
            if e.get("precision") is not None and e.get("recall") is not None else f"{m}: not yet evaluated"
            for m, e in self.evaluation.items()
        )
        return (f"Corpus: {self.corpus}. Years: {lo} to {hi}. Counting: {self.counting}. "
                f"Gold-set scores: {evals or 'none'}.")


def methodology_note(session: Session, taxonomy_version: str, mode_ids: list[str],
                     pipeline: str = "published") -> MethodologyNote:
    years = [y for y in session.scalars(select(ObservationStatAnnual.year).where(
        ObservationStatAnnual.taxonomy_version == taxonomy_version))]
    evals = {}
    for mid in mode_ids:
        e = session.scalars(select(ModeEvaluation).where(
            ModeEvaluation.mode_id == mid, ModeEvaluation.taxonomy_version == taxonomy_version,
            ModeEvaluation.pipeline == pipeline)).first()
        evals[mid] = {"precision": e.precision, "recall": e.recall, "support": e.support} if e else {}
    return MethodologyNote(
        corpus="published FDA warning letters and published Form 483s only; most 483s are never published",
        date_range=(min(years) if years else None, max(years) if years else None),
        counting="failure-mode totals count each violation once; the CFR breakdown counts each section cited; human-accepted tags only; paper-only data-integrity findings excluded; current document versions",
        evaluation=evals,
    )
