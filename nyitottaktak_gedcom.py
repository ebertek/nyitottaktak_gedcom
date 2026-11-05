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

from review_report import read_csv, review_files, write_review_report
from research_shortlist import shortlist_files, write_shortlist_report
from score_audit import audit_candidate_file, write_audit_report
from consistency_audit import write_consistency_reports
from collision_analysis import summarize_selected_p5_conflicts, write_collision_reports
from birthplace_audit import ASSESSMENTS, write_birthplace_reports
from quality_report import write_quality_report
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


def candidate_names(person: Person, birth_only: bool = False) -> list[str]:
    # GEDCOM's birth NAME is already the maiden name for women: use it directly.
    # Prefer birth/primary names; search explicitly recorded married/alias names
    # as alternatives, without guessing a surname from the spouse.
    # Filter at the source, so unnamed people are skipped in preview AND execute.
    sorted_names = sorted(enumerate(person.names),
                          key=lambda n: (n[1][1] not in ("birth", ""), n[1][1] == "married"))
    names: list[str] = []
    for index, (raw, _type) in sorted_names:
        if birth_only and _type not in ("", "birth", "maiden"):
            continue
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


def birth_name_parts(person: Person) -> tuple[str, str] | None:
    """Surname and given names from the first documented birth/primary name.

    Never borrow the surname or given names from a married NAME to manufacture
    another legal identity. Prefer structured GEDCOM fields when available.
    """
    indexed = sorted(
        enumerate(person.names),
        key=lambda item: (
            item[1][1] not in ("birth", "maiden"),
            item[1][1] not in ("birth", "maiden", ""),
            item[0],
        ),
    )
    for index, (raw, name_type) in indexed:
        if name_type not in ("birth", "maiden", ""):
            continue
        parts = person.structured_names.get(index, {})
        match = re.search(r"/([^/]+)/", raw)
        surname = (parts.get("SURN") or (match.group(1) if match else "")).strip(" /")
        given = (parts.get("GIVN") or re.sub(r"/[^/]+/", " ", raw)).strip()
        if (not norm(surname) or norm(surname) in MISSING_SURNAMES or
                not norm(given)):
            continue
        return surname, given
    return None


def hungarian_ne(name: str) -> str:
    """Append Hungarian married-name -né, lengthening final short a/e.

    For example, Béla -> Béláné and Imre -> Imréné. Preserve the rest of
    the name, including historical spellings, without guessing legal use.
    """
    name = name.strip()
    if name.endswith("a"):
        name = name[:-1] + "á"
    elif name.endswith("e"):
        name = name[:-1] + "é"
    elif name.endswith("A"):
        name = name[:-1] + "Á"
    elif name.endswith("E"):
        name = name[:-1] + "É"
    return f"{name}né"


def inferred_married_names(
        person: Person, people: dict[str, Person], families: dict[str, Family]) -> list[str]:
    """Generate unverified Hungarian forms only for explicit recorded marriages.

    This is a search heuristic. A marriage record does not establish which
    name form either spouse actually used, and no generated form is added to
    the GEDCOM or used as a mother's birth name.
    """
    original = birth_name_parts(person)
    if not original:
        return []
    birth_surname, given = original
    documented = {norm(name) for name in candidate_names(person)}
    variants: list[str] = []
    seen = set(documented)

    for family_id in person.spouse_families:
        family = families.get(family_id)
        if (not family or family.wife != person.xref or
                not family.husband or not family.marriage_recorded):
            continue
        husband = people.get(family.husband)
        husband_name = birth_name_parts(husband) if husband else None
        if not husband_name:
            continue
        spouse_surname, spouse_given = husband_name
        if norm(spouse_surname) in MISSING_SURNAMES:
            continue
        first_given = given.split()[0]
        spouse_full_ne = f"{spouse_surname} {hungarian_ne(spouse_given)}"
        spouse_surname_ne = hungarian_ne(spouse_surname)
        forms = [
            spouse_full_ne,
            f"{spouse_surname_ne} {birth_surname} {given}",
            f"{spouse_surname} {given}",
            f"{spouse_full_ne} {birth_surname} {given}",
        ]
        # Archive search has a strict 25-character limit. A shorter form
        # using the first given name can be searched without silently
        # truncating a surname or a -né marker.
        if first_given != given:
            forms.extend([
                f"{spouse_surname_ne} {birth_surname} {first_given}",
                f"{spouse_surname} {first_given}",
                f"{spouse_full_ne} {birth_surname} {first_given}",
            ])
        for form in forms:
            if norm(form) not in seen:
                seen.add(norm(form))
                variants.append(form)
    return variants


