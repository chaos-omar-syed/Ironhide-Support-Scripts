"""Live-test analysis over one snapshot: target / interceptor track selection (radar truth
match > position gate, ADS-B matches excluded, JOINT distinct assignment, per-role hysteresis —
``select_tracks``) with a per-session track-id change log (handovers / steals / losses, each
with the rule that made the new pick — ``track_events``), state coding, separation / closing /
predicted miss, health chips, az-el-range-alt errors, coverage, and the measurement-space
series (BISTATIC range / rate through chaos-spa's geometry kernels, per-sample ON-TARGET
gating). Safe on empty inputs.

Track selection (``select_tracks``, one call per tick): every track with a state in the last
3 s is scored against each truth (median horizontal distance over 10 s, ``side_scores``).  The
radar's own runtime truth match (block-143 ``truth_match.target_id``, snap["track_meta"]) sets
the tier: an id in the role's MAVLink id list (P["role_ids"], else the session patterns) makes
the track a PRIORITY candidate of that role (allowed up to 1.5 × GATE from its truth); a non-
MAVLink id (an ADS-B callsign such as N432R) EXCLUDES the track from both roles; no match ->
the plain 150 m position gate (a MAVLink match pointing at the other role is position-gated
too, with a half-gate handicap).  The (target, interceptor) pair is then chosen JOINTLY as the
best DISTINCT pair by summed cost (priority = large bonus, an empty role costs one gate), and
each role keeps its previous track while it stays inside the hysteresis band (< gate and
<= 1.5 × best + 20 m; a priority candidate still wins over a sticky position-gate one).
A["tgt_rule"] / A["itc_rule"] say why: "radar match conf 0.93" / "position gate" /
"hysteresis" / "excluded ADS-B N432R" (nothing left after an exclusion) / "no track in gate".

Error math (az/el/range/alt panel + Spec RMSE inputs) is chaos-spa's, via the
track_correlation adapter ``spa_errors.spa_err_stack`` / ``spa_summary``: only
CONFIRMED tracks' UPDATED (measurement) states are graded, spec tiles report
spa's RMSE95 (95.5 % trimmed) with the plain RMSE as a secondary number, and the
truth handed to spa is WGS-84 HAE (see truth7_to_truth8).  The remaining published
states (TENTATIVE / COASTING / extrapolated) are graded a second time through the
same adapter with spa's permissive options (``ungraded_errors``) so the panel can
draw them as a dotted line — they never enter the containment / spec statistics.
Velocity states (the North / East / vertical velocity cards): the same graded rows carry
spa's corr_df ``e_dot_errors / n_dot_errors / u_dot_errors`` (track − truth filtered velocity per
ENU axis, m/s) as e["ve"] / e["vn"] / e["vu"] with 1σ e["sig_ve"] / e["sig_vn"] / e["sig_vu"] =
spa's ``e_dot_sigma / n_dot_sigma / u_dot_sigma`` — NaN wherever the track state published no
covariance (the adapter's constant velocity-σ fill never reaches a band or a containment rate),
so A["contain"]["ve"|"vn"|"vu"] counts only rows with a real σ (n == 0 on the 8/28 archives).

CPA policy (2026-09-17, MRU91 run 3212ae22: the two drones sat together on the pad ~15 m BELOW the antenna and the
session-global running minimum — 23.4 m @ 06:50:56, speeds < 0.3 m/s — masked the real airborne pass 28.4 m @ 06:55:47
for the rest of the run; Flight 2 showed the same pad value while its closest airborne pass was 131 m):
* AIRBORNE gate (``airborne_mask``): a separation sample is a CPA candidate only when BOTH entities are >= AIRBORNE_MIN_M
  above the radar (truth / track U) AND the target's ground speed is >= AIRBORNE_TGT_SPEED_MPS at that instant.  It applies
  to the truth pair, the track pair, live and archive, the running minima and the connect-time history seed.
* PER-PASS CPA (``validated_passes``): every tick the current window is scanned for validated passes — local minima of the
  separation series below CPA_GATE_M (sidebar, default 70 m) whose separation rose by >= CPA_RISE_M (or >= CPA_RISE_FRAC of
  the minimum) within the following CPA_LOOK_S, with at least one finite sample BEFORE the minimum (a window-edge minimum
  right after a buffer refill never qualifies) and airborne at the minimum.  The passes persist in session state
  (``cpa_passes`` / ``cpa_trk_passes``: [(sep, t, E, N, horiz)], oldest first, capped at PASS_LIST_MAX) and A["cpa"] /
  A["cpa_trk"] (the gold ★, the hairlines, the map ★, the HUD words) = the MOST RECENT validated pass of the session — a
  newer validated pass replaces the displayed one; ``cpa_ok`` / ``cpa_trk_ok`` mirror the displayed pass for the page.
* ``cpa_run`` / ``cpa_trk_run`` (the "closest so far" tile) stay the SESSION minimum but airborne-gated; a running minimum
  older than the separation series' start that never became a validated pass is dropped inside analyze() (it could never be
  gated again).  A["cpa_valid"] = the running minimum IS one of the validated passes (the tile turns gold).
* a gate tightened below a stored pass drops that pass; data.seek() / reset_derived() clearing both ``cpa_run`` and
  ``cpa_ok`` also clears the pass lists (the engine detects the reset: ih/data.py keeps only the pair keys).

Two separations, two CPAs (2026-09-17, user: "what is 3D vs horizontal — I want CPA to TRACK and
CPA to TRUTH"): ``S["sep"]`` = interceptor truth <-> target truth (3D) with ``cpa_run`` / ``cpa_ok``
-> A["cpa"] (the gold ★, the hairlines, the map ★); ``S["sep_trk"]`` = interceptor truth <-> the
radar's TARGET TRACK state (A["tgt_track"], the track A["tgt_tid"] selected — never a nearest-track
proxy: the interceptor's own radar track would read ~20 m) interpolated to the same 1 Hz grid, NaN
where the track has no state within TRACK_SEP_GAP_S, with its own running minimum ``cpa_trk_run`` /
pass list ``cpa_trk_passes`` -> A["cpa_trk"] under the SAME gate + airborne rules (the track's U stands in for the
target's altitude, the target TRUTH ground speed — or the track's own where truth is missing — for its speed).  Both
pairs live in session state and are cleared together by data.seek() / data.reset_derived().  A connect-time HISTORY
scan (feed.live_snapshot -> snap["cpa_hist"], see ``seed_cpa_from_history``) seeds the PASS LISTS of both pairs (every
validated airborne pass of the LOOKBACK before the session connected, not just a minimum) — a late connect still shows
the CPA of the last pass."""
from __future__ import annotations

import time

import numpy as np
import streamlit as st

from . import data as D

TR, TK = D.TR, D.TK
CHI_NONE = float("nan")
ERR_PANEL_S = 120.0  # rolling window of the Live error panel (plots.error_fig default)
CPA_GATE_M_DEFAULT = 70.0   # sidebar "Closest-approach gate (m)" default
CPA_RISE_M = 20.0           # local-minimum test: separation must rise by >= this ...
CPA_RISE_FRAC = 0.15        # ... or by >= this fraction of the minimum ...
CPA_LOOK_S = 10.0           # ... within this many seconds after the minimum
AIRBORNE_MIN_M = 20.0       # AIRBORNE gate: both entities (truth U / track U) at least this far ABOVE the radar for a separation sample to be a CPA candidate
AIRBORNE_TGT_SPEED_MPS = 2.0   # ... AND the target's ground speed at least this (a hover beside the parked interceptor is not a pass)
PASS_LIST_MAX = 50          # validated passes kept per pair in session state (oldest dropped)
SEP_MIN_SPAN_S = 60.0       # the separation series always spans at least this (pre-flight: a 1 s grid made every x tick label identical)
TRACK_SEP_GAP_S = 2.0       # "sep · track" (interceptor truth <-> target TRACK) is NaN where the track has no published state within this of the grid time
TRACK_FLASH_S = 10.0        # tile edge / map pill flash amber this long after a target- or interceptor-track handover
SIG_KEYS = ("sig_az", "sig_el", "sig_rng", "sig_alt", "sig_pos3d", "sig_ve", "sig_vn", "sig_vu")
VEL_KEYS = ("ve", "vn", "vu")            # velocity-state errors (spa e_dot / n_dot / u_dot: track − truth, m/s) with sigmas sig_ve / sig_vn / sig_vu
ERR_ROW_KEYS = ("t", "az", "el", "rng", "alt", "pos3d", "horiz", "kind", "sig_az", "sig_el", "sig_rng", "sig_alt", "sig_pos3d",
                "ve", "vn", "vu", "sig_ve", "sig_vn", "sig_vu")   # every per-row array of an errors dict (all the same length as "t")

_BARE_STATE: dict = {}   # per-process stand-in for st.session_state when analyze() runs outside a Streamlit script (tests, CLI)


