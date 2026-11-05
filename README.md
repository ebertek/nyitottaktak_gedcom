# Nyitott Akták × GEDCOM

Find possible identity matches between a **local GEDCOM family tree** and the Hungarian [Nyitott Akták archive](https://nyitottaktak.hu/#kereses). The tool is a dependency-free Python CLI that searches the public API, saves progress, and prepares CSVs for manual review.

**A matching record is an identity lead—not proof that someone was an agent or cooperated with state security.**

## Requirements

- Python 3.10+
- A local `.ged` or `.gedcom` file
- Internet access for archive searches (preview and report-only modes work offline)

## Quick start

```bash
# Preview eligible relatives without contacting the archive
python nyitottaktak_gedcom.py 'Family Tree.ged'

# Search all eligible relatives; resume automatically if interrupted
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --delay 2

# Review the saved results without network access or a GEDCOM file
python nyitottaktak_gedcom.py --review-only

# Apply current identity-scoring rules to saved candidates, entirely offline
python nyitottaktak_gedcom.py --rescore-only
```

By default, the tool searches people born in **1850–1975** who have a usable surname and birth year. Use `--since` and `--until` to adjust the range, or `--include-undated` to include people with unknown birth years. People without surnames are skipped.

## Other useful commands

```bash
# Search particular GEDCOM IDs
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --person-id I123 --person-id I456 --verbose

# Check the archive's published database update date
python nyitottaktak_gedcom.py --check-updates

# Recheck completed searches with outdated or unknown database dates
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --refresh-stale --delay 2

# Rebuild the coverage report from the local GEDCOM and checkpoint
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --all --exhaustive --coverage-only
```

`--exhaustive` tries broader and accent-free name searches. `--order gedcom` uses file order instead of the default historical priority. `--refresh` forces fresh searches for selected people.

Use `python nyitottaktak_gedcom.py --help` for all flags.

### Optional Hungarian married-name searches

Use `--include-inferred-married-names` to search **possible**, not documented, Hungarian married forms from explicit GEDCOM `HUSB`/`WIFE` relationships with a `MARR` event. This supports forms based on the husband's full name, the husband's surname with `-né`, and the wife's given/birth names. Explicit `NAME` aliases still work without the flag; inferred names are **never treated as verified legal names** or used for the `anyja_neve` (mother's birth name) filter. Long inferred names that exceed the API's 25-character limit are not silently truncated.

```bash
# Use a separate output/checkpoint: inferred names change search coverage.
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --include-inferred-married-names \
  --output married_name_matches.csv --delay 2
```

The CSV includes `matched_name_origin` and `searched_name_origin` (`gedcom` or `inferred_married`), and the grouped review flags inferred-name leads. The option can greatly increase the number of requests, and a marriage record alone does **not** establish which married form was actually used. Regular scan checkpoints remain compatible with the default (flag-off) behavior; enabling the flag requires a different checkpoint or `--no-resume` and may requery previously completed people.

## Reports and progress

Files are written beside the command unless another output path is specified:

| File | Purpose |
| --- | --- |
| `nyitottaktak_matches.csv` | Individual archival identity leads, match evidence, source types and document links |
| `nyitottaktak_matches.coverage.csv` | Per-person search status: `completed`, `limited` or `not_completed` |
| `nyitottaktak_matches.review.csv` | One row per relative for identity verification, including conflicts and missing details |
| `nyitottaktak_matches.checkpoint.json` | Saved progress for resuming a scan |
| `nyitottaktak_matches.errors.txt` | Query errors and search limitations, when present |
| `.nyitottaktak_cache/` | Cached API responses and database update metadata |

A scan checkpoints each processed relative. **Rerun the same search command to resume** without re-querying completed people. Use matching search settings when generating offline coverage. The archive database date is checked on executed runs; refreshing old results is opt-in. Previous candidate CSVs are backed up when refreshing.

`completed` means the configured queries finished without reported limitations—not that every historical record was found. `limited` can indicate broad (`too_many`) or rejected queries; `not_completed` means the person has not been fully processed.

`--rescore-only` writes `nyitottaktak_matches.rescored.csv` and `nyitottaktak_matches.rescored.review.csv`. It does **not** contact the archive, read a GEDCOM, or modify the original matches, checkpoint, or coverage. The rescored CSV shows previous and new scores. It can only rescore **existing candidate rows**, not recover records excluded by the original score threshold.

## Search behavior

The script uses names, birth dates, birthplaces and mothers' names from GEDCOM records. It prefers birth names, supports structured `GIVN`/`SURN` and explicit aliases, and simplifies district-level birthplaces (e.g. `Budapest XI` → `Budapest`). Hungarian accent variants and verified historical names are normalized; the original text remains in reports. It does not invent married names.

### Hungarian names for places

On its first **`--execute` scan**, the script automatically downloads and builds a Hungarian place-name index from [GeoNames](https://www.geonames.org/export/) (CC BY 4.0) if one is not already cached. The default covers Hungary, Romania, Slovakia, Serbia (**including Vojvodina**), Ukraine, Croatia, Austria and Slovenia. It uses geographic IDs and context to avoid merging distinct towns that share a name.

The index is saved as `.nyitottaktak_cache/hungarian_places.csv` and reused by subsequent scans and local rescoring. Offline commands (`--review-only`, `--rescore-only`, `--coverage-only`, preview) never download GeoNames data. To build it in advance for offline rescoring, or to rebuild it after deleting the existing index:

```bash
python scripts/build_place_index.py --download

# Optional: much larger worldwide index
python scripts/build_place_index.py --world --download
```

Use `--place-index path/to/index.csv` for a custom file, or `--no-auto-place-index` to skip the initial download and use built-in aliases only when no index exists. If automatic setup fails, the scan stops with an explanation instead of silently reducing geographic coverage. An existing index is never downloaded again automatically.

Historical aliases such as Szabadka/Subotica, Hódság/Odžaci, Péterréve/Bačko Petrovo Selo and Csák/Čakovci are included in the built-in fallback. GeoNames contains many more localities, but is **not an exhaustive historical gazetteer**. Re-scoring saved leads after an index update requires `--rescore-only`; discovering new records through different search terms requires an explicit `--refresh` scan.

Searches are sequential and cached. Defaults are **30 people**, **250 HTTP requests per invocation**, and **1.5 seconds** between uncached requests. `--all` removes the local people and request caps; `--max-people 0` or `--max-requests 0` removes them separately. The minimum delay is 0.5 seconds. Server limits such as HTTP 403/429 and `too_many` remain in effect.

The archive covers selected Hungarian state-security records from **1944–1990** and is not exhaustive. A `6-os karton` can relate to unsuccessful recruitment; B-napló registers and magnetic-tape data are incomplete. B-dossier records are being published progressively. See the archive's [FAQ](https://nyitottaktak.hu/#gyik) and [ÁBTL research guidance](https://www.abtl.hu/kutatas/) before drawing historical conclusions.

## Privacy and development

Keep GEDCOM files, family documents, caches and generated CSVs **out of this public repository**. Only search filters are transmitted to Nyitott Akták when `--execute` is used; report-only operations process files locally.

```bash
python -m py_compile nyitottaktak_gedcom.py
python -m unittest discover -s tests -v
```

Tests use fictional identities and mock HTTP responses.
