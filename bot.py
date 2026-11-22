"""Discord front-end: share a MAL/AniList link (or a title), pick what to grab, done.

Commands:  /anime  /status  /cancel  /history  /library  /health
Optional:  post a link in WATCH_CHANNEL_ID and the bot picks it up with no command.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands

import checks
import jobs
import library
from jobs import Config, DiskSpaceError, ItemResult, Job
from mal import ANILIST_RE, MAL_RE, Tenrai, MalError, Plan, build_plan
from store import Store, history_record, job_from_dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("anime-bot")

# ------------------------------------------------------------------- config
TOKEN = os.environ.get("DISCORD_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID", "").isdigit() else None
WATCH_CHANNEL_ID = int(os.environ["WATCH_CHANNEL_ID"]) if os.environ.get("WATCH_CHANNEL_ID", "").isdigit() else None
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "best")
DEFAULT_DUB = os.environ.get("DEFAULT_DUB", "0") == "1"
DEFAULT_VERIFY = os.environ.get("VERIFY_TITLES", "1") != "0"
DEFAULT_EXACT = os.environ.get("DEFAULT_EXACT", "0") == "1"
MAX_ATTEMPTS = 3          # a job that was running when the bot died this many times is dropped
cfg = Config.from_env()
store = Store(cfg.work_dir / "state.json")


def preflight() -> None:
    problems = []
    if not TOKEN:
        problems.append("DISCORD_TOKEN is not set")
    if not ALLOWED:
        problems.append("ALLOWED_USER_IDS is empty (refusing to run an open download bot)")
    errors, warnings = checks.preflight(cfg)
    problems += errors
    if problems:
        sys.exit("Startup problems:\n  - " + "\n  - ".join(problems))
    for w in warnings:
        log.warning("startup check: %s", w)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Opts:
    quality: str = DEFAULT_QUALITY
    dub: bool = DEFAULT_DUB
    pick: int = 1
    search: Optional[str] = None
    verify: bool = DEFAULT_VERIFY
    exact: bool = DEFAULT_EXACT


# ---------------------------------------------------------------- job queue
queue: "asyncio.Queue[Job]" = asyncio.Queue()
pending: list = []
current: Optional[Job] = None
_job_counter = 0
startup_notes: list = []       # (channel_id, text) posted once the bot is connected

ICONS = {"queued": "⏳", "running": "⬇️", "done": "✅", "partial": "⚠️", "failed": "❌", "cancelled": "🛑"}


def persist_queue() -> None:
    """Write the running + waiting jobs to disk so a restart can pick them up again."""
    live = ([current] if current else []) + pending
    store.set_queue([j for j in live if not j.cancelled])


def bar(have: int, want: Optional[int], width: int = 10) -> str:
    if not want:
        return ""
    filled = max(0, min(width, round(width * have / want)))
    return "▰" * filled + "▱" * (width - filled)


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def item_label(it, plan: Plan) -> str:
    if it.kind == "movie":
        return "Movie"
    multi = len({p.series for p in plan.items if p.kind in ("season", "special")}) > 1
    if it.kind == "special":
        label = it.entry.type or "Special"
        return f"{it.series} {label}" if multi else label
    return f"{it.series} S{it.season:02d}" if multi else f"S{it.season:02d}"


def job_colour(job: Job) -> discord.Colour:
    if job is current:
        return discord.Colour.blurple()
    states = {r.status for r in job.results.values()}
    if "failed" in states:
        return discord.Colour.red()
    if "partial" in states:
        return discord.Colour.orange()
    if states == {"done"}:
        return discord.Colour.green()
    return discord.Colour.light_grey()


def render_job(job: Job) -> discord.Embed:
    lines = []
    for it in job.items:
        r: ItemResult = job.results[it.entry.mal_id]
        count = ""
        if it.kind in ("season", "special") and (r.have or r.want):
            count = f" · {r.have}/{r.want or '?'}"
            if (b := bar(r.have, r.want)):
                count += f" {b}"
        note = f" — {r.note}" if r.note else ""
        live = ""
        if r.status == "running" and job is current and (act := job.activity.render()):
            live = f" — *{act}*"
        lines.append(f"{ICONS[r.status]} **{item_label(it, job.plan)}** {it.entry.display}{count}{live}{note}")
    state = "running" if job is current else ("queued" if job in pending else "finished")
    title = ("⬇️ " if job is current else "") + job.title
    emb = discord.Embed(title=title[:250], description="\n".join(lines)[:4000], colour=job_colour(job))
    foot = f"Job #{job.id} · {state} · {job.quality} · {'dub' if job.dub else 'sub'}"
    if job.exact:
        foot += " · exact title"
    if job.started_at:
        foot += f" · {fmt_duration((job.finished_at or time.time()) - job.started_at)}"
    emb.set_footer(text=foot)
    return emb


async def update_status(job: Job) -> None:
    if job.status_msg:
        try:
            await job.status_msg.edit(embed=render_job(job))
        except discord.HTTPException:
            pass


async def submit(job: Job) -> int:
    channel = bot.get_partial_messageable(job.channel_id)
    pending.append(job)
    job.status_msg = await channel.send(embed=render_job(job))
    await queue.put(job)
    persist_queue()
    return len(pending) + (1 if current else 0)


async def ticker(job: Job) -> None:
    """Refresh counters and the live 'ep 4 · 45%' line while a job is running."""
    last = None
    while True:
        await asyncio.sleep(8)
        for it in job.items:
            r = job.results[it.entry.mal_id]
            if r.status == "running" and it.kind == "season":
                r.have = jobs.count_videos(jobs.season_dir(cfg, it))
        sig = (tuple(r.have for r in job.results.values()), job.activity.render())
        if sig != last:
            last = sig
            await update_status(job)


def record_expected(it, r: ItemResult) -> None:
    """Remember how many episodes a season should have, so /library can spot a short tail."""
    if it.kind == "season" and r.want and r.status in ("done", "partial"):
        d = jobs.season_dir(cfg, it)
        store.set_expected(f"{d.parent.name}/{d.name}", r.want, it.entry.mal_id)


async def run_job(job: Job) -> None:
    job.started_at = job.started_at or time.time()
    tick = asyncio.create_task(ticker(job))
    try:
        for it in job.items:
            r = job.results[it.entry.mal_id]
            if r.status == "done":                        # finished before a restart
                continue
            if job.cancelled:
                r.status = "cancelled"
                continue
            r.status = "running"
            await update_status(job)
            try:
                await jobs.download_item(cfg, job, it)
            except DiskSpaceError as ex:
                r.status, r.note = "failed", str(ex)[:150]
            except Exception as ex:                       # keep the queue alive no matter what
                log.exception("download failed")
                r.status, r.note = "failed", str(ex)[:100]
            record_expected(it, r)
            persist_queue()
            await update_status(job)
    finally:
        tick.cancel()
    job.finished_at = time.time()

    counts: dict = {}
    for r in job.results.values():
        counts[r.status] = counts.get(r.status, 0) + 1
    scan = None
    if any(r.status in ("done", "partial") and r.have for r in job.results.values()):
        scan = await jobs.jellyfin_refresh(cfg, bot.http_session)

    parts = [f"{ICONS[k]} {v} {k}" for k, v in counts.items()]
    msg = f"<@{job.owner}> **{job.title}** finished: " + ", ".join(parts)
    if scan is True:
        msg += "\n📚 Jellyfin library scan started."
    elif scan is False:
        msg += "\n⚠️ Couldn't reach Jellyfin for a scan (check JELLYFIN_URL / API key)."
    view = None
    if counts.get("failed") or counts.get("partial"):
        msg += "\nUse the buttons to retry, or re-run the same link later - finished episodes are skipped."
        details = []
        for it in job.items:
            r = job.results[it.entry.mal_id]
            if r.status in ("failed", "partial") and r.tail:
                body = "\n".join(r.tail[-5:]).replace("```", "'''")[:350]
                details.append(f"**{item_label(it, job.plan)}** - {r.note}\n```\n{body}\n```")
        if details:
            msg += "\n" + "\n".join(details)[:1300]
        view = RetryView(job)
    channel = bot.get_partial_messageable(job.channel_id)
    kwargs = {"view": view} if view else {}
    await channel.send(msg[:1990], allowed_mentions=discord.AllowedMentions(users=[discord.Object(job.owner)]), **kwargs)
    store.add_history(history_record(job, {it.entry.mal_id: item_label(it, job.plan) for it in job.items}))


async def worker() -> None:
    global current
    while True:
        job = await queue.get()
        if job in pending:
            pending.remove(job)
        current = job
        job.attempts += 1
        persist_queue()
        try:
            await run_job(job)
        except Exception:
            log.exception("job crashed")
        finally:
            current = None
            persist_queue()
            await update_status(job)
            queue.task_done()


def restore_queue() -> None:
    """Re-queue jobs that were waiting or running when the bot last stopped."""
    global _job_counter
    _job_counter = max(_job_counter, store.max_job_id())
    restored = 0
    for d in store.get_queue():
        try:
            job = job_from_dict(d)
        except Exception:
            log.exception("dropping an unreadable queued job")
            continue
        if job.attempts >= MAX_ATTEMPTS:
            log.error("job #%s was running %d times without finishing; dropping it", job.id, job.attempts)
            startup_notes.append((job.channel_id, f"⚠️ Dropped job #{job.id} ({job.title}): it was interrupted "
                                                  f"{job.attempts} times in a row. Re-run it manually."))
            continue
        sm = d.get("status_msg")
        if sm:
            job.status_msg = bot.get_partial_messageable(sm[0]).get_partial_message(sm[1])
        pending.append(job)
        queue.put_nowait(job)
        restored += 1
    if restored:
        startup_notes.append((pending[0].channel_id, f"♻️ Back online - resuming {restored} queued job(s)."))
    persist_queue()


# ---------------------------------------------------------------- bot setup
class AnimeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        if WATCH_CHANNEL_ID:
            intents.message_content = True      # privileged: enable it in the developer portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession
        self.jikan: Tenrai
        self.worker_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "anime-bot/1.0"})
        self.jikan = Tenrai(self.http_session)
        restore_queue()
        self.worker_task = asyncio.create_task(worker())
        if GUILD_ID:                            # guild sync is instant; global takes up to an hour
            guild = discord.Object(GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        if current:                              # a clean stop isn't a crash: don't count it against the job
            current.attempts = max(0, current.attempts - 1)
            persist_queue()
        await super().close()
        await self.http_session.close()


bot = AnimeBot()


# ----------------------------------------------------------- retry buttons
class SearchModal(discord.ui.Modal, title="Retry with different search text"):
    text = discord.ui.TextInput(label="Search text (first failed item only)", max_length=100,
                                placeholder="Title as it appears on the download source")

    def __init__(self, view: "RetryView") -> None:
        super().__init__()
        self.retry = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.retry.go(interaction, "new search text", search=str(self.text).strip() or None)


class RetryView(discord.ui.View):
    """Buttons under a failed/partial job: re-queue only the entries that didn't finish."""

    def __init__(self, job: Job) -> None:
        super().__init__(timeout=3600)
        self.job = job
        self.failed = [it for it in job.items if job.results[it.entry.mal_id].status in ("failed", "partial")]
        self.next_btn.disabled = job.pick >= 10
        self.force_btn.disabled = not job.verify
        self.exact_btn.disabled = job.exact

    async def go(self, interaction: discord.Interaction, label: str, *, pick: Optional[int] = None,
                 verify: Optional[bool] = None, exact: Optional[bool] = None, search: Optional[str] = None) -> None:
        global _job_counter
        if interaction.user.id != self.job.owner:
            await interaction.response.send_message("This one belongs to someone else.", ephemeral=True)
            return
        if not self.failed:
            await interaction.response.send_message("Nothing left to retry.", ephemeral=True)
            return
        items = self.failed[:1] if search else self.failed
        if search is None and len(items) == 1:
            search = self.job.search
        _job_counter += 1
        job = Job(id=_job_counter, owner=self.job.owner, channel_id=self.job.channel_id, plan=self.job.plan,
                  items=items, quality=self.job.quality, dub=self.job.dub, pick=pick or self.job.pick,
                  search=search, verify=self.job.verify if verify is None else verify,
                  exact=self.job.exact if exact is None else exact)
        self.stop()
        await interaction.response.edit_message(view=None)
        position = await submit(job)
        extra = f" It's #{position} in the queue." if position > 1 else ""
        await interaction.followup.send(f"🔁 Queued job #{job.id} ({label}, {len(items)} item(s)).{extra}")

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.success, emoji="🔁")
    async def retry_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.go(interaction, "same settings")

    @discord.ui.button(label="Next search result", style=discord.ButtonStyle.primary, emoji="⏭️")
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.go(interaction, f"result #{self.job.pick + 1}", pick=self.job.pick + 1)

    @discord.ui.button(label="Exact title", style=discord.ButtonStyle.primary, emoji="🎯")
    async def exact_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.go(interaction, "exact title match", exact=True)

    @discord.ui.button(label="Change search", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def search_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.job.owner:
            await interaction.response.send_message("This one belongs to someone else.", ephemeral=True)
            return
        await interaction.response.send_modal(SearchModal(self))

    @discord.ui.button(label="Skip title check", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def force_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.go(interaction, "title check off", verify=False)


# ---------------------------------------------------------------------- UI
def prepare(entries: list, truncated: bool, focus_id: int, mode: str):
    """Build the plan, look at the library, and pick the default selection:
    the linked entry and everything after it, minus what you already have.
    OVAs/specials are opt-in (button), unless you linked one directly."""
    plan = build_plan(entries, focus_id, truncated, mode)
    focus = next((p for p in plan.items if p.entry.mal_id == focus_id), None)
    focus_is_special = bool(focus and focus.kind == "special")
    states, selected, reached = {}, set(), False
    for p in plan.items:
        reached = reached or p.entry.mal_id == focus_id
        states[p.entry.mal_id] = jobs.library_state(cfg, p)
        have, want = states[p.entry.mal_id]
        if p.kind == "skip" or jobs.is_complete(have, want, p.entry.status):
            continue
        if focus_is_special:
            if p.entry.mal_id == focus_id:
                selected.add(p.entry.mal_id)
        elif reached and p.kind != "special":
            selected.add(p.entry.mal_id)
    return plan, states, selected


def estimate_need(plan: Plan, states: dict, selected: set, quality: str) -> float:
    total = 0.0
    for p in plan.items:
        if p.entry.mal_id in selected:
            have, want = states[p.entry.mal_id]
            total += checks.estimate_gb(checks.remaining_episodes(p.kind, have, want), quality)
    return total


def preview_embed(plan: Plan, states: dict, selected: set, opts: Opts) -> discord.Embed:
    lines = []
    for p in plan.items:
        e = p.entry
        if p.kind == "skip":
            why = "not aired yet" if e.status == "Not yet aired" else f"{e.type or 'unknown'} type isn't downloadable"
            lines.append(f"▫️ ~~{e.type or '?'} · {e.display}~~ ({why})")
            continue
        have, want = states[e.mal_id]
        done = jobs.is_complete(have, want, e.status)
        mark = "✅" if done else ("⬇️" if e.mal_id in selected else "⬜")
        eps = f"{e.episodes} eps" if e.episodes else "? eps"
        extra = " — in library" if done else (f" — {have}/{want or '?'} in library" if have else "")
        lines.append(f"{mark} **{item_label(p, plan)}** · {e.display} ({eps}){extra}")
    if plan.truncated:
        lines.append("\n*Large franchise: only the first entries were loaded.*")
    emb = discord.Embed(title=plan.title, description="\n".join(lines)[:4000], colour=discord.Colour.green())
    focus = next((p.entry for p in plan.items if p.entry.mal_id == plan.focus_id), None)
    if focus and focus.image:
        emb.set_thumbnail(url=focus.image)
    layout = ("each title gets its own folder" if plan.mode == "split"
              else "one series, seasons in air order")
    need, free = estimate_need(plan, states, selected, opts.quality), checks.free_gb(cfg.anime_dir)
    space = f"💾 ~{need:.0f} GB needed · {free:.0f} GB free"
    if need + cfg.min_free_gb > free:
        space = "⚠️ " + space + " - may not fit"
    emb.set_footer(text=f"{opts.quality} · {'dub' if opts.dub else 'sub'}{' · exact title' if opts.exact else ''}"
                        f" · layout: {layout}\n{space}\n"
                        "⬇️ = will download. Adjust the dropdown, then press Download.")
    return emb


class PreviewView(discord.ui.View):
    def __init__(self, owner: int, entries: list, truncated: bool, plan: Plan, states: dict,
                 selected: set, opts: Opts, channel_id: int):
        super().__init__(timeout=600)
        self.owner, self.entries, self.truncated = owner, entries, truncated
        self.plan, self.opts, self.channel_id = plan, opts, channel_id
        self.states = states
        self.selected = set(selected)
        self.space_warned = False

        options = []
        for p in plan.selectable:
            e = p.entry
            have, want = states[e.mal_id]
            label = f"{item_label(p, plan)} · {e.display}"
            desc = f"{e.type} · {e.episodes or '?'} eps · {e.year or '?'}"
            if jobs.is_complete(have, want, e.status):
                desc += " · in library"
            options.append(discord.SelectOption(label=label[:100], value=str(e.mal_id),
                                                description=desc[:100], default=e.mal_id in self.selected))
        self.select = discord.ui.Select(placeholder="Choose what to download", min_values=0,
                                        max_values=len(options), options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

        # the layout toggle only matters when "split" actually finds more than one series
        split = build_plan(entries, plan.focus_id, truncated, "split")
        self.layout_btn.label = "Layout: split by title" if plan.mode == "split" else "Layout: one series"
        self.layout_btn.disabled = len({p.series for p in split.items if p.kind == "season"}) <= 1
        self.specials_btn.disabled = not self._pending_specials()

    def _pending_specials(self) -> set:
        return {p.entry.mal_id for p in self.plan.selectable if p.kind == "special"
                and not jobs.is_complete(*self.states[p.entry.mal_id], p.entry.status)}

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner:
            await interaction.response.send_message("This one belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction) -> None:
        if not await self._owner_only(interaction):
            return
        self.selected = {int(v) for v in self.select.values}
        self.space_warned = False
        for opt in self.select.options:           # keep the dropdown showing what was just picked
            opt.default = int(opt.value) in self.selected
        await interaction.response.edit_message(
            embed=preview_embed(self.plan, self.states, self.selected, self.opts), view=self)

    @discord.ui.button(label="Download", style=discord.ButtonStyle.success, emoji="⬇️", row=1)
    async def start_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        global _job_counter
        if not await self._owner_only(interaction):
            return
        items = [p for p in self.plan.items if p.entry.mal_id in self.selected]
        if not items:
            await interaction.response.send_message("Pick at least one entry first.", ephemeral=True)
            return
        need = estimate_need(self.plan, self.states, self.selected, self.opts.quality)
        free = checks.free_gb(cfg.anime_dir)
        if need + cfg.min_free_gb > free and not self.space_warned:
            self.space_warned = True
            await interaction.response.send_message(
                f"⚠️ This may not fit: about {need:.0f} GB needed, {free:.0f} GB free "
                f"(keeping {cfg.min_free_gb:g} GB spare). Estimates are rough. "
                "Press **Download** again to queue it anyway, or pick less.", ephemeral=True)
            return
        _job_counter += 1
        job = Job(id=_job_counter, owner=self.owner, channel_id=self.channel_id, plan=self.plan,
                  items=items, quality=self.opts.quality, dub=self.opts.dub,
                  pick=self.opts.pick, search=self.opts.search, verify=self.opts.verify, exact=self.opts.exact)
        self.stop()
        await interaction.response.edit_message(content=f"✅ Queued {len(items)} item(s) - progress below.",
                                                embed=None, view=None)
        position = await submit(job)
        if position > 1:
            await bot.get_partial_messageable(self.channel_id).send(
                f"Job #{job.id} is #{position} in the queue.")

    @discord.ui.button(label="OVAs/specials", style=discord.ButtonStyle.primary, emoji="🎞️", row=1)
    async def specials_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._owner_only(interaction):
            return
        ids = self._pending_specials()
        if ids <= self.selected:
            self.selected -= ids            # all already on: toggle them off
        else:
            self.selected |= ids
        self.space_warned = False
        for opt in self.select.options:
            opt.default = int(opt.value) in self.selected
        await interaction.response.edit_message(
            embed=preview_embed(self.plan, self.states, self.selected, self.opts), view=self)

    @discord.ui.button(label="Layout", style=discord.ButtonStyle.primary, row=1)
    async def layout_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._owner_only(interaction):
            return
        mode = "merge" if self.plan.mode == "split" else "split"
        plan, states, selected = prepare(self.entries, self.truncated, self.plan.focus_id, mode)
        self.stop()
        view = PreviewView(self.owner, self.entries, self.truncated, plan, states, selected,
                           self.opts, self.channel_id)
        await interaction.response.edit_message(embed=preview_embed(plan, states, selected, self.opts), view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._owner_only(interaction):
            return
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class SearchView(discord.ui.View):
    def __init__(self, owner: int, results: list, on_pick):
        super().__init__(timeout=300)
        self.owner, self.on_pick = owner, on_pick
        options = [discord.SelectOption(
            label=e.display[:100], value=str(e.mal_id),
            description=f"{e.type or '?'} · {e.year or '?'} · {e.episodes or '?'} eps"[:100])
            for e in results]
        sel = discord.ui.Select(placeholder="Which one?", options=options)
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def picked(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner:
            await interaction.response.send_message("This one belongs to someone else.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="🔎 Looking up the franchise…", embed=None, view=None)
        await self.on_pick(int(self.sel.values[0]))


# ------------------------------------------------------------------- flows
async def show_preview(msg, owner: int, channel_id: int, mal_id: int, opts: Opts) -> None:
    last = [0.0]

    async def progress(found: int, text: str) -> None:       # live loading text, throttled for Discord
        now = time.monotonic()
        if now - last[0] < 1.2:
            return
        last[0] = now
        try:
            await msg.edit(content=f"🔎 Looking up the franchise… **{found}** found · {text[:60]}")
        except discord.HTTPException:
            pass

    try:
        entries, truncated = await bot.jikan.franchise(mal_id, progress)
    except MalError as ex:
        await msg.edit(content=f"❌ {ex}", embed=None, view=None)
        return
    plan, states, selected = prepare(entries, truncated, mal_id, "split")
    if not plan.selectable:
        await msg.edit(content="Nothing downloadable here (everything found is unaired or not a downloadable type).",
                       embed=None, view=None)
        return
    view = PreviewView(owner, entries, truncated, plan, states, selected, opts, channel_id)
    await msg.edit(content=None, embed=preview_embed(plan, states, selected, opts), view=view)


async def handle_query(msg, owner: int, channel_id: int, query: str, opts: Opts) -> None:
    try:
        kind, value = await bot.jikan.resolve(query)
    except MalError as ex:
        await msg.edit(content=f"❌ {ex}")
        return
    if kind == "id":
        await show_preview(msg, owner, channel_id, value, opts)
        return
    if not value:
        await msg.edit(content="No results for that. Try a MAL link instead.")
        return

    async def after_pick(mal_id: int) -> None:
        await show_preview(msg, owner, channel_id, mal_id, opts)

    await msg.edit(content="Which one did you mean?", view=SearchView(owner, value, after_pick))


def allowed(user_id: int) -> bool:
    return user_id in ALLOWED


# ---------------------------------------------------------------- commands
@bot.tree.command(name="anime", description="Download from a MAL/AniList link, or search by title")
@app_commands.describe(
    query="MyAnimeList / AniList link, or a title to search for",
    quality="Video quality (default from config)",
    dub="Download the English dub instead of the sub",
    pick="Which ani-cli search result to use if it grabs the wrong show (default 1)",
    search="Override the search text used on the download source (single entry only)",
    verify="Check the source returned the right show before saving (default on; turn off to force)",
    exact="Only accept a source result whose title matches the search text exactly",
)
@app_commands.choices(quality=[app_commands.Choice(name=q, value=q) for q in ("best", "1080", "720", "480")])
async def anime_cmd(
    interaction: discord.Interaction,
    query: str,
    quality: Optional[app_commands.Choice[str]] = None,
    dub: Optional[bool] = None,
    pick: app_commands.Range[int, 1, 10] = 1,
    search: Optional[str] = None,
    verify: Optional[bool] = None,
    exact: Optional[bool] = None,
) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ You're not allowed to use this bot.", ephemeral=True)
        return
    opts = Opts(quality=quality.value if quality else DEFAULT_QUALITY,
                dub=DEFAULT_DUB if dub is None else dub, pick=pick, search=search,
                verify=DEFAULT_VERIFY if verify is None else verify,
                exact=DEFAULT_EXACT if exact is None else exact)
    await interaction.response.defer()
    msg = await interaction.followup.send("🔎 Looking it up…", wait=True)
    await handle_query(msg, interaction.user.id, interaction.channel_id, query, opts)


@bot.tree.command(name="status", description="Show the current download and the queue")
async def status_cmd(interaction: discord.Interaction) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Not allowed.", ephemeral=True)
        return
    if not current and not pending:
        await interaction.response.send_message("Idle - nothing running or queued.", ephemeral=True)
        return
    embeds = [render_job(current)] if current else []
    text = ""
    if pending:
        text = "**Queued:** " + ", ".join(f"#{j.id} {j.title} ({len(j.items)})" for j in pending)
    await interaction.response.send_message(content=text or None, embeds=embeds, ephemeral=True)


@bot.tree.command(name="cancel", description="Stop the running download (partial files are kept for resuming)")
@app_commands.describe(clear_queue="Also drop everything waiting in the queue")
async def cancel_cmd(interaction: discord.Interaction, clear_queue: bool = False) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Not allowed.", ephemeral=True)
        return
    notes = []
    if current:
        current.cancelled = True
        if current.proc:
            try:
                os.killpg(current.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        notes.append(f"Stopping job #{current.id}.")
    if clear_queue and pending:
        for j in list(pending):
            j.cancelled = True
        notes.append(f"Dropped {len(pending)} queued job(s).")
    persist_queue()
    await interaction.response.send_message(" ".join(notes) or "Nothing to cancel.", ephemeral=True)


@bot.tree.command(name="history", description="Recently finished downloads")
@app_commands.describe(count="How many to show (default 10)")
async def history_cmd(interaction: discord.Interaction, count: app_commands.Range[int, 1, 25] = 10) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Not allowed.", ephemeral=True)
        return
    recs = store.history(count)
    if not recs:
        await interaction.response.send_message("No finished jobs yet.", ephemeral=True)
        return
    lines = []
    for h in recs:
        counts: dict = {}
        for i in h["items"]:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        worst = next((s for s in ("failed", "partial", "cancelled", "done") if s in counts), "done")
        summary = " ".join(f"{ICONS[k]}{v}" for k, v in counts.items())
        lines.append(f"{ICONS[worst]} **#{h['id']}** {h['title']} · {summary} · <t:{h['finished']}:R>")
        for i in h["items"]:
            if i["status"] in ("failed", "partial") and i.get("note"):
                lines.append(f"　↳ {i['label']}: {i['note'][:90]}")
                break
    emb = discord.Embed(title="Recent jobs", description="\n".join(lines)[:4000], colour=discord.Colour.blurple())
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="library", description="Show seasons with missing episodes")
@app_commands.describe(title="Only series whose folder name contains this",
                       everything="Also list complete seasons")
async def library_cmd(interaction: discord.Interaction, title: Optional[str] = None, everything: bool = False) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Not allowed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    infos = await asyncio.to_thread(library.scan_series, cfg.anime_dir, store.expected(), title or "")
    await interaction.followup.send(embed=library_embed(infos, everything, title), ephemeral=True)


def library_embed(infos: list, everything: bool, title: Optional[str]) -> discord.Embed:
    series = {i.series for i in infos}
    episodes = sum(i.files for i in infos)
    bad = [i for i in infos if i.incomplete]
    show = infos if everything else bad
    lines = [library.season_line(i) for i in show]
    shown, used = [], 0
    for ln in lines:
        if used + len(ln) > 3700:
            shown.append(f"… and {len(lines) - len(shown)} more")
            break
        shown.append(ln)
        used += len(ln) + 1
    if not shown:
        shown = ["Nothing found." if not infos else "✅ No missing episodes found."]
    emb = discord.Embed(title="Library" + (f" · “{title}”" if title else ""), description="\n".join(shown),
                        colour=discord.Colour.orange() if bad else discord.Colour.green())
    emb.set_footer(text=f"{len(series)} series · {episodes} episodes · {len(bad)} incomplete season(s)\n"
                        "Gaps come from the numbering; counts from MAL are known for seasons the bot downloaded.\n"
                        "Fill them by running /anime with the series link.")
    return emb


class HealthView(discord.ui.View):
    def __init__(self, owner: int, issues: list):
        super().__init__(timeout=900)
        self.owner, self.issues = owner, issues
        self.fixable = [i for i in issues if i.action]
        self.fix_btn.label = f"Fix {len(self.fixable)} issue(s)"
        self.fix_btn.disabled = not self.fixable

    @discord.ui.button(label="Fix", style=discord.ButtonStyle.danger, emoji="🧹")
    async def fix_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.owner:
            await interaction.response.send_message("This one belongs to someone else.", ephemeral=True)
            return
        if current:
            await interaction.response.send_message("A download is running; try again when it finishes.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="🧹 Fixing…", embed=None, view=None)
        fixed, errors = await asyncio.to_thread(library.apply_fixes, cfg, self.fixable)
        text = f"🧹 Fixed {fixed} issue(s)."
        if errors:
            text += f"\n⚠️ {len(errors)} problem(s):\n" + "\n".join(errors[:5])
        text += "\nRe-run the downloads for anything that was quarantined; a library scan in Jellyfin may be needed."
        await interaction.edit_original_response(content=text[:1900])


@bot.tree.command(name="health", description="Scan the library for broken or leftover files")
@app_commands.describe(deep="Also test every video with ffprobe (slow on big libraries)")
async def health_cmd(interaction: discord.Interaction, deep: bool = False) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Not allowed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ffprobe = shutil.which("ffprobe")
    issues = await library.health_scan(cfg, ffprobe, deep=deep, busy=current is not None)
    emb = discord.Embed(title="Library health", colour=discord.Colour.orange() if issues else discord.Colour.green())
    if not issues:
        emb.description = "✅ No problems found."
    else:
        by_kind: dict = {}
        for i in issues:
            by_kind.setdefault(i.kind, []).append(i)
        for kind, lst in by_kind.items():
            rows = [f"`{i.path.name[:60]}` {i.detail}" if i.kind != "duplicate" else i.detail[:120] for i in lst[:4]]
            more = f"\n… and {len(lst) - 4} more" if len(lst) > 4 else ""
            emb.add_field(name=f"{kind} ({len(lst)})", value=("\n".join(rows) + more)[:1000], inline=False)
        emb.set_footer(text="Fix: deletes leftovers and empty folders, moves corrupt videos to .quarantine. "
                            "Duplicates are never touched.")
    notes = []
    if deep and not ffprobe:
        notes.append("ffprobe isn't installed, so the deep check was skipped.")
    if current:
        notes.append("A download is running, so scratch/staging cleanup was skipped.")
    view = HealthView(interaction.user.id, issues) if issues else None
    kwargs = {"view": view} if view else {}
    await interaction.followup.send(content="\n".join(notes) or None, embed=emb, ephemeral=True, **kwargs)


# ------------------------------------------------------- watch-channel mode
@bot.event
async def on_message(message: discord.Message) -> None:
    if not WATCH_CHANNEL_ID or message.channel.id != WATCH_CHANNEL_ID or message.author.bot:
        return
    if not allowed(message.author.id):
        return
    if not (MAL_RE.search(message.content) or ANILIST_RE.search(message.content)):
        return
    msg = await message.reply("🔎 Looking it up…", mention_author=False)
    await handle_query(msg, message.author.id, message.channel.id, message.content, Opts())


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s | allowed users: %d | watch channel: %s",
             bot.user, len(ALLOWED), WATCH_CHANNEL_ID or "off")
    while startup_notes:
        channel_id, text = startup_notes.pop(0)
        try:
            await bot.get_partial_messageable(channel_id).send(text)
        except discord.HTTPException:
            log.warning("couldn't post startup note to channel %s", channel_id)


if __name__ == "__main__":
    preflight()
    bot.run(TOKEN, log_handler=None)
