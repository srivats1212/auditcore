"""Review decisions can be reversed, every change is in the audit trail, and
counts always follow the current decision. The local web review page writes
through the same functions as the CLI."""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from auditcore.analytics.stats import mode_trend, top_modes
from auditcore.models import Citation, Document, FailureMode, Observation, ReviewEvent
from auditcore.reports.static import load_rows
from auditcore.tagging.review import apply_decision, history, progress, review_items, review_queue
from auditcore.web.app import create_app, highlight

AUD = "Audit trails on the HPLC were not reviewed before batch release."
PAPER = "Weights were recorded on scrap paper and later transcribed into the logbook."


def finding(s, taxonomy, text, mode, *, n, rules=0.9, llm=None, llm_paper=False, rules_paper=False, cfrs=1,
            llm_none=False):
    """One warning-letter violation with ``cfrs`` sibling citations, each
    carrying the given machine proposals."""
    doc = Document(source="warning_letter", source_key=f"wl:r{n}", content_hash=f"r{n}", raw_path="x",
                   media_type="text/html", source_url="https://example.test/r")
    s.add(doc)
    s.flush()
    obs = Observation(document_id=doc.id, kind="wl", number=1, text=text, observed_date=date(2026, 5, 1))
    s.add(obs)
    s.flush()
    first = None
    for i in range(cfrs):
        cit = Citation(observation_id=obs.id, ordinal=i, text=text)
        s.add(cit)
        s.flush()
        first = first or cit
        if rules is not None:
            s.add(FailureMode(citation_id=cit.id, mode_id=mode, taxonomy_version=taxonomy.version,
                              provenance="rules", confidence=rules, evidence=text.split(" were")[0],
                              paper_only=rules_paper))
        if llm is not None:
            s.add(FailureMode(citation_id=cit.id, mode_id=mode, taxonomy_version=taxonomy.version,
                              provenance="llm", confidence=llm, evidence=text.split(" were")[0],
                              paper_only=llm_paper))
        if llm_none:
            s.add(FailureMode(citation_id=cit.id, mode_id="NONE", taxonomy_version=taxonomy.version,
                              provenance="llm", status="rejected", reviewer="system"))
    s.flush()
    return first


def counted(s, taxonomy, mode):
    """The mode's count as every published surface sees it: trend table,
    home-page totals, and the static report's accepted columns."""
    trend = sum(v["wl"] + v["483"] for v in mode_trend(s, mode, taxonomy.version).values())
    home = dict(top_modes(s, taxonomy.version, since_year=2026)).get(mode, 0)
    report = sum(mode in r.accepted for r in load_rows(s))
    return trend, home, report


def decide(s, taxonomy, cit, mode, action, reviewer="sk", reason=None, interface="cli"):
    accept, flag = {"a": (True, None), "p": (True, True), "e": (True, False), "r": (False, None)}[action]
    return apply_decision(s, cit.id, mode, accept=accept, paper_only=flag, reviewer=reviewer, reason=reason,
                          interface=interface, taxonomy_version=taxonomy.version)


