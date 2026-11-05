"""Fictional-only offline tests for shared-record evidence and P5 triage."""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
from collision_analysis import (
    AGGREGATE_FIELDS,
    TRIAGE_FIELDS,
    build_collision_summary,
    build_collision_triage,
    collision_profiles,
    write_collision_reports,
)
from research_shortlist import build_shortlist_rows
from review_report import build_review_rows


def lead(
    pid="I1", guid="sample-guid", source="B napló",
    kind="name_only", *, score="45",
):
    row = {
        "gedcom_id": pid,
        "gedcom_name": "Mintafalvi Éva",
        "gedcom_birth": "17 OCT 1928",
        "gedcom_birth_place": "Pécs",
        "gedcom_mother": "Próbaházi Mária",
        "archive_guid": guid,
        "archive_name": "Mintafalvi Éva",
        "archive_birth": "",
        "archive_birth_place": "",
        "archive_mother": "",
        "archive_record_type": source,
        "archive_document_url": "https://example.invalid/doc/" + guid,
        "match_score": score,
        "review_priority": "weak candidate",
        "matched_name_origin": "gedcom",
        "evidence": (
            "name=45/45; DOB unavailable on one side; "
            "mother unavailable on one side; birthplace unavailable on one side"
        ),
    }
    if kind == "strong":
        row.update(
            archive_birth="1928.10.17", archive_birth_place="Pécs",
            archive_mother="Próbaházi Mária", match_score="100",
            evidence="name=45/45; exact DOB; mother exact; birthplace exact",
        )
    elif kind == "partial":
        row.update(
            archive_mother="Próbaházi Mária", match_score="60",
            evidence="name=45/45; mother exact",
        )
    elif kind == "conflict_birth":
        row.update(
            archive_birth="1937.10.17", match_score="35",
            evidence="name=45/45; DOB conflict (year)",
        )
    elif kind == "conflict_mother":
        row.update(
            archive_mother="Teszt Mária", match_score="35",
            evidence="name=45/45; mother name differs",
        )
    elif kind == "conflict_place":
        row.update(
            archive_birth_place="Szeged", match_score="35",
            evidence="name=45/45; birthplace differs",
        )
    elif kind == "conflict_all":
        row.update(
            archive_birth="1937.10.17", archive_mother="Teszt Mária",
            archive_birth_place="Szeged", match_score="35",
            evidence=(
                "name=45/45; DOB conflict (year); "
                "mother name differs; birthplace differs"
            ),
        )
    elif kind == "missing_gedcom":
        row.update(
            gedcom_birth="", gedcom_birth_place="", gedcom_mother="",
            archive_birth_place="Pécs", archive_mother="Próbaházi Mária",
            evidence="name=45/45; mother unavailable on one side; "
                     "birthplace unavailable on one side",
        )
    elif kind != "name_only":
        raise ValueError(kind)
    return row


