#!/usr/bin/env python3
"""GEDCOM -> Nyitott Akták archival candidate finder.

Only reads a GEDCOM file from disk and calls the public search API with conservative
rate limiting. Results are *possible identity matches*, NOT assertions that someone
was an agent or collaborated with state security.

Designed for common GEDCOM 5.5/5.5.1 and UTF-8 GEDCOM 7 exports. Complex ANSEL
encodings and nonstandard vendor-specific name fields may need conversion.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
import hashlib
import json
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from review_report import review_files, write_review_report
from hungarian_places import PlaceResolver
from scripts.build_place_index import ensure_default_index

API = "https://nyitottaktak.hu/api/search"
API_META = "https://nyitottaktak.hu/api/meta"
DEFAULT_FIRST_YEAR = 1850
DEFAULT_LAST_YEAR = 1975
# These are explicit frontend validation bounds in nyitottaktak.hu's search.js.
# Earlier speculation that the server rejects years after 1950 was incorrect.
API_MIN_YEAR = 1850
DEFAULT_API_YEAR_CEILING = 1990
API_TEXT_LENGTH = {"nev": (2, 25), "szul_hely": (2, 25), "anyja_neve": (2, 25)}
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
LINE = re.compile(r"^(\d+)\s+(?:@([^@]+)@\s+)?([A-Za-z0-9_]+)(?:\s+(.*))?$")
YEAR = re.compile(r"(?<!\d)(1[6789]\d{2}|20\d{2})(?!\d)")


@dataclass
class Person:
    xref: str
    names: list[tuple[str, str]] = field(default_factory=list)  # (name, type)
    structured_names: dict[int, dict[str, str]] = field(default_factory=dict)
    born: str = ""
    birthplace: str = ""
    childhood_families: list[str] = field(default_factory=list)
    spouse_families: list[str] = field(default_factory=list)


@dataclass
class Family:
    husband: str = ""
    wife: str = ""
    children: list[str] = field(default_factory=list)
    marriage_recorded: bool = False


def read_gedcom(path: Path) -> tuple[dict[str, Person], dict[str, Family]]:
    raw = path.read_bytes()
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Hungarian legacy exports are commonly Windows-1250. ANSEL is not CP1250.
        source = raw.decode("cp1250")
        print("WARNING: Non-UTF-8 file decoded as CP1250; verify accented names.", file=sys.stderr)

    people: dict[str, Person] = {}
    families: dict[str, Family] = {}
    owner = None
    kind = ""
    section = ""
    name_index = -1

    for lineno, line in enumerate(source.splitlines(), 1):
        match = LINE.match(line.rstrip("\r"))
        if not match:
            if line.strip():
                print(f"WARNING: Unparsed GEDCOM line {lineno}", file=sys.stderr)
            continue
        level, xref, tag, value = match.groups()
        level = int(level)
        value = (value or "").strip()
        tag = tag.upper()

        if level == 0:
            owner, kind, section, name_index = None, "", "", -1
            if tag == "INDI" and xref:
                owner, kind = xref, "INDI"
                people[xref] = Person(xref)
            elif tag == "FAM" and xref:
                owner, kind = xref, "FAM"
                families[xref] = Family()
            continue
        if owner is None:
            continue
        if kind == "INDI":
            p = people[owner]
            if level == 1:
                section = tag
                if tag == "NAME":
                    p.names.append((value, ""))
                    name_index = len(p.names) - 1
                elif tag == "FAMC" and value.startswith("@"):
                    p.childhood_families.append(value.strip("@"))
                elif tag == "FAMS" and value.startswith("@"):
                    p.spouse_families.append(value.strip("@"))
                elif tag in ("_AKA", "_MARNM") and value:
                    p.names.append((value, tag))
                else:
                    name_index = -1
            elif level == 2:
                if section == "BIRT":
                    if tag == "DATE":
                        p.born = value
                    elif tag == "PLAC":
                        p.birthplace = value
                elif section == "NAME" and name_index >= 0:
                    if tag == "TYPE":
                        existing = p.names[name_index][0]
                        p.names[name_index] = (existing, value.lower())
                    elif tag in ("GIVN", "SURN"):
                        p.structured_names.setdefault(name_index, {})[tag] = value
        elif kind == "FAM" and level == 1:
            f = families[owner]
            if tag == "HUSB":
                f.husband = value.strip("@")
            elif tag == "WIFE":
                f.wife = value.strip("@")
            elif tag == "CHIL":
                f.children.append(value.strip("@"))
            elif tag == "MARR" and value.upper() != "N":
                f.marriage_recorded = True

    # Some GEDCOM exports identify relationships only through FAM members.
    for fid, f in families.items():
        for child in f.children:
            if child in people and fid not in people[child].childhood_families:
                people[child].childhood_families.append(fid)
        for partner in (f.husband, f.wife):
            if partner in people and fid not in people[partner].spouse_families:
                people[partner].spouse_families.append(fid)
    return people, families


def human_name(raw: str) -> str:
    # GEDCOM uses Given /Surname/ Suffix; Hungarian archival searches expect Surname Given.
    surnames = re.findall(r"/([^/]+)/", raw)
    remaining = re.sub(r"/[^/]+/", " ", raw)
    remaining = re.sub(r"\s+", " ", remaining).strip()
    if surnames:
        return " ".join(surnames + [remaining]).strip()
    return remaining


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def norm(value: str) -> str:
    value = strip_accents(value).casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


# GEDCOM 5.x/7 stores surnames between slashes: "Mária /Nagy/".
# A blank pair ("Mária //") means the surname is missing. Do not send
# these first-name-only entries to the API: the resulting searches can be
# extremely broad and may produce misleading candidate matches.
MISSING_SURNAMES = {"unknown", "ismeretlen", "nn", "n n", "none"}


def has_gedcom_surname(raw: str) -> bool:
    """Only accept an explicit, non-placeholder GEDCOM /Surname/ field."""
    surnames = re.findall(r"/([^/]*)/", raw)
    return any(norm(surname) not in MISSING_SURNAMES and bool(norm(surname))
               for surname in surnames)


def candidate_names(person: Person) -> list[str]:
    # GEDCOM's birth NAME is already the maiden name for women: use it directly.
    # Prefer birth/primary names; search explicitly recorded married/alias names
    # as alternatives, without guessing a surname from the spouse.
    # Filter at the source, so unnamed people are skipped in preview AND execute.
    sorted_names = sorted(enumerate(person.names),
                          key=lambda n: (n[1][1] not in ("birth", ""), n[1][1] == "married"))
    names: list[str] = []
    for index, (raw, _type) in sorted_names:
        if has_gedcom_surname(raw):
            candidate = human_name(raw)
        else:
            # GEDCOM permits a structured GIVN/SURN even when NAME is blank
            # or lacks the traditional /Surname/ delimiters.
            components = person.structured_names.get(index, {})
            surname = components.get("SURN", "").strip(" /")
            given = components.get("GIVN", "").strip()
            if not norm(surname) or norm(surname) in MISSING_SURNAMES or not given:
                continue
            candidate = f"{surname} {given}"
        if candidate and candidate.casefold() not in {n.casefold() for n in names}:
            names.append(candidate)
    return names


def mother_names(person: Person, people: dict[str, Person], families: dict[str, Family]) -> list[str]:
    mothers: list[str] = []
    for family_id in person.childhood_families:
        mother_id = families.get(family_id, Family()).wife
        mother = people.get(mother_id)
        if mother:
            for name in candidate_names(mother):
                if name not in mothers:
                    mothers.append(name)
    return mothers


def birth_year(raw: str) -> int | None:
    match = YEAR.search(raw)
    return int(match.group()) if match else None


def gedcom_date(raw: str) -> date | None:
    # Full day/month/year only. Ambiguous or approximate dates yield no exact date.
    if re.search(r"\b(ABT|EST|CAL|BEF|AFT|BET|FROM|TO)\b", raw.upper()):
        return None
    m = re.search(r"\b(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\b", raw.upper())
    if m and m.group(2) in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            pass
    return None


def archive_date(raw: str) -> date | None:
    m = re.search(r"\b(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\b", raw)
    if m:
        try:
            return date(*map(int, m.groups()))
        except ValueError:
            pass
    return None


def first_place(value: str) -> str:
    return value.split(",", 1)[0].strip()


# In GEDCOM exports a municipal district can be appended to the city, e.g.
# "Pécs III.", "Budapest IV", "Budapest XI. kerület" or "Budapest 11. ker.".
# Strip only a *trailing* district number/designator; never alter the original
# GEDCOM birthplace (which is still retained in the results CSV).
DISTRICT_SUFFIX = re.compile(
    r"\s+(?:[IVXLCDM]{1,8}|\d{1,2})\.?"
    r"(?:\s+ker(?:ület)?\.?)?$",
    flags=re.IGNORECASE,
)


def city_name(value: str) -> str:
    """Return the locality without a trailing Hungarian district number."""
    city = first_place(value)
    return DISTRICT_SUFFIX.sub("", city).strip()


# A small explicit alias table avoids treating unrelated nearby towns as one.
# Historical Hungarian and present-day Serbian names of the same locality.
# Other differences in Hungarian accents already normalize via norm().
PLACE_RESOLVER = PlaceResolver()


def normalized_place_input(value: str) -> str:
    """Remove district notation without losing historic country context."""
    city = city_name(value)
    return city + ("," + value.split(",", 1)[1] if "," in value else "")


def normalized_city(value: str) -> str:
    """Canonical named locality, with verified Hungarian exonyms when known."""
    return norm(PLACE_RESOLVER.preferred_hungarian(normalized_place_input(value)))


def clean_api_text(value: str, field_name: str) -> str:
    """Make a GEDCOM value compatible with the website's frontend validation.

    Name permits periods, but birthplace and mother's name do not. This function
    does not truncate names: removing words silently could introduce false hits.
    """
    value = unicodedata.normalize("NFC", value).strip()
    allow_dot = field_name == "nev"
    allowed_punctuation = "- ." if allow_dot else "- "
    value = "".join(c if c.isalpha() or c in "0123456789" or c in allowed_punctuation
                    else " " for c in value)
    value = re.sub(r"\s+", " ", value).strip()
    if field_name != "nev":
        value = re.sub(r"-{2,}", "-", value)
    minimum, maximum = API_TEXT_LENGTH[field_name]
    return value if minimum <= len(value) <= maximum else ""


def api_place(value: str) -> str:
    """Use city-level Hungarian archive names for known historical aliases."""
    localized_city = PLACE_RESOLVER.preferred_hungarian(normalized_place_input(value))
    return clean_api_text(localized_city, "szul_hely")


def api_name(value: str, field_name: str = "nev") -> str:
    """Use a full legal name when valid, or a shorter non-truncated variant."""
    valid = clean_api_text(value, field_name)
    if valid:
        return valid
    words = value.split()
    if len(words) > 2:
        # Keep Hungarian surname + first given name for long multi-part names.
        return clean_api_text(" ".join(words[:2]), field_name)
    return ""


def score(person: Person, archive: dict, names: list[str], mothers: list[str]) -> tuple[int, str, str]:
    reasons: list[str] = []
    archive_name = str(archive.get("nev") or "")
    # Ignore post-comma married-name annotations for core identity comparison.
    first_arch_name = norm(archive_name.split(",", 1)[0])
    match_name = ""
    name_points = 0
    for name in names:
        n = norm(name)
        if not n:
            continue
        if n == first_arch_name:
            points = 45
        elif first_arch_name.startswith(n + " ") or n.startswith(first_arch_name + " "):
            points = 36
        elif SequenceMatcher(None, n, first_arch_name).ratio() >= 0.88:
            points = 23
        else:
            points = 0
        if points > name_points:
            name_points, match_name = points, name
    if name_points == 0:
        return 0, "name does not match", ""
    reasons.append(f"name={name_points}/45")
    points = name_points

    born_year = birth_year(person.born)
    d_person, d_archive = gedcom_date(person.born), archive_date(str(archive.get("szul_ido") or ""))
    archive_year = birth_year(str(archive.get("szul_ido") or ""))
    if d_person and d_archive:
        if d_person == d_archive:
            points += 30
            reasons.append("exact DOB")
        elif d_person.year == d_archive.year:
            points -= 13
            reasons.append("DOB conflict (day/month)")
        else:
            points -= 30
            reasons.append("DOB conflict (year)")
    elif born_year and archive_year:
        distance = abs(born_year - archive_year)
        if distance == 0:
            points += 20
            reasons.append("birth year matches")
        elif distance == 1:
            points += 5
            reasons.append("birth years differ by 1")
        else:
            points -= 30
            reasons.append("birth-year conflict")
    else:
        reasons.append("DOB unavailable on one side")

    archive_mother = norm(str(archive.get("anyja_neve") or ""))
    known_mothers = [norm(m) for m in mothers if norm(m)]
    if archive_mother and known_mothers:
        if archive_mother in known_mothers:
            points += 20
            reasons.append("mother exact")
        elif any(archive_mother in x or x in archive_mother for x in known_mothers):
            points += 11
            reasons.append("mother partial")
        else:
            points -= 15
            reasons.append("mother name differs")
    else:
        reasons.append("mother unavailable on one side")

    # Apply the same city-level normalization when scoring; otherwise e.g.
    # "Pécs III." (GEDCOM) vs. "Pécs" (archive) only receives partial credit.
    ged_place = normalized_city(person.birthplace)
    archive_place = normalized_city(str(archive.get("szul_hely") or ""))
    if ged_place and archive_place:
        if ged_place == archive_place or PLACE_RESOLVER.equivalent(
                normalized_place_input(person.birthplace),
                normalized_place_input(str(archive.get("szul_hely") or ""))):
            points += 10
            reasons.append("birthplace exact")
        elif ged_place in archive_place or archive_place in ged_place:
            points += 5
            reasons.append("birthplace partial")
        else:
            points -= 5
            reasons.append("birthplace differs")
    else:
        reasons.append("birthplace unavailable on one side")

    return max(0, min(100, points)), "; ".join(reasons), match_name


class ApiQueryError(RuntimeError):
    """An API HTTP failure, including its status and a short response body."""

    def __init__(self, status: int, url: str, detail: str):
        self.status = status
        self.url = url
        self.detail = detail
        super().__init__(f"HTTP {status} for {url}" + (f"; server said: {detail}" if detail else ""))


class ApiClient:
    def __init__(self, cache_dir: Path, delay: float, max_requests: int, refresh: bool):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = max(delay, 0)
        self.max_requests = max_requests
        self.refresh = refresh
        self.requests = 0
        self.last_call = 0.0
        self.ok_responses = 0
        self.http_400 = 0
        self.cached_responses = 0

    def _request_json(self, url: str) -> dict:
        """Issue one paced, budgeted request for either API endpoint."""
        # Local request limits are unrelated to any upstream rate limit.
        if self.max_requests and self.requests >= self.max_requests:
            raise RuntimeError(f"Request budget reached ({self.max_requests}); rerun to continue from cache")
        elapsed = time.monotonic() - self.last_call
        if self.last_call and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "GenealogyArchiveResearch/0.1"})
        try:
            self.requests += 1
            self.last_call = time.monotonic()
            with urlopen(request, timeout=25) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code == 400:
                self.http_400 += 1
            detail = exc.read(500).decode("utf-8", errors="replace").strip()
            detail = re.sub(r"\s+", " ", detail)[:240]
            raise ApiQueryError(exc.code, url, detail) from exc
        except URLError as exc:
            raise RuntimeError(f"Archive connection failed ({exc}) for {url}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected response for {url}: expected JSON object")
        self.ok_responses += 1
        return result

    def fetch_meta(self) -> str:
        """Get the archive's database update date; never reuse search cache."""
        result = self._request_json(API_META)
        value = result.get("adatbazis_frissites")
        if not isinstance(value, str):
            raise ValueError("API metadata does not contain adatbazis_frissites (YYYY-MM-DD)")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid archive metadata date: {value!r}") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"Invalid archive metadata date: {value!r}")
        return value

    def fetch(self, params: dict) -> dict:
        filtered = {k: v for k, v in params.items() if v not in (None, "")}
        query_string = urlencode(sorted(filtered.items()))
        cache_file = self.cache_dir / (hashlib.sha256(query_string.encode()).hexdigest() + ".json")
        if cache_file.exists() and not self.refresh:
            self.cached_responses += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))
        url = f"{API}?{query_string}"
        result = self._request_json(url)
        if "results" not in result:
            raise RuntimeError(f"Unexpected response for {url}: expected object with 'results'")
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def search_all(self, filters: dict) -> tuple[list[dict], bool, str, bool]:
        base = {**filters, "sort": "nev", "dir": "asc"}
        records: list[dict] = []
        too_many = False
        has_missing_birth_date = False
        page = 1
        while True:
            payload = self.fetch({**base, "page": page})
            records.extend(payload.get("results") or [])
            too_many = too_many or bool(payload.get("too_many"))
            has_missing_birth_date = has_missing_birth_date or bool(payload.get("has_missing_birth_date"))
            # The website treats too_many as a failed/overly broad query, not
            # a paginated result that can safely be considered complete.
            if too_many:
                break
            page_size = max(1, int(payload.get("page_size") or 25))
            total = int(payload.get("total") or 0)
            # Only respect a result cap if the server explicitly reports one.
            # A former hard-coded 250 fallback could silently truncate searches.
            cap = int(payload.get("max_results") or total)
            if page * page_size >= min(total, cap) or not payload.get("results"):
                break
            page += 1
        return records, too_many, f"{API}?{urlencode(sorted({**base, 'page': 1}.items()))}", has_missing_birth_date