class TestReversal:
    def test_accept_then_reverse_removes_from_counts(self, session, taxonomy):
        cit = finding(session, taxonomy, AUD, "CS-AUD-002", n=1)
        decide(session, taxonomy, cit, "CS-AUD-002", "a")
        assert counted(session, taxonomy, "CS-AUD-002") == (1, 1, 1)
        assert decide(session, taxonomy, cit, "CS-AUD-002", "r", reason="audit trail was reviewed late, not skipped")
        assert counted(session, taxonomy, "CS-AUD-002") == (0, 0, 0)
        human = session.scalars(select(FailureMode).where(FailureMode.provenance == "human")).one()
        assert human.status == "rejected"
        assert {f.status for f in session.scalars(select(FailureMode).where(FailureMode.provenance == "rules"))} == {"rejected"}

    def test_reject_then_reverse_adds_back(self, session, taxonomy):
        cit = finding(session, taxonomy, AUD, "CS-AUD-002", n=1)
        decide(session, taxonomy, cit, "CS-AUD-002", "r")
        assert counted(session, taxonomy, "CS-AUD-002") == (0, 0, 0)
        decide(session, taxonomy, cit, "CS-AUD-002", "a")
        assert counted(session, taxonomy, "CS-AUD-002") == (1, 1, 1)

    def test_reversing_a_reject_keeps_paper_only_out_of_counts(self, session, taxonomy):
        cit = finding(session, taxonomy, PAPER, "CS-DI-001", n=1, rules_paper=True)
        decide(session, taxonomy, cit, "CS-DI-001", "r")
        decide(session, taxonomy, cit, "CS-DI-001", "a")  # as proposed: paper only
        assert counted(session, taxonomy, "CS-DI-001") == (0, 0, 0)
        assert any("CS-DI-001" in r.accepted_paper for r in load_rows(session))

    def test_paper_only_flips_update_counts(self, session, taxonomy):
        cit = finding(session, taxonomy, PAPER, "CS-DI-001", n=1, rules_paper=True)
        decide(session, taxonomy, cit, "CS-DI-001", "e")
        assert counted(session, taxonomy, "CS-DI-001") == (1, 1, 1)
        decide(session, taxonomy, cit, "CS-DI-001", "p")
        assert counted(session, taxonomy, "CS-DI-001") == (0, 0, 0)
        decide(session, taxonomy, cit, "CS-DI-001", "e")
        assert counted(session, taxonomy, "CS-DI-001") == (1, 1, 1)

    def test_reversal_covers_sibling_citations_and_counts_the_violation_once(self, session, taxonomy):
        cit = finding(session, taxonomy, AUD, "CS-AUD-002", n=1, cfrs=3)
        decide(session, taxonomy, cit, "CS-AUD-002", "a")
        assert counted(session, taxonomy, "CS-AUD-002") == (1, 1, 3)  # report rows are citations
        decide(session, taxonomy, cit, "CS-AUD-002", "r")
        assert counted(session, taxonomy, "CS-AUD-002") == (0, 0, 0)
        assert session.scalar(select(ReviewEvent.id).where(ReviewEvent.new_decision == "rejected")
                              .order_by(ReviewEvent.id.desc())) is not None
        assert len(session.scalars(select(ReviewEvent).where(ReviewEvent.new_decision == "rejected")).all()) == 3

    def test_audit_trail_records_every_change(self, session, taxonomy):
        cit = finding(session, taxonomy, PAPER, "CS-DI-001", n=1, rules_paper=True)
        decide(session, taxonomy, cit, "CS-DI-001", "a", reviewer="sk")
        assert not decide(session, taxonomy, cit, "CS-DI-001", "p", reviewer="sk")  # same as current: no-op
        decide(session, taxonomy, cit, "CS-DI-001", "e", reviewer="jt", reason="logbook is printed from LIMS")
        decide(session, taxonomy, cit, "CS-DI-001", "r", reviewer="sk", interface="web")
        events = history(session, cit.id, "CS-DI-001")
        assert [(e.previous_decision, e.previous_paper_only, e.new_decision, e.new_paper_only, e.reviewer,
                 e.interface) for e in events] == [
            ("proposed", None, "accepted", True, "sk", "cli"),
            ("accepted", True, "accepted", False, "jt", "cli"),
            ("accepted", False, "rejected", None, "sk", "web"),
        ]
        assert [e.reason for e in events] == [None, "logbook is printed from LIMS", None]
        assert all(e.created_at is not None and e.citation_id == cit.id for e in events)
        assert [e.created_at for e in events] == sorted(e.created_at for e in events)

    def test_decided_items_leave_the_queue_and_appear_in_the_decided_list(self, session, taxonomy):
        a = finding(session, taxonomy, AUD, "CS-AUD-002", n=1)
        b = finding(session, taxonomy, PAPER, "CS-DI-001", n=2, rules_paper=True)
        decide(session, taxonomy, a, "CS-AUD-002", "a")
        assert [i.citation_id for i in review_queue(session)] == [b.id]
        done = review_items(session, state="decided")
        assert [(i.citation_id, i.decision, i.decided_paper_only, i.reviewer) for i in done] == \
            [(a.id, "accepted", False, "sk")]
        decide(session, taxonomy, a, "CS-AUD-002", "r")
        assert review_items(session, state="decided")[0].decision == "rejected"
        assert [i.citation_id for i in review_queue(session)] == [b.id]  # a reversal does not re-queue

    def test_new_proposal_after_a_decision_is_queued_again(self, session, taxonomy):
        cit = finding(session, taxonomy, AUD, "CS-AUD-002", n=1)
        decide(session, taxonomy, cit, "CS-AUD-002", "a")
        session.add(FailureMode(citation_id=cit.id, mode_id="CS-AUD-002", taxonomy_version=taxonomy.version,
                                provenance="llm", confidence=0.8, evidence="Audit trails"))
        session.flush()
        item = review_queue(session)[0]
        assert item.pending and item.decision == "accepted" and item.sources == "llm+rules"
        assert decide(session, taxonomy, cit, "CS-AUD-002", "a")  # clears the new proposal
        assert review_queue(session) == []


