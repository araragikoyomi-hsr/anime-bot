"""Runs downloads by driving the existing ~/anime-dl script, one entry at a time."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

from mal import Entry, Plan, PlanItem, expected_titles, titles_match

VIDEO_EXT = (".mp4", ".mkv")
SRC_RE = re.compile(r"Source title: (.+)$")


# ------------------------------------------------------------------- config
@dataclass
class Config:
    anime_dl: str
    anime_dir: Path       # Jellyfin anime/TV library
    movies_dir: Path      # Jellyfin movies library
    work_dir: Path        # scratch space, must be on the same filesystem as the libraries
    jellyfin_url: Optional[str] = None
    jellyfin_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            anime_dl=os.path.expanduser(e("ANIME_DL", "~/anime-dl")),
            anime_dir=Path(e("ANIME_DIR", "/srv/media/anime")),
            movies_dir=Path(e("MOVIES_DIR", "/srv/media/movies")),
            work_dir=Path(e("WORK_DIR", "/srv/media/.anime-bot")),
            jellyfin_url=(e("JELLYFIN_URL") or "").rstrip("/") or None,
            jellyfin_key=e("JELLYFIN_API_KEY") or None,
        )


# --------------------------------------------------------------- job state
@dataclass
class ItemResult:
    status: str = "queued"      # queued running done partial failed cancelled
    have: int = 0
    want: Optional[int] = None
    note: str = ""
    tail: list = field(default_factory=list)   # last lines of anime-dl output (for failures)


@dataclass
class Job:
    id: int
    owner: int
    channel_id: int
    plan: Plan
    items: list
    quality: str = "best"
    dub: bool = False
    pick: int = 1
    search: Optional[str] = None
    verify: bool = True
    results: dict = field(default_factory=dict)
    cancelled: bool = False
    proc: Optional[asyncio.subprocess.Process] = None
    status_msg: object = None

    def __post_init__(self):
        for it in self.items:
            self.results.setdefault(it.entry.mal_id, ItemResult())

    @property
    def title(self) -> str:
        names = {it.series for it in self.items}
        return names.pop() if len(names) == 1 else self.plan.title


# ------------------------------------------------------------------- naming
def clean_name(s: str) -> str:
    """Folder/file-safe title that Jellyfin's TMDb matching still understands."""
    s = re.sub(r"[/:]", " ", s)
    s = re.sub(r'[\\*?"<>|]', "", s)
    return re.sub(r"\s+", " ", s).strip()


def search_term(s: str) -> str:
    """Punctuation that tends to break the ani-cli search is replaced with spaces."""
    return re.sub(r"\s+", " ", re.sub(r'[:/"]', " ", s)).strip()


def search_terms(e: Entry, override: Optional[str] = None) -> list:
    if override:
        return [override]
    terms = []
    for t in (e.title_en, e.title):          # English first, then MAL's default title
        if t:
            t = search_term(t)
            if t and t not in terms:
                terms.append(t)
    return terms


def folder_name(name: str, year: Optional[int]) -> str:
    return f"{name} ({year})" if year else name


def resolve_folder(root: Path, series: str, year: Optional[int]):
    """(name, year) of the folder to use. Reuses one that already exists in the library,
    ignoring capitalisation and whether it has a year, so old folders aren't duplicated."""
    name = clean_name(series)
    want = name.casefold()
    best, best_score = (name, year), -1
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            m = re.fullmatch(r"(.*?)(?: \((\d{4})\))?", d.name)
            base, y = m.group(1), (int(m.group(2)) if m.group(2) else None)
            if base.casefold() != want:
                continue
            if year and y and y != year:
                continue                      # same title, different year = different show
            score = 3 if (year and y == year) else (2 if y is None else 1)
            if score > best_score:
                best, best_score = (base, y), score
    return best


def season_target(cfg: Config, item: PlanItem):
    name, year = resolve_folder(cfg.anime_dir, item.series, item.year)
    return cfg.anime_dir / folder_name(name, year) / f"Season {item.season:02d}", name, year


def season_dir(cfg: Config, item: PlanItem) -> Path:
    return season_target(cfg, item)[0]


def movie_target(cfg: Config, item: PlanItem):
    name, year = resolve_folder(cfg.movies_dir, item.entry.display, item.entry.year)
    return cfg.movies_dir / folder_name(name, year), name, year


