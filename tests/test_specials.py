import asyncio, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs
from mal import Entry, build_plan, parse_entry, titles_match, Tenrai

FAKE = str(Path(__file__).with_name("fake_anime_dl.py"))


def E(mid, title, typ="TV", eps=12, year=2010, aired=None, status="Finished Airing", en=None, **kw):
    return Entry(mal_id=mid, title=title, title_en=en, type=typ, episodes=eps, year=year,
                 aired_from=aired or f"{year}-01-01", status=status, **kw)


# ---------------------------------------------------------------- matching
def test_type_synonyms_match():
    assert titles_match("Show OAD", ["Show OVA"])
    assert titles_match("Show Specials", ["Show Special"])
    assert not titles_match("Show", ["Show OVA"])          # the TV series must not pass for its OVA


# -------------------------------------------------------------------- plan
def test_specials_attach_to_series_and_number_after_each_other():
    s1 = E(1, "Show", year=2010)
    ova1 = E(2, "Show OVA", "OVA", 2, 2011, parent_id=1)
    s2 = E(3, "Show 2nd Season", year=2012)
    sp = E(4, "Show 2nd Season: Extra", "Special", 1, 2013, parent_id=3)
    ova2 = E(5, "Show: Another OVA", "OVA", 3, 2014)       # no parent: matched by title prefix
    plan = build_plan([s1, ova1, s2, sp, ova2], 1)
    kinds = {p.entry.mal_id: p for p in plan.items}
    assert [kinds[i].kind for i in (1, 2, 3, 4, 5)] == ["season", "special", "season", "special", "special"]
    assert {kinds[i].series for i in (1, 2, 3, 4, 5)} == {"Show"}
    assert [kinds[i].ep_offset for i in (2, 4, 5)] == [0, 2, 3]       # S00E01-02, E03, E04-06
    assert [p.entry.mal_id for p in plan.selectable][-3:] == [2, 4, 5]  # specials sort last


def test_ova_only_franchise_becomes_seasons():
    plan = build_plan([E(1, "Hell OVA", "OVA", 2, 2006), E(2, "Hell OVA 2", "OVA", 3, 2008)], 1)
    assert [(p.kind, p.season, p.series) for p in plan.items] == [("season", 1, "Hell OVA"), ("season", 2, "Hell OVA")]


def test_unaired_and_music_still_skipped():
    plan = build_plan([E(1, "Show"), E(2, "Show OVA", "OVA", status="Not yet aired"), E(3, "Song", "Music")], 1)
    assert [p.kind for p in plan.items] == ["season", "skip", "skip"]


# ------------------------------------------------------------- franchise walk
def test_franchise_finds_side_story_ova_and_parent_walk():
    raw = {
        1: {"mal_id": 1, "title": "Show", "type": "TV", "episodes": 12, "status": "Finished Airing",
            "aired": {"from": "2010-01-01"},
            "relations": [{"relation": "Side story", "entry": [{"mal_id": 2, "type": "anime"}]},
                          {"relation": "Summary", "entry": [{"mal_id": 9, "type": "anime"}]}]},
        2: {"mal_id": 2, "title": "Show OVA", "type": "OVA", "episodes": 1, "status": "Finished Airing",
            "aired": {"from": "2011-01-01"},
            "relations": [{"relation": "Parent story", "entry": [{"mal_id": 1, "type": "anime"}]}]},
    }

    class Fake(Tenrai):
        def __init__(self): super().__init__(None)
        async def _get(self, path, params=None):
            return {"data": raw[int(path.split("/")[2])]}

    async def run():
        t = Fake()
        ents, _ = await t.franchise(1)
        assert [(e.mal_id, e.parent_id) for e in ents] == [(1, None), (2, 1)]      # recap (9) ignored
        ents2, _ = await Fake().franchise(2)                                       # start from the OVA
        assert {e.mal_id for e in ents2} == {1, 2}
        assert next(e for e in ents2 if e.mal_id == 2).parent_id == 1
    asyncio.run(run())


# ------------------------------------------------------- download end-to-end
def make_cfg(tmp):
    return jobs.Config(anime_dl=FAKE, anime_dir=tmp / "anime", movies_dir=tmp / "movies", work_dir=tmp / "work")


def run_item(cfg, plan, item, **kw):
    job = jobs.Job(id=1, owner=1, channel_id=1, plan=plan, items=[item], **kw)
    asyncio.run(jobs.download_item(cfg, job, item))
    return job.results[item.entry.mal_id]


