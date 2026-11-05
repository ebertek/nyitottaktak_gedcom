"""Private offline research shortlist derived from saved archive candidates.

A lead is a proposed identity comparison, not evidence of historical conduct.
The shortlist never merges different archive records to manufacture proof.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from review_report import (
    EVIDENCE_CLASSES,
    EVIDENCE_PRIORITY,
    build_review_rows,
    missing_tree_identifiers,
    read_csv,
    select_best_evidence,
)

# Order is only an efficient research workflow, NOT probability of identity,
# historical participation, or a statement about source trustworthiness.
PRIORITY = {
    "strongly_corroborated": ("P1", "verify_corroboration"),
    "partially_corroborated": ("P2", "seek_second_identifier"),
    "insufficient_gedcom_data": ("P3", "enrich_family_tree"),
    "insufficient_comparable_data": ("P4", "inspect_unresolved_evidence"),
    "conflicting_identity": ("P5", "resolve_identity_conflict"),
    "name_only": ("P6", "defer_name_only"),
}

FIELDS = [
    "research_priority",
    "research_task",
    "gedcom_id",
    "gedcom_name",
    "gedcom_birth",
    "gedcom_birth_place",
    "gedcom_mother",
    "gedcom_missing_identifiers",
    "missing_gedcom_for_selected_record",
    "next_research_step",
    "search_status",
    "search_warnings",
    "candidate_count",
    "distinct_archive_records",
    "candidate_strongly_corroborated",
    "candidate_partially_corroborated",
    "candidate_insufficient_gedcom_data",
    "candidate_insufficient_comparable_data",
    "candidate_conflicting_identity",
    "candidate_name_only",
    "selected_archive_guid",
    "selected_archive_name",
    "selected_archive_record_type",
    "selected_archive_birth",
    "selected_archive_birth_place",
    "selected_archive_mother",
    "selected_archive_document_url",
    "selected_archive_search_url",
    "selected_identity_score",
    "selected_evidence_classification",
    "selected_evidence",
    "selected_agreeing_identifiers",
    "selected_conflicting_identifiers",
    "selected_matched_name_origin",
    "selected_is_highest_score",
    "highest_score_archive_guid",
    "highest_score_archive_name",
    "highest_score_archive_document_url",
    "highest_identity_score",
    "highest_score_evidence_classification",
    "review_flags",
    "selected_chronology_flags",
    "chronology_review_candidate_count",
    "shared_archive_guid_candidate_count",
    "selected_archive_guid_shared_people",
    "p5_conflict_birth_date",
    "p5_conflict_mother",
    "p5_conflict_birthplace",
    "p5_conflict_count",
    "candidate_birth_date_conflicts",
    "candidate_mother_conflicts",
    "candidate_birthplace_conflicts",
]


def _next_step(
        evidence: str, missing: str, inferred: bool,
        conflicting_identifiers: str = "") -> str:
    if evidence == "strongly_corroborated":
        action = "Verify identity against the original document and independent sources"
    elif evidence == "partially_corroborated":
        action = "Check the agreeing field and seek a second independent identifier"
    elif evidence == "insufficient_gedcom_data":
        action = ("Research missing GEDCOM " + missing.replace(" | ", ", ")
                  + " and compare it with the original archival record"
                  if missing else
                  "Research the missing family-tree identifiers and compare the source")
    elif evidence == "insufficient_comparable_data":
        action = "Inspect source fields and historical date/name conventions before comparing"
    elif evidence == "conflicting_identity":
        actions = {
            "birth_date": "check birth dates against original records",
            "mother": "verify the mother's recorded name",
            "birthplace": "check historical birthplace names and locations",
        }
        conflicts = set(conflicting_identifiers.split(" | "))
        focus = [description for identifier, description in actions.items()
                 if identifier in conflicts]
        action = ("Resolve identity disagreement: " + "; ".join(focus)
                  if focus else "Verify the contradictory identifier")
        action += "; do not presume this is the same person"
    else:
        action = "Defer until independent identifying information becomes available"
    if inferred:
        action += "; verify the inferred married-name form separately"
    return action


def build_shortlist_rows(
        candidates: list[dict], coverage: list[dict] | None = None,
        *, include_name_only: bool = False) -> list[dict]:
    """One best-evidence candidate per GEDCOM person, ordered for research.

    Default output omits name-only and zero-lead rows; they remain accessible
    in the untouched candidate and grouped review CSVs. All other evidence
    categories are kept, including conflicting and inconclusive records.
    """
    by_person: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        pid = str(row.get("gedcom_id") or "").strip()
        if pid:
            by_person[pid].append(row)

    shortlist = []
    for summary in build_review_rows(candidates, coverage):
        pid = summary["gedcom_id"]
        selected = select_best_evidence(by_person[pid])
        if not selected:
            continue
        chosen, assessment = selected
        classification = assessment["classification"]
        if classification == "name_only" and not include_name_only:
            continue
        missing = missing_tree_identifiers(chosen)
        inferred = str(chosen.get("matched_name_origin") or "") == "inferred_married"
        priority, task = PRIORITY[classification]
        selected_conflicts = {
            field for field in assessment["conflicting_identifiers"].split(" | ")
            if field
        } if classification == "conflicting_identity" else set()
        row = {
            "research_priority": priority,
            "research_task": task,
            "gedcom_id": pid,
            "gedcom_name": summary["gedcom_name"],
            "gedcom_birth": summary["gedcom_birth"],
            "gedcom_birth_place": summary["gedcom_birth_place"],
            "gedcom_mother": summary["gedcom_mother"],
            "gedcom_missing_identifiers": missing,
            "missing_gedcom_for_selected_record": assessment["missing_gedcom_identifiers"],
            "next_research_step": _next_step(
                classification, assessment["missing_gedcom_identifiers"] or missing,
                inferred, assessment["conflicting_identifiers"],
            ),
            "search_status": summary["search_status"],
            "search_warnings": summary["search_warnings"],
            "candidate_count": summary["candidate_count"],
            "distinct_archive_records": summary["distinct_archive_records"],
            **{f"candidate_{name}": summary[f"candidate_{name}"]
               for name in EVIDENCE_CLASSES},
            "selected_archive_guid": str(chosen.get("archive_guid") or ""),
            "selected_archive_name": str(chosen.get("archive_name") or ""),
            "selected_archive_record_type": str(chosen.get("archive_record_type") or ""),
            "selected_archive_birth": str(chosen.get("archive_birth") or ""),
            "selected_archive_birth_place": str(chosen.get("archive_birth_place") or ""),
            "selected_archive_mother": str(chosen.get("archive_mother") or ""),
            "selected_archive_document_url": str(chosen.get("archive_document_url") or ""),
            "selected_archive_search_url": str(chosen.get("api_search_url") or ""),
            "selected_identity_score": str(chosen.get("match_score") or ""),
            "selected_evidence_classification": classification,
            "selected_evidence": str(chosen.get("evidence") or ""),
            "selected_agreeing_identifiers": assessment["agreeing_identifiers"],
            "selected_conflicting_identifiers": assessment["conflicting_identifiers"],
            "selected_matched_name_origin": str(chosen.get("matched_name_origin") or ""),
            "selected_is_highest_score": summary["best_evidence_is_highest_score"],
            "highest_score_archive_guid": summary["top_archive_guid"],
            "highest_score_archive_name": summary["top_archive_name"],
            "highest_score_archive_document_url": summary["top_archive_document_url"],
            "highest_identity_score": summary["highest_identity_score"],
            "highest_score_evidence_classification": summary["top_evidence_classification"],
            "review_flags": summary["review_flags"],
            "selected_chronology_flags": summary["best_evidence_chronology_flags"],
            "chronology_review_candidate_count": summary[
                "candidate_chronology_review_count"
            ],
            "shared_archive_guid_candidate_count": summary[
                "candidate_shared_archive_guid_count"
            ],
            "selected_archive_guid_shared_people": summary[
                "best_evidence_archive_guid_shared_people"
            ],
            "p5_conflict_birth_date": (
                "yes" if "birth_date" in selected_conflicts else ""
            ),
            "p5_conflict_mother": (
                "yes" if "mother" in selected_conflicts else ""
            ),
            "p5_conflict_birthplace": (
                "yes" if "birthplace" in selected_conflicts else ""
            ),
            "p5_conflict_count": (
                len(selected_conflicts) if classification == "conflicting_identity" else ""
            ),
            "candidate_birth_date_conflicts": summary["candidate_birth_date_conflicts"],
            "candidate_mother_conflicts": summary["candidate_mother_conflicts"],
            "candidate_birthplace_conflicts": summary["candidate_birthplace_conflicts"],
        }
        shortlist.append(row)

    shortlist.sort(key=lambda row: (
        EVIDENCE_PRIORITY[row["selected_evidence_classification"]],
        row["search_status"] in ("limited", "not_completed"),
        row["selected_matched_name_origin"] == "inferred_married",
        -int(row["selected_identity_score"] or "0"),
        row["gedcom_id"],
        row["selected_archive_guid"],
    ))
    return shortlist


def write_shortlist_report(
        target: Path, candidates: list[dict], coverage: list[dict] | None = None,
        *, include_name_only: bool = False) -> tuple[int, Counter]:
    """Atomically write a *private* shortlist; does not change source rows."""
    rows = build_shortlist_rows(candidates, coverage, include_name_only=include_name_only)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return len(rows), Counter(row["research_priority"] for row in rows)


def shortlist_files(
        candidates_path: Path, coverage_path: Path, target: Path,
        *, include_name_only: bool = False) -> tuple[int, Counter, bool]:
    """Recreate the shortlist entirely offline from a saved candidates CSV."""
    review_path = candidates_path.with_suffix(".review.csv")
    protected = (candidates_path, coverage_path, review_path)
    if target.resolve() in {p.resolve() for p in protected}:
        raise ValueError("Shortlist output must not overwrite candidate, coverage or review CSV")
    candidates = read_csv(candidates_path)
    coverage_present = coverage_path.is_file()
    coverage = read_csv(coverage_path, required=False)
    count, priorities = write_shortlist_report(
        target, candidates, coverage, include_name_only=include_name_only
    )
    return count, priorities, coverage_present
