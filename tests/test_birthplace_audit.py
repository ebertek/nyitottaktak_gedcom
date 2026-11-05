"""Fictional, offline regression tests for conservative P5 birthplace research."""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
from birthplace_audit import (
    ASSESSMENTS,
    DETAIL_FIELDS,
    SUMMARY_FIELDS,
    build_birthplace_conflicts,
    build_birthplace_summary,
    write_birthplace_reports,
)
from hungarian_places import PlaceResolver


def invented(
        pid="I-TEST", guid="archive-test",
        birthplace="Subotica, North Bačka, Serbia", archive_place="Szabadka",
        evidence="name=45/45; birthplace differs",
        archive_type="6-os karton",
):
    return {
        "gedcom_id": pid, "gedcom_name": "Tesztfalvi Éva",
        "gedcom_birth": "17 Oct 1928", "gedcom_birth_place": birthplace,
        "gedcom_mother": "Mintás Katalin",
        "archive_guid": guid, "archive_name": "Tesztfalvi Éva",
        "archive_birth": "", "archive_birth_place": archive_place,
        "archive_mother": "", "archive_record_type": archive_type,
        "archive_document_url": "https://example.invalid/" + guid,
        "match_score": "45", "review_priority": "weak candidate",
        "evidence": evidence,
        "matched_name_origin": "gedcom",
    }


def write_index(root: Path) -> Path:
    index = root / "fictional-gazetteer.csv"
    with index.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "geonameid", "country", "hungarian", "native", "aliases",
        ])
        writer.writeheader()
        writer.writerows([
            {"geonameid": "101", "country": "HU", "hungarian": "Fenyőháza",
             "native": "Fenyőháza", "aliases": "Kisfalva|Fenyotanya"},
            {"geonameid": "102", "country": "RO", "hungarian": "Kisfalva",
             "native": "Kisfalva", "aliases": "Dombfalu"},
            {"geonameid": "103", "country": "HU", "hungarian": "Erdeifalu",
             "native": "Erdeifalu", "aliases": "Erdei"},
        ])
    return index


class PlaceResolverAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resolver = PlaceResolver(write_index(self.root))

    def assess(self, **kwargs):
        rows = build_birthplace_conflicts(
            [invented(**kwargs)], self.resolver, cli.normalized_place_input,
        )
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_curated_subotica_szabadka_alias_is_same_reference_locality(self):
        row = self.assess()
        self.assertEqual(row["place_assessment"], "verified_same_locality")
        self.assertEqual(row["resolution_basis"], "reviewed_alias")
        self.assertEqual(row["gedcom_entry_id"], "reviewed:subotica")
        self.assertEqual(row["archive_entry_id"], "reviewed:subotica")
        self.assertIn("rescore", row["next_research_step"])

    def test_geo_names_same_identifier_is_not_certain_identity(self):
        row = self.assess(birthplace="Fenyotanya",
                          archive_place="Fenyőháza, Hungary")
        self.assertEqual(row["place_assessment"], "verified_same_locality")
        self.assertEqual(row["resolution_basis"], "unique_gazetteer_entry")
        self.assertIn("Inspect the original", row["next_research_step"])

    def test_ambiguous_alias_preserves_possible_shared_entry(self):
        row = self.assess(birthplace="Kisfalva", archive_place="Fenyőháza")
        self.assertEqual(row["place_assessment"], "ambiguous_historical_alias")
        self.assertEqual(row["gedcom_entry_id"], "")
        self.assertEqual(row["gedcom_entry_count"], "2")
        self.assertEqual(row["shared_possible_entry_ids"], "geonames:101")
        self.assertIn("multiple places", row["next_research_step"])

    def test_country_hint_can_disambiguate_without_modifying_resolver(self):
        row = self.assess(birthplace="Kisfalva, Hungary",
                          archive_place="Fenyőháza")
        self.assertEqual(row["place_assessment"], "verified_same_locality")
        self.assertEqual(row["gedcom_entry_count"], "1")
        self.assertEqual(row["archive_entry_count"], "1")

    def test_separate_resolved_geonames_settlements_remain_distinct(self):
        row = self.assess(birthplace="Erdei", archive_place="Fenyőháza")
        self.assertEqual(row["place_assessment"], "different_locality")
        self.assertEqual(row["resolution_basis"],
                         "distinct_unique_entries_in_same_authority")
        self.assertNotEqual(row["gedcom_entry_id"], row["archive_entry_id"])

    def test_distinct_reviewed_places_are_different(self):
        row = self.assess(birthplace="Subotica", archive_place="Odžaci")
        self.assertEqual(row["place_assessment"], "different_locality")

    def test_mixed_authorities_do_not_create_false_different_place(self):
        row = self.assess(birthplace="Subotica", archive_place="Fenyőháza")
        self.assertEqual(row["place_assessment"], "unresolved_locality")
        self.assertEqual(row["resolution_basis"],
                         "conflicting_gazetteer_authorities")

    def test_place_not_in_index_stays_unresolved(self):
        row = self.assess(birthplace="Nonexistentfalu",
                          archive_place="Szegedly")
        self.assertEqual(row["place_assessment"], "unresolved_locality")
        self.assertEqual(row["gedcom_entry_count"], "0")
        self.assertEqual(row["archive_entry_count"], "0")

    def test_identical_unindexed_spelling_stays_unverified(self):
        row = self.assess(birthplace="Unlistedville",
                          archive_place="Unlistedville")
        self.assertEqual(row["place_assessment"], "unresolved_locality")
        self.assertIn("text normalizes equally",
                      row["next_research_step"].lower())

    def test_no_index_does_not_download_or_invent_place_equivalence(self):
        rows = build_birthplace_conflicts([
            invented(birthplace="Erdei", archive_place="Fenyőháza"),
        ], PlaceResolver(), cli.normalized_place_input)
        self.assertEqual(rows[0]["place_assessment"], "unresolved_locality")

    def test_district_and_accents_share_preexisting_city_normalizer(self):
        # Fictional district: current city normalization must be reused.
        row = self.assess(birthplace="Fenyőháza III., Hungary",
                          archive_place="Fenyotanya")
        self.assertEqual(row["place_assessment"], "verified_same_locality")
        self.assertEqual(row["gedcom_locality"], "Fenyőháza")

    def test_selected_p5_only_not_every_candidate_with_weak_name(self):
        p5 = invented("I-A", "bad", birthplace="Erdei",
                      archive_place="Fenyőháza")
        # Strong independent lead for the same person: selected shortlist P1.
        p1 = invented("I-A", "good", birthplace="Fenyőháza",
                      archive_place="Fenyőháza",
                      evidence="name=45/45; exact DOB; mother exact; birthplace exact")
        p1["archive_birth"] = "1928.10.17"
        p1["archive_mother"] = "Mintás Katalin"
        p1["match_score"] = "100"
        mother_only = invented(
            "I-B", "mother", evidence="name=45/45; mother name differs",
        )
        actual = build_birthplace_conflicts(
            [p5, p1, mother_only], self.resolver, cli.normalized_place_input
        )
        self.assertEqual(actual, [])

    def test_only_selected_p5_conflicts_and_no_row_mutation(self):
        data = [
            invented("I-A", "a", birthplace="Erdei",
                     archive_place="Fenyőháza"),
            invented("I-B", "b", birthplace="Kisfalva",
                     archive_place="Fenyőháza", archive_type="Mágnesszalag"),
            invented("I-C", "c", birthplace="Subotica",
                     archive_place="Szabadka"),
        ]
        original = [item.copy() for item in data]
        rows = build_birthplace_conflicts(
            data, self.resolver, cli.normalized_place_input,
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(data, original)
        summary = build_birthplace_summary(rows)
        self.assertEqual(sum(r["selected_p5_relatives"] for r in summary), 3)
        self.assertNotIn("gedcom_name", SUMMARY_FIELDS)
        self.assertNotIn("archive_guid", SUMMARY_FIELDS)
        self.assertEqual(set(r["place_assessment"] for r in rows), {
            "verified_same_locality", "ambiguous_historical_alias",
            "different_locality",
        })

    def test_empty_report_has_headers_and_zero_counts(self):
        detail = self.root / "empty.place-conflicts.csv"
        summary = self.root / "empty.place-summary.csv"
        n, counts = write_birthplace_reports(
            [], self.resolver, cli.normalized_place_input, detail, summary
        )
        self.assertEqual(n, 0)
        self.assertEqual(sum(counts.values()), 0)
        for path in (detail, summary):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_writer_keeps_detailed_ids_out_of_aggregate(self):
        detail = self.root / "private.csv"
        summary = self.root / "summary.csv"
        n, counts = write_birthplace_reports(
            [invented("SENSITIVE-ID", "private-guid")],
            self.resolver, cli.normalized_place_input, detail, summary
        )
        self.assertEqual(n, 1)
        self.assertEqual(counts["verified_same_locality"], 1)
        with detail.open(encoding="utf-8-sig", newline="") as handle:
            fields = next(csv.DictReader(handle))
        self.assertEqual(set(fields), set(DETAIL_FIELDS))
        summary_text = summary.read_text(encoding="utf-8-sig")
        self.assertNotIn("SENSITIVE-ID", summary_text)
        self.assertNotIn("private-guid", summary_text)
        self.assertNotIn("Szabadka", summary_text)
        self.assertNotIn("Subotica", summary_text)

    def test_writer_rejects_same_file_paths(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            write_birthplace_reports(
                [], self.resolver, cli.normalized_place_input,
                self.root / "same.csv", self.root / "same.csv",
            )


class PlaceCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "fictional.csv"
        self.checkpoint = self.root / "fictional.checkpoint.json"
        values = invented(birthplace="Erdei", archive_place="Fenyőháza")
        with self.source.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(values))
            writer.writeheader()
            writer.writerow(values)
        self.checkpoint.write_text('{"preserve":true}', encoding="utf-8")
        self.source_before = self.source.read_bytes()
        self.checkpoint_before = self.checkpoint.read_bytes()
        self.index = write_index(self.root)

    def invoke(self, *args):
        with patch("sys.argv", [
            "nyitottaktak_gedcom.py", "--place-index", str(self.index),
            *args,
        ]), patch.object(
            cli, "read_gedcom", side_effect=AssertionError("Unexpected GEDCOM read")
        ), patch.object(
            cli.ApiClient, "_request_json",
            side_effect=AssertionError("Unexpected API request")
        ), patch.object(
            cli, "ensure_default_index",
            side_effect=AssertionError("Unexpected GeoNames download")
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertEqual(self.source.read_bytes(), self.source_before)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_before)
        return output.getvalue()

    def test_offline_places_command_and_private_files(self):
        output = self.invoke(
            "--place-conflicts-only", "--execute",
            "--review-input", str(self.source),
        )
        self.assertIn("Offline birthplace review: 1 selected P5", output)
        self.assertIn("different_locality=1", output)
        self.assertTrue(self.source.with_suffix(".place-conflicts.csv").exists())
        self.assertTrue(self.source.with_suffix(".place-summary.csv").exists())

    def test_consistency_only_also_refreshes_birthplace_reports(self):
        output = self.invoke(
            "--consistency-only", "--review-input", str(self.source)
        )
        self.assertIn("Birthplace reconciliation: 1 selected P5", output)
        self.assertTrue(self.source.with_suffix(".place-summary.csv").exists())

    def test_review_only_refreshes_birthplace_reports(self):
        output = self.invoke("--review-only", "--review-input", str(self.source))
        self.assertIn("Birthplace reconciliation: 1 selected P5", output)
        self.assertTrue(self.source.with_suffix(".place-conflicts.csv").exists())

    def test_shortlist_only_refreshes_birthplace_reports(self):
        output = self.invoke("--shortlist-only", "--review-input", str(self.source))
        self.assertIn("Birthplace reconciliation: 1 selected P5", output)
        self.assertTrue(self.source.with_suffix(".place-conflicts.csv").exists())

    def test_birthplace_paths_protect_sources_and_checkpoint(self):
        for target in (
            self.source, self.checkpoint,
            self.source.with_suffix(".review.csv"),
            self.source.with_suffix(".collision-summary.csv"),
        ):
            for option in ("--place-conflicts-output", "--place-summary-output"):
                with self.subTest(target=target, option=option):
                    with patch("sys.argv", [
                        "nyitottaktak_gedcom.py", "--place-conflicts-only",
                        "--review-input", str(self.source),
                        option, str(target),
                    ]), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            cli.main()
                    self.assertEqual(self.source.read_bytes(), self.source_before)
                    self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_before)


if __name__ == "__main__":
    unittest.main()
