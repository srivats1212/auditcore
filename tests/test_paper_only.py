"""Decision 1: paper-only data-integrity findings are not computerized-systems
findings. They carry ``paper_only`` so they stay searchable, and no count
includes them."""

import json
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from auditcore.analytics.stats import compute_annual_stats, mode_trend
from auditcore.models import Citation, Document, FailureMode, Observation
from auditcore.tagging.llm import SYSTEM_PROMPT, LlmTagger, output_schema, strip_request_tail, taxonomy_block
from auditcore.tagging.pipeline import llm_tag_citation, rules_tag_citation
from auditcore.tagging.review import decide, review_queue

PAPER = "Weights were recorded on scrap paper and later transcribed into the logbook."
HYBRID = "HPLC peak areas were recorded on scrap paper and later transcribed into the logbook."


def citation(session, text, n=1):
    doc = Document(source="warning_letter", source_key=f"wl:p{n}", content_hash=f"h{n}", raw_path="x",
                   media_type="text/html", source_url="https://example.test/p")
    session.add(doc)
    session.flush()
    obs = Observation(document_id=doc.id, kind="wl", number=1, text=text, observed_date=date(2026, 3, 1))
    session.add(obs)
    session.flush()
    cit = Citation(observation_id=obs.id, ordinal=0, text=text)
    session.add(cit)
    session.flush()
    return cit


def flags(session, cid):
    return {(f.mode_id, f.provenance): f.paper_only for f in session.scalars(
        select(FailureMode).where(FailureMode.citation_id == cid))}


def test_taxonomy_marks_paper_capable_modes(taxonomy):
    paper = {m.id for m in taxonomy if m.paper_records}
    assert paper == {f"CS-DI-00{i}" for i in range(1, 7)} | {"CS-EDC-004"}
    assert taxonomy.version == "1.1"


def test_rules_flag_paper_only_without_electronic_cue(session, taxonomy):
    paper, hybrid = citation(session, PAPER, 1), citation(session, HYBRID, 2)
    rules_tag_citation(session, paper, taxonomy)
    rules_tag_citation(session, hybrid, taxonomy)
    assert flags(session, paper.id) == {("CS-DI-001", "rules"): True}
    assert flags(session, hybrid.id) == {("CS-DI-001", "rules"): False}


def test_prompt_carries_the_preview_fixes(taxonomy):
    assert "analytical method validation are not computerized-system validation" in SYSTEM_PROMPT
    assert "211.68 citation alone is not evidence of a computerized system" in SYSTEM_PROMPT
    assert "Set paper_only to false only when the citation text states that an electronic or hybrid record" in SYSTEM_PROMPT
    assert "findings that do not say what kind of record is involved are all paper_only" in SYSTEM_PROMPT
    block = taxonomy_block(taxonomy)
    di = block.split("CS-DI-001 |")[1].split("\nCS-")[0]
    assert "Paper records: yes" in di and "Paper records: yes" not in block.split("CS-AUD-002 |")[1].split("\nCS-")[0]
    item = output_schema(taxonomy)["properties"]["tags"]["items"]
    assert "paper_only" in item["required"] and item["properties"]["paper_only"] == {"type": "boolean"}


def test_request_tail_is_trimmed():
    body = "Your firm failed to review audit trails. In your response, you stated that you retrained staff."
    tail = " In response to this letter, provide:\n- A comprehensive assessment of your computer systems."
    assert strip_request_tail(body + tail) == body
    assert strip_request_tail(body) == body  # firm-response summaries can carry findings; kept
    assert strip_request_tail("Your response should include a plan.") == "Your response should include a plan."
    assert strip_request_tail(body + "\nYour response should include a risk assessment.") == body


def test_llm_paper_only_kept_for_di_and_forced_off_elsewhere(session, taxonomy, fake_client_factory, fake_response):
    text = PAPER + " Shared logins were used on the balance. In response to this letter, provide: an audit trail review plan."
    cit = citation(session, text)
    sent = []

    def responder(kw):
        sent.append(kw["messages"][0]["content"])
        return fake_response(json.dumps({"tags": [
            {"mode_id": "CS-DI-001", "evidence": "recorded on scrap paper", "confidence": 0.8, "paper_only": True},
            {"mode_id": "CS-ACC-001", "evidence": "Shared logins", "confidence": 0.9, "paper_only": True},
            {"mode_id": "CS-AUD-002", "evidence": "audit trail review plan", "confidence": 0.5, "paper_only": False},
        ]}))

    tagger = LlmTagger(taxonomy, client=fake_client_factory(responder), model="claude-sonnet-5-5")
    llm_tag_citation(session, cit, tagger)
    assert "In response to this letter" not in sent[0] and "Shared logins" in sent[0]
    # The request-list evidence no longer occurs in what the model saw, so AUD-002 is dropped.
    assert flags(session, cit.id) == {("CS-DI-001", "llm"): True, ("CS-ACC-001", "llm"): False}


def test_paper_only_is_reviewable_searchable_and_never_counted(db_url, taxonomy, tmp_path):
    from auditcore.web.app import create_app

    engine = create_engine(db_url)
    with Session(engine) as s:
        paper, hybrid = citation(s, PAPER, 1), citation(s, HYBRID, 2)
        rules_tag_citation(s, paper, taxonomy)
        rules_tag_citation(s, hybrid, taxonomy)
        item = next(i for i in review_queue(s) if i.citation_id == paper.id)
        assert item.paper_only
        decide(s, paper.id, "CS-DI-001", accept=True, reviewer="sk")
        decide(s, hybrid.id, "CS-DI-001", accept=True, reviewer="sk")
        assert flags(s, paper.id)[("CS-DI-001", "human")] is True
        assert flags(s, hybrid.id)[("CS-DI-001", "human")] is False

        compute_annual_stats(s, taxonomy.version)
        assert mode_trend(s, "CS-DI-001", taxonomy.version) == {2026: {"483": 0, "wl": 1}}  # hybrid only

        decide(s, hybrid.id, "CS-DI-001", accept=True, reviewer="sk", paper_only=True)  # reviewer override
        compute_annual_stats(s, taxonomy.version)
        assert mode_trend(s, "CS-DI-001", taxonomy.version) == {}
        s.commit()

    content = tmp_path / "content"
    (content / "remediation").mkdir(parents=True)
    (content / "digest").mkdir()
    client = TestClient(create_app(database_url=db_url, content_dir=content, taxonomy=taxonomy))
    page = client.get("/search", params={"paper": "1"})
    assert "2 citations" in page.text and "Paper only: Original records not preserved" in page.text


def test_electronic_cue_in_request_tail_does_not_count(session, taxonomy):
    cit = citation(session, PAPER + " In response to this letter, provide: an assessment of your electronic records.", 3)
    rules_tag_citation(session, cit, taxonomy)
    assert flags(session, cit.id) == {("CS-DI-001", "rules"): True}
