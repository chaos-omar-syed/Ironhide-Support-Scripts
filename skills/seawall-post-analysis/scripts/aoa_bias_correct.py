#!/usr/bin/env python
"""AoA (az/el) bias estimate + correction from MAVLink truth, by MRU / run / jobs.

    python aoa_bias_correct.py --mru 43 --run 6aecec5e --jobs 100-400          # live mongo
    python aoa_bias_correct.py --mru 91 --run Turquoise_Emu --jobs 2500-2900 --target 14550
    python aoa_bias_correct.py --archive /home/omar.syed/Test_Environment/Seawall_Ironhide_Testing/Seawall_Week_of_8-24/seawall_0824_data/2026-08-26/9b22a989 \
                               --t0 "2026-08-26 08:35" --t1 "2026-08-26 08:53"    # offline CSV archive

Pipeline:
  1. resolve MRU -> mongo host (10.1<MRU>.28.205), run (hex prefix / friendly
     name / 'latest'), jobs ('100-400,512').
  2. ENGINE chaotic (default for mongo) = the OFFICIAL chaotic evaluation stack,
     nothing re-implemented:
        DataLoader(run, mongo_host).load_{tracks,obs,air_traffic}_dataframe(j0, j1)
        -> evaluate_tracks_chaotic.prepare_grading_inputs(load_all_truth=True,
           recorrelate_tracks=True, target_ids=<MAVLink ids>)
             (obs-history plurality re-correlation when obs carry truth_target,
              else the tracker's own runtime truth_match)
        -> track_eval_utils.grade_correlated_tracks(track_df, obs_df)
             (corr_meas_df: measurement-space az/el/range errors per rx link)
     ENGINE geometry (the --archive CSV dump, or --engine geometry) = the
     Seawall 8/24 corr_lib moving-truth position gate; used ONLY where chaotic
     cannot run (the archive has no contributors / obs truth_target / truth_match).
  3. fit the AoA bias ROBUSTLY on the graded samples: measurement-updated states
     only, truth moving, mono range > --rmin, coast excursions masked; az/el bias
     = median error (MAD sigma, bootstrap 95% CI, per-track consistency, az-vs-
     range slope: a true angular bias is range-flat, a lateral offset falls 1/R).
  4. apply: rotate every track state by -az_bias about the radar (and -el_bias in
     the vertical plane), re-run the errors, plot uncorrected vs corrected
     overlays + error time series + histograms.  PNG + interactive HTML + JSON.

Sign convention: az_bias = median(track_bearing - truth_bearing), deg, + = track
reads CLOCKWISE of truth. A physical array yaw error of +b (array CW of model)
makes the track read -b (Seawall 8/26: cal-geometry fit +2.26 deg CW <-> flight
bias -2.2 deg), so the yaw_offset correction has the OPPOSITE sign of the bias.
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime

import numpy as np

# =============================================================================
# Inlined from the Seawall 8/24 corr_lib.py (track_correlation) so this file is
# self-contained: mongo helpers, WGS-84 ENU frame, MAVLink truth / track loaders
# (feet-MSL -> m HAE), and the geometry-gate fallback correlation.
# =============================================================================
import warnings
from datetime import timezone
from zoneinfo import ZoneInfo
from pymongo import MongoClient

AIR_TRAFFIC, TRACKS, DWELL_WITH_OBS = 106, 143, 103

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

M_PER_FT = 0.3048

VS_FTMIN_TO_MPS = M_PER_FT / 60.0          # ft/min -> m/s (0.00508)

GEOID_N = -31.4

def mavlink_alt_hae_m(alt_ft, geoid_n=None):
    """AIR_TRAFFIC 'altitude' (FEET, MSL) -> meters, WGS-84 HAE, to share the
    track x_state frame:  alt_HAE_m = alt_ft*0.3048 + geoid_N.
    Returns None for a missing altitude (EnuFrame.enu then falls back to the
    antenna altitude, i.e. U=0)."""
    if alt_ft is None:
        return None
    return alt_ft * M_PER_FT + (GEOID_N if geoid_n is None else geoid_n)

def set_local_tz(name):
    """Switch the tz used for parsing/printing local times (e.g. a unit in
    another region). Call before parse_local()."""
    global LOCAL_TZ
    LOCAL_TZ = ZoneInfo(name)

INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET, GREEN, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7",
    "#008300", "#e34948")

TRACK_PALETTE = [AQUA, VIOLET, MAGENTA, YELLOW, GREEN, RED, ORANGE, BLUE]

def connect(host, port=27017, timeout_ms=8000, db="sensor_store"):
    return MongoClient(host, port, serverSelectionTimeoutMS=timeout_ms)[db]

def run_names(db):
    """{run_<hex> collection name: friendly run name} from the runs collection
    (best effort — schema varies across builds)."""
    out = {}
    try:
        for d in db["runs"].find({}, projection={"run_id_str": 1, "name": 1,
                                                 "run_name": 1, "run_id": 1}):
            rid = str(d.get("run_id_str") or d.get("run_id") or "").replace("-", "")
            nm = d.get("name") or d.get("run_name")
            if rid and nm:
                out[f"run_{rid}"] = str(nm)
    except Exception:
        pass
    return out

def parse_local(s):
    """'14:30' or '2026-08-25 14:30[:ss]' local time -> epoch (UTC)."""
    s = s.strip()
    now = datetime.now(LOCAL_TZ)
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"]
    for f in fmts:
        try:
            d = datetime.strptime(s, f)
            if "%Y" not in f:
                d = d.replace(year=now.year, month=now.month, day=now.day)
            return d.replace(tzinfo=LOCAL_TZ).timestamp()
        except ValueError:
            continue
    raise ValueError(f"can't parse time {s!r}")

def run_spans(db):
    """[(t_first, t_last, run_name)] for every run_* collection, via natural
    (insertion) order probes: ~0.02 s per collection where time-sorted
    find_ones take ~1 s each over a slow tailscale link. Run stores are
    append-only, so natural order ≈ time order."""
    out = []
    for name in db.list_collection_names():
        if not name.startswith("run_"):
            continue
        col = db[name]
        try:
            a = col.find_one({}, sort=[("$natural", 1)],
                             projection={"time_spec_float": 1})
            b = col.find_one({}, sort=[("$natural", -1)],
                             projection={"time_spec_float": 1})
        except Exception:
            continue
        if a and b and a.get("time_spec_float") and b.get("time_spec_float"):
            out.append((a["time_spec_float"], b["time_spec_float"], name))
    return out

def antenna_origin(col, t0=None, t1=None):
    q = {"block_type": TRACKS}
    if t0:
        q["time_spec_float"] = {"$gte": t0, "$lte": t1}
    d = col.find_one(q, projection={"payload": 1})
    if not d:
        return None
    p = d["payload"][0] if isinstance(d["payload"], list) else d["payload"]
    return (math.degrees(p["antenna_location_origin_latitude_rad"]),
            math.degrees(p["antenna_location_origin_longitude_rad"]),
            p["antenna_location_origin_altitude_m"])

WGS_A = 6378137.0                    # WGS-84 semi-major axis (m)

WGS_E2 = 6.69437999014e-3            # WGS-84 first eccentricity squared

class EnuFrame:
    """Local East / North / Up (m) about the antenna origin ``ant = (lat_deg, lon_deg, alt_m_HAE)``.

    EXACT WGS-84 geodetic -> ENU (geodetic -> ECEF, then the tangent-plane rotation at the origin); scalar or
    numpy-array inputs.  Until 2026-09-11 this was an equirectangular scale of 111 320 m/deg on BOTH axes,
    which at 33.75 N is +0.363 % in N and -0.103 % in E (14.5 m north at 4 km) against the radar's native
    NED / pymap3d -- a systematic truth-vs-track range-scale bias.  U is the tangent-plane up (it differs from
    the plain HAE difference by the earth-curvature drop d^2/2R: 1.3 m at 4 km; the radar's NED "D" is
    tangent-plane too).  ``mlat`` / ``mlon`` are the WGS-84 metres per degree at the origin (meridional /
    transverse curvature radii) for linearised estimates; the exact inverse is :meth:`lla`."""

    def __init__(self, ant):
        lat0, lon0 = float(ant[0]), float(ant[1])
        alt0 = float(ant[2]) if ant[2] is not None else 0.0
        self.ant = (lat0, lon0, alt0)
        phi, lam = math.radians(lat0), math.radians(lon0)
        sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
        w = 1.0 - WGS_E2 * sp * sp
        self.mlat = math.radians(1.0) * WGS_A * (1.0 - WGS_E2) / w ** 1.5     # meridional m/deg
        self.mlon = math.radians(1.0) * WGS_A / math.sqrt(w) * cp             # transverse m/deg
        self._o = self._ecef(phi, lam, alt0)
        self._R = np.array([[-sl, cl, 0.0],                                   # rows = east, north, up in ECEF
                            [-sp * cl, -sp * sl, cp],
                            [cp * cl, cp * sl, sp]])

    @staticmethod
    def _ecef(phi, lam, h):
        sp, cp = np.sin(phi), np.cos(phi)
        n = WGS_A / np.sqrt(1.0 - WGS_E2 * sp * sp)
        return np.stack([(n + h) * cp * np.cos(lam), (n + h) * cp * np.sin(lam), (n * (1.0 - WGS_E2) + h) * sp])

    def enu(self, lat, lon, alt):
        """(lat_deg, lon_deg, alt_m_HAE) -> (E, N, U) m.  ``alt`` None / NaN -> the antenna altitude.
        Scalars in -> floats out; arrays in -> arrays out."""
        scalar = np.ndim(lat) == 0 and np.ndim(lon) == 0
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        if alt is None:
            alt = np.full(lat.shape, self.ant[2])
        else:
            alt = np.asarray(alt, float)
            alt = np.where(np.isnan(alt), self.ant[2], alt)
        p = self._ecef(np.radians(lat), np.radians(lon), alt)
        d = p - (self._o if p.ndim == 1 else self._o[:, None])
        e, n, u = self._R @ d
        if scalar:
            return float(e), float(n), float(u)
        return e, n, u

    def lla(self, E, N, U):
        """Exact inverse of :meth:`enu`: (E, N, U) m -> (lat_deg, lon_deg, alt_m_HAE)."""
        scalar = np.ndim(E) == 0
        v = np.stack([np.asarray(E, float), np.asarray(N, float), np.asarray(U, float)])
        x, y, z = self._R.T @ v + (self._o if v.ndim == 1 else self._o[:, None])
        lam = np.arctan2(y, x)
        r = np.hypot(x, y)
        phi = np.arctan2(z, r * (1.0 - WGS_E2))
        for _ in range(6):                                                    # fixed-point on the geodetic latitude (sub-mm)
            sp = np.sin(phi)
            n = WGS_A / np.sqrt(1.0 - WGS_E2 * sp * sp)
            phi = np.arctan2(z + WGS_E2 * n * sp, r)
        sp = np.sin(phi)
        n = WGS_A / np.sqrt(1.0 - WGS_E2 * sp * sp)
        h = r / np.cos(phi) - n
        lat, lon = np.degrees(phi), np.degrees(lam)
        if scalar:
            return float(lat), float(lon), float(h)
        return lat, lon, h

def clean_truth(rows, max_step_m=200.0, max_speed_mps=40.0, min_move_m=0.01):
    """rows sorted (t,E,N,U,spd,vN,vE[,vU]): drop teleports, strip stale exact
    repeats (legacy capture_mavlink republishes the last fix at 1 Hz)."""
    if not len(rows):
        return np.zeros((0, 8))
    A = np.array(sorted(map(tuple, rows)))
    if A.shape[1] == 7:                    # legacy 7-col: pad vU with NaN
        A = np.column_stack([A, np.full(len(A), np.nan)])
    keep = np.ones(len(A), bool)
    last = 0
    for i in range(1, len(A)):
        step = math.hypot(A[i, 1] - A[last, 1], A[i, 2] - A[last, 2])
        if step > max(max_step_m, max_speed_mps * (A[i, 0] - A[last, 0])):
            keep[i] = False
        else:
            last = i
    A = A[keep]
    dpos = np.concatenate([[1.0], np.hypot(np.diff(A[:, 1]), np.diff(A[:, 2]))])
    return A[dpos >= min_move_m]

def _guard_alt_units(raw_alts, ant_alt_m, alt_units):
    """Sanity guard against a feed whose altitude units changed under us (so a
    future already-metric feed isn't silently double-converted, or a metric
    override isn't misapplied to a feet feed). Warns; never mutates. raw_alts =
    wire 'altitude' values, ant_alt_m = antenna altitude (m, HAE)."""
    a = np.asarray([x for x in raw_alts if x is not None], float)
    if len(a) < 5 or ant_alt_m is None:
        return
    med = float(np.median(a))
    if alt_units == "feet":
        # Real MSL altitude for any site is hundreds-to-thousands of feet; taken
        # as feet the HAE result should land near the antenna (a tracked UAV is
        # ~never a median 100 m BELOW its sensor). A metric feed misread as feet
        # collapses to ~30% of the true value and lands far below — a correct
        # feet read sits within a few m, so 100 m is a wide-gap discriminator.
        hae = med * M_PER_FT + GEOID_N
        if hae < ant_alt_m - 100:
            warnings.warn(
                f"AIR_TRAFFIC altitude median {med:.0f} read as FEET -> {hae:.0f} m "
                f"HAE is >100 m below the antenna ({ant_alt_m:.0f} m); the feed may "
                f"already be metric — pass alt_units='meters' if so.")
    elif alt_units == "meters":
        # A metric HAE feed sits within a few hundred m of the antenna. If the
        # median is many thousands, it is almost certainly still feet.
        if abs(med - ant_alt_m) > 3000:
            warnings.warn(
                f"AIR_TRAFFIC altitude median {med:.0f} read as METERS is >3000 m "
                f"from the antenna ({ant_alt_m:.0f} m); the feed is probably still "
                f"FEET — pass alt_units='feet' (the default).")

def load_truth(col, frame, t0, t1, target=None, max_enu_m=50_000,
               alt_units="feet", geoid_n=None):
    """-> {target_id: cleaned truth array (t,E,N,U,spd,vN,vE,vU_raw)}

    U is METERS, WGS-84 HAE, in the track frame: AIR_TRAFFIC altitude (FEET,
    MSL) is converted via mavlink_alt_hae_m() before EnuFrame differences it
    against the antenna HAE.
      alt_units : 'feet' (default — every verified MRU build reports feet MSL)
                  converts ft MSL -> m HAE; 'meters' assumes an already-metric
                  HAE feed and skips the conversion (double-convert guard for a
                  future backend change).
      geoid_n   : HAE-minus-MSL for this site (m); None -> module GEOID_N.
    vU_raw is the wire vertical_speed (FEET/MINUTE), NOT unit-normalized —
    truth_vel_at() / vu_scale() convert it to m/s downstream."""
    raw = {}
    raw_alts = []
    for d in col.find({"block_type": AIR_TRAFFIC, "time_spec_float": {"$gte": t0, "$lte": t1}},
                      projection={"time_spec_float": 1, "payload": 1}):
        pl = d.get("payload") or []
        if isinstance(pl, dict):
            pl = [pl]
        for p in pl:
            if p.get("source") != "MAVLINK" or not p.get("validposition"):
                continue
            tid = p.get("target_id") or "?"
            if target and target not in tid:
                continue
            a_ft = p.get("altitude")
            raw_alts.append(a_ft)
            alt_m = (mavlink_alt_hae_m(a_ft, geoid_n) if alt_units == "feet"
                     else a_ft)
            E, N, U = frame.enu(p["lat"], p["lon"], alt_m)
            if abs(E) > max_enu_m or abs(N) > max_enu_m:
                continue
            raw.setdefault(tid, []).append(
                (d["time_spec_float"], E, N, U, p.get("speed_mps") or 0,
                 p.get("velocity_n_mps") or 0, p.get("velocity_e_mps") or 0,
                 p.get("vertical_speed") or 0))
    _guard_alt_units(raw_alts, frame.ant[2], alt_units)
    return {k: clean_truth(v) for k, v in raw.items() if len(v) >= 5}

def vu_scale(T):
    """Multiplicative scale that turns the wire vertical_speed (col 7) into m/s.

    The AIR_TRAFFIC vertical_speed is FEET/MINUTE (the earlier 'cm/s' reading
    was a misdiagnosis: MRU91 2026.3.x raw median 69, max 1854 is a textbook
    ft/min drone climb — 0.35 / 9.4 m/s — not cm/s). So the correct factor is
    VS_FTMIN_TO_MPS = 0.3048/60. The magnitude test doubles as a units guard:
    genuine m/s data (P95 < 50 m/s; a drone never climbs faster) is passed
    through at 1.0 so an already-metric feed / npz isn't double-converted."""
    if T.shape[1] < 8 or not len(T):
        return 1.0
    v = np.abs(T[:, 7])
    v = v[np.isfinite(v) & (v > 0)]
    if len(v) < 10:
        return VS_FTMIN_TO_MPS
    return VS_FTMIN_TO_MPS if np.percentile(v, 95) > 50.0 else 1.0

def truth_vel_at(T, ts, vu=None):
    """Truth-REPORTED velocity (vE, vN, vU m/s) interpolated at times ts, plus an
    ok mask (nearest sample within 3 s). Never differentiate positions."""
    ts = np.atleast_1d(np.asarray(ts, float))
    if len(T) < 2:
        z = np.zeros(len(ts))
        return z, z, z, np.zeros(len(ts), bool)
    if vu is None:
        vu = vu_scale(T)
    idx = np.clip(np.searchsorted(T[:, 0], ts), 1, len(T) - 1)
    a, b = T[idx - 1], T[idx]
    f = np.clip((ts - a[:, 0]) / np.maximum(b[:, 0] - a[:, 0], 1e-6), 0, 1)
    ok = np.minimum(np.abs(a[:, 0] - ts), np.abs(b[:, 0] - ts)) < 3.0
    vN = a[:, 5] + f * (b[:, 5] - a[:, 5])
    vE = a[:, 6] + f * (b[:, 6] - a[:, 6])
    if T.shape[1] >= 8 and np.isfinite(T[:, 7]).any():
        vU = (a[:, 7] + f * (b[:, 7] - a[:, 7])) * vu
    else:
        vU = np.full(len(ts), np.nan)
    return vE, vN, vU, ok

def is_adsb_flagged(p):
    """Track payload positively identified with an ADS-B target by the radar.

    truth_match is unpopulated on 2026.3.x builds but checked for forward
    compat; the ADSB classifier's prediction is 'UAV' precisely when there is
    NO ADS-B match, so any other non-empty prediction from it = matched."""
    tm = p.get("truth_match")
    if tm and isinstance(tm, dict):
        blob = (str(tm.get("source", "")) + str(tm.get("target_id", ""))).lower()
        if blob and "mav" not in blob:
            return True
    c = p.get("classification") or {}
    if c.get("classifier") == "ADSB" and c.get("prediction") not in (None, "", "UAV"):
        return True
    return False

def load_adsb_truth(col, frame, t0, t1, max_range_m=12_000, alt_units="feet",
                    geoid_n=None):
    """ADS-B truth streams near the radar -> {'adsb:<id>': cleaned array}.
    Far traffic can never steal a drone-adjacent track, so it is pruned.

    ADS-B rides the same AIR_TRAFFIC 'altitude' field (feet), so it is converted
    the same way for a consistent U; but ADS-B truth feeds ONLY the positional
    (horizontal) exclusion gate (gate_metric uses cols 11/12), so its altitude
    is not load-bearing."""
    raw = {}
    for d in col.find({"block_type": AIR_TRAFFIC, "time_spec_float": {"$gte": t0, "$lte": t1}},
                      projection={"time_spec_float": 1, "payload": 1}):
        pl = d.get("payload") or []
        if isinstance(pl, dict):
            pl = [pl]
        for p in pl:
            if p.get("source") != "ADS-B" or not p.get("validposition"):
                continue
            alt_m = (mavlink_alt_hae_m(p.get("altitude"), geoid_n)
                     if alt_units == "feet" else p.get("altitude"))
            E, N, U = frame.enu(p["lat"], p["lon"], alt_m)
            if math.hypot(E, N) > max_range_m:
                continue
            raw.setdefault("adsb:" + str(p.get("target_id") or p.get("hex") or "?"),
                           []).append((d["time_spec_float"], E, N, U,
                                       p.get("speed_mps") or 0,
                                       p.get("velocity_n_mps") or 0,
                                       p.get("velocity_e_mps") or 0))
    return {k: clean_truth(v) for k, v in raw.items() if len(v) >= 5}

def load_tracks(col, t0, t1, only=None, flag_adsb=False):
    """-> {track_id: array (t,E,N,U,sigE,sigN,sigU,lu,assoc,state,vE,vN,vU)}
    Cols 0-9 keep the legacy layout (match_track/meas_times read lu at col 7);
    filter-state velocities are appended at 10-12. x_state = [N,E,D,vN,vE,vD].
    With flag_adsb=True -> (tracks, set of track_ids the radar itself marked ADS-B)."""
    out = {}
    adsb_ids = set()
    q = {"block_type": TRACKS, "time_spec_float": {"$gte": t0, "$lte": t1}}
    for d in col.find(q, projection={"time_spec_float": 1, "payload": 1}):
        pl = d.get("payload") or []
        if isinstance(pl, dict):
            pl = [pl]
        for p in pl:
            tid = p.get("track_id")
            if only and tid not in only:
                continue
            if flag_adsb and is_adsb_flagged(p):
                adsb_ids.add(tid)
            x = p.get("x_state")
            if x is None:
                continue
            P = p.get("p_cov")
            sE = sN = sU = float("nan")
            if P is not None:
                P = np.asarray(P, float).reshape(6, 6)
                sE, sN, sU = (math.sqrt(max(P[1, 1], 0)), math.sqrt(max(P[0, 0], 0)),
                              math.sqrt(max(P[2, 2], 0)))
            lu = p.get("last_update_time")
            lu = lu["full_sec"] + lu["frac_sec"] if isinstance(lu, dict) else (lu or 0.0)
            vE = x[4] if len(x) > 4 else float("nan")
            vN = x[3] if len(x) > 3 else float("nan")
            vU = -x[5] if len(x) > 5 else float("nan")
            out.setdefault(tid, []).append(
                (d["time_spec_float"], x[1], x[0], -x[2], sE, sN, sU, lu,
                 p.get("total_associations") or 0, p.get("track_state") or 0,
                 vE, vN, vU))
    tracks = {k: np.array(sorted(v)) for k, v in out.items()}
    return (tracks, adsb_ids) if flag_adsb else tracks

def interp_truth(T, ts, max_gap=3.0):
    i = np.searchsorted(T[:, 0], ts)
    if i == 0 or i >= len(T):
        return None
    a, b = T[i - 1], T[i]
    if ts - a[0] > max_gap or b[0] - ts > max_gap:
        return None
    f = (ts - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
    return a[1:] + f * (b[1:] - a[1:])

def match_track(A, T):
    """-> matched array cols: t,E,N,U,sigE,sigN,sigU,lu,tE,tN,tU,dE,dN,dU,fresh,truth_spd"""
    rows = []
    prev_lu = None
    for r in A:
        tr = interp_truth(T, r[0])
        if tr is None:
            continue
        fresh = 1.0 if (prev_lu is None or r[7] - prev_lu > 1e-6) else 0.0
        prev_lu = r[7]
        rows.append([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                     tr[0], tr[1], tr[2], r[1]-tr[0], r[2]-tr[1], r[3]-tr[2],
                     fresh, tr[3]])
    return np.array(rows) if rows else np.zeros((0, 16))

MOVING_MPS = 2.0     # truth speed above which a matched sample counts as "moving"

def gate_metric(Mm, min_moving=8, moving_mps=None):
    """Median horiz distance over MOVING-truth samples only (None if too few).

    Clutter sits still, so it can shadow a parked/hovering drone's truth but
    cannot follow a moving one — requiring the match to hold while the truth
    moves kills junk associations near the launch pad."""
    sel = Mm[Mm[:, 15] > (MOVING_MPS if moving_mps is None else moving_mps)]
    if len(sel) < min_moving:
        return None
    return float(np.median(np.hypot(sel[:, 11], sel[:, 12])))

def prep_matched(Mm, max_horiz=350.0, min_n=6):
    """Matched samples truncated at the track's FINAL measurement (drops the
    death-coast runaway) and masked of coast excursions beyond max_horiz m from
    truth. Returns None if too little remains."""
    if Mm is None or len(Mm) < min_n:
        return None
    fr = np.where(Mm[:, 14] > 0)[0]
    if not len(fr):
        return None
    m = Mm[:fr[-1] + 1]
    m = m[np.hypot(m[:, 11], m[:, 12]) < max_horiz]
    return m if len(m) >= min_n else None

def auto_correlate(tracks, truths, gate_m=150.0, min_n=8,
                   adsb_truths=None, adsb_flagged=None):
    """-> (pairs, excluded)
    pairs: (track_id, target_id, matched_array, med_horiz_moving) under gate.
    excluded: {track_id: reason} for tracks ruled out as ADS-B aircraft — either
    field-flagged by the radar, or positionally matching an ADS-B truth stream
    better than any MAVLink stream."""
    out, excluded = [], {}
    for tid, A in tracks.items():
        if len(A) < min_n:
            continue
        if adsb_flagged and tid in adsb_flagged:
            excluded[tid] = "radar-flagged ADS-B"
            continue
        best = None
        for tgt, T in truths.items():
            Mm = match_track(A, T)
            if len(Mm) < min_n:
                continue
            med = gate_metric(Mm, min_moving=min_n)
            if med is not None and (best is None or med < best[3]):
                best = (tid, tgt, Mm, med)
        if not (best and best[3] < gate_m):
            continue
        if adsb_truths:
            steal = None
            for tgt, T in adsb_truths.items():
                Mm = match_track(A, T)
                if len(Mm) < min_n:
                    continue
                med = gate_metric(Mm, min_moving=min_n)
                if med is not None and med < best[3] and (steal is None or med < steal[1]):
                    steal = (tgt, med)
            if steal:
                excluded[tid] = f"matches {steal[0]} at {steal[1]:.0f} m"
                continue
        out.append(best)
    return sorted(out, key=lambda x: x[2][0, 0] if len(x[2]) else 0), excluded

def meas_times(A):
    fresh = np.concatenate([[True], np.diff(A[:, 7]) > 1e-6])
    return A[fresh, 0]



JOB_EVENT, CPI_EVENT = 136, 135
TRUTH_COLOR, RAW_COLOR, CORR_COLOR = BLUE, ORANGE, AQUA


# --------------------------------------------------------------- resolution
def mru_host(mru, host=None):
    """MRU number -> mx-node mongo host (10.1<MRU>.28.205 on every unit seen so far)."""
    if host:
        return host
    if mru is None:
        sys.exit("give --mru N (or --host)")
    return f"10.1{int(mru):02d}.28.205"


def resolve_run(db, key):
    """'latest' | hex prefix (>=6) | full run_<hex> | friendly name -> collection name."""
    names = run_names(db)                              # run_<hex>: friendly
    cols = [n for n in db.list_collection_names() if n.startswith("run_")]
    if key in (None, "", "latest"):
        spans = sorted(run_spans(db), key=lambda r: -r[1])
        if not spans:
            sys.exit("no run_* collections on this unit")
        return spans[0][2]
    k = key.replace("-", "")
    if k.startswith("run_"):
        k = k[4:]
    hits = [n for n in cols if n[4:].startswith(k.lower())]
    if not hits:
        hits = [n for n, nm in names.items() if nm.lower() == key.lower() and n in cols]
    if len(hits) != 1:
        sys.exit(f"run {key!r} matched {len(hits)} collections: {hits[:5]}")
    return hits[0]


def parse_jobs(spec):
    """'9-12,40,100-105' -> sorted unique ints."""
    out = set()
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def job_windows(col, jobs, pad=15.0, merge_gap=120.0):
    """jobs -> merged [(t0, t1)] windows from JOB_EVENT (136) docs, using the
    block_type+job_id index (fast). Falls back to CPI events (135) and dwells
    (103) for a job with no 136 docs. Returns (windows, per_job, missing)."""
    per_job, missing = {}, []
    for j in jobs:
        ts, status = [], []
        for bt in (JOB_EVENT, CPI_EVENT, DWELL_WITH_OBS):
            for d in col.find({"block_type": bt, "job_id": j},
                              projection={"time_spec_float": 1, "payload.job_status": 1,
                                          "payload.event_error_type": 1}):
                t = d.get("time_spec_float")
                if t is None:
                    continue
                ts.append(t)
                p = d.get("payload") or {}
                if isinstance(p, dict) and p.get("job_status"):
                    status.append(p["job_status"])
            if ts:
                break
        if not ts:
            missing.append(j)
            continue
        final = status[-1] if status else "?"
        per_job[j] = (min(ts), max(ts), final)
    if not per_job:
        return [], per_job, missing
    spans = sorted((a - pad, b + pad) for a, b, _ in per_job.values())
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(w) for w in merged], per_job, missing


class IndexedCol:
    """Proxy for a run collection that rewrites corr_lib's UNINDEXED
    time_spec_float range filters into the indexed time_spec.full_sec, then
    post-filters exactly. Without this every 56 kB ADS-B AIR_TRAFFIC doc in the
    run is scanned (3 s server timeouts on busy units)."""

    def __init__(self, col):
        self._c = col

    @staticmethod
    def _rewrite(q):
        q = dict(q or {})
        rng = q.pop("time_spec_float", None)
        if isinstance(rng, dict):
            q["time_spec.full_sec"] = {k: (math.floor(v) if k.startswith("$gt") else math.ceil(v))
                                       for k, v in rng.items()}
        return q, rng

    def find(self, q=None, projection=None, **kw):
        q2, rng = self._rewrite(q)
        if projection is not None and "time_spec_float" not in projection:
            projection = dict(projection, time_spec_float=1)
        for d in self._c.find(q2, projection=projection, **kw):
            t = d.get("time_spec_float")
            if rng and t is not None:
                if "$gte" in rng and t < rng["$gte"]:
                    continue
                if "$lte" in rng and t > rng["$lte"]:
                    continue
            yield d

    def find_one(self, q=None, projection=None, **kw):
        q2, _ = self._rewrite(q)
        return self._c.find_one(q2, projection=projection, **kw)


# ------------------------------------------------------------------ loaders
def load_mongo(col, windows, target, alt_units, geoid_n, keep_adsb):
    ant = None
    for a, b in windows:
        ant = antenna_origin(col, a, b)
        if ant:
            break
    if ant is None:
        sys.exit("no TRACKS (block 143) in the job windows -- wrong run/jobs?")
    frame = EnuFrame(ant)
    truths, tracks, adsb = {}, {}, {}
    for a, b in windows:
        for k, v in load_truth(col, frame, a, b, target=target, alt_units=alt_units,
                                 geoid_n=geoid_n).items():
            truths.setdefault(k, []).append(v)
        for k, v in load_tracks(col, a, b).items():
            tracks.setdefault(k, []).append(v)
        if not keep_adsb:
            for k, v in load_adsb_truth(col, frame, a, b, alt_units=alt_units,
                                          geoid_n=geoid_n).items():
                adsb.setdefault(k, []).append(v)
    cat = lambda d: {k: np.vstack(v)[np.argsort(np.vstack(v)[:, 0])] for k, v in d.items()}
    return ant, cat(truths), cat(tracks), cat(adsb)


def load_archive(path, t0, t1, target):
    """seawall_archiver CSV run dir -> (ant, truths, tracks). Offline mock source."""
    import pandas as pd
    meta = json.load(open(os.path.join(path, "meta.json")))
    ant = tuple(meta["antenna_origin_lat_lon_haeM"])
    truths = {}
    for f in sorted(os.listdir(os.path.join(path, "mavlink"))):
        tid = f[:-4]
        if target and target not in tid:
            continue
        df = pd.read_csv(os.path.join(path, "mavlink", f))
        df = df[(df.t_epoch >= t0) & (df.t_epoch <= t1) & (df.validposition > 0)]
        if len(df) < 5:
            continue
        rows = df[["t_epoch", "E_m", "N_m", "U_m_hae", "speed_mps", "vel_n_mps",
                   "vel_e_mps", "vert_spd_wire_ftmin"]].to_numpy(float)
        T = clean_truth(rows)
        if len(T) >= 5:
            truths[tid] = T
    tracks = {}
    tdir = os.path.join(path, "tracks")
    for f in sorted(os.listdir(tdir)):
        m = re.match(r"track_(\d+)\.csv$", f)
        if not m:
            continue
        df = pd.read_csv(os.path.join(tdir, f))
        df = df[(df.t_epoch >= t0) & (df.t_epoch <= t1)]
        if len(df) < 2:
            continue
        A = df[["t_epoch", "E_m", "N_m", "U_m", "sigE_m", "sigN_m", "sigU_m", "last_update_t",
                "total_associations", "track_state", "vE_mps", "vN_mps", "vU_mps"]].to_numpy(float)
        tracks[int(m.group(1))] = A[np.argsort(A[:, 0])]
    return ant, truths, tracks


# ------------------------------------------------------- chaotic engine
def _hex32_to_uuid(h):
    h = h.replace("-", "")
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def job_ranges(jobs):
    """sorted job ids -> [(a, b)] contiguous inclusive ranges."""
    out, a, prev = [], jobs[0], jobs[0]
    for j in jobs[1:]:
        if j != prev + 1:
            out.append((a, prev))
            a = j
        prev = j
    out.append((a, prev))
    return out


def load_chaotic(host, run_col, jobs, target=None, use_tentative=False, rx_node=None):
    """Official chaotic stack -> (ref_lla, T_all, samples, info).

    samples = [(track_id, target_id, S)] with S rows
      (t, E, N, U, nan, nan, nan, t, tE, tN, tU, dE, dN, dU, fresh, truth_spd,
       az_err_deg, el_err_deg, mono_rng_err_m)      <- cols 16-18 are chaotic's
    measurement-space errors from grade_correlated_tracks (rx link `rx_node`);
    E/N/U are ENU about the rx reference for the overlay plots only."""
    import os as _os
    _os.environ.setdefault("JAX_PLATFORMS", "cpu")
    import polars as pl
    from chaotic.analysis.tools.analysis_data_loader import DataLoader
    from chaotic.analysis.tools import track_eval_utils as TE
    from chaotic.analysis.standard_performance_analysis.evaluate_tracks_chaotic import prepare_grading_inputs

    L = DataLoader(_hex32_to_uuid(run_col[4:]), mongo_host=host)
    tr, ob, tu = [], [], []
    for a, b in job_ranges(jobs):
        tr.append(L.load_tracks_dataframe(a, b))
        ob.append(L.load_obs_dataframe(a, b))
        t_valid = L.load_air_traffic_dataframe(a, b)                      # is_valid_truth_target filter
        if "source" in t_valid.columns and t_valid.filter(pl.col("source") == "MAVLINK").is_empty():
            t_all = L.load_air_traffic_dataframe(a, b, only_valid=False)  # feed w/o heading_valid etc.
            if "source" in t_all.columns and not t_all.filter(pl.col("source") == "MAVLINK").is_empty():
                print("  NOTE: MAVLink truth failed chaotic is_valid_truth_target (heading_valid/"
                      "zero-speed); loading it unfiltered")
                t_valid = t_all
        tu.append(t_valid)
    cat = lambda fr: pl.concat([f for f in fr if not f.is_empty()], how="diagonal_relaxed") if any(
        not f.is_empty() for f in fr) else fr[0]
    tracks_raw, obs_raw, truth_raw = cat(tr), cat(ob), cat(tu)
    info = {"engine": "chaotic", "tracks_raw": len(tracks_raw), "obs_raw": len(obs_raw),
            "truth_raw": len(truth_raw)}
    if "source" in truth_raw.columns:
        truth_raw = truth_raw.filter(pl.col("source") == "MAVLINK")
    else:                                                # very old feed: infer from id
        truth_raw = truth_raw.filter(pl.col("target_id").str.to_lowercase().str.contains("mav"))
    if target:
        truth_raw = truth_raw.filter(pl.col("target_id").str.contains(target))
    if truth_raw.is_empty():
        return None, None, [], dict(info, why="no MAVLink truth rows in the job range")
    raw_ids = truth_raw["target_id"].unique().to_list()
    info["mavlink_targets"] = {t: int((truth_raw["target_id"] == t).sum()) for t in raw_ids}
    target_ids = sorted({str(TE.normalize_id_for_comparison(t)) for t in raw_ids})   # build_truth_df normalizes ids
    try:
        track_df, truth_df, obs_df = prepare_grading_inputs(
            tracks_raw, truth_raw, obs_raw, load_all_truth=True, recorrelate_tracks=True,
            use_tentative=use_tentative, target_ids=target_ids)
    except ValueError as e:
        return None, None, [], dict(info, why=f"prepare_grading_inputs: {e}")
    info["correlation"] = ("obs-history plurality (recorrelate_tracks_via_obs_history)"
                           if (not obs_df.is_empty() and obs_df["truth_match_id"].is_not_null().any())
                           else "tracker runtime truth_match (no obs truth_target in range)")
    corr_df, meas = TE.grade_correlated_tracks(track_df, obs_df)
    info["graded_updates"] = len(corr_df)
    if corr_df.is_empty():
        return None, None, [], dict(info, why="no truth-correlated track updates")
    ref = TE.first_rx_ref_lla(obs_df)
    if ref is None and "antenna_location_origin_latitude_rad" in tracks_raw.columns:
        r = tracks_raw.row(0, named=True)
        ref = (math.degrees(r["antenna_location_origin_latitude_rad"]),
               math.degrees(r["antenna_location_origin_longitude_rad"]), r["antenna_location_origin_altitude_m"])
    ref = list(ref)
    # one rx link for the angle errors (MRU = single rx node)
    if not meas.is_empty():
        rxs = sorted(meas["rx_node_id"].unique().to_list())
        rx_node = rx_node if rx_node in rxs else rxs[0]
        if len(rxs) > 1:
            print(f"  NOTE: {len(rxs)} rx links {rxs}; angle errors taken from rx {rx_node} (--rx-node)")
        meas = meas.filter(pl.col("rx_node_id") == rx_node)
        info["rx_node"] = rx_node
        mkey = {(int(r["track_id"]), int(r["update_id"])): r for r in meas.iter_rows(named=True)}
        any_matched = bool(meas["obs_matched"].any())
    else:
        mkey, any_matched = {}, False
        print("  NOTE: corr_meas_df empty (no obs link geometry) -> geometric az/el from ENU")
    if not any_matched:
        print("  NOTE: no contributor->obs links on this build; every graded (UPDATED) state counts as fresh")
    rows = {}
    for r in track_df.filter(pl.col("truth_match.target_id").is_not_null()).iter_rows(named=True):
        x, tx = r.get("x_state_ecef"), r.get("truth_match.truth_state_ecef")
        if not x or not tx or len(x) < 6 or len(tx) < 6:
            continue
        te, tt = TE.ecef_state_to_enu(list(x), ref), TE.ecef_state_to_enu(list(tx), ref)
        if te is None or tt is None:
            continue
        m = mkey.get((int(r["track_id"]), int(r["update_id"])))
        fresh = (1.0 if m["obs_matched"] else 0.0) if (m is not None and any_matched) else 1.0
        spd = float(np.hypot(tt[3], tt[4]))
        t = float(r["timestamp"])
        row = [t, te[0], te[1], te[2], np.nan, np.nan, np.nan, t, tt[0], tt[1], tt[2],
               te[0] - tt[0], te[1] - tt[1], te[2] - tt[2], fresh, spd]
        if m is not None:
            row += [m["az_error_deg"], m["el_error_deg"], m["mono_rng_error_km"] * 1000.0]
        else:
            row += [np.nan, np.nan, np.nan]
        rows.setdefault((int(r["track_id"]), str(r["truth_match.target_id"]).strip()), []).append(row)
    samples = []
    for (tid, tgt), rr in sorted(rows.items(), key=lambda kv: kv[1][0][0]):
        S = np.array(sorted(rr))
        if np.isnan(S[:, 16]).all():          # no link geometry: fall back to geometric errors
            S = S[:, :16]
        samples.append((tid, tgt, S))
    used = {tgt for _, tgt, _ in samples}
    T = []
    for r in truth_df.filter(pl.col("target_id").is_in(list(used))).iter_rows(named=True):
        e = TE.ecef_state_to_enu(list(r["truth_state_ecef"]), ref) if r["truth_state_ecef"] else None
        if e is not None:
            T.append((float(r["timestamp"]), e[0], e[1], e[2], float(np.hypot(e[3], e[4])), e[4], e[3], e[5]))
    T_all = clean_truth(T) if T else np.zeros((0, 8))
    return tuple(ref), T_all, samples, info


# -------------------------------------------------------------- bias fitting
def bearing_deg(E, N):
    return np.degrees(np.arctan2(E, N))


def wrap(d):
    return (d + 180.0) % 360.0 - 180.0


def fit_samples(pairs, rmin=300.0, max_horiz=350.0, moving_mps=None):
    """-> list of (tid, tgt, S) where S = matched rows that qualify for the fit:
    fresh (assoc updated), truth moving, ground range > rmin, no coast excursion.
    pairs: (tid, tgt, M[, med]) from either engine (M cols 0-15 shared layout)."""
    mv = MOVING_MPS if moving_mps is None else moving_mps
    out = []
    for pr in pairs:
        tid, tgt, Mm = pr[0], pr[1], pr[2]
        m = prep_matched(Mm, max_horiz=max_horiz, min_n=4)
        if m is None:
            continue
        rg = np.hypot(m[:, 8], m[:, 9])
        sel = (m[:, 14] > 0) & (m[:, 15] > mv) & (rg > rmin)
        if sel.sum() >= 4:
            out.append((tid, tgt, m[sel]))
    return out


def errors(S, az_bias=0.0, el_bias=0.0):
    """Per-sample az/el/range errors (deg, deg, m) after removing the given biases.
    S with >=19 cols carries chaotic's measurement-space errors (cols 16-18, from
    grade_correlated_tracks) and those are used verbatim; otherwise (geometry
    engine) the errors are computed from the ENU states about the radar."""
    if S.shape[1] >= 19:
        return S[:, 16] - az_bias, S[:, 17] - el_bias, S[:, 18]
    E, N, U = S[:, 1], S[:, 2], S[:, 3]
    tE, tN, tU = S[:, 8], S[:, 9], S[:, 10]
    daz = wrap(bearing_deg(E, N) - bearing_deg(tE, tN)) - az_bias
    de = np.degrees(np.arctan2(U, np.hypot(E, N)) - np.arctan2(tU, np.hypot(tE, tN))) - el_bias
    dr = np.sqrt(E**2 + N**2 + U**2) - np.sqrt(tE**2 + tN**2 + tU**2)
    return daz, de, dr


def robust_stat(x, n_boot=400, seed=1):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if not len(x):
        return {"n": 0, "median": float("nan"), "mad_sigma": float("nan"), "ci95": [None, None]}
    med = float(np.median(x))
    mad = float(1.4826 * np.median(np.abs(x - med)))
    rng = np.random.default_rng(seed)
    boots = np.array([np.median(rng.choice(x, len(x))) for _ in range(n_boot)])
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"n": int(len(x)), "median": med, "mad_sigma": mad,
            "ci95": [float(lo), float(hi)], "mean": float(np.mean(x)), "std": float(np.std(x))}


