"""MVP web UI (PRD section 11): citation search, trend views, failure-mode
pages, digest archive, methodology. No accounts beyond a subscribe form.

Gate: a failure mode's trend, counts, and tags are public only when its
gold-set precision meets the bar. Everything else shows as "in review".

With ``review=True`` (``auditcore serve --review``) the app also serves a
local review page at /review. It is off by default so the public site never
exposes it, and writes through the same functions as ``auditcore review``.
"""

from __future__ import annotations

import re
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..analytics.stats import cfr_breakdown, methodology_note, mode_trend, top_modes
from ..config import get_settings
from ..db import get_engine
from ..digest.build import archive, read_issue, render_issue_html
from ..evaluation.gold import passing_modes
from ..models import CfrReference, Citation, Document, Facility, FailureMode, ModeEvaluation, Observation, Subscriber
from ..remediation.patterns import load_patterns
from ..tagging.review import apply_decision, history, progress, review_items
from ..taxonomy.loader import Taxonomy, load_taxonomy
from .charts import SERIES, trend_svg

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REVIEW_PER_PAGE = 50  # matches the CLI review default
_ACTIONS = {"a": (True, None), "p": (True, True), "e": (True, False), "r": (False, None)}


def highlight(text: str, spans: list[str]) -> Markup:
    """Citation text with each evidence span marked. Spans match the way the
    tagger checks evidence: case-insensitive, any run of whitespace."""
    ranges = []
    for span in spans:
        words = (span or "").strip(" .\"'").split()
        if words:
            pattern = r"\s+".join(re.escape(w) for w in words)
            ranges += [(m.start(), m.end()) for m in re.finditer(pattern, text, re.IGNORECASE)]
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, pos = [], 0
    for start, end in merged:
        out += [escape(text[pos:start]), Markup("<mark>"), escape(text[start:end]), Markup("</mark>")]
        pos = end
    out.append(escape(text[pos:]))
    return Markup("").join(out)


def create_app(*, database_url: str | None = None, content_dir: Path | str = "content",
               taxonomy: Taxonomy | None = None, review: bool = False, reviewer: str | None = None) -> FastAPI:
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
        return TEMPLATES.TemplateResponse(request, name, {"tax": tax, "review_enabled": review, **ctx})

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
               facility_type: str = "", paper: str = "", page: int = 1, s: Session = Depends(db)):
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
                FailureMode.mode_id == mode, FailureMode.provenance == "human", FailureMode.status == "accepted",
                FailureMode.paper_only.is_(False))))
        if paper == "1":
            # Paper-only data-integrity findings: searchable, never counted as computerized systems.
            query = query.where(Citation.id.in_(select(FailureMode.citation_id).where(
                FailureMode.paper_only.is_(True), FailureMode.provenance == "human", FailureMode.status == "accepted")))
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
        paper_tags: dict[int, list[str]] = {}
        if ids:
            for cid, mid, is_paper in s.execute(select(FailureMode.citation_id, FailureMode.mode_id, FailureMode.paper_only).where(
                    FailureMode.citation_id.in_(ids), FailureMode.provenance == "human",
                    FailureMode.status == "accepted")):
                if is_paper:
                    paper_tags.setdefault(cid, []).append(mid)
                elif mid in allowed:
                    tags.setdefault(cid, []).append(mid)
        return render(request, "search.html", rows=rows, tags=tags, paper_tags=paper_tags, total=total, page=page, per=per,
                      params=dict(q=q, mode=mode, cfr=cfr, year=year, kind=kind, facility_type=facility_type, paper=paper),
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

    if review:
        if not reviewer:
            raise ValueError("the review page needs a reviewer name")
        _add_review_routes(app, db, render, tax, reviewer)
    return app


def review_years(s: Session) -> list[int]:
    """Issue years that have at least one tagged citation, newest first."""
    rows = s.scalars(select(Observation.observed_date).distinct()
                     .join(Citation, Citation.observation_id == Observation.id)
                     .join(FailureMode, FailureMode.citation_id == Citation.id)
                     .where(FailureMode.mode_id != "NONE", Observation.observed_date.is_not(None)))
    return sorted({d.year for d in rows}, reverse=True)


def _add_review_routes(app: FastAPI, db, render, tax: Taxonomy, reviewer: str) -> None:
    # Per-process token on every form: no login, but another site open in the
    # same browser cannot post decisions to this page.
    token = secrets.token_urlsafe(16)

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, state: str = "pending", mode: str = "", proposer: str = "",
                    disagree: str = "", low: str = "", paper: str = "", year: str = "", page: int = 1,
                    focus: int = 0, s: Session = Depends(db)):
        state = state if state in ("pending", "decided") else "pending"
        year = year if year.isdigit() else ""
        items = review_items(s, state=state, mode_id=mode or None, proposer=proposer or None,
                             disagreements=disagree == "1", low_confidence=low == "1", paper=paper or None,
                             year=int(year) if year else None)
        pages = max(1, -(-len(items) // REVIEW_PER_PAGE))
        page = min(max(page, 1), pages)
        shown = items[(page - 1) * REVIEW_PER_PAGE: page * REVIEW_PER_PAGE]
        params = dict(state=state, mode=mode, proposer=proposer, disagree=disagree, low=low, paper=paper, year=year)
        query = urlencode({k: v for k, v in params.items() if v})
        info = {}
        for i in shown:
            c = s.get(Citation, i.citation_id)
            info[i.citation_id] = (c, c.observation, c.observation.document)
        return render(request, "review.html", items=shown, total=len(items), page=page, pages=pages,
                      params=params, query=query, focus=focus, progress=progress(s), reviewer=reviewer,
                      token=token, info=info, modes=sorted(tax.modes), years=review_years(s),
                      history={(i.citation_id, i.mode_id): history(s, i.citation_id, i.mode_id) for i in shown},
                      marked={(i.citation_id, i.mode_id): highlight(i.text, [p.evidence for p in i.proposals])
                              for i in shown})

    @app.post("/review/decide")
    def review_decide(citation_id: int = Form(...), mode_id: str = Form(...), action: str = Form(...),
                      reason: str = Form(""), csrf: str = Form(""), back: str = Form("/review"),
                      s: Session = Depends(db)):
        if not secrets.compare_digest(csrf, token):
            raise HTTPException(403, "Stale or missing form token. Reload the review page.")
        if action not in _ACTIONS or mode_id not in tax.modes or s.get(Citation, citation_id) is None:
            raise HTTPException(400, "Unknown citation, mode or action.")
        accept, flag = _ACTIONS[action]
        apply_decision(s, citation_id, mode_id, accept=accept, paper_only=flag, reviewer=reviewer,
                       reason=reason, interface="web", taxonomy_version=tax.version)
        s.commit()
        if not back.startswith("/review") or back.startswith("//"):
            back = "/review"
        return RedirectResponse(back, status_code=303)
