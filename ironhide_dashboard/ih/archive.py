"""SAVE TO ARCHIVE — dump a time window of an MRU run (mongo) to the quickdump archive layout, so a live flight can be
replayed through the dashboard's archive path (and read by every offline tool that reads the 8/28 quickdumps).

Layout — mirrors Seawall_Ironhide_Testing/Seawall_Week_of_8-24/seawall_0824_data/2026-08-28/e65bd4f9_flight1_quickdump/ (its columns first, new ones appended):
  <ARCHIVE_ROOT>/<YYYY-MM-DD (PDT)>/<run8>_<label>/
    mavlink/<target_id>.csv  t_epoch,time_pdt,lat,lon,alt_ft_wire,E_m,N_m,U_m_hae,vel_n_mps,vel_e_mps,vert_spd_wire_ftmin,validposition
    tracks/track_<tid>.csv   t_epoch,time_pdt,E_m,N_m,U_m,vE_mps,vN_mps,vU_mps,sigE_m,sigN_m,sigU_m,total_associations,track_state,last_update_t,
                             truth_match_id,truth_match_conf,contributors,              (+3: the radar's runtime truth match, JSON contributors)
                             sigvE_mps,sigvN_mps,sigvU_mps                              (+3: 1σ filtered velocity per ENU axis, m/s — blank when the
                                                                                         state has no covariance; ih.data.archive_bundle reads them as
                                                                                         NaN, and as NaN for older archives without the columns)
    obs/obs.csv              t_epoch,time_pdt,az_rad,el_rad,rng_m,rr_mps,bi_rng_m,bi_rng_rate_mps,truth_target_id,truth_match_type   (new)
    meta.json                run, window, antenna, tx_lla, counts, layouts, mru / host, saved_at, label, columns, airborne / replay_window
  + a flight entry appended to <day>/flights.json (the 8/28 schema — n, t0, t1, t0_pdt, t1_pdt, drone_ids, segments, passes, tracking,
    issues — plus "dir", "label", "source", and the audit's airborne_segments / airborne_s / saved_t0 / saved_t1);
    ih.data.append_flight_entry assigns the flight number, ih.data.refresh_registry makes it replayable (FLIGHT_WINDOWS / FLIGHT_DIRS).

AIRBORNE AUDIT (2026-09-17) — every save is audited the moment its CSVs are written (audit_window / apply_audit): the flight
entry's REPLAY window t0/t1 is trimmed to the span the drones actually flew (the raw window kept as saved_t0/saved_t1, the
CSVs never cut), so a 21-minute save with 5.5 minutes of flying replays 5.5 minutes.  Old saves are re-audited in place with
  python -m ih.archive audit --root <IH_ARCHIVE_ROOT> [--day 2026-09-17] [--dry-run]
(audit_saved_flights: flights.json.bak first, then an atomic rewrite).  See the AIRBORNE AUDIT block below for the rule.

Truth E/N/U use the SAME frame as the live view (corr_lib.EnuFrame about the run's antenna origin; altitude FEET MSL -> m
WGS-84 HAE via corr_lib.mavlink_alt_hae_m), so a saved flight replays exactly what the Live page showed (the 8/28 dumps used
pymap3d — sub-metre different at these ranges).  Tracks go through ih.feed.track_rows: BOTH block-143 layouts (2026.3.x
x_state NED + p_cov · 2026.9.x x_state_ecef + LLA + p_cov_ecef); obs through ih.feed.obs_rows (rr = -amb_dop, bistatic pair,
truth_target).  Every query is windowed on the INDEXED time_spec.full_sec in CHUNK_S slices (a float range on
time_spec_float scans the whole run and hangs a real unit) with a long socket timeout; the dump runs as a background job
(start_save / job) with progress — never on the page's own thread.  quickdump.py (track_correlation) is a thin CLI over
save_archive: same layout, same code.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime

import numpy as np

from . import data as D
from . import feed as F
from .engine import AIRBORNE_MIN_M, AIRBORNE_TGT_SPEED_MPS   # the SAME airborne gate the engine grades passes with

CHUNK_S = 120.0                 # per-query time slice (indexed full_sec range) = progress granularity
SOCKET_TIMEOUT_MS = 120_000     # a whole-run chunk on an ADS-B-heavy unit can be slow; the page's 3 s timeout does not apply to the job
MAX_TRACK_RANGE_M = 15_000.0    # quickdump's sanity gate: tracks farther than this from the antenna are not archived
MAV_COLS = ["t_epoch", "time_pdt", "lat", "lon", "alt_ft_wire", "E_m", "N_m", "U_m_hae", "vel_n_mps", "vel_e_mps", "vert_spd_wire_ftmin", "validposition"]
TRK_COLS = ["t_epoch", "time_pdt", "E_m", "N_m", "U_m", "vE_mps", "vN_mps", "vU_mps", "sigE_m", "sigN_m", "sigU_m", "total_associations", "track_state",
            "last_update_t", "truth_match_id", "truth_match_conf", "contributors", "sigvE_mps", "sigvN_mps", "sigvU_mps"]
OBS_COLS = ["t_epoch", "time_pdt", "az_rad", "el_rad", "rng_m", "rr_mps", "bi_rng_m", "bi_rng_rate_mps", "truth_target_id", "truth_match_type"]


# ── small helpers ────────────────────────────────────────────────────────────
def iso_pdt(t: float) -> str:
    return datetime.fromtimestamp(float(t), D.PDT).isoformat()


def day_of(t: float) -> str:
    """'2026-08-28' — the PDT calendar day of an epoch (the archive's day folder)."""
    return datetime.fromtimestamp(float(t), D.PDT).strftime("%Y-%m-%d")


def run8(run: str) -> str:
    """'run_e65bd4f92a9a…' -> 'e65bd4f9' (a bare hex id -> its first 8 characters)."""
    r = str(run or "")
    r = r[4:] if r.startswith("run_") else r
    return r[:8] or "run"


def safe_label(label: str) -> str:
    """Folder-safe label: 'Flight 1' -> 'Flight_1'."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(label or "").strip()).strip("_.")
    return s or "save"


def _f(v, nd: int = 2) -> str:
    """CSV cell: '' for None / NaN, fixed decimals for floats (numpy floats included), str otherwise."""
    if v is None:
        return ""
    if isinstance(v, (float, np.floating)):
        return "" if not math.isfinite(float(v)) else f"{float(v):.{nd}f}"
    return str(v)


def _edges(t0: float, t1: float, chunk_s: float) -> list[float]:
    e = np.arange(float(t0), float(t1), float(chunk_s)).tolist()
    return e + [float(t1)] if not e or e[-1] < t1 else e


# ── the dump ─────────────────────────────────────────────────────────────────
def dump_window(col, t0: float, t1: float, out_dir: str, ant: tuple | None, *, progress=None, chunk_s: float = CHUNK_S,
                max_range_m: float = MAX_TRACK_RANGE_M) -> dict:
    """Write mavlink/ tracks/ obs/ for [t0, t1] of one run collection into ``out_dir`` (created).  ``ant`` = antenna
    (lat, lon, HAE m) or None (no TRACKS document: truth rows keep lat/lon, E/N/U blank).  ``progress(frac, msg)`` is called
    per chunk and block.  Returns the counts for meta.json."""
    import corr_lib as C

    frame = C.EnuFrame(tuple(ant)) if ant else None
    for sub in ("mavlink", "tracks", "obs"):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
    per: dict[str, dict] = {}          # target_id -> {t: (t, payload)}      (dedup across chunk edges)
    trk: dict[int, dict] = {}          # tid -> {t: (TK row, meta)}
    obs_d: dict[tuple, tuple] = {}     # (t, i, az, rng) -> obs row
    layouts = {"ned": 0, "ecef": 0}
    n_adsb_docs = 0
    edges = _edges(t0, t1, chunk_s)
    steps = max(1, len(edges) - 1)

    def rep(i: int, k: int, blk: str) -> None:
        if progress:
            progress(min(0.95, 0.95 * (i + k / 3.0) / steps), f"{blk} · {D.pdt_hms(edges[min(i + 1, steps)])} of {D.pdt_hms(t1)}")

    for i in range(steps):
        a, b = edges[i], edges[i + 1]
        for t, d in F.window(col, F.AIR_TRAFFIC, a, b, F.TRUTH_PROJECTION, extra={"payload.source": "MAVLINK"}):
            hit = False
            for p in F._payloads(d):
                if p.get("source") != "MAVLINK":
                    continue
                if p.get("lat") is None or p.get("lon") is None or p.get("altitude") is None:
                    continue                                                     # exactly what the live path (feed.truth_rows) skips
                hit = True
                tid = str(p.get("target_id") or "?").strip()
                per.setdefault(tid, {})[round(t, 3)] = (t, p)
            n_adsb_docs += not hit
        rep(i, 1, "air traffic")
        for t, d in F.window(col, F.TRACKS, a, b, F.TRACK_PROJECTION):
            meta = F.track_meta(d)
            for tid, r in F.track_rows(t, d, ant):
                if math.hypot(r[1], r[2]) > max_range_m:
                    continue
                trk.setdefault(int(tid), {})[round(t, 3)] = (r, meta.get(int(tid)))
            for p in F._payloads(d):
                if p.get("track_id") is None:
                    continue
                if p.get("x_state") is not None:
                    layouts["ned"] += 1
                elif p.get("latitude_rad") is not None:
                    layouts["ecef"] += 1
        rep(i, 2, "tracks")
        for t, d in F.window(col, F.DWELL_WITH_OBS, a, b, F.OBS_PROJECTION):
            for j, r in enumerate(F.obs_rows(t, d)):
                obs_d[(round(t, 3), j, r[1], r[3])] = r
        rep(i, 3, "observations")

    mrows = 0
    for tid, rows in per.items():
        with open(os.path.join(out_dir, "mavlink", f"{safe_label(tid)}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(MAV_COLS)
            for t, p in sorted(rows.values(), key=lambda x: x[0]):
                lat, lon, alt = float(p["lat"]), float(p["lon"]), float(p["altitude"])
                E = N = U = None
                if frame is not None:
                    E, N, U = frame.enu(lat, lon, C.mavlink_alt_hae_m(alt))
                w.writerow([f"{t:.3f}", iso_pdt(t), lat, lon, alt, _f(E), _f(N), _f(U), p.get("velocity_n_mps") or 0, p.get("velocity_e_mps") or 0,
                            p.get("vertical_speed") or 0, 1 if p.get("validposition") else 0])
                mrows += 1
    trows = 0
    for tid, rows in trk.items():
        with open(os.path.join(out_dir, "tracks", f"track_{tid}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(TRK_COLS)
            for r, m in sorted(rows.values(), key=lambda x: x[0][0]):       # TK row: t,E,N,U,sE,sN,sU,lu,assoc,state,vE,vN,vU,sigvE,sigvN,sigvU
                m = m or {}
                sv = r[13:16] if len(r) >= 16 else (float("nan"),) * 3       # velocity 1σ: blank cell when NaN (no covariance), never 0
                w.writerow([f"{r[0]:.3f}", iso_pdt(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[10]), _f(r[11]), _f(r[12]), _f(r[4]), _f(r[5]), _f(r[6]),
                            int(r[8]), int(r[9]), _f(r[7]) if r[7] else "", m.get("truth_match_id") or "", _f(m.get("truth_match_conf")),
                            m.get("contributors") or "", _f(sv[0], 3), _f(sv[1], 3), _f(sv[2], 3)])
                trows += 1
    orows = 0
    with open(os.path.join(out_dir, "obs", "obs.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(OBS_COLS)
        for k in sorted(obs_d, key=lambda k: (k[0], k[1])):
            r = obs_d[k]
            w.writerow([f"{r[0]:.3f}", iso_pdt(r[0]), f"{r[1]:.6f}", f"{r[2]:.6f}", _f(r[3], 1), _f(r[4], 3), _f(r[5], 1), _f(r[6], 3), r[7] or "", r[8] or ""])
            orows += 1
    return {"mavlink_rows": mrows, "mavlink_ids": sorted(per), "track_rows": trows, "tracks": len(trk), "obs_rows": orows, "layouts": layouts,
            "n_adsb_docs": int(n_adsb_docs)}


# ── AIRBORNE AUDIT ───────────────────────────────────────────────────────────
# A saved window is mostly dead time (drones on the pad, batteries swapped): the 9/17 "Flight 2 full" save is 21 minutes
# of which 5.5 were flown.  The audit reads the truth that was JUST written (so it is the same code for a save and for a
# re-audit of an old save) and trims the flight entry's REPLAY window to the airborne span — the data on disk is never cut.
#
# The rule is the engine's airborne gate (engine.AIRBORNE_MIN_M / AIRBORNE_TGT_SPEED_MPS, so a replayed window covers
# exactly the samples the engine can grade a pass on):
#   * a drone is AIRBORNE at a truth sample when validposition == 1, its ENU U is >= AIRBORNE_MIN_M above the RADAR and its
#     ground speed is >= AIRBORNE_TGT_SPEED_MPS (a hover on the pad is not a flight);  consecutive airborne samples join
#     into a leg while their gap is <= AIRBORNE_HOLD_S (= data.MAX_GAP_S, the no-interpolation gap);
#   * a ROLE's legs = the union over its drones (a 14551_2_* id that re-numbers mid-flight is one interceptor);
#   * AIRBORNE = the INTERSECTION of the roles that fly at all — both drones must be up, as engine.airborne_mask requires
#     (that is what pulled 9/17 "Flight 2 full" down to 07:13:40-07:19:11: the target was up from 07:12:20 but the
#     interceptor sat on the pad).  A save with only ONE role airborne (a solo sortie, e.g. 9/15) keeps that role's legs —
#     otherwise a solo flight would trim to nothing and replay its full dead window.
# Legs closer than AIRBORNE_GAP_S merge; each segment is padded LEAD_S before / TAIL_S after and clipped to the saved
# window (overlapping padded segments merge).  NO airborne segment at all -> the full window is kept with airborne_s = 0.
AIRBORNE_GAP_S = 30.0            # legs this close (or closer) are ONE replay segment
LEAD_S, TAIL_S = 20.0, 15.0      # padding around an airborne segment (the takeoff run-up / the landing)
AIRBORNE_HOLD_S = D.MAX_GAP_S    # a gap wider than this (2.5 s) breaks a leg — the truth is not interpolated across it


def _merge_ivals(ivs, gap_s: float = 0.0) -> list[tuple[float, float]]:
    """Sorted, merged [(t0, t1)] — intervals whose gap is <= ``gap_s`` become one."""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in ivs):
        if out and a - out[-1][1] <= float(gap_s):
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _intersect_ivals(a_ivs, b_ivs) -> list[tuple[float, float]]:
    """The overlap of two sorted, merged interval lists (zero-length touches dropped)."""
    out, i, j = [], 0, 0
    while i < len(a_ivs) and j < len(b_ivs):
        a, b = max(a_ivs[i][0], b_ivs[j][0]), min(a_ivs[i][1], b_ivs[j][1])
        if b > a:
            out.append((a, b))
        if a_ivs[i][1] < b_ivs[j][1]:
            i += 1
        else:
            j += 1
    return out


def drone_airborne_legs(csv_path: str, t0: float | None = None, t1: float | None = None, *, min_m: float = AIRBORNE_MIN_M,
                        min_spd: float = AIRBORNE_TGT_SPEED_MPS, hold_s: float = AIRBORNE_HOLD_S) -> list[tuple[float, float]]:
    """AIRBORNE legs [(t0, t1)] of ONE saved MAVLink truth CSV (mavlink/<target_id>.csv, MAV_COLS): samples with
    validposition == 1, U_m_hae (ENU U about the antenna) >= ``min_m`` and ground speed >= ``min_spd``, consecutive
    samples joined while their gap is <= ``hold_s``.  Blank U (a run with no TRACKS document -> no ENU) never counts.
    Never raises (a missing / broken file has no legs)."""
    legs: list[tuple[float, float]] = []
    cur: list[float] | None = None
    try:
        fh = open(csv_path, newline="")
    except OSError:
        return []
    with fh:
        for r in csv.DictReader(fh):
            try:
                t = float(r["t_epoch"])
                u = float(r["U_m_hae"])
                spd = math.hypot(float(r["vel_n_mps"] or 0.0), float(r["vel_e_mps"] or 0.0))
                valid = int(float(r.get("validposition") or 0)) == 1
            except (TypeError, ValueError, KeyError):
                continue
            if (t0 is not None and t < float(t0) - 1e-6) or (t1 is not None and t > float(t1) + 1e-6):
                continue
            if not (valid and math.isfinite(u) and u >= float(min_m) and spd >= float(min_spd)):
                continue
            if cur is not None and t - cur[1] <= float(hold_s):
                cur[1] = t
            else:
                if cur is not None:
                    legs.append((cur[0], cur[1]))
                cur = [t, t]
    if cur is not None:
        legs.append((cur[0], cur[1]))
    return legs


def _role_of(name: str, roles: tuple | None) -> str:
    """D.role_of, safe off the Streamlit thread / in the CLI (no session state -> the default patterns)."""
    try:
        return D.role_of(name, roles)
    except Exception:
        return D.role_of(name, ((), (), D.TGT_PATTERN_DEFAULT, D.ITC_PATTERN_DEFAULT))


def audit_window(out_dir: str, t0: float, t1: float, *, roles_map: dict | None = None, roles: tuple | None = None) -> dict:
    """The AIRBORNE AUDIT of one saved flight directory (see the block comment): reads mavlink/*.csv, returns
      {airborne_segments: [{t0, t1, t0_pdt, t1_pdt, drones, air_t0, air_t1, air_t0_pdt, air_t1_pdt, airborne_s}],
       airborne_s, t0, t1 (the TRIMMED replay window = first segment t0 .. last segment t1), saved_t0, saved_t1,
       legs {drone: [[t0, t1]]}, roles {drone: role}, trimmed}
    ``roles_map`` = meta.json's {"target": [ids], "interceptor": [ids]} (the mapping the flight was SAVED with, as
    ih.data.archive_bundle honours); read from meta.json when not given, then D.role_of for anything unmapped."""
    t0, t1 = float(min(t0, t1)), float(max(t0, t1))
    if roles_map is None:
        try:
            with open(os.path.join(out_dir, "meta.json")) as f:
                roles_map = json.load(f).get("roles") or {}
        except Exception:
            roles_map = {}
    saved_role = {str(i): str(r) for r, ids in (roles_map or {}).items() for i in (ids or ())}
    legs: dict[str, list[tuple[float, float]]] = {}
    who: dict[str, str] = {}
    by_role: dict[str, list[tuple[float, float]]] = {"target": [], "interceptor": []}
    for p in sorted(glob.glob(os.path.join(out_dir, "mavlink", "*.csv"))):
        name = os.path.splitext(os.path.basename(p))[0]
        role = saved_role.get(name) or _role_of(name, roles)
        if role not in by_role:
            continue
        who[name] = role
        lg = drone_airborne_legs(p, t0, t1)
        if lg:
            legs[name] = lg
            by_role[role] += lg
    air = {r: _merge_ivals(v) for r, v in by_role.items() if v}
    if len(air) >= 2:                                        # both roles flew: the span where BOTH are up (engine.airborne_mask)
        core = air["target"]
        for r, v in air.items():
            if r != "target":
                core = _intersect_ivals(core, v)
    else:                                                    # a solo sortie keeps its own legs (else it would trim to nothing)
        core = list(next(iter(air.values()), []))
    core = _merge_ivals(core, AIRBORNE_GAP_S)

    segs: list[dict] = []
    for a, b in core:
        p0, p1 = max(t0, a - LEAD_S), min(t1, b + TAIL_S)
        if segs and p0 <= segs[-1]["t1"]:                    # padding made two segments touch: one segment, two legs
            segs[-1].update(t1=max(segs[-1]["t1"], p1), air_t1=b)
            segs[-1]["airborne_s"] += b - a
        else:
            segs.append({"t0": p0, "t1": p1, "air_t0": a, "air_t1": b, "airborne_s": b - a})
    out_segs = []
    for sg in segs:
        drones = sorted(n for n, lg in legs.items()
                        if any(min(sg["air_t1"], lb) - max(sg["air_t0"], la) > 0 for la, lb in lg))
        sg["t0"], sg["t1"] = max(t0, round(sg["t0"], 3)), min(t1, round(sg["t1"], 3))     # rounding must never leave the saved window
        out_segs.append({"t0": sg["t0"], "t1": sg["t1"], "t0_pdt": iso_pdt(sg["t0"]), "t1_pdt": iso_pdt(sg["t1"]),
                         "drones": drones, "air_t0": round(sg["air_t0"], 3), "air_t1": round(sg["air_t1"], 3),
                         "air_t0_pdt": iso_pdt(sg["air_t0"]), "air_t1_pdt": iso_pdt(sg["air_t1"]), "airborne_s": round(sg["airborne_s"], 1)})
    return {"airborne_segments": out_segs, "airborne_s": round(sum(s["airborne_s"] for s in out_segs), 1),
            "saved_t0": t0, "saved_t1": t1,
            "t0": out_segs[0]["t0"] if out_segs else t0, "t1": out_segs[-1]["t1"] if out_segs else t1,
            "legs": {k: [[round(a, 3), round(b, 3)] for a, b in v] for k, v in legs.items()}, "roles": who,
            "trimmed": bool(out_segs), "rule": f"U >= {AIRBORNE_MIN_M:g} m above the radar AND ground speed >= {AIRBORNE_TGT_SPEED_MPS:g} m/s "
                                               f"(both roles when both fly) · legs merged at {AIRBORNE_GAP_S:g} s · +{LEAD_S:g} s / -{TAIL_S:g} s padding"}


def saved_window(entry: dict, out_dir: str | None = None) -> tuple[float, float] | None:
    """The RAW saved window of a flight entry: ``saved_t0``/``saved_t1`` (written by a previous audit) > meta.json's
    ``window`` > the entry's t0/t1.  Re-auditing is therefore idempotent — it never audits an already trimmed window."""
    if entry.get("saved_t0") is not None and entry.get("saved_t1") is not None:
        return float(entry["saved_t0"]), float(entry["saved_t1"])
    if out_dir:
        try:
            with open(os.path.join(out_dir, "meta.json")) as f:
                w = json.load(f).get("window")
            if w and len(w) >= 2:
                return float(w[0]), float(w[1])
        except Exception:
            pass
    if entry.get("t0") is not None and entry.get("t1") is not None:
        return float(entry["t0"]), float(entry["t1"])
    return None


def apply_audit(entry: dict, audit: dict) -> dict:
    """A flight entry with the audit applied: ``airborne_segments`` / ``airborne_s``, the REPLAY window t0/t1 trimmed to
    the airborne span (the raw window kept as ``saved_t0``/``saved_t1``), ``airborne_minutes`` = the airborne total (the
    saved window's length when nothing flew), the "saved window" segment left on the raw span, and ``passes`` filtered to
    the trimmed window (a dropped pass is noted in ``issues``)."""
    e = dict(entry)
    st0, st1 = float(audit["saved_t0"]), float(audit["saved_t1"])
    segs = list(audit["airborne_segments"])
    e["saved_t0"], e["saved_t1"], e["saved_t0_pdt"], e["saved_t1_pdt"] = st0, st1, iso_pdt(st0), iso_pdt(st1)
    e["t0"], e["t1"] = float(audit["t0"]), float(audit["t1"])
    e["t0_pdt"], e["t1_pdt"] = iso_pdt(e["t0"]), iso_pdt(e["t1"])
    e["airborne_segments"], e["airborne_s"] = segs, float(audit["airborne_s"])
    e["airborne_minutes"] = round(float(audit["airborne_s"]) / 60.0, 1) if segs else round((st1 - st0) / 60.0, 1)
    e["segments"] = [({**s, "t0": st0, "t1": st1, "t0_pdt": iso_pdt(st0), "t1_pdt": iso_pdt(st1)} if s.get("type") == "saved window" else s)
                     for s in (e.get("segments") or [])]
    if e.get("passes"):
        keep = [p for p in e["passes"] if not isinstance(p.get("t"), (int, float)) or e["t0"] <= float(p["t"]) <= e["t1"]]
        if len(keep) != len(e["passes"]):
            e["issues"] = list(e.get("issues") or []) + [f"{len(e['passes']) - len(keep)} pass(es) outside the airborne window "
                                                         f"({D.pdt_hms(e['t0'])}-{D.pdt_hms(e['t1'])}) dropped by the airborne audit"]
        e["passes"] = keep
    return e


def audit_line(row: dict) -> str:
    """One line of the audit report (CLI / report): saved window -> trimmed window + the airborne legs."""
    s0, s1 = row["saved"]
    t0, t1 = row["trimmed"]
    segs = row.get("segments") or []
    legs = " · ".join(f"{D.pdt_hms(s['air_t0'])}-{D.pdt_hms(s['air_t1'])} [{', '.join(s['drones'])}]" for s in segs) or "NO airborne segment (full window kept)"
    return (f"{row.get('day', '')} n={row.get('n')} {row.get('label', '')}: saved {D.pdt_hms(s0)}-{D.pdt_hms(s1)} ({(s1 - s0) / 60.0:.1f} min) "
            f"-> replay {D.pdt_hms(t0)}-{D.pdt_hms(t1)} ({(t1 - t0) / 60.0:.1f} min) · airborne {row['airborne_s']:.0f} s "
            f"({row['airborne_s'] / 60.0:.1f} min) in {len(segs)} segment{'' if len(segs) == 1 else 's'}: {legs}")


def audit_saved_flights(root: str | None = None, *, day: str | None = None, write: bool = True, backup: bool = True) -> list[dict]:
    """RE-AUDIT every saved flight under ``<root>/*/flights.json`` (root = ih.data.ARCHIVE_ROOT) from its CSVs and rewrite
    the entries in place (``flights.json.bak`` written first; atomic tmp + os.replace).  ``day`` limits it to one day
    folder, ``write=False`` is a dry run.  Returns one row per audited flight:
    {file, day, n, label, dir, saved (t0, t1), trimmed (t0, t1), airborne_s, segments, entry} — audit_line formats it.
    The caller refreshes the in-process registry (D.clear_flight_caches + D.refresh_registry) itself."""
    root = root or D.ARCHIVE_ROOT
    rows: list[dict] = []
    for fj in sorted(glob.glob(os.path.join(root, "*", "flights.json"))):
        fday = os.path.basename(os.path.dirname(fj))
        if day and fday != str(day):
            continue
        try:
            with open(fj) as f:
                j = json.load(f)
        except Exception:
            continue
        changed = False
        for r in j.get("runs", []) or []:
            fls = r.get("flights") or []
            for i, fl in enumerate(fls):
                d = fl.get("dir")
                if not d:
                    continue                              # built-in / hand-written entries (no saved CSV directory) are left alone
                path = d if os.path.isabs(d) else os.path.join(os.path.dirname(fj), d)
                w = saved_window(fl, path)
                if w is None:
                    continue
                a = audit_window(path, w[0], w[1])
                e = apply_audit(fl, a)
                fls[i] = e
                changed = True
                rows.append({"file": fj, "day": str(j.get("day") or fday), "n": fl.get("n"), "label": fl.get("label") or "", "dir": path,
                             "saved": (a["saved_t0"], a["saved_t1"]), "trimmed": (e["t0"], e["t1"]), "airborne_s": a["airborne_s"],
                             "segments": a["airborne_segments"], "entry": e})
        if changed and write:
            if backup:
                shutil.copy2(fj, fj + ".bak")
            tmp = fj + ".tmp"
            with open(tmp, "w") as f:
                json.dump(j, f, indent=1)
            os.replace(tmp, fj)
    return rows


def main(argv: list[str] | None = None) -> int:
    """``python -m ih.archive audit --root <IH_ARCHIVE_ROOT> [--day 2026-09-17] [--dry-run]`` — re-audit saved flights."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m ih.archive", description="Ironhide archive maintenance (airborne audit of saved flights).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("audit", help="recompute the airborne segments of every saved flight and rewrite flights.json (backed up first)")
    au.add_argument("--root", default=None, help=f"archive root (default $IH_ARCHIVE_ROOT / {D.ARCHIVE_ROOT})")
    au.add_argument("--day", default=None, help="only this day folder, e.g. 2026-09-17")
    au.add_argument("--dry-run", action="store_true", help="report only; do not rewrite flights.json")
    ns = ap.parse_args(argv)
    rows = audit_saved_flights(ns.root, day=ns.day, write=not ns.dry_run)
    print(f"AIRBORNE AUDIT · {len(rows)} saved flight(s) under {ns.root or D.ARCHIVE_ROOT}{' · DRY RUN' if ns.dry_run else ''}")
    for r in rows:
        print("  " + audit_line(r))
    if not rows:
        print("  (no saved flights found)")
    return 0


def flight_entry(meta: dict, out_dir: str, audit: dict | None = None) -> dict:
    """The flights.json entry for a saved window (8/28 schema fields + dir / label / source); ``n`` is added by
    ih.data.append_flight_entry.  ``audit`` = audit_window's result: the entry's REPLAY window is then the airborne span
    (apply_audit; the raw window stays as saved_t0/saved_t1 and the CSVs on disk are never cut)."""
    t0, t1 = meta["window"]
    tg, ic = meta["roles"]["target"], meta["roles"]["interceptor"]
    e = {"label": meta["label"], "dir": out_dir, "t0": t0, "t1": t1, "t0_pdt": iso_pdt(t0), "t1_pdt": iso_pdt(t1),
         "drone_ids": [f"{D.collapse_ids(tg)} (target)", f"{D.collapse_ids(ic)} (interceptor)"], "airborne_minutes": round((t1 - t0) / 60.0, 1),
         "segments": [{"type": "saved window", "n": 1, "t0": t0, "t1": t1, "t0_pdt": iso_pdt(t0), "t1_pdt": iso_pdt(t1),
                       "note": "the whole saved window (no flight segmentation)"}],
         "passes": [],
         "tracking": {"target": tg[0] if tg else None, "n_tracks": meta["tracks"], "track_rows": meta["track_rows"], "obs_rows": meta["obs_rows"],
                      "mavlink_rows": meta["mavlink_rows"], "layouts": meta["layouts"]},
         "issues": [],
         "source": {"mru": meta.get("mru"), "host": meta.get("host"), "port": meta.get("port"), "run": meta.get("run_collection"),
                    "saved_at_pdt": meta.get("saved_at_pdt")},
         "note": "saved from the live dashboard (ih.archive.save_archive); passes not computed"}
    return apply_audit(e, audit) if audit else e


def save_archive(host: str, port: int, db: str, run: str, t0: float, t1: float, *, label: str = "", mru=None, root: str | None = None,
                 out_name: str | None = None, register: bool = True, progress=None, roles: tuple | None = None, what: str = "") -> dict:
    """The whole SAVE TO ARCHIVE operation (blocking — the UI runs it through start_save): connect (long socket timeout),
    antenna + TX origin, dump_window, meta.json, flights.json entry (``register``).  ``roles`` = ih.data.roles_key() from the
    session (the worker thread has none) for the drone_ids labels.  Output: <root>/<day>/<out_name or run8_label>/.
    -> {ok, dir, day, run8, n (flight number | None), meta, entry, label}"""
    t0, t1 = float(min(t0, t1)), float(max(t0, t1))
    if t1 - t0 < 1.0:
        raise ValueError("empty time window")
    root = root or D.ARCHIVE_ROOT
    day, r8 = day_of(t0), run8(run)
    label = str(label or "").strip() or f"{D.pdt_hms(t0)[:5].replace(':', '')}-{D.pdt_hms(t1)[:5].replace(':', '')}"
    out_dir = os.path.join(root, day, out_name or f"{r8}_{safe_label(label)}")
    if progress:
        progress(0.0, f"connecting to {host}:{port}")
    cl = F._client(host, port, timeout_ms=F.TIMEOUT_MS, socket_timeout_ms=SOCKET_TIMEOUT_MS)
    try:
        col = cl[db][run]
        ant = F.antenna_origin(col)
        tx = F.tx_origin(col)
        counts = dump_window(col, t0, t1, out_dir, ant, progress=progress)
    finally:
        cl.close()
    if progress:
        progress(0.96, "airborne audit")
    roles = roles or ((), (), D.TGT_PATTERN_DEFAULT, D.ITC_PATTERN_DEFAULT)
    ids = counts["mavlink_ids"]
    now = time.time()
    meta = {"run": r8, "run_collection": run, "label": label,
            "what": what or f"{label} · {day} {D.pdt_hms(t0)}-{D.pdt_hms(t1)} PDT · saved from {host}:{port} ({run})",
            "window": [t0, t1], "window_pdt": [iso_pdt(t0), iso_pdt(t1)],
            "antenna": list(ant) if ant else None, "tx_lla": list(tx) if tx else None,
            "frame": "corr_lib.EnuFrame about the antenna (as the live view); altitude feet MSL -> m WGS-84 HAE (geoid -31.4)",
            "mru": mru, "host": host, "port": int(port), "db": db, "saved_at": now, "saved_at_pdt": iso_pdt(now),
            "roles": {"target": [i for i in ids if D.role_of(i, roles) == "target"], "interceptor": [i for i in ids if D.role_of(i, roles) == "interceptor"]},
            **{k: counts[k] for k in ("mavlink_rows", "track_rows", "tracks", "obs_rows", "layouts", "n_adsb_docs")}, "mavlink_ids": ids,
            "columns": {"mavlink": MAV_COLS, "tracks": TRK_COLS, "obs": OBS_COLS}}
    # AIRBORNE AUDIT of the truth just written (the whole window stays on disk; only the REPLAY window is trimmed)
    audit = audit_window(out_dir, t0, t1, roles_map=meta["roles"], roles=roles)
    meta["airborne"] = {k: audit[k] for k in ("airborne_segments", "airborne_s", "trimmed", "rule", "legs")}
    meta["replay_window"] = [audit["t0"], audit["t1"]]
    if progress:
        progress(0.97, "writing meta.json")
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    entry = flight_entry(meta, out_dir, audit)
    n = None
    if register:
        n = D.append_flight_entry(day, r8, entry, root=root)
        entry["n"] = n
    if progress:
        progress(1.0, "done")
    return {"ok": True, "dir": out_dir, "day": day, "run8": r8, "n": n, "meta": meta, "entry": entry, "label": label}


def preview(host: str, port: int, db: str, run: str, t0: float, t1: float) -> dict:
    """Document counts in the window (three indexed counts, 3 s timeout) for the save card's estimate.  Never raises."""
    out = {"ok": False, "err": None, "n106": 0, "n143": 0, "n103": 0}
    try:
        cl = F._client(host, port)
        col = cl[db][run]
        tq = F._time_q(min(t0, t1), max(t0, t1))
        for k, bt in (("n106", F.AIR_TRAFFIC), ("n143", F.TRACKS), ("n103", F.DWELL_WITH_OBS)):
            out[k] = int(col.count_documents({"block_type": bt, F.TIME_KEY: tq}))
        out["ok"] = True
        cl.close()
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:300]
    return out


