"""Regressions from real FDA documents (public domain, trimmed). Each test
pins a failure found when the pipeline first ran against fda.gov."""

import io
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from auditcore.config import Settings
from auditcore.ingest.form483 import fei_from_483, ingest_reading_room, pdf_text
from auditcore.ingest.warning_letters import (
    LetterNotAvailable,
    ingest_letter_html,
    ingest_listing,
    is_not_found_page,
    letter_text_from_html,
)
from auditcore.models import Document, Form483, IngestionRun, WarningLetter
from auditcore.tagging.rules import tag_text
from auditcore.text.cfr import extract_cfr
from auditcore.text.ocr_cleanup import clean_text
from auditcore.text.segment import split_483, split_citations, split_warning_letter

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.fda.gov"


def letter(name):
    return clean_text(letter_text_from_html((FIX / name).read_text(encoding="utf-8")))


class TestRealLetters:
    def test_api_letter_uses_deviations_and_still_segments(self):
        # ICH Q7 letters say "significant deviations"; the old start hint found nothing.
        assert [s.number for s in split_warning_letter(letter("wl_real_api_deviations.html"))] == [1, 2, 3, 4]

    def test_cited_sections_come_from_heading_only(self):
        segs = split_warning_letter(letter("wl_real_multi_cfr.html"))
        cfrs = [c.cfr.canonical for c in split_citations(segs[0].text)]
        # "(21 CFR 211.22(a) and (d))": both paragraphs, and nothing from the discussion below it.
        assert cfrs == ["21 CFR 211.22(a)", "21 CFR 211.22(d)"]

    def test_cds_letter_rules(self, taxonomy):
        segs = split_warning_letter(letter("wl_real_curia_cds.html"))
        modes = {h.mode_id for s in segs for c in split_citations(s.text) for h in tag_text(c.text, taxonomy)}
        assert {"CS-LAB-001", "CS-ACC-004"} <= modes

    def test_plain_test_samples_is_not_trial_injection(self, taxonomy):
        hits = tag_text("Your firm failed to test samples of each lot for conformity with specifications.", taxonomy)
        assert "CS-LAB-001" not in {h.mode_id for h in hits}

    def test_firm_response_summary_is_not_a_finding(self, taxonomy):
        for s in ("In your response, you commit to establishing audit trail review procedures.",
                  "Additionally in your response, you state that audit trails were not reviewed historically."):
            assert tag_text(s, taxonomy) == []

    def test_not_found_page_is_rejected(self, session, archive):
        html = (FIX / "wl_not_found.html").read_bytes()
        assert is_not_found_page(html)
        assert not is_not_found_page((FIX / "wl_real_curia_cds.html").read_bytes())
        wl = WarningLetter(cms_number="731926")
        session.add(wl)
        session.flush()
        with pytest.raises(LetterNotAvailable):
            ingest_letter_html(session, archive, wl, html, url="x")
        assert session.scalars(select(Document)).first() is None


class TestCfrLists:
    @pytest.mark.parametrize("text,expected", [
        ("(21 CFR 211.22(a) and (d))", ["21 CFR 211.22(a)", "21 CFR 211.22(d)"]),
        ("§ 211.194(a)(4) and (8)", ["21 CFR 211.194(a)(4)", "21 CFR 211.194(a)(8)"]),
        ("21 CFR 211.100(a), 211.192, and 211.68(b)", ["21 CFR 211.100(a)", "21 CFR 211.192", "21 CFR 211.68(b)"]),
    ])
    def test_lists(self, text, expected):
        assert [r.canonical for r in extract_cfr(text)] == expected


class FakeResp:
    def __init__(self, content):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


class Fetcher:
    def __init__(self, routes):
        self.routes = routes
        self.settings = Settings(database_url="sqlite://", request_delay_s=0)
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return FakeResp(body(url, params) if callable(body) else body)
        raise KeyError(url)


