"""Velocity-state plumbing (2026-09-11): the data path of the North / East / vertical velocity cards.

Contract under test (the figure code reads it, nothing else computes it):
  * feed.track_rows / live_snapshot / archive replay -> (m,16) track arrays: cols 13..15 = sigvE, sigvN, sigvU (1σ filtered
    velocity, m/s; NaN without a covariance, never 0) — ih.data.TK["svE"|"svN"|"svU"], ih.data.TK_W == 16;
  * engine errors dicts (A["errors"], A["errors_ung"], A["errors_alt"], every A["err_tracks"][i]["errors"|"ungraded"]) carry
    "ve","vn","vu" (spa corr_df e_dot/n_dot/u_dot: TRACK − truth velocity per ENU axis, m/s) and "sig_ve","sig_vn","sig_vu"
    (spa e_dot/n_dot/u_dot_sigma) with len == len("t"); a σ is NaN wherever the state published no covariance — the adapter's
    constant fill never leaves spa_errors;
  * A["contain"]["ve"|"vn"|"vu"] = engine.containment over the rows with a finite σ only (n == 0 on the 8/28 archives).

Two data paths: the F1 ARCHIVE replay (8/28 quickdumps: velocities yes, velocity σ no) and the fake-mongo LIVE path
(tests/fake_mongo.py writes σvE 0.8 / σvN 0.6 / σvU 1.5 m/s into both block-143 layouts, drops p_cov on every 17th row).

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_velocity_plumbing.py
"""
from __future__ import annotations

import logging
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.WARNING)   # bare-mode streamlit session-state warnings

import streamlit as st  # noqa: E402

import fake_mongo as FM  # noqa: E402
from ih import data as D  # noqa: E402  (puts track_correlation + chaos-spa on sys.path)
from ih import engine as E  # noqa: E402
from ih import feed as F  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402

VKEYS = ("ve", "vn", "vu")
SKEYS = ("sig_ve", "sig_vn", "sig_vu")
P = {"trail_s": 20, "spec_window": 120, "cpa_gate_m": 70.0}
F1_MID = D.hms_to_epoch("07:22:31")


@pytest.fixture(autouse=True)
def _fresh_engine_state():
    E._BARE_STATE.clear()
    yield
    E._BARE_STATE.clear()


@pytest.fixture(scope="module")
def fake():
    """A short fake run around pass 2 of F1 (both block-143 layouts, velocity covariance, every 17th row without p_cov)."""
    srv = FM.FakeMongo(F1_MID - 200.0, F1_MID + 30.0)
    restore = srv.install()
    yield srv
    restore()


def _lens_ok(e: dict) -> None:
    n = len(e["t"])
    for k in (*VKEYS, *SKEYS):
        assert k in e and len(np.asarray(e[k])) == n, (k, n)


def _f1_snap(hms: str) -> dict:
    st.session_state["flight"] = 1
    st.session_state["spec_window"] = 120
    return F.archive_snapshot(D.hms_to_epoch(hms), 180.0)


def test_key_roster_and_empty_errors():
    e = E._empty_errors()
    for k in (*VKEYS, *SKEYS):
        assert k in e and len(e[k]) == 0
    assert set(SKEYS) <= set(E.SIG_KEYS) and E.VEL_KEYS == VKEYS and set(E.ERR_ROW_KEYS) >= {*VKEYS, *SKEYS, "t", "kind"}
    assert D.TK_W == 16 and (D.TK["svE"], D.TK["svN"], D.TK["svU"]) == (13, 14, 15)
    # guard_sigmas: a 0 / negative velocity σ is UNKNOWN (NaN), never "perfect"
    g = E.guard_sigmas({"sig_ve": np.array([0.0, -1.0, 0.8]), "sig_vn": np.array([0.6]), "sig_vu": np.array([np.nan])})
    assert np.isnan(g["sig_ve"][:2]).all() and g["sig_ve"][2] == 0.8 and g["sig_vn"][0] == 0.6 and np.isnan(g["sig_vu"][0])
    # containment on the velocity keys: only finite (err, σ) pairs count
    e = {"ve": np.array([0.5, 4.0, 0.1, 1.0]), "sig_ve": np.array([1.0, 1.0, np.nan, 0.5])}   # |err|/σ = 0.5, 4, (no σ), 2
    assert E.containment(e, "ve") == {"n": 3, "p1": pytest.approx(100.0 / 3), "p3": pytest.approx(200.0 / 3)}
    assert E.containment({"ve": np.array([1.0]), "sig_ve": np.array([np.nan])}, "ve") == {"n": 0, "p1": None, "p3": None}