def session_state():
    """st.session_state inside a Streamlit script run (a page, AppTest); a module-level dict when there is no
    script-run context (bare Python: engine tests, CLI tools) so the per-session keys analyze() keeps
    (tgt_tid, _itc_tid_prev, track_events, cpa_run, cpa_ok, error timers) still persist across calls."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx(suppress_warning=True) is not None:
            return st.session_state
    except Exception:
        pass
    return _BARE_STATE


# ── Truth helpers ────────────────────────────────────────────────────────────
def interp_truth(T: np.ndarray, ts: np.ndarray, max_gap: float = D.MAX_GAP_S) -> np.ndarray:
    """(E,N,U,vE,vN,vU) at ts; NaN outside coverage or across gaps > max_gap."""
    ts = np.asarray(ts, float)
    out = np.full((len(ts), 6), np.nan)
    if T is None or len(T) < 2 or not len(ts):
        return out
    t = T[:, 0]
    for k in range(1, 7):
        out[:, k - 1] = np.interp(ts, t, T[:, k])
    idx = np.searchsorted(t, ts).clip(1, len(t) - 1)
    bad = (ts < t[0]) | (ts > t[-1]) | ((t[idx] - t[idx - 1]) > max_gap)
    out[bad] = np.nan
    return out


def latest(T: np.ndarray, t_now: float, max_age: float = 3.0) -> np.ndarray | None:
    if T is None or not len(T):
        return None
    i = int(np.searchsorted(T[:, 0], t_now, side="right"))
    if i == 0 or t_now - T[i - 1, 0] > max_age:
        return None
    return T[i - 1]


def trail(T: np.ndarray, t_now: float, trail_s: float) -> np.ndarray:
    if T is None or not len(T):
        return np.zeros((0, T.shape[1] if T is not None and T.ndim == 2 else 7))
    i0 = int(np.searchsorted(T[:, 0], t_now - trail_s, side="left"))
    i1 = int(np.searchsorted(T[:, 0], t_now, side="right"))
    return T[i0:i1]


OTHER_MAX_AGE_S = 15.0      # an unassigned MAVLink feed still draws this long after its newest row (feed_status calls it "down" past 15 s)


def other_truth(snap: dict, t_now: float, trail_s: float, hdg_mem: dict | None = None) -> list[dict]:
    """Every UNASSIGNED live MAVLink drone (snap["other"] = [(target_id, (n,7) TR rows)]) as a display-only row:
    {id, E, N, U, spd, hdg, age, trail}.  NO grading, NO role, no track association — the map draws them grey so
    the operator sees every drone in the air.  ``hdg`` = degrees CW from north from the feed's own velocity; below
    0.5 m/s the last known heading is kept (``hdg_mem``: {id: deg}, updated in place) so a hovering drone does not
    spin.  A feed whose newest row is older than OTHER_MAX_AGE_S is dropped (the feed chip still reports it)."""
    out = []
    for tid, T in (snap.get("other") or []):
        T = np.asarray(T, float)
        if T.ndim != 2 or not len(T):
            continue
        p = latest(T, t_now, OTHER_MAX_AGE_S)
        if p is None:
            continue
        spd = float(np.hypot(p[TR["vE"]], p[TR["vN"]]))
        if spd > 0.5:
            hdg = float(np.degrees(np.arctan2(p[TR["vE"]], p[TR["vN"]])))
            if hdg_mem is not None:
                hdg_mem[str(tid)] = hdg
        else:
            hdg = float((hdg_mem or {}).get(str(tid), 0.0))
        out.append({"id": str(tid), "E": float(p[TR["E"]]), "N": float(p[TR["N"]]), "U": float(p[TR["U"]]),
                    "spd": spd, "hdg": hdg, "age": float(t_now - p[TR["t"]]), "trail": trail(T, t_now, trail_s)})
    return out


def wrap_deg(a: np.ndarray) -> np.ndarray:
    return (a + 180.0) % 360.0 - 180.0


def polar(E, N, U):
    rng = np.sqrt(E * E + N * N + U * U)
    az = np.degrees(np.arctan2(E, N)) % 360.0
    el = np.degrees(np.arctan2(U, np.hypot(E, N)))
    return az, el, rng


# ── Track state coding ───────────────────────────────────────────────────────
def state_kind(a: np.ndarray) -> np.ndarray:
    """'tent' | 'meas' | 'coast' per row of a TK-layout track array."""
    k = np.where(a[:, TK["t"]] - a[:, TK["lu"]] < D.MEAS_LAG_S, "meas", "coast").astype(object)
    k[a[:, TK["state"]] == 1] = "tent"
    return k


def _recent(a: np.ndarray, t_now: float, look: float) -> np.ndarray:
    return a[(a[:, 0] >= t_now - look) & (a[:, 0] <= t_now)]


def side_scores(tracks: dict[int, np.ndarray], T: np.ndarray, t_now: float, look: float = 10.0, max_age: float = 3.0) -> dict[int, float]:
    """{tid: median horizontal distance to truth T over the last `look` s} for
    tracks with a state in the last `max_age` s."""
    out = {}
    for tid, a in tracks.items():
        s = _recent(a, t_now, look)
        if not len(s) or t_now - s[-1, 0] > max_age:
            continue
        tr = interp_truth(T, s[:, 0])
        d = np.hypot(s[:, TK["E"]] - tr[:, 0], s[:, TK["N"]] - tr[:, 1])
        d = d[np.isfinite(d)]
        if len(d):
            out[tid] = float(np.median(d))
    return out


def pick_target_track(tracks, T, t_now, prev_tid, gate=D.GATE_M):
    sc = side_scores(tracks, T, t_now)
    if not sc:
        return None, sc
    best = min(sc, key=sc.get)
    if prev_tid in sc and sc[prev_tid] < gate and sc[prev_tid] <= 1.5 * sc[best] + 20.0:
        best = prev_tid  # sticky: don't flap between overlapping tracks
    return (best if sc[best] < gate else None), sc


# ── Track selection: radar truth match > position gate, ADS-B excluded, JOINT distinct pair, hysteresis ──
MATCH_GATE_FACTOR = 1.5      # a radar-matched (priority) candidate may sit up to this × gate from its role's truth
CAND_TOP_N = 5               # candidates per role entering the brute-force joint (target, interceptor) search
PRIORITY_BONUS_M = 1.0e4     # joint-cost bonus of a priority candidate: always beats a position-gate one
HYST_BONUS_M = 1.0e3         # joint-cost bonus of the previous tick's track inside the hysteresis band: beats same-tier candidates, never a priority one
HYST_FACTOR, HYST_PAD_M = 1.5, 20.0   # hysteresis band: keep prev while d_prev < gate and d_prev <= HYST_FACTOR · d_best + HYST_PAD_M
OTHER_ROLE_PENALTY_M = 0.5 * D.GATE_M  # MAVLink match pointing at the OTHER role: still a position-gate candidate here (beats an empty role) but any null-match track up to this much farther wins
NO_TRACK_RULE = "no track in gate"
COAST_HOLD_RULE = "coasting hold"     # the previous tick's track kept while its newest state is a COASTING one, even beyond the gate
COAST_HOLD_EPS_M = 1e-3               # its joint cost: just under the "none" cost (= gate) — every in-gate candidate beats it, an empty role never does


def coasting_now(tracks: dict, tid, t_now: float, max_age: float = 3.0) -> bool:
    """True when track ``tid`` still publishes (newest state within ``max_age``) and that newest state is a COASTING one
    (published without a measurement): the tracker is propagating it.  2026-09-15 user: "why do all plots go blank during a
    coast? the track should be propagated … never go dark unless we drop the track or at the start of a track switch" — a
    propagated state drifting past the position gate must not un-select the track (role_candidates ``hold``); a MEASUREMENT
    state beyond the gate (a steal / another object) still does, and a track that stops publishing is dropped."""
    if tid is None or tid not in tracks:
        return False
    a = np.asarray(tracks[tid], float)
    if not len(a) or float(t_now) - float(a[-1, 0]) > float(max_age):
        return False
    return str(state_kind(a[-1:])[0]) == "coast"


def is_mavlink_id(mid) -> bool:
    """A MAVLink-looking radar truth-match id ('mavlink_14551', 'mav14550_1_1'); anything else that is not empty
    (an ADS-B callsign such as 'N432R') is an airliner match."""
    return str(mid or "").strip().lower().startswith("mav")


def role_ids_of(params: dict | None) -> dict:
    """{"target": [ids], "interceptor": [ids]} — the Data-source MAVLink role assignment from P["role_ids"], else
    the session's explicit assignment (D.role_assignment); empty lists mean "use the patterns" (D.role_of)."""
    r = (params or {}).get("role_ids")
    if not isinstance(r, dict):
        try:
            r = D.role_assignment()
        except Exception:
            r = {}
    return {"target": [str(x) for x in (r.get("target") or ())], "interceptor": [str(x) for x in (r.get("interceptor") or ())]}


def match_role(mid, role_ids: dict | None) -> str | None:
    """Which role the radar's truth_match id points at: 'target' / 'interceptor' when the id is in that role's
    list; a MAVLink-looking id in neither list falls back to the session patterns (D.role_of — the rule the truth
    merge itself uses, so a third feed gets the same role in both places); 'other' for a non-MAVLink match
    (ADS-B callsign -> the track is an airliner, excluded from both roles); None for no match / NaN / ''."""
    if mid is None or (isinstance(mid, float) and not np.isfinite(mid)):
        return None
    m = str(mid).strip()
    if not m or m.lower() in ("nan", "none"):
        return None
    r = role_ids or {}
    if m in (r.get("target") or ()):
        return "target"
    if m in (r.get("interceptor") or ()):
        return "interceptor"
    if is_mavlink_id(m):
        try:
            return D.role_of(m)
        except Exception:
            return D.role_by_pattern(m)
    return "other"


def track_match(meta: dict | None, tid) -> tuple:
    """(truth_match_id | None, confidence float | nan) of one track from snap["track_meta"] = {tid: {truth_match_id,
    truth_match_conf, contributors}} (B's feed contract); a missing / malformed entry reads as no match."""
    m = (meta or {}).get(tid)
    if m is None and meta:
        m = meta.get(str(tid))
    if not isinstance(m, dict):
        return None, float("nan")
    mid = m.get("truth_match_id")
    if mid is None or (isinstance(mid, float) and not np.isfinite(mid)) or str(mid).strip().lower() in ("", "nan", "none"):
        mid = None
    else:
        mid = str(mid).strip()
    try:
        conf = float(m.get("truth_match_conf"))
    except (TypeError, ValueError):
        conf = float("nan")
    return mid, conf


def match_rule(conf: float) -> str:
    return f"radar match conf {conf:.2f}" if np.isfinite(conf) else "radar match"


def excluded_rule(mid) -> str:
    return f"excluded ADS-B {mid}"


def role_candidates(scores: dict, meta: dict | None, role: str, role_ids: dict, prev, gate: float = D.GATE_M, hold: bool = False) -> tuple[list[dict], list[dict]]:
    """One role's candidates from its side_scores {tid: d}: ([{tid, d, prio, conf, sticky, cost}] sorted by cost, top
    CAND_TOP_N; [{tid, match_id, conf, d}] EXCLUDED tracks — non-MAVLink radar match).  Tier by the radar's truth
    match: id in this role's list -> PRIORITY (d < MATCH_GATE_FACTOR·gate, cost d − PRIORITY_BONUS_M); no match ->
    position gate (d < gate, cost d); a MAVLink match pointing at the OTHER role -> position gate with
    +OTHER_ROLE_PENALTY_M (a duplicate target-matched track fills an empty interceptor slot, but loses to any
    null-match track there).  The previous tick's track is STICKY (cost −HYST_BONUS_M) while d < gate and
    d <= HYST_FACTOR·d_best + HYST_PAD_M."""
    cands, excluded = [], []
    for tid, d in scores.items():
        mid, conf = track_match(meta, tid)
        mr = match_role(mid, role_ids)
        if mr == "other":
            excluded.append({"tid": int(tid), "match_id": mid, "conf": conf, "d": float(d)})
            continue
        prio = mr == role
        other = mr is not None and not prio
        if d < (MATCH_GATE_FACTOR * gate if prio else gate):
            cands.append({"tid": int(tid), "d": float(d), "prio": prio, "conf": conf, "sticky": False, "other_role": other,
                          "cost": float(d) - (PRIORITY_BONUS_M if prio else 0.0) + (OTHER_ROLE_PENALTY_M if other else 0.0)})
    if cands and prev is not None:
        best = min(c["d"] for c in cands)
        for c in cands:
            if c["tid"] == int(prev) and c["d"] < gate and c["d"] <= HYST_FACTOR * best + HYST_PAD_M:
                c["sticky"] = True
                c["cost"] -= HYST_BONUS_M
    # COAST HOLD (coasting_now): the previous track's propagated state has drifted past the gate -> it stays a candidate at a cost just
    # under "none": any in-gate track wins (a re-acquisition / handover = the allowed dark moment), an empty role never does
    if hold and prev is not None and int(prev) in scores and not any(c["tid"] == int(prev) for c in cands):
        mid, conf = track_match(meta, int(prev))
        if match_role(mid, role_ids) != "other":
            cands.append({"tid": int(prev), "d": float(scores[int(prev)]), "prio": False, "conf": conf, "sticky": True, "other_role": False,
                          "hold": True, "cost": float(gate) - COAST_HOLD_EPS_M})
    cands.sort(key=lambda c: (c["cost"], c["tid"]))
    return cands[:CAND_TOP_N], excluded


def joint_assign(tc: list[dict], ic: list[dict], gate: float = D.GATE_M) -> tuple[dict | None, dict | None]:
    """The best DISTINCT (target, interceptor) candidate pair by summed cost — brute force over each role's
    candidates plus "none" (cost = one gate, so any in-gate track beats leaving the role empty).  One track never
    serves both roles: when a single track is nearest to both truths it goes where the total is lowest and the
    other role takes its next candidate (or none)."""
    best, best_cost = (None, None), 2.0 * float(gate)
    for a in (None, *tc):
        for b in (None, *ic):
            if a is not None and b is not None and a["tid"] == b["tid"]:
                continue
            cost = (float(gate) if a is None else a["cost"]) + (float(gate) if b is None else b["cost"])
            if cost < best_cost - 1e-9:
                best, best_cost = (a, b), cost
    return best


def _pick_rule(chosen: dict | None, cands: list[dict], excluded: list[dict], gate: float) -> str:
    if chosen is not None:
        if chosen.get("hold"):
            return COAST_HOLD_RULE
        if chosen["prio"]:
            return match_rule(chosen["conf"])
        if chosen["sticky"] and any(c["tid"] != chosen["tid"] and c["d"] < chosen["d"] for c in cands):
            return "hysteresis"           # kept only because it is the previous track (a closer candidate exists)
        return "position gate"
    near = [x for x in excluded if x["d"] < gate]
    if near:                              # the role is empty BECAUSE its nearest in-gate track is an airliner
        return excluded_rule(min(near, key=lambda x: x["d"])["match_id"])
    return NO_TRACK_RULE


