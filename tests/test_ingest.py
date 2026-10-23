import io
import json
from datetime import date

from openpyxl import Workbook
from sqlalchemy import func, select

from auditcore.config import Settings
from auditcore.ingest.dashboard import ingest_dashboard
from auditcore.ingest.form483 import ingest_pdf_files, parse_reading_room
from auditcore.ingest.store import ingestion_run, store_document
from auditcore.ingest.warning_letters import cms_from_url, ingest_index, letter_text_from_html, parse_index_xlsx
from auditcore.models import ComplianceAction, Document, Facility, Form483, IngestionRun, Inspection, WarningLetter

WL_URL = ("https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/"
          "warning-letters/example-pharma-inc-699001-03042025")


class TestStore:
    def test_idempotent_and_versioned(self, session, archive):
        with ingestion_run(session, "test") as run:
            a = store_document(session, archive, source="warning_letter", source_key="wl:1", data=b"v1",
                               media_type="text/html", run=run)
            b = store_document(session, archive, source="warning_letter", source_key="wl:1", data=b"v1",
                               media_type="text/html", run=run)
            c = store_document(session, archive, source="warning_letter", source_key="wl:1", data=b"v2",
                               media_type="text/html", run=run)
        assert (a.outcome, b.outcome, c.outcome) == ("new", "skipped", "versioned")
        assert b.document.id == a.document.id
        assert c.document.supersedes_id == a.document.id and c.document.version == 2
        assert not a.document.is_current and c.document.is_current
        # raw archive keeps both versions, never overwrites
        assert archive.read(a.document.raw_path) == b"v1"
        assert archive.read(c.document.raw_path) == b"v2"
        assert run.status == "ok" and run.finished_at is not None

    def test_same_content_different_keys(self, session, archive):
        store_document(session, archive, source="form_483", source_key="483:a", data=b"same", media_type="application/pdf")
        r = store_document(session, archive, source="form_483", source_key="483:b", data=b"same", media_type="application/pdf")
        assert r.outcome == "new"


