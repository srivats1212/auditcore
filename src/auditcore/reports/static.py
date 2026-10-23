"""Static, self-contained HTML report and CSV export of the ingested corpus.

One HTML file that opens from disk with no server and no network: the data
is embedded as JSON and drawn by inline script, so the year and document-type
filters work offline. It shows human-accepted tags when any exist, otherwise
unreviewed machine proposals, and says which. The public site (`auditcore
serve`) is where the publication gate applies.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ingest.scope import in_scope
from ..models import CfrReference, Citation, Document, Facility, FailureMode, Form483, Observation, WarningLetter
from ..taxonomy.loader import Taxonomy



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
        r.in_scope = in_scope(r.kind, r.office, r.subject, r.cfr_part)
    for cid, mid, prov, status, paper in session.execute(
            select(FailureMode.citation_id, FailureMode.mode_id, FailureMode.provenance, FailureMode.status,
                   FailureMode.paper_only)
            .where(FailureMode.mode_id != "NONE")):
        r = rows.get(cid)
        if r is None:
            continue
        if prov == "human" and status == "accepted":
            (r.accepted_paper if paper else r.accepted).append(mid)
        elif prov in ("rules", "llm") and status == "proposed":
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

EXCERPT_CHARS = 900


def _evidence(session: Session) -> dict[tuple[int, str], str]:
    """Best evidence span per (citation, mode): the reviewer's, else the most
    confident proposal's."""
    best: dict[tuple[int, str], tuple[float, str]] = {}
    for cid, mid, ev, prov, conf in session.execute(
            select(FailureMode.citation_id, FailureMode.mode_id, FailureMode.evidence, FailureMode.provenance,
                   FailureMode.confidence).where(FailureMode.evidence.is_not(None), FailureMode.mode_id != "NONE")):
        rank = 2.0 if prov == "human" else float(conf or 0.0)
        if rank > best.get((cid, mid), (-1.0, ""))[0]:
            best[(cid, mid)] = (rank, ev)
    return {k: v[1] for k, v in best.items()}


def _excerpt(text: str, evidence: str | None) -> tuple[str, bool, bool, list[list[int]]]:
    """A window of the citation around its evidence, and where the evidence
    falls in it: (excerpt, cut at start, cut at end, highlight ranges)."""
    match = None
    words = (evidence or "").strip(" .\"'").split()
    pattern = r"\s+".join(re.escape(w) for w in words) if words else None
    if pattern:
        match = re.search(pattern, text, re.IGNORECASE)
    start = 0
    if match and match.end() > EXCERPT_CHARS - 100:
        start = max(0, match.start() - 250)
        space = text.rfind(" ", 0, start)
        start = space + 1 if space > start - 40 else start
    end = min(len(text), start + EXCERPT_CHARS)
    excerpt = text[start:end]
    marks = [[m.start(), m.end()] for m in re.finditer(pattern, excerpt, re.IGNORECASE)] if pattern else []
    return excerpt, start > 0, end < len(text), marks