def select_tracks(tracks: dict, Tt, Ti, t_now: float, prev_tgt=None, prev_itc=None, meta: dict | None = None,
                  role_ids: dict | None = None, gate: float = D.GATE_M) -> dict:
    """Target + interceptor track selection for one tick (see the module docstring): {"tgt_tid", "itc_tid",
    "tgt_rule", "itc_rule", "scores" (target-side {tid: d}), "iscores" (interceptor-side), "tgt_cands",
    "itc_cands" (role_candidates output), "excluded": [{tid, match_id, conf, d, rule}] sorted by distance}.
    ``meta`` = snap["track_meta"] (absent -> every track is a null match -> position gate only)."""
    role_ids = role_ids or {"target": [], "interceptor": []}
    ts, is_ = side_scores(tracks, Tt, t_now), side_scores(tracks, Ti, t_now)
    # the coast hold is the TARGET's (its metrics cards are what must not go dark); the interceptor track is display-only and keeps
    # the plain gate (8/28 F1 07:22:07: the coasting-away #129 leaves the interceptor role as before)
    tc, tex = role_candidates(ts, meta, "target", role_ids, prev_tgt, gate, hold=coasting_now(tracks, prev_tgt, t_now))
    ic, iex = role_candidates(is_, meta, "interceptor", role_ids, prev_itc, gate)
    a, b = joint_assign(tc, ic, gate)
    ex: dict[int, dict] = {}
    for x in tex + iex:
        e = ex.setdefault(x["tid"], {"tid": x["tid"], "match_id": x["match_id"], "conf": x["conf"], "d": x["d"], "rule": excluded_rule(x["match_id"])})
        e["d"] = min(e["d"], x["d"])
    return {"tgt_tid": None if a is None else a["tid"], "itc_tid": None if b is None else b["tid"],
            "tgt_rule": _pick_rule(a, tc, tex, gate), "itc_rule": _pick_rule(b, ic, iex, gate),
            "scores": ts, "iscores": is_, "tgt_cands": tc, "itc_cands": ic,
            "excluded": sorted(ex.values(), key=lambda e: (e["d"], e["tid"]))}


# ── Separation / engagement ──────────────────────────────────────────────────
def separation_series(Tt: np.ndarray, Ti: np.ndarray, t_start: float, t_now: float, min_span_s: float = SEP_MIN_SPAN_S) -> dict:
    """1 Hz truth-to-truth separation grid from min(t_start, t_now − min_span_s) to t_now (NaN where either
    truth is missing): 3D + horizontal separation and a centred closing rate (+ = closing)."""
    grid = np.arange(np.floor(min(t_start, t_now - float(min_span_s))), np.floor(t_now) + 1.0)
    if len(grid) < 2:
        grid = np.array([np.floor(t_now) - 1, np.floor(t_now)])
    a, b = interp_truth(Tt, grid), interp_truth(Ti, grid)
    d = a[:, :3] - b[:, :3]
    sep = np.sqrt((d * d).sum(axis=1))
    horiz = np.hypot(d[:, 0], d[:, 1])
    closing = np.full(len(grid), np.nan)
    if len(grid) > 2:
        closing[1:-1] = -(sep[2:] - sep[:-2]) / 2.0  # centered, 1 Hz -> m/s (+ = closing)
    return {"t": grid, "sep": sep, "horiz": horiz, "closing": closing, "tgt": a, "itc": b}


def predicted_miss(pt: np.ndarray | None, pi: np.ndarray | None) -> dict:
    """Straight-line CPA from current truth states: t_go and miss distance."""
    if pt is None or pi is None:
        return {"tgo": None, "miss": None, "rel_speed": None}
    r = pi[1:4] - pt[1:4]
    v = pi[4:7] - pt[4:7]
    vv = float(v @ v)
    if vv < 1.0:
        return {"tgo": None, "miss": None, "rel_speed": float(np.sqrt(vv))}
    tgo = float(-(r @ v) / vv)
    if tgo <= 0:
        return {"tgo": tgo, "miss": None, "rel_speed": float(np.sqrt(vv))}
    m = r + v * tgo
    return {"tgo": tgo, "miss": float(np.sqrt(m @ m)), "rel_speed": float(np.sqrt(vv))}


def cpa_gate(S: dict, cpa: tuple | None, gate_m: float = CPA_GATE_M_DEFAULT, *, rise_m: float = CPA_RISE_M,
             rise_frac: float = CPA_RISE_FRAC, look_s: float = CPA_LOOK_S) -> bool:
    """True when the running closest-so-far ``cpa`` = (sep, t, E, N, horiz) is a VALID CPA on the
    separation series ``S`` (separation_series output): (a) sep < gate_m AND (b) a true local
    minimum — the separation rose by >= rise_m OR >= rise_frac·sep within the following look_s
    seconds (the pass is over).  A minimum at the newest sample (still closing) is never valid."""
    if cpa is None or S is None or not len(S.get("t", ())):
        return False
    sep_min, t_min = float(cpa[0]), float(cpa[1])
    if not np.isfinite(sep_min) or sep_min >= float(gate_m):
        return False
    t, sep = np.asarray(S["t"], float), np.asarray(S["sep"], float)
    m = (t > t_min) & (t <= t_min + float(look_s)) & np.isfinite(sep)
    if not m.any():
        return False
    rise = float(np.max(sep[m])) - sep_min
    return bool(rise >= min(float(rise_m), float(rise_frac) * sep_min))


def interp_track(a: np.ndarray | None, ts: np.ndarray, max_gap: float = TRACK_SEP_GAP_S) -> np.ndarray:
    """(E,N,U) of a TK-layout track at ``ts`` (linear between published states); NaN where the NEAREST published
    state is more than ``max_gap`` s away (the track had no state there) and everywhere for an empty track."""
    ts = np.asarray(ts, float)
    out = np.full((len(ts), 3), np.nan)
    if a is None or not len(a) or not len(ts):
        return out
    a = np.asarray(a, float)
    o = np.argsort(a[:, 0], kind="stable")
    a = a[o]
    t = a[:, 0]
    for k in range(3):
        out[:, k] = np.interp(ts, t, a[:, 1 + k])
    idx = np.searchsorted(t, ts).clip(0, len(t) - 1)
    near = np.minimum(np.abs(ts - t[idx]), np.abs(ts - t[np.maximum(idx - 1, 0)]))
    out[near > float(max_gap)] = np.nan
    return out


def track_separation(a: np.ndarray | None, itc: np.ndarray, grid: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Interceptor truth (``itc`` = interp_truth rows on ``grid``) <-> target TRACK ``a`` (TK rows): (sep3d, horiz, trk)
    on the grid, NaN where either side is missing."""
    trk = interp_track(a, grid)
    n = min(len(grid), len(itc)) if itc is not None else 0
    d = np.full((len(grid), 3), np.nan)
    if n:
        d[:n] = np.asarray(itc, float)[:n, :3] - trk[:n]
    return np.sqrt((d * d).sum(axis=1)), np.hypot(d[:, 0], d[:, 1]), trk


def running_min(prev: tuple | None, t: np.ndarray, sep: np.ndarray, pos: np.ndarray, horiz: np.ndarray) -> tuple | None:
    """The (sep, t, E, N, horiz) running minimum: ``prev`` unless the series holds a lower finite sample."""
    sep = np.asarray(sep, float)
    if not np.isfinite(sep).any():
        return prev
    i = int(np.nanargmin(sep))
    cand = (float(sep[i]), float(t[i]), float(pos[i, 0]), float(pos[i, 1]), float(horiz[i]))
    return cand if prev is None or cand[0] < prev[0] else prev


def best_validated_pass(t: np.ndarray, sep: np.ndarray, pos: np.ndarray, horiz: np.ndarray, gate_m: float, mask: np.ndarray | None = None) -> tuple | None:
    """The LOWEST separation sample that passes the CPA gate (below ``gate_m`` and a true local minimum by cpa_gate's
    rise rule, evaluated on the WHOLE series) anywhere in a history series — None when no pass in the series qualifies.
    ``mask`` (optional) restricts the CANDIDATE samples (e.g. the seconds a track was target-side) without hiding the
    rise after them.  Used to seed the CPA pairs from the connect-time history scan (a hover on the pad or a
    still-closing tail never seeds anything)."""
    t, sep = np.asarray(t, float), np.asarray(sep, float)
    if not len(t) or not np.isfinite(sep).any():
        return None
    S = {"t": t, "sep": sep}
    cand_sep = sep if mask is None else np.where(np.asarray(mask, bool), sep, np.nan)
    if not np.isfinite(cand_sep).any():
        return None
    order = np.argsort(cand_sep, kind="stable")
    for i in order:
        if not np.isfinite(cand_sep[i]) or cand_sep[i] >= float(gate_m):
            break
        cand = (float(sep[i]), float(t[i]), float(pos[i, 0]), float(pos[i, 1]), float(horiz[i]))
        if cpa_gate(S, cand, gate_m):
            return cand
    return None


def airborne_mask(tgt: np.ndarray, itc: np.ndarray, alt_a: np.ndarray | None = None, spd: np.ndarray | None = None,
                  min_m: float = AIRBORNE_MIN_M, min_spd: float = AIRBORNE_TGT_SPEED_MPS) -> np.ndarray:
    """Per-grid-sample AIRBORNE gate: ``tgt`` / ``itc`` = interp_truth rows (E,N,U,vE,vN,vU) of the target / interceptor truth
    on the grid.  True where BOTH entities are >= ``min_m`` above the radar AND the target's ground speed is >= ``min_spd``.
    ``alt_a`` replaces the target's altitude (the TRACK's U for the track pair); ``spd`` replaces the target speed where
    finite (the track pair falls back to it where the target truth is missing).  NaN anywhere -> False."""
    tgt, itc = np.asarray(tgt, float), np.asarray(itc, float)
    n = min(len(tgt), len(itc))
    out = np.zeros(max(len(tgt), len(itc)), bool)
    if n == 0:
        return out
    alt_t = tgt[:n, 2] if alt_a is None else np.asarray(alt_a, float)[:n]
    spd_t = np.hypot(tgt[:n, 3], tgt[:n, 4])
    if spd is not None:
        s2 = np.asarray(spd, float)[:n]
        spd_t = np.where(np.isfinite(spd_t), spd_t, s2)
    with np.errstate(invalid="ignore"):
        ok = (alt_t >= float(min_m)) & (itc[:n, 2] >= float(min_m)) & (spd_t >= float(min_spd))
    out[:n] = ok & np.isfinite(alt_t) & np.isfinite(itc[:n, 2]) & np.isfinite(spd_t)
    return out


def validated_passes(t: np.ndarray, sep: np.ndarray, pos: np.ndarray, horiz: np.ndarray, gate_m: float, mask: np.ndarray | None = None, *,
                     rise_m: float = CPA_RISE_M, rise_frac: float = CPA_RISE_FRAC, look_s: float = CPA_LOOK_S) -> list[tuple]:
    """EVERY validated pass of a separation series, oldest first: samples i with sep[i] < gate_m that are (a) a true local
    minimum — strictly below every finite sample of the preceding look_s and not above any of the following look_s (a plateau
    counts once, at its first sample), (b) preceded by at least one finite sample inside look_s (a minimum at the window's
    first finite sample — a buffer refill edge — never qualifies), (c) followed by the cpa_gate rise (>= rise_m or rise_frac of
    the minimum within look_s: the pass is over) and (d) allowed by ``mask`` (the airborne gate / the target-side seconds).
    Returns [(sep, t, E, N, horiz)]."""
    t, sep = np.asarray(t, float), np.asarray(sep, float)
    n = len(t)
    if n < 2 or not np.isfinite(sep).any():
        return []
    ok = np.isfinite(sep) & (sep < float(gate_m))
    if mask is not None:
        ok &= np.asarray(mask, bool)[:n]
    if not ok.any():
        return []
    S = {"t": t, "sep": sep}
    out = []
    need = lambda v: min(float(rise_m), float(rise_frac) * float(v))   # noqa: E731  the rise that ends a pass at separation v

    def _separate(i: int, j: int) -> bool:
        """The lower sample j (within look_s of i) belongs to ANOTHER pass: the series rose by the full rise between them."""
        lo_, hi_ = (i, j) if i < j else (j, i)
        mid = sep[lo_ + 1:hi_]
        mid = mid[np.isfinite(mid)]
        return bool(len(mid)) and float(np.max(mid)) - float(sep[i]) >= need(sep[i])

    for i in np.flatnonzero(ok):
        ti, si = t[i], sep[i]
        before = np.flatnonzero((t < ti) & (t >= ti - float(look_s)) & np.isfinite(sep))
        after = np.flatnonzero((t > ti) & (t <= ti + float(look_s)) & np.isfinite(sep))
        if not len(before) or not len(after):
            continue
        # a true local minimum: strictly below the preceding look_s (a plateau counts once) and not above the following look_s —
        # unless the lower neighbour is a SEPARATE pass (the series fully rose in between: two passes < look_s apart both count)
        if any(sep[j] <= si and not (sep[j] < si and _separate(i, j)) for j in before):
            continue
        if any(sep[j] < si and not _separate(i, j) for j in after):
            continue
        cand = (float(si), float(ti), float(pos[i, 0]), float(pos[i, 1]), float(horiz[i]))
        if cpa_gate(S, cand, gate_m, rise_m=rise_m, rise_frac=rise_frac, look_s=look_s):
            out.append(cand)
    return out


def merge_passes(prev: list | None, new: list, gate_m: float, tol_s: float = 1.5) -> list:
    """The session pass list after a tick: ``prev`` (oldest first) with every pass now above the gate dropped, ``new`` passes
    added (a pass within tol_s of a stored one REPLACES it: the same pass re-derived from a longer / shifted window), sorted
    by time, capped at PASS_LIST_MAX (oldest dropped)."""
    out = [tuple(p) for p in (prev or ()) if p is not None and float(p[0]) < float(gate_m)]
    for c in new:
        out = [p for p in out if abs(float(p[1]) - float(c[1])) > float(tol_s)]
        out.append(tuple(c))
    out.sort(key=lambda p: float(p[1]))
    return out[-PASS_LIST_MAX:]


def latest_pass(passes: list | None) -> tuple | None:
    """The MOST RECENT validated pass (what the plots mark) or None."""
    return tuple(passes[-1]) if passes else None


def _is_pass(cand: tuple | None, passes: list | None, tol_s: float = 1.5) -> bool:
    return cand is not None and any(abs(float(p[1]) - float(cand[1])) <= tol_s for p in (passes or ()))


def _drop_stale_min(run: tuple | None, t_start: float, passes: list | None) -> tuple | None:
    """A running minimum older than the separation series' start that never became a validated pass can never be gated
    again (its samples are gone) — dropped so the window re-derives its own minimum.  A validated one is kept."""
    if run is not None and float(run[1]) < float(t_start) and not _is_pass(run, passes):
        return None
    return run


def _reset_pass_lists(s) -> None:
    """data.seek() / reset_derived() clear ``cpa_run`` + ``cpa_ok`` (and the track pair) but know nothing of the pass lists:
    both pair keys None while a pass list is non-empty = a reset happened since the last tick -> the lists go too."""
    for run_k, ok_k, list_k in (("cpa_run", "cpa_ok", "cpa_passes"), ("cpa_trk_run", "cpa_trk_ok", "cpa_trk_passes")):
        if s.get(run_k) is None and s.get(ok_k) is None and s.get(list_k):
            s[list_k] = []


def track_speed_on_grid(trk: np.ndarray) -> np.ndarray:
    """Ground speed (m/s) of an interp_track (E,N,U) series on the 1 Hz grid by central differences — the target-speed
    fallback of the track pair's airborne gate where the target truth is missing."""
    trk = np.asarray(trk, float)
    n = len(trk)
    out = np.full(n, np.nan)
    if n >= 3:
        out[1:-1] = np.hypot(trk[2:, 0] - trk[:-2, 0], trk[2:, 1] - trk[:-2, 1]) / 2.0
    return out


