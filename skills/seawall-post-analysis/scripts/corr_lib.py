"""Shared library: MAVLink pseudo-truth vs VP track correlation on MRU mongo.

Bakes in the hard-won lessons from the 2026-08-25 MRU91 two-drone session:
  * AIR_TRAFFIC (106) docs bundle MULTIPLE targets per payload -> ALWAYS iterate
    the full payload list (positional $ projection silently drops targets).
  * Legacy capture_mavlink republishes the last GPS fix at 1 Hz with fresh
    timestamps when the sender goes quiet -> strip exact-position repeats.
  * Truth altitude datum can be biased by hundreds of meters -> el error is
    computed against per-track median-dU-corrected truth altitude.
  * Track 'last_update_time' unchanged between docs = coast; diff it for the
    true measurement (association) rate. Publish rate is a constant ~2 Hz.
"""
import math
import warnings
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
from pymongo import MongoClient

AIR_TRAFFIC, TRACKS, DWELL_WITH_OBS = 106, 143, 103
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# --- MAVLink AIR_TRAFFIC (block 106) unit conversions -----------------------
# The AIR_TRAFFIC payload reports 'altitude' in FEET, MSL (orthometric) and
# 'vertical_speed' in FEET/MINUTE — verified against chaotic production code +
# the MRU43 schema + a DEM. The track x_state (block 143) is METERS, WGS-84 HAE
# (ellipsoidal), so truth must be converted feet->meters AND MSL->HAE to share
# the track frame. (The horizontal velocity fields speed_mps / velocity_n_mps /
# velocity_e_mps are ALREADY m/s and are left untouched; only 'speed' is knots.)
M_PER_FT = 0.3048
VS_FTMIN_TO_MPS = M_PER_FT / 60.0          # ft/min -> m/s (0.00508)
# geoid_N = HAE - MSL, site-specific. -31.4 m is the WGS84-minus-MSL undulation
# for the current MRU91/MRU43 SoCal site (SoCal runs ~ -31 to -33 m). Override
# per site via the loader's geoid_n arg; a proper geoid model (pygeodesy /
# EGM2008) should replace this constant for other locations.
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

# palette (dataviz reference)
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


def find_runs(db, t0, t1):
    """run_* collections overlapping [t0, t1], most-overlapping first."""
    out = []
    for a, b, name in run_spans(db):
        ov = min(b, t1) - max(a, t0)
        if ov > 0:
            out.append((ov, name))
    return [n for _, n in sorted(out, reverse=True)]


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


def track_vu_usable(tracks):
    """Some builds publish a nonsense 6th state element (|vU| reads 100-200
    'm/s' on MRU91 2026.3.x). Vertical velocity error is only meaningful when
    the published rate is physically plausible."""
    v = np.concatenate([A[:, 12] for A in tracks.values()
                        if A.shape[1] > 12]) if tracks else np.array([])
    v = v[np.isfinite(v)]
    return (bool(len(v)) and float(np.median(np.abs(v))) < 30.0
            and float(np.std(v)) > 0.01)     # all-zeros = velocities absent


def load_obs(col, t0, t1):
    """Raw DWELL_WITH_OBS detections -> array (t, az_rad, el_rad, rng_m,
    dop_ms, snr_db). This is every detection the radar formed, pre-tracker."""
    rows = []
    proj = {"time_spec_float": 1, "payload.observations.az_rad": 1,
            "payload.observations.el_rad": 1, "payload.observations.amb_rng_km": 1,
            "payload.observations.amb_dop_ms": 1, "payload.observations.snr_db": 1}
    for d in col.find({"block_type": DWELL_WITH_OBS,
                       "time_spec_float": {"$gte": t0, "$lte": t1}}, projection=proj):
        for o in (d.get("payload", {}).get("observations") or []):
            if o.get("az_rad") is None or o.get("amb_rng_km") is None:
                continue
            rows.append((d["time_spec_float"], o["az_rad"], o.get("el_rad") or 0.0,
                         o["amb_rng_km"] * 1000.0, o.get("amb_dop_ms") or 0.0,
                         o.get("snr_db") or 0.0))
    return np.array(sorted(rows)) if rows else np.zeros((0, 6))