def build_steps(name: str, year: int | None, birthplace: str, mothers: list[str], args: argparse.Namespace) -> list[dict]:
    """Use only valid filters, relaxing uncertain identity data as needed."""
    search_name = api_name(name)
    if not search_name:
        return []
    full: dict = {"nev": search_name}
    ceiling = min(args.api_year_ceiling or DEFAULT_API_YEAR_CEILING,
                  DEFAULT_API_YEAR_CEILING)
    if year is not None and API_MIN_YEAR <= year <= ceiling:
        lo = max(API_MIN_YEAR, year - args.year_tolerance)
        hi = min(ceiling, year + args.year_tolerance)
        if lo <= hi:
            full["szul_tol"] = lo
            # The site's JS omits szul_ig when it equals szul_tol.
            if lo != hi:
                full["szul_ig"] = hi
    place = api_place(birthplace)
    if place:
        full["szul_hely"] = place
    for mother in mothers:
        mother_filter = api_name(mother, "anyja_neve")
        if mother_filter:
            full["anyja_neve"] = mother_filter
            break
    steps = [full]
    # Place names and maternal names vary in historical documents: try either
    # filter independently before falling back to the years and then name only.
    if "szul_hely" in full:
        steps.append({k: v for k, v in full.items() if k != "szul_hely"})
    if "anyja_neve" in full:
        steps.append({k: v for k, v in full.items() if k != "anyja_neve"})
    if "anyja_neve" in full or "szul_hely" in full:
        steps.append({k: v for k, v in full.items() if k not in ("anyja_neve", "szul_hely")})
    steps.append({"nev": search_name})
    # Keep first occurrence, preserving order.
    return list({tuple(sorted(p.items())): p for p in steps}.values())


