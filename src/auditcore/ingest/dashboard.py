"""FDA Data Dashboard API: inspection and compliance-action metadata
(PRD section 4, source 1). Requires a free key: set FDA_DASHBOARD_USER and
FDA_DASHBOARD_KEY.

The API takes a JSON POST per endpoint with paging (``start``, ``rows``),
filters and a required ``columns`` list, and authenticates with
``Authorization-User`` / ``Authorization-Key`` headers. Field names follow the
published OpenAPI spec (datadashboard.fda.gov/oii/api/ddapi.json, checked
2026-10-23). Every raw response page is archived before it is parsed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ComplianceAction, Inspection
from .common import Fetcher, get_or_create_facility, parse_date, pick
from .store import RawArchive, ingestion_run, record_error, store_document, tally

log = logging.getLogger(__name__)

PAGE_SIZE = 5000  # API maximum


class DashboardError(RuntimeError):
    pass


class DashboardClient:
    def __init__(self, fetcher: Fetcher):
        s = fetcher.settings
        if not (s.dashboard_user and s.dashboard_key):
            raise RuntimeError("Set FDA_DASHBOARD_USER and FDA_DASHBOARD_KEY (free key from the FDA Data Dashboard).")
        self.fetcher = fetcher
        self.base = s.dashboard_base_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization-User": s.dashboard_user,
            "Authorization-Key": s.dashboard_key,
        }

    def pages(self, endpoint: str, filters: dict[str, Any]) -> Iterator[tuple[int, bytes, list[dict]]]:
        """Page with 1-based ``start``. ``resultcount`` is the rows in this
        page; the total is ``totalrecordcount``, returned only when the request
        sets ``returntotalcount``. ``columns: []`` (required) means all."""
        start, total = 1, None
        while True:
            body = {"start": start, "rows": PAGE_SIZE, "sort": "", "sortorder": "", "filters": filters,
                    "columns": [], "returntotalcount": total is None}
            resp = self.fetcher.request("POST", f"{self.base}/{endpoint}", headers=self.headers, data=json.dumps(body))
            payload = resp.json()
            if isinstance(payload, list) or "result" not in payload:
                # Validation and auth errors come back as a list of {statuscode, message, ...}.
                raise DashboardError(f"{endpoint}: {json.dumps(payload)[:500]}")
            rows = payload.get("result") or []
            if total is None:
                total = int(payload.get("totalrecordcount") or 0)
            yield start, resp.content, rows
            start += len(rows)
            if len(rows) < PAGE_SIZE or start > total:
                return


def _store_page(session, archive, run, endpoint, filters, start, raw):
    key = f"dashboard:{endpoint}:{json.dumps(filters, sort_keys=True)}:{start}"
    result = store_document(session, archive, source="dashboard", source_key=key, data=raw,
                            media_type="application/json", run=run)
    tally(run, result.outcome)
    return result


def upsert_inspection(session: Session, row: dict) -> Inspection | None:
    insp_id = pick(row, "InspectionID", "Inspection ID")
    if insp_id is None:
        return None
    insp_id = str(insp_id)
    fac = get_or_create_facility(
        session,
        fei=str(pick(row, "FEINumber", "FEI Number") or "") or None,
        name=pick(row, "LegalName", "Legal Name", "FirmName"),
        address=", ".join(x for x in (pick(row, "AddressLine1"), pick(row, "AddressLine2"), pick(row, "ZipCode")) if x) or None,
        city=pick(row, "City"),
        state=pick(row, "State", "StateCode"),
        country=pick(row, "CountryName", "CountryCode", "Country"),
    )
    insp = session.scalars(select(Inspection).where(Inspection.dashboard_id == insp_id)).first()
    if insp is None:
        insp = Inspection(dashboard_id=insp_id)
        session.add(insp)
    insp.facility_id = fac.id if fac else None
    insp.end_date = parse_date(pick(row, "InspectionEndDate", "Inspection End Date"))
    insp.classification = pick(row, "ClassificationCode", "Classification")  # OAI / VAI / NAI
    insp.project_area = pick(row, "ProjectArea", "Project Area")
    insp.product_type = pick(row, "ProductType", "Product Type")
    session.flush()
    return insp


def upsert_compliance_action(session: Session, row: dict) -> ComplianceAction | None:
    action_id = pick(row, "CaseInjunctionID", "Case/Injunction ID", "ActionID")
    fac = get_or_create_facility(
        session,
        fei=str(pick(row, "FEINumber", "FEI Number") or "") or None,
        name=pick(row, "LegalName", "Legal Name", "FirmName"),
        state=pick(row, "State", "StateCode"),
        country=pick(row, "CountryName", "CountryCode"),
    )
    ca = None
    if action_id is not None:
        ca = session.scalars(select(ComplianceAction).where(ComplianceAction.dashboard_id == str(action_id))).first()
    if ca is None:
        ca = ComplianceAction(dashboard_id=str(action_id) if action_id is not None else None,
                              action_type=pick(row, "ActionType", "Action Type") or "unknown")
        session.add(ca)
    ca.facility_id = fac.id if fac else None
    ca.action_date = parse_date(pick(row, "ActionTakenDate", "Action Taken Date"))
    ca.center = pick(row, "Center", "FDACenter")
    ca.product_type = pick(row, "ProductType", "Product Type")
    ca.details = row
    session.flush()
    return ca


def ingest_dashboard(session: Session, archive: RawArchive, fetcher: Fetcher, *,
                     product_types: list[str] | None = None) -> dict:
    client = DashboardClient(fetcher)
    filters: dict[str, Any] = {"ProductType": product_types} if product_types else {}
    counts = {"inspections": 0, "compliance_actions": 0}
    with ingestion_run(session, "dashboard") as run:
        for endpoint, upsert, label in (
            ("inspections_classifications", upsert_inspection, "inspections"),
            ("compliance_actions", upsert_compliance_action, "compliance_actions"),
        ):
            try:
                for start, raw, rows in client.pages(endpoint, filters):
                    _store_page(session, archive, run, endpoint, filters, start, raw)
                    for row in rows:
                        try:
                            if upsert(session, row) is not None:
                                counts[label] += 1
                        except Exception as exc:
                            record_error(run, f"{endpoint}:{row}", exc)
            except Exception as exc:
                record_error(run, endpoint, exc)
    return counts
