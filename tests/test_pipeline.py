import csv
import json
from datetime import date

import pytest
from sqlalchemy import select

from auditcore.analytics.stats import compute_annual_stats, escalation_rates, methodology_note, mode_trend
from auditcore.evaluation.gold import (
    adjudicate,
    agreement,
    cohen_kappa,
    evaluate,
    export_blind,
    import_labels,
    passing_modes,
    sample_gold,
)
from auditcore.ingest.form483 import ingest_pdf_files
from auditcore.ingest.warning_letters import ingest_letter_html
from auditcore.models import Citation, FailureMode, Observation, WarningLetter
from auditcore.tagging.llm import LlmRefusal, LlmTagger, evidence_in_text
from auditcore.tagging.pipeline import run_pipeline
from auditcore.tagging.review import decide, review_queue


def tags_of(session, cid, provenance):
    return sorted(session.scalars(select(FailureMode.mode_id).where(
        FailureMode.citation_id == cid, FailureMode.provenance == provenance, FailureMode.mode_id != "NONE")))


@pytest.fixture
def loaded(session, archive, wl_html, form483_pdf, tmp_path):
    """One warning letter and one 483 for the same facility, segmented and rule-tagged."""
    wl = WarningLetter(cms_number="699001", company_name="Example Pharma Inc.", issue_date=date(2025, 3, 4))
    session.add(wl)
    session.flush()
    ingest_letter_html(session, archive, wl, wl_html, url="https://example.test/wl")
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(form483_pdf)
    manifest = tmp_path / "m.csv"
    # Same FEI as the warning letter, earlier date: an escalation candidate.
    manifest.write_text("file,fei,company,record_date\nf.pdf,3009990001,Example Pharma Inc.,10/10/2024\n")
    ingest_pdf_files(session, archive, [pdf], manifest)
    stats = run_pipeline(session, __import__("auditcore.taxonomy.loader", fromlist=["x"]).load_taxonomy())
    return stats


class TestPipeline:
    def test_segments_and_rule_tags(self, session, loaded):
        assert loaded["documents"] == 2
        wl_obs = session.scalars(select(Observation).where(Observation.kind == "wl").order_by(Observation.number)).all()
        assert [o.number for o in wl_obs] == [1, 2, 3]
        assert all(o.facility_id is not None for o in wl_obs)

        c1 = wl_obs[0].citations[0]
        assert c1.cfr_reference.canonical == "21 CFR 211.68(b)"
        assert tags_of(session, c1.id, "rules") == ["CS-ACC-001", "CS-ACC-002", "CS-AUD-002"]

        # Observation 2 is the cloudy-solution / shared-equipment trap: no tags.
        assert tags_of(session, wl_obs[1].citations[0].id, "rules") == []
        assert tags_of(session, wl_obs[2].citations[0].id, "rules") == ["CS-SPR-001", "CS-SPR-002"]

        obs483 = session.scalars(select(Observation).where(Observation.kind == "483").order_by(Observation.number)).all()
        assert tags_of(session, obs483[0].citations[0].id, "rules") == ["CS-AUD-001", "CS-VAL-001"]
        # Observation 2: adequate audit trail sentence is ignored, the restore finding is kept.
        assert tags_of(session, obs483[1].citations[0].id, "rules") == ["CS-BKP-003"]

    def test_rerun_is_idempotent(self, session, loaded, taxonomy):
        before = session.query(FailureMode).count()
        again = run_pipeline(session, taxonomy)
        assert again["rule_tags"] == 0 and session.query(FailureMode).count() == before


class TestLlm:
    def test_tagger_request_and_evidence_check(self, session, loaded, taxonomy, fake_client_factory, fake_response):
        def responder(kw):
            text = kw["messages"][0]["content"]
            if "spreadsheet" in text:
                return fake_response(json.dumps({"tags": [
                    {"mode_id": "CS-SPR-001", "evidence": "Excel spreadsheet that had not been validated", "confidence": 0.95},
                    {"mode_id": "CS-DI-005", "evidence": "results were backdated", "confidence": 0.6},  # not in text
                ]}))
            return fake_response(json.dumps({"tags": []}))

        client = fake_client_factory(responder)
        tagger = LlmTagger(taxonomy, client=client, model="claude-opus-5-5", effort="medium")
        stats = run_pipeline(session, taxonomy, tagger=tagger)
        assert stats["llm_errors"] == 0 and stats["llm_tags"] == 1

        call = client.messages.calls[0]
        assert call["model"] == "claude-opus-5-5"
        assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
        assert call["output_config"]["format"]["type"] == "json_schema"
        assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}  # taxonomy prefix is cached

        spr = session.scalars(select(FailureMode).where(FailureMode.provenance == "llm", FailureMode.mode_id == "CS-SPR-001")).one()
        assert spr.status == "proposed" and spr.model == "claude-opus-5-5"
        assert not session.scalars(select(FailureMode).where(FailureMode.mode_id == "CS-DI-005")).first()

        # Second run does not call the model again for citations already tagged.
        n_calls = len(client.messages.calls)
        run_pipeline(session, taxonomy, tagger=tagger)
        assert len(client.messages.calls) == n_calls

    def test_refusal_is_reported(self, taxonomy, fake_client_factory, fake_response):
        client = fake_client_factory(lambda kw: fake_response("", stop_reason="refusal"))
        with pytest.raises(LlmRefusal):
            LlmTagger(taxonomy, client=client).tag("x", doc_kind="483", cfr=None)

    def test_evidence_match_normalizes_whitespace(self):
        assert evidence_in_text("audit  trail was\nnot reviewed.", "The audit trail was not reviewed by QA.")
        assert not evidence_in_text("", "anything")


class TestReview:
    def test_accept_and_reject(self, session, loaded):
        items = review_queue(session)
        assert items and all(i.proposals for i in items)
        first = items[0]
        decide(session, first.citation_id, first.mode_id, accept=True, reviewer="sk")
        human = session.scalars(select(FailureMode).where(FailureMode.citation_id == first.citation_id,
                                                          FailureMode.mode_id == first.mode_id,
                                                          FailureMode.provenance == "human")).one()
        assert human.status == "accepted" and human.evidence
        second = items[1]
        decide(session, second.citation_id, second.mode_id, accept=False, reviewer="sk")
        assert {i.mode_id for i in review_queue(session) if i.citation_id == first.citation_id} \
            .isdisjoint({first.mode_id})


def _write_labels(path, rows):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["citation_id", "mode_ids", "notes"])
        for cid, modes in rows:
            w.writerow([cid, ";".join(modes), ""])


class TestGoldAndAnalytics:
    def _accept_all_rule_tags(self, session):
        for fm in list(session.scalars(select(FailureMode).where(FailureMode.provenance == "rules"))):
            decide(session, fm.citation_id, fm.mode_id, accept=True, reviewer="sk")

    def test_blind_export_has_no_pipeline_output(self, session, loaded, tmp_path):
        ids = sample_gold(session, 10)
        assert len(ids) == len(set(ids)) == session.query(Citation).count()
        out = tmp_path / "blind.csv"
        export_blind(session, ids, out)
        header = out.read_text().splitlines()[0]
        assert header == "citation_id,text,mode_ids,notes"
        assert "CS-" not in out.read_text()

    def test_kappa(self):
        assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
        assert cohen_kappa([], []) is None

    def test_full_evaluation_gate_and_stats(self, session, loaded, taxonomy, tmp_path):
        self._accept_all_rule_tags(session)
        cits = {c.id: c for c in session.scalars(select(Citation))}
        truth = {cid: tags_of(session, cid, "human") for cid in cits}
        # Gold says the WL spreadsheet citation is NOT CS-SPR-002 (cells were "not locked" is ambiguous
        # for this test): SPR-002 precision drops to 0 and it must fail the gate.
        spr_cid = next(cid for cid, t in truth.items() if "CS-SPR-002" in t)
        gold = {cid: [m for m in t if not (cid == spr_cid and m == "CS-SPR-002")] for cid, t in truth.items()}

        a, b = tmp_path / "a.csv", tmp_path / "b.csv"
        _write_labels(a, gold.items())
        b_rows = dict(gold)
        b_rows[spr_cid] = b_rows[spr_cid] + ["CS-SPR-002"]  # tagger B disagrees on one citation
        _write_labels(b, b_rows.items())
        import_labels(session, a, tagger="A", taxonomy=taxonomy)
        import_labels(session, b, tagger="B", taxonomy=taxonomy)

        ag = agreement(session, taxonomy)
        assert ag["CS-AUD-002"]["kappa"] == 1.0 and ag["CS-SPR-002"]["kappa"] < 1.0

        disputes = tmp_path / "disputes.csv"
        res = adjudicate(session, taxonomy, disagreements_csv=disputes)
        assert res == {"agreed": len(gold) - 1, "disputed": 1, "resolved": 0}
        resolutions = tmp_path / "resolved.csv"
        _write_labels(resolutions, [(spr_cid, gold[spr_cid])])
        assert adjudicate(session, taxonomy, resolutions_csv=resolutions)["resolved"] == 1

        scores = evaluate(session, taxonomy, "published")
        assert scores["CS-AUD-002"].precision == 1.0 and scores["CS-AUD-002"].recall == 1.0
        assert scores["CS-SPR-002"].precision == 0.0
        live = passing_modes(session, taxonomy.version, bar=0.85, min_support=1)
        assert "CS-AUD-002" in live and "CS-SPR-002" not in live
        # min_support guards against shipping a mode on one lucky example
        assert passing_modes(session, taxonomy.version, bar=0.85, min_support=3) == set()

        compute_annual_stats(session, taxonomy.version)
        assert mode_trend(session, "CS-AUD-002", taxonomy.version) == {2025: {"483": 0, "wl": 1}}
        assert mode_trend(session, "CS-AUD-001", taxonomy.version) == {2024: {"483": 1, "wl": 0}}
        note = methodology_note(session, taxonomy.version, ["CS-AUD-002"]).render()
        assert "failure-mode totals count each violation once; the CFR breakdown counts each section cited" in note
        assert "precision 1.00" in note

    def test_escalation(self, session, loaded, taxonomy):
        self._accept_all_rule_tags(session)
        # Accept a WL tag that matches a 483 mode for the same FEI so an escalation exists.
        wl_cit = session.scalars(select(Citation).join(Observation).where(Observation.kind == "wl")).first()
        decide(session, wl_cit.id, "CS-AUD-001", accept=True, reviewer="sk")
        rates = escalation_rates(session, taxonomy.version)
        assert rates["CS-AUD-001"] == {"facilities_483": 1, "escalated": 1, "rate": 1.0}
        assert rates["CS-VAL-001"]["escalated"] == 0


