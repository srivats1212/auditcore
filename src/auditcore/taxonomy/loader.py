from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FailureModeTaxonomy

_ID = re.compile(r"^CS-[A-Z0-9]{2,4}-\d{3}$")


@dataclass(frozen=True)
class Mode:
    id: str
    category: str
    name: str
    definition: str
    exclude: str
    cfr: tuple[str, ...]
    related: tuple[str, ...]
    rules: tuple[re.Pattern[str], ...]
    examples: tuple[str, ...]
    # Definition also fits paper-only findings; those get the paper_only flag.
    paper_records: bool = False


@dataclass
class Taxonomy:
    version: str
    categories: dict[str, str]
    modes: dict[str, Mode] = field(default_factory=dict)

    def __iter__(self):
        return iter(self.modes.values())

    def __len__(self) -> int:
        return len(self.modes)


class TaxonomyError(ValueError):
    pass


def _compile(pattern: str, mode_id: str) -> re.Pattern[str]:
    try:
        return re.compile(rf"\b(?:{pattern})", re.IGNORECASE)
    except re.error as exc:
        raise TaxonomyError(f"{mode_id}: bad rule {pattern!r}: {exc}") from exc


def parse_taxonomy(raw: dict) -> Taxonomy:
    tax = Taxonomy(version=str(raw["version"]), categories=dict(raw["categories"]))
    for entry in raw["modes"]:
        mode_id = entry["id"]
        if not _ID.match(mode_id):
            raise TaxonomyError(f"bad mode id {mode_id!r}")
        if mode_id in tax.modes:
            raise TaxonomyError(f"duplicate mode id {mode_id}")
        if entry["category"] not in tax.categories:
            raise TaxonomyError(f"{mode_id}: unknown category {entry['category']}")
        tax.modes[mode_id] = Mode(
            id=mode_id,
            category=entry["category"],
            name=entry["name"].strip(),
            definition=entry["definition"].strip(),
            exclude=(entry.get("exclude") or "").strip(),
            cfr=tuple(entry.get("cfr") or ()),
            related=tuple(entry.get("related") or ()),
            rules=tuple(_compile(p, mode_id) for p in entry.get("rules") or ()),
            examples=tuple(entry.get("examples") or ()),
            paper_records=bool(entry.get("paper_records", False)),
        )
    for mode in tax:
        for rel in mode.related:
            if rel not in tax.modes:
                raise TaxonomyError(f"{mode.id}: related mode {rel} does not exist")
    return tax


def load_taxonomy(path: str | Path | None = None) -> Taxonomy:
    if path is None:
        return _default_taxonomy()
    return parse_taxonomy(yaml.safe_load(Path(path).read_text()))


@lru_cache(maxsize=1)
def _default_taxonomy() -> Taxonomy:
    text = resources.files("auditcore.taxonomy").joinpath("taxonomy_v1.yaml").read_text()
    return parse_taxonomy(yaml.safe_load(text))


def sync_taxonomy(session: Session, tax: Taxonomy) -> int:
    """Insert taxonomy rows for this version. Existing (mode, version) rows are
    left alone: a published version is immutable."""
    existing = set(
        session.scalars(
            select(FailureModeTaxonomy.mode_id).where(FailureModeTaxonomy.taxonomy_version == tax.version)
        )
    )
    added = 0
    for mode in tax:
        if mode.id in existing:
            continue
        session.add(
            FailureModeTaxonomy(
                mode_id=mode.id,
                taxonomy_version=tax.version,
                category=tax.categories[mode.category],
                name=mode.name,
                definition=mode.definition,
                examples=list(mode.examples),
                cfr=list(mode.cfr),
                related=list(mode.related),
            )
        )
        added += 1
    session.flush()
    return added