def match_obs(O, T, du=0.0, az_gate_rad=0.06, rng_gate_m=300.0):
    """Obs geometrically on the truth target -> (t, daz_deg, del_deg, snr, dop).
    RAW errors; du = truth altitude datum offset, median(track_U - truth_U),
    ADDED to truth U (a defect of the truth reference, not the radar)."""
    rows = []
    for t, az, el, rng, dop, snr in O:
        tr = interp_truth(T, t)
        if tr is None:
            continue
        taz = math.atan2(tr[0], tr[1])
        tgr = math.hypot(tr[0], tr[1])
        daz = (az - taz + math.pi) % (2 * math.pi) - math.pi
        if abs(daz) > az_gate_rad or abs(rng - tgr) > rng_gate_m:
            continue
        tel = math.atan2(tr[2] + du, tgr)
        rows.append((t, math.degrees(daz), math.degrees(el - tel), snr, dop))
    return np.array(rows) if rows else np.zeros((0, 5))


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


def clamp_agl(T, agl_min):
    """Keep truth only above agl_min m AGL (kills takeoff/landing junk matches).
    AGL reference = parked truth altitude: median U of low-speed samples, or the
    3rd-percentile U when the recording has no parked phase. -> (T, pad_U)."""
    if agl_min <= 0 or not len(T):
        return T, None
    parked = T[T[:, 4] < 1.0]
    pad_u = (float(np.median(parked[:, 3])) if len(parked) >= 5
             else float(np.percentile(T[:, 3], 3)))
    return T[(T[:, 3] - pad_u) > agl_min], pad_u


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


def seeker_samples(m, A, du=0.0, min_speed=4.0):
    """Truth position relative to the TRACK state, in the track-velocity frame:
    -> array (t, cross_m, vert_m, along_m). The seeker sits ahead of the track
    along the track's velocity; these feed off-boresight angles per standoff.
    A = full track array with filter velocities at cols 10-11 (load_tracks)."""
    m2 = m[m[:, 15] > min_speed]
    if len(m2) < 4 or A.shape[1] < 12:
        return np.zeros((0, 4))
    idx = np.clip(np.searchsorted(A[:, 0], m2[:, 0]), 0, len(A) - 1)
    tvE, tvN = A[idx, 10], A[idx, 11]          # TRACK state velocity
    fin = np.isfinite(tvE) & np.isfinite(tvN)
    m2, tvE, tvN = m2[fin], tvE[fin], tvN[fin]
    if len(m2) < 4 or float(np.median(np.hypot(tvE, tvN))) < 1.0:
        return np.zeros((0, 4))    # absent/zero velocities: frame undefined
    vmag = np.maximum(np.hypot(tvE, tvN), 0.5)
    uE, uN = tvE / vmag, tvN / vmag
    eE, eN, eU = -m2[:, 11], -m2[:, 12], -(m2[:, 13] - du)   # truth - track
    cross = eE * (-uN) + eN * uE
    along = eE * uE + eN * uN
    return np.column_stack([m2[:, 0], cross, eU, along])


def seeker_angles(S, standoff_m, min_denom=20.0):
    """Off-boresight angles (az_deg, el_deg) to TRUTH for a seeker at
    standoff_m ahead of the track state, boresight on the track state."""
    denom = standoff_m - S[:, 3]
    ok = denom > min_denom
    az = np.degrees(np.arctan2(S[ok, 1], denom[ok]))
    el = np.degrees(np.arctan2(S[ok, 2], denom[ok]))
    return az, el, S[ok, 0]


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


def az_bias_deg(matched_list):
    d = []
    for Mm in matched_list:
        d.append((np.degrees(np.arctan2(Mm[:, 1], Mm[:, 2]) -
                             np.arctan2(Mm[:, 8], Mm[:, 9])) + 180) % 360 - 180)
    return float(np.median(np.concatenate(d))) if d else 0.0


