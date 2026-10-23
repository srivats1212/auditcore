"""Monthly editorial digest (PRD section 10).

``draft_issue`` writes a Markdown skeleton for one month with the data
sections pre-filled: trend deltas and candidate citations for deep dives.
The editor writes the deep dives and voice. ``publish_issue`` refuses to
publish a draft that still has editor placeholders or fails the copy lint,
then renders HTML for the web archive and an email-ready HTML file.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import markdown
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..copy_lint import assert_clean
from ..models import Citation, Document, FailureMode, Observation
from ..remediation.patterns import load_patterns
from ..taxonomy.loader import Taxonomy

PLACEHOLDER = "[EDITOR:"
_FRONT = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    return start, end


def monthly_counts(session: Session, taxonomy_version: str, start: date, end: date,
                   allowed: set[str] | None) -> dict[str, int]:
    rows = session.execute(
        select(FailureMode.mode_id, Citation.id)
        .join(Citation, FailureMode.citation_id == Citation.id)
        .join(Observation, Citation.observation_id == Observation.id)
        .join(Document, Observation.document_id == Document.id)
        .where(FailureMode.provenance == "human", FailureMode.status == "accepted",
               FailureMode.taxonomy_version == taxonomy_version, Document.is_current.is_(True),
               Observation.observed_date >= start, Observation.observed_date < end)
    ).all()
    out: dict[str, set[int]] = defaultdict(set)
    for mode_id, cid in rows:
        if allowed is None or mode_id in allowed:
            out[mode_id].add(cid)
    return {m: len(c) for m, c in out.items()}


def trend_deltas(session: Session, taxonomy: Taxonomy, year: int, month: int,
                 allowed: set[str] | None) -> list[dict]:
    start, end = _month_bounds(year, month)
    base_start = date(year - 1, month, 1)
    this = monthly_counts(session, taxonomy.version, start, end, allowed)
    base = monthly_counts(session, taxonomy.version, base_start, start, allowed)
    rows = []
    for mid in set(this) | set(base):
        avg = base.get(mid, 0) / 12
        rows.append({"mode_id": mid, "name": taxonomy.modes[mid].name if mid in taxonomy.modes else mid,
                     "this_month": this.get(mid, 0), "trailing_avg": round(avg, 1),
                     "delta": round(this.get(mid, 0) - avg, 1)})
    return sorted(rows, key=lambda r: -abs(r["delta"]))


def deep_dive_candidates(session: Session, taxonomy_version: str, year: int, month: int, limit: int = 8) -> list[dict]:
    start, end = _month_bounds(year, month)
    rows = session.execute(
        select(Citation.id, Citation.text, FailureMode.mode_id, Observation.kind)
        .join(Citation, FailureMode.citation_id == Citation.id)
        .join(Observation, Citation.observation_id == Observation.id)
        .where(FailureMode.provenance == "human", FailureMode.status == "accepted",
               FailureMode.taxonomy_version == taxonomy_version,
               Observation.observed_date >= start, Observation.observed_date < end)
        .order_by(Observation.kind.desc(), Citation.id)
    ).all()
    seen, out = set(), []
    for cid, text, mid, kind in rows:
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"citation_id": cid, "mode_id": mid, "kind": kind, "excerpt": text[:400]})
        if len(out) >= limit:
            break
    return out


def draft_issue(session: Session, taxonomy: Taxonomy, year: int, month: int, *, out_dir: Path,
                remediation_dir: Path, allowed: set[str] | None) -> Path:
    deltas = trend_deltas(session, taxonomy, year, month, allowed)
    dives = deep_dive_candidates(session, taxonomy.version, year, month)
    patterns = {m: p for m, p in load_patterns(remediation_dir).items() if p.publishable}
    spotlight = next((d["mode_id"] for d in deltas if d["mode_id"] in patterns), None)

    title = date(year, month, 1).strftime("%B %Y")
    lines = [
        "---",
        yaml.safe_dump({"issue": f"{year:04d}-{month:02d}", "title": f"Computerized-systems enforcement: {title}",
                        "status": "draft"}, sort_keys=False).strip(),
        "---",
        "",
        "## What moved this month",
        "",
        "| Failure mode | This month | 12-month monthly average | Change |",
        "|---|---:|---:|---:|",
    ]
    for d in deltas[:10]:
        sign = "+" if d["delta"] > 0 else ""
        lines.append(f"| {d['name']} ({d['mode_id']}) | {d['this_month']} | {d['trailing_avg']} | {sign}{d['delta']} |")
    if not deltas:
        lines.append("| No accepted citations this month | 0 | 0 | 0 |")
    lines += ["", f"{PLACEHOLDER} two or three sentences on what the movement means operationally.]", "",
              "## Deep dives", ""]
    for i in range(3):
        lines.append(f"### {PLACEHOLDER} deep dive {i + 1} title]")
        lines.append("")
        lines.append(f"{PLACEHOLDER} write the deep dive. Candidates are listed below.]")
        lines.append("")
    lines.append("<!-- Candidate citations (remove before publishing):")
    for d in dives:
        excerpt = d["excerpt"].replace("--", "-").replace("\n", " ")
        lines.append(f"  citation {d['citation_id']} [{d['kind']}] {d['mode_id']}: {excerpt}")
    lines.append("-->")
    lines += ["", "## Remediation spotlight", ""]
    if spotlight:
        p = patterns[spotlight]
        lines.append(f"**{p.title}** ({spotlight}). [Read the full pattern](/modes/{spotlight}).")
        lines.append("")
        lines.append(f"{PLACEHOLDER} one paragraph on why this fix matters now.]")
    else:
        lines.append(f"{PLACEHOLDER} pick a published remediation pattern to spotlight.]")
    lines += ["", "## Methodology", "",
              "Counts are citations in published warning letters and published Form 483s, tagged at citation level "
              "and reviewed by a person. Most 483s are never published. Only failure modes that meet the "
              f"{get_settings().precision_bar:.2f} precision bar on our gold set appear. Full method: /methodology.", ""]

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{year:04d}-{month:02d}.md"
    if path.exists():
        raise FileExistsError(f"{path} exists; not overwriting an editor draft")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def read_issue(path: Path) -> tuple[dict, str]:
    m = _FRONT.match(path.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"{path}: missing front matter")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def render_issue_html(body_md: str) -> str:
    body = re.sub(r"<!--.*?-->", "", body_md, flags=re.S)
    return markdown.markdown(body, extensions=["extra", "tables"])


def publish_issue(path: Path, *, email_dir: Path) -> Path:
    meta, body = read_issue(path)
    if PLACEHOLDER in re.sub(r"<!--.*?-->", "", body, flags=re.S):
        raise ValueError(f"{path}: editor placeholders remain")
    assert_clean(body, str(path))
    assert_clean(meta.get("title", ""), f"{path}:title")
    html = render_issue_html(body)
    email_dir.mkdir(parents=True, exist_ok=True)
    out = email_dir / f"{meta['issue']}.html"
    out.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{meta['title']}</title></head>"
        f"<body style='font-family:Georgia,serif;max-width:640px;margin:auto;line-height:1.5'>"
        f"<h1>{meta['title']}</h1>{html}</body></html>",
        encoding="utf-8",
    )
    text = path.read_text(encoding="utf-8").replace("status: draft", "status: published", 1)
    path.write_text(text, encoding="utf-8")
    return out


def archive(digest_dir: Path) -> list[dict]:
    issues = []
    for p in sorted(digest_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].md"), reverse=True):
        meta, body = read_issue(p)
        if meta.get("status") == "published":
            issues.append({**meta, "path": p, "body": body})
    return issues