def historical_order(person: Person) -> tuple:
    """Prioritize adults of the 1956-1989 period; only affects run order."""
    year = birth_year(person.born)
    return (0 if year is not None else 1,
            abs(year - 1930) if year is not None else 9999,
            year if year is not None else 9999,
            person.xref)


# Source-type explanations summarize the archive's published FAQ and record-type
# descriptions (nyitottaktak.hu, accessed 2026-11-04). They explain what the
# *record type* represents, not the identity or actions of an individual.
ARCHIVE_SOURCE_NOTES = {
    "6 os karton": (
        "State-security registration card",
        "May concern actual, unsuccessful, or fictitious recruitment; the card "
        "alone cannot establish whether substantive cooperation occurred.",
    ),
    "b naplo": (
        "Register of archived recruitment dossiers",
        "Only five surviving registers from 1956–1962 are described by the "
        "archive; a registry entry alone does not establish collaboration.",
    ),
    "b dosszie": (
        "Recruitment dossier",
        "Can document recruitment attempts and activity, but historical "
        "claims and the person's identity require examination of the files.",
    ),
    "magnesszalag": (
        "H-register magnetic-tape data",
        "An incomplete January 1990 registry snapshot. Earlier exclusions "
        "and later deletions mean a missing entry is not conclusive.",
    ),
    "h adattar": (
        "H-register magnetic-tape data",
        "An incomplete January 1990 registry snapshot. Earlier exclusions "
        "and later deletions mean a missing entry is not conclusive.",
    ),
}


