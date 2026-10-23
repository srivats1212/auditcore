"""Stage 2: LLM-assisted tagging (PRD section 7).

For each citation the model proposes failure-mode tags from the taxonomy,
each with a verbatim evidence quote. We keep a proposal only if:
  - its mode ID is in the taxonomy (also enforced by the output schema), and
  - its evidence quote actually occurs in the citation text.
Everything the model proposes lands as ``status="proposed"``; nothing is
published until a human accepts it (stage 3).

The taxonomy block is the stable prefix of every request, so it is marked for
prompt caching; only the citation text varies.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..taxonomy.loader import Taxonomy

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You tag FDA enforcement citations (Form 483 observations and warning-letter violations) with computerized-systems failure modes from a fixed taxonomy.

A citation gets a tag only when the citation text states a deficiency that matches the mode's definition. Rules:
- Tag what FDA cited, not what FDA asked the firm to do in its response. Sentences like "Your response should include..." or "We recommend..." are remediation requests; they are not findings.
- A sentence describing an adequate or working control is not a finding.
- Respect each mode's "Not this mode" note. When two modes fit, apply both only if the text supports each one independently.
- Device-quality citations may use either QSR (pre February 2, 2026) or QMSR / ISO 13485 vocabulary. Map both to the same modes.
- Matching words are not enough: "cloudy solution" is not cloud computing, "shared drive" is not a shared login.
- Process validation, cleaning validation, utility or water-system qualification, and analytical method validation are not computerized-system validation. Tag a validation mode only when the thing not validated is software, a computerized system, or its configuration.
- A 21 CFR 211.68 citation alone is not evidence of a computerized system. The heading quotes "automatic, mechanical, or electronic equipment" for calibration and equipment findings too; tag only what the finding itself describes.
- Paper-only data integrity: modes marked "Paper records: yes" also fit findings about paper records. Set paper_only to false only when the citation text states that an electronic or hybrid record is involved (instrument data systems, chromatography or other instrument software, audit trails, spreadsheets, LIMS, electronic batch records, computerized systems, printouts from systems that also store the data). Otherwise set it to true: handwritten forms, logbooks, worksheets, printouts from simple instruments, and findings that do not say what kind of record is involved are all paper_only. Do not infer an electronic record from the type of test alone. For every other mode, paper_only is false.
- Evidence must be copied verbatim from the citation text: the shortest span that shows the deficiency.
- Confidence is your probability that a trained reviewer would accept the tag.
- If no mode applies, return an empty list. Most citations in the corpus are not about computerized systems.
"""


def taxonomy_block(taxonomy: Taxonomy) -> str:
    lines = [f"Taxonomy version {taxonomy.version}."]
    for mode in taxonomy:
        lines.append(f"\n{mode.id} | {taxonomy.categories[mode.category]} | {mode.name}")
        lines.append(f"  Definition: {mode.definition}")
        if mode.exclude:
            lines.append(f"  Not this mode: {mode.exclude}")
        if mode.paper_records:
            lines.append("  Paper records: yes")
    return "\n".join(lines)


def output_schema(taxonomy: Taxonomy) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mode_id": {"type": "string", "enum": sorted(taxonomy.modes)},
                        "evidence": {"type": "string"},
                        "confidence": {"type": "number"},
                        "paper_only": {"type": "boolean"},
                    },
                    "required": ["mode_id", "evidence", "confidence", "paper_only"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tags"],
        "additionalProperties": False,
    }


# USD per million tokens: input, output, cache read, 5-minute cache write
# (1.25x input). Fallback models are listed because a refusal fallback bills
# at their rates: Sonnet 5.5's default fallback is Sonnet 5.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0
    models: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, usage: Any) -> None:
        get = lambda k: int(getattr(usage, k, 0) or 0)  # noqa: E731
        inp, out = get("input_tokens"), get("output_tokens")
        cr, cw = get("cache_read_input_tokens"), get("cache_creation_input_tokens")
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        self.models[model] = self.models.get(model, 0) + 1
        price = PRICES.get(model)
        if price is None:
            self.unpriced_calls += 1
            return
        p_in, p_out, p_cr, p_cw = price
        self.cost_usd += (inp * p_in + out * p_out + cr * p_cr + cw * p_cw) / 1_000_000

    def as_dict(self) -> dict:
        per = self.cost_usd / self.calls if self.calls else 0.0
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "cost_usd": round(self.cost_usd, 4), "cost_per_citation_usd": round(per, 5),
                "models": self.models, "unpriced_calls": self.unpriced_calls}


@dataclass(frozen=True)
class LlmTag:
    mode_id: str
    evidence: str
    confidence: float
    paper_only: bool = False


# Where FDA's request list starts. Everything from here on is what FDA asks
# the firm to provide, not what it cited. Only these unambiguous openers are
# used: "In your response, you stated..." summaries can carry findings.
_REQUEST_TAIL = re.compile(
    r"(?:^|(?<=[.:;])\s+|\n)\s*(?:in response to this letter,?\s+(?:please\s+)?provide|"
    r"in your response to this letter,?\s+(?:please\s+)?provide|your response should include)\b",
    re.IGNORECASE,
)


def strip_request_tail(text: str) -> str:
    """The citation text up to FDA's remediation request list. The stored
    text keeps the full citation for search."""
    m = _REQUEST_TAIL.search(text)
    if m is None or not text[: m.start()].strip():
        return text
    return text[: m.start()].rstrip()


class LlmRefusal(RuntimeError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def evidence_in_text(evidence: str, text: str) -> bool:
    ev = _norm(evidence).strip(" .\"'")
    return bool(ev) and ev in _norm(text)


class LlmTagger:
    def __init__(self, taxonomy: Taxonomy, client: Any | None = None, model: str | None = None, effort: str | None = None):
        settings = get_settings()
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.taxonomy = taxonomy
        self.model = model or settings.llm_model
        self.effort = effort or settings.llm_effort
        self._system = [
            {"type": "text", "text": SYSTEM_PROMPT},
            {"type": "text", "text": taxonomy_block(taxonomy), "cache_control": {"type": "ephemeral"}},
        ]
        self._schema = output_schema(taxonomy)
        self.usage = Usage()
        self._usage_lock = threading.Lock()  # tag() may run in several threads

    def tag(self, citation_text: str, *, doc_kind: str, cfr: str | None) -> list[LlmTag]:
        prompt = (
            f"Document type: {'Form 483 observation' if doc_kind == '483' else 'warning letter violation'}\n"
            f"CFR reference: {cfr or 'none cited'}\n\n"
            f"<citation>\n{citation_text}\n</citation>"
        )
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4096,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=self._system,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": self._schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        # Price by the model that served the request: a refusal fallback may switch it.
        with self._usage_lock:
            self.usage.add(getattr(response, "model", None) or self.model, getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            raise LlmRefusal(f"model declined: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("tagging response truncated at max_tokens")

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        tags: list[LlmTag] = []
        seen: set[str] = set()
        for item in data.get("tags", []):
            mode_id = item.get("mode_id")
            if mode_id not in self.taxonomy.modes or mode_id in seen:
                continue
            if not evidence_in_text(item.get("evidence", ""), citation_text):
                log.info("dropping %s: evidence not found in citation", mode_id)
                continue
            seen.add(mode_id)
            conf = float(item.get("confidence", 0.0))
            paper_only = bool(item.get("paper_only")) and self.taxonomy.modes[mode_id].paper_records
            tags.append(LlmTag(mode_id, item["evidence"], min(max(conf, 0.0), 1.0), paper_only))
        return tags
