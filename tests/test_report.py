"""Static report: year breakdown, sample citations, scope and counting."""

from collections import Counter
from datetime import date

from auditcore.ingest.scope import in_scope, letter_in_scope
from auditcore.models import Citation, Document, FailureMode, Observation, WarningLetter
from auditcore.reports.static import _excerpt, build_report
from auditcore.tagging.review import decide

from test_web_and_content import report_data


def letter(s, taxonomy, n, year, text, tags, office="Center for Drug Evaluation and Research (CDER)", cfrs=1):
    wl = WarningLetter(cms_number=f"7{n:05d}", company_name=f"Firm {n}", issue_date=date(year, 6, 1),
                       issuing_office=office, subject="CGMP/Finished Pharmaceuticals/Adulterated",
                       letter_url=f"https://example.test/{n}")
    doc = Document(source="warning_letter", source_key=f"wl:7{n:05d}", content_hash=f"y{n}", raw_path="x",
                   media_type="text/html", source_url=f"https://example.test/{n}")
    s.add_all([wl, doc])
    s.flush()
    wl.document_id = doc.id
    obs = Observation(document_id=doc.id, kind="wl", number=1, text=text, observed_date=date(year, 6, 1),
                      warning_letter_id=wl.id)
    s.add(obs)
    s.flush()
    for i in range(cfrs):
        cit = Citation(observation_id=obs.id, ordinal=i, text=text)
        s.add(cit)
        s.flush()
        for mode, paper in tags:
            s.add(FailureMode(citation_id=cit.id, mode_id=mode, taxonomy_version=taxonomy.version, provenance="llm",
                              confidence=0.9, evidence=text.split(".")[0], paper_only=paper))
    s.flush()
    return obs


def test_year_breakdown_counts_each_violation_once(session, taxonomy, tmp_path):
    aud = "Audit trails on the HPLC were not reviewed. Other text."
    letter(session, taxonomy, 1, 2022, aud, [("CS-AUD-002", False)], cfrs=2)   # two citations, one violation
    letter(session, taxonomy, 2, 2024, aud, [("CS-AUD-002", False)])
    letter(session, taxonomy, 3, 2024, aud, [("CS-AUD-002", False), ("CS-ACC-001", False)])
    letter(session, taxonomy, 4, 2024, "Weights were written on scrap paper. More.", [("CS-DI-001", True)])
    letter(session, taxonomy, 5, 2024, aud, [("CS-AUD-002", False)], office="Human Foods Program")
    letter(session, taxonomy, 6, 2023, "Nothing about computers.", [])

    data = report_data(build_report(session, taxonomy, tmp_path, samples=1)["html"].read_text())
    assert data["years"] == [2022, 2023, 2024]
    assert data["docs"] == {"2022": [1, 0], "2023": [1, 0], "2024": [3, 0]}  # food letter excluded
    by_year = Counter((f[0], f[1]) for f in data["f"])
    assert by_year == {("CS-AUD-002", 2022): 1, ("CS-AUD-002", 2024): 2, ("CS-ACC-001", 2024): 1}
    assert data["paper"] == {"CS-DI-001": {"2024": 1}}  # listed, not counted
    assert data["coverage"]["2024"] == [[3, 3], [0, 0]]  # every test citation carries an LLM row
    assert data["coverage"]["2023"] == [[0, 1], [0, 0]]  # the untagged letter
    assert not data["accepted"] and data["excluded"] == 1
    # samples: at most `samples` per mode and year, newest first, with facility and link
    aud_samples = data["samples"]["CS-AUD-002"]
    assert [s["y"] for s in aud_samples] == [2024, 2022]
    assert aud_samples[0]["fac"].startswith("Firm ") and aud_samples[0]["url"].startswith("https://example.test/")
    assert aud_samples[0]["h"] == [[0, len("Audit trails on the HPLC were not reviewed")]]


def test_accepted_tags_replace_proposals_once_any_exist(session, taxonomy, tmp_path):
    aud = "Audit trails on the HPLC were not reviewed."
    o1 = letter(session, taxonomy, 1, 2022, aud, [("CS-AUD-002", False)])
    letter(session, taxonomy, 2, 2023, aud, [("CS-AUD-002", False)])
    decide(session, o1.citations[0].id, "CS-AUD-002", accept=True, reviewer="sk")
    data = report_data(build_report(session, taxonomy, tmp_path)["html"].read_text())
    assert data["accepted"] and [(f[0], f[1]) for f in data["f"]] == [("CS-AUD-002", 2022)]


def test_excerpt_centres_on_late_evidence():
    text = "Intro. " * 200 + "The audit trail was disabled on the GC." + " Tail." * 200
    excerpt, cut_start, cut_end, marks = _excerpt(text, "audit trail was disabled")
    assert cut_start and cut_end and len(excerpt) <= 900
    (s, e), = marks
    assert excerpt[s:e] == "audit trail was disabled"


