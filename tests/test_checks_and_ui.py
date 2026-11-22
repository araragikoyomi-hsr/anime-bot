import asyncio
from pathlib import Path

import discord
import pytest

import bot
import checks
import jobs
from mal import Entry, build_plan


def E(mid, title, typ="TV", eps=12, year=2010, **kw):
    return Entry(mal_id=mid, title=title, title_en=None, type=typ, episodes=eps, year=year,
                 aired_from=f"{year}-01-01", status="Finished Airing", **kw)


def cfg_for(tmp: Path, **kw):
    return jobs.Config(anime_dl=str(tmp / "anime-dl"), anime_dir=tmp / "anime", movies_dir=tmp / "movies",
                       work_dir=tmp / "work", **kw)


# ------------------------------------------------------------- startup checks
def test_preflight_reports_every_missing_tool(tmp_path):
    cfg = cfg_for(tmp_path)
    errors, warnings = checks.preflight(cfg, which=lambda name: None)
    text = " | ".join(errors)
    assert "anime-dl" in text and "ani-cli" in text and "flock" in text and "yt-dlp nor ffmpeg" in text
    assert any("ffprobe" in w for w in warnings)


def test_preflight_ok_and_warnings(tmp_path):
    (tmp_path / "anime-dl").write_text("#!/bin/sh\n")
    (tmp_path / "anime-dl").chmod(0o755)
    cfg = cfg_for(tmp_path, jellyfin_url="http://x")
    have = {"ani-cli", "flock", "ffmpeg", "ffprobe"}                      # no yt-dlp
    errors, warnings = checks.preflight(cfg, which=lambda n: f"/usr/bin/{n}" if n in have else None)
    assert errors == []
    assert any("yt-dlp" in w for w in warnings) and any("JELLYFIN" in w for w in warnings)


def test_preflight_unwritable_dir(tmp_path):
    (tmp_path / "anime-dl").write_text("")
    (tmp_path / "anime-dl").chmod(0o755)
    cfg = cfg_for(tmp_path)
    cfg.anime_dir.mkdir()
    cfg.anime_dir.chmod(0o500)
    try:
        import os
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere")
        errors, _ = checks.preflight(cfg, which=lambda n: "/x")
        assert any("not writable" in e for e in errors)
    finally:
        cfg.anime_dir.chmod(0o700)


def test_estimates():
    assert checks.remaining_episodes("season", 10, 12) == 2
    assert checks.remaining_episodes("season", 0, None) == checks.UNKNOWN_EPISODES
    assert checks.remaining_episodes("movie", 1, None) == 0
    assert checks.estimate_gb(10, "720") < checks.estimate_gb(10, "best")


def test_disk_guard_blocks_download(tmp_path):
    cfg = cfg_for(tmp_path, min_free_gb=10 ** 9)                         # nobody has an exabyte free
    with pytest.raises(jobs.DiskSpaceError):
        jobs.check_space(cfg)
    jobs.check_space(cfg_for(tmp_path, min_free_gb=0))


# ------------------------------------------------------------------------ UI
def test_render_job_progress_bar_and_live_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "cfg", cfg_for(tmp_path))
    plan = build_plan([E(1, "Show")], 1)
    job = jobs.Job(id=3, owner=1, channel_id=1, plan=plan, items=plan.items)
    r = job.results[1]
    r.status, r.have, r.want = "running", 6, 12
    monkeypatch.setattr(bot, "current", job)
    job.activity.feed("Episode 7 (attempt 1/3)")
    job.activity.feed("[download]  50.0% of 100MiB")
    job.started_at = 1
    emb = bot.render_job(job)
    assert "6/12 ▰▰▰▰▰▱▱▱▱▱" in emb.description and "*ep 7 · 50%*" in emb.description
    assert emb.colour == discord.Colour.blurple()
    monkeypatch.setattr(bot, "current", None)
    r.status = "failed"
    assert bot.render_job(job).colour == discord.Colour.red()


def test_bar_and_duration():
    assert bot.bar(0, 4) == "▱" * 10 and bot.bar(4, 4) == "▰" * 10 and bot.bar(1, None) == ""
    assert bot.fmt_duration(75) == "1m 15s" and bot.fmt_duration(3720) == "1h 02m"