def seed_cpa_from_history(s, snap: dict, gate_m: float) -> dict | None:
    """Connect-time history: ``snap["cpa_hist"]`` = {"tgt", "itc" (TR rows), "tracks" {tid: TK rows}, "lo", "hi", "rev",
    "complete"} for the LOOKBACK before the live buffer (feed.live_snapshot, chunked over several ticks).  Every time
    its ``rev`` advances the truth-truth and truth-track separations over [lo, hi] are recomputed and the best validated
    pass of each seeds ``cpa_run``/``cpa_ok`` and ``cpa_trk_run``/``cpa_trk_ok`` — only when lower than what the session
    already holds (a live pass seen since connecting always wins).  Returns what was seeded (for A / tests) or None."""
    H = snap.get("cpa_hist")
    if not isinstance(H, dict) or H.get("rev") is None:
        return None
    rev = H.get("rev")
    # a rev is re-read only when it is new, or when a seek / disconnect (data.seek / reset_derived) cleared both pairs after a rev
    # that HAD seeded something (a rev that yielded nothing is marked and never re-read: the arrays did not change)
    cleared = s.get("cpa_run") is None and s.get("cpa_trk_run") is None
    if s.get("_cpa_hist_rev") == rev and (not cleared or s.get("_cpa_hist_none") == rev):
        return None
    if cleared:
        _reset_pass_lists(s)
    s["_cpa_hist_rev"] = rev
    Tt, Ti = H.get("tgt"), H.get("itc")
    lo, hi = float(H.get("lo", 0.0)), float(H.get("hi", 0.0))
    if Ti is None or not len(Ti) or hi <= lo:
        s["_cpa_hist_none"] = rev
        return None
    grid = np.arange(np.floor(lo), np.floor(hi) + 1.0)
    itc = interp_truth(Ti, grid)
    out = {"truth": None, "track": None, "lo": lo, "hi": hi}
    if Tt is not None and len(Tt):
        tgt = interp_truth(Tt, grid)
        d = tgt[:, :3] - itc[:, :3]
        sep = np.sqrt((d * d).sum(axis=1))
        air = airborne_mask(tgt, itc)
        passes = validated_passes(grid, sep, tgt, np.hypot(d[:, 0], d[:, 1]), gate_m, mask=air)
        if passes:
            s["cpa_passes"] = merge_passes(s.get("cpa_passes"), passes, gate_m)
            s["cpa_ok"] = latest_pass(s["cpa_passes"])
            best = min(passes, key=lambda p: p[0])
            if s.get("cpa_run") is None or best[0] < s["cpa_run"][0]:
                s["cpa_run"] = best
            out["truth"] = best
        # the track CPA: EVERY track's own interceptor-truth <-> track series (smooth, so the rise rule works).  A track is a
        # TARGET-track candidate by ALLEGIANCE over its whole span in the history (median distance to the target truth inside
        # D.GATE_M and not larger than to the interceptor truth — the engine's side_scores logic), never by the distance at one
        # second: at a 28 m pass the target track is often momentarily nearer the interceptor.  The interceptor's own radar
        # track (median ~10 m from the interceptor, hundreds from the target) is never a candidate.  Candidate seconds =
        # the track inside the gate of the target truth; lowest validated pass wins.
        best_t, passes_t = None, []
        for a in (H.get("tracks") or {}).values():
            trk = interp_track(a, grid)
            dT = np.hypot(trk[:, 0] - tgt[:, 0], trk[:, 1] - tgt[:, 1])
            dI = np.hypot(trk[:, 0] - itc[:, 0], trk[:, 1] - itc[:, 1])
            fin = np.isfinite(dT) & np.isfinite(dI)
            if not fin.any():
                continue
            mT, mI = float(np.median(dT[fin])), float(np.median(dI[fin]))
            if not (mT < D.GATE_M and mT <= mI):
                continue
            dd = itc[:, :3] - trk
            air_t = airborne_mask(tgt, itc, alt_a=trk[:, 2], spd=track_speed_on_grid(trk))
            for cand in validated_passes(grid, np.sqrt((dd * dd).sum(axis=1)), trk, np.hypot(dd[:, 0], dd[:, 1]), gate_m,
                                         mask=np.isfinite(dT) & (dT < D.GATE_M) & air_t):
                passes_t.append(cand)
                if best_t is None or cand[0] < best_t[0]:
                    best_t = cand
        if passes_t:
            s["cpa_trk_passes"] = merge_passes(s.get("cpa_trk_passes"), passes_t, gate_m)
            s["cpa_trk_ok"] = latest_pass(s["cpa_trk_passes"])
            if s.get("cpa_trk_run") is None or best_t[0] < s["cpa_trk_run"][0]:
                s["cpa_trk_run"] = best_t
            out["track"] = best_t
    if out["truth"] is None and out["track"] is None:
        s["_cpa_hist_none"] = rev
    return out


# ── Coverage / false tracks / errors ─────────────────────────────────────────
def coverage_pct(tracks, T, t_now, W, gate=D.GATE_M, fresh=D.FRESH_S, min_speed=2.0) -> float | None:
    grid = np.arange(np.floor(t_now - W), np.floor(t_now) + 1.0)
    tr = interp_truth(T, grid)
    moving = np.isfinite(tr[:, 0]) & (np.hypot(tr[:, 3], tr[:, 4]) >= min_speed)
    if not moving.any():
        return None
    parts = [_recent(a, t_now, W + fresh) for a in tracks.values()]
    parts = [p for p in parts if len(p)]
    if not parts:
        return 0.0
    S = np.vstack(parts)
    trS = interp_truth(T, S[:, 0])
    d = np.hypot(S[:, TK["E"]] - trS[:, 0], S[:, TK["N"]] - trS[:, 1])
    good = np.sort(S[(d < gate) & (S[:, TK["t"]] - S[:, TK["lu"]] <= fresh), 0])
    if not len(good):
        return 0.0
    g = grid[moving]
    idx = np.searchsorted(good, g).clip(1, len(good) - 1)
    near = np.minimum(np.abs(g - good[idx - 1]), np.abs(good[idx] - g))
    return float(100.0 * np.mean(near <= fresh))


def _empty_errors() -> dict:
    z = np.zeros(0)
    return {"t": z, "az": z, "el": z, "rng": z, "alt": z, "pos3d": z, "horiz": z, "kind": np.zeros(0, object),
            "sig_az": z, "sig_el": z, "sig_rng": z, "sig_alt": z, "sig_pos3d": z,
            "ve": z, "vn": z, "vu": z, "sig_ve": z, "sig_vn": z, "sig_vu": z, "n_graded": 0, "n_rows": 0, "grader": "spa"}


def truth7_to_truth8(T: np.ndarray) -> np.ndarray:
    """Dashboard TR layout (t,E,N,U_hae,vE,vN,vU[m/s]) -> corr_lib 8-col truth
    (t,E,N,U_hae,spd,vN,vE,vU) that spa_errors.spa_err_stack expects.

    U is WGS-84 HAE in BOTH dashboard paths (archive CSV column ``U_m_hae``; live
    path via corr_lib.mavlink_alt_hae_m = ft->m + geoid), which is exactly what
    the adapter documents (it hands spa HAE-in-feet; feeding wire MSL feet would
    bias alt/el by geoid_N ~ -31.4 m).  vU is already m/s: corr_lib.vu_scale()
    then resolves to 1.0 (magnitude guard), so no ft/min re-scaling happens."""
    if T is None or not len(T):
        return np.zeros((0, 8))
    T = np.asarray(T, float)
    return np.column_stack([T[:, TR["t"]], T[:, TR["E"]], T[:, TR["N"]], T[:, TR["U"]],
                            np.hypot(T[:, TR["vE"]], T[:, TR["vN"]]), T[:, TR["vN"]], T[:, TR["vE"]], T[:, TR["vU"]]])


def _legacy_track_errors(a: np.ndarray, T: np.ndarray) -> dict:
    """Pre-spa in-house math (fallback only if chaos-spa / the adapter cannot import)."""
    tr = interp_truth(T, a[:, 0])
    az_k, el_k, r_k = polar(a[:, TK["E"]], a[:, TK["N"]], a[:, TK["U"]])
    az_t, el_t, r_t = polar(tr[:, 0], tr[:, 1], tr[:, 2])
    out = _empty_errors()
    out.update({
        "t": a[:, 0], "az": wrap_deg(az_k - az_t), "el": el_k - el_t, "rng": r_k - r_t, "alt": a[:, TK["U"]] - tr[:, 2],
        "pos3d": np.sqrt((a[:, TK["E"]] - tr[:, 0]) ** 2 + (a[:, TK["N"]] - tr[:, 1]) ** 2 + (a[:, TK["U"]] - tr[:, 2]) ** 2),
        "sig_pos3d": np.sqrt(a[:, TK["sE"]] ** 2 + a[:, TK["sN"]] ** 2 + a[:, TK["sU"]] ** 2),
        "horiz": np.hypot(a[:, TK["E"]] - tr[:, 0], a[:, TK["N"]] - tr[:, 1]), "kind": state_kind(a),
        "n_graded": int((np.isfinite(az_k - az_t) & (state_kind(a) == "meas")).sum()), "n_rows": int(len(a)), "grader": "legacy",   # spa semantics: coast / tentative rows are published, not graded
        # velocity states are spa's grading only: the legacy fallback carries them as NaN (same length as t)
        **{k: np.full(len(a), np.nan) for k in (*VEL_KEYS, "sig_ve", "sig_vn", "sig_vu")},
    })
    for k in ERR_ROW_KEYS:      # every per-row key the same length as t (sig_az / sig_el / sig_rng / sig_alt: not computed here -> NaN)
        if k in out and len(np.asarray(out[k])) != len(a):
            out[k] = np.full(len(a), np.nan)
    return guard_sigmas(out)


