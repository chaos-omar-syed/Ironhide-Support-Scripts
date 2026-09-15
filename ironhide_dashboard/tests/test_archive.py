"""SAVE TO ARCHIVE (ih.archive) against the fake mongo: the quickdump layout (mavlink/<id>.csv · tracks/track_<id>.csv ·
obs/obs.csv · meta.json) with the new truth-match columns, BOTH block-143 layouts landing, indexed full_sec chunked queries,
the flights.json entry (8/28 schema + dir / label / source, flight number assigned), the saved flight REPLAYING through
ih.data.archive_bundle / ih.feed.archive_snapshot with track_meta populated, the background job API with progress, and the
quickdump.py CLI (thin wrapper) producing the same layout.  Velocity-state plumbing: the sigvE/sigvN/sigvU_mps columns
round-trip through the loader ((n,17) tid-prefixed, (m,16) per track) and an OLD archive without them loads as NaN.

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_archive.py
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time

import numpy as np
import polars as pl
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ih  # noqa: E402,F401  (puts ih/vendor — corr_lib — on sys.path)

import fake_mongo as FM  # noqa: E402
from ih import archive as AR  # noqa: E402
from ih import data as D  # noqa: E402
from ih import feed as F  # noqa: E402

F1 = D.FLIGHT_WINDOWS[1]
W0, W1 = F1[0] + 60.0, F1[0] + 300.0          # a 4-minute window inside flight 1 (2 chunks of CHUNK_S + a remainder)


@pytest.fixture(scope="module")
def srv():
    return FM.FakeMongo(F1[0] - 60, F1[1] + 30)


@pytest.fixture
def fake(srv):
    restore = srv.install()
    yield srv
    restore()


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A throw-away ARCHIVE_ROOT (the 8/28 flights.json stays where it is; saved flights go under tmp)."""
    monkeypatch.setattr(D, "ARCHIVE_ROOT", str(tmp_path))
    D.clear_flight_caches()
    yield str(tmp_path)
    D.clear_flight_caches()
    for n in [k for k in D.FLIGHT_WINDOWS if k > 4]:
        D.FLIGHT_WINDOWS.pop(n, None)
        D.FLIGHT_DIRS.pop(n, None)


def _read(path: str) -> pl.DataFrame:
    return pl.read_csv(path, infer_schema_length=100000)


def _fake_docs(srv, bt: int, t0: float, t1: float) -> list[dict]:
    return [d for d in srv.colls[FM.RUN_COLL]._docs if d["block_type"] == bt and t0 <= d["time_spec_float"] <= t1]