def test_preview_shows_space_estimate_and_specials_are_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "cfg", cfg_for(tmp_path))
    entries = [E(1, "Show"), E(2, "Show OVA", "OVA", 2, 2011, parent_id=1)]
    plan, states, selected = bot.prepare(entries, False, 1, "split")
    assert selected == {1}                                               # OVA not selected by default
    emb = bot.preview_embed(plan, states, selected, bot.Opts())
    assert "GB needed" in emb.footer.text

    async def go():
        view = bot.PreviewView(1, entries, False, plan, states, selected, bot.Opts(), 1)
        assert not view.specials_btn.disabled
        assert view._pending_specials() == {2}
    asyncio.run(go())


def test_retry_view_requeues_only_failed_items(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "cfg", cfg_for(tmp_path))
    plan = build_plan([E(1, "Show"), E(2, "Show 2nd Season", year=2012)], 1)
    job = jobs.Job(id=1, owner=5, channel_id=1, plan=plan, items=plan.items, pick=1)
    job.results[1].status, job.results[2].status = "done", "failed"
    submitted = []
    monkeypatch.setattr(bot, "_job_counter", 1)                          # as if job #1 was created normally

    async def fake_submit(j):
        submitted.append(j)
        return 1
    monkeypatch.setattr(bot, "submit", fake_submit)

    class FakeResponse:
        async def edit_message(self, **kw): pass
        async def send_message(self, *a, **kw): self.denied = True

    class FakeFollowup:
        async def send(self, *a, **kw): pass

    class FakeInteraction:
        def __init__(self, uid):
            self.user = type("U", (), {"id": uid})()
            self.response, self.followup = FakeResponse(), FakeFollowup()

    async def go():
        view = bot.RetryView(job)
        assert [it.entry.mal_id for it in view.failed] == [2]
        stranger = FakeInteraction(99)
        await view.go(stranger, "x")
        assert not submitted and stranger.response.denied                # not the owner
        await view.go(FakeInteraction(5), "next", pick=2, exact=True)
    asyncio.run(go())
    new = submitted[0]
    assert [it.entry.mal_id for it in new.items] == [2] and new.pick == 2 and new.exact and new.verify
    assert new.id > job.id


def test_restore_queue_resumes_and_drops_crash_loops(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "cfg", cfg_for(tmp_path))
    monkeypatch.setattr(bot, "store", bot.Store(tmp_path / "state.json"))
    plan = build_plan([E(1, "Show")], 1)
    ok = jobs.Job(id=1, owner=1, channel_id=10, plan=plan, items=plan.items, attempts=1)
    loop = jobs.Job(id=2, owner=1, channel_id=10, plan=plan, items=plan.items, attempts=bot.MAX_ATTEMPTS)
    bot.store.set_queue([ok, loop])
    monkeypatch.setattr(bot, "pending", [])
    monkeypatch.setattr(bot, "queue", None)
    monkeypatch.setattr(bot, "startup_notes", [])

    async def go():
        monkeypatch.setattr(bot, "queue", asyncio.Queue())
        monkeypatch.setattr(bot.bot, "get_partial_messageable", lambda cid: None)
        bot.restore_queue()
        assert [j.id for j in bot.pending] == [1] and bot.queue.qsize() == 1
    asyncio.run(go())
    assert any("Dropped job #2" in t for _, t in bot.startup_notes)
    assert any("resuming 1" in t for _, t in bot.startup_notes)
    assert [d["id"] for d in bot.store.get_queue()] == [1]               # crash-looper is gone from disk


def test_library_and_health_embeds_render(tmp_path):
    from library import Issue, SeasonInfo
    infos = [SeasonInfo("Show (2010)", "Season 01", 1, eps=[1, 2, 4], files=3),
             SeasonInfo("Done (2011)", "Season 01", 1, eps=[1, 2], files=2)]
    emb = bot.library_embed(infos, False, None)
    assert "missing 3" in emb.description and "Done" not in emb.description
    assert "1 incomplete" in emb.footer.text
    assert "Done" in bot.library_embed(infos, True, "o").description
    assert "No missing" in bot.library_embed([infos[1]], False, None).description

    async def go():
        view = bot.HealthView(1, [Issue("zero-byte", tmp_path / "a.mp4"), Issue("duplicate", tmp_path, "x")])
        assert "Fix 1 issue" in view.fix_btn.label and not view.fix_btn.disabled
        assert bot.HealthView(1, [Issue("duplicate", tmp_path)]).fix_btn.disabled
    asyncio.run(go())
