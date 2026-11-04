"""Review triage regression tests. All identities and records are fictional."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
import review_report as review


def fake_candidate(pid="I1", guid="doc-1", score="85", evidence="",
                   record_type="6-os karton", archive_name="Fenyvesi Ágnes"):
    return {
        "gedcom_id": pid,
        "gedcom_name": "Fenyvesi Ágnes" if pid == "I1" else "Fenyvesi Lili",
        "gedcom_birth": "17 OCT 1966",
        "gedcom_birth_place": "Pécs III., Hungary",
        "gedcom_mother": "László Teréz",
        "archive_guid": guid,
        "archive_name": archive_name,
        "archive_birth": "1966-10-17",
        "archive_birth_place": "Pécs",
        "archive_mother": "László Teréz",
        "archive_record_type": record_type,
        "match_score": score,
        "evidence": evidence,
        "archive_document_url": "https://example.invalid/doc/" + guid,
    }


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_best_candidate_only_drives_identity_contradictions(self):
        leads = [
            fake_candidate(score="91", evidence="name=45/45; exact DOB; mother exact; birthplace exact"),
            fake_candidate(guid="doc-2", score="41",
                           evidence="name=45/45; DOB conflict (year); mother name differs"),
        ]
        result = review.build_review_rows(leads)[0]
        self.assertEqual(result["highest_identity_score"], 91)
        self.assertIn("multiple_identity_leads", result["review_flags"])
        self.assertNotIn("conflicting_identity_data", result["review_flags"])
        self.assertEqual(result["distinct_archive_records"], 2)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["top_archive_guid"], "doc-1")

    def test_top_record_contradictions_and_missing_details(self):
        leads = [
            fake_candidate(evidence="name=45/45; DOB conflict (day/month); mother name differs"),
        ]
        result = review.build_review_rows(leads)[0]
        self.assertIn("conflicting_identity_data", result["review_flags"])
        self.assertEqual(result["review_order"], 0)

        absent = review.build_review_rows([
            fake_candidate(evidence="name=45/45; DOB unavailable on one side; mother unavailable on one side")
        ])[0]
        self.assertIn("needs_additional_identifiers", absent["review_flags"])

    def test_same_archive_record_shared_across_relatives(self):
        candidates = [fake_candidate("I1", "shared-doc"), fake_candidate("I2", "shared-doc")]
        result = review.build_review_rows(candidates)
        self.assertEqual({r["gedcom_id"] for r in result}, {"I1", "I2"})
        self.assertTrue(all("archive_guid_shared_by_multiple_relatives" in row["review_flags"]
                            for row in result))

    def test_coverage_includes_people_with_no_candidate_records(self):
        coverage = [
            {"gedcom_id": "I1", "gedcom_name": "Fenyvesi Ágnes",
             "search_status": "limited", "search_warnings": "too_many"},
            {"gedcom_id": "I2", "gedcom_name": "Fenyvesi Lili",
             "search_status": "not_completed"},
            {"gedcom_id": "I3", "gedcom_name": "Fenyvesi Éva",
             "search_status": "completed"},
        ]
        rows = review.build_review_rows([fake_candidate()], coverage)
        by_id = {row["gedcom_id"]: row for row in rows}
        self.assertEqual(len(rows), 3)
        self.assertEqual(by_id["I1"]["search_status"], "limited")
        self.assertIn("incomplete_search", by_id["I1"]["review_flags"])
        self.assertEqual(by_id["I2"]["candidate_count"], 0)
        self.assertEqual(by_id["I2"]["search_status"], "not_completed")
        self.assertEqual(by_id["I3"]["candidate_count"], 0)
        self.assertEqual(by_id["I3"]["review_flags"], "no_candidate_leads")

    def test_missing_coverage_is_explicit_not_reported(self):
        row = review.build_review_rows([fake_candidate()])[0]
        self.assertEqual(row["search_status"], "not_reported")

    def test_csv_roundtrip_keeps_private_sources_and_utf8(self):
        candidate_path = self.root / "matches.csv"
        coverage_path = self.root / "matches.coverage.csv"
        output = self.root / "matches.review.csv"
        original = fake_candidate()
        with candidate_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=original.keys())
            writer.writeheader()
            writer.writerow(original)
        with coverage_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["gedcom_id", "search_status"])
            writer.writeheader()
            writer.writerow({"gedcom_id": "I1", "search_status": "completed"})
        source_bytes = candidate_path.read_bytes()

        count, _stats, present = review.review_files(
            candidate_path, coverage_path, output)
        self.assertEqual(count, 1)
        self.assertTrue(present)
        self.assertEqual(candidate_path.read_bytes(), source_bytes)
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["gedcom_name"], "Fenyvesi Ágnes")
        self.assertEqual(rows[0]["top_archive_guid"], "doc-1")
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            review.review_files(candidate_path, coverage_path, candidate_path)

    def test_review_only_cli_never_calls_api_or_needs_gedcom(self):
        candidate_path = self.root / "matches.csv"
        with candidate_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fake_candidate().keys())
            writer.writeheader()
            writer.writerow(fake_candidate())
        with patch("sys.argv", ["nyitottaktak_gedcom.py", "--review-only",
                                "--review-input", str(candidate_path)]), \
             patch.object(cli, "read_gedcom",
                          side_effect=AssertionError("should not read GEDCOM")), \
             patch.object(cli.ApiClient, "_request_json",
                          side_effect=AssertionError("should not contact API")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertIn("No coverage report found", output.getvalue())
        self.assertTrue(candidate_path.with_suffix(".review.csv").exists())


if __name__ == "__main__":
    unittest.main()