def angle_errors(Mm, az_bias=0.0):
    """-> dict of az/el errors (deg, az bias-corrected) + 1-sigma angular sigmas."""
    r_g = np.hypot(Mm[:, 1], Mm[:, 2])
    theta = np.arctan2(Mm[:, 1], Mm[:, 2])
    az_err = ((np.degrees(theta - np.arctan2(Mm[:, 8], Mm[:, 9])) + 180) % 360
              - 180) - az_bias
    sig_az = np.degrees(np.sqrt((np.cos(theta)*Mm[:, 4])**2 +
                                (np.sin(theta)*Mm[:, 5])**2) / np.maximum(r_g, 1))
    dU_med = float(np.median(Mm[:, 13])) if len(Mm) >= 10 else 0.0
    tU = Mm[:, 10] + dU_med
    el_err = np.degrees(np.arctan2(Mm[:, 3], r_g) -
                        np.arctan2(tU, np.hypot(Mm[:, 8], Mm[:, 9])))
    sig_el = np.degrees(Mm[:, 6] / np.maximum(np.hypot(r_g, Mm[:, 3]), 1))
    return {"t": Mm[:, 0], "az_err": az_err, "el_err": el_err,
            "sig_az": sig_az, "sig_el": sig_el, "dU_datum": dU_med}


# Fixed, logical axis scales shared by the report tabs and the live dashboard so
# the Az/El/Range/Alt panels read the same everywhere. (range, dtick)
ERR_SCALE = {"az": (3.0, 1.0), "el": (3.0, 1.0), "rng": (40.0, 5.0), "alt": (60.0, 10.0)}


def err_stack(Mm, az_bias=0.0):
    """RAW az/el (deg) + range/altitude (m) errors + filter 1-sigma, with NO datum
    removal (unlike angle_errors, which subtracts its own median dU). Truth altitude
    is the rigorous WGS-84 value, so these are the real radar errors.
    Cols per match_track: 1=E 2=N 3=U 4=sE 5=sN 6=sU 8=tE 9=tN 10=tU."""
    Eg, Ng, Ug = Mm[:, 1], Mm[:, 2], Mm[:, 3]
    r_g = np.hypot(Eg, Ng)
    theta = np.arctan2(Eg, Ng)
    az_err = ((np.degrees(theta - np.arctan2(Mm[:, 8], Mm[:, 9])) + 180) % 360
              - 180) - az_bias
    sig_az = np.degrees(np.sqrt((np.cos(theta) * Mm[:, 4])**2 +
                                (np.sin(theta) * Mm[:, 5])**2) / np.maximum(r_g, 1))
    tru_gr = np.hypot(Mm[:, 8], Mm[:, 9])
    el_err = np.degrees(np.arctan2(Ug, r_g) - np.arctan2(Mm[:, 10], tru_gr))
    sig_el = np.degrees(Mm[:, 6] / np.maximum(np.hypot(r_g, Ug), 1))
    trk_slant = np.hypot(r_g, Ug)
    tru_slant = np.hypot(tru_gr, Mm[:, 10])
    rng_err = trk_slant - tru_slant
    alt_err = Ug - Mm[:, 10]
    sig_rng = np.sqrt((Eg * Mm[:, 4])**2 + (Ng * Mm[:, 5])**2 +
                      (Ug * Mm[:, 6])**2) / np.maximum(trk_slant, 1)
    sig_alt = Mm[:, 6]
    return {"t": Mm[:, 0], "az_err": az_err, "el_err": el_err,
            "rng_err": rng_err, "alt_err": alt_err,
            "sig_az": sig_az, "sig_el": sig_el, "sig_rng": sig_rng, "sig_alt": sig_alt}


def meas_times(A):
    fresh = np.concatenate([[True], np.diff(A[:, 7]) > 1e-6])
    return A[fresh, 0]


def stats(Mm):
    h = np.hypot(Mm[:, 11], Mm[:, 12])
    return {"n": int(len(Mm)), "horiz_med_m": round(float(np.median(h)), 1),
            "horiz_p95_m": round(float(np.percentile(h, 95)), 1),
            "dU_datum_bias_m": round(float(np.median(Mm[:, 13])), 1)}


def get_plotlyjs():
    import plotly.offline as pyo
    return pyo.get_plotlyjs()
