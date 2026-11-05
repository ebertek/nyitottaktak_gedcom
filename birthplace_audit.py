"""Read-only birthplace review for selected P5 identity conflicts.

A place-name comparison is a research prompt, not identity proof. Only uniquely
resolved gazetteer entries may be called the same or different locality, and
neither outcome changes a stored match score. Private pairs stay in ignored
local CSV files; the aggregate report contains no people, GUIDs or places.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Callable

from hungarian_places import Entry, PlaceResolver, key
from research_shortlist import build_shortlist_rows

DETAIL_FIELDS = [
    "gedcom_id", "gedcom_name", "archive_guid", "archive_document_url",
    "archive_record_type", "selected_identity_score",
    "selected_conflicting_identifiers", "gedcom_birth_place",
    "archive_birth_place", "gedcom_locality", "archive_locality",
    "gedcom_entry_id", "archive_entry_id", "gedcom_entry_count",
    "archive_entry_count", "shared_possible_entry_ids",
    "place_assessment", "resolution_basis", "next_research_step",
]

SUMMARY_FIELDS = [
    "archive_record_type", "place_assessment", "selected_p5_relatives",
]

ASSESSMENTS = (
    "verified_same_locality",
    "ambiguous_historical_alias",
    "different_locality",
    "unresolved_locality",
)


def _index_identity(entries: tuple[Entry, ...]) -> str:
    return entries[0].identifier if len(entries) == 1 else ""


def _pair_status(
        raw_gedcom: str,
        raw_archive: str,
        resolver: PlaceResolver,
        normalize_place: Callable[[str], str],
) -> dict[str, str]:
    """Compare the same two birthplaces without guessing about names.

    - Equal unique entry IDs are a match in the local resolver, not proof of
      an individual's birthplace in an archival source.
    - Distinct unique IDs are confirmed different *within the same gazetteer
      authority*. A reviewed-to-GeoNames ID disagreement is inconclusive.
    - Ambiguity requires at least one overlapping possible entry ID.
    - Missing names and uncovered places remain unresolved.
    """
    first = normalize_place(raw_gedcom.strip())
    second = normalize_place(raw_archive.strip())
    a = resolver.candidates(first)
    b = resolver.candidates(second)
    id_a = _index_identity(a)
    id_b = _index_identity(b)
    common = sorted({item.identifier for item in a} &
                    {item.identifier for item in b})
    authority_a = id_a.split(":", 1)[0] if id_a else ""
    authority_b = id_b.split(":", 1)[0] if id_b else ""

    if id_a and id_b and id_a == id_b:
        assessment = "verified_same_locality"
        basis = (
            "reviewed_alias" if authority_a == "reviewed" else
            "unique_gazetteer_entry"
        )
        action = (
            "The current resolver maps both spellings to the same indexed "
            "locality. Inspect the original record and rescore saved evidence "
            "if the source supports this reading."
        )
    elif common and (len(a) > 1 or len(b) > 1):
        assessment = "ambiguous_historical_alias"
        basis = "overlapping_possible_gazetteer_entries"
        action = (
            "Possible shared locality, but at least one spelling identifies "
            "multiple places. Check the historical county/country and "
            "original document; do not accept an alias automatically."
        )
    elif id_a and id_b and authority_a == authority_b:
        assessment = "different_locality"
        basis = "distinct_unique_entries_in_same_authority"
        action = (
            "The two birthplaces resolve to distinct localities in the same "
            "gazetteer. Check family records and the source; nearby places "
            "must not be treated as interchangeable."
        )
    else:
        assessment = "unresolved_locality"
        basis = (
            "missing_birthplace" if not raw_gedcom.strip() or
            not raw_archive.strip() else
            "conflicting_gazetteer_authorities" if id_a and id_b else
            "unresolved_or_nonoverlapping_candidates"
        )
        action = (
            "No unambiguous equivalence or difference can be established "
            "from the local resolver. Verify historical spelling and "
            "administrative context with independent references."
        )
        if key(first.split(",", 1)[0]) == key(second.split(",", 1)[0]):
            action += " The text normalizes equally but locality identity is unverified."

    return {
        "gedcom_locality": first.split(",", 1)[0].strip(),
        "archive_locality": second.split(",", 1)[0].strip(),
        "gedcom_entry_id": id_a,
        "archive_entry_id": id_b,
        "gedcom_entry_count": str(len(a)),
        "archive_entry_count": str(len(b)),
        "shared_possible_entry_ids": " | ".join(common),
        "place_assessment": assessment,
        "resolution_basis": basis,
        "next_research_step": action,
    }


def build_birthplace_conflicts(
        candidates: list[dict],
        resolver: PlaceResolver,
        normalize_place: Callable[[str], str],
) -> list[dict[str, str]]:
    """One private row per selected P5 lead *with a birthplace conflict*."""
    output = []
    for lead in build_shortlist_rows(candidates):
        if (lead["research_priority"] != "P5" or
                lead["p5_conflict_birthplace"] != "yes"):
            continue
        first = str(lead.get("gedcom_birth_place") or "")
        second = str(lead.get("selected_archive_birth_place") or "")
        output.append({
            "gedcom_id": str(lead["gedcom_id"]),
            "gedcom_name": str(lead["gedcom_name"]),
            "archive_guid": str(lead["selected_archive_guid"]),
            "archive_document_url": str(lead["selected_archive_document_url"]),
            "archive_record_type": str(lead["selected_archive_record_type"]),
            "selected_identity_score": str(lead["selected_identity_score"]),
            "selected_conflicting_identifiers": str(
                lead["selected_conflicting_identifiers"]
            ),
            "gedcom_birth_place": first,
            "archive_birth_place": second,
            **_pair_status(first, second, resolver, normalize_place),
        })
    output.sort(key=lambda item: (
        ASSESSMENTS.index(item["place_assessment"]),
        item["gedcom_id"], item["archive_guid"],
    ))
    return output


def _source_category(raw: object) -> str:
    """Use only bounded, known source categories in identifier-free reports.

    Raw upstream record-type text is retained only in the private report.
    Unknown arbitrary strings must not unexpectedly expose personal text.
    """
    name = key(str(raw or ""))
    if "6 os karton" in name:
        return "6-os karton"
    if "b naplo" in name:
        return "B napló"
    if "b dosszie" in name:
        return "B-dosszié"
    if "magnesszalag" in name:
        return "Mágnesszalag"
    if "h adattar" in name:
        return "H-adattár"
    return "(other/unreported)"


def build_birthplace_summary(details: list[dict]) -> list[dict]:
    """Aggregate by safe source categories/status, no names, IDs, or places."""
    totals = Counter(
        (_source_category(row.get("archive_record_type")),
         row["place_assessment"])
        for row in details
    )
    return [
        {
            "archive_record_type": source,
            "place_assessment": status,
            "selected_p5_relatives": count,
        }
        for (source, status), count in sorted(totals.items())
    ]


def _write(target: Path, fields: list[str], rows: list[dict]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def write_birthplace_reports(
        candidates: list[dict],
        resolver: PlaceResolver,
        normalize_place: Callable[[str], str],
        detail_path: Path,
        summary_path: Path,
) -> tuple[int, Counter]:
    """Generate two local CSVs, with no GEDCOM reads or API calls."""
    if detail_path.resolve() == summary_path.resolve():
        raise ValueError("Birthplace detail and summary paths must differ")
    details = build_birthplace_conflicts(candidates, resolver, normalize_place)
    summaries = build_birthplace_summary(details)
    _write(detail_path, DETAIL_FIELDS, details)
    _write(summary_path, SUMMARY_FIELDS, summaries)
    return len(details), Counter(item["place_assessment"] for item in details)
