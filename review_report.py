"""Offline, privacy-preserving review summaries for Nyitott Akták identity leads.

This module reads *local* search output. It never queries the archive, and its
flags refer only to identity-data agreement and search completeness—not to
historical conduct, recruitment, or collaboration.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from consistency_audit import chronology


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
    "top_evidence_classification",
    "top_agreeing_identifiers",
    "top_conflicting_identifiers",
    "top_missing_gedcom_identifiers",
    "candidate_strongly_corroborated",
    "candidate_partially_corroborated",
    "candidate_name_only",
    "candidate_conflicting_identity",
    "candidate_insufficient_gedcom_data",
    "candidate_insufficient_comparable_data",
    "best_evidence_archive_guid",
    "best_evidence_archive_name",
    "best_evidence_archive_record_type",
    "best_evidence_archive_document_url",
    "best_evidence_identity_score",
    "best_evidence_classification",
    "best_evidence_agreeing_identifiers",
    "best_evidence_conflicting_identifiers",
    "best_evidence_missing_gedcom_identifiers",
    "best_evidence_matched_name_origin",
    "best_evidence_is_highest_score",
    "top_chronology_flags",
    "best_evidence_chronology_flags",
    "candidate_chronology_review_count",
    "candidate_shared_archive_guid_count",
    "best_evidence_archive_guid_shared_people",
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



# Exclusive classifications of candidate *identity evidence*, not historical
# conduct or archive-document reliability. Keep score and review flags separate.
EVIDENCE_CLASSES = (
    "strongly_corroborated",
    "partially_corroborated",
    "name_only",
    "conflicting_identity",
    "insufficient_gedcom_data",
    "insufficient_comparable_data",
)

# Phrases are emitted by the core score() function. Match complete semicolon
# tokens to avoid confusing e.g. "mother unavailable" with "mother exact".
EXACT_AGREEMENTS = {
    "exact dob": ("birth_date", "exact"),
    "mother exact": ("mother", "exact"),
    "birthplace exact": ("birthplace", "exact"),
}
PARTIAL_AGREEMENTS = {
    "birth year matches": ("birth_date", "year_only"),
    "mother partial": ("mother", "partial"),
    "birthplace partial": ("birthplace", "partial"),
}
CONTRADICTIONS = {
    "dob conflict (year)": "birth_date",
    "dob conflict (day/month)": "birth_date",
    "birth-year conflict": "birth_date",
    "mother name differs": "mother",
    "birthplace differs": "birthplace",
}
IDENTIFIER_COLUMNS = (
    ("birth_date", "gedcom_birth", "archive_birth"),
    ("birthplace", "gedcom_birth_place", "archive_birth_place"),
    ("mother", "gedcom_mother", "archive_mother"),
)


def classify_identity_evidence(row: dict) -> dict[str, str]:
    """Conservative classification of one saved candidate, using scored evidence.

    The archive and GEDCOM fields are used only to distinguish no archive
    identifiers from GEDCOM omissions. Never infer an agreement from field
    presence alone, nor mix evidence from different archive GUIDs.

    A birth year differing by one is not counted as an independent agreement.
    Two *exact* identifiers are required for strongly_corroborated; partial
    evidence cannot promote a candidate to that tier.
    """
    parts = {
        part.strip().casefold()
        for part in str(row.get("evidence") or "").split(";")
        if part.strip()
    }
    agreed = {
        identifier: strength
        for token, (identifier, strength) in (
            list(EXACT_AGREEMENTS.items()) + list(PARTIAL_AGREEMENTS.items())
        )
        if token in parts
    }
    conflicts = {identifier for token, identifier in CONTRADICTIONS.items()
                 if token in parts}
    missing_gedcom = {
        name for name, gedcom_field, archive_field in IDENTIFIER_COLUMNS
        if str(row.get(archive_field) or "").strip()
        and not str(row.get(gedcom_field) or "").strip()
    }
    archive_identifiers = any(
        str(row.get(field) or "").strip() for _, _, field in IDENTIFIER_COLUMNS
    )

    if conflicts:
        label = "conflicting_identity"
    elif sum(strength == "exact" for strength in agreed.values()) >= 2:
        label = "strongly_corroborated"
    elif agreed:
        label = "partially_corroborated"
    elif not archive_identifiers:
        label = "name_only"
    elif missing_gedcom:
        label = "insufficient_gedcom_data"
    else:
        # Non-empty fields are not automatically comparable (historical dates,
        # unrecognized formats, missing/inconsistent evidence from legacy CSVs).
        label = "insufficient_comparable_data"

    return {
        "classification": label,
        "agreeing_identifiers": " | ".join(
            f"{name}:{agreed[name]}" for name, _, _ in IDENTIFIER_COLUMNS
            if name in agreed
        ),
        "conflicting_identifiers": " | ".join(
            name for name, _, _ in IDENTIFIER_COLUMNS if name in conflicts
        ),
        "missing_gedcom_identifiers": " | ".join(
            name for name, _, _ in IDENTIFIER_COLUMNS if name in missing_gedcom
        ),
    }


# Evidence priority is distinct from the existing numeric score and historical
# interpretation. Conflicting candidates remain visible, but are not promoted
# over non-conflicting leads purely because their heuristic score is higher.
EVIDENCE_PRIORITY = {
    "strongly_corroborated": 0,
    "partially_corroborated": 1,
    "insufficient_gedcom_data": 2,
    "insufficient_comparable_data": 3,
    "conflicting_identity": 4,
    "name_only": 5,
}


def select_best_evidence(rows: list[dict]) -> tuple[dict, dict[str, str]] | None:
    """Choose one research lead without mixing separate archive identities.

    Rank by established corroboration first, then the number/strength of
    agreeing attributes, documented name before speculative married name, and
    only then the original numerical identity score. The highest-score lead
    remains independently available in the review report.
    """
    if not rows:
        return None

    def ranking(row: dict) -> tuple:
        assessment = classify_identity_evidence(row)
        components = assessment["agreeing_identifiers"].split(" | ")
        exact = sum(item.endswith(":exact") for item in components)
        partial = sum(
            item.endswith((":partial", ":year_only")) for item in components
        )
        conflicts = len(assessment["conflicting_identifiers"].split(" | ")) if (
            assessment["conflicting_identifiers"]) else 0
        inferred = str(row.get("matched_name_origin") or "") == "inferred_married"
        return (
            EVIDENCE_PRIORITY[assessment["classification"]],
            -exact, -partial, conflicts, inferred,
            -_int(row.get("match_score")),
            str(row.get("archive_guid") or ""),
            str(row.get("archive_name") or ""),
            str(row.get("archive_record_type") or ""),
            str(row.get("evidence") or ""),
        )

    winner = min(rows, key=ranking)
    return winner, classify_identity_evidence(winner)


def missing_tree_identifiers(row: dict) -> str:
    """List missing GEDCOM identifiers, including fields absent in the archive.

    This is a genealogy enrichment checklist, not proof that the field exists
    in the source. It intentionally does not use inferred married names.
    """
    return " | ".join(
        identifier for identifier, gedcom_field, _ in IDENTIFIER_COLUMNS
        if not str(row.get(gedcom_field) or "").strip()
    )


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
        # Classify each candidate separately. The top-class column describes
        # only the same highest-scoring row as top_identity_evidence; the
        # counters report alternatives without conflating archive identities.
        assessments = [classify_identity_evidence(row) for row in ordered]
        top_assessment = assessments[0] if assessments else {}
        evidence_counts = Counter(a["classification"] for a in assessments)
        chosen = select_best_evidence(ordered)
        best_row, best_assessment = chosen if chosen else ({}, {})
        top_chronology = chronology(top)["chronology_flags"] if top else ""
        best_chronology = chronology(best_row)["chronology_flags"] if best_row else ""
        chronology_review_count = sum(
            bool(chronology(row)["chronology_flags"]) for row in ordered
        )
        shared_candidate_count = sum(
            bool(str(row.get("archive_guid") or "").strip() and
                 len(shared_guids[str(row.get("archive_guid") or "").strip()]) > 1)
            for row in ordered
        )
        best_guid = str(best_row.get("archive_guid") or "").strip()
        shared_best_count = len(shared_guids[best_guid]) if best_guid else 0
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
            "top_evidence_classification": (
                top_assessment.get("classification") or "no_candidate_leads"
            ),
            "top_agreeing_identifiers": top_assessment.get("agreeing_identifiers", ""),
            "top_conflicting_identifiers": top_assessment.get("conflicting_identifiers", ""),
            "top_missing_gedcom_identifiers": top_assessment.get("missing_gedcom_identifiers", ""),
            **{f"candidate_{label}": evidence_counts[label]
               for label in EVIDENCE_CLASSES},
            "best_evidence_archive_guid": str(best_row.get("archive_guid") or ""),
            "best_evidence_archive_name": str(best_row.get("archive_name") or ""),
            "best_evidence_archive_record_type": str(
                best_row.get("archive_record_type") or ""
            ),
            "best_evidence_archive_document_url": str(
                best_row.get("archive_document_url") or ""
            ),
            "best_evidence_identity_score": (
                _int(best_row.get("match_score")) if best_row else ""
            ),
            "best_evidence_classification": (
                best_assessment.get("classification") or "no_candidate_leads"
            ),
            "best_evidence_agreeing_identifiers": best_assessment.get(
                "agreeing_identifiers", ""
            ),
            "best_evidence_conflicting_identifiers": best_assessment.get(
                "conflicting_identifiers", ""
            ),
            "best_evidence_missing_gedcom_identifiers": best_assessment.get(
                "missing_gedcom_identifiers", ""
            ),
            "best_evidence_matched_name_origin": str(
                best_row.get("matched_name_origin") or ""
            ),
            "best_evidence_is_highest_score": (
                "yes" if best_row is top else "no" if best_row else ""
            ),
            "top_chronology_flags": top_chronology,
            "best_evidence_chronology_flags": best_chronology,
            "candidate_chronology_review_count": chronology_review_count,
            "candidate_shared_archive_guid_count": shared_candidate_count,
            "best_evidence_archive_guid_shared_people": (
                shared_best_count if shared_best_count > 1 else ""
            ),
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