def fit_bias(samples):
    """Pool every qualifying sample -> az/el/range bias with robustness diagnostics."""
    daz = np.concatenate([errors(S)[0] for _, _, S in samples])
    de = np.concatenate([errors(S)[1] for _, _, S in samples])
    dr = np.concatenate([errors(S)[2] for _, _, S in samples])
    rg = np.concatenate([np.hypot(S[:, 8], S[:, 9]) for _, _, S in samples])
    az = robust_stat(daz)
    el = robust_stat(de)
    rn = robust_stat(dr)
    per_track = {}
    for tid, tgt, S in samples:
        a, e, _ = errors(S)
        per_track[int(tid)] = {"target": tgt, "n": int(len(S)),
                               "az_med": float(np.median(a)), "el_med": float(np.median(e)),
                               "rng_med_m": float(np.median(np.hypot(S[:, 8], S[:, 9])))}
    # a true angular bias is range-independent; a fixed lateral offset falls as 1/R
    slope = float(np.polyfit(rg / 1000.0, daz, 1)[0]) if len(rg) > 10 else float("nan")
    lateral_m = float(np.median(np.radians(daz) * rg))
    return {"az_bias_deg": az, "el_bias_deg": el, "range_bias_m": rn,
            "az_vs_range_slope_deg_per_km": slope,
            "az_equiv_lateral_offset_m": lateral_m,
            "el_equiv_alt_offset_m": float(np.median(np.radians(de) * rg)),
            "per_track": per_track,
            "per_track_az_spread_deg": float(np.std([v["az_med"] for v in per_track.values()]))
            if len(per_track) > 1 else 0.0}


