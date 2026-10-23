from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from auditcore.copy_lint import CopyStyleError, assert_clean, find_em_dashes, lint_paths
from auditcore.digest.build import archive, draft_issue, publish_issue
from auditcore.models import Citation, Document, FailureMode, ModeEvaluation, Observation, Subscriber
from auditcore.analytics.stats import compute_annual_stats
from auditcore.remediation.patterns import load_patterns
from auditcore.web.app import create_app
from auditcore.web.charts import trend_svg

ROOT = Path(__file__).resolve().parents[1]

PATTERN = """---
mode_id: CS-AUD-002
title: Make audit-trail review part of record review
status: published
author: tester
sources: ["21 CFR 211.68(b)"]
---

## What the finding means
Text.

## Why FDA cares
Text.

## Fix steps
1. Step.

## Evidence an inspector expects
- Evidence.

## The common remediation mistake
Text.
"""


@pytest.fixture
def content(tmp_path):
    root = tmp_path / "content"
    (root / "remediation").mkdir(parents=True)
    (root / "digest").mkdir()
    (root / "remediation" / "CS-AUD-002.md").write_text(PATTERN)
    return root


def seed(url, taxonomy, *, live=True):
    engine = create_engine(url)
    with Session(engine) as s:
        doc = Document(source="warning_letter", source_key="wl:1", content_hash="h", raw_path="x", media_type="text/html",
                       source_url="https://example.test/wl")
        s.add(doc)
        s.flush()
        obs = Observation(document_id=doc.id, kind="wl", number=1, text="Audit trails were not reviewed.",
                          observed_date=date(2025, 3, 4))
        s.add(obs)
        s.flush()
        cit = Citation(observation_id=obs.id, ordinal=0, text="Audit trails were not reviewed.")
        s.add(cit)
        s.flush()
        for mode in ("CS-AUD-002", "CS-SPR-002"):
            s.add(FailureMode(citation_id=cit.id, mode_id=mode, taxonomy_version=taxonomy.version,
                              provenance="human", status="accepted", confidence=1.0))
        s.add(ModeEvaluation(mode_id="CS-AUD-002", taxonomy_version=taxonomy.version, pipeline="published",
                             tp=10, fp=0 if live else 5, fn=1, precision=1.0 if live else 0.67, recall=0.91, support=11))
        s.add(ModeEvaluation(mode_id="CS-SPR-002", taxonomy_version=taxonomy.version, pipeline="published",
                             tp=3, fp=3, fn=0, precision=0.5, recall=1.0, support=3))
        compute_annual_stats(s, taxonomy.version)
        s.commit()
        return cit.id


class TestWeb:
    def test_pages_and_gate(self, db_url, taxonomy, content):
        seed(db_url, taxonomy)
        client = TestClient(create_app(database_url=db_url, content_dir=content, taxonomy=taxonomy))
        home = client.get("/")
        assert home.status_code == 200 and "Audit trail not routinely reviewed" in home.text
        assert "Unprotected cells" not in home.text  # below the bar: not shown

        page = client.get("/modes/CS-AUD-002")
        assert page.status_code == 200 and "<svg" in page.text and "Table view" in page.text
        assert "Make audit-trail review part of record review" in page.text
        assert "precision 1.00" in page.text

        held = client.get("/modes/CS-SPR-002")
        assert "In review" in held.text and "<svg" not in held.text

        search = client.get("/search", params={"q": "audit trails"})
        assert "1 citation" in search.text and "CS-AUD-002" in search.text and "CS-SPR-002" not in search.text
        assert client.get("/search", params={"mode": "CS-SPR-002"}).status_code == 404
        assert "1 citation" in client.get("/search", params={"mode": "CS-AUD-002", "kind": "wl", "year": "2025"}).text
        assert "0 citations" in client.get("/search", params={"year": "2019"}).text

        assert client.get("/methodology").status_code == 200
        assert client.get("/modes").status_code == 200
        assert client.get("/modes/CS-NOPE-001").status_code == 404
        assert client.get("/digest").status_code == 200

    def test_subscribe(self, db_url, taxonomy, content):
        client = TestClient(create_app(database_url=db_url, content_dir=content, taxonomy=taxonomy))
        r = client.post("/subscribe", data={"email": "Someone@Example.com"}, follow_redirects=False)
        assert r.status_code == 303
        client.post("/subscribe", data={"email": "someone@example.com"}, follow_redirects=False)
        with Session(create_engine(db_url)) as s:
            assert [x.email for x in s.scalars(select(Subscriber))] == ["someone@example.com"]
        assert client.post("/subscribe", data={"email": "nope"}).status_code == 400

    def test_chart_bars_have_titles(self):
        svg = trend_svg({2024: {"483": 2, "wl": 0}, 2025: {"483": 1, "wl": 3}})
        assert svg.count("<title>") == 4 and "2025, Warning letter: 3 citations" in svg


