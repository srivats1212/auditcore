"""Form 483s from the OII FOIA electronic reading room (PRD section 4, source 3).

FDA keeps reading-room PDFs for a limited period, so every PDF is archived the
moment it is seen. Two entry points:
  - ``ingest_reading_room``: parse the listing page, download each 483 PDF;
  - ``ingest_pdf_files``: ingest PDFs already saved locally, with an optional
    CSV manifest (columns: file, fei, company, record_date, url).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Form483
from ..text.ocr_cleanup import clean_text
from .common import Fetcher, find_fei, get_or_create_facility, parse_date, pick
from .store import RawArchive, ingestion_run, record_error, store_document, tally

log = logging.getLogger(__name__)

READING_ROOM_PATH = "/about-fda/office-inspections-and-investigations/oii-foia-electronic-reading-room"


@dataclass
class ReadingRoomEntry:
    pdf_url: str
    fei: str | None
    company_name: str | None
    record_date: date | None
    record_type: str | None
    establishment_type: str | None
    state: str | None


def parse_reading_room(html: str, base_url: str) -> list[ReadingRoomEntry]:
    soup = BeautifulSoup(html, "lxml")
    entries: list[ReadingRoomEntry] = []
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = [th.get_text(" ", strip=True) for th in header_row.find_all(["th", "td"])]
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if not cells:
                continue
            row = {h: c.get_text(" ", strip=True) for h, c in zip(headers, cells)}
            link = tr.find("a", href=True)
            if link is None:
                continue
            record_type = pick(row, "Record Type", "Type")
            if record_type and "483" not in record_type:
                continue
            entries.append(
                ReadingRoomEntry(
                    pdf_url=urljoin(base_url, link["href"]),
                    fei=pick(row, "FEI Number", "FEI"),
                    company_name=pick(row, "Company Name", "Firm Name", "Company") or link.get_text(strip=True),
                    record_date=parse_date(pick(row, "Record Date", "Date")),
                    record_type=record_type,
                    establishment_type=pick(row, "Establishment Type"),
                    state=pick(row, "State"),
                )
            )
    return entries


def pdf_text(data: bytes) -> tuple[str, dict]:
    """Extract text page by page. Image-only pages are flagged for OCR rather
    than silently producing an empty document."""
    pages, empty = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if len(text.strip()) < 20:
                empty.append(i + 1)
            pages.append(text)
        n_pages = len(pdf.pages)
    meta = {"pages": n_pages}
    if empty:
        meta["image_only_pages"] = empty
        meta["needs_ocr"] = len(empty) == n_pages
    return "\n\n".join(pages), meta


def _store_483(session: Session, archive: RawArchive, run, data: bytes, *, key: str, url: str,
               entry: ReadingRoomEntry | None) -> None:
    result = store_document(session, archive, source="form_483", source_key=key, data=data,
                            media_type="application/pdf", source_url=url, run=run)
    tally(run, result.outcome)
    doc = result.document
    if doc.text_raw is None:
        try:
            doc.text_raw, meta = pdf_text(data)
        except Exception as exc:
            record_error(run, key, f"pdf extraction failed: {exc}")
            doc.text_raw, meta = "", {"extraction_error": str(exc)}
        doc.meta = {**(doc.meta or {}), **meta}
        doc.text_clean = clean_text(doc.text_raw)

    fei = (entry.fei if entry else None) or find_fei(doc.text_clean)
    fac = get_or_create_facility(
        session, fei=fei, name=entry.company_name if entry else None,
        establishment_type=entry.establishment_type if entry else None,
        state=entry.state if entry else None,
    )
    form = session.scalars(select(Form483).where(Form483.document_id == doc.id)).first()
    if form is None:
        form = Form483(document_id=doc.id, pdf_url=url)
        session.add(form)
    form.facility_id = fac.id if fac else form.facility_id
    form.company_name = form.company_name or (entry.company_name if entry else None)
    form.issue_date = form.issue_date or (entry.record_date if entry else None)
    session.flush()


def ingest_reading_room(session: Session, archive: RawArchive, fetcher: Fetcher, *,
                        listing_url: str | None = None, limit: int | None = None) -> dict:
    listing_url = listing_url or urljoin(fetcher.settings.fda_base_url, READING_ROOM_PATH)
    with ingestion_run(session, "form_483") as run:
        page = fetcher.get(listing_url)
        listing = store_document(session, archive, source="form_483_listing", source_key="form_483_listing",
                                 data=page.content, media_type="text/html", source_url=listing_url, run=run)
        tally(run, listing.outcome)
        entries = parse_reading_room(page.text, listing_url)
        for entry in entries[:limit] if limit else entries:
            try:
                data = fetcher.get(entry.pdf_url).content
                _store_483(session, archive, run, data, key=f"483:{entry.pdf_url}", url=entry.pdf_url, entry=entry)
            except Exception as exc:
                record_error(run, entry.pdf_url, exc)
        return {"entries": len(entries), "errors": run.n_errors}


def ingest_pdf_files(session: Session, archive: RawArchive, paths: list[Path], manifest: Path | None = None) -> dict:
    by_file: dict[str, dict] = {}
    if manifest is not None:
        with manifest.open(newline="") as fh:
            for row in csv.DictReader(fh):
                by_file[Path(row["file"]).name] = row
    with ingestion_run(session, "form_483_files") as run:
        for path in paths:
            m = by_file.get(path.name, {})
            entry = ReadingRoomEntry(
                pdf_url=m.get("url") or path.name, fei=m.get("fei") or None, company_name=m.get("company") or None,
                record_date=parse_date(m.get("record_date")), record_type="483",
                establishment_type=m.get("establishment_type") or None, state=m.get("state") or None,
            )
            try:
                key = f"483:{m['url']}" if m.get("url") else f"483:file:{path.name}"
                _store_483(session, archive, run, path.read_bytes(), key=key, url=entry.pdf_url, entry=entry)
            except Exception as exc:
                record_error(run, str(path), exc)
        return {"files": len(paths), "errors": run.n_errors}
