# auditcore

Citation-level intelligence on FDA enforcement of computerized systems in GxP
environments: what the agency is citing, how fast it is escalating, and how to
fix it.

This is the MVP skeleton: ingestion, a versioned document store, a
65-mode failure-mode taxonomy, three-stage tagging (rules, LLM, human review),
gold-set evaluation that gates what is published, trend analytics, digest
tooling, and a small web UI.

## Operating rules

- Runs on the builder's own machine and infrastructure. No employer resources,
  devices, accounts, or networks.
- Inputs are public FDA documents only. Remediation content is public
  regulatory knowledge and general professional expertise. No employer
  materials, client specifics, or internal documents.
- No em dashes in user-facing copy (`auditcore lint` enforces this).

## Stack

Python 3.11+, Postgres, SQLAlchemy, FastAPI with server-rendered Jinja
templates, pdfplumber, Claude via the Anthropic SDK for stage-2 tagging. The
test suite runs on SQLite; production targets Postgres (full-text search uses
Postgres `to_tsvector`).

## Setup

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
createdb auditcore
export AUDITCORE_DATABASE_URL=postgresql+psycopg://localhost/auditcore
export AUDITCORE_ARCHIVE_DIR=data/archive          # raw files, write-once by SHA-256
export FDA_DASHBOARD_USER=... FDA_DASHBOARD_KEY=... # free FDA Data Dashboard key
export ANTHROPIC_API_KEY=...                        # only for `process --llm`
auditcore init
pytest
```

## Workflow

```sh
# 1. Ingest (every run is idempotent and logged in ingestion_run)
auditcore ingest dashboard --product-type Drugs     # inspections, compliance actions
auditcore ingest wl warning_letters.xlsx            # index export from the FDA Warning Letters page, then each letter
auditcore ingest 483                                # OII reading room listing; archives every PDF on sight
auditcore ingest 483-files pdfs/*.pdf --manifest manifest.csv   # PDFs saved by hand
auditcore ingest runs

# 2. Segment and propose tags
auditcore process            # rules only
auditcore process --llm      # rules + Claude proposals with verbatim evidence spans

# 3. Human review: nothing is counted until accepted here
auditcore review --reviewer <you>

# 4. Gold set (300 citations, double-blind)
auditcore gold sample --n 300 --out gold_blind.csv   # text only, no pipeline output
auditcore gold import tagger_a.csv --tagger A
auditcore gold import tagger_b.csv --tagger B
auditcore gold agreement                             # Cohen's kappa per mode
auditcore gold adjudicate --disagreements disputes.csv
auditcore gold adjudicate --resolutions resolved.csv
auditcore gold evaluate                              # precision and recall per mode, per pipeline

# 5. Analytics, content, site
auditcore stats --escalation
auditcore remediation todo        # top 20 modes and which still need a pattern
auditcore remediation check
auditcore digest draft 2026-10    # data sections filled in, editor placeholders for the rest
auditcore digest publish content/digest/2026-10.md
auditcore lint
auditcore serve
```

## How the pieces map to the PRD

| PRD | Where |
|---|---|
| 4 Ingestion, idempotency, versioning, raw archive, run log | `ingest/store.py`, `ingest/*.py` |
| 4 OCR cleanup | `text/ocr_cleanup.py` |
| 5 Data model | `models.py` |
| 6 Taxonomy v1 (65 modes, 12 categories) | `taxonomy/taxonomy_v1.yaml` |
| 7 Rules pre-tagging (CFR, word boundaries, negation, adequacy, remediation requests) | `text/cfr.py`, `tagging/rules.py` |
| 7 LLM tagging with evidence spans | `tagging/llm.py` |
| 7 Human review | `tagging/review.py`, `auditcore review` |
| 7 Gold set, precision/recall, 0.85 gate | `evaluation/gold.py` |
| 8 Annual stats, CFR breakdown, escalation, methodology notes | `analytics/stats.py` |
| 9 Remediation patterns | `remediation/patterns.py`, `content/remediation/TEMPLATE.md` |
| 10 Digest | `digest/build.py`, `content/digest/` |
| 11 Web UI | `web/` |

## Design decisions worth knowing

- **Citation is the unit.** One observation x one CFR reference. Lettered
  sub-items split into separate citations and inherit the lead sentence's CFR
  reference. Segmentation is deterministic so gold labels stay attached to the
  same rows; the LLM tags citations but never re-segments them.
- **483 observations and warning-letter violations share one `observation`
  table** (`kind` = `483` or `wl`) so a citation has a single parent. This
  departs from the PRD's separate `observation_483` table.
- **Negation is scoped to the observation, not the control.** Deficiency
  language in this corpus is itself negative ("audit trail was not reviewed"),
  so classic NegEx would drop real findings. Rules drop a sentence only when
  it negates the finding ("did not observe", "no evidence that"), describes an
  adequate control with no deficiency cue, or is FDA's remediation request
  ("Your response should include...").
- **QSR and QMSR.** Part 820 citations dated on or after February 2, 2026 are
  QMSR; both vocabularies map to the same modes, and the gold sampler
  oversamples QMSR text.
- **The gate.** A mode appears in trend views, search tags, the home page, and
  the digest only when its gold-set precision is at least
  `AUDITCORE_PRECISION_BAR` (0.85) with at least 3 gold positives. Everything
  else shows as "in review".
- **Counting.** Only human-accepted tags on current document versions count.
- **LLM.** Claude (`claude-opus-5-5`, effort `medium`) with JSON-schema output
  constrained to taxonomy IDs, the taxonomy as a cached system-prompt prefix,
  and server-side refusal fallback enabled. A proposed tag is dropped unless
  its evidence quote appears in the citation text.

## Not done yet

- **Taxonomy examples are illustrative.** Each mode has paraphrased example
  patterns, not paraphrases of specific published citations. Replace them
  during curation, and treat rule patterns as a starting point to tune against
  the gold set.
- **Remediation patterns are not written.** Only the template exists. These
  are the practitioner-written moat and should come from you.
- **Live FDA endpoints are untested.** The sandbox this was built in could not
  reach fda.gov. Parsers are tested against synthetic fixtures; expect to
  adjust column names and HTML selectors on first real run. The Dashboard
  field names follow its published columns but have not been verified with a
  live key.
- **Image-only 483 PDFs** are flagged `needs_ocr` in `document.meta`; no OCR
  engine is wired in yet (Tesseract via `ocrmypdf` is the obvious choice).
- **No migrations.** `auditcore init` uses `create_all`. Add Alembic before the
  schema changes under real data.
- **Payments and email sending.** `/subscribe` stores an address;
  `digest publish` writes email-ready HTML. Billing and delivery are open
  decisions (PRD section 14).
- **Demand validation (PRD section 13)** has not happened. The PRD's own rule
  is to run the five consultant conversations before investing further in
  taxonomy depth.