def test_f1_replay_carries_velocity_errors_without_sigma_and_empty_containment():
    """The 8/28 quickdumps publish track velocities but no velocity σ: errors graded, every σ NaN, contain n == 0."""
    snap = _f1_snap("07:22:31")
    assert snap["tracks"] and all(a.shape[1] == D.TK_W for a in snap["tracks"].values())
    assert all(np.isnan(a[:, 13:16]).all() for a in snap["tracks"].values())
    A = E.analyze(snap, P)
    assert A["tgt_tid"] == 177
    e = A["errors"]
    _lens_ok(e)
    assert len(e["t"]) > 10 and e["grader"] == "spa"
    assert np.isfinite(e["ve"]).sum() > 10 and np.isfinite(e["vn"]).sum() > 10 and np.isfinite(e["vu"]).sum() > 10
    assert all(np.isnan(e[k]).all() for k in SKEYS)
    # spa's velocity error IS track velocity − truth velocity per ENU axis (sign and units check on the real archive)
    a = A["tgt_track"]
    idx = np.searchsorted(a[:, 0], e["t"]).clip(0, len(a) - 1)
    assert np.allclose(a[idx, 0], e["t"], atol=1e-6)
    tr = E.interp_truth(snap["tgt"], e["t"])
    assert np.allclose(e["ve"], a[idx, D.TK["vE"]] - tr[:, 3], atol=1e-6)
    assert np.allclose(e["vn"], a[idx, D.TK["vN"]] - tr[:, 4], atol=1e-6)
    assert np.allclose(e["vu"], a[idx, D.TK["vU"]] - tr[:, 5], atol=1e-6)
    for k in VKEYS:
        assert A["contain"][k] == {"n": 0, "p1": None, "p3": None}
    assert set(A["contain"]) >= {"az", "el", "pos3d", "alt", "rng", *VKEYS}
    _lens_ok(A["errors_ung"])
    _lens_ok(A["errors_alt"])
    assert A["err_tracks"] and all(x["tid"] is not None for x in A["err_tracks"])
    for x in A["err_tracks"]:
        _lens_ok(x["errors"])
        _lens_ok(x["ungraded"])
    assert all(np.isnan(A["errors_ung"][k]).all() for k in SKEYS)


