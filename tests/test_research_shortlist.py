"""Private research shortlist tests using invented archival identities only."""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
from research_shortlist import (
    FIELDS,
    build_shortlist_rows,
    shortlist_files,
)
from review_report import build_review_rows


def lead(
    pid="I1", guid="fictional-card-1", score="45",
    evidence="name=45/45; DOB unavailable on one side; "
             "mother unavailable on one side; birthplace unavailable on one side",
    *, source="B napló", birth="", place="", mother="",
    gedcom_birth="17 OCT 1960", gedcom_place="Pécs",
    gedcom_mother="Mintás Mária", origin="gedcom",
):
    return {
        "gedcom_id": pid,
        "gedcom_name": "Próbaházi Ágnes",
        "gedcom_birth": gedcom_birth,
        "gedcom_birth_place": gedcom_place,
        "gedcom_mother": gedcom_mother,
        "archive_guid": guid,
        "archive_name": "Próbaházi Ágnes",
        "archive_birth": birth,
        "archive_birth_place": place,
        "archive_mother": mother,
        "archive_record_type": source,
        "archive_document_url": "https://example.invalid/doc/" + guid,
        "api_search_url": "https://example.invalid/search",
        "matched_name_origin": origin,
        "match_score": score,
        "evidence": evidence,
    }


