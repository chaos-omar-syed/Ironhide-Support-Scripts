"""Engine C1–C4 (2026-09-10): track selection precedence, joint assignment, hysteresis, track-change events.

Bare-Python tests (no Streamlit run): engine.analyze keeps its per-session state in engine._BARE_STATE when
there is no script-run context, so each test clears it.  Two data paths:

* a SYNTHETIC snapshot (analyze's input contract incl. snap["track_meta"] = {tid: {truth_match_id,
  truth_match_conf, contributors}}) — two truths ``sep`` m apart (E), both heading north, tracks riding a truth
  at a fixed horizontal offset so side_scores == the offset (sep = 100 m puts every track inside BOTH gates —
  the contested-pass case; sep = 600 m isolates one role);
* the F1 ARCHIVE replay through feed.archive_snapshot (the replay data path the Live page uses) — the
  129 -> 177 handover at 07:21:57 and the CPA gate sanity points of tests/test_apptest.py.

Run:  cd ironhide_dashboard && <sensorenv python> -m pytest tests/test_engine_selection.py -q
"""
from __future__ import annotations

import logging
import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
logging.disable(logging.WARNING)   # bare-mode streamlit session-state warnings

import streamlit as st  # noqa: E402

from ih import data as D  # noqa: E402  (puts track_correlation + chaos-spa on sys.path)
from ih import engine as E  # noqa: E402

T0 = 1_800_000_000.0
ANT = (33.748, -115.339, 140.0)
TGT0 = (1000.0, 0.0, 100.0)          # target truth at t_now; interceptor truth = TGT0 + (sep, 0, 0); both heading north at 20 m/s
VEL = (0.0, 20.0)
P = {"trail_s": 20, "spec_window": 120, "cpa_gate_m": 70.0}
MAV_T, MAV_I, ADSB = "mavlink_14550", "mavlink_14551", "N432R"
NULL = (None, float("nan"))


@pytest.fixture(autouse=True)
def _fresh_engine_state():
    E._BARE_STATE.clear()
    yield
    E._BARE_STATE.clear()


# ── synthetic snapshot ────────────────────────────────────────────────────────
def truth(t_now: float, pos=TGT0, vel=VEL, dur: float = 60.0, hz: float = 10.0) -> np.ndarray:
    t = np.arange(t_now - dur, t_now + 1e-9, 1.0 / hz)
    dt = t - t_now
    return np.column_stack([t, pos[0] + vel[0] * dt, pos[1] + vel[1] * dt, np.full_like(t, pos[2]),
                            np.full_like(t, vel[0]), np.full_like(t, vel[1]), np.zeros_like(t)])


def track(t_now: float, T: np.ndarray, off: tuple, dur: float = 15.0, hz: float = 2.0, state: int = 2) -> np.ndarray:
    """A TK-layout track riding truth T at a constant horizontal offset (dE, dN), fresh measurement states up to t_now."""
    t = np.arange(t_now - dur, t_now + 1e-9, 1.0 / hz)
    tr = E.interp_truth(T, t)
    return np.column_stack([t, tr[:, 0] + off[0], tr[:, 1] + off[1], tr[:, 2], np.full_like(t, 5.0), np.full_like(t, 5.0),
                            np.full_like(t, 8.0), t - 0.1, np.arange(len(t)) + 1.0, np.full_like(t, float(state)), tr[:, 3], tr[:, 4], tr[:, 5]])


def meta(mid, conf=float("nan")) -> dict:
    return {"truth_match_id": mid, "truth_match_conf": conf, "contributors": None}


def snap_of(t_now: float, tracks: dict, track_meta: dict | None, Tt: np.ndarray, Ti: np.ndarray, *, with_meta_key: bool = True) -> dict:
    s = {"ok": True, "err": None, "source": "test", "t_now": t_now, "ant": ANT, "tx": None, "stale": False, "label": "TEST",
         "tgt": Tt, "itc": Ti, "tgt_hist": Tt, "itc_hist": Ti, "tracks": tracks, "obs": np.zeros((0, D.OBS_COLS)), "feeds": [],
         "t_start": t_now - 60.0, "data_age": 0.0, "anchored": False, "has_truth": True, "n_adsb": 0}
    if with_meta_key:
        s["track_meta"] = dict(track_meta or {})
    return s


def scene(t_now: float, spec: dict, sep: float = 100.0) -> tuple[dict, dict, np.ndarray, np.ndarray]:
    """spec = {tid: (("tgt"|"itc", dE, dN), match_id | None, conf)} -> (tracks, track_meta, Tt, Ti)."""
    Tt, Ti = truth(t_now, TGT0), truth(t_now, (TGT0[0] + sep, TGT0[1], TGT0[2]))
    tracks, tm = {}, {}
    for tid, (where, mid, conf) in spec.items():
        side, dE, dN = where
        tracks[tid] = track(t_now, Tt if side == "tgt" else Ti, (dE, dN))
        if mid is not None or not math.isnan(conf):
            tm[tid] = meta(mid, conf)
    return tracks, tm, Tt, Ti


def run(t_now: float, spec: dict, params: dict = P, sep: float = 100.0, with_meta: bool = True, meta_override: dict | None = None) -> dict:
    tracks, tm, Tt, Ti = scene(t_now, spec, sep)
    return E.analyze(snap_of(t_now, tracks, tm if meta_override is None else meta_override, Tt, Ti, with_meta_key=with_meta), params)


def ev_of(A: dict, role: str) -> list[dict]:
    return [e for e in A["track_events"] if e["role"] == role]


