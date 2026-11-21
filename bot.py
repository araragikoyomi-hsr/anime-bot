"""Discord front-end: share a MAL/AniList link (or a title), pick what to grab, done.

Commands:  /anime  /status  /cancel
Optional:  post a link in WATCH_CHANNEL_ID and the bot picks it up with no command.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands

import jobs
from jobs import Config, ItemResult, Job
from mal import ANILIST_RE, MAL_RE, Tenrai, MalError, Plan, build_plan

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
cfg = Config.from_env()


def preflight() -> None:
    problems = []
    if not TOKEN:
        problems.append("DISCORD_TOKEN is not set")
    if not ALLOWED:
        problems.append("ALLOWED_USER_IDS is empty (refusing to run an open download bot)")
    if not (os.path.isfile(cfg.anime_dl) and os.access(cfg.anime_dl, os.X_OK)):
        problems.append(f"anime-dl not found or not executable at {cfg.anime_dl}")
    if problems:
        sys.exit("Startup problems:\n  - " + "\n  - ".join(problems))
    cfg.work_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Opts:
    quality: str = DEFAULT_QUALITY
    dub: bool = DEFAULT_DUB
    pick: int = 1
    search: Optional[str] = None
    verify: bool = DEFAULT_VERIFY


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

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "anime-bot/1.0"})
        self.jikan = Tenrai(self.http_session)
        self.worker_task = asyncio.create_task(worker())
        if GUILD_ID:                            # guild sync is instant; global takes up to an hour
            guild = discord.Object(GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await super().close()
        await self.http_session.close()


bot = AnimeBot()

# ---------------------------------------------------------------- job queue
queue: "asyncio.Queue[Job]" = asyncio.Queue()
pending: list = []
current: Optional[Job] = None
_job_counter = 0

ICONS = {"queued": "⏳", "running": "⬇️", "done": "✅", "partial": "⚠️", "failed": "❌", "cancelled": "🛑"}


def item_label(it, plan: Plan) -> str:
    if it.kind == "movie":
        return "Movie"
    multi = len({p.series for p in plan.items if p.kind in ("season", "special")}) > 1
    if it.kind == "special":
        label = it.entry.type or "Special"
        return f"{it.series} {label}" if multi else label
    return f"{it.series} S{it.season:02d}" if multi else f"S{it.season:02d}"


def render_job(job: Job) -> discord.Embed:
    lines = []
    for it in job.items:
        r: ItemResult = job.results[it.entry.mal_id]
        count = ""
        if it.kind in ("season", "special") and (r.have or r.want):
            count = f" · {r.have}/{r.want or '?'}"
        note = f" — {r.note}" if r.note else ""
        lines.append(f"{ICONS[r.status]} **{item_label(it, job.plan)}** {it.entry.display}{count}{note}")
    state = "running" if job is current else ("queued" if job in pending else "finished")
    emb = discord.Embed(title=job.title, description="\n".join(lines)[:4000], colour=discord.Colour.blurple())
    emb.set_footer(text=f"Job #{job.id} · {state} · {job.quality} · {'dub' if job.dub else 'sub'}")
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
    return len(pending) + (1 if current else 0)


async def ticker(job: Job) -> None:
    """Refresh the episode counter while a season is downloading."""
    while True:
        await asyncio.sleep(45)
        changed = False
        for it in job.items:
            r = job.results[it.entry.mal_id]
            if r.status == "running" and it.kind == "season":
                have = jobs.count_videos(jobs.season_dir(cfg, it))
                if have != r.have:
                    r.have, changed = have, True
        if changed:
            await update_status(job)


async def run_job(job: Job) -> None:
    tick = asyncio.create_task(ticker(job))
    try:
        for it in job.items:
            r = job.results[it.entry.mal_id]
            if job.cancelled:
                r.status = "cancelled"
                continue
            r.status = "running"
            await update_status(job)
            try:
                await jobs.download_item(cfg, job, it)
            except Exception as ex:                       # keep the queue alive no matter what
                log.exception("download failed")
                r.status, r.note = "failed", str(ex)[:100]
            await update_status(job)
    finally:
        tick.cancel()

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
    if counts.get("failed") or counts.get("partial"):
        msg += "\nRe-run the same link to resume - finished episodes are skipped and partials continue."
        details = []
        for it in job.items:
            r = job.results[it.entry.mal_id]
            if r.status in ("failed", "partial") and r.tail:
                body = "\n".join(r.tail[-5:]).replace("```", "'''")[:350]
                details.append(f"**{item_label(it, job.plan)}** - {r.note}\n```\n{body}\n```")
        if details:
            msg += "\n" + "\n".join(details)[:1300]
    channel = bot.get_partial_messageable(job.channel_id)
    await channel.send(msg[:1990], allowed_mentions=discord.AllowedMentions(users=[discord.Object(job.owner)]))


async def worker() -> None:
    global current
    while True:
        job = await queue.get()
        if job in pending:
            pending.remove(job)
        current = job
        try:
            await run_job(job)
        except Exception:
            log.exception("job crashed")
        finally:
            current = None
            await update_status(job)
            queue.task_done()


# ---------------------------------------------------------------------- UI
def prepare(entries: list, truncated: bool, focus_id: int, mode: str):
    """Build the plan, look at the library, and pick the default selection:
    the linked entry and everything after it, minus what you already have."""
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
        if focus_is_special:                 # they linked one OVA/special: just that one
            if p.entry.mal_id == focus_id:
                selected.add(p.entry.mal_id)
        elif reached and p.kind != "special":   # other specials are opt-in via the button
            selected.add(p.entry.mal_id)
    return plan, states, selected


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
    emb.set_footer(text=f"{opts.quality} · {'dub' if opts.dub else 'sub'} · layout: {layout}\n"
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
        await interaction.response.defer()

    @discord.ui.button(label="Download", style=discord.ButtonStyle.success, emoji="⬇️", row=1)
    async def start_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        global _job_counter
        if not await self._owner_only(interaction):
            return
        items = [p for p in self.plan.items if p.entry.mal_id in self.selected]
        if not items:
            await interaction.response.send_message("Pick at least one entry first.", ephemeral=True)
            return
        _job_counter += 1
        job = Job(id=_job_counter, owner=self.owner, channel_id=self.channel_id, plan=self.plan,
                  items=items, quality=self.opts.quality, dub=self.opts.dub,
                  pick=self.opts.pick, search=self.opts.search, verify=self.opts.verify)
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
    try:
        entries, truncated = await bot.jikan.franchise(mal_id)
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
) -> None:
    if not allowed(interaction.user.id):
        await interaction.response.send_message("⛔ You're not allowed to use this bot.", ephemeral=True)
        return
    opts = Opts(quality=quality.value if quality else DEFAULT_QUALITY,
                dub=DEFAULT_DUB if dub is None else dub, pick=pick, search=search,
                verify=DEFAULT_VERIFY if verify is None else verify)
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
    await interaction.response.send_message(" ".join(notes) or "Nothing to cancel.", ephemeral=True)


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


if __name__ == "__main__":
    preflight()
    bot.run(TOKEN, log_handler=None)
