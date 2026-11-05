"""Chronological plausibility / shared-GUID regression tests: fictional people only."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
from consistency_audit import (
    B_NAPLO_SCOPE,
    CONSISTENCY_FIELDS,
    SHARED_FIELDS,
    archive_scope,
    birth_year,
    chronology,
    consistency_rows,
    shared_record_rows,
    write_consistency_reports,
)
from research_shortlist import build_shortlist_rows
from review_report import build_review_rows


def invented(
        pid="I1", guid="fictional-document",
        born="ABT 1866", archive_birth="", source="Mágnesszalag",
        evidence="name=45/45; DOB unavailable on one side; "
                 "mother unavailable on one side; birthplace unavailable on one side",
        score="45"):
    return {
        "gedcom_id": pid,
        "gedcom_name": "Próbafalvi Mária",
        "gedcom_birth": born,
        "gedcom_birth_place": "",
        "gedcom_mother": "",
        "archive_guid": guid,
        "archive_name": "Próbafalvi Mária",
        "archive_birth": archive_birth,
        "archive_birth_place": "",
        "archive_mother": "",
        "archive_record_type": source,
        "archive_document_url": "https://example.invalid/document/" + guid,
        "match_score": score,
        "review_priority": "weak candidate",
        "evidence": evidence,
    }


class ChronologyTests(unittest.TestCase):
    def test_old_cohort_flag_is_a_review_prompt_not_exclusion(self):
        row = invented(born="ABT 1866")
        before = row.copy()
        result = chronology(row)
        self.assertIn("older_birth_cohort_review", result["chronology_flags"])
        self.assertEqual(result["age_at_scope_start"], "78")
        self.assertEqual(result["scope_start"], "1944")
        self.assertIn("retrospective", result["review_explanation"])
        self.assertIn("approximate", result["review_explanation"])
        self.assertEqual(row, before)

    def test_examples_from_nineteenth_century(self):
        for year in (1853, 1866, 1877):
            with self.subTest(year=year):
                self.assertIn(
                    "older_birth_cohort_review",
                    chronology(invented(born=str(year)))["chronology_flags"],
                )

    def test_b_naplo_uses_narrower_source_scope_only(self):
        row = invented(born="1890", source="B napló")
        actual = chronology(row)
        self.assertEqual(archive_scope("B napló"), B_NAPLO_SCOPE)
        self.assertEqual(actual["scope_start"], "1956")
        self.assertEqual(actual["scope_end"], "1962")
        self.assertEqual(actual["age_at_scope_start"], "66")
        self.assertIn("older_birth_cohort_review", actual["chronology_flags"])
        self.assertEqual(archive_scope("6-os karton"), (1944, 1990))

    def test_typical_age_is_not_flagged(self):
        result = chronology(invented(born="1928"))
        self.assertEqual(result["chronology_flags"], "")
        self.assertEqual(result["age_at_scope_start"], "16")

    def test_future_birth_year_prompts_source_date_review(self):
        result = chronology(invented(born="2001", source="6-os karton"))
        self.assertIn("born_after_archive_scope", result["chronology_flags"])
        self.assertNotIn("older_birth_cohort_review", result["chronology_flags"])

    def test_year_discrepancy_detected_without_using_score_text(self):
        result = chronology(invented(born="1928", archive_birth="1937.10.21"))
        self.assertEqual(result["chronology_flags"], "birth_year_discrepancy")
        self.assertEqual(result["archive_birth_year"], "1937")

    def test_near_year_difference_is_not_labelled_impossible(self):
        result = chronology(invented(born="1928", archive_birth="1929.10.21"))
        self.assertEqual(result["chronology_flags"], "")

    def test_missing_and_two_digit_years_not_assigned_ambiguous_century(self):
        for date_text in ("", "02-Jun-29", "unknown", "??"):
            with self.subTest(date_text=date_text):
                self.assertIsNone(birth_year(date_text))
                result = chronology(invented(born=date_text))
                self.assertEqual(result["chronology_flags"], "")
                self.assertEqual(result["age_at_scope_start"], "")

    def test_chronology_does_not_mix_birth_years_from_other_candidates(self):
        first = invented(pid="I1", guid="card", born="1928", archive_birth="1928.10.21")
        second = invented(pid="I2", guid="tape", born="1928", archive_birth="1938.10.21")
        flags = {row["archive_guid"]: row["chronology_flags"]
                 for row in consistency_rows([first, second])}
        self.assertNotIn("card", flags)
        self.assertIn("birth_year_discrepancy", flags["tape"])


class SharedArchiveTests(unittest.TestCase):
    def test_reused_guid_across_people_is_reported_once(self):
        sources = [
            invented("I1", "shared-1", born="1900"),
            invented("I1", "shared-1", born="1900"),
            invented("I2", "shared-1", born="1928"),
            invented("I3", "unique-1", born="1929"),
            invented("I4", "", born="1928"),
            invented("I5", "", born="1901"),
        ]
        results = shared_record_rows(sources)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["archive_guid"], "shared-1")
        self.assertEqual(results[0]["distinct_gedcom_people"], 2)
        self.assertEqual(results[0]["gedcom_ids"], "I1 | I2")
        self.assertEqual(results[0]["birth_year_span"], "28")
        self.assertEqual(results[0]["different_birth_years"], "yes")
        self.assertIn("may mention several people", results[0]["review_explanation"])
        source_rows = consistency_rows(sources)
        self.assertEqual(sum(r["shared_archive_guid"] == "yes" for r in source_rows), 3)
        self.assertNotIn("", {r["archive_guid"] for r in source_rows
                               if r["shared_archive_guid"] == "yes"})

    def test_same_guid_same_person_or_only_unknown_ids_is_not_shared(self):
        self.assertEqual(shared_record_rows([
            invented("I1", "one"), invented("I1", "one"),
            invented("", "two"), invented("", "two"),
        ]), [])

    def test_shared_guid_with_no_birth_year_does_not_fabricate_discrepancy(self):
        actual = shared_record_rows([
            invented("I1", "same", born=""),
            invented("I2", "same", born=""),
        ])[0]
        self.assertEqual(actual["birth_years"], "") if "birth_years" in actual else None
        self.assertEqual(actual["gedcom_birth_years"], "")
        self.assertEqual(actual["birth_year_span"], "")
        self.assertEqual(actual["different_birth_years"], "no")

    def test_same_gedcom_birth_year_does_not_claim_two_people_are_distinct_humans(self):
        actual = shared_record_rows([
            invented("I1", "shared", born="1928"),
            invented("I2", "shared", born="1928"),
        ])[0]
        self.assertEqual(actual["different_birth_years"], "no")
        self.assertEqual(actual["birth_year_span"], "0")
        self.assertIn("duplicate", actual["review_explanation"])

    def test_existing_review_and_shortlist_show_consistency_without_rescoring(self):
        sources = [
            invented("I1", "shared", born="1866"),
            invented("I2", "shared", born="1928"),
        ]
        grouped = {row["gedcom_id"]: row for row in build_review_rows(sources)}
        self.assertEqual(grouped["I1"]["candidate_shared_archive_guid_count"], 1)
        self.assertEqual(grouped["I1"]["best_evidence_archive_guid_shared_people"], 2)
        self.assertEqual(grouped["I1"]["candidate_chronology_review_count"], 1)
        self.assertIn("older_birth_cohort_review",
                      grouped["I1"]["best_evidence_chronology_flags"])
        self.assertEqual(grouped["I2"]["candidate_chronology_review_count"], 0)
        shortlist = build_shortlist_rows(sources, include_name_only=True)
        first = next(row for row in shortlist if row["gedcom_id"] == "I1")
        self.assertIn("older_birth_cohort_review",
                      first["selected_chronology_flags"])
        self.assertEqual(first["selected_archive_guid_shared_people"], 2)


class OfflineCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate_path = self.root / "fictional.csv"
        self.checkpoint_path = self.root / "fictional.checkpoint.json"
        rows = [
            invented("I1", "shared", born="1866"),
            invented("I2", "shared", born="1928"),
        ]
        with self.candidate_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.checkpoint_path.write_text('{"keep":true}', encoding="utf-8")
        self.source_bytes = self.candidate_path.read_bytes()
        self.checkpoint_bytes = self.checkpoint_path.read_bytes()

    def run_cli(self, *flags):
        with patch("sys.argv", ["nyitottaktak_gedcom.py", *flags]), \
             patch.object(cli, "read_gedcom",
                          side_effect=AssertionError("No GEDCOM access")), \
             patch.object(cli.ApiClient, "_request_json",
                          side_effect=AssertionError("No network access")), \
             patch.object(cli, "ensure_default_index",
                          side_effect=AssertionError("No GeoNames download")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main()
        return out.getvalue()

    def test_offline_consistency_reports_privately_and_preserves_sources(self):
        output = self.run_cli(
            "--consistency-only", "--execute",
            "--review-input", str(self.candidate_path)
        )
        self.assertIn("2 flagged candidate rows", output)
        self.assertIn("1 shared archive GUIDs", output)
        consistency = self.root / "fictional.consistency.csv"
        shared = self.root / "fictional.shared-records.csv"
        with consistency.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), set(CONSISTENCY_FIELDS))
        with shared.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), set(SHARED_FIELDS))
        self.assertEqual(self.candidate_path.read_bytes(), self.source_bytes)
        self.assertEqual(self.checkpoint_path.read_bytes(), self.checkpoint_bytes)

    def test_review_and_shortlist_regenerate_consistency_files(self):
        self.run_cli("--review-only", "--review-input", str(self.candidate_path))
        self.assertTrue(self.root.joinpath("fictional.review.csv").is_file())
        self.assertTrue(self.root.joinpath("fictional.shortlist.csv").is_file())
        self.assertTrue(self.root.joinpath("fictional.consistency.csv").is_file())
        self.assertTrue(self.root.joinpath("fictional.shared-records.csv").is_file())
        self.assertEqual(self.candidate_path.read_bytes(), self.source_bytes)

    def test_consistency_output_collisions_are_rejected(self):
        for output in (
            self.candidate_path, self.checkpoint_path,
            self.candidate_path.with_suffix(".review.csv"),
        ):
            with self.subTest(output=output):
                with patch("sys.argv", [
                    "nyitottaktak_gedcom.py", "--consistency-only",
                    "--review-input", str(self.candidate_path),
                    "--consistency-output", str(output),
                ]), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.main()
                self.assertEqual(self.candidate_path.read_bytes(), self.source_bytes)
                self.assertEqual(self.checkpoint_path.read_bytes(), self.checkpoint_bytes)

    def test_report_builder_rejects_same_output_for_both_reports(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            write_consistency_reports([], self.root / "same.csv", self.root / "same.csv")


if __name__ == "__main__":
    unittest.main()
