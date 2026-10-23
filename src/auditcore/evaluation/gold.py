"""Gold-label set and evaluation (PRD section 7).

Workflow
1. ``sample_gold`` picks ~300 citations, stratified so the known traps are
   covered (QMSR vocabulary, word-boundary bait, negation, adequate controls).
2. ``export_blind`` writes a CSV with citation text ONLY. No pipeline output,
   no CFR-derived hints, so the tagger cannot anchor on it.
3. Two taggers fill in ``mode_ids`` independently; ``import_labels`` loads each
   file under its tagger name ("A", "B").
4. ``agreement`` reports Cohen's kappa per mode; ``adjudicate`` auto-resolves
   agreements and exports disagreements for a third pass.
5. ``evaluate`` scores a pipeline against adjudicated labels and stores
   per-mode precision and recall in ``mode_evaluation``.
6. ``passing_modes`` is the gate the trend views use.
"""

from __future__ import annotations

import csv
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Citation, FailureMode, GoldLabel, ModeEvaluation, Observation, utcnow
from ..taxonomy.loader import Taxonomy
from ..text.cfr import QMSR_EFFECTIVE

ADJUDICATED = "adjudicated"
PIPELINES = ("rules", "llm", "proposed", "published")

TRAPS: dict[str, re.Pattern[str]] = {
    "word_boundary": re.compile(r"\b(cloudy|clouded|shared (drive|folder|area|equipment)|audit(ed|or)s?\b(?! trail)|"
                                r"backed by|server room temperature|accessories)\b", re.I),
    "negation": re.compile(r"\b(did not observe|no evidence|not observed|found no|no instances)\b", re.I),
    "adequate": re.compile(r"\b(was|were) (adequate|appropriate|in place|enabled and)\b|\badequately\b", re.I),
    "remediation_request": re.compile(r"\byour response should\b|\bwe recommend\b", re.I),
    "qmsr": re.compile(r"\b(QMSR|ISO 13485|820\.10|quality management system regulation)\b", re.I),
}


def detect_trap(text: str, observed_date=None) -> str | None:
    for name, pat in TRAPS.items():
        if pat.search(text):
            return name
    if observed_date is not None and observed_date >= QMSR_EFFECTIVE and re.search(r"\b820\.\d+", text):
        return "qmsr"
    return None


def sample_gold(session: Session, n: int = 300, *, seed: int = 13, trap_share: float = 0.3,
                rule_hit_share: float = 0.35) -> list[int]:
    """Stratified sample: traps, citations with any rule hit, and random fill.
    Citations already in the gold set are excluded."""
    rng = random.Random(seed)
    taken = set(session.scalars(select(GoldLabel.citation_id)))
    rows = session.execute(
        select(Citation.id, Citation.text, Observation.observed_date).join(Observation)
    ).all()
    pool = [r for r in rows if r.id not in taken]
    rule_hit = set(session.scalars(select(FailureMode.citation_id).where(FailureMode.provenance == "rules")))

    traps = [r.id for r in pool if detect_trap(r.text, r.observed_date)]
    hits = [r.id for r in pool if r.id in rule_hit]
    rng.shuffle(traps)
    rng.shuffle(hits)

    chosen: list[int] = []
    chosen_set: set[int] = set()
    for bucket, share in ((traps, trap_share), (hits, rule_hit_share)):
        quota, taken_here = int(n * share), 0
        for cid in bucket:
            if taken_here >= quota:
                break
            if cid not in chosen_set:
                chosen.append(cid)
                chosen_set.add(cid)
                taken_here += 1
    rest = [r.id for r in pool if r.id not in chosen_set]
    rng.shuffle(rest)
    chosen.extend(rest[: max(0, n - len(chosen))])
    return chosen[:n]


def export_blind(session: Session, citation_ids: Iterable[int], path: Path) -> int:
    ids = list(citation_ids)
    order = ids[:]
    random.Random(7).shuffle(order)  # decouple order from sampling strata
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["citation_id", "text", "mode_ids", "notes"])
        for cid in order:
            c = session.get(Citation, cid)
            if c is not None:
                w.writerow([cid, c.text, "", ""])
    return len(order)