class TestContent:
    def test_pattern_validation(self, content):
        p = load_patterns(content / "remediation")["CS-AUD-002"]
        assert p.publishable and "<h2>Fix steps</h2>" in p.html
        bad = content / "remediation" / "CS-AUD-001.md"
        bad.write_text(PATTERN.replace("CS-AUD-002", "CS-AUD-001").replace("## Fix steps", "## Fixes")
                       .replace("Text.", "Text — more."))
        q = load_patterns(content / "remediation")["CS-AUD-001"]
        assert not q.publishable
        assert "missing section: Fix steps" in q.problems and any("em dash" in x for x in q.problems)

    def test_digest_draft_and_publish(self, db_url, taxonomy, content):
        seed(db_url, taxonomy)
        with Session(create_engine(db_url)) as s:
            path = draft_issue(s, taxonomy, 2025, 3, out_dir=content / "digest",
                               remediation_dir=content / "remediation", allowed={"CS-AUD-002"})
        text = path.read_text()
        assert "Audit trail not routinely reviewed (CS-AUD-002) | 1 |" in text
        assert "CS-SPR-002" not in text.split("-->")[-1]  # gated mode not in the published sections
        assert "Remediation spotlight" in text and "/modes/CS-AUD-002" in text
        with pytest.raises(FileExistsError):
            with Session(create_engine(db_url)) as s:
                draft_issue(s, taxonomy, 2025, 3, out_dir=content / "digest",
                            remediation_dir=content / "remediation", allowed=None)
        with pytest.raises(ValueError, match="placeholders"):
            publish_issue(path, email_dir=content / "email")

        body = text.replace("[EDITOR:", "Filled in. [x").replace("]", "")
        path.write_text(body.replace("Filled in. [x", "Filled in."))
        path.write_text(path.read_text().replace("Filled in.", "Audit-trail review moved — a lot."))
        with pytest.raises(CopyStyleError):
            publish_issue(path, email_dir=content / "email")
        path.write_text(path.read_text().replace("— a lot", "a lot"))
        out = publish_issue(path, email_dir=content / "email")
        assert out.exists() and "<table>" in out.read_text()
        assert archive(content / "digest")[0]["issue"] == "2025-03"
        client = TestClient(create_app(database_url=db_url, content_dir=content, taxonomy=taxonomy))
        assert "Audit-trail review moved a lot" in client.get("/digest/2025-03").text


class TestCopyLint:
    def test_detects(self):
        assert find_em_dashes("a — b")
        assert find_em_dashes("word--word")
        assert find_em_dashes("this - that")
        assert not find_em_dashes("pre-approval, 21 CFR 211.68(b), CS-AUD-002")
        assert not find_em_dashes("- a bullet\n```\ncode -- here\n```")
        assert not find_em_dashes("run `auditcore ingest dashboard --product-type Drugs` first")
        assert find_em_dashes("`code` then prose--more")
        with pytest.raises(CopyStyleError):
            assert_clean("x — y", "t")

    def test_repo_user_facing_copy_is_clean(self):
        files = [*(ROOT / "content").rglob("*.md"), *(ROOT / "src/auditcore/web/templates").glob("*.html"),
                 ROOT / "src/auditcore/taxonomy/taxonomy_v1.yaml"]
        assert lint_paths(files) == []


def report_data(html: str) -> dict:
    import json as _json

    raw = html.split('<script id="report-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    return _json.loads(raw)


def test_static_report_and_csv(session, archive, wl_html, taxonomy, tmp_path):
    import csv as _csv
    from auditcore.ingest.warning_letters import ingest_letter_html
    from auditcore.models import WarningLetter
    from auditcore.reports.static import build_report
    from auditcore.tagging.pipeline import run_pipeline

    wl = WarningLetter(cms_number="699001", company_name="Example Pharma Inc.", issue_date=date(2025, 3, 4),
                       issuing_office="Center for Drug Evaluation and Research (CDER)")
    food = WarningLetter(cms_number="699002", company_name="Food Co", issue_date=date(2025, 3, 5),
                         issuing_office="Human Foods Program")
    session.add_all([wl, food])
    session.flush()
    ingest_letter_html(session, archive, wl, wl_html, url="https://example.test/wl")
    ingest_letter_html(session, archive, food, wl_html.replace(b"699001", b"699002"), url="https://example.test/food")
    run_pipeline(session, taxonomy)

    out = build_report(session, taxonomy, tmp_path / "rep", samples=3)
    html = out["html"].read_text()
    data = report_data(html)
    assert not data["accepted"] and "a person has not yet reviewed" in html     # unreviewed basis is stated
    assert "CS-ACC-001" in data["modes"] and data["modes"]["CS-ACC-001"][0] == "Shared logins"
    assert data["years"] == [2025] and data["docs"]["2025"] == [1, 0]           # food letter not counted
    assert any("211.68" in f[4] for f in data["f"])                            # CFR sections per finding
    sample = data["samples"]["CS-ACC-001"][0]
    assert sample["fac"] == "Example Pharma Inc." and sample["url"] == "https://example.test/wl"
    assert data["excluded"] == 1
    assert "\u2014" not in html and not find_em_dashes(html)                   # house style
    assert 'id="years"' in html and "Per 100 documents" in html              # year filter, rate toggle

    rows = list(_csv.DictReader(out["csv"].open()))
    assert {r["in_scope"] for r in rows} == {"yes", "no"}
    assert any("CS-ACC-001" in r["proposed_modes_unreviewed"] for r in rows)
    assert all(r["text"] for r in rows)
    assert "paper_only_proposed" in rows[0] and "paper_only_accepted" in rows[0]
