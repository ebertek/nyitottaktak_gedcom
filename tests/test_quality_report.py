"""Offline quality-report tests and deterministic matching regressions.

Names are clearly fictional; tests do not read live archives or user trees.
"""

from __future__ import annotations

import contextlib
import csv
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as finder
from quality_report import FIELDS, quality_rows, write_quality_report


def fake_row(
    *,
    pid="F1",
    guid="fictional-guid",
    kind="6-os karton",
    name="Próbaházi Ágnes",
    birth="1966.10.17",
    place="Pécs",
    mother="Példás Mária",
    score="100",
    evidence="name=45/45; exact DOB; mother exact; birthplace exact",
):
    return {
        "gedcom_id": pid,
        "archive_guid": guid,
        "archive_record_type": kind,
        "archive_name": name,
        "archive_birth": birth,
        "archive_birth_place": place,
        "archive_mother": mother,
        "match_score": score,
        "evidence": evidence,
    }


class QualityReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def example_rows():
        return [
            fake_row(pid="I1", guid="card-1"),
            fake_row(pid="I2", guid="card-1", score="65",
                     birth="", place="", mother="",
                     evidence="name=45/45; DOB unavailable on one side"),
            fake_row(pid="I3", guid="book-1", kind="B-napló",
                     birth="", place="", mother="", score="45",
                     evidence="name=45/45; DOB unavailable on one side; "
                              "mother unavailable on one side; birthplace unavailable on one side"),
            fake_row(pid="I4", guid="book-1", kind="B-napló",
                     birth="", place="", mother="", score="45",
                     evidence="name=45/45; DOB unavailable on one side; "
                              "mother unavailable on one side; birthplace unavailable on one side"),
            fake_row(pid="I5", guid="tape-1", kind="Mágnesszalag",
                     birth="1940", place="", mother="Példás Mária",
                     score="60", evidence="name=45/45; birth year matches; mother name differs"),
            # Missing GUIDs must never be treated as the same record.
            fake_row(pid="I6", guid="", kind="Mágnesszalag",
                     birth="", place="", mother="", score="45",
                     evidence="name=45/45"),
            fake_row(pid="I7", guid="", kind="Mágnesszalag",
                     birth="", place="", mother="", score="45",
                     evidence="name=45/45"),
        ]

    def test_counts_source_completeness_without_double_counting_guids(self):
        grouped = {row["archive_record_type"]: row
                   for row in quality_rows(self.example_rows())}
        card = grouped["6-os karton"]
        self.assertEqual(card["candidate_rows"], 2)
        self.assertEqual(card["distinct_archive_records"], 1)
        self.assertEqual(card["distinct_gedcom_people"], 2)
        self.assertEqual(card["archive_birth_pct"], "100.0")
        self.assertEqual(card["archive_mother_pct"], "100.0")
        self.assertEqual(card["candidate_with_2_plus_corroborations"], 1)

        book = grouped["B-napló"]
        self.assertEqual(book["candidate_rows"], 2)
        self.assertEqual(book["distinct_archive_records"], 1)
        self.assertEqual(book["candidate_name_only"], 2)
        self.assertEqual(book["archive_birth_pct"], "0.0")
        self.assertEqual(book["scores_35_59"], 2)

        tape = grouped["Mágnesszalag"]
        self.assertEqual(tape["distinct_archive_records"], 3)
        self.assertEqual(tape["archive_birth_partial_date"], 1)
        self.assertEqual(tape["archive_birth_full_date"], 0)
        self.assertEqual(tape["archive_mother_present"], 1)
        self.assertEqual(tape["candidate_name_only"], 2)
        self.assertEqual(tape["candidate_with_conflicting_identifiers"], 1)

        total = grouped["TOTAL (candidate sample)"]
        self.assertEqual(total["candidate_rows"], 7)
        self.assertEqual(total["distinct_archive_records"], 5)
        self.assertEqual(total["distinct_gedcom_people"], 7)
        self.assertEqual(total["candidate_name_only"], 4)

    def test_same_guid_in_different_source_is_not_merged(self):
        rows = [
            fake_row(kind="B-napló", guid="same-id"),
            fake_row(kind="6-os karton", guid="same-id"),
        ]
        total = quality_rows(rows)[-1]
        self.assertEqual(total["distinct_archive_records"], 2)

    def test_duplicate_guid_combines_field_presence_not_people(self):
        rows = [
            fake_row(guid="same", birth="1960", place="", mother=""),
            fake_row(guid="same", birth="1960.01.02", place="Szeged", mother=""),
            fake_row(guid="same", birth="", place="", mother="Minta Éva"),
        ]
        row = quality_rows(rows)[0]
        self.assertEqual(row["candidate_rows"], 3)
        self.assertEqual(row["distinct_archive_records"], 1)
        self.assertEqual(row["archive_birth_full_date"], 1)
        self.assertEqual(row["archive_birth_place_present"], 1)
        self.assertEqual(row["archive_mother_present"], 1)

    def test_empty_sample_is_header_only(self):
        self.assertEqual(quality_rows([]), [])
        src = self.root / "empty.csv"
        dst = self.root / "empty.quality.csv"
        with src.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fake_row()))
            writer.writeheader()
        result = write_quality_report(src, dst)
        self.assertEqual(result, [])
        with dst.open(newline="", encoding="utf-8-sig") as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])

    def test_output_is_only_aggregates_and_input_unchanged(self):
        src = self.root / "matches.csv"
        dst = self.root / "matches.quality.csv"
        with src.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fake_row()))
            writer.writeheader()
            writer.writerows(self.example_rows())
        original = src.read_bytes()
        result = write_quality_report(src, dst)
        self.assertEqual(len(result), 4)
        self.assertEqual(src.read_bytes(), original)
        self.assertTrue(dst.exists())
        self.assertNotIn("Próbaházi", dst.read_text(encoding="utf-8-sig"))
        self.assertNotIn("fictional-guid", dst.read_text(encoding="utf-8-sig"))
        with dst.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(set(rows[0]), set(FIELDS))
        self.assertEqual(len(rows), 4)
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            write_quality_report(src, src)

    def test_invalid_input_fails_with_actionable_error(self):
        row = fake_row(score="NaN")
        with self.assertRaisesRegex(ValueError, "match_score"):
            quality_rows([row])
        row["match_score"] = "110"
        with self.assertRaisesRegex(ValueError, "Out-of-range"):
            quality_rows([row])
        missing = self.root / "missing.csv"
        with self.assertRaisesRegex(ValueError, "not found"):
            write_quality_report(missing, self.root / "quality.csv")

    def test_cli_is_fully_offline_without_gedcom(self):
        src = self.root / "matches.csv"
        with src.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fake_row()))
            writer.writeheader()
            writer.writerow(fake_row())
        stdout = io.StringIO()
        with patch("sys.argv", [
            "nyitottaktak_gedcom.py", "--quality-only",
            "--output", str(src), "--execute",
            "--cache-dir", str(self.root / "absent-geonames"),
        ]), patch.object(finder, "ensure_default_index",
                        side_effect=AssertionError("unexpected GeoNames download")), \
                patch.object(finder.ApiClient, "_request_json",
                             side_effect=AssertionError("unexpected Nyitott Akták call")), \
                patch.object(finder, "read_gedcom",
                             side_effect=AssertionError("unexpected GEDCOM read")), \
                contextlib.redirect_stdout(stdout):
            finder.main()
        self.assertIn("Offline quality:", stdout.getvalue())
        self.assertIn("6-os karton", stdout.getvalue())
        self.assertTrue((self.root / "matches.quality.csv").is_file())