def test_live_fake_run_grades_velocity_with_real_sigma_and_containment(fake):
    """Live path end to end: block-143 velocity covariance (both layouts) -> (m,16) tracks -> spa's e_dot σ == the published
    σ on every graded row that had a covariance, NaN on the rows without one (never the adapter's constant fill), and
    A["contain"]["ve"|"vn"|"vu"] counts exactly the finite-σ rows of the display window."""
    st.session_state.clear()
    D.init_state()
    for k, v in {"source": "live", "live_host": "fake", "live_port": 27017, "live_db": "sensor_store", "live_run": FM.RUN_COLL,
                 "hist_s": 180, "spec_window": 120}.items():
        st.session_state[k] = v
    snap = F.live_snapshot(F1_MID, 180.0)
    assert snap["ok"], snap["err"]
    assert 177 in snap["tracks"] and all(a.shape[1] == D.TK_W for a in snap["tracks"].values())
    tr = snap["tracks"][177]
    have = np.isfinite(tr[:, D.TK["sE"]])
    assert have.sum() > 10 and (~have).sum() >= 1                                            # every 17th row has no p_cov
    assert np.allclose(tr[have][:, 13:16], FM.SIGV, atol=1e-6) and np.isnan(tr[~have][:, 13:16]).all()
    assert not np.any(tr[:, 13:16] == 0)
    A = E.analyze(snap, P)
    assert A["tgt_tid"] == 177
    e = A["errors"]
    _lens_ok(e)
    fin = np.isfinite(e["sig_ve"])
    assert fin.sum() > 10 and np.isfinite(e["ve"]).all()
    assert np.allclose(e["sig_ve"][fin], FM.SIGV[0]) and np.allclose(e["sig_vn"][fin], FM.SIGV[1]) and np.allclose(e["sig_vu"][fin], FM.SIGV[2])
    assert (np.isfinite(e["sig_vn"]) == fin).all() and (np.isfinite(e["sig_vu"]) == fin).all()
    assert not any(np.any(e[k] == 0) for k in SKEYS)
    # the σ-less graded rows are exactly the published states without a covariance
    idx = np.searchsorted(tr[:, 0], e["t"]).clip(0, len(tr) - 1)
    assert np.allclose(tr[idx, 0], e["t"], atol=1e-6) and (np.isfinite(tr[idx, D.TK["svE"]]) == fin).all()
    win = e["t"] >= snap["t_now"] - 120.0
    for k in VKEYS:
        c = A["contain"][k]
        assert c["n"] == int((np.isfinite(e[k]) & np.isfinite(e[f"sig_{k}"]) & win).sum()) > 0
        assert 0.0 <= c["p1"] <= c["p3"] <= 100.0
    # ungraded companion (permissive spa options): same keys, same rule for σ
    u = A["errors_ung"]
    _lens_ok(u)
    if len(u["t"]):
        assert not any(np.any(u[k] == 0) for k in SKEYS)


def _counts_direct(a: np.ndarray, t_now: float, W: float) -> dict:
    """engine.state_counts written out by hand from a raw TK track array (the reference the tests grade against)."""
    r = a[(a[:, 0] >= t_now - W) & (a[:, 0] <= t_now)]
    k = np.where(r[:, D.TK["t"]] - r[:, D.TK["lu"]] < D.MEAS_LAG_S, "meas", "coast").astype(object)
    k[r[:, D.TK["state"]] == 1] = "tent"
    span = min(float(W), float(t_now - r[0, 0]))
    return {"conf": int((k == "meas").sum()), "tent": int((k == "tent").sum()), "coast": int((k == "coast").sum()),
            "n": int(len(r)), "hz": len(r) / span, "span_s": span}


def test_state_counts_contract_and_edges():
    """engine.state_counts: counted off the RAW published states (TK layout), never off the graded rows;
    hz = n / min(window, t_now − first row in the window); zeros (never a raise / divide) on the empty edges."""
    z = {"conf": 0, "tent": 0, "coast": 0, "n": 0, "hz": 0.0, "span_s": 0.0}
    assert E.state_counts(None, 100.0, 60.0) == z and E.state_counts(np.zeros((0, D.TK_W)), 100.0, 60.0) == z
    a = np.zeros((6, D.TK_W))
    a[:, D.TK["t"]] = [50.0, 51.0, 52.0, 53.0, 54.0, 90.0]                       # the 90 s row is OUTSIDE a 60 s window ending at 100
    a[:, D.TK["lu"]] = [50.0, 51.0, 40.0, 53.0, 51.0, 90.0]                      # rows 2 and 4 are >= MEAS_LAG_S stale -> coasting
    a[:, D.TK["state"]] = [1, 2, 2, 1, 2, 2]                                     # rows 0 and 3 are TENTATIVE (state 1 wins over the lag)
    c = E.state_counts(a, 100.0, 60.0)
    assert (c["conf"], c["tent"], c["coast"], c["n"]) == (2, 2, 2, 6)             # 1,5 meas · 0,3 tent · 2,4 coast
    assert c["span_s"] == pytest.approx(50.0) and c["hz"] == pytest.approx(6 / 50.0)   # first row 50.0 -> 50 s of life, not the 60 s window
    c = E.state_counts(a, 100.0, 20.0)                                           # 20 s window: only the 90 s row survives
    assert (c["conf"], c["tent"], c["coast"], c["n"]) == (1, 0, 0, 1) and c["span_s"] == pytest.approx(10.0) and c["hz"] == pytest.approx(0.1)
    # a single row published AT t_now has zero span: rate 0.0, no ZeroDivisionError / inf
    one = np.zeros((1, D.TK_W))
    one[0, D.TK["t"]] = one[0, D.TK["lu"]] = 100.0
    one[0, D.TK["state"]] = 2
    assert E.state_counts(one, 100.0, 60.0) == {"conf": 1, "tent": 0, "coast": 0, "n": 1, "hz": 0.0, "span_s": 0.0}
    # rows OUTSIDE the window never count, and t > t_now never counts either
    fut = np.zeros((2, D.TK_W))
    fut[:, D.TK["t"]] = fut[:, D.TK["lu"]] = [100.0, 130.0]
    fut[:, D.TK["state"]] = 2
    assert E.state_counts(fut, 100.0, 60.0)["n"] == 1


