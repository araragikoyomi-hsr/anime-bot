"""MAL / AniList lookups (through the free Tenrai API) and franchise planning.

Given one MAL entry, walk its Prequel/Sequel links to find every season and
movie in the franchise, then turn that into a download plan.
"""
from __future__ import annotations

import asyncio
import difflib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

TENRAI = "https://api.tenrai.org/v1"
ANILIST = "https://graphql.anilist.co"

MAL_RE = re.compile(r"myanimelist\.net/anime/(\d+)", re.I)
ANILIST_RE = re.compile(r"anilist\.co/anime/(\d+)", re.I)

MAX_ENTRIES = 40          # safety cap for huge franchises
MAX_SELECTABLE = 25       # Discord dropdown limit
MAX_EXTRA_FETCH = 20      # side-story / "other" candidates looked up per franchise
MAX_TERMS = 3             # search terms tried per entry (each wrong-show probe costs a download)

SPECIAL_TYPES = {"OVA", "Special"}          # filed under Specials (season 0)
# MAL relations that can lead to OVAs/specials but aren't Prequel/Sequel links
EXTRA_RELATIONS = ("Side story", "Other")


class MalError(Exception):
    """A lookup failed in a way worth showing to the user."""


@dataclass
class Entry:
    mal_id: int
    title: str                      # MAL default title (usually romaji)
    title_en: Optional[str]
    type: Optional[str]             # TV, Movie, OVA, ONA, Special, ...
    episodes: Optional[int]
    year: Optional[int]
    aired_from: str                 # ISO date, "" if unknown (used for ordering)
    status: str                     # "Finished Airing", "Currently Airing", ...
    image: Optional[str] = None
    relations: list = field(default_factory=list)   # [(relation, mal_id)] Prequel/Sequel only
    titles: list = field(default_factory=list)      # every known title / synonym
    extras: list = field(default_factory=list)      # [(relation, mal_id)] side stories / other
    parents: list = field(default_factory=list)     # [mal_id] "Parent story" links
    parent_id: Optional[int] = None                 # backbone entry this OVA/special belongs to

    @property
    def display(self) -> str:
        return self.title_en or self.title


def parse_entry(d: dict) -> Entry:
    aired_from = ((d.get("aired") or {}).get("from")) or ""
    year = None
    if aired_from[:4].isdigit():
        year = int(aired_from[:4])
    elif d.get("year"):
        year = int(d["year"])

    rels, extras, parents = [], [], []
    for rel in d.get("relations") or []:
        name = rel.get("relation")
        for e in rel.get("entry") or []:
            if e.get("type") != "anime":
                continue
            rid = int(e["mal_id"])
            if name in ("Prequel", "Sequel"):
                rels.append((name, rid))
            elif name in EXTRA_RELATIONS:
                extras.append((name, rid))
            elif name == "Parent story":
                parents.append(rid)

    image = (((d.get("images") or {}).get("jpg")) or {}).get("image_url")
    return Entry(
        mal_id=int(d["mal_id"]),
        title=d.get("title") or "Unknown",
        title_en=d.get("title_english") or None,
        type=d.get("type"),
        episodes=d.get("episodes") or None,
        year=year,
        aired_from=aired_from,
        status=d.get("status") or "",
        image=image,
        relations=rels,
        titles=[t["title"] for t in (d.get("titles") or []) if t.get("title")],
        extras=extras,
        parents=parents,
    )


