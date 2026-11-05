# Nyitott Akták × GEDCOM

Find possible identity matches between a **local GEDCOM family tree** and the Hungarian [Nyitott Akták archive](https://nyitottaktak.hu/#kereses).

A dependency-free **Python 3.10+** CLI with resumable searches, historical place-name matching, and CSV reports for manual review.

## Quick start

```bash
# Preview without contacting the archive
python nyitottaktak_gedcom.py 'Family Tree.ged'

# Search all eligible people (resumes from its checkpoint)
python nyitottaktak_gedcom.py 'Family Tree.ged' \
  --execute --all --exhaustive --delay 2

# Review saved leads offline
python nyitottaktak_gedcom.py --review-only
```

By default, eligible people have a surname and a birth year from **1850–1975**. Reports and checkpoints go in the ignored `results/` directory. The first executed scan prepares a local Hungarian place-name index from GeoNames.

## Documentation

- **[CLI guide](docs/cli.md)** — search options, inferred Hungarian married names, resuming, refreshing, and place-index setup.
- **[Matching](docs/matching.md)** — GEDCOM parsing, name and birthplace variants, queries, and identity scoring.
- **[Reports and interpretation](docs/reports.md)** — CSV outputs, coverage, offline rescoring, archival source types, and privacy.

Run `python nyitottaktak_gedcom.py --help` for all options.

> **Important:** A matching record is an **identity lead**, not proof that a person was an agent or cooperated with state security. Missing results do not establish absence from archival records.

**Privacy:** Keep GEDCOM files, caches, checkpoints, and generated reports out of this public repository.
