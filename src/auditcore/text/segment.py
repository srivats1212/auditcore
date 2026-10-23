"""Split cleaned documents into observations, and observations into citations.

Segmentation is deterministic on purpose: gold labels attach to citation rows,
so the same text must always split the same way. The LLM stage tags
citations; it never re-segments them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .cfr import CfrRef, extract_cfr


@dataclass
class Segment:
    number: int
    text: str


_483_HEADER = re.compile(r"(?im)^\s*OBSERVATION\s+(\d{1,3})\b[.:]?\s*")

# Warning-letter violations are numbered paragraphs, usually "1. Your firm failed ...".
_WL_ITEM = re.compile(r"(?m)^\s*(\d{1,2})\.\s+(?=\S)")
_WL_START_HINT = re.compile(
    r"(?i)\b(significant violations|violations? (?:include|are as follows)|"
    r"observed (?:the following|significant)|cgmp violations|violations? of)\b"
)
_WL_STOP = re.compile(
    r"(?im)^\s*(data integrity remediation|cgmp consultant recommended|conclusion|"
    r"repeat (?:violations|observations)|quality systems?|responsibilities as a contractor|"
    r"within fifteen working days|this letter (?:notifies|is not intended))\b"
)


def split_483(text: str) -> list[Segment]:
    matches = list(_483_HEADER.finditer(text))
    segments = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            segments.append(Segment(int(m.group(1)), body))
    return segments


def split_warning_letter(text: str) -> list[Segment]:
    hint = _WL_START_HINT.search(text)
    region_start = hint.start() if hint else 0
    stop = _WL_STOP.search(text, region_start)
    region = text[region_start : stop.start() if stop else len(text)]

    items = list(_WL_ITEM.finditer(region))
    segments: list[Segment] = []
    expected = 1
    for i, m in enumerate(items):
        n = int(m.group(1))
        if n != expected:  # numbering broke: a list inside a violation, not a new violation
            continue
        nxt = next((x for x in items[i + 1 :] if int(x.group(1)) == expected + 1), None)
        end = nxt.start() if nxt else len(region)
        body = region[m.end() : end].strip()
        if body:
            segments.append(Segment(n, body))
        expected += 1
    return segments


# Lettered sub-items inside one observation: "A. ...", "a) ...", "(1) ...".
_SUBITEM = re.compile(r"(?m)(?:^|\n)\s*(?:[A-Za-z][.)]|\(\d{1,2}\)|\(?[ivx]{1,4}\))\s+(?=[A-Z])")


@dataclass
class CitationDraft:
    ordinal: int
    text: str
    cfr: CfrRef | None


def split_citations(observation_text: str) -> list[CitationDraft]:
    """One citation per (sub-item, CFR reference). CFR references in the lead
    sentence apply to every sub-item. An observation with no CFR reference is
    still one citation (most 483 observations cite no CFR section)."""
    subs = [m.start() for m in _SUBITEM.finditer(observation_text)]
    if subs:
        lead = observation_text[: subs[0]].strip()
        bounds = subs + [len(observation_text)]
        parts = [observation_text[bounds[i] : bounds[i + 1]].strip() for i in range(len(subs))]
    else:
        lead, parts = "", [observation_text.strip()]

    lead_refs = extract_cfr(lead) if lead else []
    drafts: list[CitationDraft] = []
    for part in parts:
        body = f"{lead} {part}".strip() if lead else part
        refs = extract_cfr(part) or lead_refs or extract_cfr(body)
        for ref in refs or [None]:
            drafts.append(CitationDraft(len(drafts), body, ref))
    return drafts
