"""Scoring drift audits: fictional data, offline and input-preserving."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
from score_audit import (
    AUDIT_FIELDS, audit_candidate_file, audit_candidate_rows, write_audit_report,
)


def invented_candidate(*, score=90, evidence=None, guid="fictional-1"):
    return {
        "gedcom_id": "I-FICTIONAL",
        "gedcom_name": "Fenyvesi Ágnes",
        "gedcom_birth": "17 Oct 1966",
        "gedcom_birth_place": "Subotica, North Bačka, Serbia",
        "gedcom_mother": "László Teréz",
        "archive_guid": guid,
        "archive_name": "Fenyvesi Ágnes",
        "archive_birth": "1966.10.17",
        "archive_birth_place": "Szabadka",
        "archive_mother": "László Teréz",
        "archive_record_type": "6-os karton",
        "match_score": str(score),
        "evidence": evidence if evidence is not None else (
            "name=45/45; exact DOB; mother exact; birthplace differs"
        ),
        "review_priority": "strong candidate",
        "archive_document_url": "https://example.invalid/" + guid,
    }


class AuditUnitTests(unittest.TestCase):
    def test_detects_stale_historical_place_alias(self):
        row = invented_candidate()
        previous = row.copy()
        report = audit_candidate_rows([row], cli.recalculate_saved_candidate)
        self.assertEqual((report.checked, report.score_changed,
                          report.evidence_changed, report.classification_changed,
                          report.unverifiable), (1, 1, 1, 1, 0))
        entry = report.details[0]
        self.assertEqual(entry["score_change"], "10")
        self.assertEqual(entry["current_identity_score"], "100")
        self.assertEqual(entry["saved_classification"], "conflicting_identity")
        self.assertEqual(entry["current_classification"], "strongly_corroborated")
        self.assertIn("birthplace exact", entry["current_evidence"])
        self.assertEqual(row, previous)

    def test_evidence_only_drift_is_not_silenced(self):
        row = invented_candidate(score=100)
        report = audit_candidate_rows([row], cli.recalculate_saved_candidate)
        self.assertEqual(report.score_changed, 0)
        self.assertEqual(report.evidence_changed, 1)
        self.assertEqual(report.classification_changed, 1)
        self.assertEqual(report.details[0]["score_change"], "0")

    def test_fresh_row_is_absent_from_detail_report(self):
        row = invented_candidate(
            score=100,
            evidence="name=45/45; exact DOB; mother exact; birthplace exact",
        )
        result = audit_candidate_rows([row], cli.recalculate_saved_candidate)
        self.assertFalse(result.has_drift)
        self.assertEqual((result.checked, result.unverifiable), (1, 0))
        self.assertEqual(result.details, [])

    def test_only_harmless_evidence_whitespace_is_ignored(self):
        row = invented_candidate(
            score=100,
            evidence="name=45/45;  exact DOB;mother exact; birthplace exact  ",
        )
        result = audit_candidate_rows([row], cli.recalculate_saved_candidate)
        self.assertFalse(result.has_drift)

    def test_missing_columns_and_invalid_scores_are_not_marked_fresh(self):
        partial = invented_candidate()
        del partial["archive_birth_place"]
        invalid = invented_candidate(score="not-a-score", guid="fictional-2")
        report = audit_candidate_rows([partial, invalid], cli.recalculate_saved_candidate)
        self.assertEqual(report.checked, 0)
        self.assertEqual(report.unverifiable, 2)
        self.assertEqual(report.score_changed, 0)
        self.assertEqual({row["status"] for row in report.details}, {"unverifiable"})
        self.assertIn("archive_birth_place", report.details[0]["issue"])

    def test_audit_counts_per_candidate_not_per_person_or_archive_guid(self):
        rows = [
            invented_candidate(guid="reused"),
            invented_candidate(guid="reused"),
            invented_candidate(score=100, evidence=(
                "name=45/45; exact DOB; mother exact; birthplace exact"),
                guid="other"),
        ]
        result = audit_candidate_rows(rows, cli.recalculate_saved_candidate)
        self.assertEqual(result.checked, 3)
        self.assertEqual(result.score_changed, 2)
        self.assertEqual(len(result.details), 2)

    def test_writer_does_not_include_names_in_identifying_metadata_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.audit.csv"
            report = audit_candidate_rows([invented_candidate()],
                                          cli.recalculate_saved_candidate)
            write_audit_report(path, report)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(set(rows[0]), set(AUDIT_FIELDS))
            self.assertEqual(rows[0]["archive_guid"], "fictional-1")
            self.assertEqual(rows[0]["gedcom_id"], "I-FICTIONAL")


class AuditCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "fictional.csv"
        self.coverage = self.root / "fictional.coverage.csv"
        self.checkpoint = self.root / "fictional.checkpoint.json"
        self.write_candidates([invented_candidate()])
        self.checkpoint.write_text('{"preserve":true}', encoding="utf-8")
        self.original_csv = self.source.read_bytes()
        self.original_checkpoint = self.checkpoint.read_bytes()

    def write_candidates(self, rows):
        with self.source.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(invented_candidate()))
            writer.writeheader()
            writer.writerows(rows)

    def invoke(self, *args):
        with patch("sys.argv", ["nyitottaktak_gedcom.py", *args]), \
             patch.object(cli, "read_gedcom",
                          side_effect=AssertionError("Unexpected GEDCOM read")), \
             patch.object(cli.ApiClient, "_request_json",
                          side_effect=AssertionError("Unexpected network call")), \
             patch.object(cli, "ensure_default_index",
                          side_effect=AssertionError("Unexpected GeoNames download")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        return output.getvalue()

    def test_audit_only_writes_privately_and_preserves_input(self):
        output = self.invoke("--audit-only", "--execute",
                             "--review-input", str(self.source))
        self.assertIn("1 score changes", output)
        self.assertIn("1 classification changes", output)
        audit_csv = self.root / "fictional.audit.csv"
        self.assertTrue(audit_csv.exists())
        with audit_csv.open(newline="", encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["current_identity_score"], "100")
        self.assertEqual(self.source.read_bytes(), self.original_csv)
        self.assertEqual(self.checkpoint.read_bytes(), self.original_checkpoint)

    def test_review_and_shortlist_warn_on_stale_evidence(self):
        message = self.invoke("--review-only", "--review-input", str(self.source))
        self.assertIn("WARNING: Stored evidence differs from current scoring", message)
        self.assertIn("--rescore-only", message)
        self.assertTrue(self.source.with_suffix(".review.csv").exists())
        self.assertTrue(self.source.with_suffix(".shortlist.csv").exists())
        message = self.invoke("--shortlist-only", "--review-input", str(self.source))
        self.assertIn("WARNING: Stored evidence differs from current scoring", message)
        self.assertEqual(self.source.read_bytes(), self.original_csv)
        self.assertEqual(self.checkpoint.read_bytes(), self.original_checkpoint)

    def test_after_rescore_no_warning(self):
        message = self.invoke("--rescore-only", "--review-input", str(self.source))
        self.assertIn("1 scores changed", message)
        rescored = self.root / "fictional.rescored.csv"
        self.assertTrue(rescored.exists())
        message = self.invoke("--shortlist-only", "--review-input", str(rescored),
                              "--review-coverage-input", str(self.coverage))
        self.assertNotIn("WARNING: Stored evidence differs", message)
        self.assertEqual(self.source.read_bytes(), self.original_csv)

    def test_audit_output_collisions_are_rejected_without_mutation(self):
        for target in (self.source, self.checkpoint,
                       self.source.with_suffix(".review.csv"),
                       self.source.with_suffix(".shortlist.csv")):
            with self.subTest(target=target):
                with patch("sys.argv", [
                    "nyitottaktak_gedcom.py", "--audit-only",
                    "--review-input", str(self.source),
                    "--audit-output", str(target),
                ]), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.main()
                self.assertEqual(self.source.read_bytes(), self.original_csv)
                self.assertEqual(self.checkpoint.read_bytes(), self.original_checkpoint)

    def test_fresh_candidate_audit_writes_header_only(self):
        current = invented_candidate(
            score=100, evidence="name=45/45; exact DOB; mother exact; birthplace exact")
        self.write_candidates([current])
        output = self.invoke("--audit-only", "--review-input", str(self.source))
        self.assertIn("0 score changes", output)
        with self.source.with_suffix(".audit.csv").open(
                newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
