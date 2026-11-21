"""Runs the real anime-dl bash script against the stub ani-cli."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ANIME_DL = str(Path(__file__).resolve().parent.parent / "anime-dl")
pytestmark = pytest.mark.skipif(not shutil.which("flock"), reason="flock not available")


def run(base: Path, *args, env=None):
    e = {**os.environ, "ANIME_BASE": str(base), "ANIME_RETRIES": "1", "ANIME_MIN_FREE_GB": "0", **(env or {})}
    return subprocess.run([ANIME_DL, "-n", "Show", "-y", "2020", *args], capture_output=True, text=True, env=e,
                          stdin=subprocess.DEVNULL, timeout=120)


def test_integer_episodes_still_work(tmp_path, stub_env):
    r = run(tmp_path, "-e", "1-2")
    assert r.returncode == 0, r.stdout + r.stderr
    names = sorted(p.name for p in (tmp_path / "Show (2020)" / "Season 01").iterdir())
    assert names == ["Show - S01E01.mp4", "Show - S01E02.mp4"]


def test_fractional_episode_goes_to_specials(tmp_path, stub_env, monkeypatch):
    monkeypatch.setenv("STUB_DECIMALS", "1.5")
    r = run(tmp_path, "-e", "1-")             # open-ended: ani-cli also returns the 1.5 recap
    assert r.returncode == 0, r.stdout + r.stderr
    show = tmp_path / "Show (2020)"
    assert sorted(p.name for p in (show / "Season 01").iterdir()) == ["Show - S01E01.mp4", "Show - S01E02.mp4"]
    assert [p.name for p in (show / "Specials").iterdir()] == ["Show - S00E10015 - Season 01 Episode 1.5.mp4"]
    assert "Unrecognized" not in r.stderr
    # re-running must not duplicate or fail
    r2 = run(tmp_path, "-e", "1-")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert len(list((show / "Specials").iterdir())) == 1


def test_unrecognized_name_is_quarantined_not_looped(tmp_path, stub_env):
    stage = tmp_path / ".anime-dl" / "Show_(2020)-S01"
    stage.mkdir(parents=True)
    clip = os.environ.get("STUB_CLIP")
    if clip:
        shutil.copy(clip, stage / "Weird Name.mp4")      # valid video, so only the name is unusual
    else:
        (stage / "Weird Name.mp4").write_bytes(b"x")
    r = run(tmp_path, "-e", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / ".anime-dl" / "unrecognized" / "Weird Name.mp4").exists()
    assert "Unrecognized filename moved" in r.stderr