def mother_names(person: Person, people: dict[str, Person], families: dict[str, Family],
                 birth_only: bool = False) -> list[str]:
    mothers: list[str] = []
    for family_id in person.childhood_families:
        mother_id = families.get(family_id, Family()).wife
        mother = people.get(mother_id)
        if mother:
            for name in candidate_names(mother, birth_only=birth_only):
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
    config = {
        "exhaustive": args.exhaustive,
        "year_tolerance": args.year_tolerance,
        "api_year_ceiling": args.api_year_ceiling,
        "min_score": args.min_score,
    }
    # Keep existing checkpoints valid for the unchanged default behavior.
    # Turning on inferred names changes query coverage and needs a separate
    # checkpoint rather than silently skipping previously completed people.
    if getattr(args, "include_inferred_married_names", False):
        config["inferred_married_names"] = True
    return config


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
    shortlist_path = (getattr(args, "shortlist_output", None) or
                      args.output.with_suffix(".shortlist.csv"))
    review_path = getattr(args, "review_output", None) or args.output.with_suffix(".review.csv")
    consistency_path = (getattr(args, "consistency_output", None) or
                        args.output.with_suffix(".consistency.csv"))
    shared_path = (getattr(args, "shared_records_output", None) or
                   args.output.with_suffix(".shared-records.csv"))
    collision_summary_path = (getattr(args, "collision_summary_output", None) or
                              args.output.with_suffix(".collision-summary.csv"))
    collision_triage_path = (getattr(args, "collision_triage_output", None) or
                             args.output.with_suffix(".collision-triage.csv"))
    place_detail_path = (getattr(args, "place_conflicts_output", None) or
                         args.output.with_suffix(".place-conflicts.csv"))
    place_summary_path = (getattr(args, "place_summary_output", None) or
                          args.output.with_suffix(".place-summary.csv"))
    protected_paths = (
        args.output, coverage_path, review_path,
        getattr(args, "checkpoint", None) or args.output.with_suffix(".checkpoint.json"),
        shortlist_path, consistency_path, shared_path,
        collision_summary_path, collision_triage_path,
        place_detail_path, place_summary_path,
    )
    if len({path.resolve() for path in protected_paths}) != len(protected_paths):
        raise ValueError("Report outputs must not overwrite candidates, coverage, "
                         "review, shortlist, consistency, shared GUID or checkpoint files")
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
            mom = ", ".join(mother_names(
                person, people, families,
                birth_only=getattr(args, "include_inferred_married_names", False))) or "unknown"
            original_place = first_place(person.birthplace)
            search_place = api_place(person.birthplace)
            place_label = original_place or "?"
            if search_place and search_place != original_place:
                place_label += f" -> search: {search_place}"
            print(f"  {person.xref}: {', '.join(candidate_names(person))} | {person.born or '?'} | {place_label} | mother: {mom}")
            if getattr(args, "include_inferred_married_names", False):
                variants = inferred_married_names(person, people, families)
                if variants:
                    print(f"    INFERRED, NOT DOCUMENTED: {', '.join(variants)}")
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
               "matched_name_origin", "searched_name_origin",
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
        candidate_rows = [
            row for entry in completed.values() for row in entry.get("rows", [])
        ]
        search_coverage = coverage_rows(eligible, completed)
        write_review_report(review_path, candidate_rows, search_coverage)
        write_shortlist_report(
            shortlist_path,
            candidate_rows, search_coverage,
            include_name_only=getattr(args, "shortlist_include_name_only", False),
        )
        write_consistency_reports(candidate_rows, consistency_path, shared_path)
        write_collision_reports(
            candidate_rows, collision_summary_path, collision_triage_path
        )
        write_birthplace_reports(
            candidate_rows, PLACE_RESOLVER, normalized_place_input,
            place_detail_path, place_summary_path,
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
        documented_names = candidate_names(person)
        inferred_names = (
            inferred_married_names(person, people, families)
            if getattr(args, "include_inferred_married_names", False) else []
        )
        names = documented_names + inferred_names
        inferred_name_keys = {norm(name) for name in inferred_names}
        mothers = mother_names(
            person, people, families,
            birth_only=getattr(args, "include_inferred_married_names", False))
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
            if norm(name) in inferred_name_keys and not clean_api_text(name, "nev"):
                # Do not silently truncate a hypothetical long married name
                # to an ambiguous two-word prefix. Still compare the full
                # form when scoring records discovered via other names.
                continue
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
                "matched_name_origin": (
                    "inferred_married" if norm(matched_name) in inferred_name_keys else "gedcom"
                ),
                "searched_name_origin": (
                    "inferred_married" if norm(searched_name) in inferred_name_keys else "gedcom"
                ),
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


def recalculate_saved_candidate(row: dict) -> tuple[int, str]:
    """Reproduce a stored candidate using its chosen GEDCOM match name.

    The CSV saves the winning name used by the original scorer, GEDCOM identity
    values and archive identity attributes; no GEDCOM or network is needed.
    """
    person = Person(
        xref=row["gedcom_id"], born=row["gedcom_birth"],
        birthplace=row["gedcom_birth_place"],
    )
    archive = {
        "nev": row["archive_name"], "szul_ido": row["archive_birth"],
        "szul_hely": row["archive_birth_place"],
        "anyja_neve": row["archive_mother"],
    }
    mothers = [name.strip() for name in row["gedcom_mother"].split(";")
               if name.strip()]
    points, evidence, _ = score(person, archive, [row["gedcom_name"]], mothers)
    return points, evidence


def warn_about_saved_scoring(source: Path) -> None:
    """Explain scoring drift before offline reports interpret stale evidence."""
    audit = audit_candidate_file(source, recalculate_saved_candidate)
    if audit.has_drift:
        print(
            f"WARNING: Stored evidence differs from current scoring: "
            f"{audit.score_changed} score changes, "
            f"{audit.evidence_changed} evidence changes, "
            f"{audit.classification_changed} classification changes "
            f"across {audit.checked} checked candidates."
        )
        print(
            "WARNING: Review priorities may be outdated. Run "
            f"--rescore-only --review-input {source} and use the resulting "
            "*.rescored.csv for evidence-based reports."
        )
    if audit.unverifiable:
        print(
            f"WARNING: Could not verify {audit.unverifiable} candidate rows "
            "(missing/invalid saved values). Use --audit-only for details."
        )


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
        score_value, evidence = recalculate_saved_candidate(row)
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


DEFAULT_OUTPUT = Path("results/nyitottaktak_matches.csv")
LEGACY_DEFAULT_OUTPUT = Path("nyitottaktak_matches.csv")


def choose_default_output(args: argparse.Namespace, argv: list[str]) -> None:
    """Retain access to legacy local reports without moving user files.

    The new directory is used for new scans. Offline modes can still read the
    prior root-level default, if no results/ counterpart has been generated.
    Explicit --output paths always win, even when they do not exist yet.
    """
    explicit_output = any(
        arg == "--output" or arg.startswith("--output=") for arg in argv
    )
    if explicit_output or args.output != DEFAULT_OUTPUT:
        return
    if args.review_input is not None and (
            args.review_only or args.rescore_only or args.quality_only
            or getattr(args, "shortlist_only", False)
            or getattr(args, "audit_only", False)
            or getattr(args, "consistency_only", False)
            or getattr(args, "place_conflicts_only", False)):
        return

    offline_report = (args.review_only or args.rescore_only or
                      args.quality_only or getattr(args, "shortlist_only", False)
                      or getattr(args, "audit_only", False)
                      or getattr(args, "consistency_only", False)
                      or getattr(args, "place_conflicts_only", False))
    coverage = args.coverage_only
    legacy_exists = (LEGACY_DEFAULT_OUTPUT.exists() if offline_report else
                     LEGACY_DEFAULT_OUTPUT.with_suffix(".checkpoint.json").exists())
    modern_exists = (DEFAULT_OUTPUT.exists() if offline_report else
                     DEFAULT_OUTPUT.with_suffix(".checkpoint.json").exists())
    if (offline_report or coverage) and legacy_exists and not modern_exists:
        args.output = LEGACY_DEFAULT_OUTPUT
        print("Using legacy root-level results for this offline operation; "
              "move them into results/ together when ready.")
    elif args.execute and not (offline_report or coverage or args.check_updates):
        legacy_checkpoint = LEGACY_DEFAULT_OUTPUT.with_suffix(".checkpoint.json")
        modern_checkpoint = DEFAULT_OUTPUT.with_suffix(".checkpoint.json")
        if legacy_checkpoint.exists() and not modern_checkpoint.exists():
            print("NOTE: Existing root-level checkpoint found. New scans default "
                  "to results/. To resume the old scan, supply "
                  "--output nyitottaktak_matches.csv or move its results "
                  "and checkpoint into results/ first.")


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
    parser.add_argument("--include-inferred-married-names", action="store_true",
                        help="Try speculative Hungarian married-name forms from explicit spouse "
                             "and MARR records; requires a separate checkpoint from regular searches")
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
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Candidate CSV (default: results/nyitottaktak_matches.csv; "
                             "explicit paths are used as given)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".nyitottaktak_cache"))
    parser.add_argument("--refresh", action="store_true", help="Refresh cached API responses")
    parser.add_argument("--place-index", type=Path, default=None,
                        help="Custom local Hungarian place index CSV (overrides automatic default)")
    parser.add_argument("--no-auto-place-index", action="store_true",
                        help="Skip automatic GeoNames download on first executed scan; "
                             "use built-in aliases if no local index exists")
    parser.add_argument("--rescore-only", action="store_true",
                        help="Recalculate existing candidate scores offline into a separate CSV; no GEDCOM or API calls")
    parser.add_argument("--audit-only", action="store_true",
                        help="Compare saved scores/evidence with current scoring offline, "
                             "without changing source files")
    parser.add_argument("--audit-output", type=Path, default=None,
                        help="Private score-drift detail CSV (default: input CSV with .audit.csv suffix)")
    parser.add_argument("--consistency-only", action="store_true",
                        help="Audit historical chronology and shared archive GUIDs offline")
    parser.add_argument("--consistency-output", type=Path, default=None,
                        help="Private candidate consistency audit (default: *.consistency.csv)")
    parser.add_argument("--shared-records-output", type=Path, default=None,
                        help="Private shared GUID audit (default: *.shared-records.csv)")
    parser.add_argument("--collision-summary-output", type=Path, default=None,
                        help="Source/evidence collision aggregates (default: *.collision-summary.csv)")
    parser.add_argument("--collision-triage-output", type=Path, default=None,
                        help="Private shared-GUID evidence profiles (default: *.collision-triage.csv)")
    parser.add_argument("--place-conflicts-only", action="store_true",
                        help="Audit selected P5 birthplace conflicts against local place references offline")
    parser.add_argument("--place-conflicts-output", type=Path, default=None,
                        help="Private P5 birthplace pair review (default: *.place-conflicts.csv)")
    parser.add_argument("--place-summary-output", type=Path, default=None,
                        help="Aggregate birthplace reconciliation counts (default: *.place-summary.csv)")
    parser.add_argument("--quality-only", action="store_true",
                        help="Summarize archive field completeness and identity evidence "
                             "per source from saved candidates; offline and GEDCOM-free")
    parser.add_argument("--quality-output", type=Path, default=None,
                        help="Aggregate quality CSV (default: candidate CSV with .quality.csv suffix)")
    parser.add_argument("--review-only", action="store_true",
                        help="Build grouped candidate review and research shortlist from local CSVs, offline")
    parser.add_argument("--shortlist-only", action="store_true",
                        help="Build evidence-prioritized research shortlist from saved candidates; "
                             "excludes name-only leads by default; offline and GEDCOM-free")
    parser.add_argument("--shortlist-output", type=Path, default=None,
                        help="Research shortlist CSV (default: candidate CSV with .shortlist.csv suffix)")
    parser.add_argument("--shortlist-include-name-only", action="store_true",
                        help="Include deferred name-only leads in research shortlist")
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
    choose_default_output(args, sys.argv[1:])
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
                   args.review_only or args.coverage_only or args.quality_only
                   or args.shortlist_only or args.audit_only or args.consistency_only
                   or args.place_conflicts_only)):
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
    if args.place_conflicts_only:
        source = args.review_input or args.output
        detail = args.place_conflicts_output or source.with_suffix(".place-conflicts.csv")
        summary = args.place_summary_output or source.with_suffix(".place-summary.csv")
        protected = (
            source, source.with_suffix(".coverage.csv"),
            source.with_suffix(".checkpoint.json"), source.with_suffix(".review.csv"),
            source.with_suffix(".shortlist.csv"), source.with_suffix(".audit.csv"),
            source.with_suffix(".quality.csv"), source.with_suffix(".consistency.csv"),
            source.with_suffix(".shared-records.csv"),
            source.with_suffix(".collision-summary.csv"),
            source.with_suffix(".collision-triage.csv"), detail, summary,
        )
        if len({p.resolve() for p in protected}) != len(protected):
            parser.error("Birthplace report output would overwrite a source or existing report")
        try:
            warn_about_saved_scoring(source)
            count, statuses = write_birthplace_reports(
                read_csv(source), PLACE_RESOLVER, normalized_place_input,
                detail, summary,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline birthplace review: {count} selected P5 birthplace conflicts "
              f"-> {detail}")
        print("Birthplace assessments: " + ", ".join(
            f"{label}={statuses[label]}" for label in ASSESSMENTS
        ) + f" -> {summary}")
        if place_index is None:
            print("NOTE: No local GeoNames index selected. Only the reviewed "
                  "built-in aliases are available; do not infer different localities.")
        print("NOTE: No candidate scores, GEDCOM data or checkpoints changed.")
        print("NOTE: Detailed birthplace reports contain private identities; do not commit.")
        return
    if args.consistency_only:
        source = args.review_input or args.output
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        consistency_path = args.consistency_output or source.with_suffix(".consistency.csv")
        shared_path = args.shared_records_output or source.with_suffix(".shared-records.csv")
        collision_summary = (
            args.collision_summary_output or source.with_suffix(".collision-summary.csv")
        )
        collision_triage = (
            args.collision_triage_output or source.with_suffix(".collision-triage.csv")
        )
        place_detail = args.place_conflicts_output or source.with_suffix(".place-conflicts.csv")
        place_summary = args.place_summary_output or source.with_suffix(".place-summary.csv")
        protected = (
            source, coverage, source.with_suffix(".review.csv"),
            source.with_suffix(".shortlist.csv"), source.with_suffix(".checkpoint.json"),
            source.with_suffix(".audit.csv"), source.with_suffix(".quality.csv"),
            consistency_path, shared_path, collision_summary, collision_triage,
            place_detail, place_summary,
        )
        if len({p.resolve() for p in protected}) != len(protected):
            parser.error("Consistency/collision output would overwrite a source or existing report")
        try:
            candidates = read_csv(source)
            flagged, shared, categories = write_consistency_reports(
                candidates, consistency_path, shared_path
            )
            group_rows, profiles = write_collision_reports(
                candidates, collision_summary, collision_triage
            )
            place_count, place_statuses = write_birthplace_reports(
                candidates, PLACE_RESOLVER, normalized_place_input,
                place_detail, place_summary,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline consistency audit: {flagged} flagged candidate rows; "
              f"{shared} shared archive GUIDs -> {consistency_path}, {shared_path}")
        print(f"Collision evidence: {group_rows} source/class groups -> {collision_summary}")
        print("Shared-GUID profiles: " + ", ".join(
            f"{label}={profiles[label]}" for label in (
                "name_only_collision", "corroborated_lead_present",
                "other_ambiguous_collision"
            )
        ) + f" -> {collision_triage}")
        print(f"Birthplace reconciliation: {place_count} selected P5 pairs; " +
              ", ".join(
                  f"{label}={place_statuses[label]}" for label in ASSESSMENTS
              ) + f" -> {place_detail}, {place_summary}")
        p5 = summarize_selected_p5_conflicts(candidates)
        print(
            "Selected P5 conflicts (non-exclusive): "
            f"{p5['relatives']} relatives; "
            f"birth_date={p5['birth_date']}, "
            f"mother={p5['mother']}, birthplace={p5['birthplace']}"
        )
        if categories:
            print("Chronology review flags: " + ", ".join(
                f"{key}={categories[key]}" for key in sorted(categories)
            ))
        print("NOTE: Historical age flags are research prompts, not proof of impossible identities.")
        print("NOTE: These reports contain private family identifiers; do not commit or share.")
        return
    if args.audit_only:
        source = args.review_input or args.output
        target = args.audit_output or source.with_suffix(".audit.csv")
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        protected = (
            source, coverage, source.with_suffix(".review.csv"),
            source.with_suffix(".shortlist.csv"),
            source.with_suffix(".checkpoint.json"),
        )
        if target.resolve() in {path.resolve() for path in protected}:
            parser.error("Audit output must not overwrite source reports or checkpoint")
        try:
            audit = audit_candidate_file(source, recalculate_saved_candidate)
            write_audit_report(target, audit)
        except ValueError as exc:
            parser.error(str(exc))
        print(
            f"Offline score audit: {audit.checked} checked; "
            f"{audit.score_changed} score changes; "
            f"{audit.evidence_changed} evidence changes; "
            f"{audit.classification_changed} classification changes; "
            f"{audit.unverifiable} unverifiable -> {target}"
        )
        print("NOTE: Audit details contain private identities; do not commit or share the CSV.")
        print("NOTE: This compares retained saved candidates only; no GEDCOM or archive API requests.")
        return
    if args.quality_only:
        source = args.review_input or args.output
        target = args.quality_output or source.with_suffix(".quality.csv")
        try:
            warn_about_saved_scoring(source)
            aggregates = write_quality_report(source, target)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline quality: {len(aggregates)} aggregate rows -> {target}")
        if aggregates:
            print("Archive source | candidates | distinct archive records | "
                  "birth % | birthplace % | mother % | name-only candidates")
            for item in aggregates:
                print(f"{item['archive_record_type']} | {item['candidate_rows']} | "
                      f"{item['distinct_archive_records']} | "
                      f"{item['archive_birth_pct']} | {item['archive_birth_place_pct']} | "
                      f"{item['archive_mother_pct']} | {item['candidate_name_only']}")
        print("NOTE: Only records retained in the existing candidate CSV are measured; "
              "this is NOT completeness of the full archive or proof of identity.")
        return
    if args.rescore_only:
        source = args.review_input or args.output
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        target = source.with_name(source.stem + ".rescored" + source.suffix)
        review_target = target.with_suffix(".review.csv")
        shortlist_target = args.shortlist_output or target.with_suffix(".shortlist.csv")
        consistency_target = args.consistency_output or target.with_suffix(".consistency.csv")
        shared_target = args.shared_records_output or target.with_suffix(".shared-records.csv")
        collision_summary = (
            args.collision_summary_output or target.with_suffix(".collision-summary.csv")
        )
        collision_triage = (
            args.collision_triage_output or target.with_suffix(".collision-triage.csv")
        )
        protected = (
            source, target, coverage, review_target, shortlist_target,
            consistency_target, shared_target, collision_summary, collision_triage,
            source.with_suffix(".checkpoint.json"),
        )
        if len({p.resolve() for p in protected}) != len(protected):
            parser.error("Rescored output must not overwrite source, reports or checkpoint")
        try:
            count, changed = rescore_csv(source, target, coverage)
            people, _, has_coverage = review_files(target, coverage, review_target)
            shortlist_count, priorities, _ = shortlist_files(
                target, coverage, shortlist_target,
                include_name_only=args.shortlist_include_name_only,
            )
            rescored_rows = read_csv(target)
            flagged, shared, _ = write_consistency_reports(
                rescored_rows, consistency_target, shared_target
            )
            collision_groups, collision_types = write_collision_reports(
                rescored_rows, collision_summary, collision_triage
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline rescoring: {count} candidate rows; {changed} scores changed -> {target}")
        print(f"Identity review: {people} relatives -> {review_target}")
        print(f"Rescored research shortlist: {shortlist_count} relatives -> {shortlist_target}")
        print(f"Rescored consistency: {flagged} flagged rows; "
              f"{shared} shared GUIDs -> {consistency_target}, {shared_target}")
        print(f"Rescored collision evidence: {collision_groups} source/class groups; "
              f"{sum(collision_types.values())} shared GUIDs "
              f"-> {collision_summary}, {collision_triage}")
        if priorities:
            print("Priorities: " + ", ".join(
                f"{key}={priorities[key]}" for key in sorted(priorities)
            ))
        if not has_coverage:
            print("NOTE: Coverage CSV missing; the review omits unsearched people.")
        print("NOTE: This only re-scores existing candidate records; it cannot recover previously discarded matches.")
        return
    if args.shortlist_only:
        source = args.review_input or args.output
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        target = args.shortlist_output or source.with_suffix(".shortlist.csv")
        consistency_target = args.consistency_output or source.with_suffix(".consistency.csv")
        shared_target = args.shared_records_output or source.with_suffix(".shared-records.csv")
        collision_summary = (
            args.collision_summary_output or source.with_suffix(".collision-summary.csv")
        )
        collision_triage = (
            args.collision_triage_output or source.with_suffix(".collision-triage.csv")
        )
        protected = (
            source, coverage, target, consistency_target, shared_target,
            collision_summary, collision_triage,
            source.with_suffix(".review.csv"), source.with_suffix(".checkpoint.json"),
        )
        if len({p.resolve() for p in protected}) != len(protected):
            parser.error("Shortlist/consistency outputs must not overwrite source or reports")
        try:
            warn_about_saved_scoring(source)
            count, priorities, has_coverage = shortlist_files(
                source, coverage, target,
                include_name_only=args.shortlist_include_name_only,
            )
        except ValueError as exc:
            parser.error(str(exc))
        candidate_rows = read_csv(source)
        flagged, shared, _ = write_consistency_reports(
            candidate_rows, consistency_target, shared_target
        )
        collision_groups, collision_types = write_collision_reports(
            candidate_rows, collision_summary, collision_triage
        )
        print(f"Offline research shortlist: {count} relatives -> {target}")
        print(f"Consistency audit: {flagged} flagged rows; {shared} shared GUIDs "
              f"-> {consistency_target}, {shared_target}")
        print(f"Collision evidence: {collision_groups} source/class groups; "
              f"{sum(collision_types.values())} shared GUIDs "
              f"-> {collision_summary}, {collision_triage}")
        print("Priorities: " + ", ".join(
            f"{key}={priorities[key]}" for key in sorted(priorities)
        ) if priorities else "Priorities: no actionable leads")
        if not has_coverage:
            print("NOTE: Coverage CSV absent; search completion is not reported.")
        print("NOTE: These are unverified identity leads, not evidence of collaboration.")
        return
    if args.review_only:
        source = args.review_input or args.output
        coverage = args.review_coverage_input or source.with_suffix(".coverage.csv")
        target = args.review_output or source.with_suffix(".review.csv")
        shortlist_target = args.shortlist_output or source.with_suffix(".shortlist.csv")
        consistency_target = args.consistency_output or source.with_suffix(".consistency.csv")
        shared_target = args.shared_records_output or source.with_suffix(".shared-records.csv")
        collision_summary = (
            args.collision_summary_output or source.with_suffix(".collision-summary.csv")
        )
        collision_triage = (
            args.collision_triage_output or source.with_suffix(".collision-triage.csv")
        )
        protected = (
            source, coverage, target, shortlist_target, consistency_target,
            shared_target, collision_summary, collision_triage,
            source.with_suffix(".checkpoint.json"),
        )
        if len({p.resolve() for p in protected}) != len(protected):
            parser.error("Review outputs must not overwrite source, reports or checkpoint")
        try:
            warn_about_saved_scoring(source)
            people, counts, has_coverage = review_files(source, coverage, target)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline identity review: {people} relatives, "
              f"{sum(counts.values())} grouped rows -> {target}")
        try:
            shortlist_count, priorities, _ = shortlist_files(
                source, coverage, shortlist_target,
                include_name_only=args.shortlist_include_name_only,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Offline research shortlist: {shortlist_count} relatives -> {shortlist_target}")
        candidate_rows = read_csv(source)
        flagged, shared, _ = write_consistency_reports(
            candidate_rows, consistency_target, shared_target
        )
        collision_groups, collision_types = write_collision_reports(
            candidate_rows, collision_summary, collision_triage
        )
        print(f"Consistency audit: {flagged} flagged rows; {shared} shared GUIDs "
              f"-> {consistency_target}, {shared_target}")
        print(f"Collision evidence: {collision_groups} source/class groups; "
              f"{sum(collision_types.values())} shared GUIDs "
              f"-> {collision_summary}, {collision_triage}")
        if priorities:
            print("Priorities: " + ", ".join(
                f"{key}={priorities[key]}" for key in sorted(priorities)
            ))
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
