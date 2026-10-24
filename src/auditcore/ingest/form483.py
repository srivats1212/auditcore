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
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, Form483
from ..text.ocr_cleanup import clean_text
from .common import Fetcher, find_fei, get_or_create_facility, parse_date, pick
from .fda_tables import as_dicts, cell_href, cell_text, iter_pages
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


_FEI_483 = re.compile(r"FE[IJl1]\s*NUMBER\s*\n?\s*([\d ]{7,14}\d)", re.I)


def fei_from_483(text: str) -> str | None:
    """The 483 header prints "FEI NUMBER" above the number, and OCR often
    reads it as "FEJNUMBER" or splits the digits ("301 0485868")."""
    m = _FEI_483.search(text)
    if m:
        digits = m.group(1).replace(" ", "")
        if 7 <= len(digits) <= 10:
            return digits
    return None


def _body_bbox(page) -> tuple[float, float] | None:
    """Vertical bounds of the observations area on a Form 483 page.

    Every page repeats a header box (district, dates, FEI, firm, establishment
    type) and a footer box (employee signature, "SEE REVERSE", form number).
    Left in, they get spliced into whichever observation crosses the page
    break. The header ends at the first horizontal rule below the "TYPE
    ESTABLISHMENT INSPECTED" label; the footer starts at the last rule above
    "EMPLOYEE(S) SIGNATURE"."""
    words = page.extract_words()
    top_label = next((w for w in words if "INSPECTED" in w["text"].upper() and w["top"] < page.height * 0.45), None)
    foot_label = next((w for w in words if w["text"].upper().startswith(("EMPLOYEE", "SIGNATURE"))
                       and w["top"] > page.height * 0.55), None)
    if top_label is None and foot_label is None:
        return None
    rules = sorted(l["top"] for l in (page.lines or []) if l.get("width", 0) > page.width * 0.5)
    if top_label is not None:
        below = [y for y in rules if y > top_label["bottom"] + 2]
        top = below[0] if below and below[0] < top_label["bottom"] + 60 else top_label["bottom"] + 30
    else:
        top = 0.0
    if foot_label is not None:
        above = [y for y in rules if y < foot_label["top"] - 1]
        bottom = above[-1] if above and above[-1] > foot_label["top"] - 30 else foot_label["top"] - 2
    else:
        bottom = page.height
    return (top, bottom) if bottom - top > 50 else None


def pdf_text(data: bytes) -> tuple[str, dict]:
    """Extract text page by page, cropped to the observations area when the
    page has the Form 483 layout. Image-only pages are flagged for OCR rather
    than silently producing an empty document. The FEI is read from the first
    page header before cropping."""
    pages, empty = [], []
    fei = None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            full = page.extract_text() or ""
            if fei is None:
                fei = fei_from_483(full)
            bounds = _body_bbox(page) if full.strip() else None
            text = page.crop((0, bounds[0], page.width, bounds[1])).extract_text() or "" if bounds else full
            if len(full.strip()) < 20:
                empty.append(i + 1)
            pages.append(text)
        n_pages = len(pdf.pages)
    meta = {"pages": n_pages}
    if fei:
        meta["fei"] = fei
    if empty:
        meta["image_only_pages"] = empty
        # Scanned 483s often carry text only on FDA's boilerplate back page.
        meta["needs_ocr"] = len(empty) >= max(1, n_pages - 1)
    # Single newline: an observation that crosses a page break reads as one paragraph.
    return "\n".join(pages), meta


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
    if entry is not None and entry.record_type and (doc.meta or {}).get("record_type") != entry.record_type:
        doc.meta = {**(doc.meta or {}), "record_type": entry.record_type}

    fei = (entry.fei if entry else None) or (doc.meta or {}).get("fei") or find_fei(doc.text_clean)
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


def is_483_record(record_type: str | None) -> bool:
    """A Form 483 itself. The reading room also lists the firm's "483
    Response", which quotes each observation before answering it and would
    be read as FDA's findings, plus state referral and FMD-145 letters.
    Records with no type are kept; segmentation finds no observations in
    anything that is not a 483."""
    if not record_type:
        return True
    return "483" in record_type and "response" not in record_type.lower()


def entries_from_listing(cols: list[str], cells: list[list[str]], base_url: str) -> list[ReadingRoomEntry]:
    out = []
    for row in as_dicts(cols, cells):
        record_type = cell_text(row.get("Record Type", ""))
        url = cell_href(row.get("Record Type", ""), base_url) or cell_href(row.get("Company Name", ""), base_url)
        if not url or not is_483_record(record_type):
            continue
        out.append(ReadingRoomEntry(
            pdf_url=url, fei=cell_text(row.get("FEI Number", "")) or None,
            company_name=cell_text(row.get("Company Name", "")) or None,
            record_date=parse_date(cell_text(row.get("Record Date", ""))), record_type=record_type,
            establishment_type=cell_text(row.get("Establishment Type", "")) or None,
            state=cell_text(row.get("State", "")) or None,
        ))
    return out


def ingest_reading_room(session: Session, archive: RawArchive, fetcher: Fetcher, *,
                        listing_url: str | None = None, limit: int | None = None,
                        max_rows: int | None = None, since: date | None = None) -> dict:
    """Page through the reading room listing (newest first) and archive every
    483 PDF not already stored. PDFs are keyed by URL; a re-published PDF at
    the same URL with new bytes becomes a new version. ``since`` skips
    records dated before it. Stored documents get their record type from the
    listing without being downloaded again."""
    listing_url = listing_url or urljoin(fetcher.settings.fda_base_url, READING_ROOM_PATH)
    downloaded = 0
    with ingestion_run(session, "form_483") as run:
        entries: list[ReadingRoomEntry] = []
        for n, (cols, raw, cells) in enumerate(iter_pages(fetcher, listing_url, max_rows=max_rows)):
            page = store_document(session, archive, source="form_483_listing",
                                  source_key=f"form_483_listing:page:{n}", data=raw,
                                  media_type="application/json", source_url=listing_url, run=run)
            tally(run, page.outcome)
            entries.extend(entries_from_listing(cols, cells, listing_url))
        for entry in entries:
            if since and entry.record_date and entry.record_date < since:
                continue
            key = f"483:{entry.pdf_url}"
            stored = session.scalars(select(Document).where(Document.source_key == key)).first()
            if stored is not None:
                if entry.record_type and (stored.meta or {}).get("record_type") != entry.record_type:
                    stored.meta = {**(stored.meta or {}), "record_type": entry.record_type}
                continue
            if limit is not None and downloaded >= limit:
                break
            try:
                data = fetcher.get(entry.pdf_url).content
                downloaded += 1
                if not data.startswith(b"%PDF"):
                    raise ValueError("response is not a PDF")
                _store_483(session, archive, run, data, key=key, url=entry.pdf_url, entry=entry)
            except Exception as exc:
                record_error(run, entry.pdf_url, exc)
            if downloaded and downloaded % 25 == 0:
                session.commit()  # a long download resumes from here if interrupted
        return {"entries": len(entries), "downloaded": downloaded, "errors": run.n_errors}


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
