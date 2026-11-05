"""Evidence-review regression tests; only fictional archive and GEDCOM names."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nyitottaktak_gedcom as cli
from review_report import (
    EVIDENCE_CLASSES,
    FIELDS,
    build_review_rows,
    classify_identity_evidence,
    review_files,
)


def lead(
        *, pid="I1", guid="record-001", score="45",
        evidence="name=45/45; DOB unavailable on one side; "
                 "mother unavailable on one side; birthplace unavailable on one side",
        ged_birth="17 OCT 1966", archive_birth="",
        ged_place="Pécs", archive_place="",
        ged_mother="Mintás Mária", archive_mother="",
        source="B napló", origin="gedcom"):
    return {
        "gedcom_id": pid,
        "gedcom_name": "Próbaházi Ágnes",
        "gedcom_birth": ged_birth,
        "gedcom_birth_place": ged_place,
        "gedcom_mother": ged_mother,
        "archive_guid": guid,
        "archive_name": "Próbaházi Ágnes",
        "archive_birth": archive_birth,
        "archive_birth_place": archive_place,
        "archive_mother": archive_mother,
        "archive_record_type": source,
        "matched_name_origin": origin,
        "match_score": score,
        "evidence": evidence,
        "archive_document_url": "https://example.invalid/document/" + guid,
    }


class EvidenceClassificationTests(unittest.TestCase):
    def test_full_identity_agreement_is_strong(self):
        row = lead(
            score="100", evidence="name=45/45; exact DOB; mother exact; birthplace exact",
            archive_birth="1966.10.17", archive_place="Pécs", archive_mother="Mintás Mária",
            source="6-os karton",
        )
        result = classify_identity_evidence(row)
        self.assertEqual(result["classification"], "strongly_corroborated")
        self.assertEqual(result["agreeing_identifiers"],
                         "birth_date:exact | birthplace:exact | mother:exact")
        self.assertEqual(result["conflicting_identifiers"], "")
        self.assertEqual(result["missing_gedcom_identifiers"], "")

    def test_exact_two_orthogonal_identifiers_is_strong(self):
        row = lead(evidence="name=45/45; mother exact; birthplace exact",
                   archive_place="Pécs", archive_mother="Mintás Mária")
        self.assertEqual(classify_identity_evidence(row)["classification"],
                         "strongly_corroborated")

    def test_one_matching_identifier_is_partial(self):
        row = lead(evidence="name=45/45; mother exact; birthplace unavailable on one side",
                   archive_mother="Mintás Mária")
        self.assertEqual(classify_identity_evidence(row)["classification"],
                         "partially_corroborated")

    def test_only_year_matching_or_partial_fields_are_partial(self):
        for evidence in ("birth year matches", "mother partial",
                         "birthplace partial", "birth year matches; birthplace partial"):
            with self.subTest(evidence=evidence):
                row = lead(evidence="name=45/45; " + evidence,
                           archive_birth="1966", archive_place="Pécs",
                           archive_mother="Mintás")
                self.assertEqual(classify_identity_evidence(row)["classification"],
                                 "partially_corroborated")

    def test_two_approximate_indicators_are_not_strong(self):
        row = lead(evidence="name=45/45; mother partial; birthplace partial",
                   archive_place="Pécs III.", archive_mother="Mintás")
        self.assertEqual(classify_identity_evidence(row)["classification"],
                         "partially_corroborated")

    def test_one_year_difference_is_not_identity_corroboration(self):
        row = lead(evidence="name=45/45; birth years differ by 1",
                   archive_birth="1967")
        self.assertEqual(classify_identity_evidence(row)["classification"],
                         "insufficient_comparable_data")
        self.assertEqual(classify_identity_evidence(row)["agreeing_identifiers"], "")

    def test_conflicts_override_simultaneous_agreements(self):
        row = lead(
            evidence="name=45/45; exact DOB; mother exact; birthplace differs",
            archive_birth="1966.10.17", archive_mother="Mintás Mária",
            archive_place="Szeged", score="90",
        )
        result = classify_identity_evidence(row)
        self.assertEqual(result["classification"], "conflicting_identity")
        self.assertEqual(result["conflicting_identifiers"], "birthplace")
        self.assertEqual(result["agreeing_identifiers"], "birth_date:exact | mother:exact")

    def test_each_conflict_token_is_recognized(self):
        tokens = {
            "DOB conflict (day/month)": "birth_date",
            "DOB conflict (year)": "birth_date",
            "birth-year conflict": "birth_date",
            "mother name differs": "mother",
            "birthplace differs": "birthplace",
        }
        for token, field in tokens.items():
            with self.subTest(token=token):
                result = classify_identity_evidence(
                    lead(evidence=f"name=45/45; {token}"))
                self.assertEqual(result["classification"], "conflicting_identity")
                self.assertEqual(result["conflicting_identifiers"], field)

    def test_name_only_requires_no_archive_identifiers(self):
        result = classify_identity_evidence(lead(score="45"))
        self.assertEqual(result["classification"], "name_only")
        # Score must not be used to infer field completeness.
        other = classify_identity_evidence(lead(score="90"))
        self.assertEqual(other["classification"], "name_only")

    def test_archive_identifiers_with_missing_tree_data_are_insufficient_gedcom(self):
        row = lead(
            ged_birth="", archive_birth="1966.10.17",
            ged_place="", archive_place="Pécs",
            ged_mother="", archive_mother="Mintás Mária",
        )
        result = classify_identity_evidence(row)
        self.assertEqual(result["classification"], "insufficient_gedcom_data")
        self.assertEqual(result["missing_gedcom_identifiers"],
                         "birth_date | birthplace | mother")

    def test_unknown_comparison_is_not_falsely_marked_corrob_or_conflict(self):
        row = lead(evidence="name=45/45; DOB unavailable on one side",
                   archive_birth="unknown historical date")
        result = classify_identity_evidence(row)
        self.assertEqual(result["classification"], "insufficient_comparable_data")
        self.assertEqual(result["agreeing_identifiers"], "")
        self.assertEqual(result["conflicting_identifiers"], "")

    def test_unrecognized_legacy_evidence_does_not_claim_strong_match(self):
        row = lead(evidence="", archive_birth="1966.10.17",
                   archive_place="Pécs", archive_mother="Mintás Mária")
        self.assertEqual(classify_identity_evidence(row)["classification"],
                         "insufficient_comparable_data")

    def test_grouped_review_breakdown_keeps_candidates_separate(self):
        leads = [
            lead(guid="card-1", score="100",
                 evidence="name=45/45; exact DOB; mother exact; birthplace exact",
                 archive_birth="1966.10.17", archive_mother="Mintás Mária",
                 archive_place="Pécs"),
            lead(guid="book-1", score="45"),
            lead(guid="card-2", score="30", evidence="name=45/45; DOB conflict (year)",
                 archive_birth="1940"),
            lead(guid="tape-1", score="60",
                 evidence="name=45/45; mother exact",
                 archive_mother="Mintás Mária"),
        ]
        result = build_review_rows(leads)[0]
        self.assertEqual(result["highest_identity_score"], 100)
        self.assertEqual(result["top_archive_guid"], "card-1")
        self.assertEqual(result["top_evidence_classification"], "strongly_corroborated")
        self.assertEqual(result["candidate_strongly_corroborated"], 1)
        self.assertEqual(result["candidate_partially_corroborated"], 1)
        self.assertEqual(result["candidate_name_only"], 1)
        self.assertEqual(result["candidate_conflicting_identity"], 1)
        self.assertEqual(
            sum(result[f"candidate_{name}"] for name in EVIDENCE_CLASSES),
            result["candidate_count"],
        )
        self.assertEqual(result["candidate_count"], 4)
        # Contradictions belonging only to other records must not be applied
        # to the top result's review flags.
        self.assertNotIn("conflicting_identity_data", result["review_flags"])

    def test_top_conflict_not_hidden_by_lower_score_strong_lead(self):
        leads = [
            lead(score="95", guid="bad-1",
                 evidence="name=45/45; exact DOB; birthplace differs",
                 archive_birth="1966.10.17", archive_place="Szeged"),
            lead(score="85", guid="good-1",
                 evidence="name=45/45; exact DOB; mother exact",
                 archive_birth="1966.10.17", archive_mother="Mintás Mária"),
        ]
        result = build_review_rows(leads)[0]
        self.assertEqual(result["top_evidence_classification"], "conflicting_identity")
        self.assertEqual(result["candidate_strongly_corroborated"], 1)
        self.assertEqual(result["candidate_conflicting_identity"], 1)
        self.assertIn("conflicting_identity_data", result["review_flags"])

    def test_missing_person_has_no_candidate_label_and_zero_counts(self):
        rows = build_review_rows([], [{
            "gedcom_id": "I9", "gedcom_name": "Tesztfalvi Éva",
            "search_status": "completed",
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["top_evidence_classification"], "no_candidate_leads")
        self.assertTrue(all(rows[0][f"candidate_{name}"] == 0
                            for name in EVIDENCE_CLASSES))
        self.assertIn("no_candidate_leads", rows[0]["review_flags"])

    def test_inferred_married_name_origin_and_review_flags_are_preserved(self):
        row = lead(
            origin="inferred_married",
            evidence="name=45/45; exact DOB; mother exact",
            archive_birth="1966.10.17", archive_mother="Mintás Mária",
        )
        result = build_review_rows([row])[0]
        self.assertEqual(result["top_evidence_classification"], "strongly_corroborated")
        self.assertIn("inferred_married_name", result["review_flags"])
        self.assertEqual(result["top_matched_name_origin"], "inferred_married")


class OfflineReviewCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_existing_candidate_csv_remains_unchanged(self):
        candidates = self.root / "fictional.csv"
        coverage = self.root / "fictional.coverage.csv"
        output = self.root / "fictional.review.csv"
        original = lead(
            evidence="name=45/45; exact DOB; mother exact; birthplace exact",
            archive_birth="1966.10.17", archive_mother="Mintás Mária",
            archive_place="Pécs",
        )
        with candidates.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(original))
            writer.writeheader()
            writer.writerow(original)
        with coverage.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["gedcom_id", "search_status"])
            writer.writeheader()
            writer.writerow({"gedcom_id": "I1", "search_status": "completed"})
        original_bytes = candidates.read_bytes()
        found, _, has_coverage = review_files(candidates, coverage, output)
        self.assertEqual(found, 1)
        self.assertTrue(has_coverage)
        self.assertEqual(candidates.read_bytes(), original_bytes)
        with output.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(set(rows[0]), set(FIELDS))
        self.assertEqual(rows[0]["top_evidence_classification"], "strongly_corroborated")
        self.assertEqual(rows[0]["candidate_strongly_corroborated"], "1")

    def test_cli_review_only_requires_no_gedcom_or_network(self):
        path = self.root / "fictional.csv"
        fake = lead()
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fake))
            writer.writeheader()
            writer.writerow(fake)
        with patch("sys.argv", [
            "nyitottaktak_gedcom.py", "--review-only",
            "--review-input", str(path),
        ]), patch.object(cli, "read_gedcom",
                       side_effect=AssertionError("unexpected GEDCOM read")), \
             patch.object(cli.ApiClient, "_request_json",
                          side_effect=AssertionError("unexpected API access")), \
             patch.object(cli, "ensure_default_index",
                          side_effect=AssertionError("unexpected GeoNames")), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        with path.with_suffix(".review.csv").open(
                newline="", encoding="utf-8-sig") as handle:
            result = list(csv.DictReader(handle))
        self.assertEqual(result[0]["top_evidence_classification"], "name_only")


if __name__ == "__main__":
    unittest.main()
