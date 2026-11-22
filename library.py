"""Library report (missing episodes) and health check (broken / leftover files)."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VIDEO_EXT = (".mp4", ".mkv")
SUB_EXT = (".vtt", ".srt", ".ass")
SEASON_RE = re.compile(r"^Season (\d+)$", re.I)
EP_RE = re.compile(r"S(\d+)E(\d+)", re.I)
STALE_DAYS = 7


# ------------------------------------------------------------------ report
def fmt_ranges(nums) -> str:
    """[3, 7, 8, 9] -> '3, 7-9'"""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


@dataclass
class SeasonInfo:
    series: str
    label: str                       # "Season 01" / "Specials"
    season: Optional[int]            # None for Specials
    eps: list = field(default_factory=list)
    files: int = 0
    unnumbered: int = 0              # videos without an SxxEyy in the name
    duplicates: list = field(default_factory=list)
    expected: Optional[int] = None   # from MAL, recorded by earlier downloads

    @property
    def gaps(self) -> list:
        if self.season is None or not self.eps:
            return []
        top = max(max(self.eps), self.expected or 0)
        return [n for n in range(1, top + 1) if n not in set(self.eps)]

    @property
    def incomplete(self) -> bool:
        return bool(self.gaps) or (self.season is not None and self.files == 0)


def season_line(i: "SeasonInfo") -> str:
    """One report line, e.g. '⚠️ **Show** · Season 01 - 10/12 · missing 3, 11-12'."""
    if i.season is None:
        return f"🎞️ **{i.series}** · Specials - {i.files} file(s)"
    bits = []
    if i.expected:
        bits.append(f"{len(i.eps)}/{i.expected}")
    else:
        bits.append(f"{i.files} ep(s)")
    if i.files == 0:
        bits.append("empty")
    if i.gaps:
        bits.append(f"missing {fmt_ranges(i.gaps)}")
    if i.duplicates:
        bits.append(f"duplicates {fmt_ranges(i.duplicates)}")
    if i.unnumbered:
        bits.append(f"{i.unnumbered} unnumbered file(s)")
    return f"{'⚠️' if i.incomplete else '✅'} **{i.series}** · {i.label} - " + " · ".join(bits)


def _videos(d: Path):
    return [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT]


def scan_series(root: Path, expected: Optional[dict] = None, name_filter: str = "") -> list:
    """One SeasonInfo per season folder. `expected` maps "<series folder>/Season NN" -> {"want": n}."""
    expected = expected or {}
    out: list = []
    if not root.is_dir():
        return out
    want = name_filter.casefold()
    for sd in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if not sd.is_dir() or sd.name.startswith("."):
            continue
        if want and want not in sd.name.casefold():
            continue
        for d in sorted(sd.iterdir()):
            if not d.is_dir():
                continue
            m = SEASON_RE.match(d.name)
            if not m and d.name != "Specials":
                continue
            info = SeasonInfo(sd.name, d.name, int(m.group(1)) if m else None)
            seen: dict = {}
            for f in _videos(d):
                info.files += 1
                em = EP_RE.search(f.name)
                if not em:
                    info.unnumbered += 1
                    continue
                n = int(em.group(2))
                seen[n] = seen.get(n, 0) + 1
            info.eps = sorted(seen)
            info.duplicates = sorted(n for n, c in seen.items() if c > 1)
            if info.season is not None:
                exp = expected.get(f"{sd.name}/{d.name}") or {}
                info.expected = exp.get("want")
            out.append(info)
    return out


# ------------------------------------------------------------------ health
@dataclass
class Issue:
    kind: str          # zero-byte | corrupt | leftover | orphan-sub | empty-dir | duplicate | stale-scratch
    path: Path
    detail: str = ""

    @property
    def action(self) -> Optional[str]:
        return {"zero-byte": "delete", "leftover": "delete", "orphan-sub": "delete",
                "empty-dir": "delete", "stale-scratch": "delete", "corrupt": "quarantine"}.get(self.kind)


def _is_stale(p: Path, days: float = STALE_DAYS) -> bool:
    try:
        return time.time() - p.stat().st_mtime > days * 86400
    except OSError:
        return False


def ffprobe_ok(ffprobe: str, path: Path) -> bool:
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True      # can't tell: don't condemn the file


def _walk(root: Path):
    for p in root.rglob("*"):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue                      # staging dirs etc. are handled separately
        yield p


async def health_scan(cfg, ffprobe: Optional[str] = None, deep: bool = False, busy: bool = False) -> list:
    """Find problems in the libraries and scratch space. Nothing is changed here.
    busy=True (a download is running) skips scratch/staging cleanup so live files are never touched."""
    issues: list = []
    sem = asyncio.Semaphore(4)
    loop = asyncio.get_running_loop()

    async def probe(f: Path):
        async with sem:
            ok = await loop.run_in_executor(None, ffprobe_ok, ffprobe, f)
        if not ok:
            issues.append(Issue("corrupt", f, "ffprobe can't read it"))

    tasks = []
    for root in (cfg.anime_dir, cfg.movies_dir):
        if not root.is_dir():
            continue
        by_key: dict = {}
        for p in _walk(root):
            suffix = p.suffix.lower()
            if p.is_file():
                if suffix in VIDEO_EXT:
                    if p.stat().st_size == 0:
                        issues.append(Issue("zero-byte", p, "0 bytes"))
                        continue
                    if deep and ffprobe:
                        tasks.append(probe(p))
                    em = EP_RE.search(p.name)
                    if em:
                        by_key.setdefault((p.parent, int(em.group(1)), int(em.group(2))), []).append(p)
                elif p.name.endswith((".stale", ".corrupt", ".ytdl")) or ".part" in p.name:
                    issues.append(Issue("leftover", p, "interrupted-download leftover"))
                elif suffix in SUB_EXT:
                    stem = p.name
                    for ext in SUB_EXT:
                        if stem.lower().endswith(ext):
                            stem = stem[: -len(ext)]
                    stem = re.sub(r"\.[a-z]{2,3}$", "", stem, flags=re.I)      # ".en"
                    if not any((p.parent / (stem + v)).exists() for v in VIDEO_EXT):
                        issues.append(Issue("orphan-sub", p, "no matching video"))
            elif p.is_dir():
                if not any(p.iterdir()):
                    issues.append(Issue("empty-dir", p, "empty folder"))
        for (_, season, ep), files in by_key.items():
            if len(files) > 1:
                issues.append(Issue("duplicate", files[0].parent,
                                    f"S{season:02d}E{ep:02d} exists {len(files)} times: "
                                    + ", ".join(f.name for f in files)))
        # staging area inside the library: only abandoned partials, never while a job runs
        stage = root / ".anime-dl"
        if stage.is_dir() and not busy:
            for p in stage.rglob("*"):
                if p.is_file() and not p.name.endswith(".lock") and _is_stale(p):
                    issues.append(Issue("stale-scratch", p, f"older than {STALE_DAYS} days"))
    if tasks:
        await asyncio.gather(*tasks)

    if cfg.work_dir.is_dir() and not busy:
        for p in cfg.work_dir.rglob("*"):
            if p.is_file() and not p.name.startswith("state.json") and _is_stale(p):
                issues.append(Issue("stale-scratch", p, f"older than {STALE_DAYS} days"))
    order = {"corrupt": 0, "zero-byte": 1, "duplicate": 2, "leftover": 3, "orphan-sub": 4,
             "stale-scratch": 5, "empty-dir": 6}
    issues.sort(key=lambda i: (order.get(i.kind, 9), str(i.path)))
    return issues


def _inside(path: Path, roots) -> bool:
    rp = path.resolve()
    return any(rp != r.resolve() and r.resolve() in rp.parents for r in roots)


def apply_fixes(cfg, issues: list) -> tuple:
    """Delete leftovers and quarantine corrupt videos. -> (fixed, [error strings]).
    Corrupt videos are moved to <library>/.quarantine, never deleted, and duplicates are never touched."""
    roots = (cfg.anime_dir, cfg.movies_dir, cfg.work_dir)
    fixed, errors = 0, []
    for issue in issues:
        act = issue.action
        if not act:
            continue
        p = issue.path
        try:
            if not _inside(p, roots):
                errors.append(f"refused (outside the libraries): {p}")
                continue
            if act == "delete":
                if p.is_dir():
                    p.rmdir()                      # only succeeds when still empty
                elif p.exists():
                    p.unlink()
            elif act == "quarantine" and p.exists():
                root = next(r for r in (cfg.anime_dir, cfg.movies_dir) if r.resolve() in p.resolve().parents)
                dest = root / ".quarantine" / p.resolve().relative_to(root.resolve())
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dest))
            fixed += 1
        except (OSError, StopIteration) as ex:
            errors.append(f"{p.name}: {ex}")
    # removing files can leave folders empty
    for root in (cfg.anime_dir, cfg.movies_dir):
        if root.is_dir():
            for d in sorted((x for x in _walk(root) if x.is_dir()), key=lambda x: -len(x.parts)):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
    return fixed, errors