class CollisionProfilesTests(unittest.TestCase):
    def test_name_only_shared_guid_is_deferred(self):
        rows = [
            lead("I1", "book"),
            lead("I2", "book"),
            lead("I2", "book"),  # repeated query must not imply three people
        ]
        profiles = collision_profiles(rows)
        self.assertEqual(profiles["book"]["profile"], "name_only_collision")
        item = build_collision_triage(rows)[0]
        self.assertEqual(item["distinct_gedcom_people"], 2)
        self.assertEqual(item["candidate_rows"], 3)
        self.assertEqual(item["name_only_only_people"], 2)
        self.assertEqual(item["strongly_corroborated_people"], 0)
        self.assertIn("Defer", item["review_action"])

    def test_corrob_present_promoted_over_name_only_overlap(self):
        rows = [
            lead("I1", "card", "6-os karton", "strong"),
            lead("I2", "card", "6-os karton", "conflict_birth"),
            lead("I3", "card", "6-os karton", "name_only"),
        ]
        item = build_collision_triage(rows)[0]
        self.assertEqual(item["collision_profile"], "corroborated_lead_present")
        self.assertEqual(item["strongly_corroborated_people"], 1)
        self.assertEqual(item["name_only_only_people"], 1)
        self.assertEqual(item["conflicting_identity_people"], 1)
        self.assertEqual(item["distinct_gedcom_people"], 3)
        self.assertIn("separately", item["review_action"])

    def test_partial_corrob_also_triggers_research_not_automatic_match(self):
        rows = [
            lead("I1", "tape", "Mágnesszalag", "partial"),
            lead("I2", "tape", "Mágnesszalag", "name_only"),
        ]
        item = build_collision_triage(rows)[0]
        self.assertEqual(item["collision_profile"], "corroborated_lead_present")
        self.assertEqual(item["partially_corroborated_people"], 1)

    def test_missing_gedcom_and_conflicting_values_are_other_ambiguous(self):
        rows = [
            lead("I1", "tape", "Mágnesszalag", "missing_gedcom"),
            lead("I2", "tape", "Mágnesszalag", "conflict_mother"),
        ]
        item = build_collision_triage(rows)[0]
        self.assertEqual(item["collision_profile"], "other_ambiguous_collision")
        self.assertEqual(item["insufficient_gedcom_data_people"], 1)
        self.assertEqual(item["conflicting_identity_people"], 1)

    def test_guid_is_global_across_sources_and_evidence_buckets(self):
        rows = [
            lead("I1", "cross", "B napló", "name_only"),
            lead("I2", "cross", "6-os karton", "strong"),
            lead("I3", "unique", "B napló", "name_only"),
        ]
        summary = {(r["archive_record_type"], r["evidence_classification"]): r
                   for r in build_collision_summary(rows)}
        book = summary[("B napló", "name_only")]
        self.assertEqual(book["candidate_rows"], 2)
        self.assertEqual(book["distinct_nonempty_archive_guids"], 2)
        self.assertEqual(book["shared_guid_candidate_rows"], 1)
        self.assertEqual(book["shared_archive_guids"], 1)
        self.assertEqual(book["shared_with_corroboration_guids"], 1)
        self.assertEqual(book["shared_name_only_only_guids"], 0)
        card = summary[("6-os karton", "strongly_corroborated")]
        self.assertEqual(card["shared_with_corroboration_guids"], 1)
        self.assertEqual(len(build_collision_triage(rows)), 1)

    def test_different_sources_and_classes_distinct_counts(self):
        rows = [
            lead("I1", "same"),
            lead("I2", "same"),
            lead("I2", "same"),
            lead("I3", "other", "Mágnesszalag", "missing_gedcom"),
            lead("I4", "other", "Mágnesszalag", "conflict_mother"),
        ]
        summary = {(r["archive_record_type"], r["evidence_classification"]): r
                   for r in build_collision_summary(rows)}
        book = summary[("B napló", "name_only")]
        self.assertEqual(book["candidate_rows"], 3)
        self.assertEqual(book["shared_guid_candidate_rows"], 3)
        self.assertEqual(book["shared_name_only_only_guids"], 1)
        other = summary[("Mágnesszalag", "conflicting_identity")]
        self.assertEqual(other["shared_other_guids"], 1)
        self.assertEqual(other["shared_with_corroboration_guids"], 0)
        self.assertEqual(
            sum(x["candidate_rows"] for x in summary.values()), len(rows)
        )

    def test_blank_and_same_person_guid_do_not_create_false_collisions(self):
        rows = [
            lead("I1", ""),
            lead("I2", ""),
            lead("I3", "solo"),
            lead("I3", "solo"),
        ]
        self.assertEqual(build_collision_triage(rows), [])
        summary = build_collision_summary(rows)
        self.assertTrue(all(x["shared_archive_guids"] == 0 for x in summary))

    def test_no_rows_produces_empty_analysis(self):
        self.assertEqual(build_collision_summary([]), [])
        self.assertEqual(build_collision_triage([]), [])

    def test_guid_profile_independent_of_other_archive_guids(self):
        rows = [
            lead("I1", "shared", "Mágnesszalag", "conflict_birth"),
            lead("I2", "shared", "Mágnesszalag", "missing_gedcom"),
            lead("I1", "unrelated", "6-os karton", "strong"),
        ]
        self.assertEqual(
            build_collision_triage(rows)[0]["collision_profile"],
            "other_ambiguous_collision",
        )


class P5ConflictTests(unittest.TestCase):
    def test_combined_birth_date_mother_and_birthplace_conflicts(self):
        chosen = lead("I1", "bad", "6-os karton", "conflict_all")
        extra = lead("I1", "other", "Mágnesszalag", "conflict_mother")
        rows = build_shortlist_rows([chosen, extra])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["research_priority"], "P5")
        self.assertEqual(r["p5_conflict_birth_date"], "yes")
        self.assertEqual(r["p5_conflict_mother"], "yes")
        self.assertEqual(r["p5_conflict_birthplace"], "yes")
        self.assertEqual(r["p5_conflict_count"], 3)
        self.assertIn("birth dates", r["next_research_step"])
        self.assertIn("mother's recorded name", r["next_research_step"])
        self.assertIn("historical birthplace", r["next_research_step"])
        self.assertEqual(r["candidate_birth_date_conflicts"], 1)
        self.assertEqual(r["candidate_mother_conflicts"], 2)
        self.assertEqual(r["candidate_birthplace_conflicts"], 1)
        review = build_review_rows([chosen, extra])[0]
        self.assertEqual(review["candidate_mother_conflicts"], 2)

    def test_p5_only_place_conflict_is_not_fabricated_dob_conflict(self):
        row = build_shortlist_rows([lead(
            "I1", "bad-place", "6-os karton", "conflict_place"
        )])[0]
        self.assertEqual(row["p5_conflict_birth_date"], "")
        self.assertEqual(row["p5_conflict_mother"], "")
        self.assertEqual(row["p5_conflict_birthplace"], "yes")
        self.assertEqual(row["p5_conflict_count"], 1)
        self.assertNotIn("birth dates", row["next_research_step"])

    def test_strong_candidate_not_downgraded_by_other_conflicting_record(self):
        rows = build_shortlist_rows([
            lead("I1", "strong", "6-os karton", "strong"),
            lead("I1", "other", "Mágnesszalag", "conflict_birth"),
        ])
        self.assertEqual(rows[0]["research_priority"], "P1")
        self.assertEqual(rows[0]["p5_conflict_birth_date"], "")
        self.assertEqual(rows[0]["p5_conflict_count"], "")
        self.assertEqual(rows[0]["candidate_birth_date_conflicts"], 1)

    def test_exact_name_and_year_match_without_two_others_is_not_p1(self):
        row = build_shortlist_rows([
            lead("I1", "partial", "Mágnesszalag", "partial"),
            lead("I1", "nameonly", "B napló"),
        ])[0]
        self.assertEqual(row["research_priority"], "P2")
        self.assertEqual(row["candidate_birth_date_conflicts"], 0)


class OfflineCollisionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "invented.csv"
        self.checkpoint = self.folder / "invented.checkpoint.json"
        rows = [
            lead("I1", "shared", "B napló"),
            lead("I2", "shared", "B napló"),
            lead("I3", "strong", "6-os karton", "strong"),
        ]
        with self.source.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.checkpoint.write_text('{"do_not_change":true}', encoding="utf-8")
        self.original = self.source.read_bytes()
        self.checkpoint_original = self.checkpoint.read_bytes()

    def invoke(self, *options):
        with patch("sys.argv", [
            "nyitottaktak_gedcom.py", *options,
        ]), patch.object(
            cli, "read_gedcom", side_effect=AssertionError("GEDCOM read")
        ), patch.object(
            cli.ApiClient, "_request_json",
            side_effect=AssertionError("Network request")
        ), patch.object(
            cli, "ensure_default_index",
            side_effect=AssertionError("Gazetteer download")
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        return output.getvalue()

    def test_consistency_only_builds_collision_reports_without_network(self):
        output = self.invoke(
            "--consistency-only", "--review-input", str(self.source),
            "--execute",
        )
        self.assertIn("Collision evidence:", output)
        self.assertIn("name_only_collision=1", output)
        aggregate = self.source.with_suffix(".collision-summary.csv")
        private = self.source.with_suffix(".collision-triage.csv")
        self.assertTrue(aggregate.exists())
        self.assertTrue(private.exists())
        with aggregate.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(set(rows[0]), set(AGGREGATE_FIELDS))
        with private.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(set(rows[0]), set(TRIAGE_FIELDS))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_original)

    def test_review_only_generates_p5_and_collision_summary(self):
        result = self.invoke("--review-only", "--review-input", str(self.source))
        self.assertIn("Collision evidence:", result)
        self.assertTrue(self.source.with_suffix(".review.csv").exists())
        self.assertTrue(self.source.with_suffix(".shortlist.csv").exists())
        self.assertTrue(self.source.with_suffix(".collision-summary.csv").exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_shortlist_only_also_regenerates_collision_summary(self):
        result = self.invoke("--shortlist-only", "--review-input", str(self.source))
        self.assertIn("Collision evidence:", result)
        self.assertTrue(self.source.with_suffix(".collision-triage.csv").exists())

    def test_collision_report_paths_cannot_overwrite_sources(self):
        for target in (self.source, self.checkpoint,
                       self.source.with_suffix(".review.csv")):
            with self.subTest(target=target):
                with patch("sys.argv", [
                    "nyitottaktak_gedcom.py", "--consistency-only",
                    "--review-input", str(self.source),
                    "--collision-triage-output", str(target),
                ]), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.main()
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_original)

    def test_collision_writer_rejects_same_output_path(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            write_collision_reports([], self.folder / "same.csv", self.folder / "same.csv")

    def test_private_summary_and_triage_separated(self):
        rows = [lead("I1", "shared"), lead("I2", "shared")]
        _count, breakdown = write_collision_reports(
            rows, self.folder / "sum.csv", self.folder / "detail.csv"
        )
        self.assertEqual(breakdown["name_only_collision"], 1)
        summary_bytes = (self.folder / "sum.csv").read_text(encoding="utf-8-sig")
        self.assertNotIn("I1", summary_bytes)
        self.assertNotIn("I2", summary_bytes)
        self.assertNotIn("shared", summary_bytes.lower().splitlines()[1])
        detail_bytes = (self.folder / "detail.csv").read_text(encoding="utf-8-sig")
        self.assertIn("I1", detail_bytes)


if __name__ == "__main__":
    unittest.main()