def _index_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Posted Date", "Letter Issue Date", "Company Name", "Issuing Office", "Subject"])
    ws.append(["03/11/2025", "03/04/2025", "Example Pharma Inc.", "CDER", "CGMP/Finished Pharmaceuticals/Adulterated"])
    ws.cell(row=2, column=3).hyperlink = WL_URL
    ws.append(["03/12/2025", "03/05/2025", "No Link Corp.", "CDER", "CGMP"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeHttpResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode()

    def json(self):
        return json.loads(self.content)


class FakeFetcher:
    def __init__(self, pages, settings=None):
        self.pages = pages
        self.settings = settings or Settings(database_url="sqlite://", request_delay_s=0)
        self.requests = []

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        page = self.pages[url]
        return FakeHttpResponse(page(kw) if callable(page) else page)


class TestWarningLetters:
    def test_cms_from_url(self):
        assert cms_from_url(WL_URL) == "699001"
        assert cms_from_url("https://www.fda.gov/foo") is None
        assert cms_from_url("/warning-letters/kellin-chemicals-co-ltd-716228-zhangjiagang") == "716228"
        assert cms_from_url("/warning-letters/excelvision-726714-fareva") == "726714"

    def test_parse_index_uses_hyperlink(self):
        rows = parse_index_xlsx(_index_xlsx())
        assert rows[0].cms_number == "699001" and rows[0].issue_date == date(2025, 3, 4)
        assert rows[1].cms_number is None

    def test_letter_text(self, wl_html):
        text = letter_text_from_html(wl_html.decode())
        assert "Home / Inspections" not in text  # nav stripped
        assert "1. Your firm failed to exercise appropriate controls" in text

    def test_ingest_index_end_to_end(self, session, archive, wl_html):
        fetcher = FakeFetcher({WL_URL: wl_html})
        out = ingest_index(session, archive, fetcher, _index_xlsx())
        assert out == {"rows": 2, "matched": 1, "fetched": 1, "errors": 1}  # the row without CMS is recorded as an error
        wl = session.scalars(select(WarningLetter)).one()
        assert wl.document is not None and wl.document.text_clean
        assert wl.facility.fei == "3009990001"
        run = session.scalars(select(IngestionRun)).one()
        assert run.status == "partial" and run.n_new == 2

        # Re-running changes nothing: idempotent.
        ingest_index(session, archive, fetcher, _index_xlsx())
        assert session.scalar(select(func.count()).select_from(Document)) == 2


class TestForm483:
    def test_parse_reading_room(self):
        html = """<table><tr><th>Record Date</th><th>FEI Number</th><th>Company Name</th><th>Record Type</th>
        <th>State</th><th>Establishment Type</th></tr>
        <tr><td>01/15/2025</td><td>3009990002</td><td><a href="/media/1/download">Sample Labs</a></td><td>483</td>
        <td>NJ</td><td>Drug Manufacturer</td></tr>
        <tr><td>01/16/2025</td><td>1</td><td><a href="/media/2/download">X</a></td><td>EIR</td><td></td><td></td></tr>
        </table>"""
        entries = parse_reading_room(html, "https://www.fda.gov/reading-room")
        assert len(entries) == 1
        e = entries[0]
        assert e.pdf_url == "https://www.fda.gov/media/1/download" and e.fei == "3009990002"
        assert e.record_date == date(2025, 1, 15)

    def test_ingest_pdf_with_manifest(self, session, archive, tmp_path, form483_pdf):
        pdf = tmp_path / "sample.pdf"
        pdf.write_bytes(form483_pdf)
        manifest = tmp_path / "m.csv"
        manifest.write_text("file,fei,company,record_date,url\nsample.pdf,3009990002,Sample Labs,01/15/2025,https://x/483.pdf\n")
        out = ingest_pdf_files(session, archive, [pdf], manifest)
        assert out["errors"] == 0
        form = session.scalars(select(Form483)).one()
        assert form.issue_date == date(2025, 1, 15) and form.facility.fei == "3009990002"
        assert "OBSERVATION 1" in form.document.text_clean
        assert "computerized system was not validated" in form.document.text_clean
        assert "PREVIOUS EDITION OBSOLETE" not in form.document.text_clean


class TestDashboard:
    def test_ingest(self, session, archive):
        settings = Settings(database_url="sqlite://", request_delay_s=0, dashboard_user="u", dashboard_key="k",
                            dashboard_base_url="https://api.test/v1")

        def inspections(kw):
            assert kw["headers"]["Authorization-Key"] == "k"
            body = json.loads(kw["data"])
            assert body["columns"] == [] and body["start"] == 1 and body["returntotalcount"] is True
            return json.dumps({"statuscode": 400, "message": "Success.", "totalrecordcount": 1, "resultcount": 1, "result": [
                {"InspectionID": 555, "FEINumber": "3009990001", "LegalName": "Example Pharma Inc.",
                 "InspectionEndDate": "2024-10-10", "Classification": "Official Action Indicated (OAI)",
                 "ClassificationCode": "OAI", "ProductType": "Drugs", "CountryName": "United States"}]}).encode()

        def actions(kw):
            return json.dumps({"totalrecordcount": 1, "resultcount": 1, "result": [
                {"CaseInjunctionID": 9, "FEINumber": "3009990001", "ActionType": "Warning Letter",
                 "ActionTakenDate": "2025-03-04"}]}).encode()

        fetcher = FakeFetcher({"https://api.test/v1/inspections_classifications": inspections,
                               "https://api.test/v1/compliance_actions": actions}, settings)
        assert ingest_dashboard(session, archive, fetcher) == {"inspections": 1, "compliance_actions": 1}
        insp = session.scalars(select(Inspection)).one()
        assert insp.classification == "OAI" and insp.end_date == date(2024, 10, 10)
        assert session.scalars(select(ComplianceAction)).one().facility_id == insp.facility_id
        assert session.scalar(select(func.count()).select_from(Facility)) == 1


def test_dashboard_error_is_raised_not_read_as_empty():
    import pytest
    from auditcore.ingest.dashboard import DashboardClient, DashboardError

    settings = Settings(database_url="sqlite://", request_delay_s=0, dashboard_user="u", dashboard_key="k",
                        dashboard_base_url="https://api.test/v1")
    err = json.dumps([{"statuscode": 410, "message": "Required parameter(s) missing from request body.",
                       "missing_parameters": ["columns"]}]).encode()
    client = DashboardClient(FakeFetcher({"https://api.test/v1/compliance_actions": err}, settings))
    with pytest.raises(DashboardError, match="missing"):
        list(client.pages("compliance_actions", {}))


def test_dashboard_pages_until_total():
    from auditcore.ingest import dashboard as dash

    settings = Settings(database_url="sqlite://", request_delay_s=0, dashboard_user="u", dashboard_key="k",
                        dashboard_base_url="https://api.test/v1")
    starts = []

    def page(kw):
        body = json.loads(kw["data"])
        starts.append(body["start"])
        n = min(2, 5 - body["start"] + 1)
        return json.dumps({"totalrecordcount": 5, "resultcount": n,
                           "result": [{"InspectionID": body["start"] + i} for i in range(n)]}).encode()

    old = dash.PAGE_SIZE
    dash.PAGE_SIZE = 2
    try:
        client = dash.DashboardClient(FakeFetcher({"https://api.test/v1/x": page}, settings))
        rows = [r for _, _, page_rows in client.pages("x", {}) for r in page_rows]
    finally:
        dash.PAGE_SIZE = old
    assert starts == [1, 3, 5] and len(rows) == 5
