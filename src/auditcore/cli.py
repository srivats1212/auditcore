"""Command line: ``auditcore <group> <command>``. Run ``auditcore -h``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .config import get_settings


def _session():
    from .db import init_db, session_scope

    init_db()
    return session_scope()


def _archive():
    from .ingest.store import RawArchive

    return RawArchive(get_settings().archive_dir)


def _fetcher():
    from .ingest.common import Fetcher

    return Fetcher(get_settings())


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------ commands

def cmd_init(a):
    from .db import init_db
    from .taxonomy.loader import load_taxonomy, sync_taxonomy

    init_db()
    with _session() as s:
        print(f"database ready; taxonomy rows added: {sync_taxonomy(s, load_taxonomy())}")


def cmd_ingest_wl(a):
    from .ingest.warning_letters import ingest_index, ingest_listing

    since = date.fromisoformat(a.since) if a.since else None
    opts = dict(fetch_letters=not a.no_fetch, limit=a.limit, subject=a.subject, refetch=a.refetch, since=since,
                products_only=not a.include_food)
    with _session() as s:
        if a.xlsx:
            _print(ingest_index(s, _archive(), None if a.no_fetch else _fetcher(), Path(a.xlsx).read_bytes(), **opts))
        else:
            _print(ingest_listing(s, _archive(), _fetcher(), max_rows=a.max_rows, **opts))


def cmd_ingest_wl_files(a):
    from .ingest.warning_letters import ingest_letter_files

    with _session() as s:
        _print(ingest_letter_files(s, _archive(), [Path(p) for p in a.paths]))


def cmd_ingest_483(a):
    from .ingest.form483 import ingest_reading_room

    with _session() as s:
        _print(ingest_reading_room(s, _archive(), _fetcher(), listing_url=a.url, limit=a.limit, max_rows=a.max_rows))


def cmd_ingest_483_files(a):
    from .ingest.form483 import ingest_pdf_files

    with _session() as s:
        _print(ingest_pdf_files(s, _archive(), [Path(p) for p in a.paths], Path(a.manifest) if a.manifest else None))


def cmd_ingest_dashboard(a):
    from .ingest.dashboard import ingest_dashboard

    with _session() as s:
        _print(ingest_dashboard(s, _archive(), _fetcher(), product_types=a.product_type or None))


def cmd_runs(a):
    from sqlalchemy import select

    from .models import IngestionRun

    with _session() as s:
        for r in s.scalars(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(a.n)):
            print(f"#{r.id} {r.source:22} {r.status:8} seen={r.n_seen} new={r.n_new} "
                  f"versioned={r.n_versioned} skipped={r.n_skipped} errors={r.n_errors} {r.started_at:%Y-%m-%d %H:%M}")


def cmd_process(a):
    from .tagging.llm import LlmTagger
    from .tagging.pipeline import run_pipeline
    from .taxonomy.loader import load_taxonomy, sync_taxonomy

    tax = load_taxonomy()
    with _session() as s:
        sync_taxonomy(s, tax)
        if a.resegment:
            from .tagging.pipeline import clear_segmentation

            print(f"cleared {clear_segmentation(s)} observations for re-segmentation")
        _print(run_pipeline(s, tax, tagger=LlmTagger(tax) if a.llm else None, limit=a.limit, llm_max=a.llm_max,
                            llm_workers=a.llm_workers))


def _decision_label(item, paper_capable: bool) -> str:
    if item.decision == "accepted" and paper_capable:
        label = "accepted" + (" as paper only" if item.decided_paper_only else " as electronic/hybrid")
    else:
        label = item.decision
    if item.decision != "proposed" and item.reviewer:
        label += f" by {item.reviewer} on {item.reviewed_at:%Y-%m-%d %H:%M}"
    return label + (" (new proposal pending)" if item.pending and item.decision != "proposed" else "")


def _show_item(item, mode, n: int, total: int) -> None:
    print("\n" + "=" * 78)
    print(f"[{n}/{total}] citation {item.citation_id}  ->  {item.mode_id} {mode.name if mode else ''}")
    print(f"proposed by {item.sources}, confidence {item.best_confidence:.2f}")
    if mode and mode.paper_records:
        print(f"record type proposed: {'PAPER ONLY (not counted as computerized systems)' if item.paper_only else 'electronic or hybrid'}")
    print(f"current decision: {_decision_label(item, bool(mode and mode.paper_records))}")
    if mode:
        print(f"definition: {mode.definition}")
    for p in item.proposals:
        if p.evidence:
            print(f"  evidence ({p.provenance}, {p.confidence or 0:.2f}): \"{p.evidence}\"")
    print("-" * 78)
    print(item.text[:2500])


def cmd_review(a):
    from .tagging.review import apply_decision, review_items
    from .taxonomy.loader import load_taxonomy

    tax = load_taxonomy()
    reviewer = a.reviewer
    with _session() as s:
        items = review_items(s, state="decided" if a.amend else "pending", mode_id=a.mode)[:a.limit]
        if not items:
            print("no decided tags to amend" if a.amend else "review queue is empty")
            return
        for n, item in enumerate(items, 1):
            mode = tax.modes.get(item.mode_id)
            _show_item(item, mode, n, len(items))
            prompt = "[a]ccept  [r]eject  [s]kip  [q]uit > "
            valid = {"a", "r", "s", "q"}
            if mode and mode.paper_records:
                prompt = "[a]ccept as proposed  [p]accept as paper only  [e]accept as electronic/hybrid  " + prompt[10:]
                valid |= {"p", "e"}
            while True:
                choice = input(prompt).strip().lower()
                if choice in valid:
                    break
            if choice == "q":
                break
            if choice in ("a", "p", "e", "r"):
                flag = {"p": True, "e": False}.get(choice)
                reason = input("reason (optional) > ").strip() if a.amend else None
                changed = apply_decision(s, item.citation_id, item.mode_id, accept=choice != "r", reviewer=reviewer,
                                         paper_only=flag, reason=reason, interface="cli", taxonomy_version=tax.version)
                if a.amend and not changed:
                    print("no change: that is already the current decision")
                s.commit()


def cmd_review_log(a):
    from sqlalchemy import select

    from .models import ReviewEvent
    from .taxonomy.loader import load_taxonomy

    paper_capable = {m.id for m in load_taxonomy() if m.paper_records}

    def fmt(e, decision, paper):
        if paper is None or e.mode_id not in paper_capable:
            return decision
        return f"{decision} ({'paper only' if paper else 'electronic/hybrid'})"

    with _session() as s:
        q = select(ReviewEvent).order_by(ReviewEvent.id.desc()).limit(a.limit)
        if a.citation:
            q = q.where(ReviewEvent.citation_id == a.citation)
        for e in reversed(s.scalars(q).all()):
            print(f"{e.created_at:%Y-%m-%d %H:%M:%S}  citation {e.citation_id:<6} {e.mode_id:11} "
                  f"{fmt(e, e.previous_decision, e.previous_paper_only)} -> {fmt(e, e.new_decision, e.new_paper_only)}  "
                  f"by {e.reviewer} via {e.interface}" + (f"  reason: {e.reason}" if e.reason else ""))


def cmd_gold_sample(a):
    from .evaluation.gold import export_blind, sample_gold

    with _session() as s:
        ids = sample_gold(s, a.n, seed=a.seed)
        print(f"exported {export_blind(s, ids, Path(a.out))} citations to {a.out} (blind: text only)")


def cmd_gold_import(a):
    from .evaluation.gold import import_labels
    from .taxonomy.loader import load_taxonomy

    with _session() as s:
        print(f"imported {import_labels(s, Path(a.csv), tagger=a.tagger, taxonomy=load_taxonomy())} labels as {a.tagger}")


def cmd_gold_agreement(a):
    from .evaluation.gold import agreement
    from .taxonomy.loader import load_taxonomy

    with _session() as s:
        _print(agreement(s, load_taxonomy(), a.a, a.b))


def cmd_gold_adjudicate(a):
    from .evaluation.gold import adjudicate
    from .taxonomy.loader import load_taxonomy

    with _session() as s:
        _print(adjudicate(s, load_taxonomy(), a=a.a, b=a.b,
                          disagreements_csv=Path(a.disagreements) if a.disagreements else None,
                          resolutions_csv=Path(a.resolutions) if a.resolutions else None))


def cmd_gold_evaluate(a):
    from .evaluation.gold import PIPELINES, evaluate, passing_modes, score_by_trap
    from .taxonomy.loader import load_taxonomy

    tax = load_taxonomy()
    bar = get_settings().precision_bar
    with _session() as s:
        for pipeline in ([a.pipeline] if a.pipeline else PIPELINES):
            scores = evaluate(s, tax, pipeline)
            live = passing_modes(s, tax.version, bar=bar, pipeline=pipeline)
            print(f"\n== {pipeline} (bar {bar:.2f}) ==")
            print(f"{'mode':12} {'P':>6} {'R':>6} {'tp':>4} {'fp':>4} {'fn':>4}  gate")
            for mid, sc in sorted(scores.items()):
                if sc.tp + sc.fp + sc.fn == 0:
                    continue
                p = f"{sc.precision:.2f}" if sc.precision is not None else "n/a"
                r = f"{sc.recall:.2f}" if sc.recall is not None else "n/a"
                print(f"{mid:12} {p:>6} {r:>6} {sc.tp:4} {sc.fp:4} {sc.fn:4}  {'PASS' if mid in live else 'hold'}")
            traps = score_by_trap(s, pipeline)
            if traps:
                print("traps:", json.dumps(traps))


def cmd_stats(a):
    from .analytics.stats import compute_annual_stats, escalation_rates
    from .taxonomy.loader import load_taxonomy

    tax = load_taxonomy()
    with _session() as s:
        print(f"annual stat rows: {compute_annual_stats(s, tax.version)}")
        if a.escalation:
            _print(escalation_rates(s, tax.version, window_days=a.window))


def cmd_digest_draft(a):
    from .digest.build import draft_issue
    from .evaluation.gold import passing_modes
    from .taxonomy.loader import load_taxonomy

    tax = load_taxonomy()
    year, month = (int(x) for x in a.month.split("-"))
    content = Path(a.content)
    with _session() as s:
        allowed = passing_modes(s, tax.version, bar=get_settings().precision_bar)
        print(draft_issue(s, tax, year, month, out_dir=content / "digest", remediation_dir=content / "remediation",
                          allowed=allowed))


def cmd_digest_publish(a):
    from .digest.build import publish_issue

    print(publish_issue(Path(a.path), email_dir=Path(a.email_dir)))


def cmd_remediation_check(a):
    from .remediation.patterns import load_patterns

    bad = 0
    for mid, p in load_patterns(Path(a.content) / "remediation").items():
        state = "ok" if not p.problems else "; ".join(p.problems)
        bad += bool(p.problems)
        print(f"{mid:12} {p.status:10} {state}")
    sys.exit(1 if bad else 0)


def cmd_remediation_todo(a):
    from sqlalchemy import func, select

    from .models import FailureMode
    from .remediation.patterns import load_patterns
    from .taxonomy.loader import load_taxonomy

    tax = load_taxonomy()
    done = {m for m, p in load_patterns(Path(a.content) / "remediation").items() if p.publishable}
    with _session() as s:
        rows = s.execute(select(FailureMode.mode_id, func.count()).where(
            FailureMode.provenance == "human", FailureMode.status == "accepted")
            .group_by(FailureMode.mode_id).order_by(func.count().desc()).limit(a.n)).all()
    for mid, n in rows:
        print(f"{mid:12} {n:6}  {'written' if mid in done else 'TODO'}  {tax.modes[mid].name if mid in tax.modes else ''}")


def cmd_lint(a):
    from .copy_lint import lint_paths

    roots = [Path(p) for p in a.paths]
    files = [f for r in roots for f in ([r] if r.is_file() else
             [*r.rglob("*.md"), *r.rglob("*.html"), *r.rglob("*.yaml")])]
    issues = lint_paths(files)
    for i in issues:
        print(f"{i.where}:{i.line}: {i.snippet}")
    print(f"{len(files)} files, {len(issues)} em dash issues")
    sys.exit(1 if issues else 0)


def cmd_report(a):
    from .reports.static import build_report
    from .taxonomy.loader import load_taxonomy

    with _session() as s:
        out = build_report(s, load_taxonomy(), Path(a.out), samples=a.samples)
    print(f"report: {out['html']}\ncsv:    {out['csv']}")


def cmd_serve(a):
    import uvicorn

    from .db import init_db
    from .web.app import create_app

    if a.review:
        if not a.reviewer:
            sys.exit("--review needs --reviewer <you>")
        if a.host not in ("127.0.0.1", "localhost", "::1"):
            sys.exit("the review page is local only: run it on 127.0.0.1")
    init_db()
    app = create_app(content_dir=a.content, review=a.review, reviewer=a.reviewer)
    if a.review:
        print(f"review page: http://{a.host}:{a.port}/review (reviewer {a.reviewer})")
    uvicorn.run(app, host=a.host, port=a.port)


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="auditcore", description="FDA computerized-systems enforcement intelligence")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="group", required=True)

    sub.add_parser("init", help="create tables and load taxonomy").set_defaults(fn=cmd_init)

    ing = sub.add_parser("ingest", help="fetch and archive source documents").add_subparsers(dest="cmd", required=True)
    x = ing.add_parser("wl", help="Warning Letters listing (newest first), then letter pages")
    x.add_argument("--xlsx", help="offline: an XLSX index that includes letter links")
    x.add_argument("--subject", help='only letters whose subject contains this, e.g. "CGMP"')
    x.add_argument("--max-rows", type=int, help="stop paging the listing after this many rows")
    x.add_argument("--limit", type=int, help="fetch at most this many letters this run")
    x.add_argument("--refetch", action="store_true", help="re-fetch stored letters to detect revisions")
    x.add_argument("--no-fetch", action="store_true", help="index only, do not fetch letter pages")
    x.add_argument("--since", help="only letters issued on or after this date, e.g. 2021-01-01")
    x.add_argument("--include-food", action="store_true",
                   help="also keep food and veterinary letters (dropped by default)")
    x.set_defaults(fn=cmd_ingest_wl)
    x = ing.add_parser("wl-files", help="saved letter HTML files named <CMS>.html")
    x.add_argument("paths", nargs="+")
    x.set_defaults(fn=cmd_ingest_wl_files)
    x = ing.add_parser("483", help="OII reading room listing and PDFs")
    x.add_argument("--url")
    x.add_argument("--max-rows", type=int, help="stop paging the listing after this many rows")
    x.add_argument("--limit", type=int, help="download at most this many PDFs this run")
    x.set_defaults(fn=cmd_ingest_483)
    x = ing.add_parser("483-files", help="saved 483 PDFs, optional CSV manifest")
    x.add_argument("paths", nargs="+")
    x.add_argument("--manifest")
    x.set_defaults(fn=cmd_ingest_483_files)
    x = ing.add_parser("dashboard", help="FDA Data Dashboard inspections and compliance actions")
    x.add_argument("--product-type", action="append")
    x.set_defaults(fn=cmd_ingest_dashboard)
    x = ing.add_parser("runs", help="recent ingestion runs")
    x.add_argument("-n", type=int, default=20)
    x.set_defaults(fn=cmd_runs)

    x = sub.add_parser("process", help="segment documents and propose tags")
    x.add_argument("--llm", action="store_true", help="also run LLM tagging (needs Anthropic credentials)")
    x.add_argument("--limit", type=int, help="process at most this many documents")
    x.add_argument("--llm-max", type=int, help="make at most this many LLM calls (one per distinct violation text)")
    x.add_argument("--llm-workers", type=int, default=1, help="LLM calls in flight at once (try 4 to 8)")
    x.add_argument("--resegment", action="store_true",
                   help="rebuild observations and citations with current code (refused once anything is reviewed)")
    x.set_defaults(fn=cmd_process)

    x = sub.add_parser("review", help="interactive human review of proposed tags")
    x.add_argument("--reviewer", required=True)
    x.add_argument("--mode")
    x.add_argument("--limit", type=int, default=50)
    x.add_argument("--amend", action="store_true", help="change already-decided tags instead of reviewing new ones")
    x.set_defaults(fn=cmd_review)
    x = sub.add_parser("review-log", help="audit trail of review decisions")
    x.add_argument("--citation", type=int)
    x.add_argument("--limit", type=int, default=50)
    x.set_defaults(fn=cmd_review_log)

    gold = sub.add_parser("gold", help="gold-label set and evaluation").add_subparsers(dest="cmd", required=True)
    x = gold.add_parser("sample", help="stratified sample, exported blind")
    x.add_argument("--n", type=int, default=300)
    x.add_argument("--seed", type=int, default=13)
    x.add_argument("--out", default="gold_blind.csv")
    x.set_defaults(fn=cmd_gold_sample)
    x = gold.add_parser("import", help="import one tagger's labels")
    x.add_argument("csv")
    x.add_argument("--tagger", required=True, help='e.g. "A" or "B"')
    x.set_defaults(fn=cmd_gold_import)
    x = gold.add_parser("agreement", help="inter-rater agreement (Cohen's kappa per mode)")
    x.add_argument("--a", default="A")
    x.add_argument("--b", default="B")
    x.set_defaults(fn=cmd_gold_agreement)
    x = gold.add_parser("adjudicate", help="resolve agreements, export or import disputes")
    x.add_argument("--a", default="A")
    x.add_argument("--b", default="B")
    x.add_argument("--disagreements")
    x.add_argument("--resolutions")
    x.set_defaults(fn=cmd_gold_adjudicate)
    x = gold.add_parser("evaluate", help="per-mode precision and recall")
    x.add_argument("--pipeline", choices=["rules", "llm", "proposed", "published"])
    x.set_defaults(fn=cmd_gold_evaluate)

    x = sub.add_parser("stats", help="recompute annual aggregates")
    x.add_argument("--escalation", action="store_true")
    x.add_argument("--window", type=int, default=730)
    x.set_defaults(fn=cmd_stats)

    dg = sub.add_parser("digest", help="monthly digest").add_subparsers(dest="cmd", required=True)
    x = dg.add_parser("draft")
    x.add_argument("month", nargs="?", default=date.today().strftime("%Y-%m"), help="YYYY-MM")
    x.add_argument("--content", default="content")
    x.set_defaults(fn=cmd_digest_draft)
    x = dg.add_parser("publish")
    x.add_argument("path")
    x.add_argument("--email-dir", default="data/email")
    x.set_defaults(fn=cmd_digest_publish)

    rm = sub.add_parser("remediation", help="remediation patterns").add_subparsers(dest="cmd", required=True)
    x = rm.add_parser("check")
    x.add_argument("--content", default="content")
    x.set_defaults(fn=cmd_remediation_check)
    x = rm.add_parser("todo", help="top modes by accepted citations and whether a pattern exists")
    x.add_argument("--content", default="content")
    x.add_argument("-n", type=int, default=20)
    x.set_defaults(fn=cmd_remediation_todo)

    x = sub.add_parser("lint", help="no-em-dash check on user-facing copy")
    x.add_argument("paths", nargs="*", default=["content", "src/auditcore/web/templates", "src/auditcore/taxonomy"])
    x.set_defaults(fn=cmd_lint)

    x = sub.add_parser("report", help="static HTML report and CSV export of the ingested corpus")
    x.add_argument("--out", default="reports")
    x.add_argument("--samples", type=int, default=3, help="sample citations kept per failure mode and year")
    x.set_defaults(fn=cmd_report)

    x = sub.add_parser("serve", help="run the web UI")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8000)
    x.add_argument("--content", default="content")
    x.add_argument("--review", action="store_true", help="also serve the local review page at /review")
    x.add_argument("--reviewer", help="name recorded on decisions made in the review page")
    x.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()
