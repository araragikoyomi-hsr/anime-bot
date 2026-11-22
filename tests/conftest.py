import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
STUB_DIR = Path(__file__).with_name("stub")


@pytest.fixture(scope="session")
def clip(tmp_path_factory) -> str:
    """A tiny valid mp4 (needed when ffprobe is installed, since anime-dl rejects invalid files)."""
    if not shutil.which("ffmpeg"):
        return ""
    out = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    return str(out)


@pytest.fixture
def stub_env(monkeypatch, clip):
    """PATH with the stub ani-cli first. Skips when ffprobe exists without ffmpeg (can't make a valid clip)."""
    if shutil.which("ffprobe") and not clip:
        pytest.skip("ffprobe installed but ffmpeg missing: can't build a test clip")
    monkeypatch.setenv("PATH", f"{STUB_DIR}:/usr/bin:/bin:/usr/local/bin")
    monkeypatch.setenv("STUB_CLIP", clip)
    for k in ("STUB_TITLE", "STUB_MAX", "STUB_DECIMALS"):
        monkeypatch.delenv(k, raising=False)