def build_report(session: Session, taxonomy: Taxonomy, out_dir: Path, *, samples: int = 3,
                 since_year: int | None = None) -> dict[str, Path]:
    """Write report.html and citations.csv. ``samples`` is the number of
    sample citations kept per failure mode and year; ``since_year`` drops
    earlier years from the HTML (the CSV keeps every citation)."""
    rows = load_rows(session)
    scope = [r for r in rows if r.in_scope and (since_year is None or (r.observed and r.observed.year >= since_year))]
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "citations.csv"
    export_csv(rows, csv_path)

    use_accepted = any(r.accepted for r in scope)
    tags_of = (lambda r: r.accepted) if use_accepted else (lambda r: r.proposed)
    paper_of = (lambda r: r.accepted_paper) if use_accepted else (lambda r: r.proposed_paper)
    evidence = _evidence(session)

    kinds = {"wl": 0, "483": 1}
    doc_index: dict[tuple[str, str], int] = {}
    docs_by_year: dict[int, list[set[int]]] = defaultdict(lambda: [set(), set()])
    obs_by_year: dict[int, list[set[int]]] = defaultdict(lambda: [set(), set()])
    for r in scope:
        if r.observed is None:
            continue
        d = doc_index.setdefault((r.kind, r.source_key), len(doc_index))
        docs_by_year[r.observed.year][kinds[r.kind]].add(d)
        obs_by_year[r.observed.year][kinds[r.kind]].add(r.observation_id)

    # Approved counting rule: a finding is one violation (observation) x one mode.
    # The CFR breakdown counts each section cited by the finding's citations.
    findings: dict[tuple[str, int], dict] = {}
    paper: dict[str, Counter] = defaultdict(Counter)
    paper_seen: set[tuple[str, int]] = set()
    for r in scope:
        if r.observed is None:
            continue
        for m in tags_of(r):
            f = findings.setdefault((m, r.observation_id), {
                "m": m, "y": r.observed.year, "k": kinds[r.kind], "d": doc_index[(r.kind, r.source_key)],
                "c": [], "rows": []})
            if r.cfr_section and r.cfr_section not in f["c"]:
                f["c"].append(r.cfr_section)
            f["rows"].append(r)
        for m in paper_of(r):
            if (m, r.observation_id) not in paper_seen:
                paper_seen.add((m, r.observation_id))
                paper[m][r.observed.year] += 1

    # Samples: per mode and year, newest first, one per violation.
    picks: dict[str, list[dict]] = defaultdict(list)
    per_year: Counter = Counter()
    for f in sorted(findings.values(), key=lambda f: (f["rows"][0].observed, f["rows"][0].citation_id), reverse=True):
        if per_year[(f["m"], f["y"])] >= samples:
            continue
        per_year[(f["m"], f["y"])] += 1
        r = f["rows"][0]
        text, cut_start, cut_end, marks = _excerpt(r.text, evidence.get((r.citation_id, f["m"])))
        picks[f["m"]].append({
            "y": f["y"], "k": f["k"], "fac": r.facility or "Facility not named", "fei": r.fei, "date": r.observed.isoformat(),
            "n": r.number, "cfr": ", ".join(f"21 CFR {c}" for c in f["c"]), "url": r.source_url, "t": text,
            "a": cut_start, "b": cut_end, "h": marks, "id": r.citation_id})

    years = sorted(docs_by_year)
    as_of = max((r.observed for r in scope if r.observed), default=None)
    used = sorted({f["m"] for f in findings.values()} | set(paper))
    excluded_docs = {(r.kind, r.source_key) for r in rows if not r.in_scope} - set(doc_index)
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "asOf": as_of.isoformat() if as_of else None,
        "accepted": use_accepted,
        "years": years,
        "docs": {y: [len(docs_by_year[y][0]), len(docs_by_year[y][1])] for y in years},
        "obs": {y: [len(obs_by_year[y][0]), len(obs_by_year[y][1])] for y in years},
        "modes": {m: [taxonomy.modes[m].name, taxonomy.categories.get(taxonomy.modes[m].category, ""),
                      taxonomy.modes[m].definition] if m in taxonomy.modes else [m, "", ""] for m in used},
        "f": [[f["m"], f["y"], f["k"], f["d"], f["c"]] for f in findings.values()],
        "paper": {m: dict(c) for m, c in paper.items()},
        "samples": picks,
        "excluded": len(excluded_docs),
        "csv": csv_path.name,
        "csvRows": len(rows),
        "taxonomy": taxonomy.version,
    }
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html_path = out_dir / "report.html"
    html_path.write_text(PAGE.replace("/*DATA*/null", payload), encoding="utf-8")
    return {"html": html_path, "csv": csv_path}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FDA Computerized-Systems Enforcement</title>
