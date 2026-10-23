"""MVP web UI (PRD section 11): citation search, trend views, failure-mode
pages, digest archive, methodology. No accounts beyond a subscribe form.

Gate: a failure mode's trend, counts, and tags are public only when its
gold-set precision meets the bar. Everything else shows as "in review".
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..analytics.stats import cfr_breakdown, methodology_note, mode_trend, top_modes
from ..config import get_settings
from ..db import get_engine
from ..digest.build import archive, read_issue, render_issue_html
from ..evaluation.gold import passing_modes
from ..models import CfrReference, Citation, Document, Facility, FailureMode, ModeEvaluation, Observation, Subscriber
from ..remediation.patterns import load_patterns
from ..taxonomy.loader import Taxonomy, load_taxonomy
from .charts import SERIES, trend_svg

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app(*, database_url: str | None = None, content_dir: Path | str = "content",
               taxonomy: Taxonomy | None = None) -> FastAPI:
    app = FastAPI(title="Computerized-Systems Enforcement Intelligence", docs_url=None, redoc_url=None)
    engine = get_engine(database_url)
    tax = taxonomy or load_taxonomy()
    content = Path(content_dir)
    settings = get_settings()

    def db():
        with Session(engine) as s:
            yield s

    def gate(s: Session) -> set[str]:
        return passing_modes(s, tax.version, bar=settings.precision_bar)

    def render(request: Request, name: str, **ctx):
        return TEMPLATES.TemplateResponse(request, name, {"tax": tax, **ctx})

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, s: Session = Depends(db)):
        allowed = gate(s)
        since = date.today().year - 1
        top = top_modes(s, tax.version, since_year=since, allowed=allowed, limit=10)
        issues = archive(content / "digest")
        return render(request, "home.html", top=top, since=since, latest=issues[0] if issues else None,
                      n_live=len(allowed))

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = "", mode: str = "", cfr: str = "", year: str = "", kind: str = "",
               facility_type: str = "", page: int = 1, s: Session = Depends(db)):
        allowed = gate(s)
        query = (select(Citation, Observation, Document)
                 .join(Observation, Citation.observation_id == Observation.id)
                 .join(Document, Observation.document_id == Document.id)
                 .where(Document.is_current.is_(True)))
        if q:
            if engine.dialect.name == "postgresql":
                query = query.where(func.to_tsvector("english", Citation.text).op("@@")(func.plainto_tsquery("english", q)))
            else:
                for term in q.split():
                    query = query.where(Citation.text.ilike(f"%{term}%"))
        if mode:
            if mode not in allowed:
                raise HTTPException(404, "This failure mode is in review and not yet published.")
            query = query.where(Citation.id.in_(select(FailureMode.citation_id).where(
                FailureMode.mode_id == mode, FailureMode.provenance == "human", FailureMode.status == "accepted")))
        if cfr:
            query = query.join(CfrReference, Citation.cfr_reference_id == CfrReference.id).where(
                or_(CfrReference.section == cfr, CfrReference.canonical.ilike(f"%{cfr}%")))
        if year.isdigit():
            y = int(year)
            query = query.where(Observation.observed_date >= date(y, 1, 1), Observation.observed_date < date(y + 1, 1, 1))
        if kind in ("483", "wl"):
            query = query.where(Observation.kind == kind)
        if facility_type:
            query = query.join(Facility, Observation.facility_id == Facility.id).where(
                Facility.establishment_type.ilike(f"%{facility_type}%"))
        per = 25
        total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = s.execute(query.order_by(Observation.observed_date.desc().nulls_last(), Citation.id)
                         .offset((max(page, 1) - 1) * per).limit(per)).all()
        ids = [r.Citation.id for r in rows]
        tags: dict[int, list[str]] = {}
        if ids:
            for cid, mid in s.execute(select(FailureMode.citation_id, FailureMode.mode_id).where(
                    FailureMode.citation_id.in_(ids), FailureMode.provenance == "human",
                    FailureMode.status == "accepted", FailureMode.mode_id.in_(allowed or {""}))):
                tags.setdefault(cid, []).append(mid)
        return render(request, "search.html", rows=rows, tags=tags, total=total, page=page, per=per,
                      params=dict(q=q, mode=mode, cfr=cfr, year=year, kind=kind, facility_type=facility_type),
                      allowed=sorted(allowed))

    @app.get("/modes", response_class=HTMLResponse)
    def modes(request: Request, s: Session = Depends(db)):
        allowed = gate(s)
        patterns = {m for m, p in load_patterns(content / "remediation").items() if p.publishable}
        return render(request, "modes.html", allowed=allowed, patterns=patterns)

    @app.get("/modes/{mode_id}", response_class=HTMLResponse)
    def mode_page(request: Request, mode_id: str, s: Session = Depends(db)):
        mode = tax.modes.get(mode_id)
        if mode is None:
            raise HTTPException(404, "Unknown failure mode")
        live = mode_id in gate(s)
        trend = mode_trend(s, mode_id, tax.version) if live else {}
        pattern = load_patterns(content / "remediation").get(mode_id)
        ev = s.scalars(select(ModeEvaluation).where(ModeEvaluation.mode_id == mode_id,
                                                    ModeEvaluation.taxonomy_version == tax.version,
                                                    ModeEvaluation.pipeline == "published")).first()
        return render(request, "mode.html", mode=mode, live=live, trend=trend,
                      chart=Markup(trend_svg(trend)), series=SERIES,
                      cfr=cfr_breakdown(s, mode_id, tax.version) if live else [],
                      note=methodology_note(s, tax.version, [mode_id]).render() if live else None,
                      pattern=pattern if pattern and pattern.publishable else None, ev=ev,
                      bar=settings.precision_bar)

    @app.get("/digest", response_class=HTMLResponse)
    def digest_index(request: Request):
        return render(request, "digest_index.html", issues=archive(content / "digest"))

    @app.get("/digest/{issue}", response_class=HTMLResponse)
    def digest_issue(request: Request, issue: str):
        if not re.fullmatch(r"\d{4}-\d{2}", issue):
            raise HTTPException(404)
        path = content / "digest" / f"{issue}.md"
        if not path.exists():
            raise HTTPException(404)
        meta, body = read_issue(path)
        if meta.get("status") != "published":
            raise HTTPException(404)
        return render(request, "digest_issue.html", meta=meta, html=Markup(render_issue_html(body)))

    @app.get("/methodology", response_class=HTMLResponse)
    def methodology(request: Request, s: Session = Depends(db)):
        evals = s.scalars(select(ModeEvaluation).where(ModeEvaluation.taxonomy_version == tax.version,
                                                       ModeEvaluation.pipeline == "published")
                          .order_by(ModeEvaluation.mode_id)).all()
        return render(request, "methodology.html", evals=[e for e in evals if e.support or e.tp or e.fp],
                      bar=settings.precision_bar, allowed=gate(s))

    @app.post("/subscribe")
    def subscribe(email: str = Form(...), s: Session = Depends(db)):
        email = email.strip().lower()
        if not _EMAIL.match(email) or len(email) > 320:
            raise HTTPException(400, "Enter a valid email address.")
        if s.scalars(select(Subscriber).where(Subscriber.email == email)).first() is None:
            s.add(Subscriber(email=email))
            s.commit()
        return RedirectResponse("/?subscribed=1", status_code=303)

    return app
