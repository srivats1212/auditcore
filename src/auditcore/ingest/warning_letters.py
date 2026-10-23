"""Warning letters: XLSX index from the FDA Warning Letters page, then the
full letter HTML for each row (PRD section 4, source 2).

The index export is downloaded from the Warning Letters page ("Export Excel")
and passed in as a file, because the export link is generated client-side.
Each row links to the letter page, whose URL ends in
``<slug>-<CMS number>-<MMDDYYYY>``; the CMS number is the letter's key.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WarningLetter
from ..text.ocr_cleanup import clean_text
from .common import Fetcher, find_fei, get_or_create_facility, parse_date, pick
from .store import RawArchive, ingestion_run, record_error, store_document, tally

log = logging.getLogger(__name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CMS_FROM_URL = re.compile(r"-(\d{5,7})-\d{6,8}/?$")


@dataclass
class IndexRow:
    company_name: str | None
    letter_url: str | None
    cms_number: str | None
    issue_date: date | None
    posted_date: date | None
    issuing_office: str | None
    subject: str | None


def cms_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _CMS_FROM_URL.search(url.split("?")[0])
    return m.group(1) if m else None


def parse_index_xlsx(data: bytes, base_url: str = "https://www.fda.gov") -> list[IndexRow]:
    wb = load_workbook(io.BytesIO(data), read_only=False, data_only=True)
    ws = wb.active
    rows = ws.iter_rows()
    header_cells = next(rows)
    headers = [str(c.value).strip() if c.value is not None else "" for c in header_cells]
    out: list[IndexRow] = []
    for cells in rows:
        row = {h: c.value for h, c in zip(headers, cells)}
        if not any(v not in (None, "") for v in row.values()):
            continue
        url = pick(row, "Letter URL", "URL", "Link")
        if not url:
            # The export usually hyperlinks the company name cell.
            for h, c in zip(headers, cells):
                if c.hyperlink is not None and c.hyperlink.target:
                    url = c.hyperlink.target
                    break
        if url:
            url = urljoin(base_url, str(url).strip())
        cms = pick(row, "CMS Number", "MARCS-CMS Number", "CMS #", "CMS") or cms_from_url(url)
        out.append(
            IndexRow(
                company_name=pick(row, "Company Name", "Company", "Firm Name"),
                letter_url=url,
                cms_number=str(cms).strip() if cms else None,
                issue_date=parse_date(pick(row, "Letter Issue Date", "Issue Date", "Letter Date")),
                posted_date=parse_date(pick(row, "Posted Date", "Posted")),
                issuing_office=pick(row, "Issuing Office", "Office"),
                subject=pick(row, "Subject"),
            )
        )
    return out


def letter_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    node = (
        soup.select_one("article")
        or soup.select_one("[role=main]")
        or soup.select_one("main")
        or soup.select_one("#main-content")
        or soup.body
        or soup
    )
    # Keep block structure so numbered violations start on their own line.
    for br in node.find_all("br"):
        br.replace_with("\n")
    blocks = [el.get_text(" ", strip=True) for el in node.find_all(["p", "li", "h1", "h2", "h3", "h4", "div"])
              if not el.find(["p", "li", "div"])]
    text = "\n\n".join(b for b in blocks if b)
    return text or node.get_text("\n", strip=True)


def ingest_index(session: Session, archive: RawArchive, fetcher: Fetcher | None, xlsx: bytes, *,
                 source_url: str | None = None, fetch_letters: bool = True, limit: int | None = None) -> dict:
    with ingestion_run(session, "warning_letters") as run:
        idx = store_document(session, archive, source="wl_index", source_key="wl_index", data=xlsx,
                             media_type=XLSX, source_url=source_url, run=run)
        tally(run, idx.outcome)
        rows = parse_index_xlsx(xlsx)
        fetched = 0
        for row in rows:
            if not row.cms_number:
                record_error(run, row.company_name or "?", "no CMS number in index row")
                continue
            wl = session.scalars(select(WarningLetter).where(WarningLetter.cms_number == row.cms_number)).first()
            if wl is None:
                wl = WarningLetter(cms_number=row.cms_number)
                session.add(wl)
            wl.company_name = wl.company_name or row.company_name
            wl.issue_date = wl.issue_date or row.issue_date
            wl.posted_date = wl.posted_date or row.posted_date
            wl.issuing_office = wl.issuing_office or row.issuing_office
            wl.subject = wl.subject or row.subject
            wl.letter_url = wl.letter_url or row.letter_url
            session.flush()

            if not (fetch_letters and fetcher and row.letter_url):
                continue
            if limit is not None and fetched >= limit:
                continue
            try:
                resp = fetcher.get(row.letter_url)
                fetched += 1
                ingest_letter_html(session, archive, wl, resp.content, url=row.letter_url, run=run)
            except Exception as exc:  # one bad letter must not stop the run
                record_error(run, f"wl:{row.cms_number}", exc)
        return {"rows": len(rows), "fetched": fetched, "errors": run.n_errors}


def ingest_letter_html(session: Session, archive: RawArchive, wl: WarningLetter, html: bytes, *,
                       url: str | None, run=None) -> str:
    result = store_document(session, archive, source="warning_letter", source_key=f"wl:{wl.cms_number}",
                            data=html, media_type="text/html", source_url=url, run=run)
    if run is not None:
        tally(run, result.outcome)
    doc = result.document
    if result.outcome != "skipped" or doc.text_raw is None:
        doc.text_raw = letter_text_from_html(html.decode("utf-8", errors="replace"))
        doc.text_clean = clean_text(doc.text_raw)
    wl.document_id = doc.id if doc.is_current else wl.document_id
    fei = find_fei(doc.text_clean)
    if fei or wl.company_name:
        fac = get_or_create_facility(session, fei=fei, name=wl.company_name)
        wl.facility_id = fac.id if fac else wl.facility_id
    session.flush()
    return result.outcome


def ingest_letter_files(session: Session, archive: RawArchive, paths: list[Path]) -> dict:
    """Offline path: ingest saved letter HTML files named by CMS number
    (e.g. ``640123.html``) for letters already present in the index."""
    with ingestion_run(session, "warning_letters_files") as run:
        for path in paths:
            cms = path.stem
            wl = session.scalars(select(WarningLetter).where(WarningLetter.cms_number == cms)).first()
            if wl is None:
                wl = WarningLetter(cms_number=cms)
                session.add(wl)
                session.flush()
            try:
                ingest_letter_html(session, archive, wl, path.read_bytes(), url=wl.letter_url, run=run)
            except Exception as exc:
                record_error(run, str(path), exc)
        return {"files": len(paths), "errors": run.n_errors}
