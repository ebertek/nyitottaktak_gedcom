"""Offline regression tests using fictional GEDCOM records."""

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

SAMPLE_GEDCOM = """0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Ágnes /Fenyvesi/
2 GIVN Ágnes
2 SURN Fenyvesi
1 BIRT
2 DATE 17 OCT 1966
2 PLAC Pécs III., Baranya, Hungary
1 FAMC @F1@
0 @I2@ INDI
1 NAME Teréz
2 GIVN Teréz
2 SURN László
0 @I3@ INDI
1 NAME Ilona //
2 GIVN Ilona
0 @I4@ INDI
1 NAME Lili /Pusztai/
1 BIRT
2 DATE 4 MAR 1928
2 PLAC Pécs, Hungary
0 @F1@ FAM
1 WIFE @I2@
1 CHIL @I1@
0 TRLR
"""


class FakeReply:
    def __init__(self, value):
        self.contents = io.BytesIO(json.dumps(value).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, count=-1):
        return self.contents.read(count)


class GedcomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "example.ged"
        self.path.write_text(SAMPLE_GEDCOM, encoding="utf-8")
        self.people, self.families = finder.read_gedcom(self.path)

    def test_full_gedcom_names_and_mothers(self):
        self.assertEqual(finder.candidate_names(self.people["I1"]), ["Fenyvesi Ágnes"])
        self.assertEqual(finder.mother_names(self.people["I1"], self.people, self.families),
                         ["László Teréz"])

    def test_structured_given_and_surname_fallback(self):
        self.assertEqual(finder.candidate_names(self.people["I2"]), ["László Teréz"])
        self.assertEqual(finder.candidate_names(self.people["I3"]), [])
        self.assertFalse(finder.has_gedcom_surname("Ilona //"))
        self.assertFalse(finder.has_gedcom_surname("Ilona /Unknown/"))

    def test_city_level_birthplace(self):
        for place, expected in (
            ("Pécs III.", "Pécs"),
            ("Budapest XI.", "Budapest"),
            ("Budapest IV", "Budapest"),
            ("Budapest XI. kerület", "Budapest"),
            ("Budapest 11. ker.", "Budapest"),
            ("Pécs III., Baranya, Hungary", "Pécs"),
            ("Mecsekszabolcs", "Mecsekszabolcs"),
        ):
            with self.subTest(place=place):
                self.assertEqual(finder.api_place(place), expected)

    def test_search_steps_honor_frontend_constraints(self):
        args = SimpleNamespace(api_year_ceiling=1990, year_tolerance=2)
        steps = finder.build_steps("Fenyvesi Ágnes", 1966, "Pécs III.",
                                   ["László Teréz"], args)
        self.assertEqual(steps[0], {
            "nev": "Fenyvesi Ágnes", "szul_tol": 1964, "szul_ig": 1968,
            "szul_hely": "Pécs", "anyja_neve": "László Teréz",
        })
        self.assertEqual(steps[-1], {"nev": "Fenyvesi Ágnes"})
        self.assertNotIn("szul_tol", finder.build_steps(
            "Fenyvesi Ágnes", 1800, "Pécs", [], args)[0])

    def test_score_agrees_with_normalized_birthplace(self):
        archive = {
            "nev": "Fenyvesi Ágnes", "szul_ido": "1966.10.17", "szul_hely": "Pécs",
            "anyja_neve": "László Teréz",
        }
        score, evidence, _ = finder.score(self.people["I1"], archive,
                                          ["Fenyvesi Ágnes"], ["László Teréz"])
        self.assertEqual(score, 100)
        self.assertIn("birthplace exact", evidence)

    def test_person_id_selects_specific_relative(self):
        args = SimpleNamespace(
            gedcom=self.path, person_id=["@I1@"], include_undated=False,
            since=1850, until=1975, order="historical", max_people=5,
            execute=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            finder.run(args)
        text = output.getvalue()
        self.assertIn("1 eligible", text)
        self.assertIn("I1: Fenyvesi Ágnes", text)
        self.assertNotIn("I2:", text)
        args.person_id = ["I999"]
        with self.assertRaisesRegex(ValueError, "I999"):
            finder.run(args)

    def scan_args(self, person_ids=None):
        return SimpleNamespace(
            gedcom=self.path, person_id=person_ids or ["I1"], include_undated=False,
            since=1850, until=1975, order="historical", max_people=5,
            execute=True, verbose=False, exhaustive=False,
            api_year_ceiling=1990, year_tolerance=2, min_score=35,
            output=Path(self.tmp.name) / "nyitottaktak_matches.csv",
            checkpoint=None, no_resume=False, refresh=False,
            cache_dir=Path(self.tmp.name) / "api_cache", delay=0.5, max_requests=3,
        )

    def test_checkpoint_resume_preserves_result_without_requery(self):
        args = self.scan_args()
        match = {
            "guid": "fictional-record-1", "nev": "Fenyvesi Ágnes",
            "szul_ido": "1966.10.17", "szul_hely": "Pécs",
            "anyja_neve": "László Teréz", "irattipus": "6-os karton",
        }
        answer = ([match], False, "https://example.invalid/search", False)
        with patch.object(finder.ApiClient, "search_all", return_value=answer) as fetch:
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
            self.assertEqual(fetch.call_count, 1)
        checkpoint = args.output.with_suffix(".checkpoint.json")
        self.assertTrue(checkpoint.exists())
        with args.output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        with patch.object(finder.ApiClient, "search_all", side_effect=AssertionError("requeried")):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        with args.output.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_checkpoint_preserves_completed_people_on_request_budget_stop(self):
        args = self.scan_args(["I1", "I4"])
        match = {
            "guid": "fictional-record-1", "nev": "Fenyvesi Ágnes",
            "szul_ido": "1966.10.17", "szul_hely": "Pécs",
            "anyja_neve": "László Teréz",
        }
        call_count = [0]

        def first_pass(_client, _filters):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("Request budget reached (3); rerun to continue from cache")
            return [match], False, "https://example.invalid/search", False

        with patch.object(finder.ApiClient, "search_all", first_pass):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                finder.run(args)
        with args.output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gedcom_id"], "I1")
        state = json.loads(args.output.with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state["completed"]), {"I1"})

        second_calls = [0]

        def second_pass(_client, _filters):
            second_calls[0] += 1
            return [], False, "https://example.invalid/search", False

        with patch.object(finder.ApiClient, "search_all", second_pass):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
            self.assertGreaterEqual(second_calls[0], 1)
        with args.output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        state = json.loads(args.output.with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state["completed"]), {"I1", "I4"})

    def test_checkpoint_rejects_changed_configuration(self):
        args = self.scan_args()
        with patch.object(finder.ApiClient, "search_all",
                          return_value=([], False, "https://example.invalid/search", False)):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        args.exhaustive = True
        with self.assertRaisesRegex(ValueError, "different GEDCOM or search configuration"):
            finder.run(args)

    def test_api_response_is_cached_without_live_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = finder.ApiClient(Path(tmp), delay=0.5, max_requests=3, refresh=False)
            response = {"results": [], "total": 0, "page_size": 25}
            with patch.object(finder, "urlopen", return_value=FakeReply(response)) as request:
                first = client.search_all({"nev": "Fenyvesi Ágnes"})
                second = client.search_all({"nev": "Fenyvesi Ágnes"})
            self.assertEqual(first, second)
            self.assertEqual(client.requests, 1)
            self.assertEqual(client.cached_responses, 1)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
