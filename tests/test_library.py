import asyncio
import os
import time
from pathlib import Path

import jobs
import library
from library import Issue, apply_fixes, fmt_ranges, health_scan, scan_series, season_line


def cfg_for(tmp: Path):
    return jobs.Config(anime_dl="x", anime_dir=tmp / "anime", movies_dir=tmp / "movies", work_dir=tmp / "work")


def touch(p: Path, data=b"x", age_days=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    return p


def test_fmt_ranges():
    assert fmt_ranges([3, 7, 8, 9, 12]) == "3, 7-9, 12"
    assert fmt_ranges([]) == ""


def test_scan_finds_gaps_short_tail_and_dupes(tmp_path):
    root = tmp_path / "anime"
    for n in (1, 2, 4, 5):
        touch(root / "Show (2010)" / "Season 01" / f"Show - S01E{n:02d}.mp4")
    touch(root / "Show (2010)" / "Season 01" / "Show - S01E05.mkv")             # duplicate of ep 5
    touch(root / "Show (2010)" / "Specials" / "Show - S00E01 - OVA.mp4")
    (root / "Show (2010)" / "Season 02").mkdir()
    touch(root / "Other (2015)" / "Season 01" / "Other - S01E01.mp4")
    touch(root / ".anime-dl" / "x" / "ignored.mp4")
    infos = scan_series(root, {"Other (2015)/Season 01": {"want": 3}})
    by = {(i.series, i.label): i for i in infos}
    s1 = by[("Show (2010)", "Season 01")]
    assert s1.gaps == [3] and s1.duplicates == [5] and s1.incomplete
    assert by[("Show (2010)", "Season 02")].incomplete                            # empty season
    assert by[("Other (2015)", "Season 01")].gaps == [2, 3]                       # MAL says 3 eps, only 1 on disk
    assert not by[("Show (2010)", "Specials")].incomplete
    assert "missing 3" in season_line(s1) and "duplicates 5" in season_line(s1)
    assert [i.series for i in scan_series(root, name_filter="other")] == ["Other (2015)"]


def test_expected_short_tail_counts_as_missing(tmp_path):
    root = tmp_path / "anime"
    for n in (1, 2, 3):
        touch(root / "S (2020)" / "Season 01" / f"S - S01E{n:02d}.mp4")
    info = scan_series(root, {"S (2020)/Season 01": {"want": 5}})[0]
    assert info.gaps == [4, 5] and "3/5" in season_line(info)


def test_health_scan_and_fix(tmp_path):
    cfg = cfg_for(tmp_path)
    good = touch(cfg.anime_dir / "Show (2010)" / "Season 01" / "Show - S01E01.mp4")
    touch(cfg.anime_dir / "Show (2010)" / "Season 01" / "Show - S01E01.en.vtt")           # has a video: keep
    empty = touch(cfg.anime_dir / "Show (2010)" / "Season 01" / "Show - S01E02.mp4", data=b"")
    left = touch(cfg.anime_dir / "Show (2010)" / "Season 01" / "Show - S01E03.mp4.corrupt")
    orphan = touch(cfg.anime_dir / "Show (2010)" / "Season 01" / "Show - S01E09.en.vtt")
    (cfg.anime_dir / "Gone (2000)" / "Season 01").mkdir(parents=True)
    dup_a = touch(cfg.anime_dir / "Dup (2011)" / "Season 01" / "Dup - S01E01.mp4")
    touch(cfg.anime_dir / "Dup (2011)" / "Season 01" / "Dup - S01E01.mkv")
    live_stage = touch(cfg.anime_dir / ".anime-dl" / "a" / "fresh.mp4.part")              # recent: not stale
    old_stage = touch(cfg.anime_dir / ".anime-dl" / "a" / "old.mp4.part", age_days=30)
    old_scratch = touch(cfg.work_dir / "probe" / "x.mp4", age_days=30)
    state = touch(cfg.work_dir / "state.json", age_days=30)

    issues = asyncio.run(health_scan(cfg))
    kinds = {(i.kind, i.path.name) for i in issues}
    assert ("zero-byte", empty.name) in kinds
    assert ("leftover", left.name) in kinds
    assert ("orphan-sub", orphan.name) in kinds
    assert any(i.kind == "empty-dir" and i.path.name == "Season 01" and "Gone" in str(i.path) for i in issues)
    assert any(i.kind == "duplicate" and "S01E01" in i.detail for i in issues)
    assert ("stale-scratch", old_stage.name) in kinds and ("stale-scratch", old_scratch.name) in kinds
    assert not any(i.path in (good, live_stage, state) for i in issues)

    busy = asyncio.run(health_scan(cfg, busy=True))                                       # a job is running
    assert not any(i.kind == "stale-scratch" for i in busy)

    fixed, errors = apply_fixes(cfg, issues)
    assert not errors and fixed >= 6
    assert good.exists() and live_stage.exists() and state.exists() and dup_a.exists()    # nothing precious touched
    assert not (empty.exists() or left.exists() or orphan.exists() or old_stage.exists() or old_scratch.exists())
    assert not (cfg.anime_dir / "Gone (2000)").exists()


def test_corrupt_video_is_quarantined_not_deleted(tmp_path):
    cfg = cfg_for(tmp_path)
    bad = touch(cfg.anime_dir / "Show (2010)" / "Season 01" / "Show - S01E01.mp4", b"junk")
    fixed, errors = apply_fixes(cfg, [Issue("corrupt", bad, "unreadable")])
    assert fixed == 1 and not errors
    assert (cfg.anime_dir / ".quarantine" / "Show (2010)" / "Season 01" / "Show - S01E01.mp4").read_bytes() == b"junk"


def test_fixes_refuse_paths_outside_the_libraries(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.anime_dir.mkdir()
    outside = touch(tmp_path / "precious.txt")
    fixed, errors = apply_fixes(cfg, [Issue("zero-byte", outside), Issue("leftover", cfg.anime_dir)])
    assert fixed == 0 and len(errors) == 2 and outside.exists() and cfg.anime_dir.exists()
