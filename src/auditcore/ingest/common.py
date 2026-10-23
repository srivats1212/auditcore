from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Any, Iterable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Facility

log = logging.getLogger(__name__)

_FEI = re.compile(r"\bFEI(?:\s*(?:No\.?|Number|#))?\s*[:#]?\s*(\d{7,10})\b", re.IGNORECASE)


class Fetcher:
    """Polite HTTP client: fixed delay between requests, bounded retries."""

    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = settings.user_agent
        self._last = 0.0

    def _wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.settings.request_delay_s:
            time.sleep(self.settings.request_delay_s - delta)
        self._last = time.monotonic()

    def request(self, method: str, url: str, *, retries: int = 3, **kw: Any) -> requests.Response:
        kw.setdefault("timeout", 60)
        for attempt in range(retries + 1):
            self._wait()
            try:
                resp = self.http.request(method, url, **kw)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp
            except requests.ConnectionError:
                if attempt == retries:
                    raise
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError("unreachable")

    def get(self, url: str, **kw: Any) -> requests.Response:
        return self.request("GET", url, **kw)


def find_fei(text: str | None) -> str | None:
    if not text:
        return None
    m = _FEI.search(text)
    return m.group(1) if m else None


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for candidate in (s, s.split("T")[0], s.split(" ")[0]):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def pick(row: dict[str, Any], *names: str) -> Any:
    """Tolerant column lookup: exact name first, then case/space-insensitive."""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    norm = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in row.items() if isinstance(k, str)}
    for n in names:
        v = norm.get(re.sub(r"[^a-z0-9]", "", n.lower()))
        if v not in (None, ""):
            return v
    return None


def get_or_create_facility(session: Session, *, fei: str | None, name: str | None, **fields: Any) -> Facility | None:
    if not fei and not name:
        return None
    fac = None
    if fei:
        fac = session.scalars(select(Facility).where(Facility.fei == fei)).first()
    if fac is None and not fei and name:
        # Without FEI we only match an existing FEI-less facility by exact name.
        fac = session.scalars(select(Facility).where(Facility.fei.is_(None), Facility.name == name)).first()
    if fac is None:
        fac = Facility(fei=fei, name=name or f"FEI {fei}")
        session.add(fac)
    for k, v in fields.items():
        if v not in (None, "") and getattr(fac, k, None) in (None, ""):
            setattr(fac, k, v)
    session.flush()
    return fac


def chunked(items: Iterable[Any], n: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch
