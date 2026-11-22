import json
from pathlib import Path

import jobs
from mal import Entry, build_plan
from store import Store, history_record, job_from_dict, job_to_dict


def E(mid, title, typ="TV", eps=12, year=2010, **kw):
    return Entry(mal_id=mid, title=title, title_en=None, type=typ, episodes=eps, year=year,
                 aired_from=f"{year}-01-01", status="Finished Airing", **kw)


def make_job():
    plan = build_plan([E(1, "Show"), E(2, "Show OVA", "OVA", 2, 2011, parent_id=1, relations=[("Sequel", 3)])], 1)
    job = jobs.Job(id=7, owner=42, channel_id=9, plan=plan, items=plan.items, quality="720", dub=True,
                   pick=2, search=None, verify=False, exact=True, attempts=1)
    job.results[1].status, job.results[1].have = "done", 12
    job.results[2].status = "running"
    return job


def test_job_roundtrip_through_json():
    job = make_job()
    back = job_from_dict(json.loads(json.dumps(job_to_dict(job))))
    assert (back.id, back.owner, back.quality, back.dub, back.pick, back.verify, back.exact, back.attempts) == \
           (7, 42, "720", True, 2, False, True, 1)
    assert [p.kind for p in back.items] == ["season", "special"]
    assert back.items[1].entry.parent_id == 1 and back.items[1].ep_offset == 0
    assert back.results[1].status == "done" and back.results[1].have == 12
    assert back.results[2].status == "queued"          # was running when the bot died -> run again


def test_queue_survives_a_new_store_instance(tmp_path):
    path = tmp_path / "state.json"
    s = Store(path)
    s.set_queue([make_job()])
    again = Store(path)
    assert [d["id"] for d in again.get_queue()] == [7]
    assert again.max_job_id() == 7


def test_history_is_capped_and_newest_first(tmp_path):
    s = Store(tmp_path / "state.json")
    job = make_job()
    for i in range(205):
        rec = history_record(job, {})
        rec["id"] = i
        s.add_history(rec)
    assert len(s.data["history"]) == 200
    assert [h["id"] for h in s.history(3)] == [204, 203, 202]


def test_corrupt_state_file_is_kept_and_replaced(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    s = Store(path)
    assert s.get_queue() == []
    assert (tmp_path / "state.json.bad").read_text() == "{not json"
    s.add_history(history_record(make_job(), {}))
    assert json.loads(path.read_text())["history"]


def test_expected_counts_only_written_on_change(tmp_path):
    s = Store(tmp_path / "state.json")
    s.set_expected("Show (2010)/Season 01", 12, 1)
    mtime = (tmp_path / "state.json").stat().st_mtime_ns
    s.set_expected("Show (2010)/Season 01", 12, 1)
    assert (tmp_path / "state.json").stat().st_mtime_ns == mtime
    assert s.expected()["Show (2010)/Season 01"]["want"] == 12


def test_activity_parsing():
    a = jobs.Activity()
    a.feed("[12:00:01] Episode 4 (attempt 2/3)")
    a.feed("[download]  45.3% of 120MiB")
    assert a.render() == "ep 4 (try 2/3) · 45%"
    a.feed("[12:05:00] Organized: Show - S01E04")
    assert a.render() == "ep 4 (try 2/3)"
    a.feed("[12:05:01] Downloading from episode 5 onward (attempt 1/3)")
    assert a.render() == "from ep 5"