# ── the dump itself ─────────────────────────────────────────────────────────
def test_save_archive_writes_quickdump_layout_with_truth_match_columns(fake, root):
    steps = []
    res = AR.save_archive("fake", 27017, "sensor_store", FM.RUN_COLL, W0, W1, label="Flight 1", mru=91, root=root,
                          progress=lambda f, m: steps.append((f, m)), roles=((), (), D.TGT_PATTERN_DEFAULT, D.ITC_PATTERN_DEFAULT))
    assert res["ok"] and res["n"] == 5 and res["run8"] == "e65bd4f9" and res["day"] == "2026-08-28"
    d = res["dir"]
    assert d == os.path.join(root, "2026-08-28", "e65bd4f9_Flight_1") and os.path.isdir(d)
    assert os.path.isdir(f"{d}/mavlink") and os.path.isdir(f"{d}/tracks") and os.path.isfile(f"{d}/obs/obs.csv") and os.path.isfile(f"{d}/meta.json")
    # progress: monotonic 0 -> 1, chunked messages for the three blocks
    fr = [f for f, _ in steps]
    assert fr[0] == 0.0 and fr[-1] == 1.0 and fr == sorted(fr) and any("tracks" in m for _, m in steps) and any("observations" in m for _, m in steps)
    # mavlink: the four archive feeds, quickdump columns, E/N/U about the antenna in the live view's frame
    mav = sorted(os.listdir(f"{d}/mavlink"))
    assert mav == ["mav14550_1_1.csv", "mav14551_2_0.csv", "mav14551_2_1.csv", "mav14551_2_34.csv"] or "mav14550_1_1.csv" in mav
    tg = _read(f"{d}/mavlink/mav14550_1_1.csv")
    assert tg.columns == AR.MAV_COLS
    ref = _read(f"{FM.ARCHIVE}/mavlink/mav14550_1_1.csv").filter((pl.col("t_epoch") >= W0) & (pl.col("t_epoch") <= W1))
    assert len(tg) == len(ref) and len(tg) > 100
    # the 8/28 dump used pymap3d (exact ENU); the live view / this writer use corr_lib.EnuFrame (equirectangular, 111 320 m/deg):
    # +0.1 % in E and +0.36 % in N at 33.75 deg (the meridional degree is ~110 919 m) -> 3-4 m at 1-3 km, exactly what the live path shows
    assert np.allclose(tg["E_m"].to_numpy(), ref["E_m"].to_numpy(), rtol=2e-3, atol=1.0) and np.allclose(tg["N_m"].to_numpy(), ref["N_m"].to_numpy(), rtol=5e-3, atol=1.0)
    assert np.abs(tg["E_m"].to_numpy() - ref["E_m"].to_numpy()).max() < 5.0 and np.abs(tg["N_m"].to_numpy() - ref["N_m"].to_numpy()).max() < 5.0
    assert np.allclose(tg["U_m_hae"].to_numpy(), ref["U_m_hae"].to_numpy(), atol=3.0)
    assert (tg["alt_ft_wire"].to_numpy() == ref["alt_ft_wire"].to_numpy()).all()               # the wire altitude is kept verbatim (feet MSL)
    # tracks: quickdump columns + the three new ones; every fake track row inside the window landed (both layouts)
    m = res["meta"]
    files = os.listdir(f"{d}/tracks")
    assert len(files) == m["tracks"] > 50
    want = sum(len(FM.FakeMongo.__dict__ and [p for p in (x["payload"] if isinstance(x["payload"], list) else [x["payload"]]) if isinstance(p, dict)
                    and (p.get("x_state") is not None or p.get("latitude_rad") is not None)]) for x in _fake_docs(fake, 143, W0, W1))
    assert m["track_rows"] == want and m["layouts"]["ned"] > 0 and m["layouts"]["ecef"] > 0 and m["layouts"]["ned"] + m["layouts"]["ecef"] == want
    t177 = _read(f"{d}/tracks/track_177.csv") if "track_177.csv" in files else _read(f"{d}/tracks/{files[0]}")
    assert t177.columns == AR.TRK_COLS
    # velocity 1σ columns (appended): the fake's SIGV on every state with a covariance (both layouts), blank where p_cov is missing
    assert AR.TRK_COLS[-3:] == ["sigvE_mps", "sigvN_mps", "sigvU_mps"]
    sv = t177.select([pl.col(c).cast(pl.Float64, strict=False) for c in AR.TRK_COLS[-3:]]).to_numpy().astype(float)
    have = np.isfinite(sv[:, 0])
    assert have.sum() > 0 and np.allclose(sv[have], FM.SIGV, atol=1e-3) and np.isnan(sv[~have]).all()
    assert (np.isfinite(t177["sigE_m"].cast(pl.Float64, strict=False).to_numpy().astype(float)) == have).all()   # blank exactly where the position σ is blank
    ref177 = _read(f"{FM.ARCHIVE}/tracks/track_177.csv").filter((pl.col("t_epoch") >= W0) & (pl.col("t_epoch") <= W1)) if "track_177.csv" in files else None
    if ref177 is not None:
        # the fake strips x_state from every 53rd row (a state-less row is skipped, not archived): count the fake's stateful rows
        n_fake = sum(1 for x in _fake_docs(fake, 143, W0, W1) for p in (x["payload"] if isinstance(x["payload"], list) else [x["payload"]])
                     if isinstance(p, dict) and p.get("track_id") == 177 and (p.get("x_state") is not None or p.get("latitude_rad") is not None))
        assert len(t177) == n_fake and len(ref177) - 12 <= len(t177) <= len(ref177)
        j = t177.join(ref177, on="t_epoch", suffix="_ref")
        assert np.allclose(j["E_m"].to_numpy(), j["E_m_ref"].to_numpy(), atol=0.05) and np.allclose(j["N_m"].to_numpy(), j["N_m_ref"].to_numpy(), atol=0.05)
        # the target-side track 177 carries a MAVLink truth match with a confidence, JSON contributors on every row
        tm = t177.filter(pl.col("truth_match_id").cast(pl.Utf8).fill_null("") != "")
        assert len(tm) > 0 and set(tm["truth_match_id"].unique().to_list()) <= {"mav14550_1_1", "mav14551_2_0"}
        assert tm["truth_match_conf"].cast(pl.Float64).min() >= 0.5 and tm["truth_match_conf"].cast(pl.Float64).max() <= 1.0
        c = json.loads(t177["contributors"][0])
        assert c[0]["tx_lla_ddm"] == list(FM.ANT) and c[0]["rx_lla_ddm"] == list(FM.ANT)
    # an ADS-B-matched track (id % 13 == 0) carries the callsign on every row
    adsb = [f for f in files if int(f[6:-4]) % FM.ADSB_TRACK_MOD == 0]
    if adsb:
        a = _read(f"{d}/tracks/{adsb[0]}")
        assert set(a["truth_match_id"].to_list()) == {FM.ADSB_CALLSIGN} and float(a["truth_match_conf"][0]) == pytest.approx(0.9)
    # obs: every fake observation in the window, rr = -amb_dop, bistatic pair, truth_target columns
    ob = _read(f"{d}/obs/obs.csv")
    assert ob.columns == AR.OBS_COLS
    n_obs = sum(1 for x in _fake_docs(fake, 103, W0, W1) for o in x["payload"]["observations"] if isinstance(o, dict))
    assert len(ob) == n_obs == m["obs_rows"] > 100
    with_dop = ob.filter(pl.col("rr_mps").is_not_null())
    assert len(with_dop) > 0 and np.allclose(with_dop["bi_rng_rate_mps"].to_numpy(), 2.0 * with_dop["rr_mps"].to_numpy(), atol=0.01)
    assert np.allclose(ob["bi_rng_m"].to_numpy(), 2.0 * ob["rng_m"].to_numpy(), atol=0.2)
    ids = set(ob["truth_target_id"].fill_null("").to_list())
    assert "mav14550_1_1" in ids and "DAL1556" in ids and "" in ids
    assert set(ob.filter(pl.col("truth_target_id").fill_null("") != "")["truth_match_type"].to_list()) == {"range_and_el"}
    # meta.json
    assert m["antenna"] == pytest.approx(list(FM.ANT)) and m["tx_lla"] is None and m["run_collection"] == FM.RUN_COLL and m["mru"] == 91
    assert m["window"] == [W0, W1] and m["roles"]["target"] == ["mav14550_1_1"] and set(m["roles"]["interceptor"]) == {"mav14551_2_0", "mav14551_2_1", "mav14551_2_34"}
    assert m["columns"] == {"mavlink": AR.MAV_COLS, "tracks": AR.TRK_COLS, "obs": AR.OBS_COLS} and m["mavlink_rows"] > 300
    # every query went through the indexed time key, never a float range
    for call in fake.colls[FM.RUN_COLL].calls:
        flt = call[1] or {}
        assert "time_spec_float" not in flt, call
        if F.TIME_KEY in flt:
            assert all(isinstance(v, int) for v in flt[F.TIME_KEY].values()), call


