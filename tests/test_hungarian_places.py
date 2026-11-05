"""Offline tests for importing Hungarian place names from GeoNames ZIP dumps."""

import csv
import tempfile
import unittest
import zipfile
from pathlib import Path

from hungarian_places import PlaceResolver
from scripts.build_place_index import build_index, generate_rows, DEFAULT_COUNTRIES, COUNTRY_LABELS


def record(geoid, name, country, feature="P"):
    fields = [""] * 19
    fields[0] = geoid
    fields[1] = name
    fields[2] = name
    fields[6] = feature
    fields[7] = "PPL" if feature == "P" else "ADM1"
    fields[8] = country
    return "\t".join(fields)


def alternate(geoid, name, lang="hu", preferred="1", historic="0"):
    return "\t".join([
        "alternate-" + geoid, geoid, lang, name,
        preferred, "0", "0", historic, "", "",
    ])


def zip_text(path, member, lines):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member, "\n".join(lines) + "\n")


class GeoNamesImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_historical_hungary_country_coverage(self):
        selected = set(DEFAULT_COUNTRIES.split(","))
        self.assertTrue({"HU", "RO", "SK", "RS", "UA", "HR", "AT", "SI"} <= selected)
        self.assertIn("Vojvodina", COUNTRY_LABELS["RS"])

    def test_import_uses_geographic_ids_and_hungarian_labels(self):
        cities = self.root / "RS.zip"
        alternates = self.root / "RS-alternates.zip"
        index = self.root / "hungarian_places.csv"
        zip_text(cities, "RS.txt", [
            record("101", "Sampleville", "RS"),
            record("102", "Example City", "RS"),
            record("103", "Not a Town", "RS", "A"),
            record("104", "No Hungarian Name", "RS"),
        ])
        zip_text(alternates, "RS.txt", [
            alternate("101", "Mintaváros"),
            alternate("101", "Mintafalu", preferred="0", historic="1"),
            alternate("102", "Példaváros"),
            alternate("103", "Nem Település"),
            alternate("104", "No Hungarian Name", lang="en"),
        ])
        count, sources = build_index([(cities, alternates)], index)
        self.assertEqual((count, sources), (2, 1))
        with index.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({r["native"]: r["hungarian"] for r in rows}, {
            "Sampleville": "Mintaváros",
            "Example City": "Példaváros",
        })
        resolver = PlaceResolver(index)
        self.assertTrue(resolver.equivalent("Sampleville, Serbia", "Mintaváros"))
        self.assertEqual(resolver.preferred_hungarian("Sampleville, Serbia"),
                         "Mintaváros")
        self.assertTrue(resolver.equivalent("Mintafalu", "Sampleville"))
        self.assertFalse(resolver.equivalent("Example City", "Sampleville"))

    def test_hu_native_place_without_hu_alternate_is_indexed(self):
        # These fictional HU settlements have either no alternate names or
        # only non-Hungarian alternate names. Both were previously dropped.
        city = self.root / "HU.zip"
        names = self.root / "HU-alternates.zip"
        index = self.root / "index-hu.csv"
        zip_text(city, "HU.txt", [
            record("701", "Mintakövesd", "HU"),
            record("702", "Próbaháza", "HU"),
            record("703", "Not a Town", "HU", "A"),
        ])
        zip_text(names, "HU.txt", [
            alternate("702", "Exampleville", lang="en"),
            alternate("703", "Not a Town", lang="hu"),
        ])
        count, sources = build_index([(city, names)], index)
        self.assertEqual((count, sources), (2, 1))
        with index.open(encoding="utf-8-sig", newline="") as handle:
            rows = {r["geonameid"]: r for r in csv.DictReader(handle)}
        self.assertEqual(rows["701"]["hungarian"], "Mintakövesd")
        self.assertEqual(rows["702"]["hungarian"], "Próbaháza")
        resolver = PlaceResolver(index)
        self.assertEqual(resolver.find("Mintakövesd").identifier,
                         "geonames:701")
        self.assertEqual(resolver.find("Próbaháza").identifier,
                         "geonames:702")

    def test_missing_hu_names_outside_hungary_are_not_invented(self):
        city = self.root / "RS-missing.zip"
        names = self.root / "RS-missing-alternates.zip"
        index = self.root / "index-rs.csv"
        zip_text(city, "RS.txt", [
            record("801", "Examplegrad", "RS"),
            record("802", "Samplegrad", "RS"),
        ])
        zip_text(names, "RS.txt", [
            alternate("801", "Examplegrad", lang="en"),
            alternate("802", "Mintaváros", lang="hu"),
        ])
        count, _ = build_index([(city, names)], index)
        self.assertEqual(count, 1)
        with index.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["geonameid"], "802")
        self.assertEqual(rows[0]["hungarian"], "Mintaváros")

    def test_native_hu_names_remain_ambiguous_without_unique_identity(self):
        city = self.root / "HU-collisions.zip"
        names = self.root / "HU-collisions-alternates.zip"
        index = self.root / "index-collisions.csv"
        zip_text(city, "HU.txt", [
            record("901", "Mintafalva", "HU"),
            record("902", "Mintafalva", "HU"),
        ])
        zip_text(names, "HU.txt", [])
        count, _ = build_index([(city, names)], index)
        self.assertEqual(count, 2)
        resolver = PlaceResolver(index)
        self.assertIsNone(resolver.find("Mintafalva"))
        self.assertEqual(
            {entry.identifier for entry in resolver.candidates("Mintafalva")},
            {"geonames:901", "geonames:902"},
        )

    def test_hu_place_with_existing_hu_name_preserves_preferred_alias(self):
        city = self.root / "HU-preferred.zip"
        names = self.root / "HU-preferred-alternates.zip"
        index = self.root / "index-preferred.csv"
        zip_text(city, "HU.txt", [
            record("999", "Example Native", "HU"),
        ])
        zip_text(names, "HU.txt", [
            alternate("999", "Próbafalu", lang="hu"),
        ])
        build_index([(city, names)], index)
        resolver = PlaceResolver(index)
        self.assertEqual(
            resolver.preferred_hungarian("Example Native"), "Próbafalu"
        )

    def test_ambiguous_native_names_require_country_context(self):
        city_a, alt_a = self.root / "RS.zip", self.root / "RS-alt.zip"
        city_b, alt_b = self.root / "HR.zip", self.root / "HR-alt.zip"
        index = self.root / "index.csv"
        zip_text(city_a, "RS.txt", [record("201", "Novigrad", "RS")])
        zip_text(alt_a, "RS.txt", [alternate("201", "Újfalu")])
        zip_text(city_b, "HR.txt", [record("202", "Novigrad", "HR")])
        zip_text(alt_b, "HR.txt", [alternate("202", "Újvár")])
        build_index([(city_a, alt_a), (city_b, alt_b)], index)
        resolver = PlaceResolver(index)
        self.assertIsNone(resolver.find("Novigrad"))
        self.assertEqual(resolver.preferred_hungarian("Novigrad"), "Novigrad")
        self.assertEqual(resolver.preferred_hungarian("Novigrad, Serbia"), "Újfalu")
        self.assertEqual(resolver.preferred_hungarian("Novigrad, Croatia"), "Újvár")
        # An archive name with no country must not force ambiguous locations
        # into one identity.
        self.assertFalse(resolver.equivalent("Novigrad, Serbia", "Újvár"))
        self.assertTrue(resolver.equivalent("Novigrad, Serbia", "Újfalu"))

    def test_missing_geonames_sources_fail_without_network(self):
        with self.assertRaisesRegex(ValueError, "GeoNames archive missing"):
            generate_rows(self.root / "absent.zip", self.root / "no-alternates.zip")

    def test_built_in_historical_aliases_work_without_index(self):
        resolver = PlaceResolver()
        self.assertTrue(resolver.equivalent(
            "Odžaci, Vojvodina, Yugoslavia", "Hódság"))
        self.assertTrue(resolver.equivalent(
            "Bačko Petrovo Selo, Serbia", "Péterréve"))
        self.assertTrue(resolver.equivalent(
            "Čakovci, Croatia", "Csák"))
        self.assertFalse(resolver.equivalent(
            "Čakovci, Croatia", "Hódság"))


if __name__ == "__main__":
    unittest.main()