def test_counting_rule_violation_once_sections_each(session, taxonomy):
    """Approved rule: failure-mode totals count each violation once; the CFR
    breakdown counts each section cited."""
    from auditcore.analytics.stats import cfr_breakdown
    from auditcore.models import CfrReference, Document

    doc = Document(source="warning_letter", source_key="wl:9", content_hash="h", raw_path="x", media_type="text/html")
    session.add(doc)
    session.flush()
    obs = Observation(document_id=doc.id, kind="wl", number=1, observed_date=date(2025, 5, 1),
                      text="Your firm failed to control computer systems (21 CFR 211.68(b), 211.100(a), and 211.192).")
    session.add(obs)
    session.flush()
    for i, sec in enumerate(["211.68", "211.100", "211.192"]):
        ref = CfrReference(canonical=f"21 CFR {sec}", part="211", section=sec)
        session.add(ref)
        session.flush()
        cit = Citation(observation_id=obs.id, ordinal=i, text=obs.text, cfr_reference_id=ref.id)
        session.add(cit)
        session.flush()
        session.add(FailureMode(citation_id=cit.id, mode_id="CS-ACC-002", taxonomy_version=taxonomy.version,
                                provenance="rules", confidence=0.9))
    session.flush()
    first = session.scalars(select(Citation).where(Citation.observation_id == obs.id)).first()
    decide(session, first.id, "CS-ACC-002", accept=True, reviewer="sk")  # one decision covers all three

    compute_annual_stats(session, taxonomy.version)
    assert mode_trend(session, "CS-ACC-002", taxonomy.version) == {2025: {"483": 0, "wl": 1}}
    assert sorted(cfr_breakdown(session, "CS-ACC-002", taxonomy.version)) == [("211.100", 1), ("211.192", 1), ("211.68", 1)]


def test_usage_and_cost_accounting(taxonomy, fake_client_factory, fake_response):
    from types import SimpleNamespace

    def responder(kw):
        r = fake_response(json.dumps({"tags": []}))
        first = not responder.seen
        responder.seen = True
        r.model = "claude-opus-5-5"
        r.usage = SimpleNamespace(input_tokens=300, output_tokens=200,
                                  cache_creation_input_tokens=5000 if first else 0,
                                  cache_read_input_tokens=0 if first else 5000)
        return r
    responder.seen = False

    tagger = LlmTagger(taxonomy, client=fake_client_factory(responder))
    for _ in range(3):
        tagger.tag("Audit trails were not reviewed.", doc_kind="wl", cfr=None)
    u = tagger.usage.as_dict()
    assert u["calls"] == 3 and u["cache_write_tokens"] == 5000 and u["cache_read_tokens"] == 10000
    # 900 in x $4 + 600 out x $20 + 5000 write x $5 + 10000 read x $0.20, per million
    assert u["cost_usd"] == round((900 * 4 + 600 * 20 + 5000 * 5 + 10000 * 0.2) / 1e6, 4)
    assert u["models"] == {"claude-opus-5-5": 3}


def test_llm_max_caps_calls(session, loaded, taxonomy, fake_client_factory, fake_response):
    client = fake_client_factory(lambda kw: fake_response(json.dumps({"tags": []})))
    stats = run_pipeline(session, taxonomy, tagger=LlmTagger(taxonomy, client=client), llm_max=2)
    assert len(client.messages.calls) == 2 and stats["llm_usage"]["calls"] == 2
