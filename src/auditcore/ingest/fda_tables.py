"""FDA listing pages (Warning Letters, OII FOIA reading room) are Drupal
"datatables" views. The HTML only carries the first page of rows; the full
list comes from ``/datatables/views/ajax`` with the view parameters embedded
in the page's drupalSettings, paged with DataTables' ``start``/``length``.

The page's XLSX export is not usable as an index: it has no letter links and
no CMS numbers, and stops at 1,000 rows.
"""

from __future__ import annotations

import json
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .common import Fetcher

AJAX_PATH = "/datatables/views/ajax"
_AJAX_CFG = re.compile(r'"ajax":\{"url":"\\/datatables\\/views\\/ajax","type":"GET","data":(\{.*?\})\}')


class DatatableError(RuntimeError):
    pass


def view_params(page_html: str) -> dict:
    m = _AJAX_CFG.search(page_html)
    if not m:
        raise DatatableError("no datatables AJAX config found on page; FDA may have changed the page")
    return json.loads(m.group(1))


def headers(page_html: str) -> list[str]:
    soup = BeautifulSoup(page_html, "lxml")
    table = soup.select_one("table#datatable") or soup.find("table")
    if table is None:
        raise DatatableError("no table on page")
    return [th.get_text(" ", strip=True) for th in table.select("thead th")]


def cell_text(cell: str) -> str:
    return BeautifulSoup(cell or "", "lxml").get_text(" ", strip=True)


def cell_href(cell: str, base_url: str) -> str | None:
    a = BeautifulSoup(cell or "", "lxml").find("a", href=True)
    return urljoin(base_url, a["href"]) if a else None


def iter_pages(fetcher: Fetcher, page_url: str, *, page_size: int = 500,
               max_rows: int | None = None) -> Iterator[tuple[list[str], bytes, list[list[str]]]]:
    """Yield (column headers, raw JSON bytes, rows) per AJAX page. Rows are
    lists of cell HTML in header order."""
    page = fetcher.get(page_url)
    html = page.text
    cols = headers(html)
    params = view_params(html)
    ajax_url = urljoin(page_url, AJAX_PATH)
    start, draw = 0, 1
    while True:
        length = page_size if max_rows is None else min(page_size, max_rows - start)
        if length <= 0:
            return
        resp = fetcher.get(ajax_url, params={**params, "draw": draw, "start": start, "length": length})
        payload = resp.json()
        rows = payload.get("data") or []
        yield cols, resp.content, rows
        start += len(rows)
        draw += 1
        if not rows or start >= int(payload.get("recordsFiltered") or payload.get("recordsTotal") or 0):
            return


def as_dicts(cols: list[str], rows: list[list[str]]) -> list[dict[str, str]]:
    return [dict(zip(cols, r)) for r in rows]