class TestListings:
    def test_warning_letter_listing(self, session, archive):
        letter_html = (FIX / "wl_real_curia_cds.html").read_bytes()
        f = Fetcher({
            f"{BASE}/datatables/views/ajax": (FIX / "wl_listing_ajax.json").read_bytes(),
            f"{BASE}/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities":
                (FIX / "wl_listing_page.html").read_bytes(),
            f"{BASE}/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/": letter_html,
        })
        out = ingest_listing(session, archive, f, subject="CGMP", limit=2)
        assert out["rows"] == 5 and out["matched"] == 4 and out["fetched"] == 2  # one row is a food letter
        params = next(p for u, p in f.calls if u.endswith("/datatables/views/ajax"))
        assert params["view_name"] == "warning_letter_solr_index" and params["start"] == 0
        wls = session.scalars(select(WarningLetter).order_by(WarningLetter.cms_number)).all()
        assert {w.cms_number for w in wls} >= {"733215", "733231"}
        baxter = next(w for w in wls if w.cms_number == "733231")
        assert baxter.issue_date == date(2026, 9, 29) and baxter.company_name == "Baxter International Inc."

        # incremental: letters already stored are not fetched again
        n = len(f.calls)
        again = ingest_listing(session, archive, f, subject="CGMP", limit=2)
        letter_calls = [u for u, _ in f.calls[n:] if "/warning-letters/" in u]
        assert again["fetched"] == 2 and len(letter_calls) == 2  # the two not fetched last time

    def test_reading_room_listing(self, session, archive, form483_pdf):
        f = Fetcher({
            f"{BASE}/datatables/views/ajax": (FIX / "rr_listing_ajax.json").read_bytes(),
            f"{BASE}/about-fda/": (FIX / "rr_listing_page.html").read_bytes(),
            f"{BASE}/media/": form483_pdf,
        })
        out = ingest_reading_room(session, archive, f)
        assert out == {"entries": 4, "downloaded": 4, "errors": 0}  # the EIR row is skipped
        forms = session.scalars(select(Form483)).all()
        assert {x.facility.fei for x in forms} >= {"3010485868"}
        assert ingest_reading_room(session, archive, f)["downloaded"] == 0  # already archived
        assert session.scalars(select(IngestionRun).order_by(IngestionRun.id.desc())).first().status == "ok"


def _483_with_real_layout() -> bytes:
    """Two pages laid out like a real Form 483: header box ending in a rule
    under TYPE ESTABLISHMENT INSPECTED, signature footer above a rule, and
    observation 2 crossing the page break."""
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    body = [
        ["OBSERVATION 1", "Audit trails on the HPLC were not reviewed.", "OBSERVATION 2",
         "Analysts shared a single login to the"],
        ["chromatography data system.", "OBSERVATION 3", "Backup restore has never been tested."],
    ]
    for n, lines in enumerate(body, 1):
        y = 760
        for h in ["DEPARTMENT OF HEALTH AND HUMAN SERVICES", "Dallas, TX 75202 FEJNUMBER", "301 0485868",
                  "CITY, STATE, ZIP CODE, COUNTRY TYPE ESTABLISHMENT INSPECTED", "Austin, TX Producer of Sterile Drugs"]:
            c.drawString(40, y, h)
            y -= 14
        c.line(30, 600, 580, 600)
        y = 580
        for line in lines:
            c.drawString(40, y, line)
            y -= 16
        c.line(30, 140, 580, 140)
        c.drawString(40, 130, "EMPLOYEE(S) SIGNATURE DATE ISSUED")
        c.drawString(40, 116, "SEE REVERSE Jane Q Investigator 9/10/2026")
        c.drawString(40, 100, f"FORM FDA 483 (09/08) PREVIOUS EDITION OBSOLETE INSPECTIONAL OBSERVATIONS PAGE {n} of 2 PAGES")
        c.showPage()
    c.save()
    return buf.getvalue()


class Test483Layout:
    def test_page_furniture_is_cropped_and_fei_read(self, taxonomy):
        raw, meta = pdf_text(_483_with_real_layout())
        assert meta["fei"] == "3010485868"
        segs = split_483(clean_text(raw))
        assert [s.number for s in segs] == [1, 2, 3]
        assert segs[1].text == "Analysts shared a single login to the chromatography data system."
        assert not any(k in s.text for s in segs for k in ("SEE REVERSE", "DEPARTMENT", "EMPLOYEE", "FORM FDA"))
        assert [h.mode_id for h in tag_text(segs[1].text, taxonomy)] == ["CS-ACC-001"]

    def test_fei_ocr_variants(self):
        assert fei_from_483("FEJNUMBER\n3010589333") == "3010589333"
        assert fei_from_483("FEI NUMBER 301 0485868") == "3010485868"

    def test_ocr_word_fixes(self):
        assert clean_text("your fnm perfo1med tests on diug products ofyour site") == \
            "your firm performed tests on drug products of your site"