def apply_correction(A, az_bias, el_bias, cols=(1, 2, 3)):
    """Rotate ENU rows of an array by -az_bias about the radar (and -el_bias in the
    vertical plane), preserving slant range. Returns a copy."""
    B = A.copy()
    E, N, U = (B[:, c] for c in cols)
    rg = np.hypot(E, N)
    th = np.arctan2(E, N) - math.radians(az_bias)
    if el_bias:
        sl = np.hypot(rg, U)
        el = np.arctan2(U, rg) - math.radians(el_bias)
        rg, U = sl * np.cos(el), sl * np.sin(el)
    B[:, cols[0]], B[:, cols[1]], B[:, cols[2]] = rg * np.sin(th), rg * np.cos(th), U
    return B


# ------------------------------------------------------------------ plotting
def make_png(out_png, title, T_all, samples, fit, az_b, el_b, tz):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(2, 3, figsize=(19, 11), facecolor=SURFACE)
    fig.suptitle(title, fontsize=15, fontweight="bold", color=INK, x=0.01, ha="left")
    colors = {tid: TRACK_PALETTE[i % len(TRACK_PALETTE)] for i, (tid, _, _) in enumerate(samples)}
    pal_i = 0

    def style(a):
        a.set_facecolor(SURFACE)
        a.grid(True, color=GRID, lw=0.8)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        a.tick_params(colors=INK2)

    # maps
    for k, (lab, ab, eb) in enumerate((("UNCORRECTED (as reported)", 0.0, 0.0),
                                       (f"CORRECTED  az {-az_b:+.2f}°, el {-el_b:+.2f}° applied", az_b, el_b))):
        a = ax[0, k]
        style(a)
        for i, ch in enumerate(_segs(T_all)):
            a.plot(ch[:, 1], ch[:, 2], color=TRUTH_COLOR, lw=2.0, label="MAVLink truth" if i == 0 else None)
        for tid, tgt, S in samples:
            Sc = apply_correction(S, ab, eb)
            a.plot(Sc[:, 1], Sc[:, 2], ".", ms=4, color=colors[tid], alpha=0.9,
                   label=f"trk {tid}" if k == 0 and len(samples) <= 8 else None)
        a.set_aspect("equal")
        a.set_xlabel("East (m)")
        a.set_ylabel("North (m)")
        a.set_title(lab, loc="left", fontweight="bold")
        if k == 0:
            a.legend(loc="best", fontsize=9, frameon=False, ncol=2)
    # horiz-err histogram
    raw_h = np.concatenate([np.hypot(*(S[:, 1:3] - S[:, 8:10]).T) for _, _, S in samples])
    cor_h = np.concatenate([np.hypot(*(apply_correction(S, az_b, el_b)[:, 1:3] - S[:, 8:10]).T)
                            for _, _, S in samples])
    a = ax[0, 2]
    style(a)
    bins = np.linspace(0, max(np.percentile(raw_h, 98), 20), 40)
    a.hist(raw_h, bins, color=RAW_COLOR, alpha=0.75, label=f"raw  med {np.median(raw_h):.0f} m")
    a.hist(cor_h, bins, color=CORR_COLOR, alpha=0.75, label=f"corrected  med {np.median(cor_h):.0f} m")
    a.set_xlabel("horizontal track − truth error (m)")
    a.set_ylabel("samples")
    a.set_title("Horizontal error, fit samples", loc="left", fontweight="bold")
    a.legend(frameon=False)

    # time series az / el
    tt = np.concatenate([S[:, 0] for _, _, S in samples])
    daz_r = np.concatenate([errors(S)[0] for _, _, S in samples])
    de_r = np.concatenate([errors(S)[1] for _, _, S in samples])
    o = np.argsort(tt)
    tt, daz_r, de_r = tt[o], daz_r[o], de_r[o]
    td = [datetime.fromtimestamp(x, tz=tz) for x in tt]
    for a, raw, b, name, ci in ((ax[1, 0], daz_r, az_b, "Azimuth", fit["az_bias_deg"]["ci95"]),
                                (ax[1, 1], de_r, el_b, "Elevation", fit["el_bias_deg"]["ci95"])):
        style(a)
        a.plot(td, raw, ".", ms=3.5, color=RAW_COLOR, alpha=0.7, label="raw")
        a.plot(td, raw - b, ".", ms=3.5, color=CORR_COLOR, alpha=0.7, label="corrected")
        a.axhline(b, color=RAW_COLOR, lw=1.6)
        a.axhspan(ci[0], ci[1], color=RAW_COLOR, alpha=0.18, lw=0)
        a.axhline(0, color=INK, lw=1.0)
        lim = max(3.0, min(8.0, np.percentile(np.abs(raw), 90) * 2.0))
        a.set_ylim(-lim, lim)
        a.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
        a.set_ylabel(f"{name.lower()} error track − truth (deg)")
        a.set_title(f"{name} error: bias {b:+.2f}°  (95% CI {ci[0]:+.2f}…{ci[1]:+.2f})",
                    loc="left", fontweight="bold")
        a.legend(frameon=False, loc="upper right")
    # az error vs range (range-independence check)
    a = ax[1, 2]
    style(a)
    rg = np.concatenate([np.hypot(S[:, 8], S[:, 9]) for _, _, S in samples])
    for tid, tgt, S in samples:
        a.plot(np.hypot(S[:, 8], S[:, 9]), errors(S)[0], ".", ms=4, color=colors[tid], alpha=0.8)
    xs = np.linspace(rg.min(), rg.max(), 2)
    p = np.polyfit(rg / 1000, np.concatenate([errors(S)[0] for _, _, S in samples]), 1)
    a.plot(xs, np.polyval(p, xs / 1000), color=INK2, lw=1.5, ls="--",
           label=f"slope {fit['az_vs_range_slope_deg_per_km']:+.2f}°/km")
    a.axhline(az_b, color=RAW_COLOR, lw=1.6, label=f"median {az_b:+.2f}°")
    a.axhline(0, color=INK, lw=1.0)
    a.set_ylim(ax[1, 0].get_ylim())
    a.set_xlabel("truth ground range (m)")
    a.set_ylabel("raw azimuth error (deg)")
    a.set_title("Az error vs range (flat = true angular bias)", loc="left", fontweight="bold")
    a.legend(frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out_png, dpi=110)
    plt.close(fig)


