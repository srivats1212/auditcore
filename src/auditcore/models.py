"""Data model (PRD section 5).

The atomic unit of the product is ``Citation``: one observation, one CFR
reference, one or more applied failure-mode tags. Every count the product
publishes is a count of citations.

Departure from the PRD table list: 483 observations and warning-letter
violations share one ``observation`` table (``kind`` = "483" | "wl") so that a
citation has exactly one parent FK. Querying by document type is a filter on
``kind``.

Types are kept portable (JSON, not JSONB) so the test suite runs on SQLite;
production runs on Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IngestionRun(Base):
    __tablename__ = "ingestion_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | partial | failed
    n_seen: Mapped[int] = mapped_column(Integer, default=0)
    n_new: Mapped[int] = mapped_column(Integer, default=0)
    n_versioned: Mapped[int] = mapped_column(Integer, default=0)
    n_skipped: Mapped[int] = mapped_column(Integer, default=0)
    n_errors: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class Document(Base):
    """Parent of every enforcement document. Immutable once written: a content
    change produces a new row with ``supersedes_id`` pointing at the prior one."""

    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint("source_key", "version"),
        UniqueConstraint("source_key", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # warning_letter | form_483 | wl_index | dashboard
    source_key: Mapped[str] = mapped_column(String(255), index=True)  # e.g. "wl:CMS-123456"
    source_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # SHA-256 of raw bytes
    raw_path: Mapped[str] = mapped_column(Text)  # path inside the archive dir
    media_type: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    text_raw: Mapped[str | None] = mapped_column(Text)
    text_clean: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_run.id"))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    supersedes: Mapped["Document | None"] = relationship(remote_side=[id])


class Facility(Base):
    __tablename__ = "facility"

    id: Mapped[int] = mapped_column(primary_key=True)
    fei: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(64))
    establishment_type: Mapped[str | None] = mapped_column(String(128))


class Inspection(Base):
    __tablename__ = "inspection"

    id: Mapped[int] = mapped_column(primary_key=True)
    dashboard_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facility.id"), index=True)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    classification: Mapped[str | None] = mapped_column(String(16))  # NAI | VAI | OAI
    project_area: Mapped[str | None] = mapped_column(Text)
    product_type: Mapped[str | None] = mapped_column(Text)
    investigators: Mapped[list] = mapped_column(JSON, default=list)

    facility: Mapped[Facility | None] = relationship()


class WarningLetter(Base):
    __tablename__ = "warning_letter"

    id: Mapped[int] = mapped_column(primary_key=True)
    cms_number: Mapped[str] = mapped_column(String(32), unique=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"))
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facility.id"), index=True)
    company_name: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None] = mapped_column(Date, index=True)
    posted_date: Mapped[date | None] = mapped_column(Date)
    issuing_office: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    letter_url: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document | None] = relationship()
    facility: Mapped[Facility | None] = relationship()


class Form483(Base):
    """One published 483 PDF from the OII reading room."""

    __tablename__ = "form_483"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facility.id"), index=True)
    inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspection.id"))
    company_name: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None] = mapped_column(Date, index=True)
    pdf_url: Mapped[str] = mapped_column(Text)

    document: Mapped[Document] = relationship()
    facility: Mapped[Facility | None] = relationship()


class Observation(Base):
    """A numbered 483 observation or warning-letter violation."""

    __tablename__ = "observation"
    __table_args__ = (UniqueConstraint("document_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)  # "483" | "wl"
    number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facility.id"), index=True)
    inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspection.id"))
    warning_letter_id: Mapped[int | None] = mapped_column(ForeignKey("warning_letter.id"))
    form_483_id: Mapped[int | None] = mapped_column(ForeignKey("form_483.id"))
    observed_date: Mapped[date | None] = mapped_column(Date, index=True)

    document: Mapped[Document] = relationship()
    citations: Mapped[list["Citation"]] = relationship(back_populates="observation")


class ComplianceAction(Base):
    __tablename__ = "compliance_action"

    id: Mapped[int] = mapped_column(primary_key=True)
    dashboard_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facility.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(128))
    action_date: Mapped[date | None] = mapped_column(Date)
    center: Mapped[str | None] = mapped_column(String(64))
    product_type: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class CfrReference(Base):
    __tablename__ = "cfr_reference"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical: Mapped[str] = mapped_column(String(64), unique=True)  # "21 CFR 211.68(b)"
    part: Mapped[str] = mapped_column(String(16), index=True)  # "211"
    section: Mapped[str] = mapped_column(String(32))  # "211.68"
    paragraph: Mapped[str | None] = mapped_column(String(32))  # "(b)"
    regime: Mapped[str | None] = mapped_column(String(16))  # cGMP | QSR | QMSR | Part11 | GLP | GCP
    description: Mapped[str | None] = mapped_column(Text)


class Citation(Base):
    __tablename__ = "citation"
    __table_args__ = (UniqueConstraint("observation_id", "ordinal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    cfr_reference_id: Mapped[int | None] = mapped_column(ForeignKey("cfr_reference.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(16), default="rules")  # rules | llm | human

    observation: Mapped[Observation] = relationship(back_populates="citations")
    cfr_reference: Mapped[CfrReference | None] = relationship()
    tags: Mapped[list["FailureMode"]] = relationship(back_populates="citation")


class FailureModeTaxonomy(Base):
    __tablename__ = "failure_mode_taxonomy"
    __table_args__ = (UniqueConstraint("mode_id", "taxonomy_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mode_id: Mapped[str] = mapped_column(String(32), index=True)  # stable, e.g. "CS-AUD-002"
    taxonomy_version: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    definition: Mapped[str] = mapped_column(Text)
    examples: Mapped[list] = mapped_column(JSON, default=list)
    cfr: Mapped[list] = mapped_column(JSON, default=list)
    related: Mapped[list] = mapped_column(JSON, default=list)


class FailureMode(Base):
    """An applied tag: citation -> taxonomy entry, with provenance."""

    __tablename__ = "failure_mode"
    __table_args__ = (UniqueConstraint("citation_id", "mode_id", "provenance"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    citation_id: Mapped[int] = mapped_column(ForeignKey("citation.id"), index=True)
    mode_id: Mapped[str] = mapped_column(String(32), index=True)
    taxonomy_version: Mapped[str] = mapped_column(String(16))
    provenance: Mapped[str] = mapped_column(String(16))  # rules | llm | human
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text)  # quoted span supporting the tag
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)  # proposed|accepted|rejected
    # A data-integrity finding about paper records only. Kept so it stays
    # searchable, but never counted as a computerized-systems finding.
    paper_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)
    model: Mapped[str | None] = mapped_column(String(64))
    reviewer: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    citation: Mapped[Citation] = relationship(back_populates="tags")


class ReviewEvent(Base):
    """Audit trail of review decisions, one row per change to a
    (citation, mode) decision, including the first one. Append-only: the
    current decision lives on ``failure_mode``; this records how it got there."""

    __tablename__ = "review_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    citation_id: Mapped[int] = mapped_column(ForeignKey("citation.id"), index=True)
    mode_id: Mapped[str] = mapped_column(String(32), index=True)
    previous_decision: Mapped[str] = mapped_column(String(16))  # proposed | accepted | rejected
    previous_paper_only: Mapped[bool | None] = mapped_column(Boolean)  # None unless previously accepted
    new_decision: Mapped[str] = mapped_column(String(16))  # accepted | rejected
    new_paper_only: Mapped[bool | None] = mapped_column(Boolean)  # None when rejected
    reviewer: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    interface: Mapped[str] = mapped_column(String(8), default="cli")  # cli | web
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class GoldLabel(Base):
    """Blind hand labels. One row per (citation, tagger). Kept apart from the
    review queue: nothing in the pipeline reads this table except evaluation."""

    __tablename__ = "gold_label"
    __table_args__ = (UniqueConstraint("citation_id", "tagger"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    citation_id: Mapped[int] = mapped_column(ForeignKey("citation.id"), index=True)
    tagger: Mapped[str] = mapped_column(String(64))  # "A", "B", or "adjudicated"
    mode_ids: Mapped[list] = mapped_column(JSON, default=list)  # empty list = no failure mode
    taxonomy_version: Mapped[str] = mapped_column(String(16))
    trap: Mapped[str | None] = mapped_column(String(32))  # qmsr | word_boundary | negation | adequate
    notes: Mapped[str | None] = mapped_column(Text)
    labeled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModeEvaluation(Base):
    """Latest gold-set score per mode; trend views gate on this."""

    __tablename__ = "mode_evaluation"
    __table_args__ = (UniqueConstraint("mode_id", "taxonomy_version", "pipeline"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mode_id: Mapped[str] = mapped_column(String(32), index=True)
    taxonomy_version: Mapped[str] = mapped_column(String(16))
    pipeline: Mapped[str] = mapped_column(String(32), default="published")  # rules | llm | published
    tp: Mapped[int] = mapped_column(Integer)
    fp: Mapped[int] = mapped_column(Integer)
    fn: Mapped[int] = mapped_column(Integer)
    precision: Mapped[float | None] = mapped_column(Float)
    recall: Mapped[float | None] = mapped_column(Float)
    support: Mapped[int] = mapped_column(Integer)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ObservationStatAnnual(Base):
    __tablename__ = "observation_stat_annual"
    __table_args__ = (UniqueConstraint("year", "mode_id", "cfr_section", "kind", "taxonomy_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    mode_id: Mapped[str] = mapped_column(String(32), index=True)
    cfr_section: Mapped[str] = mapped_column(String(32))  # "" when no CFR reference
    kind: Mapped[str] = mapped_column(String(8))  # "483" | "wl"
    taxonomy_version: Mapped[str] = mapped_column(String(16))
    citation_count: Mapped[int] = mapped_column(Integer)
    document_count: Mapped[int] = mapped_column(Integer)
    facility_count: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subscriber(Base):
    __tablename__ = "subscriber"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | active | cancelled
    plan: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
