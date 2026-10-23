"""Stage 1: high-precision rules pre-tagging (PRD section 7).

A mode fires when one of its taxonomy rule patterns matches inside a
sentence, and that sentence is a finding. Three things make a matching
sentence NOT a finding:

1. Negated finding: "we did not observe shared logins", "no evidence that
   audit trails were disabled".
2. Adequate control: "the audit trail was enabled and reviewed", with no
   deficiency cue in the sentence.
3. Remediation request: warning letters end each violation with "Your
   response should include an assessment of audit trail review ...". That
   names the topic but is FDA asking for a fix, not citing a failure.

Note on negation: in this corpus, deficiency language is itself negative
("audit trail was not reviewed"). So classic NegEx-style "a negation word
precedes the term" logic would discard most true findings. We only discard
when the negation scopes over the observation itself ("did not observe",
"no evidence"), never over the control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..taxonomy.loader import Taxonomy

# A sentence boundary that ignores "21 CFR 211.68(b)." style decimals and "e.g."
_SENTENCE_END = re.compile(r"(?<!\b[a-z])(?<!\d)(?<=[.;!?])\s+(?=[A-Z(\"'])|\n+")

_NEGATED_FINDING = re.compile(
    r"\b(did not (observe|find|identify|note|see)|"
    r"(no|not any) (evidence|indication|instances?|observations?|deficienc\w+|issues?|concerns?) (that|of|were|was|regarding|related)|"
    r"was not observed|were not observed|no (findings?|observations?) (were )?(made|noted|observed)|"
    r"found no|observed no|identified no|noted no)\b",
    re.IGNORECASE,
)

_ADEQUACY = re.compile(
    r"\b(was|were|is|are|appeared|appears)\s+(adequate(ly)?|appropriate(ly)?|acceptable|compliant|"
    r"in place|sufficient|satisfactory|properly (configured|enabled|validated|controlled))\b"
    r"|\b(adequately|appropriately|properly|routinely) (reviewed|controlled|validated|configured|restricted|backed up|enabled)\b"
    r"|\bno (deficiencies|issues|concerns) (were )?(noted|identified|observed)\b",
    re.IGNORECASE,
)

_DEFICIENCY = re.compile(
    r"\b(not|never|no|failed|failure|lack\w*|without|inadequate\w*|insufficient\w*|unable|cannot|could not|"
    r"deficien\w+|uncontrolled|unvalidated|unauthorized|disabled|deleted|overwritten|shared|"
    r"however|but)\b",
    re.IGNORECASE,
)

# Imperative or advisory language FDA uses when requesting remediation.
_REMEDIATION_REQUEST = re.compile(
    r"^\s*(\(?[a-z0-9]{1,3}[.)]\s*)?("
    r"your response should|in response to this letter|please (provide|include|submit)|"
    r"provide (a|an|the|your)|include (a|an|the)|we (recommend|request|strongly recommend)|"
    r"fda (recommends|requests)|you should|a (comprehensive|detailed|thorough) (assessment|evaluation|review)|"
    r"an? (independent|retrospective) (assessment|review|evaluation)|"
    r"(see|refer to) fda'?s guidance)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RuleHit:
    mode_id: str
    evidence: str  # the matched span
    sentence: str
    pattern: str


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]


def sentence_is_finding(sentence: str) -> bool:
    if _REMEDIATION_REQUEST.search(sentence):
        return False
    if _NEGATED_FINDING.search(sentence):
        return False
    if _ADEQUACY.search(sentence):
        # "Audit trails were enabled; however, they were not reviewed" is still a finding.
        stripped = _ADEQUACY.sub(" ", sentence)
        if not _DEFICIENCY.search(stripped):
            return False
    return True


def tag_text(text: str, taxonomy: Taxonomy) -> list[RuleHit]:
    hits: dict[str, RuleHit] = {}
    for sentence in split_sentences(text):
        if not sentence_is_finding(sentence):
            continue
        for mode in taxonomy:
            if mode.id in hits:
                continue
            for pattern in mode.rules:
                m = pattern.search(sentence)
                if m:
                    hits[mode.id] = RuleHit(mode.id, m.group(0), sentence, pattern.pattern)
                    break
    return list(hits.values())