# ── C1: precedence · exclusion · joint distinct assignment ───────────────────
def test_precedence_radar_match_beats_nearer_adsb_and_position_gate():
    """101 = MAVLink-matched target 20 m off; 102 = ADS-B-matched (N432R) 14 m off the target (NEAREST!) -> excluded;
    103 = null match 30 m off the interceptor -> position gate; 104 = decoy 2 km away."""
    spec = {101: (("tgt", 20, 0), MAV_T, 0.93), 102: (("tgt", 10, 10), ADSB, 1.0), 103: (("itc", -30, 0), *NULL), 104: (("tgt", 2000, 0), *NULL)}
    A = run(T0, spec)
    assert (A["tgt_tid"], A["tgt_rule"]) == (101, "radar match conf 0.93")
    assert (A["itc_tid"], A["itc_rule"]) == (103, "position gate")
    assert [(e["tid"], e["match_id"], e["rule"]) for e in A["excluded_tracks"]] == [(102, ADSB, "excluded ADS-B N432R")]
    assert A["excluded_tracks"][0]["d"] < 15.0 and A["excluded_tracks"][0]["conf"] == 1.0
    assert 104 not in {c["tid"] for c in A["tgt_cands"] + A["itc_cands"]}
    assert A["alt_tid"] is None, "the excluded airliner must not become the second graded target-side track"
    assert A["role_ids"] == {"target": [], "interceptor": []}
    # scores are the plain side distances (offsets), unfiltered
    assert abs(A["scores"][101] - 20.0) < 0.5 and abs(A["scores"][102] - math.hypot(10, 10)) < 0.5 and abs(A["iscores"][103] - 30.0) < 0.5
    # first acquisition of both roles -> two events, both with the rule that made the pick, no flash (old is None)
    assert [(e["role"], e["old"], e["new"], e["rule"]) for e in A["track_events"]] == \
        [("target", None, 101, "radar match conf 0.93"), ("interceptor", None, 103, "position gate")]
    assert A["tgt_flash"] is False and A["itc_flash"] is False
    assert A["tgt_change"] == A["track_events"][0] and A["itc_change"] == A["track_events"][1]
    assert A["cpa"] is None and A["errors"]["grader"] in ("spa", "legacy")   # CPA gate path untouched (100 m constant separation, no CPA)
    # the meas quad never draws the excluded airliner as a "target-side" line ...
    tracks, tm, Tt, Ti = scene(T0, spec)
    M = E.meas_space(snap_of(T0, tracks, tm, Tt, Ti), A, 30.0)
    assert {t["tid"] for t in M["tracks"]} == {101, 103}
    # ... and carries role-independent per-sample side flags (on_tgt: dT < 150 & dT <= dI; on_itc: dI < 150 & dI < dT; never both)
    for tr in M["tracks"]:
        n = len(tr["t"])
        assert tr["on_tgt"].shape == (n,) and tr["on_itc"].shape == (n,) and tr["on_tgt"].dtype == bool and tr["on_itc"].dtype == bool
        assert not np.any(tr["on_tgt"] & tr["on_itc"])
        assert np.array_equal(tr["on"], tr["on_tgt"] if tr["role"] == "target" else tr["on_itc"])
    by = {t["tid"]: t for t in M["tracks"]}
    assert by[101]["role"] == "target" and by[101]["on_tgt"].all() and not by[101]["on_itc"].any()
    assert by[103]["role"] == "interceptor" and by[103]["on_itc"].all() and not by[103]["on_tgt"].any()


def test_without_track_meta_everything_is_a_position_gate_pick():
    """B's fields absent (no snap["track_meta"] key at all) -> null matches everywhere: the nearest track wins, nothing is excluded."""
    spec = {101: (("tgt", 20, 0), MAV_T, 0.93), 102: (("tgt", 10, 10), ADSB, 1.0), 103: (("itc", -30, 0), *NULL)}
    A = run(T0, spec, with_meta=False)
    assert (A["tgt_tid"], A["tgt_rule"], A["itc_tid"], A["itc_rule"]) == (102, "position gate", 103, "position gate")
    assert A["excluded_tracks"] == [] and A["alt_tid"] == 101
    # tolerant of malformed meta entries too (NaN id from a CSV, a non-dict, a str key, an empty id)
    A2 = run(T0 + 1, spec, meta_override={102: meta(float("nan")), 101: "junk", "103": meta("")})
    assert (A2["tgt_tid"], A2["itc_tid"]) == (102, 103) and A2["excluded_tracks"] == []


def test_adsb_match_is_a_hard_exclusion_from_both_roles():
    """The only track sits 14 m off the target (90 m off the interceptor) but the radar matched it to an airliner:
    both roles stay empty and both rules explain why."""
    A = run(T0, {102: (("tgt", 10, 10), ADSB, 0.66)})
    assert A["tgt_tid"] is None and A["itc_tid"] is None
    assert A["tgt_rule"] == "excluded ADS-B N432R" and A["itc_rule"] == "excluded ADS-B N432R"
    assert A["track_events"] == [] and A["track_state"] == "NO TRACK"
    # an airliner far from both truths is listed (for the UI) but is not the empty-role reason
    A2 = run(T0 + 1, {102: (("tgt", 10, 10), ADSB, 0.66), 105: (("tgt", 3000, 0), "UAL123", 0.9)})
    assert [e["tid"] for e in A2["excluded_tracks"]] == [102, 105] and A2["tgt_rule"] == "excluded ADS-B N432R"
    # a track that was the target and then gets an ADS-B match drops out with the exclusion as the event rule
    E._BARE_STATE.clear()
    A3 = run(T0 + 2, {102: (("tgt", 10, 10), *NULL)})
    assert A3["tgt_tid"] == 102
    A4 = run(T0 + 3, {102: (("tgt", 10, 10), ADSB, 0.66)})
    assert A4["tgt_tid"] is None and ev_of(A4, "target")[-1] == {"t": T0 + 3, "role": "target", "old": 102, "new": None, "rule": "excluded ADS-B N432R"}


def test_joint_assignment_never_gives_one_track_to_both_roles():
    """One null-match track nearest to BOTH truths: it goes to the truth it is closer to, the other role is empty."""
    A = run(T0, {301: (("tgt", 30, 0), *NULL)})                 # 30 m off target, 70 m off interceptor
    assert (A["tgt_tid"], A["itc_tid"]) == (301, None) and A["itc_rule"] == "no track in gate"
    E._BARE_STATE.clear()
    A = run(T0, {301: (("tgt", 60, 0), *NULL)})                 # 60 m off target, 40 m off interceptor
    assert (A["tgt_tid"], A["itc_tid"]) == (None, 301) and A["tgt_rule"] == "no track in gate"
    # three tracks, X nearest to both: the DISTINCT pair with the least summed distance wins ((X, Z) = 30 + 120 < (Y, X) = 100 + 70)
    E._BARE_STATE.clear()
    A = run(T0, {1: (("tgt", 30, 0), *NULL), 2: (("tgt", -100, 0), *NULL), 3: (("itc", 120, 0), *NULL)})
    assert A["tgt_tid"] != A["itc_tid"] and (A["tgt_tid"], A["itc_tid"]) == (1, 3)
    ts, is_ = A["scores"], A["iscores"]
    pairs = [(ts[a] + is_[b], a, b) for a in ts for b in is_ if a != b and ts[a] < D.GATE_M and is_[b] < D.GATE_M]
    assert min(pairs)[1:] == (1, 3)
    # the radar's claim on a track outranks the interceptor's geometric pull on it: X matched TARGET, 60 m off the
    # target and 40 m off the interceptor -> X is the target, the interceptor takes nothing (no steal by proximity)
    E._BARE_STATE.clear()
    A = run(T0, {301: (("tgt", 60, 0), MAV_T, 0.55)})
    assert (A["tgt_tid"], A["tgt_rule"], A["itc_tid"]) == (301, "radar match conf 0.55", None)


