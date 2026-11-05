#!/usr/bin/env python3
"""Build an offline Hungarian place-name index from GeoNames data.

Run explicitly to download public geographical reference files. Normal GEDCOM
searching and --rescore-only never contact GeoNames. Source: GeoNames (CC BY 4.0).
The index is not a guarantee of exhaustive historical place-name coverage.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://download.geonames.org/export/dump"
DEFAULT_COUNTRIES = "RS,HR,RO,SK,UA,SI,AT,HU"
COLUMNS = ["geonameid", "country", "native", "hungarian", "aliases"]


def download_source(url: str, path: Path) -> None:
    if path.is_file():
        print(f"Using existing download: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        request = Request(url, headers={"User-Agent": "NyitottAktakGedcom/1.0 (gazetteer setup)"})
        with urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        with zipfile.ZipFile(tmp) as archive:
            if archive.testzip() is not None:
                raise ValueError(f"Invalid GeoNames ZIP: {url}")
        tmp.replace(path)
    except (HTTPError, URLError, OSError, zipfile.BadZipFile, ValueError):
        tmp.unlink(missing_ok=True)
        raise
    print(f"Downloaded {url}")


def zip_records(path: Path):
    if not path.is_file():
        raise ValueError(f"GeoNames archive missing: {path}. Use --download to fetch it.")
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".txt")]
        if not members:
            raise ValueError(f"No .txt entries in {path}")
        with archive.open(sorted(members)[0]) as stream:
            for line in stream:
                columns = line.decode("utf-8-sig", "replace").rstrip("\r\n").split("\t")
                if columns and columns[0]:
                    yield columns


def hungarian_names(archive: Path) -> dict[str, list[tuple[str, bool, bool]]]:
    names: dict[str, list[tuple[str, bool, bool]]] = defaultdict(list)
    for columns in zip_records(archive):
        # alternateNamesV2 fields: alt-id, geoname-id, language, name,
        # preferred, short, colloquial, historic, from, to.
        if len(columns) < 4 or columns[2] != "hu" or not columns[3].strip():
            continue
        name = columns[3].strip()
        if len(name) > 200:
            continue
        names[columns[1]].append((
            name,
            len(columns) > 4 and columns[4] == "1",
            len(columns) > 7 and columns[7] == "1",
        ))
    return names


def generate_rows(places_zip: Path, alternates_zip: Path) -> list[dict]:
    aliases_by_id = hungarian_names(alternates_zip)
    output: list[dict] = []
    for row in zip_records(places_zip):
        # geoname: id, name, asciiname, alternate names, lat, lon,
        # feature class, feature code, country code, etc.
        if (len(row) < 9 or row[6] != "P" or
                row[0] not in aliases_by_id):
            continue
        geonameid, native, ascii_name, country = row[0], row[1], row[2], row[8]
        candidates = aliases_by_id[geonameid]
        # Prefer the language's preferred, non-historic label, with stable
        # deterministic ordering when GeoNames lists several alternatives.
        ranked = sorted(candidates, key=lambda item: (
            not item[1], item[2], len(item[0]), item[0].casefold(),
        ))
        hungarian = ranked[0][0]
        known_names = list(dict.fromkeys([
            native, ascii_name, hungarian,
            *[item[0] for item in ranked],
        ]))
        if not native.strip() or not hungarian.strip() or not country:
            continue
        # Keep official/native and Hungarian forms, plus distinct Hungarian
        # historical forms. Punctuation/accents are normalized at read time.
        output.append({
            "geonameid": geonameid,
            "country": country.upper(),
            "native": native,
            "hungarian": hungarian,
            "aliases": "|".join(
                name.replace("|", " ") for name in known_names
                if name.strip()
            ),
        })
    return output


def build_index(sources: list[tuple[Path, Path]], output: Path) -> tuple[int, int]:
    collected: dict[str, dict] = {}
    for places, alternates in sources:
        for row in generate_rows(places, alternates):
            collected[row["geonameid"]] = row
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(sorted(collected.values(),
                                key=lambda item: (item["country"], item["hungarian"], item["geonameid"])))
    temporary.replace(output)
    return len(collected), len(sources)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--countries", default=DEFAULT_COUNTRIES,
                        help="GeoNames country codes, comma-separated (default: RS,HR,RO,SK,UA,SI,AT,HU)")
    parser.add_argument("--world", action="store_true",
                        help="Use worldwide GeoNames dumps instead (much larger download)")
    parser.add_argument("--download", action="store_true",
                        help="Fetch missing GeoNames ZIP archives before building")
    parser.add_argument("--source-dir", type=Path,
                        default=Path(".nyitottaktak_cache/geonames_source"))
    parser.add_argument("--output", type=Path,
                        default=Path(".nyitottaktak_cache/hungarian_places.csv"))
    args = parser.parse_args()

    pairs = []
    if args.world:
        pairs = [(args.source_dir / "allCountries.zip",
                  args.source_dir / "alternateNamesV2.zip")]
        urls = [
            f"{BASE_URL}/allCountries.zip",
            f"{BASE_URL}/alternateNamesV2.zip",
        ]
        print("World mode uses large GeoNames archives. This is a one-time, opt-in download.")
    else:
        countries = [code.strip().upper() for code in args.countries.split(",")]
        if not countries or any(not re.fullmatch(r"[A-Z]{2}", code) for code in countries):
            parser.error("Provide comma-separated two-letter ISO country codes")
        countries = list(dict.fromkeys(countries))
        urls = []
        for country in countries:
            pairs.append((
                args.source_dir / "countries" / f"{country}.zip",
                args.source_dir / "alternatenames" / f"{country}.zip",
            ))
            urls.extend([
                f"{BASE_URL}/{country}.zip",
                f"{BASE_URL}/alternatenames/{country}.zip",
            ])

    try:
        if args.download:
            for archive, url in zip(
                [path for pair in pairs for path in pair], urls
            ):
                download_source(url, archive)
        count, source_count = build_index(pairs, args.output)
    except (ValueError, OSError, zipfile.BadZipFile, HTTPError, URLError) as exc:
        parser.error(str(exc))
    print(f"Hungarian place index: {count} populated settlements "
          f"from {source_count} GeoNames source pair(s) -> {args.output}")
    print("Names that are absent or ambiguous in GeoNames are not automatically equated.")


if __name__ == "__main__":
    main()
