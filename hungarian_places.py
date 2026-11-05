"""Local Hungarian place-name resolver.

Small reviewed fallbacks work out of the box; a broader GeoNames index may be
generated locally and loaded explicitly. A shared name is NOT sufficient to
join two different settlements: ambiguous names stay unresolved.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


def key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    decomposed = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", decomposed).split())


COUNTRY_MARKERS = {
    "serbia": "RS", "szerbia": "RS", "vojvodina": "RS",
    "croatia": "HR", "horvatorszag": "HR", "croatia slavonia": "HR",
    "romania": "RO", "romania": "RO", "romania": "RO",
    "slovakia": "SK", "szlovakia": "SK",
    "ukraine": "UA", "ukrajna": "UA",
    "slovenia": "SI", "szlovenia": "SI",
    "austria": "AT", "ausztria": "AT",
    "hungary": "HU", "magyarorszag": "HU",
}


def country_hint(full_place: str) -> str | None:
    """Only use context as a tie-breaker; old borders may be misleading."""
    parts = [key(part) for part in full_place.split(",")[1:]]
    for part in reversed(parts):
        if part in COUNTRY_MARKERS:
            return COUNTRY_MARKERS[part]
        for marker, country in COUNTRY_MARKERS.items():
            if marker in part.split(" "):
                return country
    return None


@dataclass(frozen=True)
class Entry:
    identifier: str
    country: str
    hungarian: str
    names: tuple[str, ...]


# IDs are distinct, not country/name combinations. Names are explicitly
# verified against historical/geographic references. Bajmok and Mezőkövesd
# need no translation; normal city extraction and accent folding handle them.
REVIEWED = (
    Entry("reviewed:subotica", "RS", "Szabadka", ("Subotica", "Szabadka")),
    Entry("reviewed:odzaci", "RS", "Hódság", ("Odžaci", "Odzaci", "Hódság")),
    Entry("reviewed:peterr", "RS", "Péterréve", ("Bačko Petrovo Selo", "Péterréve")),
    Entry("reviewed:cakovci", "HR", "Csák", ("Čakovci", "Cakovci", "Csák")),
)


class PlaceResolver:
    """Canonicalize only names with a unique verified geographic identity."""

    def __init__(self, index_file: Path | None = None):
        self.reviewed: dict[str, list[Entry]] = defaultdict(list)
        self.indexed: dict[str, list[Entry]] = defaultdict(list)
        for entry in REVIEWED:
            self._add(self.reviewed, entry)
        if index_file is not None:
            if not index_file.is_file():
                raise ValueError(f"Hungarian place index not found: {index_file}")
            with index_file.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                required = {"geonameid", "country", "hungarian", "native", "aliases"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise ValueError(f"Invalid Hungarian place index columns: {index_file}")
                for row in reader:
                    if not row["geonameid"] or not row["hungarian"] or not row["country"]:
                        continue
                    aliases = [row["native"], row["hungarian"]]
                    aliases.extend(row["aliases"].split("|"))
                    entry = Entry(
                        f"geonames:{row['geonameid']}",
                        row["country"].upper(), row["hungarian"],
                        tuple(dict.fromkeys(alias.strip() for alias in aliases if alias.strip())),
                    )
                    self._add(self.indexed, entry)

    @staticmethod
    def _add(index: dict[str, list[Entry]], entry: Entry) -> None:
        for name in entry.names:
            name_key = key(name)
            if name_key and all(current.identifier != entry.identifier
                                for current in index[name_key]):
                index[name_key].append(entry)

    def find(self, full_place: str) -> Entry | None:
        locality = full_place.split(",", 1)[0].strip()
        lookup = key(locality)
        if not lookup:
            return None
        # Reviewed pairs take precedence over generated entries for the same
        # spellings. Other names are matched only when a single town resolves.
        options = self.reviewed.get(lookup) or self.indexed.get(lookup) or []
        if len(options) == 1:
            return options[0]
        country = country_hint(full_place)
        if country:
            matching = [entry for entry in options if entry.country == country]
            if len(matching) == 1:
                return matching[0]
        return None

    def equivalent(self, first: str, second: str) -> bool:
        name_a = key(first.split(",", 1)[0])
        name_b = key(second.split(",", 1)[0])
        if not name_a or not name_b:
            return False
        if name_a == name_b:
            return True
        a, b = self.find(first), self.find(second)
        return a is not None and b is not None and a.identifier == b.identifier

    def preferred_hungarian(self, full_place: str) -> str:
        place = full_place.split(",", 1)[0].strip()
        entry = self.find(full_place)
        return entry.hungarian if entry else place
