"""Split cleaned documents into observations, and observations into citations.

Segmentation is deterministic on purpose: gold labels attach to citation rows,
so the same text must always split the same way. The LLM stage tags
citations; it never re-segments them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .cfr import CfrRef, extract_cfr

ISO_CLAUSE = re.compile(r"ISO\s*13485", re.I)


@dataclass
class Segment:
    number: int
    text: str


_483_HEADER = re.compile(r"(?im)^\s*OBSERVATION\s+(\d{1,3})\b[.:]?\s*")

# Warning-letter violations are numbered paragraphs, usually "1. Your firm failed ...".
_WL_ITEM = re.compile(r"(?m)^\s*(\d{1,2})\.\s+(?=\S)")
# API letters (ICH Q7) say "deviations" where finished-drug letters say "violations".
_WL_START_HINT = re.compile(
    r"(?i)\b(significant (?:violations|deviations)|(?:violations?|deviations?) (?:include|are as follows)|"
    r"(?:violations?|deviations?) (?:were|was) (?:observed|identified|documented)|"
    r"observed (?:the following|significant)|cgmp (?:violations|deviations)|violations? of)\b"
)
# Sections that follow the numbered violations. Device and food letters add
# separately titled violation groups (UDI, misbranding, radiation control) and
# end with "Please notify this office..." and a signature block.
_WL_STOP = re.compile(
    r"(?im)^\s*(data integrity remediation|cgmp consultant recommended|conclusion|"
    r"repeat (?:violations|observations)|quality systems?|responsibilities as a contractor|"
    r"within fifteen working days|this letter (?:notifies|is not intended)|"
    r"please notify this office|sincerely\b|unique device identification violations?|"
    r"misbranding\b|electronic product radiation control|medical device reporting|"
    r"adulterat\w+ violations?|unapproved new drugs?\b|misbranded drugs?\b)"
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
    segments = _numbered_items(text, hint.start() if hint else 0)
    if not segments and hint is not None:
        segments = _numbered_items(text, 0)  # hint matched after the list; scan the whole letter
    return segments


def _numbered_items(text: str, region_start: int) -> list[Segment]:
    # Stop headings only count after the first violation: some letters say
    # "This letter notifies you..." in the introduction.
    first = next((m for m in _WL_ITEM.finditer(text, region_start) if m.group(1) == "1"), None)
    if first is None:
        return []
    region_start = first.start()
    stop = _WL_STOP.search(text, first.end())
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


# The cited sections sit in the violation's heading sentence, which ends at
# the first ")." or the first sentence break.
_LEAD_END = re.compile(r"\)\.(?=\s|$)|(?<=[a-z0-9])\.\s+(?=[A-Z])|\n")


def lead_sentence(text: str) -> str:
    m = _LEAD_END.search(text)
    return text[: m.end()] if m else text


def split_citations(observation_text: str) -> list[CitationDraft]:
    """One citation per (sub-item, cited CFR section).

    The cited sections are the ones in the heading sentence ("Your firm failed
    to ... (21 CFR 211.68(b))."). Sections mentioned later in the discussion,
    or in boilerplate such as "21 CFR parts 210 and 211", are not citations.
    Only when the heading cites nothing do we fall back to the whole text.
    Lettered sub-items inherit the heading's sections. An observation with no
    CFR reference is still one citation (most 483 observations cite none)."""
    text = observation_text.strip()
    subs = [m.start() for m in _SUBITEM.finditer(text)]
    if subs and subs[0] > 0:
        lead = text[: subs[0]].strip()
        bounds = subs + [len(text)]
        parts = [text[bounds[i] : bounds[i + 1]].strip() for i in range(len(subs))]
    else:
        lead, parts = "", [text]

    head = lead_sentence(lead or text)
    heading_refs = extract_cfr(head)
    if not heading_refs and not ISO_CLAUSE.search(head):
        # QMSR headings cite an ISO 13485 clause instead of a CFR section; then
        # sections elsewhere in the text are unrelated and must not be borrowed.
        heading_refs = extract_cfr(lead or text)
    drafts: list[CitationDraft] = []
    for part in parts:
        body = f"{lead} {part}".strip() if lead else part
        refs = (extract_cfr(lead_sentence(part)) if lead else []) or heading_refs
        for ref in refs or [None]:
            drafts.append(CitationDraft(len(drafts), body, ref))
    return drafts
