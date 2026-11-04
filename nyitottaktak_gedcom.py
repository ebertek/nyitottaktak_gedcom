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
import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://nyitottaktak.hu/api/search"
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
    born: str = ""
    birthplace: str = ""
    childhood_families: list[str] = field(default_factory=list)


@dataclass
class Family:
    wife: str = ""
    children: list[str] = field(default_factory=list)


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
                if tag == "NAME" and value:
                    p.names.append((value, ""))
                    name_index = len(p.names) - 1
                elif tag == "FAMC" and value.startswith("@"):
                    p.childhood_families.append(value.strip("@"))
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
                elif section == "NAME" and tag == "TYPE" and name_index >= 0:
                    existing = p.names[name_index][0]
                    p.names[name_index] = (existing, value.lower())
        elif kind == "FAM" and level == 1:
            f = families[owner]
            if tag == "WIFE":
                f.wife = value.strip("@")
            elif tag == "CHIL":
                f.children.append(value.strip("@"))

    # Some GEDCOM exports identify parent families only through FAM/CHIL.
    for fid, f in families.items():
        for child in f.children:
            if child in people and fid not in people[child].childhood_families:
                people[child].childhood_families.append(fid)
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
    sorted_names = sorted(person.names, key=lambda n: (n[1] not in ("birth", ""), n[1] == "married"))
    names: list[str] = []
    for raw, _type in sorted_names:
        if not has_gedcom_surname(raw):
            continue
        candidate = human_name(raw)
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
    """Search by city, not GEDCOM district (e.g. 'Budapest XI.' -> 'Budapest')."""
    return clean_api_text(city_name(value), "szul_hely")


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
    ged_place = norm(city_name(person.birthplace))
    archive_place = norm(city_name(str(archive.get("szul_hely") or "")))
    if ged_place and archive_place:
        if ged_place == archive_place:
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

    def fetch(self, params: dict) -> dict:
        filtered = {k: v for k, v in params.items() if v not in (None, "")}
        query_string = urlencode(sorted(filtered.items()))
        cache_file = self.cache_dir / (hashlib.sha256(query_string.encode()).hexdigest() + ".json")
        if cache_file.exists() and not self.refresh:
            self.cached_responses += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))
        if self.requests >= self.max_requests:
            raise RuntimeError(f"Request budget reached ({self.max_requests}); rerun to continue from cache")
        elapsed = time.monotonic() - self.last_call
        if self.last_call and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        url = f"{API}?{query_string}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "GenealogyArchiveResearch/0.1"})
        try:
            self.requests += 1
            self.last_call = time.monotonic()
            with urlopen(request, timeout=25) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code == 400:
                self.http_400 += 1
            # Read no more than 500 bytes; APIs sometimes explain validation errors.
            detail = exc.read(500).decode("utf-8", errors="replace").strip()
            detail = re.sub(r"\s+", " ", detail)[:240]
            # Especially 403/429: never attempt to bypass access restrictions.
            raise ApiQueryError(exc.code, url, detail) from exc
        except URLError as exc:
            raise RuntimeError(f"Archive connection failed ({exc}) for {url}") from exc
        if not isinstance(result, dict) or "results" not in result:
            raise RuntimeError(f"Unexpected response for {url}: expected object with 'results'")
        self.ok_responses += 1
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
            cap = int(payload.get("max_results") or 250)
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