def test_track_matched_to_the_other_role_is_a_handicapped_position_gate_candidate():
    """Two tracks both radar-matched to the TARGET (a duplicate), X 20 m / Y 40 m off it (80 / 60 m off the
    interceptor): Y fills the empty interceptor slot by position (there is nothing better) but loses it to a
    null-match track Z 120 m off the interceptor — twice as far — because Y's match points elsewhere."""
    A = run(T0, {1: (("tgt", 20, 0), MAV_T, 0.9), 2: (("tgt", 40, 0), MAV_T, 0.8)})
    assert (A["tgt_tid"], A["tgt_rule"], A["itc_tid"], A["itc_rule"]) == (1, "radar match conf 0.90", 2, "position gate")
    E._BARE_STATE.clear()
    A = run(T0, {1: (("tgt", 20, 0), MAV_T, 0.9), 2: (("tgt", 40, 0), MAV_T, 0.8), 3: (("itc", 120, 0), *NULL)})
    assert (A["tgt_tid"], A["itc_tid"], A["itc_rule"]) == (1, 3, "position gate") and A["alt_tid"] == 2
    y = next(c for c in A["itc_cands"] if c["tid"] == 2)
    assert y["other_role"] is True and abs(y["cost"] - (60.0 + E.OTHER_ROLE_PENALTY_M)) < 1.0


def test_priority_candidate_allowed_to_1p5_gate_only():
    A = run(T0, {7: (("tgt", 200, 0), MAV_T, 0.5)}, sep=600)            # 200 m > GATE, < 1.5 x GATE
    assert (A["tgt_tid"], A["tgt_rule"]) == (7, "radar match conf 0.50")
    E._BARE_STATE.clear()
    A = run(T0, {7: (("tgt", 240, 0), MAV_T, 0.5)}, sep=600)            # beyond 1.5 x GATE
    assert (A["tgt_tid"], A["tgt_rule"]) == (None, "no track in gate")
    # a null-match track at the same 200 m is never a candidate
    E._BARE_STATE.clear()
    assert run(T0, {7: (("tgt", 200, 0), *NULL)}, sep=600)["tgt_tid"] is None
    # a match without a confidence string still reads as a radar match
    E._BARE_STATE.clear()
    assert run(T0, {7: (("tgt", 20, 0), MAV_T, float("nan"))}, sep=600)["tgt_rule"] == "radar match"


def test_role_ids_param_drives_match_roles_and_pattern_fallback():
    """P["role_ids"] (the Data-source assignment) decides which MAVLink id is which role; ids in neither list fall
    back to the session patterns (14550 target / 14551 interceptor); a swap swaps the picks."""
    spec = {301: (("tgt", 20, 0), MAV_T, 0.9), 302: (("itc", 20, 0), MAV_I, 0.8)}
    A = run(T0, spec)
    assert (A["tgt_tid"], A["itc_tid"]) == (301, 302) and A["tgt_rule"] == "radar match conf 0.90" and A["itc_rule"] == "radar match conf 0.80"
    E._BARE_STATE.clear()
    swapped = {"target": [MAV_I], "interceptor": [MAV_T]}
    A = run(T0, spec, dict(P, role_ids=swapped))
    assert (A["tgt_tid"], A["itc_tid"]) == (302, 301) and A["role_ids"] == swapped
    assert A["tgt_rule"] == "radar match conf 0.80" and A["itc_rule"] == "radar match conf 0.90"
    # explicit lists that name neither id: MAVLink-looking ids still get a role by pattern, never excluded
    E._BARE_STATE.clear()
    A = run(T0, spec, dict(P, role_ids={"target": ["mav14550_1_1"], "interceptor": ["mav14551_2_0"]}))
    assert (A["tgt_tid"], A["itc_tid"]) == (301, 302) and A["excluded_tracks"] == []
    # helper semantics
    assert E.match_role(None, None) is None and E.match_role("", None) is None and E.match_role(float("nan"), None) is None
    assert E.match_role("N432R", {"target": [MAV_T]}) == "other" and E.match_role("mav14550_1_1", {}) == "target"
    assert E.match_role("mavlink_14551", {"target": ["mavlink_14551"]}) == "target"
    assert E.is_mavlink_id("MAVLINK_14551") and not E.is_mavlink_id("N432R") and not E.is_mavlink_id(None)
    assert E.role_ids_of({}) == {"target": [], "interceptor": []} and E.role_ids_of({"role_ids": {"target": [14550]}}) == {"target": ["14550"], "interceptor": []}


def test_select_tracks_direct_api_and_empty_inputs():
    tracks, tm, Tt, Ti = scene(T0, {101: (("tgt", 20, 0), MAV_T, 0.93), 103: (("itc", -30, 0), *NULL)})
    sel = E.select_tracks(tracks, Tt, Ti, T0, None, None, tm, {"target": [], "interceptor": []})
    assert (sel["tgt_tid"], sel["itc_tid"], sel["tgt_rule"], sel["itc_rule"]) == (101, 103, "radar match conf 0.93", "position gate")
    assert [c["tid"] for c in sel["tgt_cands"]][0] == 101 and sel["tgt_cands"][0]["prio"] is True and sel["excluded"] == []
    # no interceptor truth -> no interceptor candidates; no tracks -> nothing
    sel = E.select_tracks(tracks, Tt, np.zeros((0, 7)), T0, None, None, tm, None)
    assert (sel["tgt_tid"], sel["itc_tid"], sel["itc_rule"]) == (101, None, "no track in gate")
    sel = E.select_tracks({}, Tt, Ti, T0, 5, 6, None, None)
    assert (sel["tgt_tid"], sel["itc_tid"]) == (None, None) and sel["scores"] == {} and sel["excluded"] == []
    # stale tracks (no state in the last 3 s) are not candidates
    old = {101: track(T0 - 5.0, Tt, (20, 0))}
    assert E.select_tracks(old, Tt, Ti, T0, None, None, tm, None)["tgt_tid"] is None
    # joint_assign: a single shared candidate goes to the role where it is cheaper, the other role stays empty
    x_t, x_i = {"tid": 9, "cost": 60.0}, {"tid": 9, "cost": 40.0}
    assert E.joint_assign([x_t], [x_i]) == (None, x_i) and E.joint_assign([x_t], []) == (x_t, None) and E.joint_assign([], []) == (None, None)