def guard_sigmas(e: dict) -> dict:
    """A zero / negative / missing 1σ is UNKNOWN, never "perfect": such σ become NaN so the sample leaves
    the containment statistics (|err| ≤ kσ over finite pairs), the bands and the y-range floor (a live
    block-143 row without ``p_cov`` arrives with NaN sigmas; a degenerate covariance would arrive as 0)."""
    for k in SIG_KEYS:
        if k in e and len(np.asarray(e[k]).shape):
            v = np.asarray(e[k], float)
            e[k] = np.where(v > 0, v, np.nan)
    return e


PERMISSIVE = dict(coast_type="updated", use_tentative=True, use_extrapolated=True)   # spa grades EVERY published state


@st.cache_data(show_spinner=False, max_entries=256)
def _spa_errors_cached(key: tuple, _a: np.ndarray, _T8: np.ndarray, ant: tuple, tid: int, permissive: bool = False) -> dict:
    """spa_errors.spa_err_stack on one (window, track) — cached on ``key``
    (tid, window bounds, row counts, last update time); the arrays are passed
    unhashed.  Measured ~55 ms per 60-120 s window (F1 track 177, ~100 rows),
    i.e. well under the 150 ms budget, so no cadence throttling: a paused replay
    hits the cache, a playing one recomputes once per tick.
    ``permissive`` = spa's use_tentative / use_extrapolated + coast rows published as
    UPDATED: every state with a truth match is graded (same math, wider gate) — the
    source of the panel's dotted "ungraded" line, never of any statistic."""
    import spa_errors as SE  # track_correlation (on sys.path via ih/__init__)

    try:
        e = SE.spa_err_stack({int(tid): _a}, _T8, ant, **(PERMISSIVE if permissive else {}))
    except ValueError:
        # spa raises when nothing survives its CONFIRMED+UPDATED gate (tiny window,
        # all-coast / all-tentative rows, "no track rows") -> graceful empty result
        return _empty_errors()
    out = _empty_errors()
    n = len(e["t"])
    out.update({
        "t": e["t"], "az": e["az_err"], "el": e["el_err"], "rng": e["rng_err"], "alt": e["alt_err"],
        # 3D position error = spa corr_df.errors_3d (>= 0); its "σ" = the 1σ radius of the ENU covariance
        # ellipsoid = sqrt(eig1² + eig2² + eig3²) (adapter: sig_pos3d)
        "pos3d": e.get("pos3d_err", np.sqrt(e["e_err"] ** 2 + e["n_err"] ** 2 + e["u_err"] ** 2)),
        "sig_pos3d": e.get("sig_pos3d", np.full(n, np.nan)),
        "horiz": np.hypot(e["e_err"], e["n_err"]),
        # every graded sample is, by spa's gate, a CONFIRMED track's UPDATED (measurement) state
        "kind": np.full(n, "meas", dtype=object),
        "sig_az": e["sig_az"], "sig_el": e["sig_el"], "sig_rng": e["sig_rng"], "sig_alt": e["sig_alt"],
        # velocity states: spa corr_df e_dot/n_dot/u_dot errors (track − truth, m/s) and their 1σ — the adapter already
        # NaNs the σ of every row whose velocity covariance was its constant fill (13-col array / state without p_cov)
        "ve": e.get("ve_err", np.full(n, np.nan)), "vn": e.get("vn_err", np.full(n, np.nan)), "vu": e.get("vu_err", np.full(n, np.nan)),
        "sig_ve": e.get("sig_ve", np.full(n, np.nan)), "sig_vn": e.get("sig_vn", np.full(n, np.nan)), "sig_vu": e.get("sig_vu", np.full(n, np.nan)),
        "n_graded": int(np.isfinite(e["az_err"]).sum()), "n_rows": int(len(_a)), "grader": "spa",
    })
    if permissive:   # label every row by its published kind (tent / meas / coast) at its publish time
        kinds = state_kind(_a)
        idx = np.searchsorted(_a[:, 0], e["t"]).clip(0, len(_a) - 1)
        out["kind"] = kinds[idx]
    return guard_sigmas(out)


def track_errors(a: np.ndarray, T: np.ndarray, ant: tuple | None = None, t_now: float | None = None,
                 window_s: float | None = None, tid: int | None = None, permissive: bool = False) -> dict:
    """Target-track vs truth errors GRADED BY chaos-spa (spa_errors.spa_err_stack):
    az/el (deg), range/alt (m), horizontal (m) and 1-sigma per graded sample.

    Only CONFIRMED tracks' UPDATED (measurement-bearing) states are graded —
    coasting / tentative states stay on the map (hollow / triangle markers) but
    never enter the error panel or the spec statistics.  Returns the keys the
    dashboard always used (t, az, el, rng, alt, horiz, kind) plus sigmas and
    counts; empty arrays (never raises) when there is nothing to grade.
    ``ant`` = (lat, lon, HAE m) antenna origin from the snapshot; ``t_now`` /
    ``window_s`` restrict grading to the rolling window (default: whole array)."""
    if a is None or not len(a):
        return _empty_errors()
    a = np.asarray(a, float)
    if t_now is not None and window_s is not None:
        a = _recent(a, float(t_now), float(window_s))
    if len(a) < 2 or T is None or len(T) < 2:
        return _empty_errors()
    ant = tuple(float(x) for x in (ant or (D.ANT_LAT, D.ANT_LON, D.ANT_HAE)))
    # truth only has to bracket the track window (smaller spa frames, identical
    # result: spa interpolates linearly at update times and never extrapolates)
    lo, hi = float(a[0, 0]) - D.MAX_GAP_S, float(a[-1, 0]) + D.MAX_GAP_S
    i0, i1 = np.searchsorted(T[:, 0], [lo, hi])
    T8 = truth7_to_truth8(T[max(0, int(i0) - 1): int(i1) + 1])
    if len(T8) < 2:
        return _empty_errors()
    tid = int(tid if tid is not None else 0)
    key = (tid, round(float(a[0, 0]), 3), round(float(a[-1, 0]), 3), int(len(a)), round(float(a[-1, TK["lu"]]), 3),
           int(len(T8)), round(float(T8[-1, 0]), 3), ant)
    try:
        return _spa_errors_cached(key, a, T8, ant, tid, bool(permissive))
    except ImportError:  # chaos-spa / adapter missing -> legacy in-house math
        return _legacy_track_errors(a, T)


def ungraded_errors(a: np.ndarray, T: np.ndarray, graded: dict, ant: tuple | None = None, t_now: float | None = None,
                    window_s: float | None = None, tid: int | None = None) -> dict:
    """Errors of the published states spa's default gate did NOT grade (TENTATIVE / COASTING /
    extrapolated), computed the same way (spa adapter, permissive options) and reduced to the rows
    whose publish time is not among ``graded["t"]``.  Display only: the panel's dotted line."""
    if a is None or not len(a):
        return _empty_errors()
    full = track_errors(a, T, ant=ant, t_now=t_now, window_s=window_s, tid=tid, permissive=True)
    if not len(full["t"]):
        return _empty_errors()
    keep = ~np.isin(np.round(full["t"], 6), np.round(np.asarray(graded.get("t", ()), float), 6))
    out = _empty_errors()
    for k in ERR_ROW_KEYS:
        v = np.asarray(full.get(k, ()))
        out[k] = v[keep] if len(v) == len(keep) else np.full(int(keep.sum()), np.nan)   # a grader that lacks a per-row key -> NaN
    out.update({"n_graded": 0, "n_rows": int(keep.sum()), "grader": full.get("grader", "spa") + "-ungraded"})
    return out


def containment(e: dict, key: str, mask: np.ndarray | None = None) -> dict:
    """chaos-spa's containment rule for one dimension (spa.tracks.grading: containment = err / σ;
    report/utils/track_charts._containment_rate: rate = 100 · mean(|containment| ≤ 1)), i.e.
    "within 1σ" ⇔ |err| ≤ σ and "within 3σ" ⇔ |err| ≤ 3σ, over the finite (err, σ) pairs.
    Returns {n, p1, p3} (percent; None when n == 0).  Read-only on the graded numbers."""
    err = np.asarray(e.get(key, ()), float)
    sig = np.asarray(e.get(f"sig_{key}", ()), float)
    if not len(err) or len(sig) != len(err):
        return {"n": 0, "p1": None, "p3": None}
    m = np.isfinite(err) & np.isfinite(sig)
    if mask is not None:
        m &= np.asarray(mask, bool)
    n = int(m.sum())
    if n == 0:
        return {"n": 0, "p1": None, "p3": None}
    a, sg = np.abs(err[m]), sig[m]
    return {"n": n, "p1": float(100.0 * np.mean(a <= sg)), "p3": float(100.0 * np.mean(a <= 3.0 * sg))}


def containment_status(c: dict, lo: float = 55.0, hi: float = 80.0, n_min: int = 10) -> str:
    """ok when the 1σ rate sits in [lo, hi] (≈68 % expected), amber when over- or under-confident, na when n < n_min."""
    if not c or c["n"] < n_min or c["p1"] is None:
        return "na"
    return "ok" if lo <= c["p1"] <= hi else "amber"


OBS_GATE_DEG, OBS_GATE_M = 8.0, 800.0


def obs_rel_errors(obs: np.ndarray, T: np.ndarray, t_now: float | None = None, window_s: float | None = None) -> dict:
    """Raw radar obs (t, az_rad, el_rad, rng_m) as obs − truth errors relative to the TARGET truth,
    the track_correlation/ql_flight_build.py ``rel_errors`` math: truth az = atan2(E, N),
    el = atan2(U, hypot(E, N)), range = slant, alt = rng·sin(el) − U_truth; gate |Δaz| < 8°,
    |Δel| < 8°, |Δrng| < 800 m so both drones' returns stay visible.  Truth is the dashboard's
    gap-guarded interpolation (NaN outside coverage drops the row).  Display-only overlay."""
    z = np.zeros(0)
    empty = {"t": z, "az": z, "el": z, "rng": z, "alt": z, "n": 0}
    if obs is None or not len(obs) or T is None or len(T) < 2:
        return empty
    o = np.asarray(obs, float)
    if t_now is not None and window_s is not None:
        o = o[(o[:, 0] >= float(t_now) - float(window_s)) & (o[:, 0] <= float(t_now))]
    if not len(o):
        return empty
    tr = interp_truth(T, o[:, 0])
    tE, tN, tU = tr[:, 0], tr[:, 1], tr[:, 2]
    gr = np.hypot(tE, tN)
    az_t, el_t, sl_t = np.arctan2(tE, tN), np.arctan2(tU, gr), np.hypot(gr, tU)
    az, el, rng = o[:, 1], o[:, 2], o[:, 3]
    daz = np.degrees((az - az_t + np.pi) % (2.0 * np.pi) - np.pi)
    dele = np.degrees(el - el_t)
    drng = rng - sl_t
    dalt = rng * np.sin(el) - tU
    keep = np.isfinite(daz) & (np.abs(daz) < OBS_GATE_DEG) & (np.abs(dele) < OBS_GATE_DEG) & (np.abs(drng) < OBS_GATE_M)
    return {"t": o[keep, 0], "az": daz[keep], "el": dele[keep], "rng": drng[keep], "alt": dalt[keep], "n": int(keep.sum())}


def aer_rr(E, N, U, vE=None, vN=None, vU=None):
    """ENU position (+ velocity) about the radar -> az deg [0, 360), el deg, MONOSTATIC range m and
    range rate m/s (v · r̂, OPENING positive — spa's convention; None when no velocity)."""
    E, N, U = (np.asarray(v, float) for v in (E, N, U))
    rng = np.sqrt(E * E + N * N + U * U)
    az = np.degrees(np.arctan2(E, N)) % 360.0
    el = np.degrees(np.arctan2(U, np.hypot(E, N)))
    if vE is None:
        return az, el, rng, None
    vE, vN, vU = (np.asarray(v, float) for v in (vE, vN, vU))
    with np.errstate(invalid="ignore", divide="ignore"):
        rr = (E * vE + N * vN + U * vU) / np.where(rng > 0, rng, np.nan)
    return az, el, rng, rr


ON_TARGET_GATE_M = D.GATE_M   # per-sample ON-TARGET test of the measurement-space quad: horizontal distance to the target truth < this ...
TRACK_EVENTS_CAP = 50         # ... and closer to the target than to the interceptor; track-id change log kept per session (newest last)
TX_COLOCATED_NOTE = "TX {u} / RX {u} co-located → bistatic = 2×mono"