<style>
:root { --bg:#f6f5f2; --surface:#fcfcfb; --border:#dedcd6; --text:#0b0b0b; --text2:#52514e; --muted:#77756f;
  --accent:#1c5cab; --s1:#2a78d6; --s2:#eb6834; --grid:#e6e4de; --warn-bg:#fff4e5; --warn:#8a4b00; --tag:#e8f0fb;
  --heat:42,120,214; --mark:#fbe7a1; --shadow:0 1px 2px rgba(0,0,0,.05); }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) { color-scheme:dark;
  --bg:#121211; --surface:#1a1a19; --border:#33332f; --text:#f0efec; --text2:#c3c2b7; --muted:#8f8e86;
  --accent:#86b6ef; --s1:#3987e5; --s2:#d95926; --grid:#2a2a27; --warn-bg:#2b2112; --warn:#f0c37a; --tag:#1d2a3b;
  --heat:57,135,229; --mark:#6b5a1e; --shadow:none; } }
:root[data-theme="dark"] { color-scheme:dark; --bg:#121211; --surface:#1a1a19; --border:#33332f; --text:#f0efec;
  --text2:#c3c2b7; --muted:#8f8e86; --accent:#86b6ef; --s1:#3987e5; --s2:#d95926; --grid:#2a2a27; --warn-bg:#2b2112;
  --warn:#f0c37a; --tag:#1d2a3b; --heat:57,135,229; --mark:#6b5a1e; --shadow:none; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1080px; margin:0 auto; padding:28px 16px 72px; }
h1 { font-size:1.7rem; line-height:1.2; margin:0 0 6px; letter-spacing:-.01em; }
h2 { font-size:1.15rem; margin:40px 0 4px; }
h3 { font-size:1.02rem; margin:0; }
a { color:var(--accent); }
.muted { color:var(--text2); } .small { font-size:.85rem; } .tiny { font-size:.78rem; }
.lede { color:var(--text2); max-width:760px; margin:0 0 4px; }
.banner { background:var(--warn-bg); color:var(--warn); border-radius:8px; padding:10px 14px; margin:16px 0 0; font-size:.88rem; }
.controls { position:sticky; top:0; z-index:5; background:var(--bg); padding:10px 0; margin-top:14px;
  border-bottom:1px solid var(--border); display:flex; flex-wrap:wrap; gap:8px 18px; align-items:center; }
.seg { display:inline-flex; flex-wrap:wrap; border:1px solid var(--border); border-radius:8px; overflow:hidden; background:var(--surface); }
.seg button { font:inherit; font-size:.85rem; border:0; border-right:1px solid var(--border); background:transparent;
  color:var(--text2); padding:5px 11px; cursor:pointer; }
.seg button:last-child { border-right:0; }
.seg button[aria-pressed="true"] { background:var(--accent); color:var(--surface); }
.ctl-label { font-size:.78rem; color:var(--muted); margin-right:6px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin:18px 0 0; }
.tile { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; box-shadow:var(--shadow); }
.tile .v { font-size:1.6rem; font-weight:650; font-variant-numeric:tabular-nums; line-height:1.2; }
.tile .l { font-size:.8rem; color:var(--text2); }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px; margin:12px 0; box-shadow:var(--shadow); }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:.88rem; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); vertical-align:middle; }
th { color:var(--text2); font-weight:600; font-size:.78rem; white-space:nowrap; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
th.sel, td.sel { outline:2px solid var(--accent); outline-offset:-2px; }
td.heat { min-width:44px; }
tr.moderow { cursor:pointer; } tr.moderow:hover td { background:rgba(var(--heat),.06); }
.mname { font-weight:600; } .mid { color:var(--muted); font-size:.75rem; }
.legend { display:flex; gap:16px; font-size:.8rem; color:var(--text2); margin:4px 0 6px; flex-wrap:wrap; }
.sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:baseline; }
svg text { fill:var(--muted); font-size:11px; font-family:inherit; }
svg .grid { stroke:var(--grid); stroke-width:1; }
svg .lbl { fill:var(--text2); font-size:11px; }
svg .dim { opacity:.35; }
.cards { display:grid; gap:12px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px; box-shadow:var(--shadow); scroll-margin-top:70px; }
.card-head { display:flex; flex-wrap:wrap; justify-content:space-between; gap:6px 16px; align-items:baseline; }
.def { color:var(--text2); font-size:.86rem; margin:4px 0 10px; max-width:780px; }
.card-body { display:grid; grid-template-columns:minmax(220px,300px) 1fr; gap:16px; }
.stats { font-size:.84rem; color:var(--text2); }
.stats b { color:var(--text); font-variant-numeric:tabular-nums; }
.pill { display:inline-block; font-size:.74rem; border:1px solid var(--border); border-radius:999px; padding:0 8px; color:var(--text2); margin:2px 4px 2px 0; }
.sample { border-top:1px solid var(--border); padding:10px 0 2px; }
.sample:first-child { border-top:0; padding-top:0; }
.smeta { font-size:.8rem; color:var(--text2); margin-bottom:4px; }
.smeta strong { color:var(--text); }
.excerpt { white-space:pre-wrap; font-size:.86rem; max-height:11.5em; overflow:auto; }
mark { background:var(--mark); color:inherit; border-radius:2px; padding:0 1px; }
.more { margin-top:12px; }
.more button { font:inherit; font-size:.85rem; padding:6px 12px; border:1px solid var(--accent); color:var(--accent);
  background:transparent; border-radius:6px; cursor:pointer; }