def make_track_pages(outd, label, T_all, samples, az_b, el_b, tz, max_pages=12):
    """One PNG per track (plus an all-tracks page when there are several).
    Each page = 4 panels: top-down BEFORE / AFTER (row 1) and azimuth error vs
    time BEFORE / AFTER (row 2). Thick lines; no radar / antenna marker."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    pages = ([("all tracks", "all", samples)] if len(samples) > 1 else [])
    ranked = sorted(samples, key=lambda x: -len(x[2]))[:max(0, max_pages)]   # longest tracks first
    pages += [(f"trk {tid}", f"trk{tid}", [(tid, tgt, S)]) for tid, tgt, S in samples if any(tid == r[0] for r in ranked)]
    if len(samples) > max_pages:
        print(f"  NOTE: {len(samples)} tracks, per-track pages for the {max_pages} longest (raise --pages for more)")
    colors = {tid: TRACK_PALETTE[i % len(TRACK_PALETTE)] for i, (tid, _, _) in enumerate(samples)}
    written = []
    for name, tag, ss in pages:
        t_lo = min(S[0, 0] for _, _, S in ss) - 5
        t_hi = max(S[-1, 0] for _, _, S in ss) + 5
        Tw = T_all[(T_all[:, 0] >= t_lo) & (T_all[:, 0] <= t_hi)]
        fig, ax = plt.subplots(2, 2, figsize=(20, 13), facecolor=SURFACE)
        daz_all = np.concatenate([errors(S)[0] for _, _, S in ss])
        own = float(np.median(daz_all))
        lim = max(2.0, min(10.0, float(np.percentile(np.abs(daz_all), 98)) * 1.3))
        fig.suptitle(f"{label} — {name}   az error {own:+.2f}° (n={len(daz_all)})   pooled correction {-az_b:+.2f}° applied",
                     fontsize=20, fontweight="bold", color=INK, x=0.02, ha="left")
        for k, (ttl, ab, eb) in enumerate((("BEFORE", 0.0, 0.0), (f"AFTER ({-az_b:+.2f}°)", az_b, el_b))):
            # --- top-down
            a = ax[0, k]
            a.set_facecolor(SURFACE); a.grid(True, color=GRID, lw=1.0)
            for sp in ("top", "right"): a.spines[sp].set_visible(False)
            for i, chk in enumerate(_segs(Tw)):
                a.plot(chk[:, 1], chk[:, 2], color=TRUTH_COLOR, lw=4.0, label="drone GPS (MAVLink)" if i == 0 else None)
            miss = []
            for tid, tgt, S in ss:
                Sc = apply_correction(S, ab, eb)
                col = colors[tid] if len(ss) > 1 else (RAW_COLOR if k == 0 else CORR_COLOR)
                for chk in _segs(Sc):
                    a.plot(chk[:, 1], chk[:, 2], "-", lw=3.0, color=col, alpha=0.95)
                a.plot([], [], "-", lw=3.0, color=col, label=f"trk {tid}")
                miss.append(np.hypot(*(Sc[:, 1:3] - S[:, 8:10]).T))
            med = float(np.median(np.concatenate(miss)))
            a.set_aspect("equal"); a.set_xlabel("East (m)", fontsize=13); a.set_ylabel("North (m)", fontsize=13)
            a.set_title(f"Top-down — {ttl}   median miss {med:.0f} m", loc="left", fontsize=15, fontweight="bold")
            a.legend(loc="upper left", fontsize=12, frameon=False, ncol=2)
            # --- azimuth error vs time
            a = ax[1, k]
            a.set_facecolor(SURFACE); a.grid(True, color=GRID, lw=1.0)
            for sp in ("top", "right"): a.spines[sp].set_visible(False)
            for tid, tgt, S in ss:
                daz = errors(S)[0] - ab
                col = colors[tid] if len(ss) > 1 else (RAW_COLOR if k == 0 else CORR_COLOR)
                td = [datetime.fromtimestamp(x, tz=tz) for x in S[:, 0]]
                for i0, i1 in _seg_idx(S[:, 0]):
                    a.plot(td[i0:i1], daz[i0:i1], "-", lw=2.8, color=col, label=f"trk {tid}" if i0 == 0 else None)
            a.axhline(0, color=INK, lw=1.4)
            a.axhline(float(np.median(daz_all)) - ab, color=INK2, lw=1.6, ls="--",
                      label=f"median {float(np.median(daz_all)) - ab:+.2f}°")
            a.set_ylim(-lim, lim)                      # same scale before/after so the shift is visible
            a.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=tz))
            a.set_ylabel("azimuth error, track − truth (°)", fontsize=13)
            a.set_title(f"Azimuth error vs time — {ttl}", loc="left", fontsize=15, fontweight="bold")
            a.legend(loc="upper right", fontsize=12, frameon=False, ncol=3)
        fig.tight_layout(rect=(0, 0, 1, 0.955))
        path = os.path.join(outd, f"aoa_bias_page_{tag}.png")
        fig.savefig(path, dpi=110); plt.close(fig); written.append(path)
    return written


def _seg_idx(t, gap=6.0):
    s = 0
    for i in range(1, len(t)):
        if t[i] - t[i - 1] > gap:
            yield s, i
            s = i
    yield s, len(t)


def _segs(T, gap=6.0):
    s = 0
    for i in range(1, len(T)):
        if T[i, 0] - T[i - 1, 0] > gap:
            yield T[s:i]
            s = i
    yield T[s:]


def make_html(out_html, title, T_all, samples, fit, az_b, el_b, tz, summary):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    colors = {tid: TRACK_PALETTE[i % len(TRACK_PALETTE)] for i, (tid, _, _) in enumerate(samples)}
    fig = make_subplots(rows=2, cols=2, subplot_titles=(
        "UNCORRECTED (as reported)", f"CORRECTED (az {-az_b:+.2f}°, el {-el_b:+.2f}° applied)",
        f"Azimuth error — bias {az_b:+.2f}°", f"Elevation error — bias {el_b:+.2f}°"),
        vertical_spacing=0.12, horizontal_spacing=0.08)
    for k, (ab, eb) in enumerate(((0.0, 0.0), (az_b, el_b))):
        col = k + 1
        first = True
        for ch in _segs(T_all):
            fig.add_trace(go.Scatter(x=ch[:, 1], y=ch[:, 2], mode="lines", line=dict(color=TRUTH_COLOR, width=2),
                                     name="MAVLink truth", legendgroup="truth", showlegend=(k == 0 and first)),
                          row=1, col=col)
            first = False
        for tid, tgt, S in samples:
            Sc = apply_correction(S, ab, eb)
            fig.add_trace(go.Scatter(x=Sc[:, 1], y=Sc[:, 2], mode="markers",
                                     marker=dict(size=5, color=colors[tid]), name=f"trk {tid}",
                                     legendgroup=f"t{tid}", showlegend=(k == 0),
                                     text=[datetime.fromtimestamp(t, tz=tz).strftime("%H:%M:%S") for t in S[:, 0]],
                                     hovertemplate="%{text}<br>E %{x:.0f} N %{y:.0f}<extra>trk " + str(tid) + "</extra>"),
                          row=1, col=col)
    tt = np.concatenate([S[:, 0] for _, _, S in samples])
    daz = np.concatenate([errors(S)[0] for _, _, S in samples])
    de = np.concatenate([errors(S)[1] for _, _, S in samples])
    o = np.argsort(tt)
    td = [datetime.fromtimestamp(t, tz=tz) for t in tt[o]]
    for col, (raw, b) in enumerate(((daz[o], az_b), (de[o], el_b)), start=1):
        fig.add_trace(go.Scatter(x=td, y=raw, mode="markers", marker=dict(size=4, color=RAW_COLOR),
                                 name="raw error", legendgroup="raw", showlegend=(col == 1)), row=2, col=col)
        fig.add_trace(go.Scatter(x=td, y=raw - b, mode="markers", marker=dict(size=4, color=CORR_COLOR),
                                 name="corrected error", legendgroup="cor", showlegend=(col == 1)), row=2, col=col)
        fig.add_hline(y=b, line=dict(color=RAW_COLOR, width=1.5), row=2, col=col)
        fig.add_hline(y=0, line=dict(color=INK, width=1), row=2, col=col)
    fig.update_yaxes(scaleanchor="x", scaleratio=1, row=1, col=1)
    fig.update_yaxes(scaleanchor="x2", scaleratio=1, row=1, col=2)
    fig.update_xaxes(title_text="East (m)", row=1)
    fig.update_yaxes(title_text="North (m)", row=1)
    fig.update_yaxes(title_text="deg (track − truth)", row=2)
    fig.update_layout(template="plotly_white", height=980, title=dict(text=title, x=0.01),
                      legend=dict(orientation="h", y=-0.06), font=dict(color=INK, size=13))
    pre = json.dumps(summary, indent=1)
    html = ("<!doctype html><html><head><meta charset='utf-8'><title>AoA bias</title>"
            "<style>body{font-family:Segoe UI,Roboto,Arial,sans-serif;margin:16px;color:#0b0b0b}"
            "pre{background:#f4f4f2;padding:12px;border-radius:6px;font-size:12px;overflow:auto}</style></head><body>"
            + fig.to_html(full_html=False, include_plotlyjs="cdn")
            + "<h3>Summary</h3><pre>" + pre + "</pre></body></html>")
    open(out_html, "w").write(html)


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("source (mongo)")
    src.add_argument("--mru", type=int, help="MRU number -> mongo host 10.1<MRU>.28.205")
    src.add_argument("--host", help="explicit mongo host (overrides --mru)")
    src.add_argument("--port", type=int, default=27017)
    src.add_argument("--db", default="sensor_store")
    src.add_argument("--run", default="latest", help="run: hex prefix, run_<hex>, friendly name, or 'latest'")
    src.add_argument("--jobs", help="job ids, e.g. '100-400,512' -> time windows via block 136")
    src.add_argument("--pad", type=float, default=15.0, help="seconds of padding around each job window")
    src.add_argument("--list-runs", action="store_true")
    off = ap.add_argument_group("source (offline archive)")
    off.add_argument("--archive", help="seawall_archiver run dir (mavlink/*.csv, tracks/*.csv, meta.json)")
    off.add_argument("--t0"), off.add_argument("--t1")
    off.add_argument("--tz", default="America/Los_Angeles")
    sel = ap.add_argument_group("correlation")
    sel.add_argument("--engine", default="auto", choices=["auto", "chaotic", "geometry"],
                     help="auto = chaotic official stack for mongo, corr_lib geometry for --archive")
    sel.add_argument("--use-tentative", action="store_true",
                     help="chaotic: include TENTATIVE/CREATED track states (default CONFIRMED+UPDATED)")
    sel.add_argument("--rx-node", type=int, default=None, help="chaotic: rx node id for the angle errors")
    sel.add_argument("--target", help="MAVLink target_id substring (e.g. 14550)")
    sel.add_argument("--tracks", help="manual track ids (comma list) instead of auto-correlation")
    sel.add_argument("--gate", type=float, default=150.0, help="auto-correlate gate, m (median horiz while truth moving)")
    sel.add_argument("--min-dur", type=float, default=10.0, help="drop correlated fragments shorter than this, s")
    sel.add_argument("--keep-adsb", action="store_true", help="skip ADS-B positional exclusion")
    fitg = ap.add_argument_group("bias fit")
    fitg.add_argument("--rmin", type=float, default=300.0, help="min truth ground range for fit samples, m")
    fitg.add_argument("--min-speed", type=float, default=2.0, help="truth speed to count as moving, m/s")
    fitg.add_argument("--max-horiz", type=float, default=350.0, help="coast-excursion mask, m")
    fitg.add_argument("--no-el", action="store_true", help="fit/correct azimuth only")
    fitg.add_argument("--alt-units", default="feet", choices=["feet", "meters"])
    fitg.add_argument("--geoid-n", type=float, default=None)
    ap.add_argument("--pages", type=int, default=12,
                    help="max per-track 4-panel pages (longest tracks first; 0 = all-tracks page only)")
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", default=None, help="output dir (default aoa_bias_<label>)")
    args = ap.parse_args()

    global MOVING_MPS
    set_local_tz(args.tz)
    MOVING_MPS = args.min_speed
    tz = LOCAL_TZ
    samples_all, info, engine = [], {}, "geometry: corr_lib moving-truth gate (FALLBACK — no chaotic correlation available)"
    fmt = lambda t: datetime.fromtimestamp(t, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    only = set(int(x) for x in args.tracks.split(",")) if args.tracks else None
    adsb = {}
    per_job, missing, windows = {}, [], []

    if args.archive:
        if not (args.t0 and args.t1):
            ap.error("--archive needs --t0/--t1 (the CSV archive carries no job ids)")
        t0, t1 = parse_local(args.t0), parse_local(args.t1)
        windows = [(t0, t1)]
        ant, truths, tracks = load_archive(args.archive, t0, t1, args.target)
        if args.engine == "chaotic":
            sys.exit("the CSV archive has no contributors/obs truth_target/truth_match -> chaotic engine impossible")
        source = f"archive {os.path.abspath(args.archive)}"
        label = args.label or os.path.basename(os.path.normpath(args.archive))
    else:
        host = mru_host(args.mru, args.host)
        try:
            db = connect(host, args.port, db=args.db)
            db.list_collection_names()
        except Exception as e:
            sys.exit(f"can't reach mongo at {host}:{args.port} -- {e!r}")
        if args.list_runs:
            names = run_names(db)
            for a, b, n in sorted(run_spans(db), key=lambda r: -r[1])[:40]:
                print(f"{n}  {names.get(n, ''):18s} {fmt(a)} -> {fmt(b)}")
            return
        if not args.jobs:
            ap.error("--jobs is required with a mongo source (or use --archive + --t0/--t1)")
        run = resolve_run(db, args.run)
        friendly = run_names(db).get(run, "")
        col = IndexedCol(db[run])
        jobs = parse_jobs(args.jobs)
        windows, per_job, missing = job_windows(db[run], jobs, pad=args.pad)
        if not windows:
            sys.exit(f"none of jobs {args.jobs} have JOB_EVENT/CPI/dwell docs in {run}")
        print(f"MRU{args.mru or '?'} {host}  {run} ({friendly})")
        print(f"jobs requested {len(jobs)}, found {len(per_job)}, missing {len(missing)}"
              + (f" e.g. {missing[:8]}" if missing else ""))
        fails = [j for j, (_, _, s) in per_job.items() if s not in ("JOB_COMPLETE_SP", "JOB_COMPLETE_FRONTEND")]
        if fails:
            print(f"  {len(fails)} job(s) not COMPLETE (kept; e.g. {fails[:6]})")
        for a, b in windows:
            print(f"  window {fmt(a)} -> {fmt(b)}  ({b - a:.0f} s)")
        source = f"mongo {host} {run}"
        label = args.label or f"MRU{args.mru or host}_{run[4:12]}_jobs{jobs[0]}-{jobs[-1]}"
        if args.engine in ("auto", "chaotic"):
            ant, T_all, samples_all, info = load_chaotic(host, run, jobs, target=args.target,
                                                         use_tentative=args.use_tentative, rx_node=args.rx_node)
            print(f"chaotic stack: {info}")
            if not samples_all:
                if args.engine == "chaotic":
                    sys.exit(f"chaotic engine found nothing: {info.get('why')}")
                print(f"  -> falling back to corr_lib geometry gate ({info.get('why')})")
            else:
                engine = f"chaotic: {info['correlation']}"
        if args.engine == "geometry" or not samples_all:
            ant, truths, tracks, adsb = load_mongo(col, windows, args.target, args.alt_units,
                                                   args.geoid_n, args.keep_adsb)

    print(f"antenna / rx origin {ant[0]:.6f}, {ant[1]:.6f}, {ant[2]:.1f} m HAE")
    print(f"correlation engine: {engine}")
    if samples_all:
        pairs = [(tid, tgt, S, float(np.median(np.hypot(S[:, 11], S[:, 12])))) for tid, tgt, S in samples_all]
        if only:
            pairs = [p for p in pairs if p[0] in only]
        pairs = [p for p in pairs if p[2][-1, 0] - p[2][0, 0] >= args.min_dur]
    else:
        print(f"truth streams: {{{', '.join(f'{k}: {len(v)}' for k, v in truths.items())}}}")
        print(f"tracks in window: {len(tracks)}")
        if not truths:
            sys.exit("no moving MAVLink truth in the window(s)")

    if samples_all:
        pass
    elif only:
        pairs = []
        for tid in sorted(only):
            if tid not in tracks:
                print(f"  WARNING track {tid} not in window")
                continue
            best = None
            for tgt, T in truths.items():
                Mm = match_track(tracks[tid], T)
                if len(Mm) >= 4:
                    med = float(np.median(np.hypot(Mm[:, 11], Mm[:, 12])))
                    if best is None or med < best[3]:
                        best = (tid, tgt, Mm, med)
            if best:
                pairs.append(best)
    else:
        pairs, excluded = auto_correlate(tracks, truths, gate_m=args.gate, adsb_truths=adsb or None)
        for tid, why in excluded.items():
            print(f"  excluded track {tid}: {why}")
        pairs = [p for p in pairs if p[2][-1, 0] - p[2][0, 0] >= args.min_dur]
    if not pairs:
        sys.exit("nothing correlated (try --gate, --tracks, --target, --use-tentative)")
    for tid, tgt, Mm, med in pairs:
        print(f"  track {tid:>5} <-> {tgt}: n={len(Mm):4d}  med horiz {med:5.0f} m  "
              f"{fmt(Mm[0, 0])[11:]} - {fmt(Mm[-1, 0])[11:]}")

    samples = fit_samples(pairs, rmin=args.rmin, max_horiz=args.max_horiz)
    if not samples:
        sys.exit("no fit-quality samples (fresh + moving + beyond --rmin); lower --rmin?")
    fit = fit_bias(samples)
    az_b = fit["az_bias_deg"]["median"]
    trk_u_std = float(np.std(np.concatenate([S[:, 3] for _, _, S in samples])))
    fit["track_alt_pinned"] = trk_u_std < 1.0
    if fit["track_alt_pinned"]:
        print(f"  WARNING track altitude std {trk_u_std:.2f} m -> tracker pins altitude on this build; "
              "el bias is NOT an AoA measurement and is not applied")
    el_b = 0.0 if (args.no_el or fit["track_alt_pinned"]) else fit["el_bias_deg"]["median"]
    n_fit = sum(len(S) for _, _, S in samples)

    raw_h = np.concatenate([np.hypot(*(S[:, 1:3] - S[:, 8:10]).T) for _, _, S in samples])
    cor_h = np.concatenate([np.hypot(*(apply_correction(S, az_b, el_b)[:, 1:3] - S[:, 8:10]).T)
                            for _, _, S in samples])
    raw_v = np.concatenate([S[:, 3] - S[:, 10] for _, _, S in samples])
    cor_v = np.concatenate([apply_correction(S, az_b, el_b)[:, 3] - S[:, 10] for _, _, S in samples])

    summary = {
        "source": source, "label": label, "correlation_engine": engine, "chaotic_info": info, "generated": datetime.now(tz=tz).isoformat(timespec="seconds"),
        "antenna_origin_lat_lon_haeM": list(ant),
        "windows": [[fmt(a), fmt(b)] for a, b in windows],
        "jobs": {str(j): {"t0": fmt(a), "t1": fmt(b), "final_status": s} for j, (a, b, s) in per_job.items()},
        "jobs_missing": missing,
        "correlated_pairs": [{"track": int(t), "target": g, "n": int(len(M)), "med_horiz_m": round(m, 1)}
                             for t, g, M, m in pairs],
        "fit_samples": n_fit, "fit_tracks": len(samples),
        "fit": fit,
        "applied": {"az_rotation_deg": -az_b, "el_rotation_deg": -el_b},
        "horiz_err_m": {"raw_median": float(np.median(raw_h)), "corrected_median": float(np.median(cor_h)),
                        "raw_p90": float(np.percentile(raw_h, 90)), "corrected_p90": float(np.percentile(cor_h, 90))},
        "alt_err_m": {"raw_median": float(np.median(raw_v)), "corrected_median": float(np.median(cor_v))},
        "yaw_offset_correction_deg": -az_b,
        "notes": ["az_bias = median(track_bearing - truth_bearing); + = track reads clockwise of truth",
                  "yaw_offset_correction_deg = -az_bias (array physically CW of model makes tracks read CCW/low); "
                  "verify the sign against the unit's yaw_offset_rad convention before editing config",
                  "el bias and a truth-altitude datum error are indistinguishable from one target; "
                  "el_equiv_alt_offset_m gives the same number in meters"],
    }

    outd = args.out or f"aoa_bias_{label}"
    os.makedirs(outd, exist_ok=True)
    title = (f"AoA bias — {label}   az {az_b:+.2f}° (σ {fit['az_bias_deg']['mad_sigma']:.2f}°, n={n_fit}, "
             f"{len(samples)} tracks)   el {fit['el_bias_deg']['median']:+.2f}°\n"
             f"correlation: {engine}")
    if not samples_all:
        T_all = np.vstack([truths[g] for g in sorted({g for _, g, _ in samples})])
        T_all = T_all[np.argsort(T_all[:, 0])]
    make_png(os.path.join(outd, "aoa_bias.png"), title, T_all, samples, fit, az_b, el_b, tz)
    pages = make_track_pages(outd, label, T_all, samples, az_b, el_b, tz, max_pages=args.pages)
    make_html(os.path.join(outd, "aoa_bias.html"), title, T_all, samples, fit, az_b, el_b, tz, summary)
    json.dump(summary, open(os.path.join(outd, "summary.json"), "w"), indent=1, default=float)

    a, e = fit["az_bias_deg"], fit["el_bias_deg"]
    print("\n=== AoA bias fit ===")
    print(f"  samples: {n_fit} fresh/moving/>{args.rmin:.0f} m on {len(samples)} track(s)")
    print(f"  AZ bias  {a['median']:+.2f}°   95% CI [{a['ci95'][0]:+.2f}, {a['ci95'][1]:+.2f}]   "
          f"MAD-σ {a['mad_sigma']:.2f}°   per-track spread {fit['per_track_az_spread_deg']:.2f}°   "
          f"slope vs range {fit['az_vs_range_slope_deg_per_km']:+.2f}°/km")
    print(f"  EL bias  {e['median']:+.2f}°   95% CI [{e['ci95'][0]:+.2f}, {e['ci95'][1]:+.2f}]   "
          f"MAD-σ {e['mad_sigma']:.2f}°   (≡ {fit['el_equiv_alt_offset_m']:+.0f} m altitude)"
          + ("   [NOT applied, --no-el]" if args.no_el else
             "   [NOT applied: track altitude pinned]" if fit["track_alt_pinned"] else ""))
    print(f"  range bias {fit['range_bias_m']['median']:+.1f} m")
    print(f"  horiz err median  raw {np.median(raw_h):.0f} m  ->  corrected {np.median(cor_h):.0f} m"
          f"   (p90 {np.percentile(raw_h, 90):.0f} -> {np.percentile(cor_h, 90):.0f})")
    print(f"  suggested yaw_offset correction: {-az_b:+.2f}° (sign: see notes in summary.json)")
    print(f"\nwrote {outd}/aoa_bias.png, aoa_bias.html, summary.json + {len(pages)} page(s): "
          + ", ".join(os.path.basename(x) for x in pages))


if __name__ == "__main__":
    main()
