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
```

The default order prioritizes birth years close to 1930; it is **not** the same as GEDCOM file order. Use `--order gedcom` to preserve the file order, or `--person-id` to select particular relatives directly. By default, people without birth years are excluded; `--include-undated` opts them in. Individuals without usable surnames are always excluded.

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

The default `--max-people` is 30 and `--max-requests` is 250. A local cache (`.nyitottaktak_cache/`) prevents duplicate requests on reruns. `--refresh` bypasses it. The minimum delay is 0.5 seconds between uncached requests; use a longer delay for large trees. Respect the archive operator's limits and stop on access-control or rate-limit responses.

### GEDCOM fields

- Reads `NAME` in conventional `Given /Surname/` form and supports structured `GIVN`/`SURN` when `NAME` lacks surname delimiters.
- Uses maternal relationships (`FAMC` / `FAM` → `WIFE`) to search a mother's recorded name, normally her birth/maiden name.
- Simplifies birthplaces to the city (`Pécs III., Baranya, Hungary` → `Pécs`; `Budapest XI.` → `Budapest`). Original GEDCOM birthplace is retained in reports.
- Parses full birth dates for stronger confirmation, and supports approximate dates for year-only filters.
- Does not synthesize married names from spouses. Explicit GEDCOM aliases or married names are considered as additional search variants.

### Search API behavior

The site uses `GET /api/search` with `nev`, `szul_tol`, `szul_ig`, `szul_hely`, `anyja_neve`, `page`, `sort` and `dir` parameters. Its frontend validates birth years **1850–1990**, restricts text-field lengths, and rejects periods in birthplace and mother-name filters. The script searches with specific filters first, then relaxes them when useful. `--exhaustive` additionally checks accentless and broader name-based queries.

The API can return `too_many` on excessively broad searches, and some archival record types lack birth dates. The tool tracks these limits but cannot guarantee exhaustive coverage or reliable person identification.

## Output

The default report is `nyitottaktak_matches.csv`, with both GEDCOM and archive identity fields, a **heuristic review score**, a rationale, query URL and original document URL. Scores prioritize review; **they are not probabilities or judgments about historical conduct**. Errors and incomplete queries are written to `nyitottaktak_matches.errors.txt` when applicable. `--verbose` shows each outgoing filter set and the result count, which is useful for investigating zero-hit queries.

## Privacy

**Never commit real family trees, source certificates, family information, result CSVs, cache files, or API-query logs to this public repository.** GEDCOM files may contain private data about living people. The repository ignores common local output paths and GEDCOM filenames, but always check `git status` before committing.

All GEDCOM parsing and score computation happen locally. Only search filter values are sent to the external archive when `--execute` is specified.

## Development

```bash
python -m unittest discover -s tests -v
python -m py_compile nyitottaktak_gedcom.py
```

Tests use fictional individuals and mock HTTP responses; they do not contact Nyitott Akták. Please keep tests offline.