def test_f1_replay_target_track_state_counter():
    """A["tgt_counts"] on the F1 archive replay == a hand count of track 177's quickdump rows in the metrics
    window; the counts partition the raw rows (conf + tent + coast == n) and follow spec_window."""
    for W in (60.0, 120.0, 300.0):
        snap = _f1_snap("07:22:31")
        A = E.analyze(snap, {**P, "spec_window": W})
        assert A["tgt_tid"] == 177
        c = A["tgt_counts"]
        assert c["conf"] + c["tent"] + c["coast"] == c["n"] == len(E._recent(A["tgt_track"], snap["t_now"], W))
        assert c == pytest.approx(_counts_direct(A["tgt_track"], snap["t_now"], W))
        # track 177's first state is 49 s before 07:22:31 -> every window rates it over those 49 s, ~2 Hz publish cadence
        assert (c["conf"], c["tent"], c["coast"], c["n"]) == (91, 3, 3, 97)
        assert c["span_s"] == pytest.approx(49.014, abs=0.01) and c["hz"] == pytest.approx(1.979, abs=0.001)
        # NOT the graded rows: spa grades fewer states than the track published
        assert len(A["errors"]["t"]) < c["n"]
    # a window the track outlives: 07:24:15 / track 203, 60 s vs 300 s must differ
    got = {}
    for W in (60.0, 300.0):
        E._BARE_STATE.clear()
        snap = _f1_snap("07:24:15")
        A = E.analyze(snap, {**P, "spec_window": W})
        assert A["tgt_tid"] == 203
        got[W] = A["tgt_counts"]
        assert A["tgt_counts"] == pytest.approx(_counts_direct(A["tgt_track"], snap["t_now"], W))
    assert got[60.0]["n"] == 119 and got[300.0]["n"] == 200 and got[60.0] != got[300.0]
    # no target track -> zeros (the status line then renders nothing extra)
    E._BARE_STATE.clear()
    snap = _f1_snap("07:20:30")
    A = E.analyze(snap, P)
    if A["tgt_tid"] is None:
        assert A["tgt_counts"] == {"conf": 0, "tent": 0, "coast": 0, "n": 0, "hz": 0.0, "span_s": 0.0}


def test_live_fake_run_target_track_state_counter(fake):
    """The LIVE path feeds the same counter: live block-143 rows -> (m,16) tracks -> A["tgt_counts"] equal to a
    hand count of the live rows in the window (the counter never reads the archive-only columns)."""
    st.session_state.clear()
    D.init_state()
    for k, v in {"source": "live", "live_host": "fake", "live_port": 27017, "live_db": "sensor_store", "live_run": FM.RUN_COLL,
                 "hist_s": 180, "spec_window": 120}.items():
        st.session_state[k] = v
    snap = F.live_snapshot(F1_MID, 180.0)
    assert snap["ok"], snap["err"]
    A = E.analyze(snap, P)
    assert A["tgt_tid"] == 177
    c = A["tgt_counts"]
    assert c == pytest.approx(_counts_direct(snap["tracks"][177], snap["t_now"], 120.0))
    assert c["conf"] > 10 and c["n"] == c["conf"] + c["tent"] + c["coast"] and 0.5 < c["hz"] < 20.0 and c["span_s"] > 0