.note { font-size:.82rem; color:var(--text2); border-left:3px solid var(--border); padding-left:10px; margin-top:10px; }
.method li { margin:4px 0; }
footer { margin-top:48px; font-size:.8rem; color:var(--muted); }
@media (max-width:720px) { .card-body { grid-template-columns:1fr; } h1 { font-size:1.4rem; } th, td { padding:5px 4px; } }
@media print { .controls { position:static; } .more { display:none; } .excerpt { max-height:none; } }
</style></head>
<body><main>
<header>
  <h1>FDA computerized-systems enforcement</h1>
  <p class="lede" id="lede"></p>
  <div class="banner" id="basis"></div>
</header>
<noscript><p class="banner">This report draws its tables and charts with JavaScript. Enable it, or use the CSV next to this file.</p></noscript>

<div class="controls" role="toolbar" aria-label="Filters">
  <div><span class="ctl-label">Year</span><span class="seg" id="years"></span></div>
  <div><span class="ctl-label">Documents</span><span class="seg" id="kinds"></span></div>
  <div><span class="ctl-label">Measure</span><span class="seg" id="measures"></span></div>
</div>

<section class="tiles" id="tiles" aria-label="Summary"></section>

<h2>Findings per year</h2>
<p class="small muted" id="trend-note"></p>
<div class="panel"><div class="legend" id="trend-legend"></div><div id="trend"></div></div>

<h2>Failure modes by year</h2>
<p class="small muted" id="table-note"></p>
<div class="panel scroll"><table id="mode-table"></table></div>

<h2>By category</h2>
<div class="panel scroll"><table id="cat-table"></table></div>

<h2>Failure modes in detail</h2>
<p class="small muted" id="cards-note"></p>
<div class="cards" id="cards"></div>
<div class="more" id="more"></div>

<h2>CFR sections behind the findings</h2>
<p class="small muted">Sections cited by the citations that carry a finding. A violation citing two sections counts under both.</p>
<div class="panel scroll"><table id="cfr-table"></table></div>

