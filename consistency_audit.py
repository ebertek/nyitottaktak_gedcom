"""Conservative offline chronology and shared-archive-record research checks.

These are *review prompts*, never identity determinations or historical conduct
inferences. Archive material may refer retrospectively to people who were
already deceased, so a birth year alone must not exclude a candidate.

Only saved local candidate rows are inspected. No network or GEDCOM access.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

# The archive covers a broad historical period, not a dated personal event.
# B-napló has a narrower *published register scope*, not an event timestamp.
ARCHIVE_SCOPE = (1944, 1990)
B_NAPLO_SCOPE = (1956, 1962)
AGE_REVIEW_THRESHOLD = 65
YEAR = re.compile(r"(?<!\d)(1[6789]\d{2}|20\d{2})(?!\d)")
UNCERTAIN_DATE = re.compile(
    r"\b(?:ABT|ABOUT|BEF|BEFORE|AFT|AFTER|EST|CAL|BET|BETWEEN|FROM|TO|CIRCA)\b",
    re.IGNORECASE,
)

CONSISTENCY_FIELDS = [
    "gedcom_id", "gedcom_name", "archive_guid", "archive_record_type",
    "archive_document_url", "gedcom_birth", "archive_birth",
    "gedcom_birth_year", "archive_birth_year", "scope_start", "scope_end",
    "age_at_scope_start", "chronology_flags", "shared_archive_guid",
    "shared_gedcom_people", "other_gedcom_ids", "review_explanation",
]

SHARED_FIELDS = [
    "archive_guid", "archive_document_url", "archive_record_types",
    "distinct_gedcom_people", "gedcom_ids", "gedcom_birth_years",
    "min_gedcom_birth_year", "max_gedcom_birth_year",
    "birth_year_span", "different_birth_years", "review_explanation",
]


def birth_year(value: object) -> int | None:
    """Use only unambiguous 4-digit years; never guess centuries for '29'."""
    found = YEAR.search(str(value or ""))
    return int(found.group(1)) if found else None


def archive_scope(record_type: object) -> tuple[int, int]:
    name = str(record_type or "").casefold().replace("-", " ").replace("‑", " ")
    if "napló" in name or "naplo" in name:
        return B_NAPLO_SCOPE
    return ARCHIVE_SCOPE


def chronology(row: dict) -> dict[str, str]:
    """Evaluate dates conservatively, without inferring anyone's historical role.

    The age check asks whether a person was already >=65 at the beginning of
    the *reference scope*. It is an investigative prompt, not impossibility:
    registers may include older people or retrospective references.
    """
    gedcom = birth_year(row.get("gedcom_birth"))
    archived = birth_year(row.get("archive_birth"))
    start, end = archive_scope(row.get("archive_record_type"))
    flags: list[str] = []
    notes: list[str] = []
    approximate = bool(UNCERTAIN_DATE.search(str(row.get("gedcom_birth") or "")))

    if gedcom is not None and gedcom > end:
        flags.append("born_after_archive_scope")
        notes.append("GEDCOM birth year falls after the documented source scope; inspect source dating")
    elif gedcom is not None and start - gedcom >= AGE_REVIEW_THRESHOLD:
        flags.append("older_birth_cohort_review")
        notes.append(
            f"GEDCOM birth implies age >= {start - gedcom} at the earliest scope year "
            f"{start}; archived material can be retrospective"
        )
    if gedcom is not None and archived is not None and abs(gedcom - archived) >= 2:
        flags.append("birth_year_discrepancy")
        notes.append(
            f"GEDCOM and archive birth years differ by {abs(gedcom - archived)}; "
            "verify both source dates"
        )
    if approximate and flags:
        notes.append("GEDCOM birth date is approximate; do not treat the year as exact")

    return {
        "gedcom_birth_year": str(gedcom) if gedcom is not None else "",
        "archive_birth_year": str(archived) if archived is not None else "",
        "scope_start": str(start),
        "scope_end": str(end),
        "age_at_scope_start": str(start - gedcom) if gedcom is not None else "",
        "chronology_flags": " | ".join(flags),
        "review_explanation": " | ".join(notes),
    }


def shared_guid_index(candidates: list[dict]) -> dict[str, dict[str, dict]]:
    """Group only nonblank archive GUIDs by distinct GEDCOM ID.

    Duplicate searches of the same person/record cannot create a false shared
    GUID. Documents may legitimately mention more than one individual.
    """
    groups: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in candidates:
        guid = str(row.get("archive_guid") or "").strip()
        pid = str(row.get("gedcom_id") or "").strip()
        if guid and pid:
            groups[guid].setdefault(pid, row)
    return groups


def shared_record_rows(candidates: list[dict]) -> list[dict]:
    """One private row per GUID appearing against multiple GEDCOM people."""
    grouped = shared_guid_index(candidates)
    output = []
    for guid, by_person in grouped.items():
        if len(by_person) < 2:
            continue
        members = [by_person[pid] for pid in sorted(by_person)]
        years = sorted({
            year for row in members
            if (year := birth_year(row.get("gedcom_birth"))) is not None
        })
        names = sorted({str(row.get("archive_record_type") or "").strip()
                        for row in members if str(row.get("archive_record_type") or "").strip()})
        gap = years[-1] - years[0] if len(years) > 1 else 0
        output.append({
            "archive_guid": guid,
            "archive_document_url": str(next(
                (row.get("archive_document_url") for row in members
                 if row.get("archive_document_url")), ""
            )),
            "archive_record_types": " | ".join(names),
            "distinct_gedcom_people": len(by_person),
            "gedcom_ids": " | ".join(sorted(by_person)),
            "gedcom_birth_years": " | ".join(map(str, years)),
            "min_gedcom_birth_year": str(years[0]) if years else "",
            "max_gedcom_birth_year": str(years[-1]) if years else "",
            "birth_year_span": str(gap) if years else "",
            "different_birth_years": "yes" if gap >= 2 else "no",
            "review_explanation": (
                "This archive GUID is linked to several GEDCOM identities; "
                "the source may mention several people or the tree may contain "
                "duplicate/incorrect identities. Do not choose automatically."
            ),
        })
    output.sort(key=lambda row: (
        -int(row["distinct_gedcom_people"]), row["archive_guid"]
    ))
    return output


def consistency_rows(candidates: list[dict]) -> list[dict]:
    """Flag each *candidate row* with chronology prompts or shared archive GUID."""
    grouped = shared_guid_index(candidates)
    result = []
    for row in candidates:
        checks = chronology(row)
        guid = str(row.get("archive_guid") or "").strip()
        pid = str(row.get("gedcom_id") or "").strip()
        associated = grouped.get(guid, {}) if guid else {}
        is_shared = bool(pid and len(associated) > 1)
        if not checks["chronology_flags"] and not is_shared:
            continue
        related = sorted(other for other in associated if other != pid) if is_shared else []
        explanation = checks["review_explanation"]
        if is_shared:
            explanation = " | ".join(filter(None, (
                explanation,
                "Archive GUID appears against multiple GEDCOM IDs; investigate identities separately"
            )))
        result.append({
            "gedcom_id": pid,
            "gedcom_name": str(row.get("gedcom_name") or ""),
            "archive_guid": guid,
            "archive_record_type": str(row.get("archive_record_type") or ""),
            "archive_document_url": str(row.get("archive_document_url") or ""),
            "gedcom_birth": str(row.get("gedcom_birth") or ""),
            "archive_birth": str(row.get("archive_birth") or ""),
            **checks,
            "shared_archive_guid": "yes" if is_shared else "no",
            "shared_gedcom_people": str(len(associated)) if is_shared else "",
            "other_gedcom_ids": " | ".join(related),
            "review_explanation": explanation,
        })
    result.sort(key=lambda row: (
        row["gedcom_id"], row["archive_guid"], row["archive_record_type"]
    ))
    return result


def _write_csv(target: Path, columns: list[str], rows: list[dict]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def write_consistency_reports(
        candidates: list[dict], consistency_path: Path, shared_path: Path
) -> tuple[int, int, Counter]:
    """Generate local private reports without changing input rows."""
    if consistency_path.resolve() == shared_path.resolve():
        raise ValueError("Consistency and shared-records output paths must differ")
    findings = consistency_rows(candidates)
    shared = shared_record_rows(candidates)
    _write_csv(consistency_path, CONSISTENCY_FIELDS, findings)
    _write_csv(shared_path, SHARED_FIELDS, shared)
    flags: Counter = Counter()
    for row in findings:
        for flag in row["chronology_flags"].split(" | "):
            if flag:
                flags[flag] += 1
    return len(findings), len(shared), flags