def test_interceptor_only_truth_draws_and_reads_no_target_feed():
    """MRU91 live, 2026-09-15: the unit published MAVLink truth for the INTERCEPTOR only (feed "14551,mavlink_2"
    alive, no "14550,mavlink_1" target feed).  "No TARGET truth" is not "no truth": the F1 archive window with its
    target truth rows removed must still return has_truth True + an interceptor head, draw the interceptor trail and
    the radar tracks (grey, ungraded), read "NO TARGET FEED" (amber) — never "NO TRUTH FEED" / "NO TRACK" — and every
    card that grades against target truth must say WHY it is empty instead of "no samples yet"."""
    snap = dict(_f1_snap("07:22:31"))
    assert len(snap["tgt"]) and len(snap["itc"])                                   # the bundle carries both: this test removes one
    snap["tgt"], snap["tgt_hist"] = np.zeros((0, 7)), np.zeros((0, 7))
    snap["has_truth"] = bool(len(snap["tgt"]) or len(snap["itc"]))                 # feed.py's own derivation (len(tgt) or len(itc))
    A = E.analyze(snap, P)
    assert A["has_truth"] is True and A["has_itc_truth"] is True and A["has_tgt_truth"] is False and A["no_tgt_truth"] is True
    assert A["track_state"] == "NO TARGET FEED" and A["track_cls"] == "amber"
    assert A["tgt_tid"] is None and A["tgt_track"] is None and A["itc_tid"] is not None     # no invented target
    assert A["cpa"] is None and A["cpa_run"] is None and A["sep_now"] is None and not np.isfinite(A["sep"]["sep"]).any()
    h = PL.heads(A)
    assert h["tgt"] is None and h["itc"] is not None and A["itc_trail"] is not None and len(A["itc_trail"])
    # the map: interceptor truth trail + head, its radar track, and every other active track grey / dashed / ungraded
    fig = PL.map_fig(A, {**P, "map_half": 1500.0, "map_height": 640, "show_sat": False, "show_blind": False, "frame": None})
    names = [t.name for t in fig.data]
    assert "interceptor truth" in names and f"interceptor track #{A['itc_tid']}" in names
    assert names.count("radar tracks (no truth)") >= 5 and "radar track heads" in names
    grey = [t for t in fig.data if t.name == "radar tracks (no truth)"]
    assert all(t.line.color == T.GREY_TRACK and t.line.dash == "dash" for t in grey)
    tag = [a for a in fig.layout.annotations if (a.name or "") == "track_status"]
    assert len(tag) == 1 and tag[0].text == "<b>NO TARGET FEED</b>" and tag[0].font.color == T.AMBER
    # nothing graded, and the readouts say why (never PL.EMPTY_READOUT: that means "nothing YET")
    assert len(A["errors"]["t"]) == 0 and all(A["contain"][k]["n"] == 0 for k in (*VKEYS, "az", "el", "alt", "pos3d"))
    err = PL.error_fig(A, {**P, "err_height": 520})
    assert [a.text for a in err.layout.annotations if (a.name or "").startswith("readout_")] == [PL.NO_TGT_READOUT] * 4
    vel = PL.velocity_fig(A, {**P, "vel_height": 420}, truth=snap["tgt"])
    reads = [a.text for a in vel.layout.annotations if (a.name or "").startswith("readout_")]
    assert reads and set(reads) == {PL.NO_TGT_READOUT}
    sep = PL.separation_fig(A, {**P, "sep_height": 240})
    assert [a.text for a in sep.layout.annotations if (a.name or "") == "readout_sep"] == [PL.NO_TGT_SEP_READOUT]
    # the measurement space: the interceptor's truth + track keep their role, every other track is "free" (ungraded)
    ms = E.meas_space(snap, A, 120.0)
    assert ms["truth"] is None and ms["truth_itc"] is not None
    roles = {t["role"] for t in ms["tracks"]}
    assert roles == {"free", "interceptor"} and sum(1 for t in ms["tracks"] if t["role"] == "free") >= 5