class DeterministicMatchingBenchmarkTests(unittest.TestCase):
    def test_fictional_identity_positives_and_hard_negatives(self):
        """Compare stable synthetic positives against name-only and conflicts.

        This is a regression benchmark, NOT a measured real-world precision
        or recall estimate: real ground-truth identities are unavailable.
        """
        rng = random.Random(3101956)
        positives = []
        name_only = []
        negatives = []
        for index in range(120):
            year = rng.randint(1900, 1970)
            given = ("Ágnes", "Éva", "Márton", "János")[index % 4]
            # Unique fictional surname stems prevent accidental shared names.
            name = f"Próbaházi{index} {given}"
            person = finder.Person(
                f"F{index}", born=f"17 OCT {year}",
                birthplace="Pécs III., Hungary",
            )
            valid = {
                "nev": name.upper(), "szul_ido": f"{year}.10.17",
                "szul_hely": "Pécs", "anyja_neve": "Mintás Teréz",
            }
            missing = dict(valid, szul_ido="", szul_hely="", anyja_neve="")
            contradiction = dict(valid, szul_ido=f"{year + 7}.01.02",
                                 szul_hely="Szeged",
                                 anyja_neve="Tesztelő Margit")
            positive, _, _ = finder.score(person, valid, [name], ["Mintás Teréz"])
            sparse, _, _ = finder.score(person, missing, [name], ["Mintás Teréz"])
            conflict, _, _ = finder.score(person, contradiction, [name], ["Mintás Teréz"])
            positives.append(positive)
            name_only.append(sparse)
            negatives.append(conflict)

        self.assertEqual(len(positives), 120)
        self.assertTrue(all(score == 100 for score in positives))
        self.assertTrue(all(score == 45 for score in name_only))
        self.assertTrue(all(score < 45 for score in negatives))

    def test_accents_and_name_mismatch_regressions(self):
        person = finder.Person("F1", born="17 OCT 1966",
                               birthplace="Subotica, Serbia")
        record = {"nev": "PÉLDAVÁRI ÁGNES", "szul_ido": "1966.10.17",
                  "szul_hely": "Szabadka", "anyja_neve": "MINTÁS TERÉZ"}
        score, evidence, _ = finder.score(
            person, record, ["Példavári Ágnes"], ["Mintás Teréz"])
        self.assertEqual(score, 100)
        self.assertIn("birthplace exact", evidence)

        record["nev"] = "Fiktívár Gergely"
        zero, reason, _ = finder.score(
            person, record, ["Példavári Ágnes"], ["Mintás Teréz"])
        self.assertEqual(zero, 0)
        self.assertIn("name does not match", reason)


if __name__ == "__main__":
    unittest.main()
