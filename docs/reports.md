# Reports, coverage and archival interpretation

Search results are **identity leads**, not evidence that an individual was recruited, worked as an agent, or cooperated with state security. Confirm identities against original documents and independent records.

## Generated files

With the default `--output results/nyitottaktak_matches.csv`, these files are written under the ignored `results/` directory:

| File | Purpose |
| --- | --- |
| `results/nyitottaktak_matches.csv` | One row per retained person–archive candidate, with matching fields, score, source type and document URL |
| `results/nyitottaktak_matches.review.csv` | One row per GEDCOM person, with highest-score and best-evidence leads shown separately |
| `results/nyitottaktak_matches.shortlist.csv` | Research queue: best evidence-based candidate per person, omitting name-only and no-lead cases by default |
| `results/nyitottaktak_matches.audit.csv` | Offline score/evidence drift details for rows whose saved evidence differs from current scoring, or cannot be checked |
| `results/nyitottaktak_matches.consistency.csv` | Private candidate-level historical chronology and shared-GUID review prompts |
| `results/nyitottaktak_matches.shared-records.csv` | One private row per archive GUID associated with multiple distinct GEDCOM IDs |
| `results/nyitottaktak_matches.coverage.csv` | One row per selected person with search-completion status and warnings |
| `results/nyitottaktak_matches.checkpoint.json` | Completed searches and their saved candidate rows, for safe resumption |
| `results/nyitottaktak_matches.errors.txt` | Reported query errors/limitations, if any |
| `.nyitottaktak_cache/` | Cached API results, observed archive-update metadata, and the optional GeoNames place index |