def test_no_truth_at_all_still_reads_no_truth_feed():
    """The other empty state is unchanged in meaning and now says it in full: no MAVLink truth of any kind ->
    "NO TRUTH FEED", red (a truth-less run is not a track failure, but it is not an amber "target missing" either)."""
    snap = dict(_f1_snap("07:22:31"))
    for k in ("tgt", "itc", "tgt_hist", "itc_hist"):
        snap[k] = np.zeros((0, 7))
    snap["has_truth"] = False
    A = E.analyze(snap, P)
    assert A["has_truth"] is False and A["no_tgt_truth"] is False and A["tgt_tid"] is None
    assert (A["track_state"], A["track_cls"]) == ("NO TRUTH FEED", "fail")
    fig = PL.map_fig(A, {**P, "map_half": 1500.0, "map_height": 640, "show_sat": False, "show_blind": False, "frame": None})
    tag = [a for a in fig.layout.annotations if (a.name or "") == "track_status"]
    assert tag[0].text == "<b>NO TRUTH FEED</b>" and tag[0].font.color == T.FAIL
    err = PL.error_fig(A, {**P, "err_height": 520})
    assert [a.text for a in err.layout.annotations if (a.name or "").startswith("readout_")] == [PL.EMPTY_READOUT] * 4


# ── EVERY live drone on the map: target + interceptor + an UNASSIGNED third id (2026-09-15) ──────────────
UNASSIGNED_ID = "mav14552_3_0"


def _feed_rows(tid: str, t0: float, n: int, e0: float, n0: float, ve: float, vn: float) -> dict:
    """{(t, tid): (tid, TRUTH_COLS row)} buffer rows for one straight-flying MAVLink feed (1 Hz)."""
    out = {}
    for i in range(n):
        t = t0 + i
        dE, dN = e0 + ve * i, n0 + vn * i
        lat = D.ANT_LAT + dN / 111_320.0
        lon = D.ANT_LON + dE / (111_320.0 * float(np.cos(np.radians(D.ANT_LAT))))
        out[(round(t, 3), tid)] = (tid, (t, lat, lon, dE, dN, 120.0, ve, vn, 0.0, 1))
    return out


def _three_drone_buffers(t_now: float) -> dict:
    b = F._empty_rows()
    b["truth"].update(_feed_rows("mav14550_1_1", t_now - 29, 30, 300.0, 200.0, -5.0, 0.0))
    b["truth"].update(_feed_rows("mav14551_2_0", t_now - 29, 30, -300.0, 150.0, 6.0, 1.0))
    b["truth"].update(_feed_rows(UNASSIGNED_ID, t_now - 29, 30, 100.0, -400.0, 0.0, 8.0))
    b["lo"], b["hi"] = t_now - 29, t_now
    return b


def _snap_from_buffers(t_now: float) -> dict:
    tgt, itc, other, tracks, obs, obs_meta, feeds, has_truth, has_any = F._buffers_to_snapshot(_three_drone_buffers(t_now), t_now - 60.0, t_now)
    return {"ok": True, "err": None, "source": "live", "t_now": t_now, "ant": (D.ANT_LAT, D.ANT_LON, D.ANT_HAE),
            "tx": None, "tx_lla": None, "stale": False, "unit": "MRU91", "tgt": tgt, "itc": itc, "other": other,
            "tgt_hist": tgt, "itc_hist": itc, "tracks": tracks, "obs": obs, "obs_meta": obs_meta, "feeds": feeds,
            "track_meta": {}, "t_start": t_now - 60.0, "data_age": 0.0, "anchored": False,
            "has_truth": has_truth, "has_any_truth": has_any, "n_adsb": 0, "truth_unplaced": False}


