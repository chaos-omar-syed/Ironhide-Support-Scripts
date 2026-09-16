"""LIVE mongo path — exercised WITHOUT a radar through tests/fake_mongo.py (the 8/28 archive served as
mongo documents, dict-and-list payloads, ADS-B rows, missing p_cov, junk obs entries, frozen feeds, a
streaming "now" mode) and, when reachable, against the REAL MRU39 unit (10.139.28.205:27017, ADS-B only,
275 run collections, 56 kB block-106 documents).

Live-only code the archive replay hides, verified here: block-103 dict-vs-list parsing + non-dict entries,
amb_dop_ms -> monostatic opening-positive range rate (-amb_dop, spa's build_obs_df convention) reaching the
measurement-space quad, altitude FEET MSL -> m WGS-84 HAE before spa (never MSL), frozen-row and >250 m/s
teleport scrubs, the three interceptor feeds (mav14551_2_0 / _2_1 / _2_34) merged into ONE interceptor,
role_of('mavlink_14551') == interceptor, missing p_cov -> sigma NaN (never 0, never "perfect"), indexed
time windows (time_spec.full_sec) with MAVLINK-only truth queries, bounded first fetch + forward paging,
the data-anchored clock on a stale run, a run with no TRACKS document (no antenna origin) staying
CONNECTED, the probe capped to the newest 25 runs, OFFLINE within the 3 s timeout, and Live -> Archive ->
Live keeping one panel server / the same sid.  Data-source page (2026-09-10 rework): MRU number -> 10.1NN.28.205,
CONNECT auto-selects the newest run with data, the run selectbox labels, the LIVE INFO panel (feeds detected, counts,
antenna, TX), the MAVLink ROLE ASSIGNMENT (prefill, refused overlap, Swap, role_ids contract), SAVE TO ARCHIVE end to end
(background job -> flights.json -> "Switch to this flight" replays it), and the snapshot CONTRACT fields track_meta /
obs_meta / tx_lla / unit.

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_live_mongo.py
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sys
import time

import numpy as np
import pytest

os.environ.setdefault("IH_LIVE_PORT", "8912")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamlit.testing.v1 import AppTest  # noqa: E402

import fake_mongo as FM  # noqa: E402
from ih import archive as AR  # noqa: E402
from ih import data as D  # noqa: E402
from ih import engine as E  # noqa: E402
from ih import feed as F  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402

LIVE, SRC = (f"{ROOT}/views/{p}" for p in ("1_live.py", "3_data_source.py"))
F1_MID = D.hms_to_epoch("07:22:31")
T0, T1 = D.FLIGHT_WINDOWS[1][0] - 60, D.FLIGHT_WINDOWS[1][1] + 30
MRU39 = ("10.139.28.205", 27017, "sensor_store")
LEAK = re.compile(r">(nan|None|inf|-inf)<|\b(nan|None) (m|s|°|%)\b|\bnan\b")
FIG_KEYS = tuple(LS.FIG_KEYS)          # whatever the panel server declares (owner A may add / fold figures)
MATRIX: list[tuple[str, str]] = []


def _mru39_up() -> bool:
    try:
        with socket.create_connection((MRU39[0], MRU39[1]), timeout=2.0):
            return True
    except OSError:
        return False


MRU39_UP = _mru39_up()
needs_mru39 = pytest.mark.skipif(not MRU39_UP, reason="MRU39 mongo 10.139.28.205:27017 unreachable")


def _at(page: str, **state) -> AppTest:
    at = AppTest.from_file(page, default_timeout=240)
    for k, v in D.STATE_DEFAULTS.items():
        at.session_state[k] = v
    if "text_scale" not in at.session_state:
        at.session_state["text_scale"] = "Normal"   # tests render at Normal text unless they set it
    at.session_state["show_sat"] = False
    at.session_state["meas_open"] = True   # the measurement-space quad is collapsed by default (A15): the figure rows want it built + pushed
    for k, v in state.items():
        at.session_state[k] = v
    return at


def _md(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def _exc(at) -> list[str]:
    return [f"{e.type}: {e.message}" for e in at.exception]


def _tiles(md: str):
    return re.findall(r'<div class="ih-tile ([a-z]*)"><div class="k">(.*?)</div><div class="v">(.*?)</div>(?:<div class="s">(.*?)</div>)?', md)


def _status(md: str) -> str:
    m = re.search(r'<div class="ih-status">(.*?)</div>', md)
    return re.sub(r"<[^>]+>", " ", m.group(1)) if m else ""


def _ss(at, k):
    try:
        return at.session_state[k]
    except KeyError:
        return None


def _live_state(coll: str, host: str = "fake", port: int = 27017, **extra) -> dict:
    return {"source": "live", "live_host": host, "live_port": port, "live_db": "sensor_store", "live_run": coll, "hist_s": 180, **extra}


def _no_leak(md: str) -> None:
    assert not LEAK.search(md), LEAK.search(md).group(0)


# ── fixtures: the fake built once, INSTALLED per test (the MRU39 tests must see the real pymongo) ──
@pytest.fixture(scope="module")
def srv():
    return FM.FakeMongo(T0, T1)


@pytest.fixture(scope="module")
def srv_now():
    """The archive as a live STREAM: the wall clock at build time == 07:22:31 (pass 2, still closing)."""
    return FM.FakeMongo(T0, T1, shift_to_now=F1_MID)


@pytest.fixture
def fake(srv):
    restore = srv.install()
    yield srv
    restore()


@pytest.fixture
def fake_now(srv_now):
    restore = srv_now.install()
    yield srv_now
    restore()


# ── the fake itself ──────────────────────────────────────────────────────────
def test_fake_query_engine_has_mongo_semantics(srv):
    c = srv.colls[FM.RUN_COLL]
    assert srv.n_docs["106"] > 900 and srv.n_docs["143"] > 900 and srv.n_docs["103"] > 500
    assert c.count_documents({"block_type": 143}) == srv.n_docs["143"]
    # dotted path into a LIST payload: any element matches (ADS-B-only docs never match MAVLINK)
    assert c.count_documents({"block_type": 106, "payload.source": "MAVLINK"}) == srv.n_docs["106"]
    assert srv.colls[FM.ADSB_ONLY_COLL].count_documents({"block_type": 106, "payload.source": "MAVLINK"}) == 0
    assert srv.colls[FM.ADSB_ONLY_COLL].count_documents({"block_type": 106}) > 0
    # indexed-style range on time_spec.full_sec + newest-first sort
    lo = int(F1_MID) - 10
    n = sum(1 for _ in c.find({"block_type": 143, "time_spec.full_sec": {"$gte": lo, "$lte": lo + 10}}))
    assert 5 <= n <= 40, n
    d = c.find_one({"block_type": 143}, sort=[("time_spec.full_sec", -1), ("time_spec.frac_sec", -1)], projection={"time_spec_float": 1})
    assert d["time_spec_float"] == max(x["time_spec_float"] for x in c._docs if x["block_type"] == 143)
    assert c.find_one({}, sort=[("$natural", -1)])["time_spec_float"] == c._docs[-1]["time_spec_float"]
    # projection applies per array element and keeps only the asked fields
    d = c.find_one({"block_type": 103}, projection=F.OBS_PROJECTION)
    o = [x for x in d["payload"]["observations"] if isinstance(x, dict)][0]
    assert set(o) <= {"az_rad", "el_rad", "amb_rng_km", "amb_dop_ms", "amb_bistatic_rng_km", "amb_bistatic_rng_rate_ms", "truth_target"} and "snr_db" not in o
    tts = [x["truth_target"] for dd in c.find({"block_type": 103}, projection=F.OBS_PROJECTION)
           for x in dd["payload"]["observations"] if isinstance(x, dict) and x.get("truth_target")]
    assert tts and all(set(tt) <= {"target_id", "match_type"} for tt in tts)                # nested projection: no lat/lon/hex/error_info shipped
    assert {tt["target_id"] for tt in tts} >= {"mav14550_1_1", "DAL1556"}
    both = {type(x["payload"]).__name__ for x in c._docs if x["block_type"] == 106}
    assert both == {"list", "dict"}                                                     # both payload shapes present
    assert any(not isinstance(x, dict) for d in c._docs if d["block_type"] == 103 for x in d["payload"]["observations"])


# ── row parsers: the live-only code ─────────────────────────────────────────
def test_truth_rows_dict_vs_list_adsb_ignored_and_hae_not_msl():
    import corr_lib as C

    frame = C.EnuFrame(FM.ANT)
    t = 1787926951.0
    mav = {"source": "MAVLINK", "target_id": "mavlink_14551", "lat": FM.ANT[0], "lon": FM.ANT[1], "altitude": 1000.0, "validposition": 1,
           "velocity_n_mps": 1.0, "velocity_e_mps": 2.0, "vertical_speed": 600.0}
    adsb = FM._adsb(t, 1)
    rows, n_other = F.truth_rows(t, {"payload": [adsb, mav, adsb, "junk"]}, frame)
    assert n_other == 2 and len(rows) == 1 and rows[0][0] == "mavlink_14551"
    r = rows[0][1]
    assert r[5] == pytest.approx(FM.hae_m(1000.0) - FM.ANT[2], abs=1e-9)             # U = HAE(ft MSL -> m) - antenna HAE, not MSL
    assert r[8] == 600.0 and r[6] == 2.0 and r[7] == 1.0 and r[9] == 1              # vert speed stays wire ft/min (merge_truth scales), (vE, vN) order
    rows_d, n_d = F.truth_rows(t, {"payload": mav}, frame)                          # dict payload
    assert rows_d[0][1] == r and n_d == 0
    assert F.truth_rows(t, {"payload": {"source": "MAVLINK", "lat": None, "lon": 1, "altitude": 1}}, frame) == ([], 0)
    assert D.role_of("mavlink_14551") == "interceptor" and D.role_of("mavlink_14550") == "target"
    assert D.role_of("mav14551_2_34") == "interceptor" and D.role_of("mav14550_1_1") == "target"


def test_track_rows_ned_to_enu_missing_pcov_nan_and_skips():
    t = 1787926951.0
    P = np.zeros((6, 6)); P[0, 0], P[1, 1], P[2, 2] = 4.0, 9.0, 16.0
    P[3, 3], P[4, 4], P[5, 5] = 0.6 ** 2, 0.8 ** 2, 1.5 ** 2                            # velocity block, NED order [vN, vE, vD]
    p_ok = {"track_id": 7, "track_state": 2, "x_state": [100.0, 200.0, -50.0, 1.0, 2.0, -3.0], "p_cov": P.tolist(),
            "last_update_time": {"full_sec": 1787926950, "frac_sec": 0.5}, "total_associations": 12}
    p_nocov = {**p_ok, "track_id": 8}; p_nocov.pop("p_cov")
    p_nostate = {"track_id": 9, "track_state": 2}
    rows = F.track_rows(t, {"payload": [p_ok, p_nocov, p_nostate, "junk", 3]})
    assert [r[0] for r in rows] == [7, 8]
    r = rows[0][1]
    assert (r[1], r[2], r[3]) == (200.0, 100.0, 50.0)                                 # (E, N, U) from NED [N, E, D]
    assert (r[4], r[5], r[6]) == (3.0, 2.0, 4.0)                                      # sigE = sqrt(P[1,1]), sigN = sqrt(P[0,0]), sigU = sqrt(P[2,2])
    assert (r[10], r[11], r[12]) == (2.0, 1.0, 3.0) and r[7] == 1787926950.5 and r[9] == 2.0
    assert len(r) == D.TK_W == 16 and np.allclose(r[13:16], (0.8, 0.6, 1.5), atol=1e-12)   # sigvE = sqrt(P[4,4]), sigvN = sqrt(P[3,3]), sigvU = sqrt(P[5,5])
    r8 = rows[1][1]
    assert all(np.isnan(v) for v in (r8[4], r8[5], r8[6])) and not any(v == 0 for v in (r8[4], r8[5], r8[6]))   # missing p_cov -> NaN, never 0
    assert len(r8) == 16 and all(np.isnan(v) for v in r8[13:16]) and not any(v == 0 for v in r8[13:16])        # ... velocity sigmas too
    assert F.track_rows(t, {"payload": p_ok})[0][0] == 7                                # dict payload


def test_track_rows_ecef_lla_layout_matches_the_ned_layout():
    """The 2026.9.x layout (x_state_ecef / p_cov_ecef / latitude_rad …, no x_state) must land on the same ENU
    row as the 2026.3.x NED layout for the same state — the fake writes both for the archive's tracks."""
    lat_r, lon_r, h = math.radians(FM.ANT[0]), math.radians(FM.ANT[1]), FM.ANT[2]
    R = F.enu_rotation(lat_r, lon_r)
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-12) and np.allclose(F.lla_to_ecef(0.0, 0.0, 0.0), [F.WGS_A, 0, 0])
    E, N, U, vE, vN, vU = 1500.0, -300.0, 120.0, 20.0, -5.0, 1.5
    ant_ecef = F.lla_to_ecef(lat_r, lon_r, h)
    import corr_lib as C
    frame = C.EnuFrame((FM.ANT[0], FM.ANT[1], h))
    Penu = np.diag([9.0, 4.0, 16.0])
    Pvenu = np.array([[0.64, 0.10, 0.00], [0.10, 0.36, 0.05], [0.00, 0.05, 2.25]])      # velocity block [vE, vN, vU], deliberately NOT diagonal
    Pe = np.zeros((6, 6)); Pe[:3, :3] = R.T @ Penu @ R; Pe[3:, 3:] = R.T @ Pvenu @ R
    _lat, _lon, _alt = frame.lla(E, N, U)
    p = {"track_id": 5, "track_state": 2, "version_id": 1.7, "latitude_rad": math.radians(_lat), "longitude_rad": math.radians(_lon),
         "altitude_m": _alt, "x_state_ecef": [*(ant_ecef + R.T @ np.array([E, N, U])), *(R.T @ np.array([vE, vN, vU]))], "p_cov_ecef": Pe.tolist(),
         "track_velocity_n": vN, "track_velocity_e": vE, "last_update_time": {"full_sec": 10, "frac_sec": 0.25}, "total_associations": 3,
         "antenna_location_origin_latitude_rad": lat_r, "antenna_location_origin_longitude_rad": lon_r, "antenna_location_origin_altitude_m": h}
    rows = F.track_rows(11.0, {"payload": [p]})
    assert len(rows) == 1 and rows[0][0] == 5
    r = rows[0][1]
    assert np.allclose(r[1:4], (E, N, U), atol=1e-6)                                        # position via the truth's EnuFrame
    assert np.allclose(r[4:7], (3.0, 2.0, 4.0), atol=1e-6)                                  # sigmas from the rotated ECEF covariance (E, N, U)
    assert np.allclose(r[10:13], (vE, vN, vU), atol=1e-9) and r[7] == 10.25 and r[9] == 2.0
    # velocity sigmas: rotate the ECEF velocity block back to ENU and compare = sqrt(diag(R · Pe[3:6,3:6] · R^T)) = the ENU diagonal
    assert len(r) == D.TK_W == 16
    assert np.allclose(r[13:16], np.sqrt(np.diag(R @ np.asarray(p["p_cov_ecef"])[3:, 3:] @ R.T)), atol=1e-9)
    assert np.allclose(r[13:16], (0.8, 0.6, 1.5), atol=1e-9)
    q = dict(p); q.pop("p_cov_ecef")
    r2 = F.track_rows(11.0, {"payload": q})[0][1]
    assert all(np.isnan(v) for v in r2[4:7])                                                # no covariance -> NaN, never 0
    assert all(np.isnan(v) for v in r2[13:16]) and len(r2) == 16                            # ... velocity sigmas too
    q2 = {k: v for k, v in p.items() if not k.startswith("antenna_location")}
    assert F.track_rows(11.0, {"payload": q2}) == [] and F.track_rows(11.0, {"payload": q2}, ant=FM.ANT)[0][1][1:4] == r[1:4]   # origin fallback


