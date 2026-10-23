# Tagging preview, 2026-10-23

Twenty citations from the ingested corpus, read in full and tagged by hand
against taxonomy v1 using the LLM tagger's instructions
(`src/auditcore/tagging/llm.py`, `SYSTEM_PROMPT`). The purpose is to find
where the taxonomy and prompt break before spending on API calls.

**These are not gold labels.** One tagger, and the tagger could see the
rule-stage output while reading. The gold set needs two blind taggers.

Sample: 6 citations with a rule hit, 10 that mention computerized-systems
vocabulary but had no rule hit, 4 picked as trap text. Citation IDs refer to
the corpus as re-segmented on 2026-10-23.

## Tags

| # | Citation | Facility (source) | Rules | Manual tags | Unsure |
|---|---|---|---|---|---|
| 1 | 347 | [Island Kinetics, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/island-kinetics-inc-dba-covalence-laboratories-726379-07162026) | CS-VAL-001 before fix, none after | none | |
| 2 | 174 | [Curia New York, violation 2](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/curia-new-york-inc-728790-09182026) | CS-EDC-004 | CS-EDC-004, CS-DI-001 | CS-DI-006 |
| 3 | 400 | [Wizcure Pharmaa, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/wizcure-pharmaa-private-limited-726378-06242026) | CS-DI-002 | CS-DI-002, CS-DI-005 | |
| 4 | 531 | [Ava Inc., violation 2](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/ava-inc-721180-04142026) | CS-LAB-001 | CS-LAB-001, CS-DI-004 | CS-AUD-005, CS-DI-003 |
| 5 | 10 | [SnugZ USA, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/snugz-usa-inc-735284-09282026) | CS-DI-003 | CS-DI-003 | |
| 6 | 92 | [RDM Express, violation 2](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/rdm-express-inc-729223-09112026) (food) | CS-DI-005 before fix, none after | none | |
| 7 | 8 | [Baxter, violation 3](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/baxter-international-inc-733231-09292026) | none | none | |
| 8 | 152 | [Unilam, violation 6](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/unilam-co-ltd-738562-09222026) (device) | none | none | |
| 9 | 358 | [Shimoga Chemicals, violation 3](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/shimoga-chemicals-727904-07132026) | none | CS-DI-001 | CS-LAB-001 |
| 10 | 534 | [DO&CO, violation 2](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/doco-aktiengesellschaftdoco-inc-717773-03302026) (food) | none | none | |
| 11 | 72 | [Dreyer's Grand Ice Cream, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/dreyers-grand-ice-cream-inc-727644-09292026) (food) | none | none | |
| 12 | 514 | [Harbin Jixianglong, violation 3](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/harbin-jixianglong-biotech-co-ltd-723330-05012026) | none | none | |
| 13 | 271 | [Nipro Renal Solutions, violation 9](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/nipro-renal-solutions-usa-corporation-732874-07242026) (device) | none | none | |
| 14 | 65 | [Eurofins SF Analytical, violation 2](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/eurofins-sf-analytical-laboratories-inc-736056-10062026) | none | none | CS-DI-001 |
| 15 | 135 | [RC Outsourcing 483, observation 5](https://www.fda.gov/media/195189/download) | none | none | |
| 16 | 419 | [Huons, violation 4](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/huons-co-ltd-724650-06152026) | none | none | |
| 17 | 474 | [Aversa's Italian Bakery, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/aversas-italian-bakery-722904-05202026) (food) | none | none | |
| 18 | 109 | [Victory Medical Center Pharmacy 483, observation 10](https://www.fda.gov/media/195803/download) | none | none | |
| 19 | 286 | [Waves and Wine, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/waves-and-wine-inc-724861-07062026) (animal food) | none | none | |
| 20 | 67 | [Lewis Sausage, violation 1](https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/lewis-sausage-company-inc-717432-08052026) (animal food) | none | none | |

Totals: 8 confident manual tags on 5 citations, 5 unsure. Rules, after the
fixes below, proposed 4 tags and all 4 match a manual tag (precision 4/4 on
this sample, recall 4/8). Before the fixes, rules proposed 6 with 2 false
positives.

## Where it breaks

### 1. Most data-integrity findings are not about computerized systems (decision needed)

Of the 8 confident tags, only citation 4 (FTIR audit trail, HPLC and GC
trial injections) clearly involves a computerized system. The rest are paper
or bench practice: shredded printouts (2), swapped microbial plates and
pre-filled forms (3), retesting into compliance (5), missing raw data (9).
The DI modes and CS-DI-003 define the failure without reference to a
computerized system, so they tag any data-integrity finding. Counted as
"computerized-systems enforcement", that overstates the category.

Options:
- **A.** Restrict the DI and lab modes to electronic or hybrid records (add
  "in an electronic or hybrid record" to each definition). Paper-only DI drops
  out.
- **B.** Keep tagging all DI, add a per-tag `medium` field (electronic, hybrid,
  paper), and show computerized-systems counts as the electronic plus hybrid
  subset.
- **C.** Reposition the product as "data integrity and computerized systems".

Recommendation: B. It keeps the information, consultants care about paper
DI too, and the headline numbers stay honest.

### 2. The corpus filter lets in food and veterinary letters (fixed in the report, decision for ingestion)

Six of 20 citations came from food or animal-food letters (21 CFR 117, 507),
because their subjects also say "CGMP". In the full corpus that is 34 of 135
letters. The report now excludes them by issuing office. Ingestion still
fetches them; filtering on the issuing office (CDER, CBER, CDRH) at ingest
time would avoid fetching them at all.

### 3. "Validation" and "system" are overloaded (rule fixed, prompt needs a line)

Process validation, cleaning validation, water-system qualification (1) and
analytical method validation (7, 12) all use the words the VAL modes rely
on. The CS-VAL-001 rule fired on "unvalidated system" for a water system;
it now requires a computer, software or "computerized system" term. The LLM
prompt should say explicitly that process, cleaning, utility and analytical
method validation are not computerized-system validation.

### 4. The 211.68 heading is not evidence of a computerized system (prompt needs a line)

483 observation 15 opens with the 211.68(a) language, "automatic, mechanical
and electronic equipment", but the finding is temperature mapping of
incubators. CFR 211.68 alone should not raise confidence in a CS mode. The
prompt currently passes the CFR reference to the model with no caveat.

### 5. Word sense (rule fixed)

"The product is fabricated from two or more ingredients" (food labeling, 6)
fired CS-DI-005. The rule now requires "fabricated" to apply to data,
records, results, entries, documents, logs or signatures.

### 6. Segmentation (mostly fixed)

- **Letter tails inside violations (fixed).** Device letters ran the last
  violation into "Please notify this office", the signature and the CC line
  (8, 13), and add-on sections (UDI, misbranding, radiation control) were
  glued onto the previous violation. 12 of 135 letters were affected; now 0.
- **QMSR citations borrowed unrelated CFR sections (fixed).** A heading citing
  only "ISO 13485:2016 Clause 7.4.1" fell back to every section in the text,
  attaching UDI and radiation-product parts. The fallback is now skipped when
  the heading cites an ISO clause.
- **Sub-findings inside one violation (open).** Violations 1 and 9 each hold
  three unrelated sub-findings under their own headings ("Lack of Process
  Validation", "Inadequate Cleaning Validation"...). Under the approved
  counting rule that is still one violation, but a tag on one sub-finding
  attaches to the whole text.
- **Remediation requests are half the text (open, cost).** Everything after
  "In response to this letter, provide:" is FDA's request list. The prompt
  already says to ignore it, but it is roughly 30 to 50 percent of the tokens
  in each citation. Trimming it before tagging would cut input cost and remove
  a false-positive source. It must stay in the stored text for search.
- **Food letters restart numbering per section (out of scope once 2 is decided).**

## Unsure tags

- **4, CS-AUD-005 vs CS-DI-004.** The FTIR audit trail showed no activity on
  days the usage log showed testing. Either the audit trail is incomplete
  (AUD-005) or the records conflict (DI-004). Tagged DI-004 because the
  letter frames it as missing data, but the definitions do not separate the
  two. Suggest an exclude note on AUD-005: "use DI-004 when the gap is shown
  by comparing records".
- **4, CS-DI-003.** A trial injection was repeated and the better result
  reported. LAB-001's exclude note sends retest-after-OOS to DI-003, but
  this is a trial injection, not a retest. Kept LAB-001 only.
- **9, CS-LAB-001.** "An analyst who performed unreported injections" appears
  under a training heading, not as its own finding. A one-clause mention:
  tag or not? The taxonomy has no rule for passing mentions.
- **2, CS-DI-006.** The firm could not say who generated a pH meter printout.
  That is non-attributable, but the record is a paper printout, so it depends
  on decision 1.
- **14, CS-DI-001.** FDA says data records are "unreliable" and photos of
  plates did not exist. Generic data-integrity language without a specific
  failure. Left untagged; the LLM is likely to tag it.

## Prompt changes to make before the first paid run

1. Scope sentence matching decision 1 (for example: "Data-integrity modes apply
   only when the record is electronic or hybrid", or a `medium` field).
2. "Process, cleaning, utility and analytical method validation are not
   computerized-system validation."
3. "A 21 CFR 211.68 citation alone is not evidence of a computerized system."
4. "Passing mentions inside another finding are tagged only if they state a
   deficiency on their own."
5. Trim the remediation-request tail from the text sent to the model.