class TestFilters:
    def seed(self, s, taxonomy):
        both = finding(s, taxonomy, AUD, "CS-AUD-002", n=1, llm=0.95, llm_none=False)
        rules_only = finding(s, taxonomy, "Users shared one login on the CDS were seen.", "CS-ACC-001", n=2,
                             rules=0.9, llm_none=True)
        llm_low = finding(s, taxonomy, "Trial injections were run before the official sequence.", "CS-LAB-001",
                          n=3, rules=None, llm=0.5)
        flag_split = finding(s, taxonomy, PAPER, "CS-DI-001", n=4, rules_paper=True, llm=0.8, llm_paper=False)
        never_llm = finding(s, taxonomy, "Backups were never restored in a test.", "CS-BKP-001", n=5)
        return both, rules_only, llm_low, flag_split, never_llm

    def ids(self, s, **kw):
        return {i.citation_id for i in review_items(s, **kw)}

    def test_filters(self, session, taxonomy):
        both, rules_only, llm_low, flag_split, never_llm = self.seed(session, taxonomy)
        assert self.ids(session, mode_id="CS-LAB-001") == {llm_low.id}
        assert self.ids(session, proposer="rules") == {rules_only.id, never_llm.id}
        assert self.ids(session, proposer="llm") == {llm_low.id}
        assert self.ids(session, proposer="both") == {both.id, flag_split.id}
        # LLM saw it and proposed nothing; LLM-only; both proposed with different flags. Not: never seen by the LLM.
        assert self.ids(session, disagreements=True) == {rules_only.id, llm_low.id, flag_split.id}
        assert self.ids(session, low_confidence=True) == {llm_low.id}
        assert self.ids(session, paper="paper") == {flag_split.id}  # rules (0.9) outranks the LLM (0.8)
        assert flag_split.id not in self.ids(session, paper="electronic")
        decide(session, taxonomy, flag_split, "CS-DI-001", "e")
        assert flag_split.id in self.ids(session, state="decided", paper="electronic")
        assert flag_split.id not in self.ids(session, paper="electronic")  # pending list only

    def test_progress(self, session, taxonomy):
        both, rules_only, *_ = self.seed(session, taxonomy)
        assert (progress(session).total, progress(session).reviewed, progress(session).precision) == (5, 0, None)
        decide(session, taxonomy, both, "CS-AUD-002", "a")
        decide(session, taxonomy, rules_only, "CS-ACC-001", "r")
        p = progress(session)
        assert (p.total, p.reviewed, p.accepted, p.rejected, p.precision) == (5, 2, 1, 1, 0.5)