def test_obs_rows_list_dict_junk_and_range_rate_sign():
    t = 1787926951.0
    obs = [{"az_rad": 1.0, "el_rad": 0.1, "amb_rng_km": 1.5, "amb_dop_ms": 16.3}, "junk", 3.0, None,
           {"az_rad": 1.1, "amb_rng_km": 1.6}, {"el_rad": 0.2, "amb_rng_km": 1.6}]
    rows = F.obs_rows(t, {"payload": {"dwell_id": 1, "observations": obs}})
    assert len(rows) == 2 and len(rows[0]) == 9
    assert rows[0][:5] == (t, 1.0, 0.1, 1500.0, -16.3)                                # rr = -amb_dop (opening positive, monostatic)
    assert np.isnan(rows[0][5]) and np.isnan(rows[0][6]) and rows[0][7:] == (None, None)   # no bistatic fields / truth match -> NaN / None
    assert rows[1][2] == 0.0 and np.isnan(rows[1][4])                                 # el defaults 0, no doppler -> NaN
    r2 = F.obs_rows(t, {"payload": [{"observations": obs}, "x"]})[0]                  # list payload (NaN-safe compare)
    assert r2[:5] == rows[0][:5] and r2[7:] == rows[0][7:] and np.isnan(r2[5]) and np.isnan(r2[6])
    rich = {"az_rad": 1.0, "el_rad": 0.1, "amb_rng_km": 1.5, "amb_dop_ms": -4.0, "amb_bistatic_rng_km": 3.1, "amb_bistatic_rng_rate_ms": 8.2,
            "truth_target": {"target_id": "DAL1556", "match_type": "range_and_el", "source": "ADS-B"}}
    r = F.obs_rows(t, {"payload": {"observations": [rich, {**rich, "truth_target": None}, {**rich, "truth_target": {"target_id": ""}}]}})
    assert r[0][4:] == (4.0, 3100.0, 8.2, "DAL1556", "range_and_el") and r[1][7:] == (None, None) and r[2][7:] == (None, None)
    # track_meta: the radar's runtime truth match in the CONTRACT shape (conf NaN when absent, contributors JSON string)
    doc = {"payload": [{"track_id": 7, "truth_match": {"target_id": "CSG2537", "match_type": "plurality_based, confidence_1.00"},
                        "contributors": [{"tx_lla_ddm": [33.9, -118.3, -37.2], "rx_lla_ddm": [33.9, -118.3, -37.2]}]},
                       {"track_id": 8, "truth_match": None, "contributors": None}, {"track_id": 9, "truth_match": {"target_id": "", "match_type": "x"}}, "junk"]}
    tm = F.track_meta(doc)
    assert tm[7]["truth_match_id"] == "CSG2537" and tm[7]["truth_match_conf"] == 1.0 and json.loads(tm[7]["contributors"])[0]["tx_lla_ddm"] == [33.9, -118.3, -37.2]
    assert tm[8] == {"truth_match_id": None, "truth_match_conf": pytest.approx(float("nan"), nan_ok=True), "contributors": None}
    assert tm[9]["truth_match_id"] is None and np.isnan(tm[9]["truth_match_conf"])
    assert F.match_conf("plurality_based, confidence_0.87") == 0.87 and F.match_conf("range_and_el") is None
    assert F.match_role("mav14550_1_1") == "target" and F.match_role("mavlink_14551") == "interceptor" and F.match_role("N432R") == "other"
    assert F.match_role(None) is None and F.match_role("abc", tgt_ids=("abc",)) == "target"
    # tx_lla from contributors: distinct TX -> LLA, co-located -> None, JSON string accepted
    assert F.tx_from_contributors([{"tx_lla_ddm": [33.9164, -118.3320, -37.2], "rx_lla_ddm": [33.9155, -118.3317, -14.2]}]) == (33.9164, -118.3320, -37.2)
    assert F.tx_from_contributors([{"tx_lla_ddm": [33.9, -118.3, -37.2], "rx_lla_ddm": [33.9, -118.3, -37.2]}]) is None
    assert F.tx_from_contributors(json.dumps([{"tx_lla_ddm": [1.0, 2.0, 3.0]}])) == (1.0, 2.0, 3.0) and F.tx_from_contributors("nonsense") is None


