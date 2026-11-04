# Nyitott Akták × GEDCOM

A dependency-free Python command-line tool for finding **possible identity matches** between a local GEDCOM family tree and the public [Nyitott Akták](https://nyitottaktak.hu/#kereses) archive search API.

**An archival record is not proof that an individual was a communist agent, informant, or collaborator.** Search results are leads for manual source examination. Name matches alone are unreliable, particularly for common Hungarian names.

## Requirements

- Python 3.10 or newer (standard library only)
- A GEDCOM `.ged` or `.gedcom` export on your own computer
- Network connectivity only when running `--execute`

## Usage

```bash
# Preview eligible people, no HTTP requests
python nyitottaktak_gedcom.py 'Family Tree.ged'

# Search a small historical sample
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --since 1900 --until 1950 --max-people 20 --max-requests 100 --delay 2

# Investigate one or more specific GEDCOM identifiers
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --person-id I123 --person-id I456 --verbose --delay 2

# Broader queries and accentless name variants (more requests)
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --exhaustive --max-people 30 --delay 2

# Search EVERY eligible person in a single invocation, with no local request cap
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --all --exhaustive --delay 2

# Also include people without a known birth year (more broad searches)
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --all --include-undated --exhaustive --delay 2
```

The default order prioritizes birth years close to 1930; it is **not** the same as GEDCOM file order. Use `--order gedcom` to preserve the file order, or `--person-id` to select particular relatives directly. By default, people without birth years are excluded; `--include-undated` opts them in. Individuals without usable surnames are always excluded.

### Tracking database updates

The website's `GET /api/meta` returns an `adatbazis_frissites` database update date (for example, `2026-11-04`). The tool checks this endpoint **live at the beginning of every executed search**, then compares the date with the last observation saved locally under `.nyitottaktak_cache/database-meta.json`.

```bash
# Metadata-only check, no GEDCOM file or archival search required
python nyitottaktak_gedcom.py --check-updates

# Recheck previously completed people only if their database date is older
# or their original checkpoint has no verifiable database date.
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --refresh-stale --delay 2
```

The checkpoint stores a `source_updated` date **per completed person**, preserving compatibility with older checkpoints. The summary distinguishes `current`, `older snapshot` and `unknown` entries. A person is recorded as current only if all their search responses came from the live server while the database reported that date. Cached queries from earlier runs and older checkpoint files have **unknown** provenance rather than falsely claiming freshness.

Database changes do **not** automatically invalidate a completed search: ordinary runs preserve existing results and display a warning. `--refresh-stale` opt-in rechecks older/unknown completed entries and bypasses their cached API replies. An ordinary `--refresh` continues to force a complete selected requery. Before replacing checkpointed results, the script copies the previous CSV to a dated `.pre-refresh-...` backup. Interrupted refreshes remain unfinished and can be safely resumed.

An unsuccessful metadata check **does not cause an ordinary scan to discard progress**, but archive freshness cannot then be established; `--refresh-stale` requires valid metadata and refuses to proceed without it. HTTP 403/429 responses from the metadata endpoint stop the run. The metadata check counts as one HTTP request against any local `--max-requests` budget.

**Important:** A changed database date does not say which person or record changed. Even when marked current, a search is limited to the public dataset and the filters used, not a comprehensive archival investigation. Always retain the underlying source documents for verification.

### Resuming long searches

Each successfully searched person is checkpointed immediately to `nyitottaktak_matches.checkpoint.json` (derived from `--output`). The CSV is also rewritten **atomically and cumulatively** after each completed individual. When you run **the same search again**, already completed individuals are skipped and their candidate rows are retained; only unfinished individuals generate new API requests. Cached HTTP replies additionally avoid querying the same URL twice.

```bash
# Search your whole tree in successive batches of up to 250 NEW HTTP requests each
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --exhaustive --max-people 1000 --max-requests 250 --delay 2

# Run the same command again to continue from the checkpoint
python nyitottaktak_gedcom.py 'Family Tree.ged' --execute \
  --exhaustive --max-people 1000 --max-requests 250 --delay 2
```

**Important:** `--max-people` is the size of the selected historical prefix, **not** the number of new people per invocation. To progress beyond the first 30, increase it (e.g., `--max-people 1000`).

The checkpoint verifies the GEDCOM file contents and search-affecting settings. If either changes, start a separate checkpoint with `--checkpoint other-run.json`, or explicitly discard previous checkpoint progress with `--no-resume`. `--refresh` reprocesses selected people against the live API. A first run using this version will back up an existing v4 output CSV as `nyitottaktak_matches.pre-checkpoint.csv`; previously cached API responses are reused to reconstruct completed searches.

Keep the checkpoint and CSV private. Neither contains a verified conclusion about historical conduct.

The default `--max-people` is 30 and `--max-requests` is 250. **These are application safety defaults, not limits from Nyitott Akták**. Set either to `0` to disable just that local limit (e.g. `--max-people 0 --max-requests 0`), or use `--all` to disable both. `--all` still respects the GEDCOM eligibility range (`--since` and `--until`), surname requirements, and whether `--include-undated` is set.

The server may separately return `too_many`, impose access limits, or specify a maximum result count for an individual query. The script no longer **assumes** a 250-result pagination cap when `max_results` is missing; it follows reported `total` and `page_size`. Unlimited local budgets do **not** override server-side protections.

A local cache (`.nyitottaktak_cache/`) prevents duplicate requests on reruns. `--refresh` bypasses it. The minimum delay is 0.5 seconds between uncached requests; **2 seconds or more is advisable for a large tree**. Requests are sequential, never parallel. Respect the archive operator's limits; a 403/429 response stops further searches. A transient network or server error can leave the affected person unfinished, to be retried on the next run.

### GEDCOM fields

- Reads `NAME` in conventional `Given /Surname/` form and supports structured `GIVN`/`SURN` when `NAME` lacks surname delimiters.
- Uses maternal relationships (`FAMC` / `FAM` → `WIFE`) to search a mother's recorded name, normally her birth/maiden name.
- Simplifies birthplaces to the city (`Pécs III., Baranya, Hungary` → `Pécs`; `Budapest XI.` → `Budapest`). Original GEDCOM birthplace is retained in reports.
- Parses full birth dates for stronger confirmation, and supports approximate dates for year-only filters.
- Does not synthesize married names from spouses. Explicit GEDCOM aliases or married names are considered as additional search variants.

### Search API behavior

The site uses `GET /api/search` with `nev`, `szul_tol`, `szul_ig`, `szul_hely`, `anyja_neve`, `page`, `sort` and `dir` parameters. Its frontend validates birth years **1850–1990**, restricts text-field lengths, and rejects periods in birthplace and mother-name filters. The script searches with specific filters first, then relaxes them when useful. `--exhaustive` additionally checks accentless and broader name-based queries.

The API can return `too_many` on excessively broad searches, and some archival record types lack birth dates. The tool tracks these limits but cannot guarantee exhaustive coverage or reliable person identification.

## Archive coverage and interpretation

The distinctions below come from Nyitott Akták's [published FAQ and record-type descriptions](https://nyitottaktak.hu/#gyik) (November 4, 2026), not from an inference by this tool. Its database contains selected **1944–1990** state-security records, **not a complete list of all collaborators**:

| Record type | What it contains | Important limitation |
| --- | --- | --- |
| **6-os karton** | State-security registration card, including identification and recruitment/network details | Cards were also created for unsuccessful or fictitious recruitment. A card alone cannot establish that meaningful cooperation occurred. |
| **B-napló** | A register of archived recruitment dossiers | The archive describes only five surviving registers, all from **1956–1962**. A name-only result requires careful identity verification. |
| **Mágnesszalag / H-adattár** | Surviving electronic registry snapshot representing **January 1990** | Incomplete: people excluded or deceased before 1970 were not recorded, and later entries were also deleted. |
| **B-dosszié** | Recruitment dossier with potentially detailed records | Publication is being expanded progressively through **February 25, 2027**. The primary documents need historical interpretation. |

The FAQ explicitly warns that records were destroyed, individuals outside the formal network were not entered in these registries, and archived allegations can be inaccurate, incomplete, or reflect the state-security agency's own perspective. **A missing result is not proof of absence; a matching result is not proof of cooperation.** Multiple cover names or registry numbers may correspond to one individual.

The output CSV includes `archive_source_summary` and `archive_interpretation_limits` based **only** on `archive_record_type`. These are explanatory source notes, not an individualized classification. They are derived when the CSV is exported, so existing checkpoints gain them without requesting the same archive records again.

**Database updates:** The archive says it is updated continuously, with B-dossier additions scheduled through February 25, 2027. Use `--check-updates` to compare the published database date against the previous local observation. The normal scan also checks automatically; `--refresh-stale` selectively rechecks old or unverified completions. `--refresh` forces re-querying every selected individual.

For additional historical verification, the archive directs researchers to [ÁBTL research](https://www.abtl.hu/kutatas/) and its [records-access portal](https://lar.abtl.hu/ords/r/abtl/sb/home).

## Search coverage audit

A scan can encounter rejected filters, `too_many` responses, or interrupted requests. The candidate CSV alone does not show which relatives were fully searched, so the tool maintains a **second local report**: `nyitottaktak_matches.coverage.csv`. It includes one row per selected eligible GEDCOM person, their search status, number of raw hits/candidates, archive update date, and any search warnings.

| Status | Meaning |
| --- | --- |
| `completed` | All planned requests for this person finished with no reported query limitations. **Not** proof that every relevant archival record was found. |
| `limited` | The person is checkpointed but one or more queries returned `too_many`, a recoverable HTTP 400, or an invalid name variant. The result set may be incomplete. |
| `not_completed` | The person is not checkpointed, because the scan has not reached them or was interrupted/failed. |

Generate or regenerate coverage **without contacting Nyitott Akták**:

```bash
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --all --exhaustive --coverage-only
```

The offline audit compares the GEDCOM and selected search settings with your checkpoint. Use the same `--exhaustive`, `--year-tolerance` and `--min-score` options as the original scan, or the checkpoint configuration check will reject it. `--coverage-only` does not require `--execute`, does not call `/api/meta`, and does not change checkpoints. During an ordinary `--execute` scan, the coverage CSV is updated after each fully completed person. Pass `--coverage-output another-private-file.csv` to change the report path.

A `limited` entry is not re-queried automatically, because a `too_many` query could remain inherently broad. Manually inspect the reported warnings, narrow identity filters or adjust your search strategy, then explicitly refresh the selected individual if needed. Existing checkpoints from earlier script versions may lack old HTTP 400 warnings; the new report cannot reconstruct historical warnings that were never saved.

## Offline candidate review

After your scan (or at any time after the candidate CSV has been saved), generate a **person-level identity review report**, entirely offline:

```bash
# Uses nyitottaktak_matches.csv and, when present, the coverage CSV
python nyitottaktak_gedcom.py --review-only

# Custom private input/output paths, still without a GEDCOM or network calls
python nyitottaktak_gedcom.py --review-only \
  --review-input my_matches.csv \
  --review-coverage-input my_matches.coverage.csv \
  --review-output my_matches.review.csv
```

The script also writes `nyitottaktak_matches.review.csv` automatically after each checkpointed search (alongside the existing candidate and coverage CSVs).

The grouped report has one row per relative represented in the **coverage report**, including those with zero candidate records. If no coverage CSV is available, only relatives who have candidate rows can be included, and their search status is `not_reported`.

Fields include candidate count, distinct archive records, record types, the highest **identity** score, the top-scoring candidate's archive link and identity evidence, search completeness, and explanatory review flags:

- `conflicting_identity_data`: date, mother's name or birthplace contradicts the GEDCOM for the *top candidate*.
- `archive_guid_shared_by_multiple_relatives`: the same archival document has candidate rows for two or more GEDCOM people.
- `incomplete_search`: coverage was capped, rejected, or interrupted.
- `multiple_identity_leads`: multiple potential archival records for one person.
- `needs_additional_identifiers`: a top candidate lacks birth, maternal or birthplace corroboration.
- `no_candidate_leads`: no accepted candidate rows; **not proof of archival absence**.

All flags and `review_order` are **manual identity-verification aids**, not assessments of recruitment or collaboration. Only the best-scoring candidate is used for the top candidate's contradiction flags; conflicts from lower-scoring leads remain in the original detailed matches CSV. Always open the original archival document before making an identity determination.

**Do not upload the private reports, GEDCOM, or archive query cache to this public GitHub repository.** The report is generated from the already saved local CSVs; no network access is required in `--review-only` mode.

## Output

The default report is `nyitottaktak_matches.csv`, with both GEDCOM and archive identity fields, a **heuristic review score**, a rationale, query URL and original document URL. Scores prioritize review; **they are not probabilities or judgments about historical conduct**. The separate source context columns explain document types but never determine whether a particular person cooperated. Errors and incomplete queries are written to `nyitottaktak_matches.errors.txt` when applicable. `--verbose` shows each outgoing filter set and the result count, which is useful for investigating zero-hit queries.

## Privacy

**Never commit real family trees, source certificates, family information, result CSVs, cache files, or API-query logs to this public repository.** GEDCOM files may contain private data about living people. The repository ignores common local output paths and GEDCOM filenames, but always check `git status` before committing.

All GEDCOM parsing and score computation happen locally. Only search filter values are sent to the external archive when `--execute` is specified.

## Development

```bash
python -m unittest discover -s tests -v
python -m py_compile nyitottaktak_gedcom.py
```

Tests use fictional individuals and mock HTTP responses; they do not contact Nyitott Akták. Please keep tests offline.