@pytest.fixture
def web(db_url, taxonomy, tmp_path):
    content = tmp_path / "content"
    (content / "remediation").mkdir(parents=True)
    (content / "digest").mkdir()
    engine = create_engine(db_url)
    with Session(engine) as s:
        cits = TestFilters().seed(s, taxonomy)
        s.commit()
        ids = [c.id for c in cits]
    client = TestClient(create_app(database_url=db_url, content_dir=content, taxonomy=taxonomy,
                                   review=True, reviewer="sk"))
    return client, engine, ids


def token(page) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def post(client, page, cid, mode, action, reason="", back="/review"):
    return client.post("/review/decide", data={"citation_id": cid, "mode_id": mode, "action": action,
                                               "reason": reason, "csrf": token(page), "back": back},
                       follow_redirects=False)


class TestWebReview:
    def test_off_by_default(self, db_url, taxonomy, tmp_path):
        client = TestClient(create_app(database_url=db_url, content_dir=tmp_path, taxonomy=taxonomy))
        assert client.get("/review").status_code == 404
        assert client.post("/review/decide", data={}).status_code in (404, 405)
        assert 'href="/review"' not in client.get("/methodology").text

    def test_page_shows_the_review_information(self, web):
        client, _, ids = web
        page = client.get("/review")
        assert page.status_code == 200
        assert "Reviewed <b>0</b> of <b>5</b>" in page.text
        assert "Audit trail not routinely reviewed" in page.text  # mode name
        assert "Audit trails exist but are not reviewed" in page.text  # definition
        assert "<mark>Audit trails on the HPLC</mark>" in page.text  # evidence highlighted in the text
        assert "confidence 0.95" in page.text and "Proposed by <b>llm+rules</b>" in page.text
        assert "record type proposed: <b>paper only (not counted)</b>" in page.text
        assert "rules and LLM disagree" in page.text
        for key in ("a", "p", "e", "r", "s"):
            assert f'data-key="{key}"' in page.text

    def test_decisions_write_the_same_state_as_the_cli(self, web, taxonomy):
        client, engine, ids = web
        both, rules_only, llm_low, flag_split, never_llm = ids
        page = client.get("/review")
        r = post(client, page, both, "CS-AUD-002", "a", back="/review?mode=&page=1&focus=0")
        assert r.status_code == 303 and r.headers["location"] == "/review?mode=&page=1&focus=0"
        post(client, page, flag_split, "CS-DI-001", "e")
        post(client, page, rules_only, "CS-ACC-001", "r", reason="login was not shared")
        with Session(engine) as s:
            assert {i.citation_id for i in review_queue(s)} == {llm_low, never_llm}  # what the CLI would show
            assert counted(s, taxonomy, "CS-AUD-002") == (1, 1, 1)
            assert counted(s, taxonomy, "CS-DI-001") == (1, 1, 1)  # accepted as electronic: counts
            events = s.scalars(select(ReviewEvent).order_by(ReviewEvent.id)).all()
            assert [(e.citation_id, e.new_decision, e.reviewer, e.interface) for e in events] == [
                (both, "accepted", "sk", "web"), (flag_split, "accepted", "sk", "web"),
                (rules_only, "rejected", "sk", "web")]
            assert events[-1].reason == "login was not shared"
            # A CLI decision shows up on the web page.
            apply_decision(s, never_llm, "CS-BKP-001", accept=True, reviewer="jt", taxonomy_version=taxonomy.version)
            s.commit()
        page = client.get("/review")
        assert "Reviewed <b>4</b> of <b>5</b>" in page.text and "Accepted <b>3</b>" in page.text
        assert "Running precision (accepted / decided) <b>0.75</b>" in page.text
        decided = client.get("/review", params={"state": "decided"})
        assert "by jt" in decided.text and "Change decision" in decided.text

    def test_change_decision_button_reverses(self, web, taxonomy):
        client, engine, ids = web
        both = ids[0]
        post(client, client.get("/review"), both, "CS-AUD-002", "a")
        decided = client.get("/review", params={"state": "decided"})
        assert '<details class="change">' in decided.text
        post(client, decided, both, "CS-AUD-002", "r", reason="QA did review it")
        with Session(engine) as s:
            assert counted(s, taxonomy, "CS-AUD-002") == (0, 0, 0)
            assert [(e.previous_decision, e.new_decision) for e in history(s, both, "CS-AUD-002")] == [
                ("proposed", "accepted"), ("accepted", "rejected")]
        page = client.get("/review", params={"state": "decided"})
        assert "accepted to rejected by sk via web: QA did review it" in " ".join(page.text.split())

    def test_filters_and_pagination(self, web, db_url, taxonomy):
        client, engine, ids = web
        both, rules_only, llm_low, flag_split, never_llm = ids
        text = client.get("/review", params={"proposer": "llm"}).text
        assert "1 item in this list" in text and "CS-LAB-001" in text
        assert "3 items in this list" in client.get("/review", params={"disagree": "1"}).text
        assert "1 item in this list" in client.get("/review", params={"low": "1"}).text
        assert "1 item in this list" in client.get("/review", params={"paper": "paper"}).text
        with Session(engine) as s:
            for n in range(60):
                finding(s, taxonomy, f"Audit trails were not reviewed on system {n}.", "CS-AUD-002", n=100 + n)
            s.commit()
        first = client.get("/review")
        assert "65 items in this list, page 1 of 2" in first.text and first.text.count('class="card item"') == 50
        second = client.get("/review", params={"page": 2})
        assert second.text.count('class="card item"') == 15 and "Previous page" in second.text

    def test_rejects_bad_posts(self, web):
        client, _, ids = web
        page = client.get("/review")
        data = {"citation_id": ids[0], "mode_id": "CS-AUD-002", "action": "a", "csrf": "wrong"}
        assert client.post("/review/decide", data=data).status_code == 403
        assert post(client, page, ids[0], "CS-AUD-002", "x").status_code == 400
        assert post(client, page, ids[0], "CS-NOPE-001", "a").status_code == 400
        r = post(client, page, ids[0], "CS-AUD-002", "a", back="https://evil.test/")
        assert r.headers["location"] == "/review"


