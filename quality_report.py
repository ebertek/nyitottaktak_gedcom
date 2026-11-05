"""Offline, aggregate quality metrics for archived identity candidates.

This module describes the *saved candidate sample*, not the full archive.
No GEDCOM data or names are ever written to the aggregate report.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path


FIELDS = [
    "archive_record_type",
    "candidate_rows",
    "distinct_gedcom_people",
    "distinct_archive_records",
    "archive_name_present",
    "archive_birth_present",
    "archive_birth_full_date",
    "archive_birth_partial_date",
    "archive_birth_place_present",
    "archive_mother_present",
    "archive_birth_pct",
    "archive_birth_place_pct",
    "archive_mother_pct",
    "candidate_name_only",
    "candidate_with_1_plus_corroboration",
    "candidate_with_2_plus_corroborations",
    "candidate_with_conflicting_identifiers",
    "scores_0_34",
    "scores_35_59",
    "scores_60_84",
    "scores_85_100",
    "average_identity_score",
]

ARCHIVE_FIELDS = (
    "archive_name",
    "archive_birth",
    "archive_birth_place",
    "archive_mother",
)

# Archive dates appear as YYYY.MM.DD; retain nonstandard formats as partial,
# rather than assuming they are confirmed full dates.
FULL_DATE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}\.?")

# Evidence is already a part of candidate CSVs produced by the main scorer.
CONFLICT = ("dob conflict", "birth-year conflict", "mother name differs",
            "birthplace differs")


def _present(value: object) -> bool:
    return bool(str(value or "").strip())


def _pct(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}" if denominator else "0.0"


def _archive_identity(row: dict[str, str], index: int) -> tuple[str, str]:
    """Use stable archive IDs, never conflate missing IDs or different sources."""
    guid = row.get("archive_guid", "").strip()
    return ("guid", guid) if guid else ("row", str(index))


def _corroborations(evidence: str) -> int:
    """Count independent identity attributes, not how many phrases occur."""
    found = evidence.casefold()
    return sum((
        "exact dob" in found or "birth year matches" in found or
        "birth years differ by 1" in found,
        "mother exact" in found or "mother partial" in found,
        "birthplace exact" in found or "birthplace partial" in found,
    ))


def _merge_archive_fields(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    """Union available fields across duplicate references to one archive GUID.

    This measures whether a field is present, not which conflicting value is
    correct; only an actual source-document inspection can establish that.
    """
    merged = dict(left)
    for field in ARCHIVE_FIELDS:
        if not _present(merged.get(field)) and _present(right.get(field)):
            merged[field] = right[field]
    old_birth = (merged.get("archive_birth") or "").strip()
    new_birth = (right.get("archive_birth") or "").strip()
    if FULL_DATE.fullmatch(new_birth) and not FULL_DATE.fullmatch(old_birth):
        merged["archive_birth"] = new_birth
    return merged


def quality_rows(candidates: list[dict[str, str]]) -> list[dict[str, object]]:
    """Aggregate per-source evidence completeness in an already filtered CSV.

    Distinct archive record coverage uses (record type, GUID) when available,
    whereas score and corroboration distributions count candidate rows.
    """
    grouped: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
    for index, row in enumerate(candidates):
        kind = str(row.get("archive_record_type") or "").strip() or "(unknown)"
        grouped[kind].append((index, row))

    def summarize(kind: str, indexed: list[tuple[int, dict[str, str]]]) -> dict[str, object]:
        unique: dict[tuple[str, str, str], dict[str, str]] = {}
        people: set[str] = set()
        counts = {
            "candidate_name_only": 0,
            "candidate_with_1_plus_corroboration": 0,
            "candidate_with_2_plus_corroborations": 0,
            "candidate_with_conflicting_identifiers": 0,
            "scores_0_34": 0,
            "scores_35_59": 0,
            "scores_60_84": 0,
            "scores_85_100": 0,
        }
        total_score = 0
        for index, row in indexed:
            kind_for_row = str(row.get("archive_record_type") or "").strip() or "(unknown)"
            identity = (kind_for_row, *_archive_identity(row, index))
            unique[identity] = (
                _merge_archive_fields(unique[identity], row)
                if identity in unique else row
            )
            if _present(row.get("gedcom_id")):
                people.add(row["gedcom_id"].strip())
            try:
                score = int(row["match_score"])
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"Invalid match_score on candidate row {index + 2}") from exc
            if not 0 <= score <= 100:
                raise ValueError(f"Out-of-range match_score on candidate row {index + 2}")
            total_score += score
            bucket = ("scores_0_34" if score < 35 else
                      "scores_35_59" if score < 60 else
                      "scores_60_84" if score < 85 else "scores_85_100")
            counts[bucket] += 1
            supported = _corroborations(row.get("evidence", ""))
            counts["candidate_with_1_plus_corroboration"] += supported >= 1
            counts["candidate_with_2_plus_corroborations"] += supported >= 2
            counts["candidate_with_conflicting_identifiers"] += any(
                phrase in row.get("evidence", "").casefold() for phrase in CONFLICT
            )
            # No archive-side identifiers besides the name; this is distinct
            # from disagreement or from a record with an uncorroborated date.
            counts["candidate_name_only"] += not any(
                _present(row.get(field)) for field in
                ("archive_birth", "archive_birth_place", "archive_mother")
            )

        distinct_records = list(unique.values())
        n = len(distinct_records)
        birth = sum(_present(r.get("archive_birth")) for r in distinct_records)
        full_dates = sum(bool(FULL_DATE.fullmatch(
            (r.get("archive_birth") or "").strip())) for r in distinct_records)
        place = sum(_present(r.get("archive_birth_place")) for r in distinct_records)
        mother = sum(_present(r.get("archive_mother")) for r in distinct_records)
        return {
            "archive_record_type": kind,
            "candidate_rows": len(indexed),
            "distinct_gedcom_people": len(people),
            "distinct_archive_records": n,
            "archive_name_present": sum(_present(r.get("archive_name")) for r in distinct_records),
            "archive_birth_present": birth,
            "archive_birth_full_date": full_dates,
            "archive_birth_partial_date": birth - full_dates,
            "archive_birth_place_present": place,
            "archive_mother_present": mother,
            "archive_birth_pct": _pct(birth, n),
            "archive_birth_place_pct": _pct(place, n),
            "archive_mother_pct": _pct(mother, n),
            **counts,
            "average_identity_score": f"{total_score / len(indexed):.1f}" if indexed else "0.0",
        }

    rows = [summarize(kind, entries) for kind, entries in sorted(grouped.items())]
    if candidates:
        rows.append(summarize("TOTAL (candidate sample)", [
            (i, row) for i, row in enumerate(candidates)
        ]))
    return rows


def write_quality_report(source: Path, target: Path) -> list[dict[str, object]]:
    """Read local candidate CSV and write only aggregate counts to a new CSV."""
    if source.resolve() == target.resolve():
        raise ValueError("Quality report must not overwrite the source candidate CSV")
    if not source.is_file():
        raise ValueError(f"Candidate CSV not found: {source}")
    try:
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"gedcom_id", "archive_guid", "archive_record_type", "archive_name",
                        "archive_birth", "archive_birth_place", "archive_mother",
                        "match_score", "evidence"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(
                    "Candidate CSV missing required fields: " +
                    ", ".join(sorted(required - set(reader.fieldnames or [])))
                )
            candidates = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"Unable to read candidate CSV {source}: {exc}") from exc
    rows = quality_rows(candidates)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        with temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return rows
