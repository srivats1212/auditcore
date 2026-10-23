"""Normalize OCR and PDF-extraction artifacts before anything is tagged.

FDA 483 PDFs routinely come out of extraction as "O B S E R V A T I O N  1",
"comput- erized", ligature glyphs, and page furniture repeated on every page.
Tagging raw text misses most keyword hits, so every document goes through
``clean_text`` first. Each pass is a small, testable function.
"""

from __future__ import annotations

import re
import unicodedata

_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "­": "",  # soft hyphen
    "​": "",  # zero-width space
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    " ": " ",
}

# Runs of 3+ single characters separated by single spaces: "O B S E R V".
# Words inside a run are separated by 2+ spaces, which we keep as one space.
_SPACED_RUN = re.compile(r"(?<![^\s])((?:[A-Za-z0-9§.()\-]\s){2,}[A-Za-z0-9§.()\-])(?![^\s])")

# Hyphenated line breaks: "comput-\nerized" -> "computerized".
_HYPHEN_BREAK = re.compile(r"([a-z])-[ \t]*\n\s*([a-z])")
# Same break after lines were already joined: "comput- erized". Suspended
# hyphens ("pre- and post-") are left alone.
_HYPHEN_INLINE = re.compile(r"\b([a-z]{2,})- (?!(?:and|or|to|through|nor)\b)([a-z]{2,})\b")

# Page furniture typical of FDA Form 483 (FDA 483 header/footer blocks).
_FURNITURE = [
    re.compile(r"^\s*FORM FDA 483.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*PREVIOUS EDITION OBSOLETE.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*INSPECTIONAL OBSERVATIONS\s+PAGE \d+ OF \d+.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Page \d+ of \d+\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*EMPLOYEE\(S\) SIGNATURE.*$", re.IGNORECASE | re.MULTILINE),
]

# Common OCR confusions inside regulatory vocabulary. Applied with word
# boundaries; deliberately short so it cannot invent words.
_OCR_FIXES = [
    (re.compile(r"\b2l\s*CFR\b", re.IGNORECASE), "21 CFR"),
    (re.compile(r"\b21\s*C\s*F\s*R\b", re.IGNORECASE), "21 CFR"),
    (re.compile(r"\bCFR\s*(\d+)\s*\.\s*(\d+)"), r"CFR \1.\2"),
    (re.compile(r"§\s*§"), "§§"),
    (re.compile(r"\baudit\s*-\s*trail", re.IGNORECASE), "audit trail"),
    # Collapsed letter-spacing glues the number on ("OBSERVATION1"), and PDF
    # extraction can run a heading into the previous line. Force a line start.
    (re.compile(r"[ \t]*\bOBSERVATION\s*(\d{1,3})\b"), r"\nOBSERVATION \1"),
]


def normalize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    return text


def collapse_spaced_letters(text: str) -> str:
    def _join(match: re.Match[str]) -> str:
        return match.group(1).replace(" ", "")

    lines = []
    for line in text.split("\n"):
        # Split on 2+ spaces (word gaps in spaced text), collapse each chunk.
        chunks = re.split(r"( {2,})", line)
        lines.append("".join(_SPACED_RUN.sub(_join, c) if not c.isspace() else " " for c in chunks))
    return "\n".join(lines)


def join_hyphenated_breaks(text: str) -> str:
    return _HYPHEN_INLINE.sub(r"\1\2", _HYPHEN_BREAK.sub(r"\1\2", text))


def strip_page_furniture(text: str) -> str:
    for pattern in _FURNITURE:
        text = pattern.sub("", text)
    return text


# Whole-word misreads seen in reading-room 483s. Only words that are not
# themselves English words, so a fix can never change meaning.
_WORD_FIXES = {
    "fnm": "firm", "fum": "firm", "finn": "firm", "fiim": "firm", "fmn": "firm",
    "diug": "drug", "diugs": "drugs", "di11g": "drug", "di11gs": "drugs", "dmg": "drug", "dmgs": "drugs",
    "conti-ol": "control", "conti-ols": "controls", "contiol": "control", "contiols": "controls",
    "instmment": "instrument", "instmments": "instruments", "aheady": "already",
    "detemiination": "determination", "detennination": "determination", "in1plemented": "implemented",
}
_WORD_FIX_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(_WORD_FIXES, key=len, reverse=True)) + r")\b")
# "1" read for "r" inside a lowercase word: perfo1med, Laborato1y, obse1ved.
_ONE_FOR_R = re.compile(r"(?<=[a-z])1(?=[a-z])")
# Missing space after short function words: "ofyour", "ifyou", "ofthe".
_GLUED = re.compile(r"\b(of|if|in|on|to)(your|you|the|this|these|those|their|its)\b")


def apply_ocr_fixes(text: str) -> str:
    for pattern, repl in _OCR_FIXES:
        text = pattern.sub(repl, text)
    text = _ONE_FOR_R.sub("r", text)
    text = _GLUED.sub(r"\1 \2", text)
    return _WORD_FIX_RE.sub(lambda m: _WORD_FIXES[m.group(1)], text)


def unwrap_lines(text: str) -> str:
    """Join hard-wrapped lines inside paragraphs; keep blank lines and
    numbered-item starts ("OBSERVATION 3", "1.", "2)") as breaks."""
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        starts_item = bool(re.match(r"^(OBSERVATION\s+\d+|\d{1,2}[.)]\s|[A-Z][A-Z \-]{6,}$)", stripped))
        if out and out[-1] and not starts_item:
            out[-1] = f"{out[-1]} {stripped}"
        else:
            out.append(stripped)
    return "\n".join(out)


def squeeze_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text: str) -> str:
    text = normalize_unicode(text)
    text = strip_page_furniture(text)
    text = collapse_spaced_letters(text)
    text = join_hyphenated_breaks(text)
    text = apply_ocr_fixes(text)
    text = unwrap_lines(text)
    return squeeze_whitespace(text)
