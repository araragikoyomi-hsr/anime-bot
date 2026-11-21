"""Small JSON store: the download queue (survives restarts), job history, expected episode counts."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from jobs import ItemResult, Job
from mal import Entry, Plan, PlanItem

log = logging.getLogger("anime-bot.store")
HISTORY_CAP = 200
_ENTRY_FIELDS = {f.name for f in dataclasses.fields(Entry)}


# ------------------------------------------------------- job <-> dict
def _entry_from(d: dict) -> Entry:
    return Entry(**{k: v for k, v in d.items() if k in _ENTRY_FIELDS})


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id, "owner": job.owner, "channel_id": job.channel_id,
        "quality": job.quality, "dub": job.dub, "pick": job.pick, "search": job.search,
        "verify": job.verify, "exact": job.exact, "attempts": job.attempts,
        "status_msg": [job.status_msg.channel.id, job.status_msg.id] if getattr(job.status_msg, "id", None) else None,
        "plan": {
            "focus_id": job.plan.focus_id, "truncated": job.plan.truncated, "mode": job.plan.mode,
            "items": [{"entry": dataclasses.asdict(p.entry), "kind": p.kind, "season": p.season,
                       "series": p.series, "year": p.year, "ep_offset": p.ep_offset}
                      for p in job.plan.items],
        },
        "item_ids": [it.entry.mal_id for it in job.items],
        "results": {str(k): {"status": r.status, "have": r.have, "want": r.want, "note": r.note}
                    for k, r in job.results.items()},
    }


def job_from_dict(d: dict) -> Job:
    items = [PlanItem(_entry_from(i["entry"]), i["kind"], i["season"], i["series"], i["year"], i.get("ep_offset", 0))
             for i in d["plan"]["items"]]
    plan = Plan(items=items, focus_id=d["plan"]["focus_id"], truncated=d["plan"]["truncated"], mode=d["plan"]["mode"])
    by_id = {p.entry.mal_id: p for p in items}
    job_items = [by_id[i] for i in d["item_ids"] if i in by_id]
    job = Job(id=d["id"], owner=d["owner"], channel_id=d["channel_id"], plan=plan, items=job_items,
              quality=d.get("quality", "best"), dub=d.get("dub", False), pick=d.get("pick", 1),
              search=d.get("search"), verify=d.get("verify", True), exact=d.get("exact", False),
              attempts=d.get("attempts", 0))
    for k, r in (d.get("results") or {}).items():
        res = job.results.setdefault(int(k), ItemResult())
        res.status = "queued" if r["status"] == "running" else r["status"]   # interrupted -> run again
        res.have, res.want, res.note = r.get("have", 0), r.get("want"), r.get("note", "")
    return job


def history_record(job: Job, labels: dict) -> dict:
    """labels: mal_id -> display label (built by the bot, which knows how to name items)."""
    return {
        "id": job.id, "owner": job.owner, "title": job.title, "finished": int(job.finished_at or time.time()),
        "started": int(job.started_at or 0), "quality": job.quality, "dub": job.dub,
        "items": [{"label": labels.get(it.entry.mal_id, it.entry.display), "name": it.entry.display,
                   "status": job.results[it.entry.mal_id].status, "have": job.results[it.entry.mal_id].have,
                   "want": job.results[it.entry.mal_id].want, "note": job.results[it.entry.mal_id].note}
                  for it in job.items],
    }


# ------------------------------------------------------------ the store
class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"queue": [], "history": [], "expected": {}}
        self._loaded = False

    def load(self) -> None:
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict):
                for k in self.data:
                    if isinstance(raw.get(k), type(self.data[k])):
                        self.data[k] = raw[k]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as ex:
            bak = self.path.with_suffix(".json.bad")
            log.warning("state file unreadable (%s); keeping a copy at %s and starting fresh", ex, bak)
            try:
                os.replace(self.path, bak)
            except OSError:
                pass

    def save(self) -> None:
        if not self._loaded:
            self.load()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)           # atomic: a crash can't leave half a file
        except OSError:
            log.exception("couldn't save state")

    # queue
    def get_queue(self) -> list:
        if not self._loaded:
            self.load()
        return list(self.data["queue"])

    def set_queue(self, jobs: list) -> None:
        if not self._loaded:
            self.load()
        self.data["queue"] = [job_to_dict(j) for j in jobs]
        self.save()

    # history
    def add_history(self, rec: dict) -> None:
        if not self._loaded:
            self.load()
        self.data["history"].append(rec)
        del self.data["history"][:-HISTORY_CAP]
        self.save()

    def history(self, n: int = 10) -> list:
        if not self._loaded:
            self.load()
        return list(reversed(self.data["history"][-n:]))

    def max_job_id(self) -> int:
        if not self._loaded:
            self.load()
        ids = [h.get("id", 0) for h in self.data["history"]] + [q.get("id", 0) for q in self.data["queue"]]
        return max(ids, default=0)

    # expected episode counts (for the /library report)
    def set_expected(self, key: str, want: Optional[int], mal_id: int) -> None:
        if not self._loaded:
            self.load()
        if want and self.data["expected"].get(key, {}).get("want") != want:
            self.data["expected"][key] = {"want": want, "mal_id": mal_id}
            self.save()

    def expected(self) -> dict:
        if not self._loaded:
            self.load()
        return dict(self.data["expected"])
