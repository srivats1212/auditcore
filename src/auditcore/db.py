from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _factory
    if url is not None or _engine is None:
        _engine = create_engine(url or get_settings().database_url, future=True)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def init_db(url: str | None = None) -> Engine:
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    if url is not None or _factory is None:
        get_engine(url)
    assert _factory is not None
    session = _factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
