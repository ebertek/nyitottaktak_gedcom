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

ORIGINAL_FETCH_META = finder.ApiClient.fetch_meta

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
        # Existing offline tests must never accidentally call the live /api/meta.
        meta_patch = patch.object(finder.ApiClient, "fetch_meta", return_value="2026-11-04")
        meta_patch.start()
        self.addCleanup(meta_patch.stop)

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
            checkpoint=None, no_resume=False, refresh=False, refresh_stale=False,
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

    def test_interrupted_refresh_does_not_leave_stale_completed_entry(self):
        args = self.scan_args()
        record = {
            "guid": "fictional-record-1", "nev": "Fenyvesi Ágnes",
            "szul_ido": "1966.10.17", "szul_hely": "Pécs",
            "anyja_neve": "László Teréz",
        }
        with patch.object(finder.ApiClient, "search_all",
                          return_value=([record], False, "https://example.invalid/search", False)):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        args.refresh = True
        with patch.object(finder.ApiClient, "search_all",
                          side_effect=RuntimeError("Request budget reached (3)")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                finder.run(args)
        state = json.loads(args.output.with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(state["completed"], {})
        with args.output.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 0)

    def test_checkpoint_rejects_changed_configuration(self):
        args = self.scan_args()
        with patch.object(finder.ApiClient, "search_all",
                          return_value=([], False, "https://example.invalid/search", False)):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        args.exhaustive = True
        with self.assertRaisesRegex(ValueError, "different GEDCOM or search configuration"):
            finder.run(args)

    def test_all_switch_uses_unlimited_local_budgets(self):
        with patch("sys.argv", ["nyitottaktak_gedcom.py", str(self.path), "--all"]), \
             patch.object(finder, "run") as run_mock:
            finder.main()
        args = run_mock.call_args.args[0]
        self.assertEqual(args.max_people, 0)
        self.assertEqual(args.max_requests, 0)
        self.assertFalse(args.include_undated)
        self.assertFalse(args.execute)

    def test_no_people_cap_preserves_all_eligible_people(self):
        args = self.scan_args(person_ids=[])
        args.person_id = []
        args.execute = False
        args.max_people = 0
        with contextlib.redirect_stdout(io.StringIO()) as output:
            finder.run(args)
        rendered = output.getvalue()
        self.assertIn("I1: Fenyvesi Ágnes", rendered)
        self.assertIn("I4: Pusztai Lili", rendered)

    def test_zero_request_budget_allows_more_than_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = finder.ApiClient(Path(tmp), delay=0.5, max_requests=0, refresh=False)
            response = {"results": [], "total": 0, "page_size": 25}
            def fake_response(_request, timeout):
                return FakeReply(response)
            with patch.object(finder, "urlopen", side_effect=fake_response) as requests, \
                 patch.object(finder.time, "sleep"):
                for index in range(251):
                    client.fetch({"nev": f"Fictional-{index}", "page": 1})
            self.assertEqual(client.requests, 251)
            self.assertEqual(requests.call_count, 251)

    def test_explicit_request_budget_still_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = finder.ApiClient(Path(tmp), delay=0.5, max_requests=2, refresh=False)
            def fake_response(_request, timeout):
                return FakeReply({"results": [], "total": 0, "page_size": 25})
            with patch.object(finder, "urlopen", side_effect=fake_response), \
                 patch.object(finder.time, "sleep"):
                client.fetch({"nev": "Fictional-A"})
                client.fetch({"nev": "Fictional-B"})
                with self.assertRaisesRegex(RuntimeError, "Request budget reached"):
                    client.fetch({"nev": "Fictional-C"})

    def test_pagination_without_reported_cap_does_not_stop_at_250(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = finder.ApiClient(Path(tmp), delay=0.5, max_requests=0, refresh=False)
            def page_response(params):
                page = params["page"]
                return {
                    "results": [{"guid": f"fictional-{page}-{i}"} for i in range(25)],
                    "page_size": 25,
                    "total": 275,
                    "page": page,
                }
            with patch.object(client, "fetch", side_effect=page_response) as fetch:
                rows, too_many, _url, _missing = client.search_all({"nev": "Fictional"})
            self.assertEqual(len(rows), 275)
            self.assertFalse(too_many)
            self.assertEqual(fetch.call_count, 11)

    def test_archive_type_notes_do_not_assert_historical_activity(self):
        cases = (
            ("6-os karton", "State-security registration card", "unsuccessful"),
            ("B napló", "Register of archived recruitment dossiers", "1956"),
            ("B-napló", "Register of archived recruitment dossiers", "1956"),
            ("B dosszié", "Recruitment dossier", "examination"),
            ("Mágnesszalag", "H-register magnetic-tape data", "1990"),
            ("H-adattár", "H-register magnetic-tape data", "1990"),
        )
        for record_type, expected_summary, expected_text in cases:
            with self.subTest(record_type=record_type):
                summary, caveat = finder.archive_source_context(record_type)
                self.assertEqual(summary, expected_summary)
                self.assertIn(expected_text, caveat)
        summary, caveat = finder.archive_source_context("Unknown source")
        self.assertIn("not recognized", summary)
        self.assertIn("original document", caveat)

    def test_legacy_checkpoint_rows_gain_context_on_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "matches.csv"
            # Rows written by earlier checkpoint versions have no new context keys.
            saved = {
                "I1": {"rows": [{
                    "gedcom_id": "I1",
                    "archive_guid": "fictional-record-1",
                    "archive_record_type": "6-os karton",
                    "match_score": 85,
                }]},
            }
            columns = [
                "gedcom_id", "archive_guid", "archive_record_type",
                "match_score", "archive_source_summary",
                "archive_interpretation_limits",
            ]
            finder.write_report(output, columns, saved)
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["archive_source_summary"],
                             "State-security registration card")
            self.assertIn("fictitious recruitment",
                          rows[0]["archive_interpretation_limits"])
            self.assertEqual(rows[0]["archive_guid"], "fictional-record-1")

    def test_meta_endpoint_parsing_validates_iso_date(self):
        client = finder.ApiClient(Path(self.tmp.name) / "meta", delay=0.5,
                                  max_requests=10, refresh=False)
        with patch.object(finder.ApiClient, "_request_json",
                          return_value={"adatbazis_frissites": "2026-11-04"}) as request:
            self.assertEqual(ORIGINAL_FETCH_META(client), "2026-11-04")
        request.assert_called_once_with(finder.API_META)
        for payload in (
            {"adatbazis_frissites": "unknown"},
            {"adatbazis_frissites": "2026-99-99"},
            {"adatbazis_frissites": 20261104},
            {},
        ):
            with self.subTest(payload=payload), \
                 patch.object(finder.ApiClient, "_request_json", return_value=payload):
                with self.assertRaises(ValueError):
                    ORIGINAL_FETCH_META(client)

    def test_meta_tracking_detects_change_without_scanning_gedcom(self):
        client = finder.ApiClient(Path(self.tmp.name) / "track", 0.5, 10, False)
        with patch.object(finder.ApiClient, "fetch_meta",
                          side_effect=["2026-11-04", "2026-11-04", "2026-11-06"]):
            with contextlib.redirect_stdout(io.StringIO()) as first:
                self.assertEqual(finder.check_archive_update(client, client.cache_dir),
                                 "2026-11-04")
            with contextlib.redirect_stdout(io.StringIO()) as second:
                finder.check_archive_update(client, client.cache_dir)
            with contextlib.redirect_stdout(io.StringIO()) as third:
                finder.check_archive_update(client, client.cache_dir)
        self.assertIn("first local observation", first.getvalue())
        self.assertIn("unchanged", second.getvalue())
        self.assertIn("UPDATED: 2026-11-04 -> 2026-11-06", third.getvalue())
        state = json.loads((client.cache_dir / "database-meta.json").read_text())
        self.assertEqual(state["last_seen"], "2026-11-06")

    def test_check_updates_cli_does_not_require_gedcom(self):
        directory = Path(self.tmp.name) / "metacli"
        with patch("sys.argv", ["nyitottaktak_gedcom.py", "--check-updates",
                                "--cache-dir", str(directory)]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                finder.main()
        self.assertIn("2026-11-04", output.getvalue())
        self.assertTrue((directory / "database-meta.json").is_file())

    def test_existing_checkpoint_can_recheck_only_stale_people(self):
        args = self.scan_args(["I1"])
        record = {
            "guid": "fictional-record-1", "nev": "Fenyvesi Ágnes",
            "szul_ido": "1966.10.17", "szul_hely": "Pécs",
            "anyja_neve": "László Teréz",
        }
        response = ([record], False, "https://example.invalid/search", False)
        with patch.object(finder.ApiClient, "search_all", return_value=response):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        path = args.output.with_suffix(".checkpoint.json")
        self.assertEqual(json.loads(path.read_text())["completed"]["I1"]["source_updated"],
                         "2026-11-04")
        with patch.object(finder.ApiClient, "fetch_meta", return_value="2026-11-06"), \
             patch.object(finder.ApiClient, "search_all",
                          side_effect=AssertionError("should remain checkpointed")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                finder.run(args)
        self.assertIn("1 older snapshot", output.getvalue())
        args.refresh_stale = True
        refreshed_with_live_http = []
        def newer_search(client, _filters):
            refreshed_with_live_http.append(client.refresh)
            return response
        with patch.object(finder.ApiClient, "fetch_meta", return_value="2026-11-06"), \
             patch.object(finder.ApiClient, "search_all", newer_search):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        self.assertEqual(refreshed_with_live_http, [True])
        state = json.loads(path.read_text())
        self.assertEqual(state["completed"]["I1"]["source_updated"], "2026-11-06")
        self.assertEqual(len(list(Path(self.tmp.name).glob("nyitottaktak_matches.pre-refresh-*.csv"))), 1)

    def test_unknown_legacy_checkpoint_is_not_falsely_marked_current(self):
        args = self.scan_args()
        with patch.object(finder.ApiClient, "search_all",
                          return_value=([], False, "https://example.invalid/search", False)):
            with contextlib.redirect_stdout(io.StringIO()):
                finder.run(args)
        path = args.output.with_suffix(".checkpoint.json")
        state = json.loads(path.read_text())
        del state["completed"]["I1"]["source_updated"]
        path.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(finder.ApiClient, "search_all",
                          side_effect=AssertionError("unexpected requery")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                finder.run(args)
        self.assertIn("1 unknown", output.getvalue())

    def test_failed_required_metadata_cannot_mutate_checkpoint(self):
        args = self.scan_args()
        args.refresh_stale = True
        with patch.object(finder.ApiClient, "fetch_meta", side_effect=ValueError("malformed date")):
            with self.assertRaisesRegex(ValueError, "Could not check archive database date"):
                finder.run(args)
        self.assertFalse(args.output.with_suffix(".checkpoint.json").exists())

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