def test_feed_scrubs_frozen_and_teleport_rows():
    import polars as pl

    t = np.arange(0, 20, 1.0)
    lat = 33.7 + 1e-5 * t; lon = -115.3 + 1e-5 * t
    lat[10:] = lat[9]; lon[10:] = lon[9]                                               # frozen from t=10
    raw = {"name": "mav14550_1_1", "role": "target", "t": t, "lat": lat, "lon": lon}
    assert D.feed_status(raw, 19.5)["state"] == "frozen" and D.feed_status(raw, 5.5)["state"] == "alive"
    assert D.feed_status(raw, 40.0)["state"] == "down" and D.feed_status(raw, 22.5)["state"] in ("stale", "frozen")
    df = pl.DataFrame({"t_epoch": t, "lat": lat, "lon": lon, "E_m": t * 10, "N_m": t * 0, "U_m_hae": t * 0, "vel_e_mps": t * 0 + 10, "vel_n_mps": t * 0,
                       "vert_spd_wire_ftmin": t * 0, "validposition": [1] * 20})
    c = D.clean_feed(df)
    assert len(c) == 11                                                               # stale republishes dropped: the 10 moving rows + the LAST row of the
    assert float(c["t_epoch"][-1]) == 19.0                                            # parked position (2026-09-15 ih.data.clean_feed: a parked vehicle must
    assert float(c["t_epoch"][-2]) == 9.0                                             # still have a current sample, not vanish from the window)
    E_ = np.array(list(range(10)), float) * 10.0
    E_[5] = 5000.0                                                                    # a 5 km teleport in 1 s
    df2 = pl.DataFrame({"t_epoch": t[:10], "lat": lat[:10], "lon": lon[:10], "E_m": E_, "N_m": t[:10] * 0, "U_m_hae": t[:10] * 0, "vel_e_mps": t[:10] * 0,
                        "vel_n_mps": t[:10] * 0, "vert_spd_wire_ftmin": t[:10] * 0 + 100.0, "validposition": [1] * 10})
    m = D.merge_truth([df2], -1, 100)
    assert len(m) == 9 and 5000.0 not in m[:, D.TR["E"]]                             # teleport row dropped
    assert np.allclose(m[:, D.TR["vU"]], 100.0 * D.FPM_TO_MPS)                       # ft/min -> m/s


