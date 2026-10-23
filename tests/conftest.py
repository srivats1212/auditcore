from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from auditcore.ingest.store import RawArchive
from auditcore.models import Base
from auditcore.taxonomy.loader import load_taxonomy

FIXTURES = Path(__file__).parent / "fixtures"

# Synthetic 483 text with the OCR damage real reading-room PDFs show:
# letter-spaced headings, hyphenated breaks, and page furniture.
FORM_483_LINES = [
    "DEPARTMENT OF HEALTH AND HUMAN SERVICES",
    "SYNTHETIC TEST FIXTURE. Fictional firm. FEI NUMBER 3009990002",
    "O B S E R V A T I O N  1",
    "Audit trails on the dissolution system were disabled at installation and the",
    "comput- erized system was not validated for its intended use.",
    "O B S E R V A T I O N  2",
    "The audit trail was enabled and was adequately reviewed by QA.",
    "Backup of the particle counter data was performed weekly; however, restore",
    "has never been tested.",
    "FORM FDA 483 (09/08) PREVIOUS EDITION OBSOLETE INSPECTIONAL OBSERVATIONS PAGE 1 OF 1",
]


@pytest.fixture
def taxonomy():
    return load_taxonomy()


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'web.sqlite3'}"
    Base.metadata.create_all(create_engine(url))
    return url


@pytest.fixture
def archive(tmp_path):
    return RawArchive(tmp_path / "archive")


@pytest.fixture
def wl_html() -> bytes:
    return (FIXTURES / "wl_example.html").read_bytes()


@pytest.fixture
def form483_pdf() -> bytes:
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 740
    for line in FORM_483_LINES:
        c.drawString(40, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responder(kwargs)


class FakeClient:
    """Stands in for anthropic.Anthropic(); records requests."""

    def __init__(self, responder):
        self.messages = FakeMessages(responder)
        self.beta = type("Beta", (), {"messages": self.messages})()


@pytest.fixture
def fake_client_factory():
    return FakeClient


@pytest.fixture
def fake_response():
    return FakeResponse
