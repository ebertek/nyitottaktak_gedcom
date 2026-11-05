"""Offline, privacy-preserving review summaries for Nyitott Akták identity leads.

This module reads *local* search output. It never queries the archive, and its
flags refer only to identity-data agreement and search completeness—not to
historical conduct, recruitment, or collaboration.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path


FIELDS = [
    "gedcom_id",
    "gedcom_name",
    "gedcom_birth",
    "gedcom_birth_place",
    "gedcom_mother",
    "search_status",
    "search_warnings",
    "candidate_count",
    "distinct_archive_records",
    "record_types",
    "highest_identity_score",
    "top_archive_guid",
    "top_archive_name",
    "top_archive_birth",
    "top_archive_birth_place",
    "top_archive_mother",
    "top_archive_record_type",
    "top_matched_name_origin",
    "top_searched_name_origin",
    "top_identity_evidence",
    "top_archive_document_url",
    "review_flags",
    "review_order",
]

# Order explicitly describes *identity checking effort*, not suspected conduct.
REVIEW_ORDER = {
    "conflicting_identity_data": 0,
    "archive_guid_shared_by_multiple_relatives": 1,
    "incomplete_search": 2,
    "multiple_identity_leads": 3,
    "needs_additional_identifiers": 4,
    "inferred_married_name": 4,
    "identity_lead_to_review": 5,
    "no_candidate_leads": 6,
}


def _int(value: object) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _unique_guids(rows: list[dict]) -> int:
    """Empty GUIDs cannot be treated as a single known archive identity."""
    known = {row.get("archive_guid", "").strip() for row in rows
             if row.get("archive_guid", "").strip()}
    unknown = sum(not row.get("archive_guid", "").strip() for row in rows)
    return len(known) + unknown


def build_review_rows(candidates: list[dict], coverage: list[dict] | None = None) -> list[dict]:
    """Summarize best-scoring leads, contradictions and coverage per GEDCOM ID.

    A match score is an existing heuristic used solely to order manual review.
    A high score does not establish an individual's historical role.
    """
    coverage = coverage or []
    by_person: dict[str, list[dict]] = defaultdict(list)
    person_details: dict[str, dict] = {}
    shared_guids: dict[str, set[str]] = defaultdict(set)

    for source in candidates:
        pid = str(source.get("gedcom_id") or "").strip()
        if not pid:
            continue
        row = dict(source)
        by_person[pid].append(row)
        person_details.setdefault(pid, row)
        guid = str(row.get("archive_guid") or "").strip()
        if guid:
            shared_guids[guid].add(pid)

    for source in coverage:
        pid = str(source.get("gedcom_id") or "").strip()
        if pid:
            person_details.setdefault(pid, dict(source))

    coverage_index = {
        str(row.get("gedcom_id") or "").strip(): row
        for row in coverage if str(row.get("gedcom_id") or "").strip()
    }
    summary = []
    for pid, detail in person_details.items():
        rows = by_person[pid]
        ordered = sorted(rows, key=lambda row: (
            -_int(row.get("match_score")), str(row.get("archive_guid") or ""),
            str(row.get("archive_name") or ""),
        ))
        top = ordered[0] if ordered else {}
        evidence = str(top.get("evidence") or "")
        found = evidence.casefold()
        flags = []

        # Only the top-scoring candidate's identity attributes are compared
        # here; mixing evidence from unrelated records is misleading.
        if any(token in found for token in (
            "dob conflict", "birth-year conflict", "mother name differs",
            "birthplace differs",
        )):
            flags.append("conflicting_identity_data")
        if any(
            len(shared_guids[str(row.get("archive_guid") or "").strip()]) > 1
            for row in ordered if str(row.get("archive_guid") or "").strip()
        ):
            flags.append("archive_guid_shared_by_multiple_relatives")

        status = str(coverage_index.get(pid, {}).get("search_status") or "not_reported")
        if status in ("limited", "not_completed"):
            flags.append("incomplete_search")
        if len(ordered) > 1:
            flags.append("multiple_identity_leads")

        if top and (
            "dob unavailable" in found or
            "mother unavailable" in found or
            "birthplace unavailable" in found
        ):
            flags.append("needs_additional_identifiers")

        if top.get("matched_name_origin") == "inferred_married":
            # A search-generated name may be historically plausible but is not
            # documented as a legal/used name in the GEDCOM.
            flags.append("inferred_married_name")

        if not ordered:
            flags.append("no_candidate_leads")
        elif not flags:
            flags.append("identity_lead_to_review")

        # The review_order is for triage by identity confidence and missing
        # evidence only. It does NOT measure historical participation.
        group = min(REVIEW_ORDER[flag] for flag in flags)
        details = top if ordered else detail
        source_types = sorted({str(r.get("archive_record_type") or "").strip()
                               for r in ordered if str(r.get("archive_record_type") or "").strip()})

        summary.append({
            "gedcom_id": pid,
            "gedcom_name": str(details.get("gedcom_name") or detail.get("gedcom_name") or ""),
            "gedcom_birth": str(details.get("gedcom_birth") or detail.get("gedcom_birth") or ""),
            "gedcom_birth_place": str(details.get("gedcom_birth_place") or
                                     detail.get("gedcom_birth_place") or ""),
            "gedcom_mother": str(details.get("gedcom_mother") or detail.get("gedcom_mother") or ""),
            "search_status": status,
            "search_warnings": str(coverage_index.get(pid, {}).get("search_warnings") or ""),
            "candidate_count": len(rows),
            "distinct_archive_records": _unique_guids(rows),
            "record_types": " | ".join(source_types),
            "highest_identity_score": _int(top.get("match_score")) if top else "",
            "top_archive_guid": str(top.get("archive_guid") or ""),
            "top_archive_name": str(top.get("archive_name") or ""),
            "top_archive_birth": str(top.get("archive_birth") or ""),
            "top_archive_birth_place": str(top.get("archive_birth_place") or ""),
            "top_archive_mother": str(top.get("archive_mother") or ""),
            "top_archive_record_type": str(top.get("archive_record_type") or ""),
            "top_matched_name_origin": str(top.get("matched_name_origin") or ""),
            "top_searched_name_origin": str(top.get("searched_name_origin") or ""),
            "top_identity_evidence": evidence,
            "top_archive_document_url": str(top.get("archive_document_url") or ""),
            "review_flags": " | ".join(flags),
            "review_order": group,
        })

    summary.sort(key=lambda row: (
        row["review_order"],
        -_int(row["highest_identity_score"]),
        row["gedcom_id"],
    ))
    return summary


def read_csv(path: Path, required: bool = True) -> list[dict]:
    if not path.exists():
        if not required:
            return []
        raise ValueError(f"File not found: {path}")
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "gedcom_id" not in reader.fieldnames:
                raise ValueError(f"Missing gedcom_id column in {path}")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc


def write_review_report(output: Path, candidates: list[dict],
                        coverage: list[dict] | None = None) -> Counter:
    rows = build_review_rows(candidates, coverage)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open(newline="", encoding="utf-8-sig", mode="w") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)
    return Counter(row["review_flags"].split(" | ")[0] for row in rows)


def review_files(candidates_path: Path, coverage_path: Path,
                 output_path: Path) -> tuple[int, Counter, bool]:
    """Generate a review CSV without needing a GEDCOM or network access."""
    if output_path.resolve() in (candidates_path.resolve(), coverage_path.resolve()):
        raise ValueError("Review output must not overwrite a source CSV")
    candidates = read_csv(candidates_path)
    coverage_present = coverage_path.exists()
    coverage = read_csv(coverage_path, required=False)
    counts = write_review_report(output_path, candidates, coverage)
    people = len(build_review_rows(candidates, coverage))
    return people, counts, coverage_present
