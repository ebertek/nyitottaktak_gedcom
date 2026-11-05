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

Built-in mappings cover Szabadka/Subotica, Hódság/Odžaci, Péterréve/Bačko Petrovo Selo and Csák/Čakovci. To extend this across **all populated places with Hungarian names listed by GeoNames**, generate a local index once:

```bash
# Historical Hungary region: HU, RO, SK, RS, UA, HR, AT, SI
# RS includes Vojvodina. Download covers entire present-day countries.
python scripts/build_place_index.py --download

# Or use the full worldwide GeoNames export (large download)
python scripts/build_place_index.py --world --download
```

The index is saved to `.nyitottaktak_cache/hungarian_places.csv` and automatically used by searches and `--rescore-only`. Use `--place-index path/to/index.csv` to select another file. Building the index is the only operation that contacts GeoNames; scans otherwise contact only Nyitott Akták. Geographic IDs and country context prevent ambiguous names from being merged automatically.

Source: [GeoNames downloadable data](https://www.geonames.org/export/) (CC BY 4.0). Its Hungarian-language names are extensive but **not a complete historical gazetteer**. Build a fresh index periodically as its data changes. If you change the index, re-score saved leads with `--rescore-only`; new API search terms require explicitly re-querying selected people (`--refresh`).

Searches are sequential and cached. Defaults are **30 people**, **250 HTTP requests per invocation**, and **1.5 seconds** between uncached requests. `--all` removes the local people and request caps; `--max-people 0` or `--max-requests 0` removes them separately. The minimum delay is 0.5 seconds. Server limits such as HTTP 403/429 and `too_many` remain in effect.

The archive covers selected Hungarian state-security records from **1944–1990** and is not exhaustive. A `6-os karton` can relate to unsuccessful recruitment; B-napló registers and magnetic-tape data are incomplete. B-dossier records are being published progressively. See the archive's [FAQ](https://nyitottaktak.hu/#gyik) and [ÁBTL research guidance](https://www.abtl.hu/kutatas/) before drawing historical conclusions.

## Privacy and development

Keep GEDCOM files, family documents, caches and generated CSVs **out of this public repository**. Only search filters are transmitted to Nyitott Akták when `--execute` is used; report-only operations process files locally.

```bash
python -m py_compile nyitottaktak_gedcom.py
python -m unittest discover -s tests -v
```

Tests use fictional identities and mock HTTP responses.
