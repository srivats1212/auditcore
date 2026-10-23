"""House style for user-facing copy: no em dashes (PRD sections 10 and 12).

Use commas, colons, or periods instead. Checked on every remediation pattern,
digest issue, and web template before publishing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# U+2014 em dash, U+2015 horizontal bar, and the ASCII stand-ins "--" / " - "
# used as a sentence dash between words.
_EM_DASH = re.compile(r"[—―]|(?<=\w) ?-- ?(?=\w)|(?<=\w) - (?=\w)")


@dataclass(frozen=True)
class LintIssue:
    where: str
    line: int
    snippet: str


def find_em_dashes(text: str, where: str = "<text>") -> list[LintIssue]:
    issues = []
    in_code = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for m in _EM_DASH.finditer(line):
            lo = max(0, m.start() - 30)
            issues.append(LintIssue(where, n, line[lo : m.end() + 30].strip()))
    return issues


def lint_paths(paths: list[Path]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for p in paths:
        issues.extend(find_em_dashes(p.read_text(encoding="utf-8"), str(p)))
    return issues


class CopyStyleError(ValueError):
    def __init__(self, issues: list[LintIssue]):
        self.issues = issues
        detail = "; ".join(f"{i.where}:{i.line}: {i.snippet!r}" for i in issues[:10])
        super().__init__(f"em dash in user-facing copy (use a comma, colon, or period): {detail}")


def assert_clean(text: str, where: str) -> None:
    issues = find_em_dashes(text, where)
    if issues:
        raise CopyStyleError(issues)