def test_unassigned_mavlink_feed_reaches_the_snapshot():
    """A third drone whose target_id matches NEITHER auto-assign pattern ("14550,mavlink_1" / "14551,mavlink_2") must
    not be dropped and must not be merged into a role: feed.feed_role -> "unassigned", its rows come out in
    snap["other"] under their OWN id, it is listed in snap["feeds"] with role "unassigned", has_truth stays the
    target / interceptor truth and has_any_truth covers all three."""
    t_now = F1_MID
    assert F.feed_role("mav14550_1_1") == "target" and F.feed_role("mav14551_2_0") == "interceptor"
    assert F.feed_role(UNASSIGNED_ID) == F.UNASSIGNED and D.role_of(UNASSIGNED_ID) == "interceptor"   # D.role_of alone would merge it
    snap = _snap_from_buffers(t_now)
    assert len(snap["tgt"]) and len(snap["itc"])
    assert [k for k, _ in snap["other"]] == [UNASSIGNED_ID]
    rows = snap["other"][0][1]
    assert rows.shape[1] == 7 and len(rows) >= 25 and np.isfinite(rows).all()
    assert {f["name"]: f["role"] for f in snap["feeds"]}[UNASSIGNED_ID] == "unassigned"
    assert snap["has_truth"] is True and snap["has_any_truth"] is True
    # the unassigned rows never leak into the role truths (its N is uniquely negative here)
    assert (snap["tgt"][:, 2] > 0).all() and (snap["itc"][:, 2] > 0).all()


def test_unassigned_drone_passes_through_the_engine_and_draws_grey():
    """analyze() passes the unassigned drone through as A["other_truth"] (position / heading / speed / age / trail,
    no role, nothing graded) and the map draws a GREY trail, a grey head marker and an id pill for it."""
    t_now = F1_MID
    snap = _snap_from_buffers(t_now)
    A = E.analyze(snap, P)
    assert A["n_unassigned"] == 1 and A["unassigned_txt"] == "1 UNASSIGNED FEED" and A["has_any_truth"] is True
    o = A["other_truth"][0]
    assert o["id"] == UNASSIGNED_ID and o["age"] <= E.OTHER_MAX_AGE_S and len(o["trail"])
    assert abs(o["hdg"] - 0.0) < 1e-6 and abs(o["spd"] - 8.0) < 0.5          # due north at 8 m/s
    assert abs(o["E"] - 100.0) < 1.0 and abs(o["N"] - (-400.0 + 8.0 * 29)) < 2.0
    # no role invented, and the graded path is untouched by the third drone
    assert A["has_tgt_truth"] and A["has_itc_truth"] and A["no_tgt_truth"] is False
    assert UNASSIGNED_ID not in (A.get("role_ids") or {}).get("target", []) + (A.get("role_ids") or {}).get("interceptor", [])
    fig = PL.map_fig(A, {**P, "map_half": 1500.0, "map_height": 640, "show_sat": False, "show_blind": False, "frame": None})
    names = [t.name for t in fig.data]
    assert "target truth" in names and "interceptor truth" in names
    grey = [t for t in fig.data if t.name == "unassigned drones"]
    assert len(grey) == 1 and grey[0].line.color == T.GREY_TRACK and len(grey[0].x) >= 2
    head = [t for t in fig.data if t.name == "unassigned drone heads"]
    assert len(head) == 1 and head[0].mode == "markers" and head[0].marker.color == T.GREY_TRACK
    assert abs(float(head[0].x[0]) - o["E"]) < 0.5 and abs(float(head[0].y[0]) - o["N"]) < 0.5
    pills = [a for a in fig.layout.annotations if (a.name or "").startswith(PL.OTH_PILL_NAME)]
    assert [a.text for a in pills] == [f"{UNASSIGNED_ID} · unassigned"] and pills[0].bordercolor == T.GREY_TRACK
    # the panel's DOM-overlay feeds are untouched (two slots only): the unassigned pill lives IN the figure
    assert set(PL.heads(A)) == {"tgt", "itc"} and all(not str(p["name"]).startswith(PL.OTH_PILL_NAME) for p in PL.track_pills(A, P))
    assert not any((a.name or "").startswith("pill_") and UNASSIGNED_ID in str(a.text) for a in fig.layout.annotations)
    # the map's track_status tag is unchanged by the extra drone
    tag = [a for a in fig.layout.annotations if (a.name or "") == "track_status"]
    assert len(tag) == 1 and "UNASSIGNED" not in tag[0].text


