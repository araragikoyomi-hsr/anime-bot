"""Startup checks and disk-space estimates."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Optional

# Rough size of one 24-minute episode at each quality setting (GB). Deliberately on the high side.
GB_PER_EP = {"best": 0.7, "1080": 0.7, "720": 0.4, "480": 0.22, "360": 0.14}
UNKNOWN_EPISODES = 12     # assumed length when MAL doesn't know the episode count
MOVIE_EPISODES = 3        # a movie is about three episodes of data


def free_gb(path: Path) -> float:
    """Free space (GB) on the filesystem holding path, or on its nearest existing parent."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1024 ** 3


def estimate_gb(remaining_episodes: float, quality: str) -> float:
    return remaining_episodes * GB_PER_EP.get(quality, GB_PER_EP["best"])


def remaining_episodes(kind: str, have: int, want: Optional[int]) -> float:
    """Episodes still to fetch for one plan item (library_state gives have/want)."""
    if kind == "movie":
        return 0 if have else MOVIE_EPISODES
    return max((want or UNKNOWN_EPISODES) - have, 0)


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True      # can't tell, don't nag


def preflight(cfg, which: Callable[[str], Optional[str]] = shutil.which) -> tuple:
    """-> (errors, warnings). Errors stop the bot from starting; warnings are logged."""
    errors, warnings = [], []

    if not (os.path.isfile(cfg.anime_dl) and os.access(cfg.anime_dl, os.X_OK)):
        errors.append(f"anime-dl not found or not executable at {cfg.anime_dl}")
    if not which("ani-cli"):
        errors.append("ani-cli is not on PATH (the service's PATH= line must include it)")
    if not which("flock"):
        errors.append("flock is not on PATH (install util-linux)")
    have_ytdlp, have_ffmpeg = which("yt-dlp"), which("ffmpeg")
    if not (have_ytdlp or have_ffmpeg):
        errors.append("neither yt-dlp nor ffmpeg is on PATH (ani-cli needs one to download)")
    elif not have_ytdlp:
        warnings.append("yt-dlp not found: interrupted downloads restart instead of resuming")
    if not which("ffprobe"):
        warnings.append("ffprobe not found: corrupt files won't be detected (install ffmpeg)")

    for label, d in (("ANIME_DIR", cfg.anime_dir), ("MOVIES_DIR", cfg.movies_dir)):
        if d.exists():
            if not os.access(d, os.W_OK):
                errors.append(f"{label} {d} is not writable by this user")
        else:
            parent = d
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if os.access(parent, os.W_OK):
                warnings.append(f"{label} {d} doesn't exist yet; it will be created on first download")
            else:
                errors.append(f"{label} {d} doesn't exist and can't be created (no write access to {parent})")

    if cfg.work_dir.exists():
        for d in (cfg.anime_dir, cfg.movies_dir):
            if d.exists() and not _same_fs(cfg.work_dir, d):
                warnings.append(f"WORK_DIR and {d} are on different filesystems: finished files "
                                f"will be copied instead of moved instantly")
    if bool(cfg.jellyfin_url) != bool(cfg.jellyfin_key):
        warnings.append("only one of JELLYFIN_URL / JELLYFIN_API_KEY is set, library scans are disabled")
    if cfg.anime_dir.exists() and free_gb(cfg.anime_dir) < cfg.min_free_gb:
        warnings.append(f"less than {cfg.min_free_gb:g} GB free on {cfg.anime_dir}; downloads will be refused")
    return errors, warnings