def _enu_of_lla(lla: tuple, ant: tuple) -> np.ndarray:
    """ENU (m) of a WGS-84 (deg, deg, HAE m) point about the antenna ``ant`` (deg, deg, HAE m)."""
    from . import feed as F

    la, lo, h = float(ant[0]), float(ant[1]), float(ant[2])
    o = F.lla_to_ecef(np.radians(la), np.radians(lo), h)
    R = F.enu_rotation(np.radians(la), np.radians(lo))
    return R @ (F.lla_to_ecef(np.radians(float(lla[0])), np.radians(float(lla[1])), float(lla[2])) - o)


def bistatic_series(E, N, U, vE=None, vN=None, vU=None, tx_enu=None):
    """BISTATIC range (m) and range rate (m/s, OPENING positive = spa's grading convention) of ENU
    samples about the RECEIVER (at the ENU origin) for a transmitter at ``tx_enu`` (None = co-located):
    chaos-spa's kernels spa.geometry.bistatic_range_m (|Tx−T| + |Rx−T| − |Tx−Rx|) and
    bistatic_range_rate_mps(closing_positive=False) per sample, so the numbers are the spa report's.
    Rotation-invariant, so ENU vectors are as good as ECEF ones.  Range rate None without velocity."""
    E, N, U = (np.asarray(v, float) for v in (E, N, U))
    n = len(E)
    rx = np.zeros(3)
    tx = np.zeros(3) if tx_enu is None else np.asarray(tx_enu, float)
    P = np.column_stack([E, N, U])
    try:
        from spa.geometry import bistatic_range_m, bistatic_range_rate_mps
    except ImportError:   # spa missing: same formulas in-house
        bistatic_range_m = lambda t, r, p: float(np.linalg.norm(p - t) + np.linalg.norm(p - r) - np.linalg.norm(t - r))  # noqa: E731

        def bistatic_range_rate_mps(t6, r6, p6, closing_positive=True):
            a, b = p6[:3] - t6[:3], p6[:3] - r6[:3]
            op = float(np.dot(a, p6[3:]) / np.linalg.norm(a) + np.dot(b, p6[3:]) / np.linalg.norm(b))
            return -op if closing_positive else op
    rng = np.full(n, np.nan)
    ok = np.isfinite(P).all(axis=1) & (np.hypot(np.hypot(E, N), U) > 0)
    for i in np.flatnonzero(ok):
        rng[i] = bistatic_range_m(tx, rx, P[i])
    if vE is None:
        return rng, None
    V = np.column_stack([np.asarray(vE, float), np.asarray(vN, float), np.asarray(vU, float)])
    rr = np.full(n, np.nan)
    tx6, rx6 = np.concatenate([tx, np.zeros(3)]), np.zeros(6)
    okv = ok & np.isfinite(V).all(axis=1)
    for i in np.flatnonzero(okv):
        rr[i] = bistatic_range_rate_mps(tx6, rx6, np.concatenate([P[i], V[i]]), closing_positive=False)
    return rng, rr


def on_target_mask(w: np.ndarray, Tt: np.ndarray | None, Ti: np.ndarray | None, gate: float = ON_TARGET_GATE_M) -> np.ndarray:
    """Per-sample ON-TARGET flag of a TK-layout track slice: horizontal distance to the target truth < gate
    AND not closer to the interceptor truth than to the target (a stolen / dragged track is DEPARTED from
    the sample it leaves the target).  All False without target truth."""
    n = len(w)
    if Tt is None or len(Tt) < 2 or not n:
        return np.zeros(n, bool)
    tr = interp_truth(Tt, w[:, 0])
    dT = np.hypot(w[:, TK["E"]] - tr[:, 0], w[:, TK["N"]] - tr[:, 1])
    on = np.isfinite(dT) & (dT < gate)
    if Ti is not None and len(Ti) > 1:
        ti = interp_truth(Ti, w[:, 0])
        dI = np.hypot(w[:, TK["E"]] - ti[:, 0], w[:, TK["N"]] - ti[:, 1])
        on &= ~(np.isfinite(dI) & (dI < dT))
    return on


def meas_space(snap: dict, A: dict, window_s: float) -> dict:
    """Series for the MEASUREMENT SPACE quad (truth, tracks & obs vs time, BISTATIC about TX/RX):
    {"t_lo", "t_now", "truth": {t, az, el, rng, rr, rng_mono}, "truth_itc": {…}, "tracks": [{tid, role, t, az, el, rng, rr,
     on, kind}], "obs": {t, az, el, rng, rr}, "obs_rr": bool, "geom": {colocated, tx, rx, note}}.
    rng = BISTATIC range (m) = |TX→target| + |target→RX| − |TX−RX| and rr = its rate (m/s, OPENING positive),
    both through chaos-spa's spa.geometry kernels (bistatic_series); RX = the run's antenna origin, TX =
    snap["tx"] (LLA) when the run publishes one, else co-located (every vprime MRU) -> bistatic = 2 x mono,
    flagged in geom["note"].  ``on`` = per-sample ON-TARGET flag (on_target_mask: < 150 m from the target
    truth and closer to it than to the interceptor; interceptor-side tracks use the mirror test), ``kind`` =
    state_kind per sample (meas / tent / coast).  Tracks = every track target-side at some point in the
    window (median horizontal distance to target truth < GATE over its window samples, or the engine's
    current target / alt track) + the interceptor-side track (role "interceptor").  With NO TARGET TRUTH in
    the window (no MAVLink feed at all, or the interceptor-only feed of 2026-09-15) nothing can be
    correlated: every other track comes back with role "free" (grey, ungraded) and the obs are ungated
    unless a target feed exists and is merely in a gap.  Obs (t, az_rad, el_rad,
    rng_m, rr, bi_rng_m, bi_rr) are kept through the same 8° / 8° / 800 m gates as the error overlay so both
    drones' returns appear; the obs bistatic pair comes from block-103 amb_bistatic_rng_km /
    amb_bistatic_rng_rate_ms when present, else 2 x (amb_rng_km / -amb_dop_ms) for the co-located TX; obs_rr
    is True only when a finite obs range rate exists (live mongo only: amb_dop is not archived)."""
    t_now = float(A["t_now"])
    t_lo = t_now - float(window_s)
    Tt, Ti = snap.get("tgt"), snap.get("itc")
    ant = tuple(snap.get("ant") or A.get("ant") or (D.ANT_LAT, D.ANT_LON, D.ANT_HAE))
    tx = snap.get("tx")
    tx_enu = _enu_of_lla(tx, ant) if tx else None
    colocated = tx_enu is None or float(np.linalg.norm(tx_enu)) < 1.0
    unit = str(A.get("unit") or D.UNIT).replace("MRU", "")
    note = TX_COLOCATED_NOTE.format(u=unit) if colocated else f"TX {tx[0]:.4f}, {tx[1]:.4f} / RX {ant[0]:.4f}, {ant[1]:.4f} · baseline {np.linalg.norm(tx_enu) / 1000:.2f} km"
    out = {"t_lo": t_lo, "t_now": t_now, "truth": None, "truth_itc": None, "tracks": [], "obs": None, "obs_rr": False,
           "geom": {"colocated": bool(colocated), "tx": tuple(tx) if tx else None, "rx": ant, "note": note}}

    def truth_series(T):
        if T is None or not len(T):
            return None
        w = T[(T[:, 0] >= t_lo) & (T[:, 0] <= t_now)]
        if not len(w):
            return None
        az, el, rng_m, _ = aer_rr(w[:, TR["E"]], w[:, TR["N"]], w[:, TR["U"]])
        rng, rr = bistatic_series(w[:, TR["E"]], w[:, TR["N"]], w[:, TR["U"]], w[:, TR["vE"]], w[:, TR["vN"]], w[:, TR["vU"]], tx_enu)
        return {"t": w[:, 0], "az": az, "el": el, "rng": rng, "rr": rr, "rng_mono": rng_m}

    out["truth"], out["truth_itc"] = truth_series(Tt), truth_series(Ti)
    tracks = snap.get("tracks") or {}
    tgt_tid, alt_tid, itc_tid = A.get("tgt_tid"), A.get("alt_tid"), A.get("itc_tid")
    excluded = {e["tid"] for e in (A.get("excluded_tracks") or ()) if isinstance(e, dict)}   # ADS-B-matched airliners: never a "target-side" line
    has_tgt = Tt is not None and len(Tt) > 1
    no_truth = not A.get("has_truth", bool(Tt is not None and len(Tt)))
    # with no TARGET truth (no feed at all, or the interceptor-only case) nothing can be correlated: every track
    # that is not the interceptor's is a "free" (grey, ungraded) line, exactly as in the no-truth-at-all case
    tgt_expected = bool(A.get("tgt_feed", not no_truth))
    for tid, a in sorted(tracks.items(), key=lambda kv: float(kv[1][0, 0]) if len(kv[1]) else 0.0):   # first appearance order (colour slots)
        if tid in excluded:
            continue
        w = _recent(np.asarray(a, float), t_now, float(window_s))
        if len(w) < 2:
            continue
        role = None
        if tid == itc_tid:
            role = "interceptor"
        elif tid in (tgt_tid, alt_tid):
            role = "target"
        elif has_tgt:
            tr = interp_truth(Tt, w[:, 0])
            d = np.hypot(w[:, TK["E"]] - tr[:, 0], w[:, TK["N"]] - tr[:, 1])
            d = d[np.isfinite(d)]
            if len(d) and float(np.median(d)) < D.GATE_M:
                role = "target"
        else:
            role = "free"          # no target truth to correlate against: every track, grey, ungraded
        if role is None:
            continue
        az, el, _, _ = aer_rr(w[:, TK["E"]], w[:, TK["N"]], w[:, TK["U"]])
        rng, rr = bistatic_series(w[:, TK["E"]], w[:, TK["N"]], w[:, TK["U"]], w[:, TK["vE"]], w[:, TK["vN"]], w[:, TK["vU"]], tx_enu)
        # role-independent per-sample side tests (A renders a stolen track's earlier target period from these,
        # whatever role the joint assignment gives the track now): on_tgt = dT < gate and dT <= dI;
        # on_itc = dI < gate and dI < dT (mutually exclusive; both all-False without the respective truth)
        on_tgt = on_target_mask(w, Tt, Ti)
        on_itc_incl = on_target_mask(w, Ti, Tt)      # mirror: on the INTERCEPTOR and not closer to the target (dI <= dT)
        on_itc = on_itc_incl & ~on_tgt               # strict dI < dT: a sample is never on both sides
        if role == "target":
            on = on_tgt
        elif role == "interceptor":
            on = on_itc_incl
        else:
            on = np.ones(len(w), bool)
        out["tracks"].append({"tid": int(tid), "role": role, "t": w[:, 0], "az": az, "el": el, "rng": rng, "rr": rr, "on": on,
                              "on_tgt": on_tgt, "on_itc": on_itc, "kind": state_kind(w)})
    obs = snap.get("obs")
    if obs is not None and len(obs):
        o = np.asarray(obs, float)
        o = o[(o[:, 0] >= t_lo) & (o[:, 0] <= t_now)]
        if o.shape[1] < D.OBS_COLS:
            o = np.column_stack([o, np.full((len(o), D.OBS_COLS - o.shape[1]), np.nan)])
        keep = np.ones(len(o), bool)
        if has_tgt and len(o):
            tr = interp_truth(Tt, o[:, 0])
            gr = np.hypot(tr[:, 0], tr[:, 1])
            az_t, el_t, sl_t = np.arctan2(tr[:, 0], tr[:, 1]), np.arctan2(tr[:, 2], gr), np.hypot(gr, tr[:, 2])
            daz = np.degrees((o[:, 1] - az_t + np.pi) % (2.0 * np.pi) - np.pi)
            keep = np.isfinite(daz) & (np.abs(daz) < OBS_GATE_DEG) & (np.abs(np.degrees(o[:, 2] - el_t)) < OBS_GATE_DEG) & (np.abs(o[:, 3] - sl_t) < OBS_GATE_M)
        elif tgt_expected:
            keep = np.zeros(len(o), bool)   # a TARGET feed exists but no target truth in the window: nothing to gate against
        # no target feed at all (no truth, or interceptor-only): every obs is kept ungated, like the tracks
        if keep.any():
            o = o[keep]
            bi_rng, bi_rr = o[:, 5].copy(), o[:, 6].copy()
            if colocated:   # unit publishes no bistatic pair: co-located TX -> exactly 2 x the monostatic values
                m = ~np.isfinite(bi_rng)
                bi_rng[m] = 2.0 * o[m, 3]
                m = ~np.isfinite(bi_rr)
                bi_rr[m] = 2.0 * o[m, 4]
            out["obs"] = {"t": o[:, 0], "az": np.degrees(o[:, 1]) % 360.0, "el": np.degrees(o[:, 2]), "rng": bi_rng, "rr": bi_rr, "rng_mono": o[:, 3]}
            out["obs_rr"] = bool(np.isfinite(bi_rr).any())
    return out