# ── C1 hysteresis + C2 events (sep = 600 m: the interceptor gate is out of play) ─────────────────
def test_hysteresis_keeps_prev_inside_band_and_priority_overrides_it():
    t = T0
    A = run(t, {201: (("tgt", 30, 0), *NULL)}, sep=600)
    assert (A["tgt_tid"], A["tgt_rule"]) == (201, "position gate")
    # tick 2: 202 is closer (25 m) but 201 (40 m) is inside 1.5 x 25 + 20 = 57.5 m -> kept, rule "hysteresis", no event
    t += 1
    A = run(t, {201: (("tgt", 40, 0), *NULL), 202: (("tgt", 25, 0), *NULL)}, sep=600)
    assert (A["tgt_tid"], A["tgt_rule"]) == (201, "hysteresis") and len(ev_of(A, "target")) == 1 and A["alt_tid"] == 202
    # tick 3: 201 drifts to 120 m -> outside the band -> handover to 202, one event carrying the new rule
    t += 1
    A = run(t, {201: (("tgt", 120, 0), *NULL), 202: (("tgt", 25, 0), *NULL)}, sep=600)
    assert (A["tgt_tid"], A["tgt_rule"]) == (202, "position gate")
    assert ev_of(A, "target")[-1] == {"t": float(t), "role": "target", "old": 201, "new": 202, "rule": "position gate"}
    assert A["tgt_flash"] is True and A["tgt_change"] == ev_of(A, "target")[-1]
    # tick 4: 202 still nearest & sticky, but 203 (60 m) carries the radar's TARGET match -> priority wins over hysteresis
    t += 1
    A = run(t, {202: (("tgt", 25, 0), *NULL), 203: (("tgt", 60, 0), MAV_T, 0.71)}, sep=600)
    assert (A["tgt_tid"], A["tgt_rule"]) == (203, "radar match conf 0.71")
    assert ev_of(A, "target")[-1] == {"t": float(t), "role": "target", "old": 202, "new": 203, "rule": "radar match conf 0.71"}
    # tick 5: unchanged -> no new event (paused replays rerun the same tick); flash still on (< 10 s)
    n = len(A["track_events"])
    A = run(t, {202: (("tgt", 25, 0), *NULL), 203: (("tgt", 60, 0), MAV_T, 0.71)}, sep=600)
    assert len(A["track_events"]) == n and A["tgt_flash"] is True
    # 11 s later, same picks: the flash is over
    t += 11
    A = run(t, {202: (("tgt", 25, 0), *NULL), 203: (("tgt", 60, 0), MAV_T, 0.71)}, sep=600)
    assert len(A["track_events"]) == n and A["tgt_flash"] is False and ev_of(A, "interceptor") == []
    # a sticky priority track is not out-voted by a marginally closer priority one
    E._BARE_STATE.clear()
    t += 1
    run(t, {203: (("tgt", 40, 0), MAV_T, 0.7)}, sep=600)
    t += 1
    A = run(t, {203: (("tgt", 40, 0), MAV_T, 0.7), 204: (("tgt", 30, 0), MAV_T, 0.7)}, sep=600)
    assert A["tgt_tid"] == 203 and A["tgt_rule"] == "radar match conf 0.70"


def test_interceptor_hysteresis_is_independent_of_the_target_pick():
    """Contested pass (sep = 100 m): the interceptor keeps its sticky track inside its own band even when the
    target's best candidate is that same track — the target then takes its next in-gate candidate."""
    t = T0
    A = run(t, {11: (("tgt", 30, 0), *NULL), 12: (("itc", -40, 0), *NULL)})           # 12: 60 m off target, 40 m off interceptor
    assert (A["tgt_tid"], A["itc_tid"]) == (11, 12)
    t += 1
    A = run(t, {11: (("tgt", 140, 0), *NULL), 12: (("tgt", 30, 0), *NULL)})             # 12 now 30 m off target / 70 m off interceptor; 11 40 m off interceptor
    assert (A["tgt_tid"], A["tgt_rule"], A["itc_tid"], A["itc_rule"]) == (11, "position gate", 12, "hysteresis")
    assert A["tgt_tid"] != A["itc_tid"] and len(A["track_events"]) == 2
    t += 1
    A = run(t, {11: (("tgt", 140, 0), *NULL), 12: (("tgt", 30, 0), *NULL), 13: (("itc", 10, 0), *NULL)})   # a fresh track on the interceptor
    assert (A["tgt_tid"], A["itc_tid"]) == (12, 13)                                       # 12's band gone (1.5 x 10 + 20 = 35 < 70) -> the pair swaps
    assert {(e["role"], e["old"], e["new"]) for e in A["track_events"][2:]} == {("target", 11, 12), ("interceptor", 12, 13)}


def test_track_events_session_log_capped_at_50_newest_last_and_loss_logged():
    t = T0
    for i in range(60):   # flip the target between 11 and 12 every tick (the loser sits far outside the hysteresis band)
        near, far = (11, 12) if i % 2 == 0 else (12, 11)
        A = run(t + i, {near: (("tgt", 30, 0), *NULL), far: (("tgt", 140, 0), *NULL)}, sep=600)
    ev = A["track_events"]
    assert len(ev) == E.TRACK_EVENTS_CAP == 50 and ev is not E._BARE_STATE["track_events"] and ev == E._BARE_STATE["track_events"]
    assert ev[-1]["t"] == float(t + 59) and all(ev[i]["t"] <= ev[i + 1]["t"] for i in range(len(ev) - 1))
    assert all(set(e) == {"t", "role", "old", "new", "rule"} for e in ev) and {e["role"] for e in ev} == {"target"}
    # a lost track logs new None with the empty-role rule
    A = run(t + 60, {99: (("tgt", 900, 0), *NULL)}, sep=600)
    assert A["track_events"][-1]["new"] is None and A["track_events"][-1]["rule"] == "no track in gate" and A["tgt_tid"] is None
    # an interceptor id change is logged with its own role/rule
    A = run(t + 61, {103: (("itc", -30, 0), *NULL)}, sep=600)
    assert ev_of(A, "interceptor")[-1] == {"t": float(t + 61), "role": "interceptor", "old": None, "new": 103, "rule": "position gate"}