def test_unassigned_drone_draws_with_no_role_truth_at_all():
    """The interceptor-only / no-truth empty states must still show an unassigned drone (it is the ONLY thing
    flying): grey trail + head + pill, and the status words stay the honest role-truth words."""
    t_now = F1_MID
    snap = _snap_from_buffers(t_now)
    snap["tgt"], snap["tgt_hist"] = np.zeros((0, 7)), np.zeros((0, 7))
    snap["itc"], snap["itc_hist"] = np.zeros((0, 7)), np.zeros((0, 7))
    snap["has_truth"] = False
    A = E.analyze(snap, P)
    assert A["has_truth"] is False and A["has_any_truth"] is True and A["n_unassigned"] == 1
    assert A["track_state"] == "NO TRUTH FEED"                       # unchanged: an unassigned feed is not role truth
    fig = PL.map_fig(A, {**P, "map_half": 1500.0, "map_height": 640, "show_sat": False, "show_blind": False, "frame": None})
    names = [t.name for t in fig.data]
    assert "unassigned drones" in names and "unassigned drone heads" in names
    assert [a.text for a in fig.layout.annotations if (a.name or "").startswith(PL.OTH_PILL_NAME)] == [f"{UNASSIGNED_ID} · unassigned"]


def test_map_hover_carries_altitude_for_every_vehicle():
    """The map hover names the ALTITUDE (and ground speed) of every vehicle: target / interceptor / unassigned truth
    trails and every radar-track trace.  BOTH truth and track U are metres above the RADAR (TRUTH_COLS U_m_hae is the
    ENU up of an HAE altitude, not an absolute one), so the antenna HAE is added to both and the hover reads one
    absolute scale.  Checked on the F1 archive figure (target + interceptor + tracks)."""
    snap = _f1_snap("07:22:31")
    A = E.analyze(snap, P)
    fig = PL.map_fig(A, {**P, "map_half": 1500.0, "map_height": 640, "show_sat": False, "show_blind": False, "frame": None})
    vehicles = [t for t in fig.data if t.hovertemplate and ("truth" in str(t.name) or "track" in str(t.name) or "unassigned" in str(t.name))]
    assert len(vehicles) >= 3
    for t in vehicles:
        assert "alt %{customdata[0]:.0f} m HAE" in t.hovertemplate, t.name
        cd = np.asarray(t.customdata, float)
        assert cd.ndim == 2 and cd.shape[1] == 2 and len(cd) == len(t.x)
        assert np.isfinite(cd[:, 0]).any()
    # a track's altitude is HAE (radar-relative U + antenna HAE), so it sits within a few hundred m of the truth's
    tk = next(t for t in fig.data if "track #" in str(t.name))
    tt = next(t for t in fig.data if str(t.name) == "target truth")
    assert abs(float(np.asarray(tt.customdata, float)[:, 0].max()) - (float(A["tgt_trail"][:, 3].max()) + D.ANT_HAE)) < 1.0
    assert abs(float(np.nanmedian(np.asarray(tk.customdata, float)[:, 0]) - np.nanmedian(np.asarray(tt.customdata, float)[:, 0]))) < 300.0
    # the unassigned drone's hover too (synthetic third feed, HAE straight from its own rows)
    A2 = E.analyze(_snap_from_buffers(F1_MID), P)
    f2 = PL.map_fig(A2, {**P, "map_half": 1500.0, "map_height": 640, "show_sat": False, "show_blind": False, "frame": None})
    oth = [t for t in f2.data if str(t.name).startswith("unassigned")]
    assert len(oth) == 2 and all("alt %{customdata[0]:.0f} m HAE" in t.hovertemplate for t in oth)
    assert abs(float(np.asarray(oth[1].customdata, float)[0, 0]) - (120.0 + D.ANT_HAE)) < 1e-6   # head marker: U above the radar + antenna HAE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