# ── the Live page on the fake (stale run: anchored clock) ────────────────────
def test_live_full_run_anchored_pages_forward_merges_interceptor_feeds(fake):
    at = _at(LIVE, **_live_state(FM.RUN_COLL)).run()
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"] and snap["anchored"] is True and snap["data_age"] > 86400                 # a 13-day-old run
    newest143 = max(d["time_spec_float"] for d in fake.colls[FM.RUN_COLL]._docs if d["block_type"] == 143)
    assert snap["t_now"] == newest143                                                          # pinned to the newest TRACK document
    assert snap["has_truth"] and snap["n_adsb"] > 0                                            # ADS-B rows seen and ignored
    feeds = snap["feeds"]
    assert sorted(f["name"] for f in feeds if f["role"] == "interceptor") == ["mav14551_2_0", "mav14551_2_1", "mav14551_2_34"]
    assert [f["name"] for f in feeds if f["role"] == "target"] == ["mav14550_1_1"]
    itc = snap["itc"]
    assert len(itc) > 50 and np.all(np.diff(itc[:, 0]) >= 0.49)                                # ONE interceptor, 0.5 s dedup across the three feeds
    # both block-143 layouts parsed: every 3rd document is ECEF/LLA -> the row count matches the fake's rows in the window
    lo, hi = snap["t_now"] - 180.0, snap["t_now"]
    want = sum(len(FM._payloads(d) if hasattr(FM, "_payloads") else (d["payload"] if isinstance(d["payload"], list) else [d["payload"]]))
               for d in fake.colls[FM.RUN_COLL]._docs if d["block_type"] == 143 and lo <= d["time_spec_float"] <= hi)
    got = sum(len(a) for a in snap["tracks"].values())
    assert 0.95 * want <= got <= want, (want, got)                                             # (a few NED rows deliberately lack x_state)
    assert snap["obs"].shape[1] == D.OBS_COLS == 8 and np.isfinite(snap["obs"][:, 4]).mean() > 0.7   # obs carry range rate (+ bistatic, match code)
    md = _md(at)
    _no_leak(md)
    st = _status(md)
    assert "LIVE" in st and "last data" in st and "d  ago" in st.replace("</b>", " ")   # (trk/s left the row 2026-09-15: the target Hz is in the counter)
    assert set(LS.figs_of(at.session_state["_sid"])) == set(FIG_KEYS)
    # second tick: forward paging only — the windowed finds cover (hi - overlap, t_now], never the whole window again
    c = fake.colls[FM.RUN_COLL]
    n0 = len(c.calls)
    at.run()
    assert not _exc(at), _exc(at)
    finds = [q for kind, q, *_ in c.calls[n0:] if kind == "find" and q and "time_spec.full_sec" in q]
    assert finds, c.calls[n0:]
    for q in finds:
        assert q["time_spec.full_sec"]["$lte"] - q["time_spec.full_sec"]["$gte"] <= F.OVERLAP_S + 2
        assert "time_spec_float" not in q                                                      # never the unindexed field
    assert all("time_spec_float" not in (q or {}) for kind, q, *_ in c.calls if kind in ("find", "count"))
    mav_q = [q for kind, q, *_ in c.calls if kind == "find" and q and q.get("block_type") == 106]
    assert mav_q and all(q.get("payload.source") == "MAVLINK" for q in mav_q)                  # truth queries are MAVLINK-only
    MATRIX.append(("live fake · full run (stale, anchored)", "ok"))


