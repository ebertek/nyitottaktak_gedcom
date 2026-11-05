"""Offline regression tests for historical place aliases and rescoring."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as finder


class PlaceAliasTests(unittest.TestCase):
    def test_hungarian_and_serbian_names_refer_to_same_locality(self):
        self.assertEqual(finder.normalized_city("Subotica, North Bačka, Serbia"), "szabadka")
        self.assertEqual(finder.normalized_city("Szabadka, Bács-Bodrog, Hungary"), "szabadka")
        self.assertEqual(finder.normalized_city("SZABADKA"), "szabadka")
        self.assertEqual(finder.api_place("Subotica, North Bačka, Serbia"), "Szabadka")
        self.assertEqual(finder.api_place("Szabadka, Bács-Bodrog, Hungary"), "Szabadka")

    def test_hungarian_accent_variants_and_unrelated_places(self):
        self.assertEqual(
            finder.normalized_city("MEZÖKÖVESD"),
            finder.normalized_city("Mezőkövesd, Borsod-Gömör-Kishont, Hungary"),
        )
        self.assertEqual(finder.api_place("Mezőkövesd, Borsod, Hungary"), "Mezőkövesd")
        self.assertNotEqual(finder.normalized_city("Kecskemét"), finder.normalized_city("Mezőkövesd"))
        self.assertNotEqual(finder.normalized_city("Kecskemét"), finder.normalized_city("Szabadka"))

    def test_full_identity_agreement_with_historical_birthplace_alias(self):
        person = finder.Person(
            xref="fictional-I1", born="17 Oct 1966",
            birthplace="Subotica, North Bačka, Serbia",
        )
        archive = {
            "nev": "Fenyvesi Ágnes", "szul_ido": "1966.10.17",
            "szul_hely": "Szabadka", "anyja_neve": "László Teréz",
        }
        score, evidence, _ = finder.score(
            person, archive, ["Fenyvesi Ágnes"], ["László Teréz"])
        self.assertEqual(score, 100)
        self.assertIn("birthplace exact", evidence)

    def test_rescore_preserves_input_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "matches.csv"
            target = root / "matches.rescored.csv"
            coverage = root / "matches.coverage.csv"
            checkpoint = root / "matches.checkpoint.json"
            checkpoint.write_text('{"fictional":true}', encoding="utf-8")
            sample = {
                "gedcom_id": "I1",
                "gedcom_name": "Fenyvesi Ágnes",
                "gedcom_birth": "17 Oct 1966",
                "gedcom_birth_place": "Subotica, North Bačka, Serbia",
                "gedcom_mother": "László Teréz",
                "archive_guid": "fictional-document",
                "archive_name": "Fenyvesi Ágnes",
                "archive_birth": "1966.10.17",
                "archive_birth_place": "Szabadka",
                "archive_mother": "László Teréz",
                "archive_record_type": "6-os karton",
                "match_score": "90",
                "review_priority": "strong candidate",
                "evidence": "name=45/45; exact DOB; mother exact; birthplace differs",
            }
            with source.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(sample))
                writer.writeheader()
                writer.writerow(sample)
            source_bytes, checkpoint_bytes = source.read_bytes(), checkpoint.read_bytes()
            count, changed = finder.rescore_csv(source, target, coverage)
            self.assertEqual((count, changed), (1, 1))
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["previous_match_score"], "90")
            self.assertEqual(row["match_score"], "100")
            self.assertEqual(row["score_change"], "10")
            self.assertIn("birthplace exact", row["evidence"])
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                finder.rescore_csv(source, source, coverage)

    def test_rescore_only_cli_is_offline_and_requires_no_gedcom(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "matches.csv"
            sample = {
                "gedcom_id": "I1", "gedcom_name": "Fenyvesi Ágnes",
                "gedcom_birth": "17 Oct 1966",
                "gedcom_birth_place": "Subotica, North Bačka, Serbia",
                "gedcom_mother": "László Teréz",
                "archive_guid": "fictional-document",
                "archive_name": "Fenyvesi Ágnes",
                "archive_birth": "1966.10.17",
                "archive_birth_place": "SZABADKA",
                "archive_mother": "László Teréz",
                "archive_record_type": "6-os karton",
                "match_score": "90",
                "review_priority": "strong candidate",
                "evidence": "birthplace differs",
            }
            with source.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(sample))
                writer.writeheader()
                writer.writerow(sample)
            with patch("sys.argv", ["nyitottaktak_gedcom.py", "--rescore-only",
                                    "--output", str(source)]), \
                 patch.object(finder.ApiClient, "_request_json",
                              side_effect=AssertionError("unexpected network request")), \
                 patch.object(finder, "read_gedcom",
                              side_effect=AssertionError("unexpected GEDCOM read")), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                finder.main()
            self.assertIn("1 scores changed", output.getvalue())
            self.assertTrue((root / "matches.rescored.csv").exists())
            self.assertTrue((root / "matches.rescored.review.csv").exists())


if __name__ == "__main__":
    unittest.main()