def _parse_modes(cell: str) -> list[str]:
    return sorted({m.strip() for m in re.split(r"[;,\s]+", cell or "") if m.strip() and m.strip().upper() != "NONE"})


def import_labels(session: Session, path: Path, *, tagger: str, taxonomy: Taxonomy) -> int:
    if tagger == ADJUDICATED:
        raise ValueError("use adjudicate() to write adjudicated labels")
    n = 0
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            cid = int(row["citation_id"])
            modes = _parse_modes(row.get("mode_ids", ""))
            unknown = [m for m in modes if m not in taxonomy.modes]
            if unknown:
                raise ValueError(f"citation {cid}: unknown mode ids {unknown}")
            _upsert_label(session, cid, tagger, modes, taxonomy.version, row.get("notes") or None)
            n += 1
    session.flush()
    return n


def _upsert_label(session: Session, cid: int, tagger: str, modes: list[str], version: str, notes: str | None) -> None:
    label = session.scalars(select(GoldLabel).where(GoldLabel.citation_id == cid, GoldLabel.tagger == tagger)).first()
    citation = session.get(Citation, cid)
    trap = detect_trap(citation.text, citation.observation.observed_date) if citation else None
    if label is None:
        label = GoldLabel(citation_id=cid, tagger=tagger, taxonomy_version=version)
        session.add(label)
    label.mode_ids, label.notes, label.trap, label.labeled_at = modes, notes, trap, utcnow()


def _labels(session: Session, tagger: str) -> dict[int, set[str]]:
    return {g.citation_id: set(g.mode_ids or []) for g in session.scalars(select(GoldLabel).where(GoldLabel.tagger == tagger))}


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe == 1:
        return 1.0 if po == 1 else None
    return (po - pe) / (1 - pe)


def agreement(session: Session, taxonomy: Taxonomy, a: str = "A", b: str = "B") -> dict[str, dict]:
    la, lb = _labels(session, a), _labels(session, b)
    common = sorted(set(la) & set(lb))
    report: dict[str, dict] = {}
    for mode in taxonomy:
        xa = [mode.id in la[c] for c in common]
        xb = [mode.id in lb[c] for c in common]
        if any(xa) or any(xb):
            report[mode.id] = {"kappa": cohen_kappa(xa, xb), "a_pos": sum(xa), "b_pos": sum(xb),
                               "both": sum(x and y for x, y in zip(xa, xb))}
    exact = sum(la[c] == lb[c] for c in common)
    report["_overall"] = {"citations": len(common), "exact_agreement": exact / len(common) if common else None}
    return report


def adjudicate(session: Session, taxonomy: Taxonomy, *, a: str = "A", b: str = "B",
               disagreements_csv: Path | None = None, resolutions_csv: Path | None = None) -> dict[str, int]:
    """Agreements become adjudicated labels. Disagreements are written out for
    a third pass; a filled-in resolutions file is then imported."""
    la, lb = _labels(session, a), _labels(session, b)
    agreed = disputed = resolved = 0
    disputes = []
    for cid in sorted(set(la) & set(lb)):
        if la[cid] == lb[cid]:
            _upsert_label(session, cid, ADJUDICATED, sorted(la[cid]), taxonomy.version, None)
            agreed += 1
        else:
            disputes.append(cid)
            disputed += 1
    if disagreements_csv is not None and disputes:
        with disagreements_csv.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["citation_id", "text", f"{a}_modes", f"{b}_modes", "mode_ids", "notes"])
            for cid in disputes:
                w.writerow([cid, session.get(Citation, cid).text, ";".join(sorted(la[cid])), ";".join(sorted(lb[cid])), "", ""])
    if resolutions_csv is not None:
        with resolutions_csv.open(newline="") as fh:
            for row in csv.DictReader(fh):
                modes = _parse_modes(row.get("mode_ids", ""))
                unknown = [m for m in modes if m not in taxonomy.modes]
                if unknown:
                    raise ValueError(f"citation {row['citation_id']}: unknown mode ids {unknown}")
                _upsert_label(session, int(row["citation_id"]), ADJUDICATED, modes, taxonomy.version, row.get("notes") or None)
                resolved += 1
    session.flush()
    return {"agreed": agreed, "disputed": disputed, "resolved": resolved}