def setup_env(monkeypatch, **env):
    for k in ("FAKE_TITLE", "FAKE_GOOD_PICK", "FAKE_NO_RESULT", "FAKE_MAX", "FAKE_LOG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))


def test_special_downloads_and_files_as_s00(tmp_path, monkeypatch):
    setup_env(monkeypatch, FAKE_TITLE="Show OVA", FAKE_MAX=2)
    cfg = make_cfg(tmp_path)
    s1, ova = E(1, "Show"), E(2, "Show OVA", "OVA", 2, 2011, parent_id=1)
    plan = build_plan([s1, ova], 1)
    item = plan.items[1]
    r = run_item(cfg, plan, item)
    d = cfg.anime_dir / "Show (2010)" / "Specials"
    names = sorted(p.name for p in d.iterdir())
    assert r.status == "done" and (r.have, r.want) == (2, 2), (r, names)
    assert names == [
        "Show - S00E01 - Show OVA Ep 1 [mal-2-e1].en.vtt", "Show - S00E01 - Show OVA Ep 1 [mal-2-e1].mp4",
        "Show - S00E02 - Show OVA Ep 2 [mal-2-e2].en.vtt", "Show - S00E02 - Show OVA Ep 2 [mal-2-e2].mp4"]
    assert jobs.library_state(cfg, item) == (2, 2)
    # idempotent: second run is a no-op and doesn't call the downloader
    r2 = run_item(cfg, plan, item)
    assert r2.status == "done" and r2.note == "already in library"


def test_second_special_numbers_after_first_and_collisions_shift(tmp_path, monkeypatch):
    setup_env(monkeypatch, FAKE_TITLE="Show Special", FAKE_MAX=1)
    cfg = make_cfg(tmp_path)
    d = cfg.anime_dir / "Show (2010)" / "Specials"
    d.mkdir(parents=True)
    (d / "Show - S00E01.mkv").write_bytes(b"manual")           # a file the user added by hand
    plan = build_plan([E(1, "Show"), E(2, "Show Special", "Special", 1, 2011, parent_id=1)], 1)
    r = run_item(cfg, plan, plan.items[1])
    assert r.status == "done"
    assert any(p.name.startswith("Show - S00E02 - Show Special") for p in d.iterdir())   # slot 1 was taken


def test_wrong_pick_is_retried_with_next_result(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    setup_env(monkeypatch, FAKE_TITLE="Show OVA", FAKE_GOOD_PICK=2, FAKE_MAX=1, FAKE_LOG=log)
    cfg = make_cfg(tmp_path)
    plan = build_plan([E(1, "Show"), E(2, "Show OVA", "OVA", 1, 2011, parent_id=1)], 1)
    r = run_item(cfg, plan, plan.items[1], pick=1)
    assert r.status == "done", r
    assert "-S 1" in log.read_text().splitlines()[0] and "-S 2" in log.read_text().splitlines()[1]


def test_only_wrong_show_fails_with_clear_note(tmp_path, monkeypatch):
    setup_env(monkeypatch, FAKE_TITLE="Show OVA", FAKE_GOOD_PICK=9, FAKE_MAX=1)
    cfg = make_cfg(tmp_path)
    plan = build_plan([E(1, "Show"), E(2, "Show OVA", "OVA", 1, 2011, parent_id=1)], 1)
    r = run_item(cfg, plan, plan.items[1])
    assert r.status == "failed" and "wrong show" in r.note
    assert not (cfg.anime_dir / "Show (2010)" / "Specials").exists()


def test_no_results_fails_cheaply(tmp_path, monkeypatch):
    setup_env(monkeypatch, FAKE_NO_RESULT=1)
    cfg = make_cfg(tmp_path)
    plan = build_plan([E(1, "Show"), E(2, "Show OVA", "OVA", 1, 2011, parent_id=1)], 1)
    r = run_item(cfg, plan, plan.items[1])
    assert r.status == "failed" and "no results" in r.note.lower()


def test_search_terms_add_latin_synonyms_only():
    e = E(1, "Shingeki no Kyojin", en="Attack on Titan", titles=["進撃の巨人", "AoT", "Attack on Titan", "SnK"])
    assert jobs.search_terms(e) == ["Attack on Titan", "Shingeki no Kyojin", "AoT"]
