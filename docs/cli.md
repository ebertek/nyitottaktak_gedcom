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
  --output results/married_name_matches.csv --delay 2
```

Use a **different `--output`** (hence a different checkpoint) when enabling this option: it changes which searches must be performed. The script labels inferred-name leads in results. See [name matching](matching.md).

## Result files and migrating previous scans

New scans save all derived files under the ignored **`results/`** directory by default:

```text
results/
  nyitottaktak_matches.csv
  nyitottaktak_matches.review.csv
  nyitottaktak_matches.coverage.csv
  nyitottaktak_matches.checkpoint.json
```

Custom `--output` paths are used **exactly as provided**. To keep another scan organized, specify `--output results/married_name_matches.csv`. Root-level CSVs and checkpoint files are also ignored for backward compatibility.

**Existing files are not moved automatically.** To migrate a previous scan, stop any running scan, ensure no destination files conflict, and move its candidate CSV **together with** its same-prefix checkpoint, review, coverage, error and backup files. For example, on Linux:

```bash
mkdir -p results
# Run the commands only for existing prefixes; answer overwrite prompts carefully.
mv -i -- nyitottaktak_matches* results/
mv -i -- married_name_matches* results/
```

After moving, resume using the corresponding `--output results/FILE.csv`; otherwise an old custom path will still write in the root. When no new default report exists, offline review, quality, rescore and coverage commands automatically recognize the old root-level default. New executed scans deliberately use `results/` rather than silently resuming or moving old checkpoints, and print a migration notice when appropriate.

## Resume and refresh

A completed person's queries are recorded in the `*.checkpoint.json` next to the output CSV.

- **Resume:** rerun with the same GEDCOM, settings, output and checkpoint. Completed people are skipped. After moving files, use their new `results/` path.
- **Check source freshness:** `python nyitottaktak_gedcom.py --check-updates`.
- **Revisit stale/unknown-date results:** run with `--execute --all --exhaustive --refresh-stale` and the same search settings.
- **Force new API searches:** use `--refresh`. Before re-querying completed people, the script preserves a dated backup of the candidate CSV.
- **Fresh scan:** use a new `--output` or `--checkpoint`; `--no-resume` discards previous checkpoint progress for the selected run, so preserve existing outputs first.

Changing search-affecting settings, including inferred married names, may make a checkpoint incompatible. Do **not** use `--no-resume` casually against your only copy of an existing scan.

## Local reports

```bash
# Rebuild grouped identity review (no GEDCOM or network)
python nyitottaktak_gedcom.py --review-only

# Check old scores AND explanations against current matching rules
python nyitottaktak_gedcom.py --audit-only

# Check historical age plausibility and GUIDs shared by GEDCOM people.
# Also generates evidence-aware collision aggregate and private triage CSVs.
python nyitottaktak_gedcom.py --consistency-only \
  --review-input results/nyitottaktak_matches.rescored.csv

# Recalculate saved candidates and rebuild the review/shortlist (offline)
python nyitottaktak_gedcom.py --rescore-only

# Summarize field availability and identity corroboration per archive source
python nyitottaktak_gedcom.py --quality-only

# Prioritize research tasks from existing candidates (no GEDCOM or archive API)
python nyitottaktak_gedcom.py --shortlist-only

# Rebuild coverage from the GEDCOM and its matching checkpoint (offline)
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --all --exhaustive --coverage-only
```

`--review-input`, `--review-coverage-input` and `--review-output` select review files. `--output` changes the candidate CSV and derived report/checkpoint paths. The consistency command produces four CSVs entirely offline: `*.consistency.csv` (private candidate-level chronology/GUID flags), `*.shared-records.csv` (private shared-GUID IDs), `*.collision-summary.csv` (aggregate source/evidence-class collision counts with no personal identifiers), and `*.collision-triage.csv` (private one-row-per-shared-GUID evidence profiles). They are generated automatically for grouped reviews, shortlists and rescored candidates too. Use `--consistency-output`, `--shared-records-output`, `--collision-summary-output`, and `--collision-triage-output` for custom paths; findings never reject a lead automatically. The research shortlist additionally exposes P5 conflicts by birth date, mother's name and birthplace (selected record separately from all-candidate conflict counts). The quality command writes a separate aggregate-only CSV; use `--review-input` and `--quality-output` to select its input/output. `--audit-only` writes a private `*.audit.csv` containing only changed/unverifiable candidate rows; `--audit-output` selects its destination. **Every** `--review-only`, `--shortlist-only`, or `--quality-only` command now checks the saved evidence against the current score function, and warns about score changes, evidence-text changes, and altered evidence classes. These warnings do **not** rewrite original candidates or alter the derived classification: use `--rescore-only` when notified. This creates a separate `*.rescored.csv` and automatically rebuilds its grouped review **and prioritized shortlist** while preserving the original candidates, checkpoint, and coverage. `--shortlist-include-name-only` includes deferred leads; `--shortlist-output` selects a custom shortlist destination. For an alternate saved scan use `--review-input PATH`; for a rescored input use `--review-coverage-input` if the original coverage file should be reused. See [reports and interpretation](reports.md).

## Birthplace gazetteer

On the **first executed scan**, the tool automatically downloads a local Hungarian-language GeoNames index if absent. The default countries are **HU, RO, SK, RS (including Vojvodina), UA, HR, AT, SI**. Subsequent scans reuse the index without another download.

To prebuild it or prepare an index before running offline reports:

```bash
python scripts/build_place_index.py --download
```

`--world --download` builds a much larger worldwide index instead. `--place-index PATH` selects a custom index. `--no-auto-place-index` skips first-run setup and uses built-in examples when no index exists. An index cannot guarantee every historical name. See [name and birthplace matching](matching.md).

Use `python nyitottaktak_gedcom.py --help` for the complete argument list.