def _events_list(s) -> list[dict]:
    """The session's track-event log as dicts; legacy (t, role, old, new) tuples left by an older session are
    converted in place (rule "")."""
    ev = s.get("track_events")
    if not isinstance(ev, list):
        return []
    out = []
    for e in ev:
        if isinstance(e, dict):
            out.append(e)
        elif isinstance(e, (tuple, list)) and len(e) >= 4:
            out.append({"t": float(e[0]), "role": str(e[1]), "old": e[2], "new": e[3], "rule": str(e[4]) if len(e) > 4 else ""})
    return out


def track_events(s, t_now: float, tgt_tid, itc_tid, tgt_rule: str = "", itc_rule: str = "") -> list[dict]:
    """Append {"t", "role", "old", "new", "rule"} to the session log for every target / interceptor track-id
    change since the previous tick (s["tgt_tid"] / s["_itc_tid_prev"] hold the previous ids; ``rule`` = why the
    NEW pick was made, A["tgt_rule"] / A["itc_rule"]), cap the log at TRACK_EVENTS_CAP (oldest dropped, newest
    last) and return it.  An unchanged tick appends nothing (paused replays rerun the same t_now); a lost track
    logs new None with the role's empty-rule ("no track in gate" / "excluded ADS-B …")."""
    ev = _events_list(s)
    for role, old, new, rule in (("target", s.get("tgt_tid"), tgt_tid, tgt_rule), ("interceptor", s.get("_itc_tid_prev"), itc_tid, itc_rule)):
        if old != new:
            ev.append({"t": float(t_now), "role": role, "old": None if old is None else int(old),
                       "new": None if new is None else int(new), "rule": str(rule or "")})
    del ev[:-TRACK_EVENTS_CAP]
    s["track_events"] = ev
    return ev


def coast_times(a: np.ndarray | None, t_now: float, window_s: float) -> np.ndarray:
    """Publish times of COASTING states (state_kind == 'coast') of one track inside the window —
    the error panel breaks its line/bands across them ("gaps = coasting")."""
    if a is None or not len(a):
        return np.zeros(0)
    r = _recent(np.asarray(a, float), float(t_now), float(window_s))
    return r[state_kind(r) == "coast", 0] if len(r) else np.zeros(0)


def state_counts(a: np.ndarray | None, t_now: float, window_s: float) -> dict:
    """One track's PUBLISHED-STATE history inside the window, counted by state kind, plus its update rate.

    Counted off the raw track rows (TK layout) — every state the radar published — never off the graded
    rows, so the numbers are the track's own update history and not a statement about grading:
    ``conf`` = CONFIRMED states carrying a measurement update (state_kind "meas"), ``tent`` = TENTATIVE
    (track_state 1), ``coast`` = states published without a fresh measurement (state_kind "coast").
    ``hz`` = n / min(window_s, t_now - first row in the window): a track that was created 40 s ago is
    rated over those 40 s, not over the whole window (else every young track reads slow).
    Returns zeros (hz 0.0) when the track has no row in the window — never raises."""
    out = {"conf": 0, "tent": 0, "coast": 0, "n": 0, "hz": 0.0, "span_s": 0.0}
    if a is None or not len(a):
        return out
    r = _recent(np.asarray(a, float), float(t_now), float(window_s))
    if not len(r):
        return out
    k = state_kind(r)
    out.update({"conf": int((k == "meas").sum()), "tent": int((k == "tent").sum()),
                "coast": int((k == "coast").sum()), "n": int(len(r))})
    span = max(0.0, min(float(window_s), float(t_now) - float(r[0, TK["t"]])))
    out["span_s"] = span
    out["hz"] = float(out["n"] / span) if span > 0 else 0.0
    return out


def rmse(x: np.ndarray) -> float | None:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.sqrt(np.mean(x * x))) if len(x) else None


def spa_stats(e: dict, mask: np.ndarray | None = None) -> dict:
    """{az, el, rng, alt: {n, mean, std, rmse, rmse95}} via spa_errors.spa_summary —
    spa's own conventions (az/el/rng: meas_summary ddof=1 + nearest-quantile
    95.5 % trimmed RMSE; alt: target_summary ddof=0 + linear-percentile).
    ``rmse`` is the plain untrimmed RMSE, kept as the secondary number."""
    empty = {"n": 0, "mean": None, "std": None, "rmse": None, "rmse95": None}
    keys = {"az": "az_err", "el": "el_err", "rng": "rng_err", "alt": "alt_err"}
    if e is None or not len(e.get("t", ())):
        return {k: dict(empty) for k in keys}
    m = np.ones(len(e["t"]), bool) if mask is None else np.asarray(mask, bool)
    errs = {v: np.asarray(e[k], float)[m] for k, v in keys.items()}
    try:
        import spa_errors as SE

        s = SE.spa_summary(errs)
        return {k: {kk: s[v].get(kk) for kk in ("n", "mean", "std", "rmse", "rmse95")} for k, v in keys.items()}
    except ImportError:
        out = {}
        for k, v in keys.items():
            x = errs[v][np.isfinite(errs[v])]
            out[k] = dict(empty) if not len(x) else {"n": int(len(x)), "mean": float(x.mean()), "std": float(x.std()),
                                                     "rmse": rmse(x), "rmse95": rmse(x)}
        return out


HEAVY_ROWS = 250      # graded-window size above which spa_err_stack exceeds ~150 ms (300 s spec windows)
HEAVY_PERIOD_S = 2.0  # ...then recompute at most every 2 s of wall time while the clock advances


def _windowed_errors(s, a, T, ant, t_now: float, window_s: float, tid, slot: str = "", graded: dict | None = None) -> dict:
    """track_errors() with a 2 s wall-clock timer for HEAVY windows only.
    Measured spa_err_stack latency on the F1 replay: 60-120 s windows (60-230
    rows) 50-110 ms -> recomputed every tick; 300 s windows (300-420 rows)
    120-180 ms -> the last result is reused for HEAVY_PERIOD_S between
    recomputes.  A paused replay always hits st.cache_data regardless."""
    n = 0 if a is None else int(len(_recent(np.asarray(a, float), t_now, window_s)))
    key = f"_err_timer{slot}"
    last = s.get(key)
    if n > HEAVY_ROWS and last and last["tid"] == tid and time.time() - last["wall"] < HEAVY_PERIOD_S:
        return last["errors"]
    if graded is not None:   # the ungraded companion of an already-graded window
        e = ungraded_errors(a, T, graded, ant=ant, t_now=t_now, window_s=window_s, tid=tid)
    else:
        e = track_errors(a, T, ant=ant, t_now=t_now, window_s=window_s, tid=tid)
    if n > HEAVY_ROWS:
        s[key] = {"tid": tid, "wall": time.time(), "errors": e}
    return e


