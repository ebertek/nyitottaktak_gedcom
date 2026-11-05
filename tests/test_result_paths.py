"""Result directory defaults and safe legacy-report compatibility tests.

All data is fictional. Never commit personal GEDCOMs or archived CSV reports.
"""

import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nyitottaktak_gedcom as finder


class ResultPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_dir = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.old_dir)
        self.gedcom = Path("fictional.ged")
        self.gedcom.write_text(
            "0 HEAD\n1 CHAR UTF-8\n"
            "0 @I1@ INDI\n1 NAME Ágnes /Próbafalvi/\n"
            "1 BIRT\n2 DATE 17 OCT 1966\n2 PLAC Pécs, Hungary\n0 TRLR\n",
            encoding="utf-8",
        )

    def run_cli(self, *flags, **kwargs):
        with patch("sys.argv", ["nyitottaktak_gedcom.py", *flags]), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            finder.main()
        return stdout.getvalue()

    def test_fresh_scan_default_files_are_all_under_results(self):
        reply = (
            [{"guid": "synthetic-card", "nev": "Próbafalvi Ágnes",
              "szul_ido": "1966.10.17", "szul_hely": "Pécs",
              "anyja_neve": "", "irattipus": "6-os karton"}],
            False, "https://example.invalid/search", False,
        )
        with patch.object(finder.ApiClient, "fetch_meta", return_value="2026-11-04"), \
             patch.object(finder.ApiClient, "search_all", return_value=reply):
            self.run_cli(str(self.gedcom), "--execute", "--no-auto-place-index",
                         "--person-id", "I1", "--max-requests", "20")

        for name in (
            "nyitottaktak_matches.csv",
            "nyitottaktak_matches.review.csv",
            "nyitottaktak_matches.coverage.csv",
            "nyitottaktak_matches.checkpoint.json",
        ):
            self.assertTrue((Path("results") / name).exists(), name)
            self.assertFalse(Path(name).exists(), name)

    def test_legacy_quality_report_still_reads_old_root_files(self):
        source = Path("nyitottaktak_matches.csv")
        columns = [
            "gedcom_id", "archive_guid", "archive_record_type",
            "archive_name", "archive_birth", "archive_birth_place",
            "archive_mother", "match_score", "evidence",
        ]
        with source.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerow({
                "gedcom_id": "F1", "archive_guid": "fictional-1",
                "archive_record_type": "6-os karton",
                "archive_name": "Próbafalvi Ágnes",
                "archive_birth": "", "archive_birth_place": "",
                "archive_mother": "", "match_score": "45",
                "evidence": "name=45/45",
            })
        contents = source.read_bytes()
        output = self.run_cli("--quality-only")
        self.assertIn("Using legacy root-level results", output)
        self.assertEqual(source.read_bytes(), contents)
        self.assertTrue(Path("nyitottaktak_matches.quality.csv").exists())
        self.assertFalse(Path("results/nyitottaktak_matches.quality.csv").exists())

    def test_legacy_review_report_still_works(self):
        path = Path("nyitottaktak_matches.csv")
        path.write_text("gedcom_id,archive_guid,archive_name,match_score\n"
                        "I1,fictional-1,Próbafalvi Ágnes,45\n",
                        encoding="utf-8")
        output = self.run_cli("--review-only")
        self.assertIn("Using legacy root-level results", output)
        self.assertTrue(Path("nyitottaktak_matches.review.csv").is_file())

    def test_explicit_output_overrides_default_and_legacy_fallback(self):
        Path("nyitottaktak_matches.csv").write_text("old", encoding="utf-8")
        args = SimpleNamespace(
            output=finder.DEFAULT_OUTPUT,
            review_input=None, review_only=True, rescore_only=False,
            quality_only=False, coverage_only=False, check_updates=False,
            execute=False,
        )
        finder.choose_default_output(args, ["--output", "results/nyitottaktak_matches.csv"])
        self.assertEqual(args.output, finder.DEFAULT_OUTPUT)
        finder.choose_default_output(args, ["--output=results/nyitottaktak_matches.csv"])
        self.assertEqual(args.output, finder.DEFAULT_OUTPUT)
        args.output = Path("custom/matches.csv")
        finder.choose_default_output(args, [])
        self.assertEqual(args.output, Path("custom/matches.csv"))

    def test_legacy_checkpoint_is_never_silently_moved(self):
        legacy = Path("nyitottaktak_matches.checkpoint.json")
        legacy.write_text('{"completed":{}}', encoding="utf-8")
        args = SimpleNamespace(
            output=finder.DEFAULT_OUTPUT, review_input=None,
            review_only=False, rescore_only=False, quality_only=False,
            coverage_only=False, check_updates=False, execute=True,
        )
        with contextlib.redirect_stdout(io.StringIO()) as message:
            finder.choose_default_output(args, [])
        self.assertEqual(args.output, finder.DEFAULT_OUTPUT)
        self.assertIn("Existing root-level checkpoint found", message.getvalue())
        self.assertEqual(legacy.read_text(encoding="utf-8"), '{"completed":{}}')
        self.assertFalse(Path("results").exists())

    def test_legacy_coverage_mode_uses_old_checkpoint(self):
        Path("nyitottaktak_matches.checkpoint.json").write_text(
            '{"completed":{}}', encoding="utf-8")
        args = SimpleNamespace(
            output=finder.DEFAULT_OUTPUT, review_input=None,
            review_only=False, rescore_only=False, quality_only=False,
            coverage_only=True, check_updates=False, execute=False,
        )
        finder.choose_default_output(args, [])
        self.assertEqual(args.output, finder.LEGACY_DEFAULT_OUTPUT)

    def test_new_reports_take_precedence_over_legacy(self):
        Path("nyitottaktak_matches.csv").write_text("legacy", encoding="utf-8")
        output = finder.DEFAULT_OUTPUT
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("modern", encoding="utf-8")
        args = SimpleNamespace(
            output=output, review_input=None, review_only=True,
            rescore_only=False, quality_only=False, coverage_only=False,
            check_updates=False, execute=False,
        )
        finder.choose_default_output(args, [])
        self.assertEqual(args.output, output)


if __name__ == "__main__":
    unittest.main()
