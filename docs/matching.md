# How matching works

The tool compares **possible identities** from a private GEDCOM tree with records returned by the public [Nyitott Akták search](https://nyitottaktak.hu/#kereses). It does not establish whether a person participated in state-security activities.

## GEDCOM identity data

The parser reads individual `NAME`, `GIVN`, `SURN`, `BIRT/DATE`, `BIRT/PLAC`, and family links. It uses `FAMC` and family `WIFE`/`CHIL` links to identify a person's mother. `FAMS` and family `HUSB`/`WIFE`/`MARR` support optional married-name inference.

- Hungarian archive queries use **surname followed by given names**. A GEDCOM `Ágnes /Fenyvesi/` becomes `Fenyvesi Ágnes`.
- Multiple explicit `NAME` structures, `TYPE birth`/`TYPE married`, `_AKA` and `_MARNM` can contribute search variants. Identical repeated forms are deduplicated.
- Structured `GIVN`/`SURN` can provide a usable name when the `NAME` value itself lacks a surname.
- Blank or placeholder surnames are excluded to avoid broad first-name-only searches.
- A mother's **documented birth/primary name** is used for `anyja_neve` when inferred married-name searches are enabled. Generated married names never become maternal maiden-name filters.

### Inferred married names

With `--include-inferred-married-names`, a recorded marriage can generate Hungarian variants using the recorded husband's given and family names and the wife's birth name. Examples (fictional):

- `Kőris Béláné` (husband's full name plus `-né`)
- `Kőrisné Fenyvesi Ágnes` (husband's surname plus `-né`, followed by her birth name)
- `Kőris Ágnes` (husband's surname with her given name)
- `Kőris Béláné Fenyvesi Ágnes` (combined form)

The suffix lengthens a final short `a` or `e` (`Béla` → `Béláné`, `Imre` → `Imréné`). Inferred forms are **search hypotheses**, not names known to have been used. The output labels `matched_name_origin` and `searched_name_origin` as `gedcom` or `inferred_married`.

The archive's name filter has a 25-character limit: overlong inferred forms are not silently truncated. Shortened variants with the first given name may be added where applicable. The option can substantially increase the query count.

## Search sequence

For each eligible person and name variant, the script starts with the most specific valid filters (name, birth-year window, birthplace and mother's name). It progressively relaxes birthplace and mother's name, then falls back to year/name or name-only searches. `--exhaustive` adds additional name-only and accent-folded queries even after potential candidates appear.

The API's published input constraints are observed: names **2–25 characters**, birthplaces and mothers' names **2–25 characters**, and year filters **1850–1990**. The default birth-year tolerance is ±2 years. Some archival record types lack birth-date fields, so a matching name can be a weak lead.

Queries are cached; duplicated archive records are consolidated by record GUID for each person. Warnings such as `too_many` and rejected filters appear in coverage/error reporting. A returned set is not automatically exhaustive.

## Place-name normalization

GEDCOM location strings retain their original text in output. For comparison and API queries:

- Administrative suffixes are removed: `Pécs III.` → `Pécs`, `Budapest XI` → `Budapest`.
- Accent/case normalization handles forms such as `MEZÖKÖVESD` and `Mezőkövesd`.
- A local [GeoNames](https://www.geonames.org/export/) index identifies Hungarian exonyms across Hungary, Romania, Slovakia, Serbia (including Vojvodina), Ukraine, Croatia, Austria and Slovenia. For example, `Subotica` and `Szabadka` refer to the same city.
- Verified built-in pairs are available if the full index is not loaded. Country context helps disambiguate shared place names; unknown or ambiguous aliases should not be assumed equivalent.

The first executed scan builds the regional index if missing; see the [CLI guide](cli.md). GeoNames data is CC BY 4.0 and is not a complete historical gazetteer.

## Identity scoring

A record must match the name sufficiently to be considered. The code combines an approximate name score with independent identifiers:

| Evidence | Score contribution |
| --- | ---: |
| Exact normalized name | +45 |
| Name with extra/missing components | +36 |
| Fuzzy name similarity (threshold 0.88) | +23 |
| Exact date of birth | +30 |
| Same birth year only | +20 |
| Mother's exact name | +20 |
| Mother's partial name | +11 |
| Equivalent birthplace | +10 |
| Partially matching birthplace | +5 |

Conflicting dates, mothers and birthplaces reduce the score; missing fields do not count as matching evidence. Scores are limited to **0–100**, with candidate rows retained by default from **35** upward.

Review categories based on the existing heuristic score remain **strong ≥85**, **possible ≥60**, and **weak 35–59**. Separately, the grouped review report computes a **field-evidence classification** that distinguishes exact corroboration, partial agreement, name-only results, missing comparisons, and contradictions independently of the numeric score. See [evidence classifications](reports.md#evidence-based-review-classifications). A score of 100 means high agreement among available fields, **not** confirmation of the person's historical role or of the authenticity of an archive entry.

A name-only hit can score 45 despite having no other useful identifiers. Review original documents and seek independent corroboration. More information is in [reports and interpretation](reports.md).
