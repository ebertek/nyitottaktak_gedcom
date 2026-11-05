"""Offline comparison of saved identity evidence with the current scoring rules.

This module never contacts the archive or loads GEDCOM data. Individual
differences are sensitive and belong only in the ignored local results folder.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from review_report import classify_identity_evidence, read_csv

# The fields required to *reproduce* a saved score from its matched GEDCOM
# name and identity attributes. Old/incomplete exports remain auditable as
# unknown rather than being silently treated as current.
REQUIRED_FIELDS = {
    "gedcom_id", "gedcom_name", "gedcom_birth", "gedcom_birth_place",
    "gedcom_mother", "archive_name", "archive_birth", "archive_birth_place",
    "archive_mother", "match_score", "evidence",
}

AUDIT_FIELDS = [
    "status", "gedcom_id", "archive_guid", "archive_record_type",
    "saved_identity_score", "current_identity_score", "score_change",
    "saved_evidence", "current_evidence",
    "saved_classification", "current_classification", "issue",
]


@dataclass
class AuditResult:
    checked: int
    score_changed: int
    evidence_changed: int
    classification_changed: int
    unverifiable: int
    details: list[dict[str, str]]

    @property
    def has_drift(self) -> bool:
        return bool(self.score_changed or self.evidence_changed)


def _canonical_evidence(value: object) -> str:
    """Ignore harmless spacing around semicolon-delimited score explanations."""
    return "; ".join(
        part.strip() for part in str(value or "").split(";") if part.strip()
    )


def audit_candidate_rows(
    candidates: list[dict],
    recalculate: Callable[[dict], tuple[int, str]],
) -> AuditResult:
    """Check existing rows in memory; do not modify source rows or checkpoints."""
    checked = score_changed = evidence_changed = classification_changed = unverifiable = 0
    details: list[dict[str, str]] = []

    for row in candidates:
        base = {
            "gedcom_id": str(row.get("gedcom_id") or ""),
            "archive_guid": str(row.get("archive_guid") or ""),
            "archive_record_type": str(row.get("archive_record_type") or ""),
            "saved_identity_score": str(row.get("match_score") or ""),
            "saved_evidence": str(row.get("evidence") or ""),
        }
        missing = sorted(REQUIRED_FIELDS - row.keys())
        issue = ", ".join(missing) if missing else ""
        if not issue:
            try:
                saved_score = int(str(row["match_score"]))
                if not 0 <= saved_score <= 100:
                    raise ValueError("score outside 0..100")
                current_score, current_evidence = recalculate(row)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                issue = f"could not reproduce saved score: {type(exc).__name__}"

        if issue:
            unverifiable += 1
            details.append({
                **base, "status": "unverifiable", "issue": issue,
                "current_identity_score": "", "score_change": "",
                "current_evidence": "", "saved_classification": "",
                "current_classification": "",
            })
            continue

        checked += 1
        changed_score = saved_score != current_score
        changed_evidence = (
            _canonical_evidence(row.get("evidence")) !=
            _canonical_evidence(current_evidence)
        )
        if not (changed_score or changed_evidence):
            continue

        score_changed += changed_score
        evidence_changed += changed_evidence
        refreshed = dict(row)
        refreshed["match_score"] = current_score
        refreshed["evidence"] = current_evidence
        old_class = classify_identity_evidence(row)["classification"]
        new_class = classify_identity_evidence(refreshed)["classification"]
        classification_changed += old_class != new_class
        details.append({
            **base, "status": "stale",
            "current_identity_score": str(current_score),
            "score_change": str(current_score - saved_score),
            "current_evidence": current_evidence,
            "saved_classification": old_class,
            "current_classification": new_class,
            "issue": "",
        })

    return AuditResult(
        checked, score_changed, evidence_changed,
        classification_changed, unverifiable, details,
    )


def audit_candidate_file(
    source: Path, recalculate: Callable[[dict], tuple[int, str]],
) -> AuditResult:
    """Audit a saved candidate CSV without writing or changing it."""
    return audit_candidate_rows(read_csv(source), recalculate)


def write_audit_report(target: Path, result: AuditResult) -> None:
    """Write only stale/unverifiable rows; keep GEDCOM identifiers private."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS)
            writer.writeheader()
            writer.writerows(result.details)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