def test_scope_covers_pre_reorganization_field_divisions():
    assert letter_in_scope("Division of Pharmaceutical Quality Operations III", "CGMP/Finished Pharmaceuticals/Adulterated")
    assert letter_in_scope("Office of Medical Device and Radiological Health Division II", "CGMP/QSR/Medical Devices")
    assert letter_in_scope("Office of Biological Products Operations Division 1", "CGMP/Deviations")
    assert letter_in_scope("Center for Biologics Evaluation and Research (CBER)", None)
    assert not letter_in_scope("Human Foods Program", "CGMP/Food/Adulterated")
    assert not letter_in_scope("Center for Veterinary Medicine", "CGMP/Finished Pharmaceuticals")
    assert not letter_in_scope("Division of Human and Animal Food Operations West V", "CGMP/Food")
    # Offices covering several product areas fall back to the subject line.
    assert letter_in_scope("Office of Inspections and Investigations", "CGMP/Active Pharmaceutical Ingredient (API)/Adulterated")
    assert not letter_in_scope("Office of Inspections and Investigations", "Seafood HACCP/CGMP for Foods/Adulterated")
    assert not letter_in_scope("Office of Inspections and Investigations", "CGMP/Dietary Supplement/Adulterated")
    assert in_scope("483", None, None, None)
    assert in_scope("wl", None, None, "211") and not in_scope("wl", None, None, "117")


def test_483_record_types():
    from auditcore.ingest.form483 import is_483_record

    assert is_483_record("483") and is_483_record("") and is_483_record(None)
    assert not is_483_record("483 Response") and not is_483_record("State Referral Letter")
    assert not is_483_record("FMD-145 Letter")


def test_483_response_documents_are_not_segmented(session):
    from auditcore.models import Document
    from auditcore.tagging.pipeline import segment_document

    text = "OBSERVATION 1\\nAudit trails were not reviewed.\\nResponse: we have retrained staff."
    resp = Document(source="form_483", source_key="483:r", content_hash="r", raw_path="x", media_type="application/pdf",
                    text_raw=text, meta={"record_type": "483 Response"})
    session.add(resp)
    session.flush()
    assert segment_document(session, resp) == []


def test_report_since_year_drops_earlier_years(session, taxonomy, tmp_path):
    aud = "Audit trails on the HPLC were not reviewed."
    letter(session, taxonomy, 1, 2019, aud, [("CS-AUD-002", False)])
    letter(session, taxonomy, 2, 2022, aud, [("CS-AUD-002", False)])
    out = build_report(session, taxonomy, tmp_path, since_year=2021)
    data = report_data(out["html"].read_text())
    assert data["years"] == [2022] and [f[1] for f in data["f"]] == [2022]
    assert len(out["csv"].read_text().splitlines()) == 3  # header plus both citations


def test_repeated_483_observation_numbers_merge(session):
    from auditcore.models import Document
    from auditcore.tagging.pipeline import segment_document

    text = ("OBSERVATION 1\nAudit trails were not reviewed.\nOBSERVATION 2\nBackups were not tested.\n"
            "AMENDMENT 1\nOBSERVATION 1\nThe audit trail review SOP was not followed.")
    doc = Document(source="form_483", source_key="483:amended", content_hash="am", raw_path="x",
                   media_type="application/pdf", text_raw=text, meta={"record_type": "Amended 483"})
    session.add(doc)
    session.flush()
    obs = sorted(segment_document(session, doc), key=lambda o: o.number)
    assert [o.number for o in obs] == [1, 2]
    assert "Audit trails were not reviewed" in obs[0].text and "SOP was not followed" in obs[0].text


def test_483_inline_headings_split_in_sequence():
    from auditcore.text.segment import split_483

    text = ("DURING AN INSPECTION OF YOUR FIRM (I) (WE) OBSERVED: OBSERVATION #1 Equipment is not cleaned. "
            "As detailed in Observation 1 above, the SOP is old. Specifically, see Observation 3 for lots. "
            "Observation 2: Procedures are not followed. Add Continuation Page Observation 3 Audit trails "
            "are not reviewed.")
    segs = split_483(text)
    assert [s.number for s in segs] == [1, 2, 3]
    assert segs[0].text.startswith("Equipment is not cleaned") and "detailed in Observation 1" in segs[0].text
    assert segs[2].text == "Audit trails are not reviewed."


def test_unnumbered_warning_letter_is_one_violation():
    from auditcore.text.segment import split_warning_letter

    text = ("Dear Ms. Example:\n\nThe FDA inspected your facility. Significant violations include, but are not "
            "limited to, the following:\n\nFailure to submit a report to FDA no later than 30 calendar days after "
            "becoming aware of a malfunction, as required by 21 CFR 803.50(a)(2). For example, the information "
            "for incident 1 reasonably suggests the ventilator malfunctioned.\n\nPlease notify this office in "
            "writing within fifteen business days.\n\nSincerely,\nDirector")
    segs = split_warning_letter(text)
    assert [s.number for s in segs] == [1]
    assert segs[0].text.startswith("Significant violations include") and "Please notify" not in segs[0].text
    numbered = "Dear X:\n\nWe observed significant violations, including:\n\n1. Your firm failed to review audit trails.\n\n2. Your firm failed to back up data."
    assert [s.number for s in split_warning_letter(numbered)] == [1, 2]