def count_videos(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(1 for f in d.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT)


def present_episodes(d: Path) -> set:
    out = set()
    if d.is_dir():
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXT:
                m = re.search(r"S\d+E(\d+)", f.name)
                if m:
                    out.add(int(m.group(1)))
    return out


def library_state(cfg: Config, item: PlanItem):
    """(episodes_in_library, episodes_expected) for an entry."""
    if item.kind == "movie":
        return (1 if count_videos(movie_target(cfg, item)[0]) else 0), 1
    if item.kind == "season":
        return count_videos(season_dir(cfg, item)), item.entry.episodes
    return 0, None


def is_complete(have: int, want: Optional[int], status: str) -> bool:
    return bool(want) and have >= want and status != "Currently Airing"


# ------------------------------------------------------------ process runner
async def _run(cfg: Config, job: Job, args: list, env_extra: dict, tail: deque,
               titles: Optional[list] = None) -> int:
    env = {**os.environ, **env_extra}
    proc = await asyncio.create_subprocess_exec(
        cfg.anime_dl, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,          # own process group so /cancel can stop everything
    )
    job.proc = proc
    buf = b""
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            parts = re.split(rb"[\r\n]+", buf + chunk)
            buf = parts.pop()[-2000:]    # yt-dlp progress uses \r, so keep only the tail
            for p in parts:
                if p.strip():
                    line = p.decode(errors="replace").strip()
                    tail.append(line)
                    m = SRC_RE.search(line)
                    if m and titles is not None:
                        titles.append(m.group(1).strip())
        return await proc.wait()
    finally:
        job.proc = None


def _failure_note(tail: deque) -> str:
    """Pick the most informative line from anime-dl's output (not the cleanup chatter)."""
    lines = [l for l in tail if l]
    for pat in (r"no results", r"^error", r"not found|not released", r"giving up", r"failed"):
        for l in reversed(lines):
            if re.search(pat, l, re.I):
                return l[:120]
    return lines[-1][:120] if lines else "no output from anime-dl"


def _base_args(job: Job, name: str, year: Optional[int], season: int, term: str, episodes: str) -> list:
    args = ["-n", name]
    if year:
        args += ["-y", str(year)]
    args += ["-s", str(season), "-Q", term, "-S", str(job.pick), "-e", episodes, "-q", job.quality]
    if job.dub:
        args.append("--dub")
    return args


# ---------------------------------------------------------------- downloads
async def download_item(cfg: Config, job: Job, item: PlanItem) -> None:
    if item.kind == "movie":
        await _download_movie(cfg, job, item)
    else:
        await _download_season(cfg, job, item)


async def _probe(cfg: Config, job: Job, item: PlanItem, name: str, year: Optional[int],
                 term: str, ep: int, tail: deque):
    """Download ONE episode into a scratch library to learn which show the source really
    returned. Returns (files, source_title, rc); the caller decides whether to keep them."""
    scratch = cfg.work_dir / "probe"
    folder = folder_name(name, year)
    shutil.rmtree(scratch / folder, ignore_errors=True)
    titles: list = []
    rc = await _run(
        cfg, job, _base_args(job, name, year, item.season, term, str(ep)),
        {"ANIME_BASE": str(scratch), "ANIME_RETRIES": "1"}, tail, titles,
    )
    pdir = scratch / folder / f"Season {item.season:02d}"
    files = sorted(pdir.glob(f"*S{item.season:02d}E{ep:02d}*")) if pdir.is_dir() else []
    return files, (titles[-1] if titles else None), rc


async def _download_season(cfg: Config, job: Job, item: PlanItem) -> None:
    e = item.entry
    r = job.results[e.mal_id]
    sdir, name, year = season_target(cfg, item)
    r.want = e.episodes
    r.have = count_videos(sdir)
    if is_complete(r.have, r.want, e.status):
        r.status, r.note = "done", "already in library"
        return

    # Finished shows with a known count get an explicit range (episodes are filed one by
    # one, so progress shows up as it goes). Airing/unknown shows use "all".
    spec = f"1-{e.episodes}" if (e.episodes and e.status == "Finished Airing") else "all"
    terms = search_terms(e, job.search if len(job.items) == 1 else None)
    tail: deque = deque(maxlen=25)
    rc = 1
    mismatch: Optional[str] = None

    if job.verify:
        # Step 1: fetch one episode and check the source gave us the right show
        # (a sequel search can quietly return season 1 instead).
        expected = expected_titles(e)
        chosen = None
        for term in terms:
            ep = next(n for n in range(1, 10_000) if n not in present_episodes(sdir))
            files, src, _ = await _probe(cfg, job, item, name, year, term, ep, tail)
            if job.cancelled:
                break
            if files and (src is None or titles_match(src, expected)):
                sdir.mkdir(parents=True, exist_ok=True)
                for f in files:
                    shutil.move(str(f), str(sdir / f.name))
                if src is None:
                    r.note = "unverified: update anime-dl so it reports the source title"
                chosen = term
                break
            if files and src:
                mismatch = src
            shutil.rmtree(cfg.work_dir / "probe" / folder_name(name, year), ignore_errors=True)
        terms = [chosen] if chosen else []
        r.have = count_videos(sdir)

    for i, term in enumerate(terms):
        last = i == len(terms) - 1
        before = count_videos(sdir)
        rc = await _run(
            cfg, job,
            _base_args(job, name, year, item.season, term, spec),
            {"ANIME_BASE": str(cfg.anime_dir), "ANIME_RETRIES": "3" if last else "1"},
            tail,
        )
        # Try the next title only if this one produced nothing at all
        if job.cancelled or rc == 0 or count_videos(sdir) > before or last:
            break

    r.have = count_videos(sdir)
    r.tail = list(tail)[-8:]
    if job.cancelled:
        r.status = "cancelled"
    elif job.verify and not terms:
        r.status = "failed"
        r.note = (f"wrong show? source returned “{mismatch}” (retry with pick/search, "
                  f"or verify:False to force)") if mismatch else _failure_note(tail)
    elif r.want and r.have >= r.want:
        r.status = "done"
    elif rc == 0 and r.have > 0 and not r.want:
        r.status = "done"
    elif r.have > 0:
        r.status = "partial"
        r.note = "still airing - run again later" if e.status == "Currently Airing" else "some episodes missing"
    else:
        r.status, r.note = "failed", _failure_note(tail)


async def _download_movie(cfg: Config, job: Job, item: PlanItem) -> None:
    e = item.entry
    r = job.results[e.mal_id]
    r.want = 1
    dest, name, year = movie_target(cfg, item)
    r.have = 1 if count_videos(dest) else 0
    if r.have:
        r.status, r.note = "done", "already in library"
        return

    # Reuse anime-dl in a scratch base dir, then file the result as a Jellyfin movie.
    stage_base = cfg.work_dir / "movies"
    stage_base.mkdir(parents=True, exist_ok=True)
    folder = folder_name(name, year)
    src_dir = stage_base / folder / "Season 01"
    expected = expected_titles(e)
    terms = search_terms(e, job.search if len(job.items) == 1 else None)
    tail: deque = deque(maxlen=25)
    mismatch: Optional[str] = None
    video = None
    files: list = []

    for i, term in enumerate(terms):
        last = i == len(terms) - 1
        shutil.rmtree(stage_base / folder, ignore_errors=True)
        titles: list = []
        rc = await _run(
            cfg, job, _base_args(job, name, year, 1, term, "1"),
            {"ANIME_BASE": str(stage_base), "ANIME_RETRIES": "3" if last else "1"}, tail, titles,
        )
        files = sorted(src_dir.glob("*S01E01*")) if src_dir.is_dir() else []
        video = next((f for f in files if f.suffix.lower() in VIDEO_EXT), None)
        src = titles[-1] if titles else None
        if job.cancelled:
            break
        if video and job.verify and src and not titles_match(src, expected):
            mismatch = src
            video = None
            continue
        if video or last:
            break

    r.tail = list(tail)[-8:]
    if job.cancelled:
        r.status = "cancelled"
    elif video:
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            suffix = f.name.split("S01E01", 1)[1]          # ".mp4", ".en.vtt", ...
            shutil.move(str(f), str(dest / f"{folder}{suffix}"))
        r.have, r.status = 1, "done"
    elif mismatch:
        r.status = "failed"
        r.note = f"wrong show? source returned “{mismatch}” (retry with pick/search, or verify:False)"
    else:
        r.status, r.note = "failed", _failure_note(tail)
    shutil.rmtree(stage_base / folder, ignore_errors=True)


# ----------------------------------------------------------------- jellyfin
async def jellyfin_refresh(cfg: Config, session: aiohttp.ClientSession) -> Optional[bool]:
    """True = scan started, False = configured but failed, None = not configured."""
    if not (cfg.jellyfin_url and cfg.jellyfin_key):
        return None
    headers = {
        "Authorization": f'MediaBrowser Token="{cfg.jellyfin_key}"',
        "X-Emby-Token": cfg.jellyfin_key,
    }
    try:
        async with session.post(
            f"{cfg.jellyfin_url}/Library/Refresh", headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return resp.status in (200, 204)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False