def test_flights_json_entry_and_registry_replay(fake, root):
    res = AR.save_archive("fake", 27017, "sensor_store", FM.RUN_COLL, W0, W1, label="Flight 1", mru=91, root=root)
    fj = os.path.join(root, "2026-08-28", "flights.json")
    with open(fj) as f:
        j = json.load(f)
    assert j["day"] == "2026-08-28" and [r["run"] for r in j["runs"]] == ["e65bd4f9"]
    fl = j["runs"][0]["flights"]
    assert len(fl) == 1
    e = fl[0]
    for k in ("n", "t0", "t1", "t0_pdt", "t1_pdt", "drone_ids", "airborne_minutes", "segments", "passes", "tracking", "issues", "dir", "label", "source"):
        assert k in e, k
    assert e["n"] == 5 and e["dir"] == res["dir"] and e["label"] == "Flight 1" and e["passes"] == [] and e["t0"] == W0 and e["t1"] == W1
    assert e["drone_ids"] == ["mav14550_1_1 (target)", "mav14551_2_* (interceptor)"] and e["source"]["mru"] == 91 and e["source"]["run"] == FM.RUN_COLL
    assert e["t0_pdt"].startswith("2026-08-28T07:2") and e["airborne_minutes"] == 4.0
    # the registry sees it: flight 5 replays through the archive path with truth, tracks AND track_meta
    D.clear_flight_caches()
    reg = D.refresh_registry()
    assert 5 in reg and D.FLIGHT_WINDOWS[5] == (W0, W1) and D.FLIGHT_DIRS[5] == res["dir"] and D.flight_dir(5) == res["dir"]
    assert D.flight_label(5) == "8/28 · Flight 1 · 07:20–07:24 · 0 passes" and D.flight_short(5) == "8/28 · Flight 1"
    assert D.passes(5) == [] and D.engagement_window(D.flight_info(5)) == (W0, W1)
    b = D.archive_bundle(5, ((), (), D.TGT_PATTERN_DEFAULT, D.ITC_PATTERN_DEFAULT))
    assert b["ant"] == pytest.approx(FM.ANT) and len(b["tgt"]) > 100 and len(b["itc"]) > 50 and b["n_tracks"] > 50
    assert {r["name"] for r in b["raw"]} >= {"mav14550_1_1", "mav14551_2_0"}
    tm = b["track_meta"]
    assert tm and all(set(v) == {"truth_match_id", "truth_match_conf", "contributors", "t"} for v in tm.values())
    matched = {k: v for k, v in tm.items() if v["truth_match_id"]}
    assert matched and any(v["truth_match_id"] == FM.ADSB_CALLSIGN for v in matched.values())
    assert all(isinstance(v["truth_match_conf"], float) for v in tm.values()) and all(v["contributors"] is None or json.loads(v["contributors"]) for v in tm.values())
    # velocity sigmas round-trip: (n,17) tid-prefixed, SIGV where the state had a covariance, NaN elsewhere; (m,16) per track
    assert b["tracks"].shape[1] == D.TK_W + 1 == 17
    sv = b["tracks"][:, 14:17]
    have = np.isfinite(sv[:, 0])
    assert have.sum() > 0 and (~have).sum() > 0 and np.allclose(sv[have], FM.SIGV, atol=1e-3) and np.isnan(sv[~have]).all()
    trk = D.slice_tracks(b["tracks"], W0, W1)
    assert trk and all(a.shape[1] == D.TK_W for a in trk.values())
    # a second save of the same window (same dir) keeps the flight number; a different label gets the next one
    res2 = AR.save_archive("fake", 27017, "sensor_store", FM.RUN_COLL, W0, W1, label="Flight 1", mru=91, root=root)
    assert res2["n"] == 5 and len(json.load(open(fj))["runs"][0]["flights"]) == 1
    D.clear_flight_caches()
    assert D.next_flight_label("2026-08-28", "e65bd4f9") == "Flight 2"
    res3 = AR.save_archive("fake", 27017, "sensor_store", FM.RUN_COLL, W0 + 30, W1 + 30, label="Flight 2", mru=91, root=root)
    assert res3["n"] == 6 and len(json.load(open(fj))["runs"][0]["flights"]) == 2
    D.clear_flight_caches()
    assert sorted(D.refresh_registry()) == [1, 2, 3, 4, 5, 6]


