#!/usr/bin/env python3
"""Stand-in for anime-dl: writes SxxEyy files into $ANIME_BASE like the real script does.

FAKE_TITLE      title printed as "Source title:" for the good result
FAKE_GOOD_PICK  only this -S pick returns FAKE_TITLE; others return "Some Other Show"
FAKE_NO_RESULT  print "no results" and exit 1
"""
import os, re, sys
from pathlib import Path

a = sys.argv[1:]
opt = {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2) if a[i].startswith("-")}
name, season, spec, pick = opt["-n"], int(opt.get("-s", 1)), opt["-e"], int(opt.get("-S", 1))
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(a) + "\n")
if os.environ.get("FAKE_NO_RESULT"):
    print("no results found"); sys.exit(1)

eps = []
for part in spec.split(","):
    if m := re.fullmatch(r"(\d+)-(\d+)", part): eps += range(int(m[1]), int(m[2]) + 1)
    elif m := re.fullmatch(r"(\d+)-", part):    eps += range(int(m[1]), int(os.environ.get("FAKE_MAX", "3")) + 1)
    elif part == "all":                          eps += range(1, int(os.environ.get("FAKE_MAX", "3")) + 1)
    else:                                        eps.append(int(part))

good = os.environ.get("FAKE_GOOD_PICK")
title = os.environ.get("FAKE_TITLE", "Test Show")
if good and int(good) != pick:
    title = "Some Other Show"
d = Path(os.environ["ANIME_BASE"]) / name / f"Season {season:02d}"
d.mkdir(parents=True, exist_ok=True)
for ep in eps:
    p = d / f"{name} - S{season:02d}E{ep:02d}.mp4"
    if not p.exists():
        p.write_bytes(b"x" * 10)
        (d / f"{name} - S{season:02d}E{ep:02d}.en.vtt").write_text("WEBVTT")
    print(f"Source title: {title}")
