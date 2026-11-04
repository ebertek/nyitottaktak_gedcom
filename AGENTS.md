# Contributor / agent notes

- Keep the Python CLI dependency-free and compatible with Python 3.10+.
- The upstream project is `ebertek/nyitottaktak_gedcom`. Propose changes in a branch/PR, not by editing deployed copies or committing user GEDCOM files.
- **Never commit real GEDCOM trees, archival search results, local caches, family documents, live-person information, or copied browser cookies.** Test identities must be fictional.
- Upstream search form constraints (from its public `search.js`): `nev` length 2–25 (may include periods); `szul_hely` and `anyja_neve` 2–25 (no periods); birth years 1850–1990; some document types support fewer filters; `too_many` means the result set is not exhaustive.
- Search names as Hungarian surname + given name; prefer the recorded birth name, preserve explicit aliases, and never invent maiden/married names. Use structured GEDCOM `GIVN`/`SURN` as fallback. Keep original birthplace but strip district suffixes for API filtering and place comparisons.
- Results are **identity leads only** and must not imply an individual was a collaborator. Keep negative/ambiguous identity evidence visible.
- Default to preview-only and conservative, sequential HTTP requests. Honor 403/429, cache responses, and never attempt access-control bypass.
- All CI tests must be offline, with synthetic records and mocked network access. Run `python -m unittest discover -s tests -v` and `python -m py_compile nyitottaktak_gedcom.py` for any code changes.
