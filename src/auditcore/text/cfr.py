"""CFR reference extraction and normalization.

Handles the forms seen in warning letters and 483s:
  "21 CFR 211.68(b)", "21 C.F.R. § 211.194(a)(4)", "21 CFR 211.68 and 211.100",
  "§§ 211.188 and 211.192", "21 CFR Part 11", "21 CFR 820.70(i)".

Regime matters because QMSR replaced the QSR on February 2, 2026: 820.x
citations before that date are QSR, after it they are QMSR (which incorporates
ISO 13485:2016 by reference, so post-QMSR letters often cite "ISO 13485 clause
4.1.6" via 820.10). Both vocabularies must map to the same failure modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

QMSR_EFFECTIVE = date(2026, 2, 2)

_PARA = r"(?:\([a-z0-9]{1,4}\))*"
_SECTION = rf"\d{{1,4}}\.\d{{1,4}}{_PARA}"

# Anchored form: "21 CFR" / "21 C.F.R." then optional "§"/"§§"/"Part", then a list.
_ANCHORED = re.compile(
    rf"21\s*C\.?\s*F\.?\s*R\.?\s*(?:§§?|sections?|parts?)?\s*"
    rf"(?P<list>(?:{_SECTION}|\d{{1,4}}(?:\s*,?\s*subpart\s+[A-Z])?)"
    rf"(?:\s*(?:,|and|or|&)\s*(?:§§?\s*)?(?:{_SECTION}))*)",
    re.IGNORECASE,
)
# Bare section-sign form: "§ 211.192", "§§ 211.188 and 211.192".
_SECTION_SIGN = re.compile(
    rf"§§?\s*(?P<list>{_SECTION}(?:\s*(?:,|and|or|&)\s*(?:§\s*)?{_SECTION})*)"
)
_ONE_SECTION = re.compile(rf"(?P<sec>\d{{1,4}}\.\d{{1,4}})(?P<para>{_PARA})")
_PART_ONLY = re.compile(r"^(?P<part>\d{1,4})\b")
_ISO_13485 = re.compile(r"ISO\s*13485(?::2016)?(?:,?\s*(?:clause|section)\s*(?P<clause>\d+(?:\.\d+)*))?", re.I)

# Parts we care about for computerized systems, with their regime label.
PART_REGIME = {
    "11": "Part11",
    "58": "GLP",
    "210": "cGMP",
    "211": "cGMP",
    "212": "cGMP",
    "600": "cGMP",
    "606": "cGMP",
    "1271": "cGMP",
    "312": "GCP",
    "803": "MDR",
    "820": "QSR",  # resolved to QMSR by date, see regime_for()
}


@dataclass(frozen=True)
class CfrRef:
    part: str
    section: str | None  # "211.68" or None for "Part 11"
    paragraph: str | None  # "(b)(1)"
    start: int
    end: int

    @property
    def canonical(self) -> str:
        if self.section is None:
            return f"21 CFR Part {self.part}"
        return f"21 CFR {self.section}{self.paragraph or ''}"


def regime_for(part: str, on: date | None) -> str | None:
    regime = PART_REGIME.get(part)
    if part == "820" and on is not None and on >= QMSR_EFFECTIVE:
        return "QMSR"
    return regime


def _expand(list_text: str, base: int, start: int, end: int) -> list[CfrRef]:
    refs = [
        CfrRef(m.group("sec").split(".")[0], m.group("sec"), m.group("para") or None, start, end)
        for m in _ONE_SECTION.finditer(list_text)
    ]
    if not refs:
        part = _PART_ONLY.match(list_text.strip())
        if part:
            refs.append(CfrRef(part.group("part"), None, None, start, end))
    return refs


def extract_cfr(text: str) -> list[CfrRef]:
    found: list[CfrRef] = []
    covered: list[tuple[int, int]] = []
    for m in _ANCHORED.finditer(text):
        found.extend(_expand(m.group("list"), m.start("list"), m.start(), m.end()))
        covered.append((m.start(), m.end()))
    for m in _SECTION_SIGN.finditer(text):
        if any(s <= m.start() < e for s, e in covered):
            continue
        found.extend(_expand(m.group("list"), m.start("list"), m.start(), m.end()))
    # Deduplicate by canonical form, keep first occurrence.
    seen: set[str] = set()
    unique = []
    for ref in sorted(found, key=lambda r: r.start):
        if ref.canonical not in seen:
            seen.add(ref.canonical)
            unique.append(ref)
    return unique


def extract_iso13485_clauses(text: str) -> list[str]:
    return [m.group("clause") for m in _ISO_13485.finditer(text) if m.group("clause")]
