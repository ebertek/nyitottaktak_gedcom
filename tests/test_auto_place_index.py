"""Automatic, first-run Hungarian gazetteer setup tests (all network mocked)."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as finder
from scripts import build_place_index


def write_test_index(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["geonameid", "country", "native", "hungarian", "aliases"]
        )
        writer.writeheader()
        writer.writerow({
            "geonameid": "1234567", "country": "RO",
            "native": "Fictionville", "hungarian": "Mintafalu",
            "aliases": "Fictionville|Mintafalu",
        })


class AutoPlaceIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.gedcom = self.root / "sample.ged"
        self.gedcom.write_text("0 HEAD\n0 TRLR\n", encoding="utf-8")

    def invoke(self, *args):
        argv = [
            "nyitottaktak_gedcom.py", str(self.gedcom),
            "--cache-dir", str(self.cache), *args
        ]
        with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
            finder.main()

    def test_first_executed_scan_bootstraps_and_loads_regional_index(self):
        index = self.cache / "hungarian_places.csv"

        def bootstrap(cache):
            self.assertEqual(cache, self.cache)
            write_test_index(index)
            return index

        with patch.object(finder, "ensure_default_index", side_effect=bootstrap) as build, \
             patch.object(finder, "run") as scan, \
             patch.object(finder.ApiClient, "_request_json",
                          side_effect=AssertionError("unexpected archive call")):
            self.invoke("--execute")
        build.assert_called_once_with(self.cache)
        scan.assert_called_once()
        self.assertTrue(finder.PLACE_RESOLVER.equivalent("Fictionville", "Mintafalu"))

    def test_existing_index_is_reused_without_download(self):
        write_test_index(self.cache / "hungarian_places.csv")
        with patch.object(finder, "ensure_default_index",
                          side_effect=AssertionError("must not redownload")), \
             patch.object(finder, "run") as scan:
            self.invoke("--execute")
        scan.assert_called_once()
        self.assertEqual(finder.api_place("Fictionville"), "Mintafalu")

    def test_preview_and_offline_commands_never_bootstrap(self):
        with patch.object(finder, "ensure_default_index",
                          side_effect=AssertionError("unexpected GeoNames download")), \
             patch.object(finder, "run") as run, \
             patch.object(finder, "rescore_csv", return_value=(1, 0)), \
             patch.object(finder, "review_files", return_value=(1, {}, False)):
            self.invoke()
            self.invoke("--coverage-only")
            self.invoke("--rescore-only")
            self.invoke("--review-only")
        self.assertEqual(run.call_count, 2)
        self.assertFalse((self.cache / "hungarian_places.csv").exists())

    def test_opt_out_uses_builtin_places(self):
        with patch.object(finder, "ensure_default_index",
                          side_effect=AssertionError("unexpected GeoNames download")), \
             patch.object(finder, "run") as scan:
            self.invoke("--execute", "--no-auto-place-index")
        scan.assert_called_once()
        self.assertEqual(finder.api_place("Subotica, Serbia"), "Szabadka")

    def test_download_failure_is_explicit_and_scan_does_not_start(self):
        errors = io.StringIO()
        with patch.object(finder, "ensure_default_index",
                          side_effect=OSError("server temporarily unavailable")), \
             patch.object(finder, "run") as scan, \
             patch("sys.argv", ["nyitottaktak_gedcom.py", str(self.gedcom),
                                "--cache-dir", str(self.cache), "--execute"]), \
             contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                finder.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Automatic Hungarian place index setup failed", errors.getvalue())
        self.assertIn("--no-auto-place-index", errors.getvalue())
        scan.assert_not_called()

    def test_invalid_gedcom_does_not_trigger_download(self):
        with patch.object(finder, "ensure_default_index",
                          side_effect=AssertionError("unexpected download")), \
             patch("sys.argv", ["nyitottaktak_gedcom.py",
                                str(self.root / "missing.ged"), "--execute"]), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                finder.main()

    def test_builder_imports_eight_countries_once(self):
        index = self.cache / "hungarian_places.csv"

        def fake_build(sources, target):
            self.assertEqual(len(sources), 8)
            self.assertEqual(target, index)
            write_test_index(target)
            return 1, len(sources)

        with patch.object(build_place_index, "download_source") as download, \
             patch.object(build_place_index, "build_index",
                          side_effect=fake_build) as build:
            self.assertEqual(build_place_index.ensure_default_index(self.cache), index)
            self.assertEqual(download.call_count, 16)
            build.assert_called_once()
            self.assertEqual(build_place_index.ensure_default_index(self.cache), index)
            self.assertEqual(download.call_count, 16)
            build.assert_called_once()

    def test_empty_download_does_not_leave_empty_index(self):
        with patch.object(build_place_index, "download_source"), \
             patch.object(build_place_index, "build_index", return_value=(0, 8)):
            with self.assertRaisesRegex(ValueError, "no Hungarian-named"):
                build_place_index.ensure_default_index(self.cache)
        self.assertFalse((self.cache / "hungarian_places.csv").exists())


if __name__ == "__main__":
    unittest.main()