# ── background jobs (the page polls job(); the thread never touches Streamlit) ───────────────────────
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def start_save(**kw) -> str:
    """Run save_archive(**kw) in a daemon thread; returns the job id for job()."""
    jid = uuid.uuid4().hex[:8]
    with _LOCK:
        JOBS[jid] = {"id": jid, "state": "running", "frac": 0.0, "msg": "starting", "result": None, "error": None, "t_start": time.time(),
                     "t_end": None, "params": {k: v for k, v in kw.items() if k != "progress"}}

    def prog(frac: float, msg: str) -> None:
        with _LOCK:
            JOBS[jid]["frac"], JOBS[jid]["msg"] = float(frac), str(msg)

    def run() -> None:
        try:
            res = save_archive(progress=prog, **kw)
            with _LOCK:
                JOBS[jid].update(state="done", frac=1.0, msg="done", result=res, t_end=time.time())
        except Exception as ex:
            with _LOCK:
                JOBS[jid].update(state="error", error=f"{type(ex).__name__}: {ex}"[:300], t_end=time.time())

    threading.Thread(target=run, name=f"ih-save-{jid}", daemon=True).start()
    return jid


def job(jid) -> dict | None:
    """A snapshot of one job ({id, state: running|done|error, frac, msg, result, error, t_start, t_end, params}) or None."""
    with _LOCK:
        j = JOBS.get(str(jid)) if jid else None
        return dict(j) if j else None


def running_jobs() -> list[dict]:
    with _LOCK:
        return [dict(j) for j in JOBS.values() if j["state"] == "running"]


if __name__ == "__main__":      # python -m ih.archive audit --root <IH_ARCHIVE_ROOT>
    raise SystemExit(main())