class ShortlistRankingTests(unittest.TestCase):
    def test_best_corrob_can_beat_higher_scored_conflicting_record(self):
        conflicting = lead(
            guid="bad", score="90", source="Mágnesszalag",
            evidence="name=45/45; exact DOB; mother exact; birthplace differs",
            birth="1960.10.17", mother="Mintás Mária", place="Szeged",
        )
        strong = lead(
            guid="good", score="75", source="6-os karton",
            evidence="name=45/45; mother exact; birthplace exact",
            mother="Mintás Mária", place="Pécs",
        )
        name_only = lead(guid="weak", score="45")
        original = [conflicting.copy(), strong.copy(), name_only.copy()]
        grouped = build_review_rows([conflicting, strong, name_only])[0]
        self.assertEqual(grouped["highest_identity_score"], 90)
        self.assertEqual(grouped["top_archive_guid"], "bad")
        self.assertEqual(grouped["top_evidence_classification"], "conflicting_identity")
        self.assertEqual(grouped["best_evidence_archive_guid"], "good")
        self.assertEqual(grouped["best_evidence_classification"], "strongly_corroborated")
        self.assertEqual(grouped["best_evidence_identity_score"], 75)
        self.assertEqual(grouped["best_evidence_is_highest_score"], "no")

        row = build_shortlist_rows([conflicting, strong, name_only])[0]
        self.assertEqual(row["research_priority"], "P1")
        self.assertEqual(row["selected_archive_guid"], "good")
        self.assertEqual(row["selected_archive_document_url"],
                         "https://example.invalid/doc/good")
        self.assertEqual(row["selected_is_highest_score"], "no")
        self.assertEqual(row["highest_score_archive_guid"], "bad")
        self.assertEqual(row["highest_identity_score"], 90)
        self.assertEqual(row["highest_score_evidence_classification"],
                         "conflicting_identity")
        self.assertEqual(row["candidate_conflicting_identity"], 1)
        self.assertEqual(row["candidate_name_only"], 1)
        self.assertEqual([conflicting, strong, name_only], original)

    def test_one_row_per_person_and_defer_name_only_by_default(self):
        candidates = [
            lead(pid="I1", guid="a", score="70",
                 evidence="name=45/45; mother exact",
                 mother="Mintás Mária"),
            lead(pid="I1", guid="b", score="45"),
            lead(pid="I2", guid="c", score="45"),
        ]
        coverage = [
            {"gedcom_id": "I1", "gedcom_name": "Próbaházi Ágnes",
             "search_status": "completed"},
            {"gedcom_id": "I2", "gedcom_name": "Tesztfalvi Éva",
             "search_status": "completed"},
            {"gedcom_id": "I3", "gedcom_name": "Minta Ödön",
             "search_status": "not_completed"},
        ]
        rows = build_shortlist_rows(candidates, coverage)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gedcom_id"], "I1")
        self.assertEqual(rows[0]["research_priority"], "P2")
        self.assertEqual(rows[0]["candidate_count"], 2)
        self.assertEqual(rows[0]["search_status"], "completed")
        all_rows = build_shortlist_rows(candidates, coverage, include_name_only=True)
        self.assertEqual([row["gedcom_id"] for row in all_rows], ["I1", "I2"])
        self.assertEqual(all_rows[1]["research_priority"], "P6")
        self.assertEqual(all_rows[1]["research_task"], "defer_name_only")

    def test_missing_tree_fields_and_archival_comparison_needs(self):
        rows = build_shortlist_rows([
            lead(
                pid="I4", guid="missing", score="45",
                evidence="name=45/45; DOB unavailable on one side; "
                         "mother unavailable on one side; birthplace unavailable on one side",
                birth="1960.10.17", place="Pécs", mother="Mintás Mária",
                gedcom_birth="", gedcom_place="", gedcom_mother="",
            ),
        ])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["research_priority"], "P3")
        self.assertEqual(row["gedcom_missing_identifiers"],
                         "birth_date | birthplace | mother")
        self.assertEqual(row["missing_gedcom_for_selected_record"],
                         "birth_date | birthplace | mother")
        self.assertIn("birth_date, birthplace, mother", row["next_research_step"])
        self.assertEqual(row["selected_evidence_classification"],
                         "insufficient_gedcom_data")

    def test_missing_tree_fields_include_attributes_not_in_archive(self):
        result = build_shortlist_rows([
            lead(pid="I7", guid="partial", score="60",
                 evidence="name=45/45; mother exact",
                 mother="Mintás Mária", gedcom_birth="", gedcom_place=""),
        ])[0]
        self.assertEqual(result["research_priority"], "P2")
        self.assertEqual(result["gedcom_missing_identifiers"],
                         "birth_date | birthplace")
        self.assertEqual(result["missing_gedcom_for_selected_record"], "")

    def test_inferred_married_name_cannot_become_documented_identity(self):
        row = build_shortlist_rows([
            lead(pid="I8", guid="inferred", score="85",
                 evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs", origin="inferred_married"),
        ])[0]
        self.assertEqual(row["selected_matched_name_origin"], "inferred_married")
        self.assertIn("verify the inferred married-name form separately",
                      row["next_research_step"])
        self.assertIn("inferred_married_name", row["review_flags"])

    def test_documented_candidate_wins_tie_before_inferred_name(self):
        rows = build_shortlist_rows([
            lead(guid="inferred", score="95",
                 evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs", origin="inferred_married"),
            lead(guid="documented", score="75",
                 evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs", origin="gedcom"),
        ])
        self.assertEqual(rows[0]["selected_archive_guid"], "documented")

    def test_priorities_and_deterministic_sorting(self):
        rows = build_shortlist_rows([
            lead(pid="I4", guid="conflict", score="80",
                 evidence="name=45/45; mother name differs",
                 mother="Other Mária"),
            lead(pid="I2", guid="missing", score="45",
                 evidence="name=45/45; mother unavailable on one side",
                 mother="Mintás Mária", gedcom_mother=""),
            lead(pid="I1", guid="strong", score="75",
                 evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs"),
            lead(pid="I3", guid="unknown", score="45",
                 evidence="name=45/45; DOB unavailable on one side",
                 birth="unknown"),
        ])
        self.assertEqual(
            [(r["gedcom_id"], r["research_priority"]) for r in rows],
            [("I1", "P1"), ("I2", "P3"), ("I3", "P4"), ("I4", "P5")],
        )

    def test_no_leads_produces_header_only(self):
        self.assertEqual(build_shortlist_rows([], [{
            "gedcom_id": "I1", "search_status": "completed",
        }]), [])


class OfflineShortlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.candidates = self.root / "fictional.csv"
        self.coverage = self.root / "fictional.coverage.csv"
        self.shortlist = self.root / "fictional.shortlist.csv"

    def write_candidates(self, rows):
        columns = list(lead())
        with self.candidates.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def test_source_unchanged_and_csv_columns(self):
        self.write_candidates([
            lead(guid="one", score="75",
                 evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs"),
        ])
        self.coverage.write_text("gedcom_id,search_status\nI1,completed\n",
                                 encoding="utf-8")
        original = self.candidates.read_bytes()
        count, priorities, present = shortlist_files(
            self.candidates, self.coverage, self.shortlist)
        self.assertEqual((count, present), (1, True))
        self.assertEqual(priorities["P1"], 1)
        self.assertEqual(self.candidates.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.checkpoint.json")), [])
        with self.shortlist.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(set(rows[0]), set(FIELDS))
        self.assertEqual(rows[0]["research_priority"], "P1")

    def test_prevents_overwriting_candidate_coverage_or_review(self):
        self.write_candidates([lead()])
        for target in (
                self.candidates, self.coverage,
                self.candidates.with_suffix(".review.csv")):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "must not overwrite"):
                    shortlist_files(self.candidates, self.coverage, target)

    def test_missing_coverage_is_not_falsely_called_completed(self):
        self.write_candidates([
            lead(score="75", evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs"),
        ])
        _count, _priorities, present = shortlist_files(
            self.candidates, self.coverage, self.shortlist)
        self.assertFalse(present)
        with self.shortlist.open(newline="", encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["search_status"], "not_reported")

    def test_shortlist_only_cli_is_offline_and_works_without_gedcom(self):
        self.write_candidates([
            lead(score="75", evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs"),
        ])
        with patch("sys.argv", [
            "nyitottaktak_gedcom.py", "--shortlist-only",
            "--review-input", str(self.candidates), "--execute",
            "--cache-dir", str(self.root / "cache"),
        ]), patch.object(cli, "read_gedcom",
                        side_effect=AssertionError("unexpected GEDCOM read")), \
             patch.object(cli.ApiClient, "_request_json",
                          side_effect=AssertionError("unexpected HTTP request")), \
             patch.object(cli, "ensure_default_index",
                          side_effect=AssertionError("unexpected GeoNames download")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertIn("Offline research shortlist: 1 relatives", output.getvalue())
        self.assertTrue(self.shortlist.exists())

    def test_review_only_generates_shortlist_without_touching_candidates(self):
        self.write_candidates([
            lead(score="75", evidence="name=45/45; mother exact; birthplace exact",
                 mother="Mintás Mária", place="Pécs"),
        ])
        original = self.candidates.read_bytes()
        with patch("sys.argv", [
            "nyitottaktak_gedcom.py", "--review-only",
            "--review-input", str(self.candidates),
        ]), patch.object(cli, "read_gedcom",
                        side_effect=AssertionError("unexpected GEDCOM read")), \
             patch.object(cli.ApiClient, "_request_json",
                          side_effect=AssertionError("unexpected API access")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertIn("Offline research shortlist:", output.getvalue())
        self.assertEqual(self.candidates.read_bytes(), original)
        self.assertTrue(self.candidates.with_suffix(".review.csv").exists())
        self.assertTrue(self.shortlist.exists())


if __name__ == "__main__":
    unittest.main()
