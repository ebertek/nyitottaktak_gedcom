"""Offline shared-archive-GUID evidence profiles and source-level aggregates.

These are *identity-research leads*, never claims about historical conduct.
One archival GUID may legitimately refer to multiple people. This module
does not alter, filter, or re-score saved candidate rows.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from consistency_audit import birth_year
from review_report import classify_identity_evidence
from research_shortlist import build_shortlist_rows

AGGREGATE_FIELDS = [
    "archive_record_type",
    "evidence_classification",
    "candidate_rows",
    "distinct_gedcom_people",
    "distinct_nonempty_archive_guids",
    "shared_guid_candidate_rows",
    "shared_archive_guids",
    "shared_name_only_only_guids",
    "shared_with_corroboration_guids",
    "shared_other_guids",
]

TRIAGE_FIELDS = [
    "archive_guid", "archive_document_url", "archive_record_types",
    "collision_profile", "distinct_gedcom_people", "candidate_rows",
    "gedcom_ids", "gedcom_birth_years", "birth_year_span",
    "strongly_corroborated_people", "partially_corroborated_people",
    "name_only_only_people", "conflicting_identity_people",
    "insufficient_gedcom_data_people", "insufficient_comparable_data_people",
    "review_action",
]

CORROBORATED = {"strongly_corroborated", "partially_corroborated"}


def _known_rows(candidates: list[dict]) -> dict[str, list[dict]]:
    """Group GUIDs globally across all sources, retaining every candidate row."""
    by_guid: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        guid = str(row.get("archive_guid") or "").strip()
        pid = str(row.get("gedcom_id") or "").strip()
        if guid and pid:
            by_guid[guid].append(row)
    return by_guid


def collision_profiles(candidates: list[dict]) -> dict[str, dict]:
    """Describe shared GUIDs using *independent per-person* evidence classes.

    Duplicate search rows for the same person and archive record do not
    inflate numbers of distinct people. Classifications remain attached to the
    original person/record pair and cannot be pooled to claim corroboration.
    """
    profiles = {}
    for guid, rows in _known_rows(candidates).items():
        people: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            pid = str(row["gedcom_id"]).strip()
            people[pid].add(classify_identity_evidence(row)["classification"])
        if len(people) < 2:
            continue
        everyone_name_only = all(
            classes == {"name_only"} for classes in people.values()
        )
        any_corrob = any(
            bool(classes & CORROBORATED) for classes in people.values()
        )
        if any_corrob:
            profile = "corroborated_lead_present"
            action = (
                "Inspect independently corroborated person-record comparisons "
                "separately; a shared document is not a shared person"
            )
        elif everyone_name_only:
            profile = "name_only_collision"
            action = (
                "Defer name-only overlaps until additional identifiers "
                "or original document context are available"
            )
        else:
            profile = "other_ambiguous_collision"
            action = (
                "Inspect missing or conflicting identifying fields for each "
                "GEDCOM person separately"
            )
        profiles[guid] = {
            "profile": profile,
            "action": action,
            "people": people,
            "rows": rows,
        }
    return profiles


def build_collision_summary(candidates: list[dict]) -> list[dict]:
    """Aggregate by archival source and evidence class, with no private IDs.

    All candidate-row counts count original saved rows. Distinct GUID counts
    within each (source, class) are not additive across rows: the same archive
    document may appear under multiple source labels or evidence classes.
    Shared-GUID profiles are computed *globally*, not from just one row/group.
    """
    profiles = collision_profiles(candidates)
    buckets: dict[tuple[str, str], dict] = {}
    for row in candidates:
        pid = str(row.get("gedcom_id") or "").strip()
        if not pid:
            continue
        source = str(row.get("archive_record_type") or "").strip() or "(unreported)"
        label = classify_identity_evidence(row)["classification"]
        guid = str(row.get("archive_guid") or "").strip()
        key = (source, label)
        if key not in buckets:
            buckets[key] = {
                "count": 0, "people": set(), "guids": set(),
                "shared_rows": 0, "shared_guids": set(),
                "by_profile": defaultdict(set),
            }
        b = buckets[key]
        b["count"] += 1
        b["people"].add(pid)
        if guid:
            b["guids"].add(guid)
        if guid in profiles:
            b["shared_rows"] += 1
            b["shared_guids"].add(guid)
            b["by_profile"][profiles[guid]["profile"]].add(guid)
    output = []
    for (source, label), b in sorted(buckets.items()):
        output.append({
            "archive_record_type": source,
            "evidence_classification": label,
            "candidate_rows": b["count"],
            "distinct_gedcom_people": len(b["people"]),
            "distinct_nonempty_archive_guids": len(b["guids"]),
            "shared_guid_candidate_rows": b["shared_rows"],
            "shared_archive_guids": len(b["shared_guids"]),
            "shared_name_only_only_guids": len(
                b["by_profile"]["name_only_collision"]
            ),
            "shared_with_corroboration_guids": len(
                b["by_profile"]["corroborated_lead_present"]
            ),
            "shared_other_guids": len(
                b["by_profile"]["other_ambiguous_collision"]
            ),
        })
    return output


def build_collision_triage(candidates: list[dict]) -> list[dict]:
    """One private GUID-level row; do not infer merged identities."""
    profiles = collision_profiles(candidates)
    result = []
    for guid, info in profiles.items():
        rows = info["rows"]
        people = info["people"]
        years = sorted({
            year for row in rows
            if (year := birth_year(row.get("gedcom_birth"))) is not None
        })
        year_span = years[-1] - years[0] if len(years) >= 2 else None
        types = sorted({
            str(row.get("archive_record_type") or "").strip() for row in rows
            if str(row.get("archive_record_type") or "").strip()
        })
        counts = Counter()
        for classes in people.values():
            for label in classes:
                counts[label] += 1
        result.append({
            "archive_guid": guid,
            "archive_document_url": next((
                str(row.get("archive_document_url")) for row in rows
                if row.get("archive_document_url")
            ), ""),
            "archive_record_types": " | ".join(types),
            "collision_profile": info["profile"],
            "distinct_gedcom_people": len(people),
            "candidate_rows": len(rows),
            "gedcom_ids": " | ".join(sorted(people)),
            "gedcom_birth_years": " | ".join(map(str, years)),
            "birth_year_span": str(year_span) if year_span is not None else "",
            "strongly_corroborated_people": counts["strongly_corroborated"],
            "partially_corroborated_people": counts["partially_corroborated"],
            "name_only_only_people": sum(
                classes == {"name_only"} for classes in people.values()
            ),
            "conflicting_identity_people": counts["conflicting_identity"],
            "insufficient_gedcom_data_people": counts["insufficient_gedcom_data"],
            "insufficient_comparable_data_people": counts[
                "insufficient_comparable_data"
            ],
            "review_action": info["action"],
        })
    profile_order = {
        "corroborated_lead_present": 0,
        "other_ambiguous_collision": 1,
        "name_only_collision": 2,
    }
    result.sort(key=lambda item: (
        profile_order[item["collision_profile"]],
        -int(item["distinct_gedcom_people"]),
        item["archive_guid"],
    ))
    return result


def summarize_selected_p5_conflicts(candidates: list[dict]) -> dict[str, int]:
    """Aggregate selected P5 identifiers without exposing names, IDs or URLs.

    The selected lead is determined independently for each GEDCOM person by
    the existing evidence-ranking rules. Counts are non-exclusive: one person
    may disagree on multiple identifiers. Alternative candidate disagreements
    do not become the selected lead's conflicts.
    """
    selected = [
        row for row in build_shortlist_rows(candidates)
        if row["research_priority"] == "P5"
    ]
    return {
        "relatives": len(selected),
        "birth_date": sum(row["p5_conflict_birth_date"] == "yes" for row in selected),
        "mother": sum(row["p5_conflict_mother"] == "yes" for row in selected),
        "birthplace": sum(row["p5_conflict_birthplace"] == "yes" for row in selected),
    }


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


def write_collision_reports(
    candidates: list[dict], summary_path: Path, triage_path: Path
) -> tuple[int, Counter]:
    """Write privacy-preserving aggregates and a separate *private* GUID queue."""
    if summary_path.resolve() == triage_path.resolve():
        raise ValueError("Collision report output paths must differ")
    summary = build_collision_summary(candidates)
    triage = build_collision_triage(candidates)
    _write(summary_path, AGGREGATE_FIELDS, summary)
    _write(triage_path, TRIAGE_FIELDS, triage)
    return len(summary), Counter(
        row["collision_profile"] for row in triage
    )
