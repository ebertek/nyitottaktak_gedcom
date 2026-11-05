# CLI guide

The CLI runs with Python 3.10+ and uses only the Python standard library.

All examples assume you are in the repository root. Use your own local GEDCOM file; never commit it.

## Search

Preview eligible people and proposed filters without archive requests:

```bash
python nyitottaktak_gedcom.py 'Family Tree.ged'
```

Search all eligible people, using broad name variants and a two-second delay:

```bash
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --delay 2
```

Search one or more GEDCOM IDs:

```bash
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --person-id I123 --person-id I456 --verbose
```

Default eligibility: individuals with a usable surname and a known birth year between **1850 and 1975**. Adjust with `--since`, `--until`, or `--include-undated`. People without usable surnames are skipped.

Without `--all`, the local caps are **30 people** and **250 HTTP requests per invocation**; `--max-people 0` and `--max-requests 0` individually remove those caps. Requests are sequential, with a default **1.5-second** delay (minimum **0.5 seconds**). Upstream limits remain in effect.

`--exhaustive` adds broader name-only and accent-folded searches, increasing the number of requests. `--order historical|oldest|gedcom` controls processing order; `historical` is the default.

## Hungarian married names (optional)

An explicit GEDCOM marriage (`HUSB`, `WIFE`, `MARR`) can generate **speculative** Hungarian married-name search variants, including `-né` forms. This never establishes that any form was legally used. Documented `NAME` variants are searched without this option.

```bash
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --include-inferred-married-names \
  --output married_name_matches.csv --delay 2
```

Use a **different `--output`** (hence a different checkpoint) when enabling this option: it changes which searches must be performed. The script labels inferred-name leads in results. See [name matching](matching.md).

## Resume and refresh

A completed person's queries are recorded in the `*.checkpoint.json` next to the output CSV.

- **Resume:** rerun with the same GEDCOM, settings, output and checkpoint. Completed people are skipped.
- **Check source freshness:** `python nyitottaktak_gedcom.py --check-updates`.
- **Revisit stale/unknown-date results:** run with `--execute --all --exhaustive --refresh-stale` and the same search settings.
- **Force new API searches:** use `--refresh`. Before re-querying completed people, the script preserves a dated backup of the candidate CSV.
- **Fresh scan:** use a new `--output` or `--checkpoint`; `--no-resume` discards previous checkpoint progress for the selected run, so preserve existing outputs first.

Changing search-affecting settings, including inferred married names, may make a checkpoint incompatible. Do **not** use `--no-resume` casually against your only copy of an existing scan.

## Local reports

```bash
# Rebuild grouped identity review (no GEDCOM or network)
python nyitottaktak_gedcom.py --review-only

# Recalculate scores in a new CSV using existing candidate rows (offline)
python nyitottaktak_gedcom.py --rescore-only

# Rebuild coverage from the GEDCOM and its matching checkpoint (offline)
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --all --exhaustive --coverage-only
```

`--review-input`, `--review-coverage-input` and `--review-output` select review files. `--output` changes the candidate CSV and derived report/checkpoint paths. See [reports and interpretation](reports.md).

## Birthplace gazetteer

On the **first executed scan**, the tool automatically downloads a local Hungarian-language GeoNames index if absent. The default countries are **HU, RO, SK, RS (including Vojvodina), UA, HR, AT, SI**. Subsequent scans reuse the index without another download.

To prebuild it or prepare an index before running offline reports:

```bash
python scripts/build_place_index.py --download
```

`--world --download` builds a much larger worldwide index instead. `--place-index PATH` selects a custom index. `--no-auto-place-index` skips first-run setup and uses built-in examples when no index exists. An index cannot guarantee every historical name. See [name and birthplace matching](matching.md).

Use `python nyitottaktak_gedcom.py --help` for the complete argument list.