def test_old_archive_without_velocity_sigma_columns_loads_as_nan():
    """The 8/28 quickdumps predate sigv*_mps: the loader pads NaN so every track array keeps ONE width (16)."""
    with open(f"{FM.ARCHIVE}/tracks/track_177.csv") as f:
        cols = next(csv.reader(f))
    assert not set(AR.TRK_COLS[-3:]) & set(cols)
    b = D.archive_bundle(1, ((), (), D.TGT_PATTERN_DEFAULT, D.ITC_PATTERN_DEFAULT))
    assert b["tracks"].shape[1] == D.TK_W + 1 == 17 and len(b["tracks"]) > 1000
    assert np.isnan(b["tracks"][:, 14:17]).all() and np.isfinite(b["tracks"][:, 11:14]).mean() > 0.9     # velocities present, sigmas NaN
    trk = D.slice_tracks(b["tracks"], *D.FLIGHT_WINDOWS[1])
    assert trk and all(a.shape[1] == D.TK_W for a in trk.values()) and np.isnan(trk[177][:, 13:16]).all()


def test_archive_snapshot_of_saved_flight_matches_live_path_columns(fake, root):
    import streamlit as st

    res = AR.save_archive("fake", 27017, "sensor_store", FM.RUN_COLL, W0, W1, label="Flight 1", root=root)
    D.clear_flight_caches()
    D.refresh_registry()
    st.session_state.clear()
    D.init_state()
    st.session_state["flight"] = res["n"]
    snap = F.archive_snapshot(W0 + 120.0, 180.0)
    assert snap["ok"] and snap["tx_lla"] is None and snap["tx"] is None and snap["unit"] == D.UNIT
    assert snap["tgt"].shape[1] == 7 and len(snap["tgt"]) > 50 and snap["tracks"] and snap["obs"].shape[1] == D.OBS_COLS
    assert all(a.shape[1] == D.TK_W for a in snap["tracks"].values())                       # the saved flight replays 16-col tracks
    assert set(snap["obs_meta"]) == {"truth_target_id", "truth_match_type"} and len(snap["obs_meta"]["truth_target_id"]) == len(snap["obs"])
    assert snap["track_meta"] and all("truth_match_id" in v for v in snap["track_meta"].values())
    assert abs(snap["ant"][0] - FM.ANT[0]) < 1e-9


