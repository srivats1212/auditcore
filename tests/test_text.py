from datetime import date

from auditcore.text.cfr import extract_cfr, extract_iso13485_clauses, regime_for
from auditcore.text.ocr_cleanup import clean_text, collapse_spaced_letters
from auditcore.text.segment import split_483, split_citations, split_warning_letter


def canon(text):
    return [r.canonical for r in extract_cfr(text)]


class TestCfr:
    def test_basic_forms(self):
        assert canon("(21 CFR 211.68(b))") == ["21 CFR 211.68(b)"]
        assert canon("21 C.F.R. § 211.194(a)(4)") == ["21 CFR 211.194(a)(4)"]
        assert canon("21 CFR 211.68(b) and 211.100(a)") == ["21 CFR 211.68(b)", "21 CFR 211.100(a)"]
        assert canon("§§ 211.188 and 211.192") == ["21 CFR 211.188", "21 CFR 211.192"]
        assert canon("21 CFR Part 11") == ["21 CFR Part 11"]

    def test_deduplicates(self):
        assert canon("21 CFR 211.68(b) ... again 21 CFR 211.68(b)") == ["21 CFR 211.68(b)"]

    def test_no_false_numbers(self):
        assert canon("Lot 211.68 was released on 3.4 days") == []

    def test_qmsr_regime_by_date(self):
        assert regime_for("820", date(2025, 6, 1)) == "QSR"
        assert regime_for("820", date(2026, 2, 2)) == "QMSR"
        assert regime_for("211", date(2026, 6, 1)) == "cGMP"

    def test_iso_13485_clause(self):
        assert extract_iso13485_clauses("ISO 13485:2016, clause 4.1.6 as incorporated by 21 CFR 820.10") == ["4.1.6"]


class TestOcrCleanup:
    def test_spaced_letters(self):
        assert collapse_spaced_letters("O B S E R V A T I O N  1") == "OBSERVATION 1"

    def test_spaced_letters_leaves_normal_text(self):
        assert collapse_spaced_letters("I am a QA lead") == "I am a QA lead"

    def test_full_cleanup(self):
        raw = ("O B S E R V A T I O N  1\nThe comput-\nerized system audit\ntrail was not reviewed per 2l CFR 211.68(b).\n"
               "Page 2 of 5\nFORM FDA 483 (09/08) PREVIOUS EDITION OBSOLETE\nﬁle was deleted")
        out = clean_text(raw)
        assert "computerized system audit trail was not reviewed per 21 CFR 211.68(b)." in out
        assert "Page 2 of 5" not in out and "FORM FDA 483" not in out
        assert "file was deleted" in out


class TestSegment:
    def test_split_483(self):
        text = "Header\nOBSERVATION 1 First finding.\nOBSERVATION 2 Second finding."
        segs = split_483(text)
        assert [(s.number, s.text) for s in segs] == [(1, "First finding."), (2, "Second finding.")]

    def test_split_wl_stops_at_remediation_section(self):
        text = ("Intro. Our investigator observed significant violations, including:\n\n"
                "1. Your firm failed to do A (21 CFR 211.68(b)).\n\nDetails of A.\n\n"
                "2. Your firm failed to do B (21 CFR 211.192).\n\n"
                "Data Integrity Remediation\n\n1. A comprehensive investigation.")
        segs = split_warning_letter(text)
        assert [s.number for s in segs] == [1, 2]
        assert "Details of A." in segs[0].text
        assert "comprehensive investigation" not in segs[1].text

    def test_split_wl_ignores_nested_numbering(self):
        text = ("significant violations:\n1. Failed X.\n1. nested item\n2. Failed Y.\n")
        assert [s.number for s in split_warning_letter(text)] == [1, 2]

    def test_citations_one_per_cfr(self):
        drafts = split_citations("Failure to control systems (21 CFR 211.68(b) and 211.100(a)).")
        assert [d.cfr.canonical for d in drafts] == ["21 CFR 211.68(b)", "21 CFR 211.100(a)"]

    def test_citation_without_cfr(self):
        drafts = split_citations("Audit trails were not reviewed.")
        assert len(drafts) == 1 and drafts[0].cfr is None

    def test_subitems_inherit_lead_cfr(self):
        text = "Controls over computer systems are lacking (21 CFR 211.68(b)).\nA. Shared logins.\nB. No audit trail review."
        drafts = split_citations(text)
        assert len(drafts) == 2
        assert all(d.cfr.canonical == "21 CFR 211.68(b)" for d in drafts)
        assert "Shared logins" in drafts[0].text and "No audit trail review" in drafts[1].text


def test_inline_hyphen_break_and_suspended_hyphen():
    assert clean_text("the comput- erized system") == "the computerized system"
    assert clean_text("pre- and post-filtration") == "pre- and post-filtration"


def test_glued_observation_heading_is_split():
    out = clean_text("Header text O B S E R V A T I O N 1 First finding.")
    assert split_483(out)[0].number == 1


def test_stop_heading_in_introduction_does_not_end_the_list():
    text = ("Intro.\nThis letter notifies you of the violations of the Act.\n"
            "We observed significant violations:\n1. You did not identify controls.\n2. You did not validate them.\n"
            "Conclusion\nEnd.")
    assert [s.number for s in split_warning_letter(text)] == [1, 2]
