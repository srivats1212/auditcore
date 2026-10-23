"""Practitioner-written remediation patterns (PRD section 9).

One Markdown file per failure mode in ``content/remediation/<MODE_ID>.md``
with YAML front matter. Only ``status: published`` patterns are shown.
Every pattern must contain the five required sections and pass the copy
lint. Content rule: public regulatory knowledge and general professional
expertise only. No employer materials, client specifics, or internal
documents; the ``sources`` front-matter field lists the public references
the pattern relies on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import markdown
import yaml

from ..copy_lint import find_em_dashes

REQUIRED_SECTIONS = (
    "What the finding means",
    "Why FDA cares",
    "Fix steps",
    "Evidence an inspector expects",
    "The common remediation mistake",
)

_FRONT = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


@dataclass
class Pattern:
    mode_id: str
    title: str
    status: str
    author: str | None
    sources: list[str]
    body_md: str
    path: Path
    problems: list[str] = field(default_factory=list)

    @property
    def html(self) -> str:
        return markdown.markdown(self.body_md, extensions=["extra"])

    @property
    def publishable(self) -> bool:
        return self.status == "published" and not self.problems


def parse_pattern(path: Path) -> Pattern:
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"{path}: missing YAML front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    p = Pattern(
        mode_id=meta.get("mode_id") or path.stem,
        title=meta.get("title") or path.stem,
        status=meta.get("status", "draft"),
        author=meta.get("author"),
        sources=list(meta.get("sources") or []),
        body_md=body,
        path=path,
    )
    headings = {h.strip().lower() for h in re.findall(r"(?m)^##\s+(.+)$", body)}
    for s in REQUIRED_SECTIONS:
        if s.lower() not in headings:
            p.problems.append(f"missing section: {s}")
    if not p.sources:
        p.problems.append("no public sources listed")
    for issue in find_em_dashes(text, str(path)):
        p.problems.append(f"em dash at line {issue.line}")
    return p


def load_patterns(root: Path) -> dict[str, Pattern]:
    out = {}
    for path in sorted(root.glob("CS-*.md")):
        p = parse_pattern(path)
        out[p.mode_id] = p
    return out
