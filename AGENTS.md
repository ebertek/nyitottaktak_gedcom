# Contributor / agent notes

- Keep the Python CLI dependency-free and compatible with Python 3.10+.
- The upstream project is `ebertek/nyitottaktak_gedcom`. Propose changes in a branch/PR, not by editing deployed copies or committing user GEDCOM files.
- **Never commit real GEDCOM trees, archival search results, local caches, family documents, live-person information, or copied browser cookies.** Test identities must be fictional.
- Upstream search form constraints (from its public `search.js`): `nev` length 2–25 (may include periods); `szul_hely` and `anyja_neve` 2–25 (no periods); birth years 1850–1990; some document types support fewer filters; `too_many` means the result set is not exhaustive.
- Search names as Hungarian surname + given name; prefer the recorded birth name, preserve explicit aliases, and never invent maiden/married names. Use structured GEDCOM `GIVN`/`SURN` as fallback. Keep original birthplace but strip district suffixes for API filtering and place comparisons.
- Results are **identity leads only** and must not imply an individual was a collaborator. Keep negative/ambiguous identity evidence visible.
- Default to preview-only and conservative, sequential HTTP requests. Honor 403/429, cache responses, and never attempt access-control bypass.
- All CI tests must be offline, with synthetic records and mocked network access. Run `python -m unittest discover -s tests -v` and `python -m py_compile nyitottaktak_gedcom.py` for any code changes.
- Nyitott Akták's FAQ distinguishes a 6-os card (including unsuccessful/fictitious recruitment), 1956–1962 B-napló registers, the incomplete January 1990 H-register snapshot, and gradually published B-dossiers (announced through 2027-02-25). Public search absence is inconclusive; never claim the search proves or disproves historical conduct.
- Archive source explanations must be derived from the returned `irattipus`, **not** a person's identity-match score, cover name, or apparent historical role. Preserve prior checkpoints when adding derived report columns; compute them at CSV export.
- The archive updates its online collections over time. HTTP caches/checkpoints are intentionally persistent, so explain `--refresh` when discussing newly published material. No automated live API calls in tests.
- The site's `/api/meta` provides `adatbazis_frissites` (ISO date). Do not infer which records changed from the date alone. The CLI checks it live and stores the last observation under the ignored cache directory.
- Per-person checkpoint `source_updated` is set only when all search responses were live. Old checkpoint entries or cached search responses have unknown freshness. Changing the public dataset must never silently erase prior findings; use `--refresh-stale` or `--refresh` deliberately and keep the dated pre-refresh CSV backup.
- Metadata checks must respect local request pacing/budget and upstream 403/429; tests mock all networking including metadata. `--check-updates` is metadata-only and works without a GEDCOM file.
