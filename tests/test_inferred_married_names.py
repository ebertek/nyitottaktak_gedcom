"""Offline tests for opt-in Hungarian married-name search variants.

All identities are fictional. No real GEDCOM, family document, or archive data
belongs in repository tests.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nyitottaktak_gedcom as finder
from review_report import build_review_rows


GEDCOM = """0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Ágnes Éva /Fenyvesi/
2 TYPE birth
2 GIVN Ágnes Éva
2 SURN Fenyvesi
1 NAME Ágnes Éva /Borókai/
2 TYPE married
1 SEX F
1 FAMS @F1@
1 FAMS @F2@
1 FAMS @F3@
1 BIRT
2 DATE 17 OCT 1966
2 PLAC Pécs, Hungary
0 @I2@ INDI
1 NAME Lőrinc /Borókai/
2 GIVN Lőrinc
2 SURN Borókai
1 SEX M
1 FAMS @F1@
0 @I3@ INDI
1 NAME Imre /Csengeri/
1 SEX M
1 FAMS @F2@
0 @I4@ INDI
1 NAME Lili /Kőris/
1 BIRT
2 DATE 5 MAY 1990
1 FAMC @F1@
0 @I5@ INDI
1 NAME Zoltán /Bükkösi/
0 @I6@ INDI
1 NAME Anna /Pusztai/
1 FAMS @F4@
0 @F1@ FAM
1 HUSB @I2@
1 WIFE @I1@
1 CHIL @I4@
1 MARR
2 DATE 26 NOV 1988
0 @F2@ FAM
1 HUSB @I3@
1 WIFE @I1@
1 MARR
2 DATE 1 JUN 2000
0 @F3@ FAM
1 HUSB @I5@
1 WIFE @I1@
0 @F4@ FAM
1 WIFE @I6@
1 MARR
2 DATE 1 JAN 1980
0 TRLR
"""


class InferredMarriedNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gedcom = self.root / "fictional.ged"
        self.gedcom.write_text(GEDCOM, encoding="utf-8")
        self.people, self.families = finder.read_gedcom(self.gedcom)

    def test_family_members_and_marriage_are_read(self):
        person = self.people["I1"]
        self.assertEqual(person.spouse_families, ["F1", "F2", "F3"])
        self.assertEqual(self.families["F1"].husband, "I2")
        self.assertEqual(self.families["F1"].wife, "I1")
        self.assertTrue(self.families["F1"].marriage_recorded)
        self.assertFalse(self.families["F3"].marriage_recorded)
        self.assertEqual(self.people["I4"].childhood_families, ["F1"])

    def test_generates_all_traditional_forms_without_claiming_legal_use(self):
        variants = finder.inferred_married_names(
            self.people["I1"], self.people, self.families)
        self.assertIn("Borókai Lőrincné", variants)
        self.assertIn("Borókainé Fenyvesi Ágnes Éva", variants)
        self.assertIn("Borókainé Fenyvesi Ágnes", variants)
        self.assertIn("Borókai Lőrincné Fenyvesi Ágnes Éva", variants)
        self.assertIn("Csengeri Imrené", variants)
        # Explicit GEDCOM married name remains documented, not an inference.
        self.assertNotIn("Borókai Ágnes Éva", variants)
        self.assertNotIn("Bükkösi Zoltánné", variants)
        self.assertEqual(len(variants), len(set(variants)))
        self.assertEqual(finder.inferred_married_names(
            self.people["I6"], self.people, self.families), [])

    def test_maternal_filters_prioritize_documented_birth_names(self):
        child = self.people["I4"]
        self.assertEqual(finder.mother_names(
            child, self.people, self.families, birth_only=True),
            ["Fenyvesi Ágnes Éva"])
        self.assertEqual(finder.mother_names(
            child, self.people, self.families),
            ["Fenyvesi Ágnes Éva", "Borókai Ágnes Éva"])
        self.assertEqual(finder.candidate_names(self.people["I1"]),
                         ["Fenyvesi Ágnes Éva", "Borókai Ágnes Éva"])

    def test_explicit_spouse_family_can_be_linked_from_fam_only(self):
        original = GEDCOM.replace("1 FAMS @F1@\n1 FAMS @F2@\n1 FAMS @F3@\n", "")
        self.gedcom.write_text(original, encoding="utf-8")
        people, families = finder.read_gedcom(self.gedcom)
        self.assertEqual(people["I1"].spouse_families, ["F1", "F2", "F3"])
        self.assertIn("Borókai Lőrincné", finder.inferred_married_names(
            people["I1"], people, families))

    def test_inference_does_not_change_default_checkpoint_schema(self):
        base = SimpleNamespace(exhaustive=True, year_tolerance=2,
                               api_year_ceiling=1990, min_score=35)
        self.assertEqual(finder.checkpoint_config(base), {
            "exhaustive": True, "year_tolerance": 2,
            "api_year_ceiling": 1990, "min_score": 35,
        })
        base.include_inferred_married_names = True
        self.assertTrue(finder.checkpoint_config(base)["inferred_married_names"])

    def test_preview_shows_unverified_search_variants(self):
        args = self.scan_args()
        args.execute = False
        with contextlib.redirect_stdout(io.StringIO()) as output:
            finder.run(args)
        self.assertIn("INFERRED, NOT DOCUMENTED:", output.getvalue())
        self.assertIn("Borókai Lőrincné", output.getvalue())

    def scan_args(self):
        return SimpleNamespace(
            gedcom=self.gedcom, person_id=["I1"], include_undated=False,
            include_inferred_married_names=True,
            since=1850, until=1975, order="historical", max_people=0,
            execute=True, verbose=False, exhaustive=False,
            api_year_ceiling=1990, year_tolerance=2, min_score=35,
            output=self.root / "matches.csv",
            checkpoint=None, no_resume=False, refresh=False,
            refresh_stale=False, coverage_only=False, coverage_output=None,
            review_output=None, cache_dir=self.root / "api_cache",
            delay=0.5, max_requests=0,
        )

    def test_search_and_grouped_review_label_inference(self):
        args = self.scan_args()
        result = {
            "guid": "fictional-archive-001", "nev": "Borókai Lőrincné",
            "szul_ido": "1966.10.17", "szul_hely": "Pécs",
            "anyja_neve": "", "irattipus": "6-os karton",
        }
        calls = []

        def fake_search(_client, filters):
            calls.append(dict(filters))
            if filters["nev"] == "Borókai Lőrincné":
                return [result], False, "https://example.invalid/search", False
            return [], False, "https://example.invalid/search", False

        with patch.object(finder.ApiClient, "fetch_meta", return_value="2026-11-04"), \
             patch.object(finder.ApiClient, "search_all", fake_search), \
             contextlib.redirect_stdout(io.StringIO()):
            finder.run(args)

        searched_names = {query["nev"] for query in calls}
        self.assertIn("Borókai Lőrincné", searched_names)
        self.assertIn("Fenyvesi Ágnes Éva", searched_names)
        self.assertNotIn("Borókainé Fenyvesi Ágnes Éva", searched_names)
        with args.output.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_name_origin"], "inferred_married")
        self.assertEqual(rows[0]["searched_name_origin"], "inferred_married")
        self.assertEqual(rows[0]["gedcom_name"], "Borókai Lőrincné")
        self.assertEqual(rows[0]["gedcom_mother"], "")
        with args.output.with_suffix(".review.csv").open(
                newline="", encoding="utf-8-sig") as handle:
            grouped = list(csv.DictReader(handle))
        self.assertEqual(grouped[0]["top_matched_name_origin"], "inferred_married")
        self.assertIn("inferred_married_name", grouped[0]["review_flags"])
        with args.output.with_suffix(".checkpoint.json").open(
                encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertTrue(state["config"]["inferred_married_names"])

    def test_review_preserves_provenance_without_historical_inference(self):
        row = {
            "gedcom_id": "I1", "gedcom_name": "Borókai Lőrincné",
            "archive_guid": "fictional", "archive_name": "Borókai Lőrincné",
            "match_score": "85", "matched_name_origin": "inferred_married",
            "searched_name_origin": "inferred_married",
        }
        review = build_review_rows([row])[0]
        self.assertIn("inferred_married_name", review["review_flags"])
        self.assertEqual(review["top_matched_name_origin"], "inferred_married")


if __name__ == "__main__":
    unittest.main()