<h2>Method</h2>
<div class="panel"><ul class="method small" id="method"></ul><p class="note" id="paper-note"></p></div>
<footer id="footer"></footer>
</main>
<script id="report-data" type="application/json">/*DATA*/null</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("report-data").textContent);
  var KIND = ["Warning letters", "Form 483s"];
  var state = { year: null, kind: null, measure: "count", showAll: false };

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "cls") n.className = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c != null) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function svg(tag, attrs) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs || {}) n.setAttribute(k, attrs[k]);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function fmt(v) { return v.toLocaleString("en-US"); }
  function kindOk(k) { return state.kind === null || state.kind === k; }
  function yearOk(y) { return state.year === null || state.year === y; }
  function docsIn(y) { var d = D.docs[y] || [0, 0]; return state.kind === null ? d[0] + d[1] : d[state.kind]; }
  function obsIn(y) { var d = D.obs[y] || [0, 0]; return state.kind === null ? d[0] + d[1] : d[state.kind]; }
  function docsSel() { return D.years.filter(yearOk).reduce(function (a, y) { return a + docsIn(y); }, 0); }
  function value(count, y) {
    if (state.measure === "count") return count;
    var d = y === null ? docsSel() : docsIn(y);
    return d ? 100 * count / d : 0;
  }
  function show(v) { return state.measure === "count" ? fmt(v) : v.toFixed(1); }
  function rows() { return D.f.filter(function (f) { return kindOk(f[2]); }); }
  var lastYear = D.years[D.years.length-1];
  var partial = D.asOf && D.asOf.slice(5) !== "12-31";
  function yearLabel(y) { return partial && y === lastYear ? y + "*" : String(y); }

  // ---- header
  var y0 = D.years[0], y1 = lastYear;
  document.getElementById("lede").textContent = "What FDA cited about computerized systems and electronic records in " +
    "published warning letters and Form 483s for drugs, biologics and devices, " + (y0 === y1 ? String(y0) : y0 + " to " + y1) +
    (partial ? " (" + y1 + " through " + D.asOf + ")" : "") + ".";
  document.getElementById("basis").textContent = D.accepted
    ? "Counts use findings accepted by a human reviewer."
    : "Counts use machine-proposed tags (rules and Claude) that a person has not yet reviewed. Treat them as a draft: " +
      "they show where enforcement concentrates, not verified totals.";

  // ---- controls
  function seg(id, items, key) {
    var box = clear(document.getElementById(id));
    items.forEach(function (it) {
      box.appendChild(el("button", { type: "button", "aria-pressed": String(state[key] === it[0]), text: it[1],
        onclick: function () { state[key] = it[0]; state.showAll = false; render(); } }));
    });
  }

  // ---- tiles
  function tiles() {
    var fs = rows().filter(function (f) { return yearOk(f[1]); });
    var docsWith = {}, modes = {};
    fs.forEach(function (f) { docsWith[f[3]] = 1; modes[f[0]] = 1; });
    var nd = docsSel(), nw = Object.keys(docsWith).length;
    var obs = D.years.filter(yearOk).reduce(function (a, y) { return a + obsIn(y); }, 0);
    var items = [
      [fmt(nd), state.kind === null ? "documents (warning letters and 483s)" : KIND[state.kind].toLowerCase()],
      [fmt(obs), "violations and observations"],
      [fmt(fs.length), "computerized-systems findings"],
      [nd ? Math.round(100 * nw / nd) + "%" : "n/a", "of documents with at least one finding"],
      [fmt(Object.keys(modes).length), "failure modes cited"]];
    var box = clear(document.getElementById("tiles"));
    items.forEach(function (t) { box.appendChild(el("div", { cls: "tile" }, [el("div", { cls: "v", text: t[0] }), el("div", { cls: "l", text: t[1] })])); });
  }

  // ---- charts
  function barChart(series, opts) {
    // series: [{y, a, b}] stacked a (series 1) + b (series 2)
    var W = opts.w || 640, H = opts.h || 200, pl = 36, pr = 8, pt = 12, pb = 28;
    var max = Math.max.apply(null, series.map(function (s) { return s.a + s.b; }).concat([0]));
    var nice = max <= 0 ? 1 : Math.pow(10, Math.floor(Math.log10(max)));
    var top = Math.ceil(max / nice) * nice || 1;
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", role: "img", "aria-label": opts.label });
    var iw = W-pl-pr, ih = H-pt-pb;
    for (var i = 0; i <= 4; i++) {
      var gy = pt + ih * (1-i / 4);
      root.appendChild(svg("line", { x1: pl, x2: W-pr, y1: gy, y2: gy, "class": "grid" }));
      var t = svg("text", { x: pl-6, y: gy + 4, "text-anchor": "end" });
      t.textContent = opts.fmt(top * i / 4); root.appendChild(t);
    }
    var bw = iw / series.length, gap = Math.min(18, bw * 0.3), w = Math.min(bw-gap, opts.maxBar || 90);
    series.forEach(function (s, j) {
      var x = pl + j * bw + (bw-w) / 2;
      var g = svg("g", state.year !== null && state.year !== s.y ? { "class": "dim" } : {});
      var ha = ih * s.a / top, hb = ih * s.b / top;
      var r1 = svg("rect", { x: x, y: pt + ih-ha, width: w, height: Math.max(ha, 0), fill: "var(--s1)", rx: 2 });
      var r2 = svg("rect", { x: x, y: pt + ih-ha-hb, width: w, height: Math.max(hb, 0), fill: "var(--s2)", rx: 2 });
      var tip = svg("title", {}); tip.textContent = opts.tip(s); g.appendChild(tip);
      g.appendChild(r1); if (s.b) g.appendChild(r2);
      var lab = svg("text", { x: x + w / 2, y: pt + ih-ha-hb-4, "text-anchor": "middle", "class": "lbl" });
      lab.textContent = s.a + s.b ? opts.fmt(s.a + s.b) : ""; g.appendChild(lab);
      var yl = svg("text", { x: x + w / 2, y: H-10, "text-anchor": "middle" });
      yl.textContent = yearLabel(s.y); g.appendChild(yl);
      g.style.cursor = "pointer";
      g.addEventListener("click", function () { state.year = state.year === s.y ? null : s.y; render(); });
      root.appendChild(g);
    });
    return root;
  }

  function trend() {
    var by = {};
    D.years.forEach(function (y) { by[y] = [0, 0]; });
    rows().forEach(function (f) { by[f[1]][f[2]]++; });
    var series = D.years.map(function (y) {
      var a = state.kind === 1 ? 0 : by[y][0], b = state.kind === 0 ? 0 : by[y][1];
      if (state.measure !== "count") { var d = docsIn(y); a = d ? 100 * a / d : 0; b = d ? 100 * b / d : 0; }
      return { y: y, a: a, b: b };
    });
    var box = clear(document.getElementById("trend"));
    box.appendChild(barChart(series, { label: "Findings per year", fmt: function (v) { return state.measure === "count" ? fmt(Math.round(v)) : v.toFixed(0); },
      tip: function (s) { return yearLabel(s.y) + ": " + show(s.a) + " in warning letters, " + show(s.b) + " in Form 483s"; } }));
    var lg = clear(document.getElementById("trend-legend"));
    if (state.kind !== 1) lg.appendChild(el("span", {}, [el("span", { cls: "sw", style: "background:var(--s1)" }), "Warning letters"]));
    if (state.kind !== 0) lg.appendChild(el("span", {}, [el("span", { cls: "sw", style: "background:var(--s2)" }), "Form 483s"]));
    document.getElementById("trend-note").textContent = (state.measure === "count"
      ? "Number of findings (violation x failure mode) by issue year."
      : "Findings per 100 documents issued that year, so years with more letters do not look worse by volume alone.") +
      " Documents per year: " + D.years.map(function (y) { return yearLabel(y) + " " + docsIn(y); }).join(", ") +
      ". Click a bar to filter by year." + (partial ? " *" + lastYear + " is a partial year." : "");
  }

  function spark(vals) {
    var W = 84, H = 22, max = Math.max.apply(null, vals.concat([0])) || 1, bw = W / vals.length;
    var root = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, "aria-hidden": "true" });
    vals.forEach(function (v, i) {
      var h = Math.max(v ? 2 : 0, (H-2) * v / max);
      root.appendChild(svg("rect", { x: i * bw + 1, y: H-h, width: bw-2, height: h, rx: 1,
        fill: state.year === D.years[i] ? "var(--s2)" : "var(--s1)" }));
    });
    return root;
  }

  // ---- mode table
  function modeCounts() {
    var c = {};
    rows().forEach(function (f) {
      var m = c[f[0]] || (c[f[0]] = { years: {}, total: 0, docs: {}, wl: 0, f483: 0, cfr: {} });
      m.years[f[1]] = (m.years[f[1]] || 0) + 1;
      if (yearOk(f[1])) {
        m.total++; m.docs[f[3]] = 1; if (f[2] === 0) m.wl++; else m.f483++;
        f[4].forEach(function (s) { m.cfr[s] = (m.cfr[s] || 0) + 1; });
      }
    });
    return c;
  }
  function sortedModes(c) {
    return Object.keys(c).filter(function (m) { return c[m].total > 0; })
      .sort(function (a, b) { return c[b].total-c[a].total || a.localeCompare(b); });
  }

  function modeTable(c, order) {
    var t = clear(document.getElementById("mode-table"));
    var head = el("tr", {}, [el("th", { text: "Failure mode" })]);
    D.years.forEach(function (y) { head.appendChild(el("th", { cls: "n" + (state.year === y ? " sel" : ""), text: yearLabel(y) })); });
    head.appendChild(el("th", { cls: "n", text: state.year === null ? "Total" : "Total " + state.year }));
    head.appendChild(el("th", { text: "Trend" }));
    t.appendChild(el("thead", {}, [head]));
    var body = el("tbody");
    var max = 0;
    order.forEach(function (m) { D.years.forEach(function (y) { max = Math.max(max, value(c[m].years[y] || 0, y)); }); });
    order.forEach(function (m) {
      var info = D.modes[m];
      var tr = el("tr", { cls: "moderow", title: "Show details", onclick: function () {
        var card = document.getElementById("mode-" + m);
        if (!card) { state.showAll = true; render(); card = document.getElementById("mode-" + m); }
        if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
      } }, [el("td", {}, [el("div", { cls: "mname", text: info[0] }), el("div", { cls: "mid", text: m + " · " + info[1] })])]);
      var vals = [];
      D.years.forEach(function (y) {
        var v = value(c[m].years[y] || 0, y); vals.push(v);
        var a = max ? 0.08 + 0.55 * v / max : 0;
        tr.appendChild(el("td", { cls: "n heat" + (state.year === y ? " sel" : ""), style: v ? "background:rgba(var(--heat)," + a.toFixed(3) + ")" : "",
          text: v ? show(v) : "·" }));
      });
      tr.appendChild(el("td", { cls: "n" }, [el("b", { text: show(value(c[m].total, state.year)) })]));
      tr.appendChild(el("td", {}, [spark(vals)]));
      body.appendChild(tr);
    });
    t.appendChild(body);
    document.getElementById("table-note").textContent = (state.measure === "count"
      ? "Findings by failure mode and issue year; each violation counts once per mode."
      : "Findings per 100 documents issued that year.") + " Click a row for definitions, CFR sections and sample citations.";
  }

  function catTable(c, order) {
    var cats = {};
    order.forEach(function (m) {
      var k = D.modes[m][1] || "Other", x = cats[k] || (cats[k] = { total: 0, modes: 0, years: {} });
      x.total += c[m].total; x.modes++;
      D.years.forEach(function (y) { x.years[y] = (x.years[y] || 0) + (c[m].years[y] || 0); });
    });
    var names = Object.keys(cats).sort(function (a, b) { return cats[b].total-cats[a].total; });
    var max = Math.max.apply(null, names.map(function (k) { return cats[k].total; }).concat([1]));
    var t = clear(document.getElementById("cat-table"));
    t.appendChild(el("thead", {}, [el("tr", {}, [el("th", { text: "Category" }), el("th", { text: "" }),
      el("th", { cls: "n", text: "Findings" }), el("th", { cls: "n", text: "Modes" }), el("th", { text: "Trend" })])]));
    var body = el("tbody");
    names.forEach(function (k) {
      var x = cats[k], w = 100 * x.total / max;
      body.appendChild(el("tr", {}, [el("td", { cls: "mname", text: k }),
        el("td", { style: "width:38%" }, [el("div", { style: "height:10px;border-radius:0 4px 4px 0;background:var(--s1);width:" + w.toFixed(1) + "%" })]),
        el("td", { cls: "n", text: fmt(x.total) }), el("td", { cls: "n", text: x.modes }),
        el("td", {}, [spark(D.years.map(function (y) { return value(x.years[y] || 0, y); }))])]));
    });
    t.appendChild(body);
  }

  // ---- cards
  function sampleNode(s) {
    var text = s.t, parts = [], pos = 0;
    s.h.forEach(function (r) {
      if (r[0] < pos) return;
      parts.push(text.slice(pos, r[0])); parts.push(el("mark", { text: text.slice(r[0], r[1]) })); pos = r[1];
    });
    parts.push(text.slice(pos));
    if (s.a) parts.unshift("…");
    if (s.b) parts.push("…");
    var meta = el("div", { cls: "smeta" }, [el("strong", { text: s.fac }), s.fei ? " · FEI " + s.fei : "",
      " · " + (s.k === 0 ? "Warning letter, violation " : "Form 483, observation ") + s.n,
      " · " + s.date, s.cfr ? " · " + s.cfr : "", " · ",
      s.url ? el("a", { href: s.url, target: "_blank", rel: "noopener", text: "source" }) : "no source link"]);
    return el("div", { cls: "sample" }, [meta, el("div", { cls: "excerpt" }, parts)]);
  }

  function cards(c, order) {
    var box = clear(document.getElementById("cards"));
    var limit = state.showAll ? order.length : Math.min(order.length, 10);
    order.slice(0, limit).forEach(function (m) {
      var info = D.modes[m], x = c[m];
      var chart = barChart(D.years.map(function (y) {
        var v = value(x.years[y] || 0, y); return { y: y, a: v, b: 0 };
      }), { w: 300, h: 150, label: info[0] + " by year", fmt: function (v) { return state.measure === "count" ? String(Math.round(v)) : v.toFixed(1); },
        tip: function (s) { return yearLabel(s.y) + ": " + show(s.a); } });
      var cfr = Object.keys(x.cfr).sort(function (a, b) { return x.cfr[b] - x.cfr[a]; }).slice(0, 4);
      var samples = (D.samples[m] || []).filter(function (s) { return yearOk(s.y) && kindOk(s.k); }).slice(0, 3);
      var stats = el("div", { cls: "stats" }, [
        el("div", {}, [el("b", { text: fmt(x.total) }), " findings in ", el("b", { text: fmt(Object.keys(x.docs).length) }), " documents"]),
        el("div", {}, [el("b", { text: fmt(x.wl) }), " in warning letters, ", el("b", { text: fmt(x.f483) }), " in Form 483s"]),
        el("div", { style: "margin-top:6px" }, cfr.length ? cfr.map(function (s) { return el("span", { cls: "pill", text: "21 CFR " + s + " (" + x.cfr[s] + ")" }); })
          : [el("span", { cls: "tiny muted", text: "No CFR section cited (typical for 483s)" })])]);
      box.appendChild(el("article", { cls: "card", id: "mode-" + m }, [
        el("div", { cls: "card-head" }, [el("h3", { text: info[0] }), el("span", { cls: "mid", text: m + " · " + info[1] })]),
        el("p", { cls: "def", text: info[2] }),
        el("div", { cls: "card-body" }, [el("div", {}, [chart, stats]),
          el("div", {}, samples.length ? samples.map(sampleNode) : [el("p", { cls: "small muted", text: "No sample citation for this filter." })])])]));
    });
    var more = clear(document.getElementById("more"));
    if (order.length > limit) more.appendChild(el("button", { type: "button", text: "Show all " + order.length + " failure modes",
      onclick: function () { state.showAll = true; render(); } }));
    document.getElementById("cards-note").textContent = "Top failure modes for the current filter, with up to three recent citations each. " +
      "Highlighted text is the evidence the tag rests on.";
  }

  function cfrTable(c, order) {
    var tot = {};
    order.forEach(function (m) { for (var s in c[m].cfr) tot[s] = (tot[s] || 0) + c[m].cfr[s]; });
    var secs = Object.keys(tot).sort(function (a, b) { return tot[b] - tot[a]; }).slice(0, 15);
    var max = Math.max.apply(null, secs.map(function (s) { return tot[s]; }).concat([1]));
    var t = clear(document.getElementById("cfr-table"));
    t.appendChild(el("thead", {}, [el("tr", {}, [el("th", { text: "Section" }), el("th", { text: "" }), el("th", { cls: "n", text: "Findings" })])]));
    var body = el("tbody");
    if (!secs.length) body.appendChild(el("tr", {}, [el("td", { colspan: 3, cls: "muted", text: "No CFR sections for this filter." })]));
    secs.forEach(function (s) {
      body.appendChild(el("tr", {}, [el("td", { text: "21 CFR " + s }),
        el("td", { style: "width:50%" }, [el("div", { style: "height:10px;border-radius:0 4px 4px 0;background:var(--s2);width:" + (100 * tot[s] / max).toFixed(1) + "%" })]),
        el("td", { cls: "n", text: fmt(tot[s]) })]));
    });
    t.appendChild(body);
  }

  function method() {
    var items = [
      "Sources: warning letters posted on fda.gov and Form 483s in FDA's OII FOIA electronic reading room. Most 483s are never published, so 483 counts are a small, non-random slice of inspections.",
      "Scope: drug, biologic and device letters (CDER, CBER, CDRH and the field divisions that inspect for them). " + D.excluded + " food and veterinary letters in the database are excluded.",
      "Unit: a finding is one numbered warning-letter violation or 483 observation tagged with one failure mode. A violation citing several CFR sections counts once per mode.",
      "Year is the calendar year of the warning-letter or 483 issue date." + (partial ? " " + lastYear + " runs through " + D.asOf + "." : ""),
      "Failure modes come from taxonomy " + D.taxonomy + ". " + (D.accepted ? "Only findings a reviewer accepted are counted."
        : "Tags are proposals from keyword rules and Claude, each backed by a verbatim evidence quote; none has been reviewed by a person yet."),
      "Per 100 documents divides by all in-scope documents issued that year, including ones with no computerized-systems finding."];
    var ul = clear(document.getElementById("method"));
    items.forEach(function (t) { ul.appendChild(el("li", { text: t })); });
    var p = Object.keys(D.paper), n = 0;
    p.forEach(function (m) { for (var y in D.paper[m]) n += D.paper[m][y]; });
    document.getElementById("paper-note").textContent = n
      ? n + " data-integrity findings that involve only paper records (handwritten forms, logbooks, simple printouts, or no record type stated) are excluded from every count: " +
        p.sort(function (a, b) { return sum(D.paper[b]) - sum(D.paper[a]); }).map(function (m) { return D.modes[m][0] + " (" + sum(D.paper[m]) + ")"; }).join(", ") + "."
      : "No paper-only data-integrity findings were excluded.";
    var f = clear(document.getElementById("footer"));
    f.appendChild(document.createTextNode("Generated " + D.generated + " by auditcore. Every citation, including excluded ones, is in "));
    f.appendChild(el("a", { href: D.csv, text: D.csv }));
    f.appendChild(document.createTextNode(" (" + fmt(D.csvRows) + " rows)."));
  }
  function sum(o) { var s = 0; for (var k in o) s += o[k]; return s; }

  function render() {
    seg("years", [[null, "All years"]].concat(D.years.map(function (y) { return [y, yearLabel(y)]; })), "year");
    seg("kinds", [[null, "All"], [0, "Warning letters"], [1, "Form 483s"]], "kind");
    seg("measures", [["count", "Count"], ["rate", "Per 100 documents"]], "measure");
    var c = modeCounts(), order = sortedModes(c);
    tiles(); trend(); modeTable(c, order); catTable(c, order); cards(c, order); cfrTable(c, order); method();
  }
  render();
})();
</script>
</body></html>
"""