def archive_source_context(record_type: str) -> tuple[str, str]:
    """Summarize document provenance without inferring a historical role."""
    key = norm(record_type)
    return ARCHIVE_SOURCE_NOTES.get(
        key,
        (
            "Archive document type not recognized",
            "Check the original document and its source description before "
            "drawing conclusions about identity or historical activity.",
        ),
    )


CHECKPOINT_VERSION = 1


def checkpoint_config(args: argparse.Namespace) -> dict:
    """Only search/scoring options belong here, not per-run budgets or scope."""
    return {
        "exhaustive": args.exhaustive,
        "year_tolerance": args.year_tolerance,
        "api_year_ceiling": args.api_year_ceiling,
        "min_score": args.min_score,
    }


def load_checkpoint(path: Path, gedcom_digest: str, config: dict) -> dict:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Unreadable checkpoint {path}: {exc}") from exc
    if (state.get("version") != CHECKPOINT_VERSION or
            state.get("gedcom_sha256") != gedcom_digest or state.get("config") != config):
        raise ValueError(
            f"Checkpoint {path} belongs to a different GEDCOM or search configuration. "
            "Use --checkpoint with a new filename, or --no-resume to start fresh."
        )
    entries = state.get("completed")
    if not isinstance(entries, dict):
        raise ValueError(f"Invalid checkpoint format: {path}")
    return entries


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def check_archive_update(client: ApiClient, cache_dir: Path, required: bool = False) -> str | None:
    """Check fresh /api/meta and remember the last observed database date.

    A metadata timestamp records a changed *dataset*, not which record changed.
    Unlike search results, this call must never come from the URL response cache.
    """
    try:
        updated = client.fetch_meta()
    except ApiQueryError as exc:
        if exc.status in (403, 429):
            raise ValueError(f"Archive rejected metadata request (HTTP {exc.status}); stopping") from exc
        if required:
            raise ValueError(f"Could not check archive database date: {exc}") from exc
        print(f"WARNING: Could not check archive database date: {exc}", file=sys.stderr)
        return None
    except (RuntimeError, ValueError) as exc:
        if required:
            raise ValueError(f"Could not check archive database date: {exc}") from exc
        print(f"WARNING: Could not check archive database date: {exc}", file=sys.stderr)
        return None

    path = cache_dir / "database-meta.json"
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8")).get("last_seen")
        except (ValueError, OSError, AttributeError) as exc:
            print(f"WARNING: Could not read previous archive metadata: {exc}", file=sys.stderr)
    if previous and previous < updated:
        print(f"ARCHIVE DATABASE UPDATED: {previous} -> {updated}")
        print("  Previously completed searches may be stale; use --refresh-stale to recheck them.")
    elif previous and previous > updated:
        print(f"ARCHIVE DATABASE DATE MOVED BACKWARD: {previous} -> {updated}; investigate before interpreting changes.")
    elif previous == updated:
        print(f"Archive database: {updated} (unchanged since last check)")
    else:
        print(f"Archive database: {updated} (first local observation)")
    atomic_json(path, {
        "last_seen": updated,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    })
    return updated


def checkpoint_freshness(completed: dict, eligible_ids: set[str], updated: str | None) -> tuple[int, int, int]:
    """Count fresh, older and unknown searches for the current eligible scope."""
    fresh = older = unknown = 0
    for xref, entry in completed.items():
        if xref not in eligible_ids:
            continue
        checked = entry.get("source_updated")
        if not updated or not checked:
            unknown += 1
        elif checked == updated:
            fresh += 1
        else:
            older += 1
    return fresh, older, unknown


def snapshot_before_refresh(output: Path) -> Path | None:
    """Preserve the previous CSV before any refresh invalidates completed rows."""
    if not output.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = output.with_name(f"{output.stem}.pre-refresh-{stamp}{output.suffix}")
    backup = base
    counter = 1
    while backup.exists():
        backup = output.with_name(f"{output.stem}.pre-refresh-{stamp}-{counter}{output.suffix}")
        counter += 1
    shutil.copy2(output, backup)
    return backup