def run(args: argparse.Namespace) -> None:
    people, families = read_gedcom(args.gedcom)
    eligible: list[Person] = []
    missing_years = 0
    missing_surnames = 0
    for person in people.values():
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
    if args.order == "historical":
        eligible.sort(key=historical_order)
    elif args.order == "oldest":
        eligible.sort(key=lambda p: (birth_year(p.born) if birth_year(p.born) is not None else 9999, p.xref))
    eligible = eligible[:args.max_people]
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
    columns = ["gedcom_id", "gedcom_name", "gedcom_birth", "gedcom_birth_place", "gedcom_mother",
               "archive_guid", "archive_name", "archive_birth", "archive_birth_place", "archive_mother",
               "archive_cover_name", "archive_record_type", "match_score", "review_priority", "evidence",
               "searched_name", "api_search_url", "archive_document_url"]
    rows: list[dict] = []
    errors: list[str] = []
    recovered_400s: list[str] = []
    incomplete_searches: list[str] = []
    people_with_raw_results = 0
    raw_unique_results = 0
    stop_now = False
    for index, person in enumerate(eligible, 1):
        names = candidate_names(person)
        mothers = mother_names(person, people, families)
        year = birth_year(person.born)
        birthplace = city_name(person.birthplace)
        print(f"[{index}/{len(eligible)}] {names[0]} ({year or '?'})", flush=True)
        archive_hits: dict[str, tuple[dict, str, str]] = {}
        missing_birth_date_notice = False

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
                errors.append(f"{person.xref}: No valid search-name variant for {name!r} (2–25 characters)")
                continue
            for step_num, step in enumerate(deduped_steps):
                # Only stop broadening on an actual identity candidate, rather
                # than any unrelated substring result returned by the API.
                if (step_num and not args.exhaustive and
                        any(score(person, hit[0], names, mothers)[0] >= args.min_score
                            for hit in archive_hits.values())):
                    break
                try:
                    results, capped, url, includes_missing_birth_dates = client.search_all(step)
                except ApiQueryError as exc:
                    if exc.status == 400:
                        # Usually a rejected filter combination. Keep going to a
                        # less-specific query and record what the server said.
                        recovered_400s.append(f"{person.xref}: {exc}")
                        print(f"  WARN: rejected filter (HTTP 400); trying broader search. {exc.detail[:120]}", file=sys.stderr)
                        continue
                    errors.append(f"{person.xref}: {exc}")
                    print(f"  ERROR: {exc}", file=sys.stderr)
                    if exc.status in (403, 429):
                        stop_now = True
                        break
                    continue
                except RuntimeError as exc:
                    errors.append(f"{person.xref}: {exc}")
                    print(f"  ERROR: {exc}", file=sys.stderr)
                    if "Request budget reached" in str(exc):
                        stop_now = True
                        break
                    continue
                if capped:
                    warning = f"{person.xref}: search for {name!r} returned too_many; narrow the filters"
                    incomplete_searches.append(warning)
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

        if archive_hits:
            people_with_raw_results += 1
            raw_unique_results += len(archive_hits)
        accepted_for_person = 0
        for result, searched_name, url in archive_hits.values():
            quality, reasons, matched_name = score(person, result, names, mothers)
            if quality < args.min_score:
                continue
            priority = "strong candidate" if quality >= 85 else ("possible candidate" if quality >= 60 else "weak candidate")
            accepted_for_person += 1
            rows.append({
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
        if missing_birth_date_notice:
            print("  NOTE: Archive search includes record types without birth dates; identity checks may be weaker.")
        if stop_now:
            print("Stopping early to respect the site's limits.", file=sys.stderr)
            break

    rows.sort(key=lambda r: (-int(r["match_score"]), r["gedcom_id"], r["archive_guid"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    error_file = args.output.with_suffix(".errors.txt")
    if errors or recovered_400s or incomplete_searches:
        error_file.write_text("\n".join(errors + recovered_400s + incomplete_searches) + "\n", encoding="utf-8")
        print(f"Errors and rejected filters saved to: {error_file}", file=sys.stderr)
    elif error_file.exists():
        error_file.unlink()  # Don't leave stale errors from a previous run.
    print(f"Finished: {client.requests} network requests ({client.ok_responses} successful, "
          f"{client.http_400} HTTP 400, {client.cached_responses} cache hits); "
          f"{raw_unique_results} unique raw records across {people_with_raw_results} people; "
          f"{len(rows)} candidate rows -> {args.output}")
    if stop_now or errors or incomplete_searches:
        print("NOTE: Some searches failed or were not completed; absence of matches is not conclusive.")
    print("IMPORTANT: A match is an identity lead, not evidence that someone was an agent.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gedcom", type=Path, help="Local .ged/.gedcom file")
    parser.add_argument("--execute", action="store_true", help="Actually send API requests (otherwise preview only)")
    parser.add_argument("--exhaustive", action="store_true", help="Also search name-only and unaccented variants")
    parser.add_argument("--include-undated", action="store_true", help="Include GEDCOM people with no birth year")
    parser.add_argument("--since", type=int, default=DEFAULT_FIRST_YEAR)
    parser.add_argument("--until", type=int, default=DEFAULT_LAST_YEAR)
    parser.add_argument("--year-tolerance", type=int, default=2)
    parser.add_argument("--api-year-ceiling", type=int, default=DEFAULT_API_YEAR_CEILING,
                        help="Maximum birth year used in API filters (website permits 1850–1990; default: 1990; 0 resets to 1990)")
    parser.add_argument("--order", choices=("historical", "oldest", "gedcom"), default="historical",
                        help="Search ordering; historical prioritizes adults around 1956 (default)")
    parser.add_argument("--max-people", type=int, default=30)
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between uncached requests")
    parser.add_argument("--min-score", type=int, default=35)
    parser.add_argument("--output", type=Path, default=Path("nyitottaktak_matches.csv"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".nyitottaktak_cache"))
    parser.add_argument("--refresh", action="store_true", help="Refresh cached API responses")
    args = parser.parse_args()
    if not args.gedcom.is_file():
        parser.error(f"GEDCOM file not found: {args.gedcom}")
    if args.delay < 0.5 or args.max_people < 1 or args.max_requests < 1 or args.min_score < 0 or args.year_tolerance < 0:
        parser.error("Use --delay >= 0.5, positive budgets, nonnegative score and tolerance")
    run(args)


if __name__ == "__main__":
    main()