def test_highlight_escapes_and_marks():
    out = highlight("A <b> audit\ntrail was off. Audit trail again.", ["audit trail", ""])
    assert str(out) == "A &lt;b&gt; <mark>audit\ntrail</mark> was off. <mark>Audit trail</mark> again."


def test_cli_amend(db_url, taxonomy, monkeypatch, capsys):
    from auditcore import cli, db

    engine = db.get_engine(db_url)
    with Session(engine) as s:
        cit = finding(s, taxonomy, PAPER, "CS-DI-001", n=1, rules_paper=True)
        s.commit()
        cid = cit.id
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    cli.main(["review", "--reviewer", "sk"])
    answers = iter(["e", "printed from the LIMS"])
    cli.main(["review", "--reviewer", "jt", "--amend"])
    out = capsys.readouterr().out
    assert "current decision: accepted as paper only by sk" in out
    with Session(engine) as s:
        assert [(e.new_decision, e.new_paper_only, e.reviewer, e.reason) for e in history(s, cid, "CS-DI-001")] == [
            ("accepted", True, "sk", None), ("accepted", False, "jt", "printed from the LIMS")]
        assert counted(s, taxonomy, "CS-DI-001") == (1, 1, 1)
    cli.main(["review-log"])
    assert "accepted (paper only) -> accepted (electronic/hybrid)  by jt via cli  reason: printed from the LIMS" \
        in capsys.readouterr().out
