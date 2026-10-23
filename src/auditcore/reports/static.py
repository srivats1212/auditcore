"""Static, self-contained HTML report and CSV export of the ingested corpus.

Internal working view: it opens from disk with no server and no network, and
it may show unreviewed machine proposals (labelled as such). The public site
(`auditcore serve`) is where the publication gate applies.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CfrReference, Citation, Document, Facility, FailureMode, Form483, Observation, WarningLetter
from ..taxonomy.loader import Taxonomy

# Warning letters from these issuing offices are in scope (GxP drugs, biologics,
# devices). Food and veterinary letters also say "CGMP" but cite 21 CFR 117/507.
IN_SCOPE_OFFICES = re.compile(r"\b(CDER|CBER|Devices and Radiological Health|Drug Evaluation|Biologics)\b", re.I)
OUT_OF_SCOPE_PARTS = re.compile(r"^(1|10[0-9]|11[0-9]|50[0-9]|1[0-9]{3})$")  # food, labeling, radiation parts


@dataclass
class Row:
    citation_id: int
    observation_id: int
    kind: str
    number: int
    text: str
    cfr: str | None
    cfr_section: str | None
    cfr_part: str | None
    observed: date | None
    facility: str | None
    fei: str | None
    establishment: str | None
    office: str | None
    subject: str | None
    source_url: str | None
    source_key: str
    in_scope: bool = True
    proposed: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    # Paper-only data-integrity findings: listed, never counted as computerized systems.
    proposed_paper: list[str] = field(default_factory=list)
    accepted_paper: list[str] = field(default_factory=list)


def _scope(kind: str, office: str | None, cfr_part: str | None) -> bool:
    if kind == "483":
        return True  # the OII reading room is drug-only today
    if office and IN_SCOPE_OFFICES.search(office):
        return True
    if office is None and cfr_part and not OUT_OF_SCOPE_PARTS.match(cfr_part):
        return True
    return False


def load_rows(session: Session) -> list[Row]:
    q = (
        select(Citation, Observation, Document, CfrReference, Facility, WarningLetter, Form483)
        .join(Observation, Citation.observation_id == Observation.id)
        .join(Document, Observation.document_id == Document.id)
        .outerjoin(CfrReference, Citation.cfr_reference_id == CfrReference.id)
        .outerjoin(Facility, Observation.facility_id == Facility.id)
        .outerjoin(WarningLetter, Observation.warning_letter_id == WarningLetter.id)
        .outerjoin(Form483, Observation.form_483_id == Form483.id)
        .where(Document.is_current.is_(True))
        .order_by(Observation.observed_date.desc().nulls_last(), Citation.id)
    )
    rows: dict[int, Row] = {}
    for cit, obs, doc, cfr, fac, wl, f483 in session.execute(q):
        office = wl.issuing_office if wl else None
        rows[cit.id] = Row(
            citation_id=cit.id, observation_id=obs.id, kind=obs.kind, number=obs.number, text=cit.text,
            cfr=cfr.canonical if cfr else None, cfr_section=cfr.section if cfr else None,
            cfr_part=cfr.part if cfr else None, observed=obs.observed_date,
            facility=(fac.name if fac else None) or (wl.company_name if wl else None) or (f483.company_name if f483 else None),
            fei=fac.fei if fac else None, establishment=fac.establishment_type if fac else None,
            office=office, subject=wl.subject if wl else None,
            source_url=doc.source_url or (wl.letter_url if wl else None) or (f483.pdf_url if f483 else None),
            source_key=doc.source_key,
        )
    for r in rows.values():
        r.in_scope = _scope(r.kind, r.office, r.cfr_part)
    for cid, mid, prov, status, paper in session.execute(
            select(FailureMode.citation_id, FailureMode.mode_id, FailureMode.provenance, FailureMode.status,
                   FailureMode.paper_only)
            .where(FailureMode.mode_id != "NONE")):
        r = rows.get(cid)
        if r is None:
            continue
        if prov == "human" and status == "accepted":
            (r.accepted_paper if paper else r.accepted).append(mid)
        elif prov in ("rules", "llm") and status != "rejected":
            target = r.proposed_paper if paper else r.proposed
            if mid not in target:
                target.append(mid)
    for r in rows.values():
        # Rules and LLM can disagree on the flag; until review, a computerized-systems reading wins.
        r.proposed_paper = [m for m in r.proposed_paper if m not in r.proposed]
    return list(rows.values())


def export_csv(rows: list[Row], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["citation_id", "observation_id", "document_type", "observation_number", "issue_date",
                    "facility", "fei", "establishment_type", "issuing_office", "subject", "in_scope",
                    "cfr_cited", "proposed_modes_unreviewed", "accepted_modes", "paper_only_proposed",
                    "paper_only_accepted", "source_url", "source_key", "text"])
        for r in rows:
            w.writerow([r.citation_id, r.observation_id, "Form 483" if r.kind == "483" else "Warning letter",
                        r.number, r.observed.isoformat() if r.observed else "", r.facility or "", r.fei or "",
                        r.establishment or "", r.office or "", r.subject or "", "yes" if r.in_scope else "no",
                        r.cfr or "", ";".join(sorted(r.proposed)), ";".join(sorted(r.accepted)),
                        ";".join(sorted(r.proposed_paper)), ";".join(sorted(r.accepted_paper)),
                        r.source_url or "", r.source_key, r.text])
    return len(rows)


# ---------------------------------------------------------------- HTML

CSS = """
:root { --bg:#f6f5f2; --surface:#fcfcfb; --border:#dedcd6; --text:#0b0b0b; --text2:#52514e; --muted:#77756f;
  --accent:#1c5cab; --bar:#2a78d6; --bar2:#eb6834; --warn-bg:#fff4e5; --warn:#8a4b00; --tag:#e8f0fb; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) { color-scheme:dark;
  --bg:#121211; --surface:#1a1a19; --border:#33332f; --text:#f0efec; --text2:#c3c2b7; --muted:#8f8e86;
  --accent:#86b6ef; --bar:#3987e5; --bar2:#d95926; --warn-bg:#2b2112; --warn:#f0c37a; --tag:#1d2a3b; } }
:root[data-theme="dark"] { color-scheme:dark; --bg:#121211; --surface:#1a1a19; --border:#33332f; --text:#f0efec;
  --text2:#c3c2b7; --muted:#8f8e86; --accent:#86b6ef; --bar:#3987e5; --bar2:#d95926; --warn-bg:#2b2112;
  --warn:#f0c37a; --tag:#1d2a3b; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1000px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:1.55rem; line-height:1.25; margin:0 0 4px; }
h2 { font-size:1.1rem; margin:32px 0 8px; }
a { color:var(--accent); }
.muted { color:var(--text2); } .small { font-size:.85rem; }
.banner { background:var(--warn-bg); color:var(--warn); border-radius:8px; padding:10px 14px; margin:16px 0; font-size:.9rem; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:16px 0; }
.tile { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px; }
.tile .v { font-size:1.5rem; font-weight:650; font-variant-numeric:tabular-nums; }
.tile .l { font-size:.8rem; color:var(--text2); }
.card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:14px; margin:10px 0; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); vertical-align:top; }
th { color:var(--text2); font-weight:600; font-size:.8rem; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.barcell { width:32%; min-width:110px; }
.bar { height:10px; border-radius:0 4px 4px 0; background:var(--bar); min-width:2px; }
.bar.alt { background:var(--bar2); }
.tag { display:inline-block; background:var(--tag); border-radius:4px; padding:0 6px; margin:2px 4px 2px 0; font-size:.78rem; }
.meta { font-size:.82rem; color:var(--text2); margin-bottom:6px; }
.excerpt { white-space:pre-wrap; font-size:.88rem; }
.note { font-size:.82rem; color:var(--text2); border-left:3px solid var(--border); padding-left:10px; margin-top:8px; }
code { font-size:.85em; }
@media (max-width:600px) { td.barcell, th.barhead { display:none; } th, td { padding:5px 4px; } }
"""


def _bar_rows(items: list[tuple[str, str, int, int]], max_v: int, alt: bool = False) -> str:
    out = []
    for label, sub, v, v2 in items:
        w = 100 * v / max_v if max_v else 0
        out.append(f"<tr><td>{label}<div class='small muted'>{sub}</div></td>"
                   f"<td class='barcell'><div class='bar{' alt' if alt else ''}' style='width:{w:.1f}%' "
                   f"title='{escape(re.sub('<[^>]+>', '', label))}: {v}'></div></td>"
                   f"<td class='n'>{v}</td><td class='n'>{v2}</td></tr>")
    return "".join(out)


def build_report(session: Session, taxonomy: Taxonomy, out_dir: Path, *, samples: int = 10) -> dict[str, Path]:
    rows = load_rows(session)
    scope = [r for r in rows if r.in_scope]
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "citations.csv"
    export_csv(rows, csv_path)

    use_accepted = any(r.accepted for r in scope)
    tags_of = (lambda r: r.accepted) if use_accepted else (lambda r: r.proposed)
    paper_of = (lambda r: r.accepted_paper) if use_accepted else (lambda r: r.proposed_paper)
    basis = ("human-accepted tags" if use_accepted
             else "machine-proposed tags from the rules stage, not yet reviewed by a person")

    docs = {(r.kind, r.source_key) for r in scope}
    excluded_docs = {(r.kind, r.source_key) for r in rows if not r.in_scope} - docs
    n_obs = len({r.observation_id for r in scope})
    dates = [r.observed for r in scope if r.observed]

    # Approved counting rule: failure-mode totals count each violation (observation) once;
    # the CFR breakdown counts each section cited.
    mode_obs: dict[str, set[int]] = defaultdict(set)
    mode_docs: dict[str, set[str]] = defaultdict(set)
    mode_kind: dict[str, Counter] = defaultdict(Counter)
    for r in scope:
        for m in tags_of(r):
            if r.observation_id not in mode_obs[m]:
                mode_kind[m][r.kind] += 1
            mode_obs[m].add(r.observation_id)
            mode_docs[m].add(r.source_key)
    top = sorted(mode_obs, key=lambda m: (-len(mode_obs[m]), m))

    cfr_cites: Counter = Counter()
    cfr_by_mode: dict[str, Counter] = defaultdict(Counter)
    for r in scope:
        if r.cfr:
            cfr_cites[r.cfr_section or r.cfr] += 1
            for m in tags_of(r):
                cfr_by_mode[m][r.cfr_section or r.cfr] += 1
    cfr_obs: dict[str, set[int]] = defaultdict(set)
    for r in scope:
        if r.cfr:
            cfr_obs[r.cfr_section or r.cfr].add(r.observation_id)

    # Samples: tagged citations first (one per violation), then the newest untagged ones.
    seen_obs: set[int] = set()
    picks: list[Row] = []
    for pool in ([r for r in scope if tags_of(r)], [r for r in scope if not tags_of(r)]):
        for r in pool:
            if len(picks) >= samples:
                break
            if r.observation_id in seen_obs:
                continue
            seen_obs.add(r.observation_id)
            picks.append(r)

    office_counts = Counter((r.office or ("OII reading room (Form 483)" if r.kind == "483" else "unknown"), r.in_scope)
                            for r in {(r.kind, r.source_key): r for r in rows}.values())

    def mode_label(m: str) -> str:
        mode = taxonomy.modes.get(m)
        return f"<strong>{escape(mode.name if mode else m)}</strong> <span class='small muted'>{m}</span>"

    def mode_sub(m: str) -> str:
        mode = taxonomy.modes.get(m)
        k = mode_kind[m]
        cfrs = ", ".join(f"{s} ({n})" for s, n in cfr_by_mode[m].most_common(3)) or "no CFR section cited"
        cat = taxonomy.categories.get(mode.category, "") if mode else ""
        return f"{escape(cat)} &middot; {k['wl']} warning-letter, {k['483']} Form 483 &middot; {escape(cfrs)}"

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    max_mode = max((len(v) for v in mode_obs.values()), default=0)
    top_cfr = cfr_cites.most_common(20)
    max_cfr = max((v for _, v in top_cfr), default=0)

    html = [f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enforcement Corpus Report</title><style>{CSS}</style></head><body><main>
<h1>Computerized-systems enforcement: corpus report</h1>
<div class="muted small">Generated {generated} from the local auditcore database. Internal working view.</div>
<div class="banner">Failure-mode counts below use <strong>{escape(basis)}</strong>.
{'' if use_accepted else 'They are for checking the pipeline, not for citing. Nothing here has passed the 0.85 precision gate used by the public site.'}</div>

<div class="tiles">
<div class="tile"><div class="v">{sum(1 for k, _ in docs if k == 'wl')}</div><div class="l">warning letters in scope</div></div>
<div class="tile"><div class="v">{sum(1 for k, _ in docs if k == '483')}</div><div class="l">published Form 483s</div></div>
<div class="tile"><div class="v">{n_obs}</div><div class="l">violations and observations</div></div>
<div class="tile"><div class="v">{len(scope)}</div><div class="l">citations</div></div>
<div class="tile"><div class="v">{sum(len(v) for v in mode_obs.values())}</div><div class="l">failure-mode findings (violation x mode)</div></div>
</div>
<p class="small muted">Issue dates {min(dates).isoformat() if dates else 'n/a'} to {max(dates).isoformat() if dates else 'n/a'}.
{len(excluded_docs)} food and veterinary letters are excluded from these counts; they appear in the CSV with <code>in_scope = no</code>.
Corpus: published warning letters and published Form 483s only; most 483s are never published.</p>

<h2>Top failure modes</h2>
"""]
    if top:
        html.append("<div class='scroll'><table><thead><tr><th>Failure mode</th><th class='barhead'></th>"
                    "<th class='n'>Violations</th><th class='n'>Documents</th></tr></thead><tbody>")
        html.append(_bar_rows([(mode_label(m), mode_sub(m), len(mode_obs[m]), len(mode_docs[m])) for m in top[:20]], max_mode))
        html.append("</tbody></table></div>")
    else:
        html.append("<div class='card muted'>No failure-mode tags yet. Run <code>auditcore process</code>.</div>")
    html.append("<p class='note'>Failure-mode totals count each violation once; the CFR breakdown counts each section cited. "
                "A violation is one numbered warning-letter violation or one numbered 483 observation.</p>")

    paper_obs: dict[str, set[int]] = defaultdict(set)
    for r in scope:
        for m in paper_of(r):
            paper_obs[m].add(r.observation_id)
    if paper_obs:
        listed = ", ".join(f"{escape(taxonomy.modes[m].name) if m in taxonomy.modes else m} ({len(v)})"
                           for m, v in sorted(paper_obs.items(), key=lambda kv: (-len(kv[1]), kv[0])))
        n_paper = len(set().union(*paper_obs.values()))
        html.append(f"<p class='note'>{n_paper} violations with paper-only data-integrity findings are left out of "
                    f"the counts above: {listed}. They involve no electronic or hybrid record. The CSV lists them "
                    "under <code>paper_only_proposed</code> and <code>paper_only_accepted</code>.</p>")

    html.append(f"<h2>CFR breakdown</h2><p class='small muted'>Sections cited across all {len(scope)} in-scope citations, "
                "whether or not a failure mode was tagged. Most 483 observations cite no section.</p>")
    if top_cfr:
        html.append("<div class='scroll'><table><thead><tr><th>Section</th><th class='barhead'></th><th class='n'>Times cited</th>"
                    "<th class='n'>Violations</th></tr></thead><tbody>")
        html.append(_bar_rows([(f"21 CFR {escape(s)}", "", n, len(cfr_obs[s])) for s, n in top_cfr], max_cfr, alt=True))
        html.append("</tbody></table></div>")
    no_cfr = sum(1 for r in scope if not r.cfr)
    html.append(f"<p class='note'>{no_cfr} of {len(scope)} citations cite no CFR section.</p>")

    html.append(f"<h2>Sample citations</h2><p class='small muted'>{len(picks)} citations: tagged ones first, then the most recent. "
                "Excerpts are the first 700 characters; the CSV has full text.</p>")
    for r in picks:
        tags = "".join(f"<span class='tag'>{escape(m)} {escape(taxonomy.modes[m].name) if m in taxonomy.modes else ''}</span>"
                       for m in sorted(tags_of(r)))
        link = f"<a href='{escape(r.source_url)}' rel='noopener'>source</a>" if r.source_url else "no source link"
        excerpt = r.text[:700] + ("..." if len(r.text) > 700 else "")
        html.append(f"""<div class="card"><div class="meta"><strong>{escape(r.facility or 'Unknown facility')}</strong>
{' &middot; FEI ' + escape(r.fei) if r.fei else ''} &middot; {'Form 483 observation' if r.kind == '483' else 'Warning letter violation'} {r.number}
{' &middot; ' + r.observed.isoformat() if r.observed else ''}{' &middot; ' + escape(r.cfr) if r.cfr else ''} &middot; {link}</div>
<div class="excerpt">{escape(excerpt)}</div>{('<div style="margin-top:6px">' + tags + '</div>') if tags else ''}</div>""")

    html.append("<h2>Documents by issuing office</h2><div class='scroll'><table><thead><tr><th>Issuing office</th>"
                "<th>In scope</th><th class='n'>Documents</th></tr></thead><tbody>")
    for (office, ins), n in sorted(office_counts.items(), key=lambda kv: -kv[1]):
        html.append(f"<tr><td>{escape(office)}</td><td>{'yes' if ins else 'no'}</td><td class='n'>{n}</td></tr>")
    html.append(f"</tbody></table></div><p class='note'>All citations, including excluded ones, are in "
                f"<a href='{csv_path.name}'>{csv_path.name}</a> ({len(rows)} rows).</p></main></body></html>")

    html_path = out_dir / "report.html"
    html_path.write_text("\n".join(html), encoding="utf-8")
    return {"html": html_path, "csv": csv_path}