def test_live_adsb_only_run_is_connected_not_offline(fake):
    at = _at(LIVE, **_live_state(FM.ADSB_ONLY_COLL)).run()
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"] and snap["ant"] is None and not snap["has_truth"] and len(snap["tracks"]) == 0
    md = _md(at)
    _no_leak(md)
    assert "OFFLINE" not in md and "CONNECTED" in md and "No MAVLink truth" in md
    tiles = _tiles(md)
    assert [t[2] for t in tiles] == ["NO TRUTH FEED", "—", "—"] and tiles[0][0] == "fail"   # target track tile first (ih.engine track_state, 2026-09-15:
    #                                                                                        no MAVLink truth at all is a FAIL state; a target-only gap is amber "NO TARGET FEED")
    assert "NO TRUTH FEED" in _status(md)   # (trk/s left the row 2026-09-15)
    err = LS.figs_of(at.session_state["_sid"])["err"]
    reads = [a["text"] for a in err["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
    assert reads == [PL.EMPTY_READOUT] * 4                                                     # four titled empty cards
    MATRIX.append(("live fake · ADS-B only run (no truth, no tracks)", "connected, empty state"))


def test_live_tracks_only_run_renders_grey_tracks_and_obs(fake):
    at = _at(LIVE, **_live_state(FM.TRACKS_ONLY_COLL)).run()
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"] and len(snap["tracks"]) > 5 and not snap["has_truth"]
    figs = LS.figs_of(at.session_state["_sid"])
    names = [t.get("name") for t in figs["map"]["data"]]
    assert names.count("radar tracks (no truth)") >= 5 and "radar track heads" in names
    grey = [t for t in figs["map"]["data"] if t.get("name") == "radar tracks (no truth)"]
    assert all(t["line"]["color"] == T.GREY_TRACK and t["line"]["dash"] == "dash" for t in grey) and sum(t.get("showlegend", True) for t in grey) == 1
    meas_names = {t.get("name") for t in figs["meas"]["data"]}
    assert any(n.endswith("· no truth") for n in meas_names) and "raw obs" in meas_names            # A7 labels: "#<tid> · no truth"
    assert any(t.get("name") == "raw obs" and t.get("xaxis") == "x2" for t in figs["meas"]["data"])   # range-rate obs from amb_dop
    md = _md(at)
    _no_leak(md)
    assert "NO TRUTH" in md and "radar tracks active" in md
    MATRIX.append(("live fake · tracks-only run (no truth)", "grey tracks on map + meas, obs incl. range rate"))


# ── the Live page on the fake as a STREAM (wall-clock live, mid-engagement) ──
def test_live_stream_mid_engagement_grades_marks_and_range_rate_sign(fake_now):
    t0 = time.time()
    at = _at(LIVE, **_live_state(FM.RUN_COLL)).run()
    dt = time.time() - t0
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"] and snap["anchored"] is False and snap["data_age"] < F.LIVE_STALE_S               # truly live
    assert at.session_state["tgt_tid"] == 177
    md = _md(at)
    _no_leak(md)
    tiles = _tiles(md)
    assert re.fullmatch(r"\d+<small>m</small>", tiles[2][2]) and re.fullmatch(r"\d+<small>m</small>", tiles[1][2])   # closest-so-far is the third tile
    assert tiles[0][2].startswith("#177") and tiles[0][0] in ("ok", "amber")   # target track tile first
    feeds = snap["feeds"]
    assert [f["state"] for f in feeds if f["role"] == "target"] == ["alive"]
    assert sum(f["state"] == "alive" for f in feeds if f["role"] == "interceptor") >= 1
    figs = LS.figs_of(at.session_state["_sid"])
    reads = [a["text"] for a in figs["err"]["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
    assert all(r.startswith(("now ", "last ")) for r in reads), reads
    body = json.loads(LS.response_body(LS.STORE[at.session_state["_sid"]], {k: "" for k in FIG_KEYS}))
    assert set(body["figs"]) == set(FIG_KEYS) and body["heads"]["tgt"] and body["heads"]["itc"]
    # missing p_cov rows: sigma NaN, excluded from containment, never 0
    P = {k: at.session_state[k] for k in ("map_half", "trail_s", "show_sat", "show_blind", "show_obs", "spec_window", "cpa_gate_m")}
    tr = snap["tracks"][177]
    assert np.isnan(tr[:, D.TK["sE"]]).sum() >= 1, "the fake drops p_cov on every 17th row"
    assert not np.any(tr[:, D.TK["sE"]] == 0)
    e = E.track_errors(tr, snap["tgt"], ant=snap["ant"], t_now=snap["t_now"], window_s=120.0, tid=177)
    assert len(e["t"]) > 10
    for k in ("sig_az", "sig_el", "sig_alt", "sig_pos3d"):
        assert not np.any(np.asarray(e[k]) == 0.0)
    c = E.containment(e, "az")
    assert c["n"] == int((np.isfinite(e["az"]) & np.isfinite(e["sig_az"])).sum()) and c["n"] <= int(np.isfinite(e["az"]).sum())
    # obs range rate: -amb_dop reproduces the target truth's opening-positive monostatic rate
    A = {"t_now": snap["t_now"], "tgt_tid": 177, "alt_tid": None, "itc_tid": None, "has_truth": True}
    M = E.meas_space(snap, A, 120.0)
    assert M["obs_rr"] is True and M["obs"] is not None
    tr_i = E.interp_truth(snap["tgt"], M["obs"]["t"])
    _, _, _, rr_t = E.aer_rr(tr_i[:, 0], tr_i[:, 1], tr_i[:, 2], tr_i[:, 3], tr_i[:, 4], tr_i[:, 5])
    ok = np.isfinite(rr_t) & np.isfinite(M["obs"]["rr"])
    # A9: the quad is BISTATIC — the fake publishes amb_bistatic_rng_rate_ms = 2 x the monostatic opening-positive rate (co-located TX)
    assert ok.sum() > 20 and float(np.median(np.abs(M["obs"]["rr"][ok] - 2.0 * rr_t[ok]))) < 4.0
    assert float(np.median(np.abs(M["obs"]["rng_mono"][ok] * 2.0 - M["obs"]["rng"][ok]))) < 1.0                    # bistatic range = 2 x mono here
    assert any(t.get("name") == "raw obs" and t.get("xaxis") == "x2" for t in figs["meas"]["data"])
    MATRIX.append(("live fake · streaming now @ pass 2", f"graded, tgt #177, first render {dt:.1f} s"))


def test_live_stream_frozen_target_feed_flagged():
    fm = FM.FakeMongo(F1_MID - 200, F1_MID + 90, shift_to_now=F1_MID, freeze={"mav14550_1_1": F1_MID - 40}, with_obs=False)
    restore = fm.install()
    try:
        at = _at(LIVE, **_live_state(FM.RUN_COLL)).run()
        assert not _exc(at), _exc(at)
        snap = _ss(at, "last_snap")
        tgt = [f for f in snap["feeds"] if f["role"] == "target"]
        assert tgt and tgt[0]["state"] == "frozen" and tgt[0]["frozen"] is True
        more = "\n".join(m.value for m in at.expander[0].markdown)
        assert re.search(r'MAVLink feeds</div><div class="v">TGT FROZEN', more), more[:400]      # A10: one "MAVLink feeds" chip (TGT state · INT state)
        md = _md(at)                                                                          # 2026-09-14: feed health is WORDS in the status line, only when not alive
        # 2026-09-15 ("remove all the random icons"): the word carries the state on its own — class-less bold, no coloured icon span
        stat = re.search(r'<div class="ih-status">(.*?)</div>', md).group(1)
        assert re.search(r'<span><b>TGT FEED FROZEN \d+ s</b></span>', stat), stat
        assert 'class="g fail"' not in stat and "<svg" not in stat, stat
        tail = snap["tgt"][snap["tgt"][:, 0] > F1_MID - 40 + fm.shift + 1.0]
        assert len(tail) == 1, len(tail)      # the frozen repeats are scrubbed EXCEPT the last one (2026-09-15 ih.data.clean_feed keeps the
                                              # first AND last row of a parked position, so a parked vehicle still has a current sample)
        _no_leak(_md(at))
    finally:
        restore()
    MATRIX.append(("live fake · frozen target GPS", "feed chip FROZEN, status word TGT FEED FROZEN, repeats scrubbed"))


# ── Data source page: MRU number -> Connect -> auto-selected run -> live info -> roles -> save -> switch to the saved flight ──
@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """A throw-away ARCHIVE_ROOT for the save flow; the flight registered there is dropped again afterwards."""
    monkeypatch.setattr(D, "ARCHIVE_ROOT", str(tmp_path))
    D.clear_flight_caches()
    yield str(tmp_path)
    D.clear_flight_caches()
    for n in [k for k in D.FLIGHT_WINDOWS if k > 4]:
        D.FLIGHT_WINDOWS.pop(n, None)
        D.FLIGHT_DIRS.pop(n, None)


def test_data_source_connect_autoselects_run_info_roles_save_and_switch(fake, tmp_root):
    at = _at(SRC, mru_number=91, ds_mode="live").run()
    assert not _exc(at), _exc(at)
    md = _md(at)
    assert "NOT CONNECTED" in md and "10.191.28.205" in md                                       # the resolver's "→ IP" line
    assert at.radio(key="_ds_mode_w").value == "live" and at.number_input(key="_mru_w").value == 91
    assert at.session_state["source"] == "archive" and at.session_state["mode"] == "archive"       # not connected: the engine keeps the replay
    at.button(key="connect_btn").click().run()
    assert not _exc(at), _exc(at)
    pr = at.session_state["live_probe"]
    assert pr["ok"] and pr["n_total"] == 3 and all("start_t" in r and "friendly" in r for r in pr["runs"])
    assert at.session_state["live_host"] == "10.191.28.205" and at.session_state["live_run"] == FM.RUN_COLL          # newest run WITH data
    assert at.session_state["source"] == "live" and at.session_state["mode"] == "live" and at.session_state["live_run_name"] == "Red_Mandrill"
    sel = at.selectbox(key="_run_w")
    assert sel.value == FM.RUN_COLL and len(sel.options) == 3 and any("Red_Mandrill" in str(o) and "e65bd4f9" in str(o) for o in sel.options)
    # LIVE INFO: feeds detected, counts, antenna, co-located TX, run start from runs.date_time (UTC 14:00 -> 07:00 PDT)
    md = _md(at)
    assert "Live info" in md and "mav14550_1_1" in md and "mav14551_2_34" in md and "co-located" in md
    info = at.session_state["_live_info"]
    assert info["ok"] and len(info["feeds"]) == 4 and info["n143_w"] > 0 and info["n103_w"] > 0 and info["ant"] and info["tx_lla"] is None
    assert abs(info["start_t"] - D.hms_to_epoch("07:00:00")) < 1.0 and info["latency_ms"] is not None
    assert sorted(at.session_state["_ids_seen"]) == ["mav14550_1_1", "mav14551_2_0", "mav14551_2_1", "mav14551_2_34"]
    _no_leak(md)
    # ROLE ASSIGNMENT: prefilled by the patterns, an overlap is refused, an explicit change drives role_ids, Swap swaps
    tg, ic = at.multiselect(key="_tgt_ids_w"), at.multiselect(key="_itc_ids_w")
    assert tg.value == ["mav14550_1_1"] and set(ic.value) == {"mav14551_2_0", "mav14551_2_1", "mav14551_2_34"}
    assert at.session_state["role_ids"] == {"target": [], "interceptor": []}                                     # prefill is not an assignment
    tg.select("mav14551_2_34").run()
    assert not _exc(at), _exc(at)
    assert "REFUSED" in _md(at) and at.session_state["role_ids"] == {"target": [], "interceptor": []}
    assert at.multiselect(key="_tgt_ids_w").value == ["mav14550_1_1"]                                              # reverted
    at.multiselect(key="_itc_ids_w").unselect("mav14551_2_34").run()
    assert not _exc(at), _exc(at)
    assert at.session_state["role_ids"] == {"target": ["mav14550_1_1"], "interceptor": ["mav14551_2_0", "mav14551_2_1"]}
    assert at.session_state["truth_tgt_ids"] == ["mav14550_1_1"] and "explicit" in _md(at)
    at.button(key="swap_roles").click().run()
    assert at.session_state["role_ids"] == {"target": ["mav14551_2_0", "mav14551_2_1"], "interceptor": ["mav14550_1_1"]}
    at.button(key="swap_roles").click().run()
    at.multiselect(key="_itc_ids_w").select("mav14551_2_34").run()
    assert at.session_state["role_ids"] == {"target": ["mav14550_1_1"], "interceptor": ["mav14551_2_0", "mav14551_2_1", "mav14551_2_34"]}
    # the Live page renders live with that state; snapshot CONTRACT fields present
    live = _at(LIVE, **{k: at.session_state[k] for k in ("source", "live_host", "live_port", "live_db", "live_run", "hist_s", "truth_tgt_ids", "truth_itc_ids")}).run()
    assert not _exc(live), _exc(live)
    snap = _ss(live, "last_snap")
    assert snap["ok"] and "LIVE" in _status(_md(live)) and snap["unit"] == "MRU91" and snap["tx_lla"] is None and snap["obs"].shape[1] == 8
    assert set(snap["obs_meta"]) == {"truth_target_id", "truth_match_type"} and len(snap["obs_meta"]["truth_target_id"]) == len(snap["obs"])
    assert snap["track_meta"] and all(set(v) == {"truth_match_id", "truth_match_conf", "contributors", "t"} for v in snap["track_meta"].values())
    assert live.session_state["role_ids"] == {"target": ["mav14550_1_1"], "interceptor": ["mav14551_2_0", "mav14551_2_1", "mav14551_2_34"]}
    # SAVE TO ARCHIVE: last 10 min (default) -> background job -> SAVED callout -> Switch to this flight replays it
    assert at.radio(key="_save_scope_w").value == "last" and at.text_input(key="_save_label_w").value == "Flight 1"
    md = _md(at)
    assert "Save to archive" in md and "e65bd4f9_Flight_1" in md and tmp_root in md
    at.button(key="save_btn").click().run()
    assert not _exc(at), _exc(at)
    jid = at.session_state["_save_job"]
    t0 = time.time()
    while AR.job(jid)["state"] == "running" and time.time() - t0 < 180:
        time.sleep(0.1)
    j = AR.job(jid)
    assert j["state"] == "done", j
    at.run()
    assert not _exc(at), _exc(at)
    md = _md(at)
    assert "SAVED" in md and "flights.json" in md and j["result"]["n"] == 5 and j["result"]["dir"].startswith(tmp_root)
    assert os.path.isfile(os.path.join(j["result"]["dir"], "obs", "obs.csv"))
    at.button(key="switch_saved").click().run()
    assert not _exc(at), _exc(at)
    assert at.session_state["ds_mode"] == "archive" and at.session_state["source"] == "archive" and at.session_state["mode"] == "archive"
    assert at.session_state["flight"] == 5 and at.selectbox(key="_flight_w").value == 5 and "Saved from" in _md(at)
    assert abs(at.session_state["anchor_t"] - D.replay_bounds(5)[0]) < 1.0
    live2 = _at(LIVE, **{k: at.session_state[k] for k in ("source", "flight", "anchor_t", "playing")}).run()
    assert not _exc(live2), _exc(live2)
    # 2026-09-15: the status strip carries NO source part in archive mode (the sidebar already says the day / flight)
    st5 = _status(_md(live2))
    assert "LIVE" not in st5 and "ARCHIVE" not in st5 and "F5" not in st5, st5
    assert "PAUSED" in st5 and re.search(r"\d\d:\d\d:\d\d", st5), st5
    MATRIX.append(("data source · MRU 91 -> connect -> auto run -> info -> roles -> save -> switch", "ok"))


def test_data_source_mode_selector_archive_and_disconnect(fake):
    at = _at(SRC, mru_number=91, ds_mode="live", source="live", live_host="fake", live_port=27017, live_run=FM.RUN_COLL,
             live_probe=F.probe("fake", 27017, "sensor_store", "")).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["mode"] == "live" and "Live info" in _md(at)
    at.radio(key="_ds_mode_w").set_value("archive").run()
    assert not _exc(at), _exc(at)
    assert at.session_state["ds_mode"] == "archive" and at.session_state["source"] == "archive" and at.session_state["mode"] == "archive"
    assert at.selectbox(key="_flight_w").value == 1 and "Archive replay" in _md(at) and "Live info" not in _md(at)
    at.radio(key="_ds_mode_w").set_value("live").run()
    assert not _exc(at), _exc(at)
    assert at.session_state["source"] == "live" and at.session_state["mode"] == "live"                     # a followed run: LIVE re-engages at once
    # the sidebar's Disconnect (D.disconnect): back to the replay, run dropped
    at.session_state["live_run"] = ""
    at.session_state["live_probe"] = None
    at.session_state["source"] = "archive"
    at.run()
    assert not _exc(at), _exc(at)
    assert "NOT CONNECTED" in _md(at) and at.session_state["mode"] == "archive"


def test_connect_helper_prefers_recent_data_then_data_then_newest(fake):
    res = F.connect("fake", 27017, "sensor_store", "")
    assert res["ok"] and res["run"] == FM.RUN_COLL and res["row"]["friendly"] == "Red_Mandrill" and "newest run with" in res["reason"]
    res2 = F.connect("fake", 27017, "sensor_store", "silver")                                   # filter -> no counts? the ADS-B-only run has 106 docs
    assert res2["ok"] and res2["run"] == FM.ADSB_ONLY_COLL
    res3 = F.connect("fake", 27017, "sensor_store", "nomatch")
    assert res3["ok"] and res3["run"] is None and "no run_*" in res3["reason"]
    bad = F.connect("10.255.255.1", 27017, "sensor_store", "")
    assert not bad["ok"] and "ServerSelectionTimeoutError" in bad["err"] and bad["run"] is None


def test_live_info_and_mavlink_span_helpers(fake):
    info = F.live_info("fake", 27017, "sensor_store", FM.RUN_COLL)
    assert info["ok"] and [f["id"] for f in info["feeds"]] == ["mav14550_1_1", "mav14551_2_0", "mav14551_2_1", "mav14551_2_34"]
    f0 = info["feeds"][0]
    assert f0["rows"] > 20 and f0["first_t"] <= f0["last_t"] <= info["newest_t"] and f0["state"] in ("alive", "stale", "down") and f0["age"] >= 0
    assert info["n143_w"] > 20 and info["n103_w"] > 20 and info["n106_w"] >= info["n_mav_docs_w"] > 0 and info["ant"] == pytest.approx(FM.ANT)
    assert info["tx_lla"] is None and info["friendly"] == "Red_Mandrill" and abs(info["start_t"] - D.hms_to_epoch("07:00:00")) < 1
    assert info["newest_143_t"] and info["data_age"] > 0 and info["window_s"] == 60.0
    i2 = F.live_info("fake", 27017, "sensor_store", FM.TRACKS_ONLY_COLL, start_t=123.0)
    assert i2["ok"] and i2["feeds"] == [] and i2["n106_w"] == 0 and i2["start_t"] == 123.0
    assert i2["tx_lla"] == pytest.approx((FM.ANT[0] + FM.TX_OFFSET_DEG, FM.ANT[1], FM.ANT[2]))                  # contributors tx_lla_ddm -> TX
    i3 = F.live_info("fake", 27017, "sensor_store", FM.ADSB_ONLY_COLL)
    assert i3["ok"] and i3["feeds"] == [] and i3["n_adsb_docs_w"] == i3["n106_w"] > 0 and i3["ant"] is None
    assert not F.live_info("", 27017, "sensor_store", FM.RUN_COLL)["ok"] and not F.live_info("10.255.255.1", 27017, "sensor_store", FM.RUN_COLL)["ok"]
    sp = F.mavlink_span("fake", 27017, "sensor_store", FM.RUN_COLL)
    assert sp["ok"] and sp["first_t"] < sp["last_t"] and sp["first_t"] >= T0 and sp["last_t"] <= T1
    assert F.mavlink_span("fake", 27017, "sensor_store", FM.TRACKS_ONLY_COLL) == {"ok": True, "first_t": None, "last_t": None, "err": None}
    assert F.run_index(fake.dbs["sensor_store"])[FM.RUN_COLL]["friendly"] == "Red_Mandrill"


def test_live_snapshot_contract_track_meta_obs_meta_tx_lla(fake_now):
    at = _at(LIVE, **_live_state(FM.RUN_COLL)).run()
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"] and snap["tx_lla"] is None and snap["tx"] is None and snap["unit"] == "MRU91"
    tm = snap["track_meta"]
    assert tm and set(tm) <= set(snap["tracks"]) and all(set(v) == {"truth_match_id", "truth_match_conf", "contributors", "t"} for v in tm.values())
    mav = [v for v in tm.values() if v["truth_match_id"] and v["truth_match_id"].startswith("mav")]
    assert mav and all(0.5 <= v["truth_match_conf"] <= 1.0 for v in mav) and {v["truth_match_id"] for v in mav} <= {"mav14550_1_1", "mav14551_2_0"}
    assert all(np.isnan(v["truth_match_conf"]) for v in tm.values() if v["truth_match_id"] is None)
    assert all(v["truth_match_conf"] == pytest.approx(0.9) for v in tm.values() if v["truth_match_id"] == FM.ADSB_CALLSIGN)
    assert all(json.loads(v["contributors"])[0]["tx_lla_ddm"] == list(FM.ANT) for v in tm.values() if v["contributors"])
    obs, om = snap["obs"], snap["obs_meta"]
    assert obs.shape[1] == 8 and len(om["truth_target_id"]) == len(obs) == len(om["truth_match_type"])
    ids = {x for x in om["truth_target_id"] if x}
    assert "mav14550_1_1" in ids and "DAL1556" in ids
    code = obs[:, 7]
    is_t = np.array([x == "mav14550_1_1" for x in om["truth_target_id"]])
    is_a = np.array([x == "DAL1556" for x in om["truth_target_id"]])
    is_n = np.array([x is None for x in om["truth_target_id"]])
    assert np.array_equal(code == 1.0, is_t) and np.array_equal(code == 3.0, is_a) and np.isnan(code[is_n]).all()
    assert all(m == "range_and_el" for m, i in zip(om["truth_match_type"], om["truth_target_id"]) if i)
    fin = np.isfinite(obs[:, 4])
    assert fin.any() and np.allclose(obs[fin, 6], 2.0 * obs[fin, 4], atol=1e-6) and np.allclose(obs[:, 5], 2.0 * obs[:, 3], atol=1e-3)   # bistatic pair from the feed
    # a run whose contributors carry a distinct TX -> tx_lla (and tx) derived
    at2 = _at(LIVE, **_live_state(FM.TRACKS_ONLY_COLL)).run()
    assert not _exc(at2), _exc(at2)
    s2 = _ss(at2, "last_snap")
    assert s2["ok"] and s2["tx_lla"] == pytest.approx((FM.ANT[0] + FM.TX_OFFSET_DEG, FM.ANT[1], FM.ANT[2])) and s2["tx"] == s2["tx_lla"]
    MATRIX.append(("live fake · snapshot contract", f"track_meta {len(tm)} tracks ({len(mav)} MAVLink-matched), obs_meta aligned, tx_lla None / derived"))


def test_probe_caps_counts_to_the_newest_limit_runs(fake):
    pr = F.probe("fake", 27017, "sensor_store", "", limit=1)
    assert pr["ok"] and len(pr["runs"]) == 3 and "n143" in pr["runs"][0] and "n143" not in pr["runs"][2]
    assert pr["runs"][0]["last_t"] >= pr["runs"][1]["last_t"] >= (pr["runs"][2]["last_t"] or 0)          # newest first
    r0 = next(r for r in pr["runs"] if r["name"] == FM.RUN_COLL)
    assert abs(r0["start_t"] - D.hms_to_epoch("07:00:00")) < 1.0 and r0["friendly"] == "Red_Mandrill"       # runs.date_time (UTC) -> start
    assert F.run_label(r0, r0["last_t"] + 30.0) == f"Red_Mandrill · e65bd4f9 · 07:00:00 · 30 s ago"
    d = F.run_detail("fake", 27017, "sensor_store", FM.TRACKS_ONLY_COLL)
    assert d["ok"] and d["newest_143_t"] and d["n143_15min"] > 100 and d["mavlink_60s"] is False and d["ant"]
    d2 = F.run_detail("fake", 27017, "sensor_store", FM.ADSB_ONLY_COLL)
    assert d2["ok"] and d2["newest_143_t"] is None and d2["ant"] is None


def test_unreachable_host_offline_within_timeout_no_exception(fake):
    t0 = time.time()
    at = _at(LIVE, **_live_state(FM.RUN_COLL, host="10.255.255.1")).run()
    dt = time.time() - t0
    assert not _exc(at), _exc(at)
    md = _md(at)
    assert "OFFLINE" in md and "ServerSelectionTimeoutError" in md and dt < 15, dt
    _no_leak(md)
    pr = F.probe("10.255.255.1", 27017, "sensor_store", "")
    assert not pr["ok"] and "ServerSelectionTimeoutError" in pr["err"]
    # 2026-09-15: the probe timeout is 8 s (3 s read as "unreachable" while MRU91 was up) and the Data source page words a dead
    # probe as "NO REPLY · <host>:<port> did not answer within 8 s" — no "OFFLINE" / "unreachable" callout there any more.
    assert F.PROBE_TIMEOUT_MS == 8000
    ds = _at(SRC, mru_number=91, ds_mode="live", live_host="10.255.255.1", live_port=27017,
             live_probe={"ok": False, "err": pr["err"], "ts": time.time(), "runs": [], "n_total": 0, "limit": 25}).run()
    assert not _exc(ds), _exc(ds)
    dmd = "\n".join(m.value for m in ds.markdown if "ih-callout" in m.value)
    assert "NO REPLY" in dmd and f"did not answer within {F.PROBE_TIMEOUT_MS // 1000} s" in dmd and "did not answer within 8 s" in dmd, dmd
    assert "OFFLINE" not in dmd and "unreachable" not in dmd, dmd
    MATRIX.append(("live · unreachable host", f"OFFLINE in {dt:.1f} s, no exception; Data source says NO REPLY within 8 s"))


def test_live_archive_live_switch_one_server_same_sid_fresh_figures(fake):
    at = _at(LIVE, **_live_state(FM.TRACKS_ONLY_COLL)).run()
    assert not _exc(at), _exc(at)
    sid = at.session_state["_sid"]
    srv = LS._SERVER
    st0 = dict(LS.STORE[sid]["fig_stamps"])
    assert any(t.get("name") == "radar tracks (no truth)" for t in LS.figs_of(sid)["map"]["data"])
    # -> archive (what the Data source radio's callback does)
    at.session_state["source"] = "archive"
    at.session_state["anchor_t"] = F1_MID
    for k in ("_live_buf", "live_ant"):
        if k in at.session_state:
            del at.session_state[k]
    D.reset_derived.__wrapped__ if hasattr(D.reset_derived, "__wrapped__") else None
    at.session_state["cpa_run"] = None; at.session_state["cpa_ok"] = None; at.session_state["tgt_tid"] = None; at.session_state["last_snap"] = None
    at.run()
    assert not _exc(at), _exc(at)
    assert at.session_state["_sid"] == sid and LS._SERVER is srv and LS.start(LS.port()) is srv
    st1 = dict(LS.STORE[sid]["fig_stamps"])
    assert all(st1[k] > st0[k] for k in FIG_KEYS), (st0, st1)                                  # every figure rebuilt for the new source
    assert at.session_state["tgt_tid"] == 177 and "LIVE" not in _status(_md(at)), _status(_md(at))   # 2026-09-15: archive = NO source part at all (live still says "LIVE <run>")
    assert not any(t.get("name") == "radar tracks (no truth)" for t in LS.figs_of(sid)["map"]["data"])   # no stale live figure
    # -> live again
    at.session_state["source"] = "live"
    at.session_state["cpa_run"] = None; at.session_state["cpa_ok"] = None; at.session_state["tgt_tid"] = None; at.session_state["last_snap"] = None
    at.run()
    assert not _exc(at), _exc(at)
    st2 = dict(LS.STORE[sid]["fig_stamps"])
    assert all(st2[k] > st1[k] for k in FIG_KEYS) and at.session_state["_sid"] == sid and LS._SERVER is srv
    assert "LIVE" in _status(_md(at)) and _ss(at, "last_snap")["ok"]
    assert sum(1 for k in LS.STORE if k == sid) == 1
    MATRIX.append(("live -> archive -> live", "one server, same sid, figures rebuilt each switch"))


# ── the REAL unit: MRU39 (ADS-B only, no MAVLink, 275 runs) ─────────────────
@needs_mru39
def test_mru39_probe_275_runs_newest_first_bounded():
    t0 = time.time()
    pr = F.probe(*MRU39, "")
    dt = time.time() - t0
    assert pr["ok"], pr["err"]
    assert pr["n_total"] >= 200 and len(pr["runs"]) == pr["n_total"] and dt < 30, (pr["n_total"], dt)
    lt = [r["last_t"] or 0 for r in pr["runs"]]
    assert lt == sorted(lt, reverse=True)
    assert all("n143" in r and "n106" in r for r in pr["runs"][:pr["limit"]]) and not any("n143" in r for r in pr["runs"][pr["limit"]:])
    MATRIX.append(("MRU39 · probe", f"{pr['n_total']} runs newest-first in {dt:.1f} s, counts for {pr['limit']}"))
    pytest.mru39_probe = pr  # type: ignore[attr-defined]


@needs_mru39
def test_mru39_data_source_connect_lists_all_runs_and_shows_live_info():
    at = _at(SRC, mru_number=39, ds_mode="live").run()
    assert not _exc(at), _exc(at)
    assert "10.139.28.205" in _md(at)                                                            # MRU 39 -> 10.139.28.205
    t0 = time.time()
    at.button(key="connect_btn").click().run()
    dt = time.time() - t0
    assert not _exc(at), _exc(at)
    pr = at.session_state["live_probe"]
    assert pr["ok"] and pr["n_total"] >= 200 and dt < 120, (pr.get("err"), dt)
    sel = at.selectbox(key="_run_w")
    assert len(sel.options) == pr["n_total"] and sel.value == at.session_state["live_run"] and at.session_state["source"] == "live"
    row = next(r for r in pr["runs"] if r["name"] == sel.value)
    assert (row.get("n143") or 0) + (row.get("n106") or 0) > 0                                 # auto-selected: the newest run WITH data
    assert sum(1 for r in pr["runs"][:pr["limit"]] if r.get("start_t")) >= 20                   # runs.date_time parsed for the named runs
    md = _md(at)
    assert "Live info" in md and ("Waiting for MAVLink feeds" in md or "MAVLink id" in md)
    info = at.session_state["_live_info"]
    assert info["ok"] and info["latency_ms"] is not None and info["start_t"] and info["newest_t"]
    _no_leak(md)
    assert at.button(key="save_btn") and at.text_input(key="_save_label_w").value.startswith("Flight ")
    MATRIX.append(("MRU39 · data source connect", f"{pr['n_total']} runs, auto {sel.value[:12]}…, connect {dt:.1f} s, feeds {len(info['feeds'])}, "
                                                  f"tx {'derived' if info['tx_lla'] else 'co-located'}"))


@needs_mru39
def test_mru39_newest_run_connected_no_truth_clean_empty_state():
    pr = getattr(pytest, "mru39_probe", None) or F.probe(*MRU39, "")
    run = pr["runs"][0]["name"]
    t0 = time.time()
    at = _at(LIVE, **_live_state(run, host=MRU39[0], port=MRU39[1])).run()
    dt = time.time() - t0
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"], snap["err"]
    assert not snap["has_truth"] and snap["tgt"].shape[0] == 0 and snap["itc"].shape[0] == 0     # ADS-B never becomes truth
    md = _md(at)
    _no_leak(md)
    assert "OFFLINE" not in md and ("CONNECTED" in md or snap["has_truth"])
    tiles = _tiles(md)
    assert tiles[2][2] == "—" and tiles[1][2] == "—" and tiles[0][2] in ("NO TRUTH", "NO TRACK")   # target track tile first
    st = _status(md)
    assert "NO TRUTH FEED" in st
    figs = LS.figs_of(at.session_state["_sid"])
    assert set(figs) == set(FIG_KEYS)
    reads = [a["text"] for a in figs["err"]["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
    assert reads == [PL.EMPTY_READOUT] * 4
    titles = [a["text"] for a in figs["err"]["layout"]["annotations"] if a["text"].startswith("<b>")]
    assert titles == [f"<b>{PL.ERR_TITLE[k]}</b> ({PL.ERR_UNIT[k].strip()})" for k in PL.ERR_KEYS]   # 2026-09-15: the card title carries its unit
    MATRIX.append(("MRU39 · newest run", f"connected, no truth, age {D.fmt_age(snap['data_age'])}, first render {dt:.1f} s"))


@needs_mru39
def test_mru39_run_with_tracks_renders_grey_ungraded():
    pr = getattr(pytest, "mru39_probe", None) or F.probe(*MRU39, "")
    cands = [r for r in pr["runs"][:pr["limit"]] if r.get("n143", 0) > 0]
    if not cands:
        pytest.skip("no run with TRACKS among the newest 25")
    run = cands[0]["name"]
    t0 = time.time()
    at = _at(LIVE, **_live_state(run, host=MRU39[0], port=MRU39[1])).run()
    dt = time.time() - t0
    assert not _exc(at), _exc(at)
    snap = _ss(at, "last_snap")
    assert snap["ok"], snap["err"]
    assert snap["ant"] is not None, snap
    if not snap["tracks"]:
        pytest.skip(f"run {run[:12]} has TRACKS docs but none inside the live window (data_age {snap.get('data_age')}) — unit idle")
    assert snap["anchored"] is True or snap["data_age"] < F.LIVE_STALE_S
    md = _md(at)
    _no_leak(md)
    figs = LS.figs_of(at.session_state["_sid"])
    names = [t.get("name") for t in figs["map"]["data"]]
    assert "radar tracks (no truth)" in names and "radar track heads" in names
    assert "trk/s" in _status(md)
    reads = [a["text"] for a in figs["err"]["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
    assert reads == [PL.EMPTY_READOUT] * 4                                                     # nothing to grade without truth
    # second tick pages forward and stays cheap
    t1 = time.time(); at.run(); dt2 = time.time() - t1
    assert not _exc(at), _exc(at) and dt2 < 10
    MATRIX.append(("MRU39 · run with tracks", f"{run[:12]}… {len(snap['tracks'])} tracks grey, anchored={snap['anchored']}, first render {dt:.1f} s, tick {dt2:.1f} s"))


@needs_mru39
def test_mru39_live_archive_live_switch():
    pr = getattr(pytest, "mru39_probe", None) or F.probe(*MRU39, "")
    run = pr["runs"][0]["name"]
    at = _at(LIVE, **_live_state(run, host=MRU39[0], port=MRU39[1])).run()
    assert not _exc(at), _exc(at)
    sid, srv = at.session_state["_sid"], LS._SERVER
    st0 = dict(LS.STORE[sid]["fig_stamps"])
    at.session_state["source"] = "archive"; at.session_state["anchor_t"] = F1_MID
    for k in ("_live_buf", "live_ant"):
        if k in at.session_state:
            del at.session_state[k]
    at.session_state["last_snap"] = None; at.session_state["tgt_tid"] = None
    at.run(); assert not _exc(at), _exc(at)
    st1 = dict(LS.STORE[sid]["fig_stamps"])
    assert at.session_state["tgt_tid"] == 177 and all(st1[k] > st0[k] for k in FIG_KEYS)
    at.session_state["source"] = "live"; at.session_state["last_snap"] = None; at.session_state["tgt_tid"] = None
    at.run(); assert not _exc(at), _exc(at)
    assert at.session_state["_sid"] == sid and LS._SERVER is srv and _ss(at, "last_snap")["ok"]
    assert all(LS.STORE[sid]["fig_stamps"][k] > st1[k] for k in FIG_KEYS)
    MATRIX.append(("MRU39 · live -> archive -> live", "one server, same sid, rebuilt"))


def test_zz_write_matrix():
    out = os.environ.get("IH_MATRIX_OUT")
    if out:
        with open(out, "a") as f:
            for k, v in MATRIX:
                f.write(f"{k}\t{v}\n")