def test_events_live_in_session_state_with_bare_fallback_and_legacy_tuples_convert():
    assert E.session_state() is E._BARE_STATE                    # no Streamlit script-run context here
    E._BARE_STATE["track_events"] = [(T0 - 5.0, "target", None, 5), (T0 - 4.0, "interceptor", 5, None)]   # an older session's tuples
    E._BARE_STATE["tgt_tid"] = 5
    A = run(T0, {201: (("tgt", 30, 0), *NULL)}, sep=600)
    ev = A["track_events"]
    assert ev[:2] == [{"t": T0 - 5.0, "role": "target", "old": None, "new": 5, "rule": ""},
                      {"t": T0 - 4.0, "role": "interceptor", "old": 5, "new": None, "rule": ""}]
    assert ev[2] == {"t": T0, "role": "target", "old": 5, "new": 201, "rule": "position gate"}
    assert E._BARE_STATE["tgt_tid"] == 201 and E._BARE_STATE["_itc_tid_prev"] is None
    assert E._BARE_STATE["track_events"] == ev and st.session_state.get("track_events") in (None, [])   # nothing leaks into the streamlit mock


# ── F1 archive replay: the 129 -> 177 handover event and the (untouched) CPA gate ────────────────
def _f1_snap(hms: str) -> dict:
    from ih import feed as F

    st.session_state["flight"] = 1
    st.session_state["spec_window"] = 120
    return F.archive_snapshot(D.hms_to_epoch(hms), 180.0)


def test_f1_replay_emits_target_129_to_177_event_between_072150_and_072200():
    """Replaying F1 tick by tick from 07:21:44: the target track hands over 129 -> 177 (position gate, no radar
    match in the 8/28 quickdumps) at 07:21:57 and track 129 — dragged onto the interceptor — becomes the
    interceptor's track at the same tick (the 'steal', now visible as an interceptor event)."""
    A = None
    for sec in range(44, 68):
        hms = f"07:21:{sec:02d}" if sec < 60 else f"07:22:{sec - 60:02d}"
        A = E.analyze(_f1_snap(hms), P)
        if hms == "07:21:48":
            assert (A["itc_tid"], A["itc_rule"]) == (177, "hysteresis")        # 177 kept as interceptor while 129 is marginally closer
        if hms == "07:21:55":
            assert (A["tgt_tid"], A["tgt_rule"]) == (129, "hysteresis")        # 177 now closer to the target, 129 still inside the band
        if hms == "07:21:58":
            assert A["tgt_flash"] is True and A["tgt_change"]["old"] == 129
        assert A["tgt_tid"] is None or A["tgt_tid"] != A["itc_tid"]
    ev = [e for e in A["track_events"] if e["role"] == "target" and e["old"] == 129 and e["new"] == 177]
    assert len(ev) == 1 and D.hms_to_epoch("07:21:50") <= ev[0]["t"] <= D.hms_to_epoch("07:22:00") and ev[0]["rule"] == "position gate"
    assert D.pdt_hms(ev[0]["t"]) == "07:21:57"
    steal = [e for e in A["track_events"] if e["role"] == "interceptor" and e["new"] == 129]
    assert len(steal) == 1 and steal[0]["t"] == ev[0]["t"] and steal[0]["rule"] == "position gate"
    assert (A["tgt_tid"], A["itc_tid"]) == (177, None) and A["excluded_tracks"] == []   # 07:22:07: 129 gone, 177 stays the target
    assert E._BARE_STATE["track_events"] == A["track_events"] and len(A["track_events"]) <= 50


def test_f1_cpa_gate_unchanged_by_joint_selection():
    """C3: the CPA gate's documented sanity points (tests/test_apptest.py) still hold through analyze():
    07:22:00 running min 78.4 m @ 07:21:43 — rejected by the 70 m gate, accepted by a 100 m one; 07:22:31 the
    minimum itself, still closing -> no CPA; 07:22:45 first valid CPA 59 m @ 07:22:31."""
    A = E.analyze(_f1_snap("07:22:00"), P)
    assert abs(A["cpa_run"][0] - 78.4) < 0.6 and D.pdt_hms(A["cpa_run"][1]) == "07:21:43" and A["cpa"] is None and A["cpa_gate_m"] == 70.0
    assert A["tgt_tid"] == 177 and A["tgt_rule"] == "position gate"
    E._BARE_STATE.clear()
    A = E.analyze(_f1_snap("07:22:00"), dict(P, cpa_gate_m=100.0))
    assert A["cpa"] is not None and abs(A["cpa"][0] - 78.4) < 0.6 and D.pdt_hms(A["cpa"][1]) == "07:21:43" and A["cpa_valid"]
    E._BARE_STATE.clear()
    A = E.analyze(_f1_snap("07:22:31"), P)
    assert A["cpa"] is None and A["sep_now"] is not None
    E._BARE_STATE.clear()
    A = E.analyze(_f1_snap("07:22:45"), P)
    assert A["cpa"] is not None and abs(A["cpa"][0] - 59.0) < 0.6 and D.pdt_hms(A["cpa"][1]) == "07:22:31" and A["cpa_run"] == A["cpa"]
    # the gate helper itself: a minimum at the newest sample (still closing) is never valid
    S = {"t": np.arange(10.0), "sep": np.array([300, 250, 200, 150, 100, 60, 40, 30, 20, 10.0])}
    assert E.cpa_gate(S, (10.0, 9.0, 0, 0, 10.0), 70.0) is False
    S["sep"] = np.array([300, 250, 200, 150, 100, 60, 40, 30, 60, 90.0])
    assert E.cpa_gate(S, (30.0, 7.0, 0, 0, 30.0), 70.0) is True and E.cpa_gate(S, (30.0, 7.0, 0, 0, 30.0), 25.0) is False


# ── 2026-09-17: two separations / two CPAs (truth + TRACK), history seeding, reset ───────────────
def test_interp_track_running_min_and_best_validated_pass_helpers():
    """interp_track: linear E/N/U between published states, NaN where the nearest state is > TRACK_SEP_GAP_S away and for an
    empty track; running_min keeps the lower of previous / series; best_validated_pass returns the LOWEST sample that passes
    the gate (rise rule on the whole series), ``mask`` restricting the candidates only; a still-closing tail never seeds."""
    a = np.zeros((4, 16))
    a[:, 0] = [0.0, 1.0, 2.0, 10.0]
    a[:, 1] = [0.0, 10.0, 20.0, 100.0]
    a[:, 2] = 5.0
    a[:, 3] = 7.0
    out = E.interp_track(a, np.array([0.5, 1.0, 3.5, 6.0, 9.0, 12.5]))
    assert np.allclose(out[0], [5.0, 5.0, 7.0]) and np.allclose(out[1], [10.0, 5.0, 7.0]) and np.allclose(out[2], [35.0, 5.0, 7.0])   # 3.5 s: 1.5 s from the state at 2 s
    assert np.isnan(out[3]).all() and np.allclose(out[4], [90.0, 5.0, 7.0]) and np.isnan(out[5]).all()                                 # 6 s: 4 s from either neighbour; 12.5 s: 2.5 s past the last
    assert np.isnan(E.interp_track(None, np.array([1.0]))).all() and np.isnan(E.interp_track(np.zeros((0, 16)), np.array([1.0]))).all()
    t = np.arange(10.0)
    sep = np.array([300, 250, 200, 150, 100, 60, 40, 30, 60, 90.0])
    pos, horiz = np.zeros((10, 3)), sep.copy()
    assert E.running_min(None, t, sep, pos, horiz)[:2] == (30.0, 7.0)
    assert E.running_min((20.0, 1.0, 0, 0, 20.0), t, sep, pos, horiz) == (20.0, 1.0, 0, 0, 20.0)
    assert E.running_min((20.0, 1.0, 0, 0, 20.0), t, np.full(10, np.nan), pos, horiz) == (20.0, 1.0, 0, 0, 20.0)
    assert E.best_validated_pass(t, sep, pos, horiz, 70.0)[:2] == (30.0, 7.0)
    assert E.best_validated_pass(t, sep, pos, horiz, 25.0) is None                                     # 30 m is not below a 25 m gate
    assert E.best_validated_pass(t, sep, pos, horiz, 70.0, mask=t != 7)[:2] == (40.0, 6.0)             # the masked-out minimum is skipped, the rise still counts
    assert E.best_validated_pass(t, np.array([300, 250, 200, 150, 100, 60, 40, 30, 20, 10.0]), pos, horiz, 70.0) is None   # still closing


