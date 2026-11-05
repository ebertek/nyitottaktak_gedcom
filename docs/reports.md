# Reports, coverage and archival interpretation

Search results are **identity leads**, not evidence that an individual was recruited, worked as an agent, or cooperated with state security. Confirm identities against original documents and independent records.

## Generated files

With the default `--output results/nyitottaktak_matches.csv`, these files are written under the ignored `results/` directory:

| File | Purpose |
| --- | --- |
| `results/nyitottaktak_matches.csv` | One row per retained person–archive candidate, with matching fields, score, source type and document URL |
| `results/nyitottaktak_matches.review.csv` | One row per GEDCOM person for manual review, incorporating missing or conflicting identity evidence |
| `results/nyitottaktak_matches.coverage.csv` | One row per selected person with search-completion status and warnings |
| `results/nyitottaktak_matches.checkpoint.json` | Completed searches and their saved candidate rows, for safe resumption |
| `results/nyitottaktak_matches.errors.txt` | Reported query errors/limitations, if any |
| `.nyitottaktak_cache/` | Cached API results, observed archive-update metadata, and the optional GeoNames place index |

A custom `--output` sets the report path verbatim; for additional scans use `--output results/married_name_matches.csv`. Old files are **not** moved automatically. [Migration and resume instructions](cli.md#result-files-and-migrating-previous-scans) explain how to relocate a whole checkpoint/report set. Generated reports contain private names and potentially sensitive family relationships: **keep them out of public repositories**.

## Candidate and grouped review reports

The candidate CSV includes names, dates, places, mother, archive GUID, record type, score, evidence and source links. If married-name inference is enabled, `matched_name_origin` and `searched_name_origin` identify whether a name came from GEDCOM or was inferred. Inferred names must be verified separately.

The grouped review CSV summarizes the best-scoring identity lead for each selected person, candidate counts, distinct archive-record counts, record types, search status and flags. Examples of flags:

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

This creates `results/nyitottaktak_matches.rescored.csv` and `results/nyitottaktak_matches.rescored.review.csv`, leaving the original CSV, checkpoint and coverage untouched. The rescored candidate CSV includes previous score and score change.

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