A custom `--output` sets the report path verbatim; for additional scans use `--output results/married_name_matches.csv`. Old files are **not** moved automatically. [Migration and resume instructions](cli.md#result-files-and-migrating-previous-scans) explain how to relocate a whole checkpoint/report set. Generated reports contain private names and potentially sensitive family relationships: **keep them out of public repositories**.

## Candidate and grouped review reports

The candidate CSV includes names, dates, places, mother, archive GUID, record type, score, evidence and source links. If married-name inference is enabled, `matched_name_origin` and `searched_name_origin` identify whether a name came from GEDCOM or was inferred. Inferred names must be verified separately.

The grouped review CSV summarizes the best-scoring identity lead for each selected person, candidate counts, distinct archive-record counts, record types, search status and flags.

### Evidence-based review classifications

`top_evidence_classification` labels only the **highest-scoring candidate** for each GEDCOM person, with a deliberately conservative interpretation:

| Classification | Meaning |
| --- | --- |
| `strongly_corroborated` | Two or more **independent exact** identifiers agree (full date of birth, mother and/or birthplace), with no recorded contradictions |
| `partially_corroborated` | At least one identifier agrees; or comparisons are partial (birth year only, partly matching mother/place) |
| `name_only` | The archive record has no birth date, birthplace or mother's name; the matching name alone cannot establish identity |
| `conflicting_identity` | At least one compared identity attribute contradicts the GEDCOM, even when other attributes agree |
| `insufficient_gedcom_data` | The archive supplies identifiers missing from the GEDCOM, so no independent corroboration or contradiction can be established |
| `insufficient_comparable_data` | Archive identifiers are present, but the existing scoring evidence does not establish agreement or contradiction (for example, an unrecognized date); a one-year birth difference alone is not considered corroboration |
| `no_candidate_leads` | Coverage includes this person, but the saved candidate CSV contains no candidate row |

`top_agreeing_identifiers`, `top_conflicting_identifiers` and `top_missing_gedcom_identifiers` list which *fields*, if any, support the classification. The six numeric `candidate_*` evidence columns count the classifications **across all candidate rows for that person**, so strong alternatives are not hidden by a different highest-scoring lead. These are candidate-row counts, not distinct archive records; their sum equals `candidate_count`.

The classification uses the **existing saved `evidence` text**, not score thresholds or mere presence of fields. It does not rescore or modify candidate rows, search coverage or checkpoints. Existing `review_flags`, `review_order`, highest-score selection, and inferred-name provenance remain unchanged. Any disagreement takes precedence over positive identity indicators for that candidate; two partial matches do not become `strongly_corroborated`.

**Even “strongly corroborated” does not establish historical activity, recruitment or cooperation.** A marriage-name inference is still hypothetical even if other identifiers agree. For older CSVs with missing/unrecognized evidence, the classifier falls back to an inconclusive category rather than fabricating an agreement.

To regenerate the enriched report with no GEDCOM and no archive API requests:

```bash
python nyitottaktak_gedcom.py --review-only
```

For an alternate scan use `--review-input results/married_name_matches.csv`. If you want to retain a previous review report unchanged, pass `--review-output results/married_name_matches.evidence.review.csv`.

Examples of flags:

- `conflicting_identity_data`: the top-scoring lead has an identity-data conflict.
- `multiple_identity_leads`: more than one candidate row needs investigation.
- `archive_guid_shared_by_multiple_relatives`: the same archive GUID appears against multiple GEDCOM identities.
- `needs_additional_identifiers`: the top lead lacks important corroborating fields.
- `inferred_married_name`: the top matched name was generated as a search hypothesis.
- `incomplete_search`: queries for this person were not fully completed.

Flags and score order are **review priorities**, not assessments of archival conduct. Conflicting fields in several different records should not be combined into one putative identity.

Rebuild the grouped report offline:

```bash
python nyitottaktak_gedcom.py --review-only
```

## Prioritized research shortlist

The grouped `*.review.csv` now includes **both** the highest numerical score (`top_*`) and an independently selected `best_evidence_*` candidate. The latter uses the identity-evidence classes above: exact independent corroboration is preferred to partial corroboration, missing family-tree details, unresolved comparisons, and conflicts. A name-only match is last. Scores are used only as a **tie-breaker within a comparable evidence class**; documented names are preferred to hypothetical inferred married names at equal corroboration strength.

Each run also writes `*.shortlist.csv`, with one best-evidence lead per GEDCOM relative:

- `P1` — **verify corroboration:** two or more exact independent identifiers match, with no conflicts
- `P2` — **seek second identifier:** one or more exact/partial identifiers agree, but not enough for P1
- `P3` — **enrich family tree:** archived identity details are missing from the GEDCOM
- `P4` — **inspect unresolved evidence:** fields are present, but comparability cannot be established
- `P5` — **resolve identity conflict:** one or more compared identifiers disagree; investigate, rather than assume a match
- `P6` — **defer name-only:** available archive details contain no independent identifiers; **excluded by default**

The `gedcom_missing_identifiers` field lists missing birth date, birthplace and mother's birth name in the GEDCOM, even when an archive record doesn't supply them. `missing_gedcom_for_selected_record` lists the subset that can be compared against the chosen archive lead. Columns also preserve the selected document URL, score, source type, agreement/contradiction, whether it differs from the highest-score lead, optional marriage-name inference provenance, total candidate counts and scan coverage. The report **never combines data from separate archive records to create corroboration**.

To regenerate both reports from saved candidates with no GEDCOM/API access:

```bash
python nyitottaktak_gedcom.py --review-only
```

To update only the shortlist, or include deferred name-only leads:

```bash
python nyitottaktak_gedcom.py --shortlist-only

python nyitottaktak_gedcom.py --shortlist-only --shortlist-include-name-only
```

For an alternate saved scan, use `--review-input results/married_name_matches.csv` with either option. Use `--shortlist-output PATH` to choose the destination. Existing input candidates and checkpoints are not modified; `--review-only` also regenerates its existing grouped review CSV. The shortlist is a **workflow queue, not an identity confidence probability** or a finding of involvement in state-security activities. Name-only and zero-lead relatives remain visible in the original candidate and grouped review reports.

## Auditing outdated saved evidence

Saved CSV candidate rows retain their **original** scores and scoring explanations until explicitly rescored. Changes to place aliases or matching rules can make those explanations stale; a misleading `birthplace differs` may still be present even when the current scorer recognizes two historical place names as equivalent.

All offline `--review-only`, `--shortlist-only`, and `--quality-only` operations now compare each saved candidate row to the current scorer before reporting. If any numerical score, evidence text or resulting evidence class changes, they **print a warning before generating reports**; old source data is **never silently modified** and derived reports still reflect the supplied input. Scoring checks are performed against the saved matched GEDCOM name and identifying fields, without loading the private family tree or contacting the archive. Rows with missing or malformed fields are flagged as **unverifiable**, not incorrectly declared current.

For a detailed private diff or a separate rescored data set:

```bash
# Read-only audit: summary in terminal; details in ignored *.audit.csv
python nyitottaktak_gedcom.py --audit-only

# Writes *.rescored.csv, *.rescored.review.csv and *.rescored.shortlist.csv;
# original candidates, checkpoint and coverage remain unchanged.
python nyitottaktak_gedcom.py --rescore-only
```

The audit CSV includes private GEDCOM and archive identifiers, before/after scores, evidence text and classifications; it only lists changed or unverifiable rows. **Never commit or share the detailed CSV.** An audit with no detected drift is a check of **saved scoring consistency only**—not archive completeness, not verification against the latest online archive, not confirmation that old filtered-out candidates can be recovered, and not proof of someone's historical role.

If you are using another candidate set, pass `--review-input PATH`; optional `--audit-output PATH` chooses the audit destination. Rescoring an existing `*.rescored.csv` creates a *new*, more deeply suffixed result rather than overwriting the original input.

## Historical chronology and shared archival GUIDs

`--consistency-only` performs **two conservative offline audits** of the currently saved candidate CSV. No GEDCOM or network access is required:

```bash
python nyitottaktak_gedcom.py --consistency-only
# To check the corrected identity scores:
python nyitottaktak_gedcom.py --consistency-only \
  --review-input results/nyitottaktak_matches.rescored.csv
```

The two private output files are:

- `*.consistency.csv`: **one row per flagged candidate row**, containing the GEDCOM ID, archive GUID/document URL, dates, source scope, age at the beginning of that scope, chronology flags, count of other GEDCOM IDs sharing the same archive GUID, and an explanation.
- `*.shared-records.csv`: **one row per shared non-empty archive GUID**, showing the unique GEDCOM IDs involved, known 4-digit birth years and their spread. Repeated queries for the *same* GEDCOM ID do not create a false shared-record alert. Blank GUIDs never become a common identity.

Chronology prompts include:

| Flag | Review meaning |
| --- | --- |
| `older_birth_cohort_review` | The person would have been **65 or older at the beginning of the documented reference scope**; warrants human checking, but is absolutely *not* an impossibility |
| `born_after_archive_scope` | The GEDCOM birth year falls after the archival reference period; check dating, scope and individual identity |
| `birth_year_discrepancy` | The GEDCOM and archival birth years are at least two years apart; inspect original birth records |

The broad reference period is **1944–1990**, while B-napló uses **1956–1962**, as described in the project's archive-source guidance. These are **historical collection/reference ranges, not known dates of recruitment, activity or an individual's appearance in the archive**. A person born in 1853, 1866 or 1877 might legitimately appear as a retrospective reference. The age threshold is a workflow heuristic, not an assertion of suspicious conduct or a likelihood of being a particular individual. `ABT`/other approximate GEDCOM birth years remain approximate; unknown or ambiguous 2-digit years are not expanded into guessed centuries. A one-year birth-year discrepancy does not raise the dedicated two-year flag, but the matching evidence still remains visible separately.

A shared GUID **does not prove** two GEDCOM people are the same person, nor that the archive is wrong. One document may refer to several people; a family tree may also contain duplicates. Compare the original document, people and years independently. An ID shared among individuals with different birth years is simply a useful investigative clue.

The grouped `*.review.csv` now appends `top_chronology_flags`, `best_evidence_chronology_flags`, `candidate_chronology_review_count`, `candidate_shared_archive_guid_count` and `best_evidence_archive_guid_shared_people`. The prioritized shortlist appends corresponding selected-lead and per-person alert fields. **These fields do not change identity scoring, evidence ranking, review ordering, candidate retention or checkpoints.**

The audits are produced automatically by `--review-only`, `--shortlist-only`, `--rescore-only` and completed executed scans, or on demand using `--consistency-only`. `--consistency-output PATH` and `--shared-records-output PATH` customize the destinations; output collisions with candidate, checkpoint and standard report files are rejected in advance. These reports are **private**; never commit or share identifying detail CSVs.

## Coverage is not archival completeness

`*.coverage.csv` distinguishes:

| Status | Meaning |
| --- | --- |
| `completed` | All configured queries for this person finished without reported limitations |
| `limited` | One or more queries were too broad, rejected or otherwise flagged |
| `not_completed` | The person has not been fully processed in the selected scope |

**Completed does not mean the archive contains all relevant historical records**, and a person without hits cannot be assumed absent from surviving materials.

```bash
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --all --exhaustive --coverage-only
```

The offline coverage command uses the GEDCOM and checkpoint. Match the original scan configuration when generating it.

## Offline quality and corroboration summary

To see which archival document types provide usable identifying details **in the saved candidate sample**:

```bash
python nyitottaktak_gedcom.py --quality-only

# Analyze an alternate candidate CSV without touching the original
python nyitottaktak_gedcom.py --quality-only \
  --review-input results/married_name_matches.csv
```

The command needs no GEDCOM, network access or API cache. It writes `results/nyitottaktak_matches.quality.csv` by default (or `--quality-output PATH`) with **aggregate counts only**; it never writes people's names, GEDCOM IDs or archive GUIDs.

The report groups by `archive_record_type` and includes a total across the retained sample:

- **Source completeness**: percentages of distinct archive records with a birth date, birthplace, mother's name and full vs partial dates. Records with the same source and archive GUID count once; missing GUIDs stay separate.
- **Candidate evidence**: counts of name-only rows, rows supported by one or two independent identity attributes, conflicting identity evidence and score bands. These metrics count candidate rows because one archive record may be considered against multiple GEDCOM identities.
- **Sample size**: number of candidate rows, distinct GEDCOM IDs and distinct archive records, plus average identity score.

**Crucial limitation:** these are records **already returned and retained** by the search and minimum score threshold, not a representative sample of the archive. A low field-completeness percentage in this CSV does **not** establish the completeness of the full archival source. Name-only rows count records without any archive-side birth date, birthplace or mother field; they are not evidence of someone's conduct.

## Rescoring existing leads

After upgrading the matching rules or preparing an updated local place index, recalculate scores without contacting the archive:

```bash
python nyitottaktak_gedcom.py --rescore-only
```

This creates `results/nyitottaktak_matches.rescored.csv`, `results/nyitottaktak_matches.rescored.review.csv`, and `results/nyitottaktak_matches.rescored.shortlist.csv`, leaving the original CSV, checkpoint and coverage untouched. The rescored candidate CSV includes previous score and score change.

**Rescoring cannot find additional records**, repeat incomplete queries, or restore results originally excluded by the match-score threshold. A fresh archive search may be needed for new names, changed place filters or newly published records. See the [CLI guide](cli.md) for checkpoint and refresh options.

## How to interpret source types

Nyitott Akták exposes selected Hungarian state-security material dating roughly from **1944–1990**, with substantial gaps and ongoing publication. A search result is not equivalent to proof of involvement.

- **6-os karton**: registration cards can include unsuccessful or fictitious recruitment attempts. Do not infer cooperation from a card alone.
- **B-napló**: surviving recruitment-dossier registers (described by the archive for 1956–1962) provide registry references, not a determination of actual activity.
- **B-dosszié**: dossier content must be examined in context, including provenance and contradictory material.
- **Mágnesszalag / H-adattár**: incomplete historical registry data; missing or deleted records limit what can be concluded.

Consult the [Nyitott Akták FAQ](https://nyitottaktak.hu/#gyik) and [ÁBTL research guidance](https://www.abtl.hu/kutatas/) before drawing conclusions. The script's `archive_source_summary` and `archive_interpretation_limits` fields describe the **document type**, not an individual's conduct.

## Privacy and development

GEDCOM trees may contain living people. Archive filters (names and optional identity fields) are transmitted to Nyitott Akták only on executed searches; a first executed scan may also download the public GeoNames place dataset. Preview and report-only modes do not send private identity data.

Never commit GEDCOM files, results, checkpoints, caches or family documents. Automated tests use fictional identities and mocked network requests:

```bash
python -m py_compile nyitottaktak_gedcom.py
python -m unittest discover -s tests -v
```

See [name matching](matching.md) for what the scores actually measure.