def test_f1_track_cpa_pair_rides_the_target_track_and_the_hud_names_both():
    """F1 replay 07:22:45: the truth CPA 59.0 m @ 07:22:31 (unchanged) and the new TRACK CPA — interceptor truth <-> target
    track #177 — 60.3 m @ 07:22:30, both in session state (cpa_trk_run / cpa_trk_ok), S["sep_trk"] on the same grid, the HUD
    words "CPA truth 59 m · 07:22:31 · CPA track 60 m · 07:22:30".  At 07:22:00 (pass 1, 78 m) the track pair has a running
    minimum (143 m) but no CPA: "CPA track —" beside a 100 m-gate truth CPA."""
    from ih import plots as PL

    A = E.analyze(_f1_snap("07:22:45"), P)
    assert abs(A["cpa"][0] - 59.0) < 0.6 and D.pdt_hms(A["cpa"][1]) == "07:22:31"
    assert A["tgt_tid"] == 177 and A["cpa_trk"] is not None and abs(A["cpa_trk"][0] - 60.3) < 0.6 and D.pdt_hms(A["cpa_trk"][1]) == "07:22:30"
    assert A["cpa_trk_valid"] and A["cpa_trk_run"] == A["cpa_trk"] == E._BARE_STATE["cpa_trk_ok"] == E._BARE_STATE["cpa_trk_run"]
    S = A["sep"]
    assert len(S["sep_trk"]) == len(S["t"]) == len(S["trk"]) and np.isfinite(S["sep_trk"]).any() and A["sep_trk_now"] is not None
    assert PL.cpa_hud(A) == "CPA 59 m · 07:22:31"   # 2026-09-17 pm: one CPA, one word (the track CPA stays a tile number)
    E._BARE_STATE.clear()
    A = E.analyze(_f1_snap("07:22:00"), dict(P, cpa_gate_m=100.0))
    assert A["cpa"] is not None and A["cpa_trk"] is None and A["cpa_trk_run"] is not None and A["cpa_trk_run"][0] > 100.0
    assert PL.cpa_hud(A) == "CPA 78 m · 07:21:43"


def test_history_seed_seeds_both_pairs_once_per_rev_and_again_after_a_reset():
    """seed_cpa_from_history: a snap["cpa_hist"] with a validated 30 m truth pass and a target-allegiance track (median near
    the target) seeds cpa_run/cpa_ok and cpa_trk_run/cpa_trk_ok; the interceptor's own radar track (median near the
    interceptor) is never a candidate; the same rev is not re-read — unless data.seek()/reset_derived() cleared both pairs;
    a live running minimum lower than the history is kept."""
    t = T0 + np.arange(0.0, 60.0, 0.5)
    itc = np.column_stack([t, 1000.0 + 20.0 * (t - T0 - 30.0), np.zeros_like(t), np.full_like(t, 100.0), np.full_like(t, 20.0), np.zeros_like(t), np.zeros_like(t)])   # E-bound through the target at t=+30
    tgt = np.column_stack([t, np.full_like(t, 1000.0), 30.0 + 3.0 * (t - T0 - 30.0), np.full_like(t, 100.0), np.zeros_like(t), np.full_like(t, 3.0), np.zeros_like(t)])   # 30 m north at t=+30, crawling N at 3 m/s (>= AIRBORNE_TGT_SPEED_MPS: a hover is not a pass)
    trk = np.zeros((len(t), 16)); trk[:, 0] = t; trk[:, 1] = 1000.0; trk[:, 2] = tgt[:, 2] + 15.0; trk[:, 3] = 100.0; trk[:, 7] = t; trk[:, 9] = 2   # target track: 15 m north of the target truth
    own = np.zeros((len(t), 16)); own[:, 0] = t; own[:, 1] = itc[:, 1] + 5.0; own[:, 2] = 3.0; own[:, 3] = 100.0; own[:, 7] = t; own[:, 9] = 2   # the interceptor's own radar track
    H = {"tgt": tgt, "itc": itc, "tracks": {7: trk, 9: own}, "lo": float(t[0]), "hi": float(t[-1]), "rev": "run:1"}
    s: dict = {}
    out = E.seed_cpa_from_history(s, {"cpa_hist": H}, 70.0)
    assert out["truth"] is not None and abs(out["truth"][0] - 30.0) < 0.6 and abs(out["truth"][1] - (T0 + 30.0)) <= 1.0
    assert out["track"] is not None and abs(out["track"][0] - 45.0) < 0.6 and s["cpa_trk_ok"] == s["cpa_trk_run"] == out["track"]   # #7, never #9 (~5 m)
    assert s["cpa_ok"] == s["cpa_run"] == out["truth"]
    assert E.seed_cpa_from_history(s, {"cpa_hist": H}, 70.0) is None                                     # same rev: not re-read
    s["cpa_run"] = s["cpa_ok"] = s["cpa_trk_run"] = s["cpa_trk_ok"] = None                                  # data.seek() / reset_derived()
    assert E.seed_cpa_from_history(s, {"cpa_hist": H}, 70.0)["truth"] == out["truth"]                      # ... re-seeded
    s["cpa_run"] = (12.0, T0 + 100.0, 0.0, 0.0, 12.0)
    assert E.seed_cpa_from_history(s, {"cpa_hist": dict(H, rev="run:2")}, 70.0)["truth"] is not None and s["cpa_run"][0] == 12.0   # a lower live minimum keeps the tile; the history pass is still listed
    assert len(s["cpa_passes"]) == 1 and s["cpa_ok"] == s["cpa_passes"][-1] and abs(s["cpa_ok"][0] - 30.0) < 0.6
    assert E.seed_cpa_from_history(s, {"cpa_hist": {"tgt": tgt, "itc": itc, "tracks": {}, "lo": float(t[0]), "hi": float(t[-1]), "rev": "run:3"}}, 20.0) is not None
    assert E.seed_cpa_from_history(s, {"cpa_hist": None}, 70.0) is None and E.seed_cpa_from_history(s, {}, 70.0) is None
    for k in ("cpa_trk_run", "cpa_trk_ok"):
        assert k in D.STATE_DEFAULTS                                                                        # data.seek / reset_derived clear them with the truth pair