def test_tracks_only_run_saves_without_truth_and_records_tx(fake, root):
    res = AR.save_archive("fake", 27017, "sensor_store", FM.TRACKS_ONLY_COLL, W0, W0 + 90, label="Copper", root=root)
    m = res["meta"]
    assert m["mavlink_rows"] == 0 and m["mavlink_ids"] == [] and m["tracks"] > 0 and m["obs_rows"] > 0
    assert m["tx_lla"] is not None and m["tx_lla"][0] == pytest.approx(FM.ANT[0] + FM.TX_OFFSET_DEG) and m["roles"] == {"target": [], "interceptor": []}
    assert os.listdir(f"{res['dir']}/mavlink") == []
    e = json.load(open(os.path.join(root, "2026-08-28", "flights.json")))["runs"][0]["flights"][0]
    assert e["drone_ids"] == ["— (target)", "— (interceptor)"] and e["tracking"]["target"] is None


def test_empty_window_is_refused_and_preview_counts(fake, root):
    with pytest.raises(ValueError):
        AR.save_archive("fake", 27017, "sensor_store", FM.RUN_COLL, W0, W0 + 0.5, root=root)
    pv = AR.preview("fake", 27017, "sensor_store", FM.RUN_COLL, W0, W1)
    lo, hi = math.floor(W0), math.ceil(W1)                                                   # the indexed full_sec bounds (whole seconds)
    n_int = lambda bt: sum(1 for d in fake.colls[FM.RUN_COLL]._docs if d["block_type"] == bt and lo <= d["time_spec"]["full_sec"] <= hi)  # noqa: E731
    assert pv["ok"] and pv["n143"] == n_int(143) >= len(_fake_docs(fake, 143, W0, W1)) and pv["n103"] == n_int(103) and pv["n106"] > 0
    bad = AR.preview("10.255.255.1", 27017, "sensor_store", FM.RUN_COLL, W0, W1)
    assert not bad["ok"] and "ServerSelectionTimeoutError" in bad["err"]
    assert AR.run8("run_e65bd4f92a9a4d6aa883c5f9d896a6ca") == "e65bd4f9" and AR.run8("abcdef0123") == "abcdef01" and AR.safe_label(" Flight 1 / a ") == "Flight_1_a"
    assert AR.day_of(W0) == "2026-08-28" and len(AR._edges(0.0, 250.0, 120.0)) == 4 and AR._edges(0.0, 240.0, 120.0) == [0.0, 120.0, 240.0]