class Tenrai:
    """Tiny rate-limited Tenrai client (their limit is ~3 requests/second)."""

    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[int, Entry] = {}

    async def _get(self, path: str, params: Optional[dict] = None):
        for attempt in range(4):
            async with self._lock:
                wait = 0.45 - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            try:
                async with self.s.get(
                    f"{TENRAI}{path}", params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status == 200:
                        return await r.json()
                    if r.status == 404:
                        return None
                    if r.status not in (429, 500, 502, 503, 504):
                        raise MalError(f"Tenrai returned HTTP {r.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(2 * (attempt + 1))
        raise MalError("Tenrai (MAL lookup) isn't responding right now - try again in a minute.")

    async def get_entry(self, mal_id: int) -> Optional[Entry]:
        if mal_id in self._cache:
            return self._cache[mal_id]
        j = await self._get(f"/anime/{mal_id}/full")
        if not j or "data" not in j:
            return None
        e = parse_entry(j["data"])
        self._cache[mal_id] = e
        return e

    async def search(self, text: str, limit: int = 8) -> list[Entry]:
        j = await self._get("/anime", {"q": text, "limit": limit})
        return [parse_entry(d) for d in (j or {}).get("data", [])]

    async def anilist_to_mal(self, anilist_id: int) -> Optional[int]:
        q = "query($id:Int){Media(id:$id,type:ANIME){idMal}}"
        try:
            async with self.s.post(
                ANILIST, json={"query": q, "variables": {"id": anilist_id}},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                j = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise MalError("AniList isn't responding right now.")
        return ((j.get("data") or {}).get("Media") or {}).get("idMal")

    async def resolve(self, text: str):
        """Return ("id", mal_id) for links, or ("search", [Entry, ...]) for titles."""
        text = text.strip()
        m = MAL_RE.search(text)
        if m:
            return "id", int(m.group(1))
        m = ANILIST_RE.search(text)
        if m:
            mal_id = await self.anilist_to_mal(int(m.group(1)))
            if not mal_id:
                raise MalError("That AniList entry has no MAL ID, so I can't map it. Try the MAL link.")
            return "id", mal_id
        if text.startswith("http"):
            raise MalError("I only understand MyAnimeList and AniList links (or just type a title).")
        return "search", await self.search(text)

    async def franchise(self, start_id: int, progress=None):
        """Every entry connected to start_id, oldest first.

        The backbone is the Prequel/Sequel chain. OVAs and specials are usually attached by
        other relations (Side story / Other / Parent story), so those are looked up too and
        kept only if they turn out to be OVA/Special/TV Special entries.

        progress: optional async callback(found_count, stage_text) for a live loading message.
        """
        async def tick(text: str) -> None:
            if progress:
                await progress(len(seen), text)

        seen: dict[int, Entry] = {}
        todo = [start_id]
        candidates: list = []          # (relation, mal_id, found_from)
        truncated = False
        while todo:
            mid = todo.pop(0)
            if mid in seen:
                continue
            if len(seen) >= MAX_ENTRIES:
                truncated = True
                break
            e = await self.get_entry(mid)
            if not e:
                if mid == start_id:
                    raise MalError("MAL doesn't have an anime with that ID.")
                continue
            seen[mid] = e
            await tick(e.display)
            todo.extend(rid for _, rid in e.relations if rid not in seen)
            if mid == start_id and e.type in (SPECIAL_TYPES | {"TV Special"}):
                # started from an OVA/special: walk up to the series it belongs to
                todo.extend(pid for pid in e.parents if pid not in seen)
            candidates.extend((rel, rid, mid) for rel, rid in e.extras if rid not in seen)

        fetched = 0
        for rel, rid, found_from in sorted(candidates, key=lambda c: EXTRA_RELATIONS.index(c[0])):
            if rid in seen:
                continue
            if fetched >= MAX_EXTRA_FETCH:
                break
            fetched += 1
            if fetched == 1:
                await tick("looking for OVAs and specials")
            e = await self.get_entry(rid)
            if e and (e.type in SPECIAL_TYPES or e.type == "TV Special"):
                if e.parent_id is None:
                    e.parent_id = found_from
                seen[rid] = e
                await tick(e.display)

        for e in seen.values():        # OVAs reached through the sequel chain: use their declared parent
            if e.type in SPECIAL_TYPES and e.parent_id is None:
                e.parent_id = next((pid for pid in e.parents if pid in seen), None)

        entries = sorted(seen.values(), key=lambda e: (e.aired_from or "9999", e.mal_id))
        return entries, truncated


# ------------------------------------------------------------ title helpers
_NUM_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
              "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "vi": 6, "vii": 7, "viii": 8, "ix": 9}
_FILLER = {"season", "part", "cour", "the"}
_LANG_TAG = re.compile(r"\((?:sub|dub)\)", re.I)
_TYPE_SYNONYMS = {"oad": "ova", "oav": "ova", "specials": "special", "sp": "special"}
_TYPE_WORDS = {"ova", "special"}


def _tokens(title: str) -> list:
    t = unicodedata.normalize("NFKC", title).casefold()
    t = _LANG_TAG.sub(" ", t)
    return "".join(ch if ch.isalnum() else " " for ch in t).split()


def _marker(tok: str):
    """Season-ish tokens (2, 2nd, second, II, a lone symbol like Omega) -> comparable value."""
    if tok.isdigit():
        return int(tok)
    m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", tok)
    if m:
        return int(m.group(1))
    if tok in _NUM_WORDS:
        return _NUM_WORDS[tok]
    if tok in _ROMAN:
        return _ROMAN[tok]
    if len(tok) == 1 and ord(tok) > 127:
        return tok
    return None


def _split_title(title: str):
    """-> (core words, set of season markers)"""
    core, markers = [], set()
    for tok in _tokens(title):
        tok = _TYPE_SYNONYMS.get(tok, tok)      # "OAD" / "Specials" / "SP" compare equal to "OVA" / "Special"
        mk = _marker(tok)
        if mk is not None:
            markers.add(mk)
        elif tok not in _FILLER:
            core.append(tok)
    return core, frozenset(markers)


def base_key(title: str) -> str:
    """'Fate/Zero 2nd Season' and 'Fate/Zero' share a key; 'Fate/stay night' does not."""
    return " ".join(_split_title(title)[0])


def titles_match(source_title: str, expected: list) -> bool:
    """Does the title the download source returned look like the MAL entry we asked for?
    Season markers must agree exactly (so season 1 can't pass for 'II' or an 'Omega' sequel)."""
    s_core, s_marks = _split_title(source_title)
    s_text = " ".join(s_core)
    for ex in expected:
        e_core, e_marks = _split_title(ex)
        if not e_core or e_marks != s_marks:
            continue
        if difflib.SequenceMatcher(None, s_text, " ".join(e_core)).ratio() >= 0.85:
            return True
    return False


def expected_titles(e: Entry) -> list:
    out = []
    for t in [e.title_en, e.title, *e.titles]:
        if t and t not in out:
            out.append(t)
    return out


# --------------------------------------------------------------------- plan
@dataclass
class PlanItem:
    entry: Entry
    kind: str          # "season" | "movie" | "special" | "skip"
    season: int = 0
    series: str = ""   # folder (series) this item belongs to
    year: Optional[int] = None
    ep_offset: int = 0  # specials: S00 numbers start after this (episodes of earlier specials)


@dataclass
class Plan:
    items: list
    focus_id: int
    truncated: bool = False
    mode: str = "split"        # "split" = one folder per title group, "merge" = one series

    @property
    def selectable(self) -> list:
        # seasons/movies first so the 25-option dropdown cuts off extras, not the main show
        main = [p for p in self.items if p.kind not in ("skip", "special")]
        extra = [p for p in self.items if p.kind == "special"]
        return (main + extra)[:MAX_SELECTABLE]

    @property
    def title(self) -> str:
        focus = next((p for p in self.items if p.entry.mal_id == self.focus_id), None)
        return (focus.series if focus and focus.series else None) or (self.items[0].series if self.items else "Unknown")


def _special_home(e: Entry, items: list, groups: list, idx: int):
    """(series, year) an OVA/special should be filed under."""
    # 1. the entry it was found under (side story of season 2 -> season 2's series)
    if e.parent_id:
        for p in items:
            if p.entry.mal_id == e.parent_id and p.kind == "season":
                return p.series, p.year
    # 2. title prefix: "Fate/Zero OVA" belongs to the group whose key is "fate zero"
    best = None
    for t in (e.title_en, e.title):
        words = [w for w in _split_title(t or "")[0] if w not in _TYPE_WORDS]
        text = " ".join(words) + " "
        for g in groups:
            for key in g["keys"]:
                if text.startswith(key + " ") and (best is None or len(key) > len(best[0])):
                    best = (key, g)
    if best:
        return best[1]["first"].display, best[1]["first"].year
    # 3. nearest season by air date: the one before, else the first
    before = [p for p in items[:idx] if p.kind == "season"]
    if before:
        return before[-1].series, before[-1].year
    first = next(p for p in items if p.kind == "season")
    return first.series, first.year


def build_plan(entries: list, focus_id: int, truncated: bool = False, mode: str = "split") -> Plan:
    """TV/ONA entries become seasons, movies stay movies, OVAs/specials go to the series' Specials.

    split: entries whose titles differ beyond a season marker (Fate/Zero vs Fate/stay night,
           Nisemonogatari vs Bakemonogatari) become separate series, numbered from season 1.
    merge: everything is one series, seasons numbered in air-date order.
    A franchise made only of OVAs/specials has no series to hang them on, so they become
    seasons of one series instead.
    """
    items: list[PlanItem] = []
    groups: list[dict] = []
    specials: list[int] = []            # indexes into items, in air order
    for e in entries:
        if e.status == "Not yet aired" or not e.type:
            items.append(PlanItem(e, "skip"))
        elif e.type in SPECIAL_TYPES or (e.type == "TV Special" and e.parent_id):
            specials.append(len(items))
            items.append(PlanItem(e, "special", 0))
        elif e.type == "TV Special":
            items.append(PlanItem(e, "season", 1, e.display, e.year))
        elif e.type in ("TV", "ONA"):
            keys = {k for k in (base_key(e.title_en or ""), base_key(e.title)) if k}
            if mode == "merge":
                g = groups[0] if groups else None
            else:
                g = next((g for g in groups if g["keys"] & keys), None) if keys else None
            if g is None:
                g = {"keys": set(), "first": e, "n": 0}
                groups.append(g)
            g["keys"] |= keys
            g["n"] += 1
            items.append(PlanItem(e, "season", g["n"], g["first"].display, g["first"].year))
        elif e.type == "Movie":
            items.append(PlanItem(e, "movie", 0, e.display, e.year))
        else:
            items.append(PlanItem(e, "skip"))        # Music, PV, CM, ...

    if specials and not any(p.kind == "season" for p in items):
        first = items[specials[0]].entry
        for n, idx in enumerate(specials, 1):
            it = items[idx]
            it.kind, it.season, it.series, it.year = "season", n, first.display, first.year
        specials = []

    counters: dict = {}
    for idx in specials:
        it = items[idx]
        it.series, it.year = _special_home(it.entry, items, groups, idx)
        key = it.series.casefold()
        it.ep_offset = counters.get(key, 0)
        counters[key] = it.ep_offset + (it.entry.episodes or 1)
    return Plan(items=items, focus_id=focus_id, truncated=truncated, mode=mode)
