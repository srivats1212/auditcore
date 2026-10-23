"""Content-addressed raw archive plus versioned ``document`` rows.

Rules (PRD section 4):
- hash the raw bytes (SHA-256) before storing anything;
- a known hash for the same source key is a skip;
- a new hash for a known source key is a new version linked by ``supersedes_id``;
- nothing is overwritten, ever. Archive files are written once, by hash.
"""

from __future__ import annotations

import hashlib
import mimetypes
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, IngestionRun, utcnow

Outcome = Literal["new", "versioned", "skipped"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawArchive:
    """Write-once store keyed by hash: ``<root>/<aa>/<bb>/<hash><ext>``."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(self, digest: str, ext: str) -> Path:
        return self.root / digest[:2] / digest[2:4] / f"{digest}{ext}"

    def put(self, data: bytes, media_type: str) -> tuple[str, str]:
        digest = sha256(data)
        ext = mimetypes.guess_extension(media_type.split(";")[0].strip()) or ".bin"
        path = self.path_for(digest, ext)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest, str(path.relative_to(self.root))

    def read(self, rel_path: str) -> bytes:
        return (self.root / rel_path).read_bytes()


@dataclass
class StoreResult:
    outcome: Outcome
    document: Document


def store_document(
    session: Session,
    archive: RawArchive,
    *,
    source: str,
    source_key: str,
    data: bytes,
    media_type: str,
    source_url: str | None = None,
    run: IngestionRun | None = None,
    meta: dict | None = None,
) -> StoreResult:
    digest = sha256(data)

    existing = session.scalars(
        select(Document).where(Document.source_key == source_key, Document.content_hash == digest)
    ).first()
    if existing is not None:
        return StoreResult("skipped", existing)

    current = session.scalars(
        select(Document)
        .where(Document.source_key == source_key)
        .order_by(Document.version.desc())
        .limit(1)
    ).first()

    _, rel_path = archive.put(data, media_type)
    doc = Document(
        source=source,
        source_key=source_key,
        source_url=source_url,
        content_hash=digest,
        raw_path=rel_path,
        media_type=media_type,
        version=(current.version + 1) if current else 1,
        supersedes_id=current.id if current else None,
        is_current=True,
        ingestion_run_id=run.id if run else None,
        meta=meta or {},
    )
    if current is not None:
        current.is_current = False
    session.add(doc)
    session.flush()
    return StoreResult("versioned" if current else "new", doc)


@contextmanager
def ingestion_run(session: Session, source: str) -> Iterator[IngestionRun]:
    """Record a run. Per-item errors go through ``record_error`` and do not
    abort the run; an uncaught exception marks it failed and re-raises."""
    run = IngestionRun(source=source, errors=[])
    session.add(run)
    session.flush()
    try:
        yield run
    except Exception as exc:
        run.status = "failed"
        record_error(run, "run", exc)
        run.finished_at = utcnow()
        session.commit()
        raise
    run.status = "partial" if run.n_errors else "ok"
    run.finished_at = utcnow()
    session.flush()


def record_error(run: IngestionRun, key: str, exc: BaseException | str) -> None:
    run.n_errors += 1
    # Reassign so SQLAlchemy sees the JSON column change.
    run.errors = [*(run.errors or []), {"key": key, "error": str(exc)[:2000]}]


def tally(run: IngestionRun, outcome: Outcome) -> None:
    run.n_seen += 1
    if outcome == "new":
        run.n_new += 1
    elif outcome == "versioned":
        run.n_versioned += 1
    else:
        run.n_skipped += 1