def predictions(session: Session, pipeline: str, citation_ids: Iterable[int]) -> dict[int, set[str]]:
    ids = list(citation_ids)
    q = select(FailureMode.citation_id, FailureMode.mode_id).where(
        FailureMode.citation_id.in_(ids), FailureMode.mode_id != "NONE",
        FailureMode.paper_only.is_(False),  # gold labels are computerized-systems modes only
    )
    if pipeline == "rules":
        q = q.where(FailureMode.provenance == "rules")
    elif pipeline == "llm":
        q = q.where(FailureMode.provenance == "llm")
    elif pipeline == "proposed":
        q = q.where(FailureMode.provenance.in_(["rules", "llm"]))
    elif pipeline == "published":
        q = q.where(FailureMode.provenance == "human", FailureMode.status == "accepted")
    else:
        raise ValueError(f"unknown pipeline {pipeline}")
    out: dict[int, set[str]] = defaultdict(set)
    for cid, mid in session.execute(q):
        out[cid].add(mid)
    return out


@dataclass
class ModeScore:
    mode_id: str
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def support(self) -> int:
        return self.tp + self.fn


def score(gold: dict[int, set[str]], pred: dict[int, set[str]], taxonomy: Taxonomy) -> dict[str, ModeScore]:
    scores = {m.id: ModeScore(m.id, 0, 0, 0) for m in taxonomy}
    for cid, truth in gold.items():
        guess = pred.get(cid, set())
        for mid in truth | guess:
            if mid not in scores:
                continue
            if mid in truth and mid in guess:
                scores[mid].tp += 1
            elif mid in guess:
                scores[mid].fp += 1
            else:
                scores[mid].fn += 1
    return scores


def score_by_trap(session: Session, pipeline: str) -> dict[str, dict[str, int]]:
    """Trap citations: how many did the pipeline tag when gold says nothing applies (false alarms)."""
    labels = list(session.scalars(select(GoldLabel).where(GoldLabel.tagger == ADJUDICATED)))
    pred = predictions(session, pipeline, [g.citation_id for g in labels])
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"citations": 0, "false_positive_tags": 0, "exact": 0})
    for g in labels:
        if not g.trap:
            continue
        truth, guess = set(g.mode_ids or []), pred.get(g.citation_id, set())
        out[g.trap]["citations"] += 1
        out[g.trap]["false_positive_tags"] += len(guess - truth)
        out[g.trap]["exact"] += int(truth == guess)
    return dict(out)


def evaluate(session: Session, taxonomy: Taxonomy, pipeline: str = "published") -> dict[str, ModeScore]:
    gold = _labels(session, ADJUDICATED)
    pred = predictions(session, pipeline, gold.keys())
    scores = score(gold, pred, taxonomy)
    now = utcnow()
    for mid, s in scores.items():
        row = session.scalars(select(ModeEvaluation).where(
            ModeEvaluation.mode_id == mid, ModeEvaluation.taxonomy_version == taxonomy.version,
            ModeEvaluation.pipeline == pipeline)).first()
        if row is None:
            row = ModeEvaluation(mode_id=mid, taxonomy_version=taxonomy.version, pipeline=pipeline)
            session.add(row)
        row.tp, row.fp, row.fn = s.tp, s.fp, s.fn
        row.precision, row.recall, row.support, row.evaluated_at = s.precision, s.recall, s.support, now
    session.flush()
    return scores


def passing_modes(session: Session, taxonomy_version: str, *, bar: float, pipeline: str = "published",
                  min_support: int = 3) -> set[str]:
    """Modes allowed into trend views. Undefined precision (no predictions on
    the gold set) or too little support fails the gate: a mode is shown only
    when there is evidence it is tagged correctly."""
    rows = session.scalars(select(ModeEvaluation).where(
        ModeEvaluation.taxonomy_version == taxonomy_version, ModeEvaluation.pipeline == pipeline))
    return {r.mode_id for r in rows if r.precision is not None and r.precision >= bar and r.support >= min_support}