# ── Main analysis ────────────────────────────────────────────────────────────
def analyze(snap: dict, params: dict) -> dict:
    s = session_state()   # st.session_state under Streamlit, a module dict when run bare (tests)
    t_now = float(snap["t_now"])
    Tt, Ti, tracks = snap["tgt"], snap["itc"], snap["tracks"]
    trail_s = float(params.get("trail_s", 20))
    W = float(params.get("spec_window", 120))
    A: dict = {"t_now": t_now, "ok": snap["ok"], "err": snap.get("err"), "stale": snap.get("stale", False),
               "data_age": snap.get("data_age"), "anchored": bool(snap.get("anchored", False)), "n_adsb": int(snap.get("n_adsb", 0) or 0)}
    has_truth = bool(len(Tt) or len(Ti)) if Tt is not None and Ti is not None else False
    A["has_truth"] = bool(snap.get("has_truth", has_truth)) or has_truth
    # PER-ROLE truth (2026-09-15, MRU91 live: the unit published MAVLink truth for the INTERCEPTOR only — feed
    # "14551,mavlink_2" alive, no "14550,mavlink_1" target feed).  "No TARGET truth" is NOT "no truth": the
    # interceptor trail / head still draw, the radar tracks draw grey and ungraded, and only the target-graded
    # numbers (errors, velocity Δ, CPA / separation) are unavailable.
    A["has_tgt_truth"] = bool(Tt is not None and len(Tt))
    A["has_itc_truth"] = bool(Ti is not None and len(Ti))
    A["no_tgt_truth"] = bool(A["has_truth"] and not A["has_tgt_truth"])     # something to show, nothing to grade
    # is a TARGET feed even expected? (a momentary gap in a live target feed must stay gated, an absent feed must not)
    A["tgt_feed"] = bool(A["has_tgt_truth"] or any(f.get("role") == "target" for f in (snap.get("feeds") or []) if isinstance(f, dict)))

    # truth heads + trails
    A["tgt_now"], A["itc_now"] = latest(Tt, t_now), latest(Ti, t_now)
    A["tgt_trail"], A["itc_trail"] = trail(Tt, t_now, trail_s), trail(Ti, t_now, trail_s)
    # UNASSIGNED live MAVLink drones (an id matching neither auto-assign pattern): passed through for the map only —
    # position / heading / speed / age + a short trail, no role invented and nothing graded against them
    mem = dict(s.get("_oth_hdg") or {})                      # per-id last heading (a hovering drone keeps its icon angle)
    A["other_truth"] = other_truth(snap, t_now, trail_s, mem)
    s["_oth_hdg"] = mem
    A["n_unassigned"] = len(A["other_truth"])
    # the views may append this to the status words ("2 UNASSIGNED FEEDS"); the map's track_status tag is unchanged
    A["unassigned_txt"] = f"{A['n_unassigned']} UNASSIGNED FEED{'S' if A['n_unassigned'] != 1 else ''}" if A["n_unassigned"] else ""
    A["has_any_truth"] = bool(snap.get("has_any_truth", A["has_truth"])) or A["has_truth"] or bool(A["other_truth"])

    # engagement: separation since flight start, closest-so-far, prediction
    S = separation_series(snap["tgt_hist"], snap["itc_hist"], snap.get("t_start", t_now - 600), t_now)
    A["sep"] = S
    gate_m = float(params.get("cpa_gate_m", s.get("cpa_gate_m", CPA_GATE_M_DEFAULT)))
    A["cpa_gate_m"] = gate_m
    # connect-time history (live: the LOOKBACK before the buffer, fetched over a few ticks): seeds BOTH CPA pairs with
    # the best validated pass found there before this tick's running minima are taken
    _reset_pass_lists(s)                                 # data.seek()/reset_derived() cleared the pairs -> the pass lists go too
    A["cpa_seeded"] = seed_cpa_from_history(s, snap, gate_m)
    air = airborne_mask(S["tgt"], S["itc"])              # AIRBORNE gate per grid sample (both >= AIRBORNE_MIN_M up, target moving)
    S["airborne"] = air
    sep_air = np.where(air, S["sep"], np.nan)
    passes = merge_passes(s.get("cpa_passes"), validated_passes(S["t"], S["sep"], S["tgt"], S["horiz"], gate_m, mask=air), gate_m)
    s["cpa_passes"] = passes
    A["cpa_passes"] = list(passes)                       # every validated airborne pass of the session, oldest first
    cpa = _drop_stale_min(s.get("cpa_run"), float(S["t"][0]), passes)   # a never-validated minimum older than the series can never gate: dropped
    cpa = running_min(cpa, S["t"], sep_air, S["tgt"], S["horiz"])
    s["cpa_run"] = cpa
    A["cpa_run"] = cpa                                   # session minimum, AIRBORNE-gated (the "closest so far" tile)
    ok = latest_pass(passes)                             # the MOST RECENT validated pass is THE CPA the plots mark (None until one validates)
    s["cpa_ok"] = ok
    A["cpa"] = ok
    A["cpa_valid"] = _is_pass(cpa, passes)               # the running minimum IS one of the validated passes (tile turns gold)
    # "separation now" = the newest FINITE 1 Hz grid point within the last 3 s (the last grid point
    # can sit a few hundred ms past the newest truth sample and interpolate to NaN)
    tail = S["sep"][-4:]
    fin = np.flatnonzero(np.isfinite(tail))
    A["sep_now"] = float(tail[fin[-1]]) if len(fin) else None
    A["closing_now"] = float(np.nanmean(S["closing"][-4:-1])) if len(S["closing"]) > 4 and np.isfinite(S["closing"][-4:-1]).any() else None
    A["pred"] = predicted_miss(A["tgt_now"], A["itc_now"])

    # target + interceptor tracks: radar truth match (priority; ADS-B match excluded) > position gate, chosen JOINTLY
    # as a distinct pair with per-role hysteresis (select_tracks); every id change is logged with its rule
    role_ids = role_ids_of(params)
    sel = select_tracks(tracks, Tt, Ti, t_now, s.get("tgt_tid"), s.get("_itc_tid_prev"), snap.get("track_meta"), role_ids)
    tid, itid, scores = sel["tgt_tid"], sel["itc_tid"], sel["scores"]
    A["tgt_rule"], A["itc_rule"], A["excluded_tracks"], A["role_ids"] = sel["tgt_rule"], sel["itc_rule"], sel["excluded"], role_ids
    A["tgt_cands"], A["itc_cands"], A["iscores"] = sel["tgt_cands"], sel["itc_cands"], sel["iscores"]
    A["track_events"] = list(track_events(s, t_now, tid, itid, A["tgt_rule"], A["itc_rule"]))
    s["tgt_tid"], s["_itc_tid_prev"] = tid, itid
    A["tgt_tid"], A["scores"], A["itc_tid"] = tid, scores, itid
    last_t = next((e for e in reversed(A["track_events"]) if e["role"] == "target" and e["new"] is not None), None)
    last_i = next((e for e in reversed(A["track_events"]) if e["role"] == "interceptor" and e["new"] is not None), None)
    A["tgt_change"] = last_t                                   # {"t","role","old","new","rule"} of the current target track's arrival (None = never)
    A["itc_change"] = last_i
    A["tgt_flash"] = bool(last_t and last_t["old"] is not None and t_now - last_t["t"] < TRACK_FLASH_S)    # a real handover < 10 s ago (not the first acquisition)
    A["itc_flash"] = bool(last_i and last_i["old"] is not None and t_now - last_i["t"] < TRACK_FLASH_S)
    A["unit"] = str(snap.get("unit") or D.UNIT)
    A["tgt_track"] = tracks.get(tid) if tid is not None else None
    A["itc_track"] = tracks.get(A["itc_tid"]) if A["itc_tid"] is not None else None
    # "sep · track": interceptor truth <-> the TARGET TRACK's state (the track selected above, by id) on the separation
    # grid + its own running minimum / gated CPA (cpa_trk_run / cpa_trk_ok -> A["cpa_trk"]) under the same gate rule
    S["sep_trk"], S["horiz_trk"], S["trk"] = track_separation(A["tgt_track"], S["itc"], S["t"])
    air_t = airborne_mask(S["tgt"], S["itc"], alt_a=S["trk"][:, 2], spd=track_speed_on_grid(S["trk"]))   # the track's U + the target's speed
    S["airborne_trk"] = air_t
    passes_t = merge_passes(s.get("cpa_trk_passes"), validated_passes(S["t"], S["sep_trk"], S["trk"], S["horiz_trk"], gate_m, mask=air_t), gate_m)
    s["cpa_trk_passes"] = passes_t
    A["cpa_trk_passes"] = list(passes_t)
    cpa_t = _drop_stale_min(s.get("cpa_trk_run"), float(S["t"][0]), passes_t)
    cpa_t = running_min(cpa_t, S["t"], np.where(air_t, S["sep_trk"], np.nan), S["trk"], S["horiz_trk"])
    s["cpa_trk_run"] = cpa_t
    A["cpa_trk_run"] = cpa_t
    ok_t = latest_pass(passes_t)
    s["cpa_trk_ok"] = ok_t
    A["cpa_trk"] = ok_t                                  # the track CPA the plots mark: the most recent validated airborne pass (None until one)
    A["cpa_trk_valid"] = _is_pass(cpa_t, passes_t)
    tail_t = S["sep_trk"][-4:]
    fin_t = np.flatnonzero(np.isfinite(tail_t))
    A["sep_trk_now"] = float(tail_t[fin_t[-1]]) if len(fin_t) else None
    # chaos-spa graded errors over the rolling window (antenna LLA from the snapshot:
    # archive bundle constants or the live run's TRACKS-derived origin)
    A["ant"] = tuple(snap.get("ant") or (D.ANT_LAT, D.ANT_LON, D.ANT_HAE))
    win = max(W, ERR_PANEL_S)
    A["errors"] = _windowed_errors(s, A["tgt_track"], Tt, A["ant"], t_now, win, tid)
    # second target-side track inside the gate (handover / duplicate): graded for the error PANEL only
    # (dark red line); spec tiles, chips and containment rates stay on the primary target track
    excluded_ids = {e["tid"] for e in A["excluded_tracks"]}   # an airliner (ADS-B-matched) track is never graded as a target track
    alt_tid = next((k for k, v in sorted(scores.items(), key=lambda kv: kv[1]) if k not in (tid, A["itc_tid"]) and k not in excluded_ids and v < D.GATE_M), None)
    A["alt_tid"] = alt_tid
    A["errors_alt"] = _windowed_errors(s, tracks.get(alt_tid), Tt, A["ant"], t_now, win, alt_tid, slot="_alt") if alt_tid is not None else _empty_errors()
    # the remaining published states (tentative / coasting / extrapolated) — dotted "ungraded" line, never in any statistic
    A["errors_ung"] = _windowed_errors(s, A["tgt_track"], Tt, A["ant"], t_now, win, tid, slot="_ung", graded=A["errors"]) if tid is not None else _empty_errors()
    A["errors_alt_ung"] = (_windowed_errors(s, tracks.get(alt_tid), Tt, A["ant"], t_now, win, alt_tid, slot="_alt_ung", graded=A["errors_alt"])
                           if alt_tid is not None else _empty_errors())
    A["err_tracks"] = [{"tid": k, "errors": e, "ungraded": u, "coast_t": coast_times(tracks.get(k), t_now, win)}
                       for k, e, u in ((tid, A["errors"], A["errors_ung"]), (alt_tid, A["errors_alt"], A["errors_alt_ung"]))
                       if k is not None and (len(e["t"]) or len(u["t"]))]
    # spa containment rates over the DISPLAY window (last W s) of the primary target track
    mWin = A["errors"]["t"] >= t_now - W
    # containment per card: az / el / alt = spa's |err| <= kσ; pos3d = |err_3d| <= k·σ_3d with σ_3d the 1σ radius of the
    # covariance ellipsoid (documented approximation of the Mahalanobis rule; label "3D pos · 1σ radius")
    # ve / vn / vu (velocity states): spa's |e_dot| <= k·e_dot_sigma over the rows with a REAL velocity σ only (n counts those rows;
    # n == 0 when the run publishes no covariance / the archive predates the sigv columns)
    A["contain"] = {k: containment(A["errors"], k, mWin) for k in ("az", "el", "pos3d", "alt", "rng", *VEL_KEYS)}
    # raw obs (block 103) vs target truth for the ✕ overlay — display only, never enters any metric
    A["obs_err"] = obs_rel_errors(snap.get("obs"), Tt, t_now=t_now, window_s=win)

    # other tracks: latest state within 3 s (+ the arrays themselves: the map draws them grey when there is no truth)
    others, free = [], {}
    for k, a in tracks.items():
        if k in (tid, A["itc_tid"]):
            continue
        if len(a) and t_now - a[-1, 0] <= 3.0:
            others.append((k, a[-1], state_kind(a[-1:])[0]))
            free[int(k)] = a
    A["others"], A["free_tracks"] = others, free

    # health
    tk = A["tgt_track"]
    if tk is None or not len(tk):
        # HONEST wording, three different situations (the status row and the map tag render these words as they are):
        #   no MAVLink truth at all            -> "NO TRUTH FEED" (red): nothing to correlate against
        #   interceptor truth but no target    -> "NO TARGET FEED" (amber): the pass is visible, nothing is graded
        #   target truth, no track in the gate -> "NO TRACK" (red): a real radar failure
        if not A["has_truth"]:
            A["track_state"], A["track_cls"] = "NO TRUTH FEED", "fail"
        elif not A["has_tgt_truth"]:
            A["track_state"], A["track_cls"] = "NO TARGET FEED", "amber"
        else:
            A["track_state"], A["track_cls"] = "NO TRACK", "fail"
        A["update_age"] = None
    else:
        last = tk[-1]
        age = t_now - float(last[TK["lu"]])
        A["update_age"] = age
        st_i = int(last[TK["state"]])
        if st_i == 1:
            A["track_state"], A["track_cls"] = "TENTATIVE", "amber"
        elif age > D.FRESH_S:
            A["track_state"], A["track_cls"] = "COASTING", "amber" if age < 5 else "fail"
        else:
            A["track_state"], A["track_cls"] = "CONFIRMED", "ok"
    # the target track's own published-state history over the METRICS window W (the status-line counter beside the
    # track id): conf / tent / coast counts + the track's update rate in Hz — raw rows, independent of grading
    A["tgt_counts"] = state_counts(tk, t_now, W)
    A["coverage60"] = coverage_pct(tracks, Tt, t_now, 60.0)
    e = A["errors"]
    m30 = (e["t"] >= t_now - 30) & np.isfinite(e["horiz"])
    A["horiz_med30"] = float(np.median(e["horiz"][m30])) if m30.any() else None

    # allegiance of the target track: distance to target vs interceptor over last 5 s
    A["allegiance"], A["alleg_cls"], A["dT"], A["dI"] = "—", "na", None, None
    if tk is not None and len(tk):
        r = _recent(tk, t_now, 5.0)
        tt, ti = interp_truth(Tt, r[:, 0]), interp_truth(Ti, r[:, 0])
        dT = np.hypot(r[:, TK["E"]] - tt[:, 0], r[:, TK["N"]] - tt[:, 1])
        dI = np.hypot(r[:, TK["E"]] - ti[:, 0], r[:, TK["N"]] - ti[:, 1])
        dTm = float(np.nanmedian(dT)) if np.isfinite(dT).any() else None
        dIm = float(np.nanmedian(dI)) if np.isfinite(dI).any() else None
        A["dT"], A["dI"] = dTm, dIm
        if dTm is not None and dIm is not None:
            if dIm < dTm:
                A["allegiance"], A["alleg_cls"] = "STEAL RISK", "fail"
            elif dIm < 1.5 * dTm or dIm < 80.0:
                A["allegiance"], A["alleg_cls"] = "CONTESTED", "amber"
            else:
                A["allegiance"], A["alleg_cls"] = "TARGET", "ok"
        elif dTm is not None:
            A["allegiance"], A["alleg_cls"] = "TARGET", "ok"

    n10 = sum(int(len(_recent(a, t_now, 10.0))) for a in tracks.values())
    A["tracks_per_s"] = n10 / 10.0
    A["n_tracks_active"] = sum(1 for a in tracks.values() if len(a) and t_now - a[-1, 0] <= 3.0)
    A["feeds"] = snap.get("feeds", [])

    A["coverage_w"] = coverage_pct(tracks, Tt, t_now, W)     # coverage over the metrics window (the Spec page and its RMSE95 tiles are gone)
    return A


def feed_chip(A: dict, role: str) -> tuple[str, str, str, str]:
    fs = [f for f in A.get("feeds", []) if f["role"] == role]
    if not fs:
        return (f"MAVLink {role}", "NO FEED", "no rows", "na")
    order = {"alive": 0, "stale": 1, "frozen": 2, "down": 3, "none": 4}
    best = min(fs, key=lambda f: order[f["state"]])
    n_alive = sum(1 for f in fs if f["state"] == "alive")
    cls = {"alive": "ok", "stale": "amber", "frozen": "fail", "down": "fail", "none": "na"}[best["state"]]
    age = "—" if best["age"] is None else f"{best['age']:.1f} s"
    return (f"MAVLink {role}", best["state"].upper(), f"{n_alive}/{len(fs)} feeds alive · age {age}", cls)