# ── 2026-09-17: AIRBORNE gate + PER-PASS CPA (MRU91 run 3212ae22 saved archives) ────────────────
def _saved_flight(day: str, label: str) -> int | None:
    D.refresh_registry()
    for f in D.saved_flights():
        if f.get("day") == day and f.get("label") == label:
            return int(f["n"])
    return None


def _replay(fl: int, gate_m: float, start: float, stop: float, step: float = 1.0) -> list[dict]:
    """Tick the engine over a saved flight (window 180 s) and return the per-tick (t, cpa, cpa_trk, cpa_run, passes) rows."""
    from ih import feed as F

    st.session_state["flight"] = fl
    st.session_state["spec_window"] = 120
    rows = []
    for t in np.arange(np.floor(start), stop + 1e-9, step):
        A = E.analyze(F.archive_snapshot(float(t), 180.0), dict(P, cpa_gate_m=gate_m))
        rows.append({"t": float(t), "cpa": A["cpa"], "cpa_trk": A["cpa_trk"], "run": A["cpa_run"], "run_trk": A["cpa_trk_run"], "tid": A["tgt_tid"],
                     "passes": list(A["cpa_passes"]), "valid": A["cpa_valid"]})
    return rows


def _hms(day: str, hms: str) -> float:
    return D.hms_to_epoch(hms, day)


def test_airborne_mask_validated_passes_and_merge_helpers():
    """airborne_mask: both >= AIRBORNE_MIN_M above the radar AND target ground speed >= AIRBORNE_TGT_SPEED_MPS (a pad hover 15 m
    below the antenna, or a hovering target, is never a candidate; alt_a / spd substitute the track's U / a speed fallback).
    validated_passes: EVERY local minimum under the gate with the rise after it, a finite sample before it (a window-edge
    minimum never qualifies), a plateau once; merge_passes replaces a re-derived pass and drops those above a tightened gate."""
    n = 6
    tgt = np.column_stack([np.zeros(n), np.zeros(n), [-15.0, 50.0, 50.0, 50.0, 50.0, np.nan], np.zeros(n), [0.0, 0.0, 15.0, 15.0, 15.0, 15.0], np.zeros(n)])
    itc = np.column_stack([np.zeros(n), np.zeros(n), [-15.0, 50.0, 50.0, 10.0, 50.0, 50.0], np.zeros(n), np.zeros(n), np.zeros(n)])
    assert E.airborne_mask(tgt, itc).tolist() == [False, False, True, False, True, False]     # pad / hover / ok / interceptor low / ok / NaN
    assert E.airborne_mask(tgt, itc, alt_a=np.array([50, 50, 5, 50, 50, 50.0])).tolist() == [False, False, False, False, True, True]   # the track's U replaces the target's (also where truth U is NaN)
    assert E.airborne_mask(tgt, itc, spd=np.full(n, 9.0)).tolist() == [False, False, True, False, True, False]      # the speed fallback fills only NaN truth speeds (the hover keeps its 0)
    t = np.arange(20.0)
    sep = np.array([300, 250, 200, 150, 100, 60, 40, 30, 60, 90, 120, 80, 50, 35, 35, 55, 80, 110, 140, 170.0])
    pos, horiz = np.zeros((20, 3)), sep.copy()
    ps = E.validated_passes(t, sep, pos, horiz, 70.0)
    assert [(p[0], p[1]) for p in ps] == [(30.0, 7.0), (35.0, 13.0)]                        # two passes, the plateau once (its first sample)
    assert E.validated_passes(t, sep, pos, horiz, 70.0, mask=t != 7) == [(35.0, 13.0, 0.0, 0.0, 35.0)]   # a masked minimum yields NO pass (a neighbour never stands in)
    assert E.validated_passes(t, sep, pos, horiz, 32.0) == [(30.0, 7.0, 0.0, 0.0, 30.0)]
    edge = np.array([59.05, 70, 85, 100, 120, 140, 160, 180, 200, 220.0])                 # buffer refill: the minimum IS the first sample
    assert E.validated_passes(np.arange(10.0), edge, np.zeros((10, 3)), edge, 70.0) == []
    edge2 = np.concatenate([[np.nan], edge])                                                # ... or the first FINITE sample
    assert E.validated_passes(np.arange(11.0), edge2, np.zeros((11, 3)), edge2, 70.0) == []
    closing = np.array([300, 250, 200, 150, 100, 60, 40, 30, 20, 10.0])
    assert E.validated_passes(t[:10], closing, pos[:10], closing, 70.0) == []                # still closing: no rise yet
    merged = E.merge_passes([(30.0, 7.0, 0, 0, 30.0)], [(29.5, 7.5, 1, 1, 29.0), (35.0, 13.0, 0, 0, 35.0)], 70.0)
    assert merged == [(29.5, 7.5, 1, 1, 29.0), (35.0, 13.0, 0, 0, 35.0)] and E.latest_pass(merged) == (35.0, 13.0, 0, 0, 35.0)
    assert E.merge_passes(merged, [], 32.0) == [(29.5, 7.5, 1, 1, 29.0)] and E.merge_passes(None, [], 70.0) == [] and E.latest_pass([]) is None
    assert E._drop_stale_min((5.0, 1.0, 0, 0, 5.0), 100.0, []) is None                       # older than the series, never validated -> dropped
    assert E._drop_stale_min((29.5, 7.5, 1, 1, 29.0), 100.0, merged) == (29.5, 7.5, 1, 1, 29.0)   # a validated pass is kept
    assert E._drop_stale_min((5.0, 150.0, 0, 0, 5.0), 100.0, []) == (5.0, 150.0, 0, 0, 5.0)  # inside the series: kept