def write_report(output: Path, columns: list[str], completed: dict) -> None:
    """Write a complete cumulative CSV from successfully checkpointed people."""
    rows = [row for entry in completed.values() for row in entry["rows"]]
    rows.sort(key=lambda row: (-int(row["match_score"]), row["gedcom_id"], row["archive_guid"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        # Derive contextual columns at export time, not in checkpoints. This
        # enriches previously saved candidate rows without forcing a rescan.
        for row in rows:
            summary, limits = archive_source_context(str(row.get("archive_record_type") or ""))
            writer.writerow({
                **row,
                "archive_source_summary": summary,
                "archive_interpretation_limits": limits,
            })
    temporary.replace(output)


def coverage_rows(eligible: list[Person], completed: dict) -> list[dict]:
    """Report query processing, not archival completeness or personal conduct."""
    rows = []
    for person in eligible:
        entry = completed.get(person.xref)
        warnings = (entry or {}).get("warnings") or []
        status = ("not_completed" if entry is None else
                  "limited" if warnings else "completed")
        rows.append({
            "gedcom_id": person.xref,
            "gedcom_name": (entry or {}).get("name") or (candidate_names(person) or [""])[0],
            "gedcom_birth": person.born,
            "search_status": status,
            "raw_records": (entry or {}).get("raw_count", ""),
            "candidate_rows": len(entry.get("rows", [])) if entry is not None else "",
            "archive_database_date": (entry or {}).get("source_updated") or "",
            "search_warnings": " | ".join(str(w) for w in warnings),
        })
    return rows


def write_coverage_report(output: Path, eligible: list[Person], completed: dict) -> Counter:
    """Atomically write selected people's scan status, including missing entries."""
    rows = coverage_rows(eligible, completed)
    columns = ["gedcom_id", "gedcom_name", "gedcom_birth", "search_status",
               "raw_records", "candidate_rows", "archive_database_date", "search_warnings"]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)
    return Counter(row["search_status"] for row in rows)


def run(args: argparse.Namespace) -> None:
    people, families = read_gedcom(args.gedcom)
    eligible: list[Person] = []
    missing_years = 0
    missing_surnames = 0
    requested_ids = [value.strip().strip("@") for value in (args.person_id or [])]
    missing_ids = [value for value in requested_ids if value not in people]
    if missing_ids:
        raise ValueError("GEDCOM person ID(s) not found: " + ", ".join(missing_ids))
    requested_set = set(requested_ids)
    for person in people.values():
        if requested_set and person.xref not in requested_set:
            continue
        yr = birth_year(person.born)
        if not candidate_names(person):
            missing_surnames += 1
            continue
        if yr is None and not args.include_undated:
            missing_years += 1
            continue
        if yr is not None and not (args.since <= yr <= args.until):
            continue
        eligible.append(person)
    print(f"Loaded {len(people)} individuals; {len(eligible)} eligible; "
          f"{missing_surnames} without a surname skipped; "
          f"{missing_years} without birth year skipped.")
    if requested_ids:
        eligible.sort(key=lambda person: requested_ids.index(person.xref))
    elif args.order == "historical":
        eligible.sort(key=historical_order)
    elif args.order == "oldest":
        eligible.sort(key=lambda p: (birth_year(p.born) if birth_year(p.born) is not None else 9999, p.xref))
    # Zero means no client-side cap on eligible people.
    if args.max_people:
        eligible = eligible[:args.max_people]
    coverage_path = args.coverage_output or args.output.with_suffix(".coverage.csv")
    if args.coverage_only:
        checkpoint_path = args.checkpoint or args.output.with_suffix(".checkpoint.json")
        current = load_checkpoint(
            checkpoint_path, hashlib.sha256(args.gedcom.read_bytes()).hexdigest(),
            checkpoint_config(args))
        counts = write_coverage_report(coverage_path, eligible, current)
        print(f"Coverage: {counts['completed']} completed, {counts['limited']} limited, "
              f"{counts['not_completed']} not completed -> {coverage_path}")
        print("NOTE: Completed means the tool finished its planned queries without reported "
              "limits, not that the archive contains every relevant document.")
        return
    if not args.execute:
        print("PREVIEW ONLY. Add --execute to query the site. First eligible people:")
        for person in eligible[:20]:
            mom = ", ".join(mother_names(person, people, families)) or "unknown"
            original_place = first_place(person.birthplace)
            search_place = api_place(person.birthplace)
            place_label = original_place or "?"
            if search_place and search_place != original_place:
                place_label += f" -> search: {search_place}"
            print(f"  {person.xref}: {', '.join(candidate_names(person))} | {person.born or '?'} | {place_label} | mother: {mom}")
        return

    client = ApiClient(args.cache_dir, args.delay, args.max_requests, args.refresh)
    archive_updated = check_archive_update(client, args.cache_dir, required=args.refresh_stale)
    checkpoint_path = args.checkpoint or args.output.with_suffix(".checkpoint.json")
    gedcom_digest = hashlib.sha256(args.gedcom.read_bytes()).hexdigest()
    config = checkpoint_config(args)
    existing_checkpoint = checkpoint_path.exists()
    completed = ({} if args.no_resume
                 else load_checkpoint(checkpoint_path, gedcom_digest, config))
    if not existing_checkpoint and args.output.exists():
        # v4 had no checkpoint. Keep its report until the new run regenerates
        # the same results from cached API responses.
        backup = args.output.with_name(args.output.stem + ".pre-checkpoint" + args.output.suffix)
        if not backup.exists():
            shutil.copy2(args.output, backup)
            print(f"Existing pre-checkpoint CSV preserved as: {backup}")
    if completed:
        restored = sum(len(entry["rows"]) for entry in completed.values())
        print(f"Resuming: {len(completed)} completed individuals and "
              f"{restored} candidates retained from {checkpoint_path}")
        fresh, older, unknown = checkpoint_freshness(
            completed, {person.xref for person in eligible}, archive_updated)
        print(f"Archive freshness for selected people: {fresh} current, "
              f"{older} older snapshot, {unknown} unknown.")
        if older or unknown:
            print("  Checkpoint progress is preserved. "
                  "Use --refresh-stale to re-query older/unknown entries, if desired.")
    columns = ["gedcom_id", "gedcom_name", "gedcom_birth", "gedcom_birth_place", "gedcom_mother",
               "archive_guid", "archive_name", "archive_birth", "archive_birth_place", "archive_mother",
               "archive_cover_name", "archive_record_type", "match_score", "review_priority", "evidence",
               "searched_name", "api_search_url", "archive_document_url",
               "archive_source_summary", "archive_interpretation_limits"]
    errors: list[str] = []
    recovered_400s: list[str] = []
    incomplete_searches: list[str] = []
    stop_now = False
    skipped_from_checkpoint = 0
    backup_made = False

    def persist() -> None:
        atomic_json(checkpoint_path, {
            "version": CHECKPOINT_VERSION,
            "gedcom_sha256": gedcom_digest,
            "config": config,
            "completed": completed,
        })
        write_report(args.output, columns, completed)
        write_coverage_report(coverage_path, eligible, completed)
        review_path = getattr(args, "review_output", None) or args.output.with_suffix(".review.csv")
        write_review_report(
            review_path,
            [row for entry in completed.values() for row in entry.get("rows", [])],
            coverage_rows(eligible, completed),
        )

    for index, person in enumerate(eligible, 1):
        prior = completed.get(person.xref)
        is_stale = bool(
            prior is not None and archive_updated and
            prior.get("source_updated") != archive_updated
        )
        requery = args.refresh or (args.refresh_stale and is_stale)
        if prior is not None and not requery:
            skipped_from_checkpoint += 1
            continue
        if prior is not None and requery:
            if not backup_made:
                backed_up = snapshot_before_refresh(args.output)
                if backed_up:
                    print(f"Pre-refresh candidate report saved as: {backed_up}")
                backup_made = True
            # Remove the old completion *before* starting the refresh so an
            # interrupted request cannot silently retain stale identity leads.
            completed.pop(person.xref)
            persist()
        # On an explicit refresh, bypass all cached queries for this person.
        client.refresh = bool(args.refresh or (args.refresh_stale and is_stale))
        before_cached = client.cached_responses
        names = candidate_names(person)
        mothers = mother_names(person, people, families)
        year = birth_year(person.born)
        birthplace = city_name(person.birthplace)
        print(f"[{index}/{len(eligible)}] {names[0]} ({year or '?'})", flush=True)
        archive_hits: dict[str, tuple[dict, str, str]] = {}
        missing_birth_date_notice = False
        person_failed = False
        person_warnings: list[str] = []
        person_rows: list[dict] = []

        # Search names separately (birth, married/aliases) with a filtered-first
        # approach. If that returns nothing, progressively remove strict fields.
        # --exhaustive additionally searches names alone even when filtered matches exist.
        to_search: list[str] = []
        for name in names:
            if name not in to_search:
                to_search.append(name)
            # Accent-folded alternative may be required for inconsistent indexing.
            if args.exhaustive and strip_accents(name) != name and strip_accents(name) not in to_search:
                to_search.append(strip_accents(name))

        for name in to_search:
            deduped_steps = build_steps(name, year, birthplace, mothers, args)
            if not deduped_steps:
                warning = f"{person.xref}: No valid search-name variant for {name!r} (2–25 characters)"
                errors.append(warning)
                person_warnings.append(warning)
                continue
            for step_num, step in enumerate(deduped_steps):
                # Only stop broadening on an actual identity candidate, rather
                # than any unrelated substring result returned by the API.
                if (step_num and not args.exhaustive and
                        any(score(person, hit[0], names, mothers)[0] >= args.min_score
                            for hit in archive_hits.values())):
                    break
                if args.verbose:
                    print(f"  QUERY: {urlencode(step)}", flush=True)
                try:
                    results, capped, url, includes_missing_birth_dates = client.search_all(step)
                except ApiQueryError as exc:
                    if exc.status == 400:
                        # Usually a rejected filter combination. Keep going to a
                        # less-specific query and record what the server said.
                        warning = f"{person.xref}: HTTP 400 rejected search filter; broader alternatives attempted"
                        recovered_400s.append(f"{person.xref}: {exc}")
                        if warning not in person_warnings:
                            person_warnings.append(warning)
                        print(f"  WARN: rejected filter (HTTP 400); trying broader search. {exc.detail[:120]}", file=sys.stderr)
                        continue
                    errors.append(f"{person.xref}: {exc}")
                    print(f"  ERROR: {exc}", file=sys.stderr)
                    person_failed = True
                    if exc.status in (403, 429):
                        stop_now = True
                        break
                    continue
                except RuntimeError as exc:
                    person_failed = True
                    errors.append(f"{person.xref}: {exc}")
                    print(f"  ERROR: {exc}", file=sys.stderr)
                    if "Request budget reached" in str(exc):
                        stop_now = True
                        break
                    continue
                if args.verbose:
                    print(f"  RESULT: {len(results)} records; too_many={capped}; "
                          f"missing_birth_dates={includes_missing_birth_dates}", flush=True)
                if capped:
                    warning = f"{person.xref}: search for {name!r} returned too_many; narrow the filters"
                    incomplete_searches.append(warning)
                    person_warnings.append(warning)
                    print(f"  WARNING: {warning}", file=sys.stderr)
                if includes_missing_birth_dates:
                    missing_birth_date_notice = True
                for result in results:
                    guid = str(result.get("guid") or f"unnamed:{hash(json.dumps(result, sort_keys=True))}")
                    archive_hits.setdefault(guid, (result, name, url))
                if capped:
                    # Removing more filters can only make an already too-broad
                    # search worse. Try another name variant instead.
                    break
            if stop_now:
                break

        accepted_for_person = 0
        for result, searched_name, url in archive_hits.values():
            quality, reasons, matched_name = score(person, result, names, mothers)
            if quality < args.min_score:
                continue
            priority = "strong candidate" if quality >= 85 else ("possible candidate" if quality >= 60 else "weak candidate")
            accepted_for_person += 1
            person_rows.append({
                "gedcom_id": person.xref,
                "gedcom_name": matched_name or names[0],
                "gedcom_birth": person.born,
                "gedcom_birth_place": person.birthplace,
                "gedcom_mother": "; ".join(mothers),
                "archive_guid": result.get("guid", ""),
                "archive_name": result.get("nev", ""),
                "archive_birth": result.get("szul_ido", ""),
                "archive_birth_place": result.get("szul_hely", ""),
                "archive_mother": result.get("anyja_neve", ""),
                "archive_cover_name": result.get("fedonev", ""),
                "archive_record_type": result.get("irattipus", ""),
                "match_score": quality,
                "review_priority": priority,
                "evidence": reasons,
                "searched_name": searched_name,
                "api_search_url": url,
                "archive_document_url": (
                    f"https://nyitottaktak.hu/#dokumentum/{result['guid']}"
                    if result.get("guid") else ""
                ),
            })
        print(f"  Raw records: {len(archive_hits)}; candidates after score filter: {accepted_for_person}", flush=True)
        if not person_failed and not stop_now:
            completed[person.xref] = {
                "name": names[0],
                "raw_count": len(archive_hits),
                "rows": person_rows,
                "warnings": person_warnings,
                # A cached query could predate the site's latest update. Never
                # label it current unless every HTTP response was fetched live.
                "source_updated": (
                    archive_updated if client.cached_responses == before_cached else None
                ),
            }
            persist()
        else:
            print("  NOTE: Person not checkpointed because search was interrupted or failed.")
        if missing_birth_date_notice:
            print("  NOTE: Archive search includes record types without birth dates; identity checks may be weaker.")
        if stop_now:
            print("Stopping early to respect the site's limits.", file=sys.stderr)
            break

    # Write even if no people were searched (e.g. fully resumed).
    persist()
    raw_unique_results = sum(int(entry.get("raw_count", 0)) for entry in completed.values())
    people_with_raw_results = sum(int(entry.get("raw_count", 0)) > 0 for entry in completed.values())
    candidate_count = sum(len(entry["rows"]) for entry in completed.values())
    saved_warnings = [w for entry in completed.values() for w in entry.get("warnings", [])]
    error_file = args.output.with_suffix(".errors.txt")
    if errors or recovered_400s or incomplete_searches or saved_warnings:
        error_file.write_text("\n".join(dict.fromkeys(
            errors + recovered_400s + incomplete_searches + saved_warnings)) + "\n", encoding="utf-8")
        print(f"Errors and rejected filters saved to: {error_file}", file=sys.stderr)
    elif error_file.exists():
        error_file.unlink()  # Don't leave stale errors from a previous run.
    print(f"Finished: {client.requests} network requests ({client.ok_responses} successful, "
          f"{client.http_400} HTTP 400, {client.cached_responses} cache hits); "
          f"{raw_unique_results} unique raw records across {people_with_raw_results} people; "
          f"{candidate_count} candidate rows -> {args.output}")
    print(f"Checkpoint: {len(completed)} saved people; {skipped_from_checkpoint} skipped this run; "
          f"{sum(p.xref not in completed for p in eligible)} remaining in selected scope.")
    fresh, older, unknown = checkpoint_freshness(
        completed, {person.xref for person in eligible}, archive_updated)
    print(f"Archive freshness: {fresh} current, {older} older snapshot, {unknown} unknown "
          "among completed people in this scope.")
    coverage_counts = Counter(row["search_status"] for row in coverage_rows(eligible, completed))
    print(f"Search coverage: {coverage_counts['completed']} completed, "
          f"{coverage_counts['limited']} limited, "
          f"{coverage_counts['not_completed']} not completed -> {coverage_path}")
    print(f"Identity review: {getattr(args, 'review_output', None) or args.output.with_suffix('.review.csv')} "
          "(offline triage, not a finding of historical conduct).")
    if stop_now or errors or incomplete_searches or saved_warnings:
        print("NOTE: Some searches failed or were not completed; absence of matches is not conclusive.")
    print("IMPORTANT: A match is an identity lead, not evidence that someone was an agent.")
    print("ARCHIVE SCOPE: No public search hit does not rule out archival records "
          "or historical involvement. Check original documents before drawing conclusions.")


def rescore_csv(source: Path, target: Path, coverage: Path) -> tuple[int, int]:
    """Recalculate existing candidate rows offline without changing checkpoints.

    The saved GEDCOM identity fields are sufficient to reproduce the scoring
    applied to each already-accepted result. This cannot recover records that
    were originally rejected by the score threshold or never returned by API.
    """
    if source.resolve() == target.resolve() or target.resolve() == coverage.resolve():
        raise ValueError("Rescored output must not overwrite original candidates or coverage")
    if not source.is_file():
        raise ValueError(f"Candidate CSV not found: {source}")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        required = {
            "gedcom_id", "gedcom_name", "gedcom_birth", "gedcom_birth_place",
            "gedcom_mother", "archive_name", "archive_birth", "archive_birth_place",
            "archive_mother", "match_score", "evidence", "review_priority",
        }
        if not required.issubset(set(columns)):
            raise ValueError(f"Candidate CSV missing columns: {', '.join(sorted(required - set(columns)))}")
        rows = list(reader)

    changed = 0
    for row in rows:
        person = Person(
            xref=row["gedcom_id"], born=row["gedcom_birth"],
            birthplace=row["gedcom_birth_place"],
        )
        archive = {
            "nev": row["archive_name"], "szul_ido": row["archive_birth"],
            "szul_hely": row["archive_birth_place"],
            "anyja_neve": row["archive_mother"],
        }
        mother_names_in_csv = [x.strip() for x in row["gedcom_mother"].split(";") if x.strip()]
        score_value, evidence, _ = score(
            person, archive, [row["gedcom_name"]], mother_names_in_csv)
        try:
            previous_score = int(row["match_score"])
        except ValueError as exc:
            raise ValueError(f"Invalid match score in {source}: {row['match_score']!r}") from exc
        delta = score_value - previous_score
        if delta:
            changed += 1
        row["previous_match_score"] = previous_score
        row["score_change"] = delta
        row["match_score"] = score_value
        row["evidence"] = evidence
        row["review_priority"] = (
            "strong candidate" if score_value >= 85 else
            "possible candidate" if score_value >= 60 else "weak candidate"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    fieldnames = columns + [name for name in ("previous_match_score", "score_change")
                            if name not in columns]
    rows.sort(key=lambda row: (-int(row["match_score"]), row["gedcom_id"], row.get("archive_guid", "")))
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(target)
    return len(rows), changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gedcom", nargs="?", type=Path, help="Local .ged/.gedcom file")
    parser.add_argument("--execute", action="store_true", help="Actually send API requests (otherwise preview only)")
    parser.add_argument("--person-id", action="append", default=[], metavar="ID",
                        help="Only search this GEDCOM individual (repeatable; e.g. I123 or @I123@)")
    parser.add_argument("--verbose", action="store_true", help="Print API query filters and result counts")
    parser.add_argument("--checkpoint", type=Path, default=None,
                        help="Progress file (default: output CSV with .checkpoint.json suffix)")
    parser.add_argument("--no-resume", action="store_true",
                        help="Discard previous checkpoint progress and start a fresh result set")
    parser.add_argument("--exhaustive", action="store_true", help="Also search name-only and unaccented variants")
    parser.add_argument("--include-undated", action="store_true", help="Include GEDCOM people with no birth year")
    parser.add_argument("--since", type=int, default=DEFAULT_FIRST_YEAR)
    parser.add_argument("--until", type=int, default=DEFAULT_LAST_YEAR)
    parser.add_argument("--year-tolerance", type=int, default=2)
    parser.add_argument("--api-year-ceiling", type=int, default=DEFAULT_API_YEAR_CEILING,
                        help="Maximum birth year used in API filters (website permits 1850–1990; default: 1990; 0 resets to 1990)")
    parser.add_argument("--order", choices=("historical", "oldest", "gedcom"), default="historical",
                        help="Search ordering; historical prioritizes adults around 1956 (default)")
    parser.add_argument("--max-people", type=int, default=30,
                        help="Maximum eligible people to process (default: 30; 0 = all)")
    parser.add_argument("--max-requests", type=int, default=250,
                        help="Local HTTP request budget per invocation (default: 250; 0 = unlimited)")
    parser.add_argument("--all", action="store_true",
                        help="Process all eligible people in one invocation (sets both budgets to unlimited; "
                             "add --include-undated separately if needed)")
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between uncached requests")
    parser.add_argument("--min-score", type=int, default=35)
    parser.add_argument("--output", type=Path, default=Path("nyitottaktak_matches.csv"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".nyitottaktak_cache"))
    parser.add_argument("--refresh", action="store_true", help="Refresh cached API responses")
    parser.add_argument("--place-index", type=Path, default=None,
                        help="Custom local Hungarian place index CSV (overrides automatic default)")
    parser.add_argument("--no-auto-place-index", action="store_true",
                        help="Skip automatic GeoNames download on first executed scan; "
                             "use built-in aliases if no local index exists")
    parser.add_argument("--rescore-only", action="store_true",
                        help="Recalculate existing candidate scores offline into a separate CSV; no GEDCOM or API calls")
    parser.add_argument("--review-only", action="store_true",
                        help="Build grouped candidate review from local CSVs without GEDCOM or network")
    parser.add_argument("--review-input", type=Path, default=None,
                        help="Candidate CSV for --review-only (default: --output)")
    parser.add_argument("--review-coverage-input", type=Path, default=None,
                        help="Coverage CSV for --review-only (default: derived from candidate CSV)")
    parser.add_argument("--review-output", type=Path, default=None,
                        help="Grouped identity review CSV (default: candidate CSV with .review.csv suffix)")
    parser.add_argument("--coverage-only", action="store_true",
                        help="Generate a scan coverage CSV from the GEDCOM and checkpoint without making HTTP requests")
    parser.add_argument("--coverage-output", type=Path, default=None,
                        help="Coverage CSV path (default: output CSV with .coverage.csv suffix)")
    parser.add_argument("--check-updates", action="store_true",
                        help="Check /api/meta for changes and exit (GEDCOM file not required)")
    parser.add_argument("--refresh-stale", action="store_true",
                        help="Recheck completed people searched before the latest database update; "
                             "unknown-date checkpoints are also rechecked")
    args = parser.parse_args()
    if args.all:
        args.max_people = 0
        args.max_requests = 0
    if (args.delay < 0.5 or args.max_people < 0 or args.max_requests < 0 or
            args.min_score < 0 or args.year_tolerance < 0):
        parser.error("Use --delay >= 0.5, nonnegative budgets (0 = unlimited), score and tolerance")
    global PLACE_RESOLVER
    place_index = args.place_index
    if place_index is None:
        candidate_index = args.cache_dir / "hungarian_places.csv"
        if candidate_index.is_file():
            place_index = candidate_index
        elif (args.execute and not args.no_auto_place_index and
              not (args.check_updates or args.rescore_only or
                   args.review_only or args.coverage_only)):
            # Only actual archive scans can download additional data. Preview
            # and offline/metadata-only commands preserve their no-GeoNames
            # network guarantees. Check inputs before starting a download.
            if args.gedcom is None or not args.gedcom.is_file():
                parser.error(f"GEDCOM file not found: {args.gedcom}")
            try:
                place_index = ensure_default_index(args.cache_dir)
            except (OSError, URLError, ValueError, zipfile.BadZipFile) as exc:
                parser.error(
                    "Automatic Hungarian place index setup failed: "
                    f"{exc}. Retry, or use --no-auto-place-index to search "
                    "with built-in place aliases only."
                )
    try:
        PLACE_RESOLVER = PlaceResolver(place_index)
    except (ValueError, OSError, csv.Error) as exc:
        parser.error(str(exc))
    if place_index is not None:
        print(f"Hungarian place index: {place_index}")
    if args.check_updates:
        try:
            client = ApiClient(args.cache_dir, args.delay, args.max_requests, False)
            check_archive_update(client, args.cache_dir, required=True)
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.rescore_only:
        source = args.review_input or args.output
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        target = source.with_name(source.stem + ".rescored" + source.suffix)
        review_target = target.with_suffix(".review.csv")
        try:
            count, changed = rescore_csv(source, target, coverage)
            people, _, has_coverage = review_files(target, coverage, review_target)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline rescoring: {count} candidate rows; {changed} scores changed -> {target}")
        print(f"Identity review: {people} relatives -> {review_target}")
        if not has_coverage:
            print("NOTE: Coverage CSV missing; the review omits unsearched people.")
        print("NOTE: This only re-scores existing candidate records; it cannot recover previously discarded matches.")
        return
    if args.review_only:
        source = args.review_input or args.output
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        target = args.review_output or source.with_suffix(".review.csv")
        try:
            people, counts, has_coverage = review_files(source, coverage, target)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline identity review: {people} relatives, "
              f"{sum(counts.values())} grouped rows -> {target}")
        if not has_coverage:
            print(f"NOTE: No coverage report found at {coverage}; "
                  "unsearched individuals cannot be included.")
        print("IMPORTANT: These are unverified identity leads, not evidence of collaboration.")
        return
    if args.gedcom is None or not args.gedcom.is_file():
        parser.error(f"GEDCOM file not found: {args.gedcom}")
    try:
        run(args)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