# ── background job API ──────────────────────────────────────────────────────
def test_background_job_reports_progress_then_done(fake, root):
    jid = AR.start_save(host="fake", port=27017, db="sensor_store", run=FM.RUN_COLL, t0=W0, t1=W0 + 90, label="Job", mru=91, root=root)
    j = AR.job(jid)
    assert j and j["state"] in ("running", "done") and j["params"]["label"] == "Job" and "progress" not in j["params"]
    t0 = time.time()
    while AR.job(jid)["state"] == "running" and time.time() - t0 < 120:
        time.sleep(0.05)
    j = AR.job(jid)
    assert j["state"] == "done", j
    assert j["frac"] == 1.0 and j["msg"] == "done" and j["result"]["ok"] and j["result"]["n"] == 5 and j["t_end"] >= j["t_start"]
    assert os.path.isfile(os.path.join(j["result"]["dir"], "meta.json")) and AR.job("nope") is None and not AR.running_jobs()
    bad = AR.start_save(host="10.255.255.1", port=27017, db="sensor_store", run=FM.RUN_COLL, t0=W0, t1=W0 + 90, root=root)
    t0 = time.time()
    while AR.job(bad)["state"] == "running" and time.time() - t0 < 30:
        time.sleep(0.05)
    assert AR.job(bad)["state"] == "error" and "ServerSelectionTimeoutError" in AR.job(bad)["error"]


# ── quickdump.py: the thin CLI over the same function ───────────────────────
def test_quickdump_cli_uses_the_same_writer(fake, root, capsys):
    import quickdump as Q

    assert Q.hhmm_to_epoch("2026-08-28", "0719") == D.hms_to_epoch("07:19:00")
    res = Q.main(["0721", "0724", "e65bd4f9_cli_test", "--day", "2026-08-28", "--host", "fake", "--run", FM.RUN_COLL, "--root", root])
    assert res["dir"] == os.path.join(root, "2026-08-28", "e65bd4f9_cli_test") and res["n"] is None          # the CLI does not register unless asked
    assert _read(f"{res['dir']}/tracks/{os.listdir(res['dir'] + '/tracks')[0]}").columns == AR.TRK_COLS
    out = capsys.readouterr().out
    assert "QUICKDUMP e65bd4f9_cli_test:" in out and "mavlink rows" in out and "obs rows" in out
    assert not os.path.exists(os.path.join(root, "2026-08-28", "flights.json"))
    res2 = Q.main(["0721", "0724", "e65bd4f9_cli_reg", "--day", "2026-08-28", "--host", "fake", "--run", FM.RUN_COLL, "--root", root, "--register", "--mru", "91"])
    assert res2["n"] == 5 and res2["meta"]["mru"] == 91 and os.path.exists(os.path.join(root, "2026-08-28", "flights.json"))


def test_resolver_and_run_labels():
    assert F.resolve_mru_host(91) == "10.191.28.205" and F.resolve_mru_host("46") == "10.146.28.205" and F.resolve_mru_host(39) == "10.139.28.205"
    assert F.resolve_mru_host(48) == "10.148.28.205" and F.resolve_mru_host(5) == "10.105.28.205"
    for bad in (0, 100, "x"):
        with pytest.raises((ValueError, TypeError)):
            F.resolve_mru_host(bad)
    assert F.live_host({"mru_number": 91, "live_custom_host": ""}) == "10.191.28.205"
    assert F.live_host({"mru_number": 91, "live_custom_host": " 100.77.96.104 "}) == "100.77.96.104"
    assert F.live_host({"mru_number": None, "live_custom_host": ""}) == "" and F.live_unit({"mru_number": 91}) == "MRU91"
    now = D.hms_to_epoch("12:00:00")
    r = {"name": "run_8e3fccebc21a4b1d846ba0b938286eba", "friendly": "Gold_Falcon", "start_t": D.hms_to_epoch("11:56:23"), "last_t": now - 3.1 * 3600}
    assert F.run_label(r, now) == "Gold_Falcon · 8e3fcceb · 11:56:23 · 3.1 h ago"
    assert F.run_label({"name": "run_abcdef0123", "friendly": "", "start_t": None, "last_t": None}, now) == "unnamed · abcdef01 · start — · no data"