@pytest.mark.skipif(_saved_flight("2026-09-17", "Flight 1") is None, reason="9-17 saved archive 'Flight 1' not present (IH_ARCHIVE_ROOT)")
def test_9_17_flight1_cpa_is_the_airborne_pass_never_the_pad_value():
    """MRU91 run 3212ae22 Flight 1 replayed tick by tick from 06:48:20 (window 180 s, gate 70 m): the two drones sat together on the pad
    ~15 m BELOW the antenna at 23.4 m (06:50:56, speeds < 0.3 m/s) — with the airborne gate that is never a CPA nor the running
    minimum.  The first airborne pass 28.4 m @ 06:55:48 validates at 06:55:51 (truth) and the track pass 19.4 m @ 06:55:50 on
    track #728 at 06:55:54; a SECOND airborne pass 32.9 m @ 06:57:03 then becomes the displayed CPA (most recent validated pass)
    while the running minimum stays the 28.4 m pass."""
    fl, day = _saved_flight("2026-09-17", "Flight 1"), "2026-09-17"
    rows = _replay(fl, 70.0, _hms(day, "06:48:20"), _hms(day, "06:57:30"))
    for r in rows:
        for v in (r["cpa"], r["run"]):
            assert v is None or v[0] > 25.0, (D.pdt_hms(r["t"]), v)                          # the 23.4 m pad value never appears
    at = lambda hms: min(rows, key=lambda r: abs(r["t"] - _hms(day, hms)))   # noqa: E731  the tick nearest a PDT time
    assert at("06:55:49")["cpa"] is None and abs(at("06:55:49")["run"][0] - 28.4) < 0.6           # minimum reached, still closing
    r = at("06:56:30")
    assert abs(r["cpa"][0] - 28.4) < 0.6 and D.pdt_hms(r["cpa"][1]) in ("06:55:47", "06:55:48") and r["valid"]
    assert r["run"] == r["cpa"] and [round(p[0], 1) for p in r["passes"]] == [28.4]
    assert r["cpa_trk"] is not None and 18.5 < r["cpa_trk"][0] < 20.5 and D.pdt_hms(r["cpa_trk"][1]) == "06:55:50" and r["tid"] == 728
    r = at("06:57:30")
    assert abs(r["cpa"][0] - 32.9) < 0.6 and D.pdt_hms(r["cpa"][1]) == "06:57:03"                  # the newer validated pass is displayed ...
    assert abs(r["run"][0] - 28.4) < 0.6 and D.pdt_hms(r["run"][1]) == "06:55:48" and r["valid"]  # ... the session minimum stays (and IS a validated pass)
    assert [round(p[0], 1) for p in r["passes"]] == [28.4, 32.9]
    assert E._BARE_STATE["cpa_ok"] == r["cpa"] and E._BARE_STATE["cpa_passes"] == r["passes"]


@pytest.mark.skipif(_saved_flight("2026-09-17", "Flight 2 full") is None, reason="9-17 saved archive 'Flight 2 full' not present (IH_ARCHIVE_ROOT)")
def test_9_17_flight2_has_no_pass_under_70m_and_never_the_pad_value():
    """Flight 2 full (07:00–07:21) replayed at 2 s ticks: the pad artefact 22.8 m @ 07:11:20 (both ~15 m below the antenna) never
    appears; under a 70 m gate there is NO validated pass (A["cpa"] None throughout); the closest AIRBORNE pass is 131 m @ 07:18:21
    (validated under a 200 m gate, running minimum 131.3 m)."""
    fl, day = _saved_flight("2026-09-17", "Flight 2 full"), "2026-09-17"
    rows = _replay(fl, 70.0, _hms(day, "07:00:04"), _hms(day, "07:21:00"), step=2.0)
    assert all(r["cpa"] is None and r["cpa_trk"] is None and not r["passes"] for r in rows)
    assert all(r["run"] is None or r["run"][0] > 100.0 for r in rows), min((r["run"] for r in rows if r["run"]), key=lambda v: v[0])
    last = rows[-1]
    assert abs(last["run"][0] - 131.3) < 0.6 and D.pdt_hms(last["run"][1]) == "07:18:21"
    E._BARE_STATE.clear()
    rows = _replay(fl, 200.0, _hms(day, "07:17:00"), _hms(day, "07:19:00"), step=1.0)
    assert abs(rows[-1]["cpa"][0] - 131.3) < 0.6 and D.pdt_hms(rows[-1]["cpa"][1]) == "07:18:21" and rows[-1]["valid"]


def test_analyze_drops_a_stale_unvalidated_minimum_and_resets_pass_lists_with_the_pairs():
    """8/28 F1 through analyze(): a never-validated running minimum older than the separation series (the 9-17 masking failure) is
    dropped INSIDE analyze() — the window re-derives 59.0 m @ 07:22:31 and the pass validates; a minimum still inside the series is
    kept; clearing cpa_run + cpa_ok (data.seek / reset_derived) clears the pass lists too and the window re-validates the pass."""
    s = E._BARE_STATE
    s["cpa_run"] = s["cpa_trk_run"] = (5.0, D.hms_to_epoch("07:22:31") - 600.0, 0.0, 0.0, 5.0)
    s["cpa_ok"] = s["cpa_trk_ok"] = None
    A = E.analyze(_f1_snap("07:22:45"), P)
    assert abs(A["cpa_run"][0] - 59.0) < 0.6 and abs(A["cpa"][0] - 59.0) < 0.6 and D.pdt_hms(A["cpa"][1]) == "07:22:31" and A["cpa_valid"]
    assert A["cpa_trk"] is not None and abs(A["cpa_trk"][0] - 60.3) < 0.6 and A["cpa_trk_run"] == A["cpa_trk"]
    keep = (5.0, D.hms_to_epoch("07:22:31") - 30.0, 0.0, 0.0, 5.0)
    s["cpa_run"] = keep
    A = E.analyze(_f1_snap("07:22:47"), P)
    assert A["cpa_run"] == keep and not A["cpa_valid"] and abs(A["cpa"][0] - 59.0) < 0.6      # inside the series: kept (not a validated pass -> tile not gold)
    assert len(s["cpa_passes"]) == 1
    s["cpa_run"] = s["cpa_ok"] = None                                                          # data.seek() / reset_derived()
    A = E.analyze(_f1_snap("07:22:49"), P)
    assert s["cpa_passes"] and abs(A["cpa"][0] - 59.0) < 0.6 and A["cpa_run"] == A["cpa"]     # re-derived from the window
    A = E.analyze(_f1_snap("07:22:49"), dict(P, cpa_gate_m=50.0))
    assert A["cpa"] is None and A["cpa_passes"] == [] and A["cpa_trk"] is None                  # gate tightened below the pass: dropped


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
