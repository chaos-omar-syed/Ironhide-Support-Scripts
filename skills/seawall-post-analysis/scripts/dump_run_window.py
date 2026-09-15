#!/usr/bin/env python
"""dump_run_window.py — dump a time window (or a job range) of an MRU radar run from its mongo into the "quickdump"
archive layout every offline Seawall post-analysis tool reads.  SELF-CONTAINED (pymongo + numpy; no streamlit / ih /
corr_lib) — replaces track_correlation/quickdump.py, whose ih.archive import dragged in the dashboard package.  Same
layout and columns as ih.archive.save_archive (its columns first, new ones appended).

    micromamba run -n sensorenv python dump_run_window.py --mru 43 --run 6aecec5e --jobs 21627-23147 --label flight1
    micromamba run -n sensorenv python dump_run_window.py --mru 43 --run Lavender_Bison --t0 "2026-09-03 15:25" --t1 "2026-09-03 15:39"
        --label flight1 [--root ./archive] [--tz America/Los_Angeles] [--geoid-n -31.4] [--antenna lat,lon,hae] [--no-obs] [--no-adsb]
        [--chunk-s 600] [--overwrite]       ·       dump_run_window.py --mru 43 --list-runs       ·       dump_run_window.py --selftest

OUTPUT  <root>/<YYYY-MM-DD local>/<run8>_<label>/   (exact column lists: MAV_COLS / TRK_COLS / OBS_COLS / JOB_COLS below + meta.json "columns")
  mavlink/<target_id>.csv  block 106, source == MAVLINK, ids exactly as the radar publishes them: t_epoch,time_local,lat,lon,alt_ft_wire,
                           E_m,N_m,U_m_hae,speed_mps,vel_n_mps,vel_e_mps,vert_spd_wire_ftmin,validposition,source
  adsb/<target_id>.csv     same columns for every non-MAVLINK source (kept out of mavlink/ so old readers that glob it still see drones only)
  tracks/track_<tid>.csv   block 143 ENU state + sigmas, total_associations, track_state, last_update_t, truth_match_id/conf/source,
                           contributors (JSON), sigvE/N/U_mps, track_type
  obs/obs.csv              block 103 detections: job/dwell/node/obs ids, az_rad, el_rad, rng_m, rr_mps, bi_rng_m, bi_rng_rate_mps, snr_db, truth_target
  jobs/jobs.csv            block 136: job_id,t_first,t_last,final_status,error_type   (skipped when the run has none)
  meta.json                run (collection), run_id8, friendly_name, mru, host, window_epoch/local, tz, antenna_origin_lat_lon_haeM
                           (+ "antenna" duplicate for old readers), tx_lla, geoid_n_m, counts, "143_layout", columns, saved_at, tool_version

UNITS (seawall_0824_data/README.md): alt_ft_wire = WIRE value, FEET MSL; vert_spd_wire_ftmin FEET/MINUTE; speed/vel already
m/s.  E/N/U = metres ENU about the run's antenna origin, WGS-84 HAE frame: U_m_hae = enu(lat, lon, ft*0.3048 + geoid_n),
geoid_n = HAE - MSL (-31.4 m SoCal site).  Obs rng_m = amb_rng_km*1000, rr_mps = -amb_dop_ms (monostatic, OPENING positive).
BLOCK-143 LAYOUTS, per payload:  "ned_xstate" (2026.3.x) x_state=[N,E,D,vN,vE,vD] about antenna_location_origin_* + p_cov
-> E=x[1], N=x[0], U=-x[2], sigmas sqrt(P[1,1]) sqrt(P[0,0]) sqrt(P[2,2]) (vel 4,3,5);  "ecef" (2026.9.x) only x_state_ecef +
p_cov_ecef + latitude_rad/longitude_rad/altitude_m -> exact WGS-84 ECEF -> ENU about the antenna origin, covariance R P R^T.
Antenna origin: payload antenna_location_origin_*, else the obs(103) sensor_nodes rx-node centroid (ECEF -> LLA), else
--antenna.  A payload carrying BOTH layouts is written from NED and the ECEF path is cross-checked (meta layout_crosscheck_*).
MONGO RULES: MongoClient(host)["sensor_store"][run].  NEVER range-filter time_spec_float (unindexed: full scan of 56 kB
ADS-B docs) — every query filters the INDEXED time_spec.full_sec in --chunk-s slices with a long socket timeout and the
exact float bound is re-applied in Python; a job range becomes a window through block-136 docs by job_id (indexed);
block-106 payloads are a dict OR a list (every entry iterated, never a positional $ projection).  CSVs are written tmp ->
rename, the dump is built in <dir>.partial and swapped in; an existing dump is left alone unless --overwrite.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
from pymongo import MongoClient

TOOL, TOOL_VERSION = "dump_run_window.py", "1.0"
AIR_TRAFFIC, TRACKS, DWELL_WITH_OBS, JOB_EVENT = 106, 143, 103, 136
TIME_KEY = "time_spec.full_sec"                       # indexed (block_type, full_sec, frac_sec)
GEOID_N_DEFAULT, M_PER_FT = -31.4, 0.3048
SOCKET_TIMEOUT_MS, SELECT_TIMEOUT_MS = 300_000, 10_000
MAV_COLS = ["t_epoch", "time_local", "lat", "lon", "alt_ft_wire", "E_m", "N_m", "U_m_hae", "speed_mps", "vel_n_mps", "vel_e_mps",
            "vert_spd_wire_ftmin", "validposition", "source"]
TRK_COLS = ["t_epoch", "time_local", "E_m", "N_m", "U_m", "vE_mps", "vN_mps", "vU_mps", "sigE_m", "sigN_m", "sigU_m", "total_associations",
            "track_state", "last_update_t", "truth_match_id", "truth_match_conf", "truth_match_source", "contributors",
            "sigvE_mps", "sigvN_mps", "sigvU_mps", "track_type"]
OBS_COLS = ["t_epoch", "time_local", "job_id", "dwell_id", "node_id", "obs_id", "az_rad", "el_rad", "rng_m", "rr_mps", "bi_rng_m",
            "bi_rng_rate_mps", "snr_db", "truth_target_id", "truth_match_type"]
JOB_COLS = ["job_id", "t_first", "t_last", "final_status", "error_type"]


def _proj(*fields, prefix="payload."):
    return {"time_spec_float": 1, "time_spec": 1, **{(f if f.startswith("@") else prefix + f).lstrip("@"): 1 for f in fields}}


TRUTH_PROJ = _proj("source", "target_id", "lat", "lon", "altitude", "speed_mps", "velocity_n_mps", "velocity_e_mps", "vertical_speed", "validposition")
TRACK_PROJ = _proj("track_id", "x_state", "p_cov", "x_state_ecef", "p_cov_ecef", "latitude_rad", "longitude_rad", "altitude_m", "track_velocity_n",
                   "track_velocity_e", "last_update_time", "total_associations", "track_state", "track_type", "antenna_location_origin_latitude_rad",
                   "antenna_location_origin_longitude_rad", "antenna_location_origin_altitude_m", "truth_match.target_id", "truth_match.match_type",
                   "truth_match.source", "contributors")
OBS_PROJ = _proj("@job_id", "@node_id", "job_id", "dwell_id", "sensor_nodes.node_id", *("observations." + k for k in (
    "obs_id", "node_id", "az_rad", "el_rad", "amb_rng_km", "amb_dop_ms", "amb_bistatic_rng_km", "amb_bistatic_rng_rate_ms", "snr_db",
    "truth_target.target_id", "truth_target.match_type")))
JOB_PROJ = _proj("@job_id", "job_id", "job_status", "event_error_type")
TX_KEYS = (("tx_antenna_location_latitude_rad", "tx_antenna_location_longitude_rad", "tx_antenna_location_altitude_m"),
           ("transmitter_location_latitude_rad", "transmitter_location_longitude_rad", "transmitter_location_altitude_m"))


# ── exact WGS-84 geodesy (corr_lib.EnuFrame, extended with ECEF inputs) ──────────────────────────────────────────────
WGS_A, WGS_E2 = 6378137.0, 6.69437999014e-3


def lla_to_ecef(lat_rad, lon_rad, h_m):
    sp, cp = np.sin(lat_rad), np.cos(lat_rad)
    n = WGS_A / np.sqrt(1.0 - WGS_E2 * sp * sp)
    return np.stack([(n + h_m) * cp * np.cos(lon_rad), (n + h_m) * cp * np.sin(lon_rad), (n * (1.0 - WGS_E2) + h_m) * sp])


def ecef_to_lla_deg(x, y, z):
    """ECEF (m) -> (lat deg, lon deg, HAE m); fixed-point on the geodetic latitude (sub-mm)."""
    lam, r = math.atan2(y, x), math.hypot(x, y)
    phi = math.atan2(z, r * (1.0 - WGS_E2))
    for _ in range(8):
        sp = math.sin(phi)
        phi = math.atan2(z + WGS_E2 * WGS_A / math.sqrt(1.0 - WGS_E2 * sp * sp) * sp, r)
    sp = math.sin(phi)
    return math.degrees(phi), math.degrees(lam), r / math.cos(phi) - WGS_A / math.sqrt(1.0 - WGS_E2 * sp * sp)


class EnuFrame:
    """Local East/North/Up (m) about ant = (lat deg, lon deg, HAE m): geodetic -> ECEF -> tangent-plane rotation.
    R rows = east, north, up unit vectors in ECEF (ENU = R (ecef - origin); velocities / covariances rotate with R)."""

    def __init__(self, ant):
        self.ant = (float(ant[0]), float(ant[1]), float(ant[2]) if ant[2] is not None else 0.0)
        phi, lam = math.radians(self.ant[0]), math.radians(self.ant[1])
        sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
        self.o = lla_to_ecef(phi, lam, self.ant[2])
        self.R = np.array([[-sl, cl, 0.0], [-sp * cl, -sp * sl, cp], [cp * cl, cp * sl, sp]])

    def enu(self, lat_deg, lon_deg, alt_m):
        alt = self.ant[2] if alt_m is None or not math.isfinite(float(alt_m)) else float(alt_m)
        return self.enu_ecef(lla_to_ecef(math.radians(float(lat_deg)), math.radians(float(lon_deg)), alt))

    def enu_ecef(self, xyz):
        e, n, u = self.R @ (np.asarray(xyz, float) - self.o)
        return float(e), float(n), float(u)


# ── small helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────
def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _f(v, nd=2):
    """CSV cell: '' for None / NaN, fixed decimals for floats, str otherwise."""
    if v is None:
        return ""
    if isinstance(v, (float, np.floating)):
        return "" if not math.isfinite(float(v)) else f"{float(v):.{nd}f}"
    return str(v)


def _doc_t(d):
    t = d.get("time_spec_float")
    if t is None:
        ts = d.get("time_spec") or {}
        if "full_sec" not in ts:
            return None
        t = float(ts["full_sec"]) + float(ts.get("frac_sec", 0.0))
    return float(t)


def _payloads(d):
    pl = (d or {}).get("payload") or []
    return [p for p in ([pl] if isinstance(pl, dict) else pl) if isinstance(p, dict)]


def _lu(v):
    if isinstance(v, dict):
        return float(v.get("full_sec", 0)) + float(v.get("frac_sec", 0.0))
    return float(v) if v is not None else float("nan")


def _opt(v):
    try:
        return float("nan") if v is None else float(v)
    except (TypeError, ValueError):
        return float("nan")


def safe_label(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or "").strip()).strip("_.") or "x"


def run8(run):
    r = str(run or "")
    return (r[4:] if r.startswith("run_") else r)[:8] or "run"


def match_conf(match_type):
    """'plurality_based, confidence_0.87' -> 0.87 (NaN when absent)."""
    m = re.search(r"confidence[_ =:]*([0-9]*\.?[0-9]+)", str(match_type or ""))
    return float(m.group(1)) if m else float("nan")


def parse_truth_match(p):
    """payload.truth_match -> (id | None, conf, source | None) — source inferred from the id when the radar omits it."""
    tm = p.get("truth_match")
    if not isinstance(tm, dict):
        return None, float("nan"), None
    mid = tm.get("target_id")
    mid = None if mid in (None, "") else str(mid).strip()
    src = tm.get("source") or (("MAVLINK" if re.search(r"mav", mid, re.I) else "ADS-B") if mid else None)
    return mid, match_conf(tm.get("match_type")), src


def parse_local(s, tz):
    """'2026-09-03 15:25[:ss]' / '2026-09-03T15:25' / '15:25' (today) local time, or a float epoch -> epoch seconds."""
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    now = datetime.now(tz)
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%H:%M:%S", "%H:%M"):
        try:
            d = datetime.strptime(s, f)
        except ValueError:
            continue
        if "%Y" not in f:
            d = d.replace(year=now.year, month=now.month, day=now.day)
        return d.replace(tzinfo=tz).timestamp()
    raise ValueError(f"cannot parse time {s!r}")


# ── row parsers ───────────────────────────────────────────────────────────────────────────────────────────────────────
def truth_rows(t, doc, frame, geoid_n):
    """Block-106 doc -> [(source, target_id, MAV_COLS row)] for EVERY entry; E/N/U only for finite, non-(0,0) lat/lon; U = ft MSL -> m HAE."""
    out = []
    for p in _payloads(doc):
        tid, src = str(p.get("target_id") or "?").strip(), str(p.get("source") or "?").strip()
        lat, lon, alt = _opt(p.get("lat")), _opt(p.get("lon")), _opt(p.get("altitude"))
        E = N = U = None
        if frame is not None and math.isfinite(lat) and math.isfinite(lon) and not (lat == 0.0 and lon == 0.0):
            E, N, U = frame.enu(lat, lon, alt * M_PER_FT + geoid_n if math.isfinite(alt) else None)
        out.append((src, tid, [f"{t:.3f}", None, _f(lat, 7), _f(lon, 7), "" if not math.isfinite(alt) else repr(alt), _f(E), _f(N), _f(U),
                              p.get("speed_mps") or 0, p.get("velocity_n_mps") or 0, p.get("velocity_e_mps") or 0, p.get("vertical_speed") or 0,
                              1 if p.get("validposition") else 0, src]))
    return out


def _origin_rad(p):
    try:
        return (float(p["antenna_location_origin_latitude_rad"]), float(p["antenna_location_origin_longitude_rad"]),
                float(p["antenna_location_origin_altitude_m"]))
    except (KeyError, TypeError, ValueError):
        return None


def _sig3(P):
    return tuple(math.sqrt(max(float(P[i, i]), 0.0)) for i in range(3))


def new_stats():
    return {"layouts": {"ned_xstate": 0, "ecef": 0}, "no_origin": 0, "xcheck_max_m": 0.0, "xcheck_n": 0, "tracks_out_of_range": 0}


def track_rows(t, doc, frames, fallback_frame, stats):
    """Block-143 doc -> [(tid, row in TRK_COLS order)].  Layout per payload: NED x_state (primary) or ECEF/LLA.
    ``frames`` caches an EnuFrame per payload origin; ``fallback_frame`` serves payloads without one.  stats (new_stats()):
    layout counts, no-origin skips, max |NED - ECEF| for payloads carrying both."""
    out = []
    for p in _payloads(doc):
        tid = p.get("track_id")
        if tid is None:
            continue
        o, fr = _origin_rad(p), fallback_frame
        if o is not None:
            fr = frames.get(o) or frames.setdefault(o, EnuFrame((math.degrees(o[0]), math.degrees(o[1]), o[2])))
        x, xe = p.get("x_state"), p.get("x_state_ecef")
        sE = sN = sU = svE = svN = svU = float("nan")
        try:
            if x is not None and len(x) >= 3:                                              # layout A: NED state
                x = [float(v) for v in x]
                E, N, U = x[1], x[0], -x[2]
                vN, vE, vU = (x[3], x[4], -x[5]) if len(x) >= 6 else (float("nan"),) * 3
                if p.get("p_cov") is not None:
                    P = np.asarray(p["p_cov"], float).reshape(6, 6)
                    sE, sN, sU = math.sqrt(max(P[1, 1], 0)), math.sqrt(max(P[0, 0], 0)), math.sqrt(max(P[2, 2], 0))
                    svE, svN, svU = math.sqrt(max(P[4, 4], 0)), math.sqrt(max(P[3, 3], 0)), math.sqrt(max(P[5, 5], 0))
                stats["layouts"]["ned_xstate"] += 1
                if xe is not None and len(xe) >= 3 and fr is not None:                     # both layouts: cross-check
                    e2, n2, u2 = fr.enu_ecef(xe[:3])
                    stats["xcheck_max_m"] = max(stats["xcheck_max_m"], abs(e2 - E), abs(n2 - N), abs(u2 - U))
                    stats["xcheck_n"] += 1
            elif xe is not None or p.get("latitude_rad") is not None:                       # layout B: ECEF + LLA
                if fr is None:
                    stats["no_origin"] += 1
                    continue
                if xe is not None and len(xe) >= 3:
                    E, N, U = fr.enu_ecef(xe[:3])
                else:
                    E, N, U = fr.enu(math.degrees(float(p["latitude_rad"])), math.degrees(float(p["longitude_rad"])), _opt(p.get("altitude_m")))
                if xe is not None and len(xe) >= 6:
                    vE, vN, vU = (float(v) for v in fr.R @ np.asarray(xe[3:6], float))
                else:
                    vE, vN, vU = _opt(p.get("track_velocity_e")), _opt(p.get("track_velocity_n")), float("nan")
                if p.get("p_cov_ecef") is not None:
                    Pe = np.asarray(p["p_cov_ecef"], float).reshape(6, 6)
                    sE, sN, sU = _sig3(fr.R @ Pe[:3, :3] @ fr.R.T)
                    svE, svN, svU = _sig3(fr.R @ Pe[3:6, 3:6] @ fr.R.T)
                stats["layouts"]["ecef"] += 1
            else:
                continue
        except (TypeError, ValueError):
            continue
        mid, conf, src = parse_truth_match(p)
        contrib = json.dumps(p.get("contributors") if p.get("contributors") is not None else [], default=str)
        out.append((int(tid), [f"{t:.3f}", None, _f(E), _f(N), _f(U), _f(vE, 3), _f(vN, 3), _f(vU, 3), _f(sE), _f(sN), _f(sU),
                               int(p.get("total_associations") or 0), int(p.get("track_state") or 0), _f(_lu(p.get("last_update_time")), 3),
                               mid or "", _f(conf), src or "", contrib, _f(svE, 3), _f(svN, 3), _f(svU, 3),
                               "" if p.get("track_type") is None else p.get("track_type")]))
    return out


def obs_rows(t, doc):
    """Block-103 doc -> [row in OBS_COLS order]; rr = -amb_dop_ms (opening positive); node from the obs, else sensor_nodes, else header."""
    out = []
    for p in _payloads(doc):
        nodes = [n.get("node_id") for n in (p.get("sensor_nodes") or []) if isinstance(n, dict)]
        job, dwell = p.get("job_id", doc.get("job_id")), p.get("dwell_id")
        for i, o in enumerate(p.get("observations") or []):
            if not isinstance(o, dict) or o.get("az_rad") is None or o.get("amb_rng_km") is None:
                continue
            node, dop = o.get("node_id", nodes[0] if nodes else doc.get("node_id")), o.get("amb_dop_ms")
            tt = o.get("truth_target") if isinstance(o.get("truth_target"), dict) else {}
            out.append([f"{t:.3f}", None, "" if job is None else job, "" if dwell is None else dwell, "" if node is None else node,
                        o.get("obs_id", i), f"{float(o['az_rad']):.6f}", f"{float(o.get('el_rad') or 0.0):.6f}", _f(float(o["amb_rng_km"]) * 1000.0, 1),
                        _f(float("nan") if dop is None else -float(dop), 3), _f(_opt(o.get("amb_bistatic_rng_km")) * 1000.0, 1),
                        _f(_opt(o.get("amb_bistatic_rng_rate_ms")), 3), _f(_opt(o.get("snr_db"))),
                        str(tt.get("target_id") or "").strip(), str(tt.get("match_type") or "")])
    return out


def _job_event(acc, t, d):
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    j = p.get("job_id", d.get("job_id"))
    if j is None:
        return
    v = acc.setdefault(int(j), {"t0": t, "t1": t, "status": "", "t_status": -1.0, "err": None})
    v["t0"], v["t1"] = min(v["t0"], t), max(v["t1"], t)
    if t >= v["t_status"]:
        v["status"], v["t_status"] = str(p.get("job_status") or ""), t
    if p.get("event_error_type") and not v["err"]:
        v["err"] = str(p["event_error_type"])


# ── mongo access ──────────────────────────────────────────────────────────────────────────────────────────────────────
def run_index(db):
    """{run_<hex>: {friendly, start_t, version}} from the ``runs`` collection (best effort; schema varies)."""
    out = {}
    try:
        for d in db["runs"].find({}, projection={"run_id_str": 1, "name": 1, "run_name": 1, "date_time": 1, "version": 1}):
            rid = str(d.get("run_id_str") or "").replace("-", "").lower()
            if not re.fullmatch(r"[0-9a-f]{8,}", rid):
                continue
            try:
                start = datetime.strptime(str(d.get("date_time")), "%Y-%m-%d_%H_%M_%S").replace(tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                start = None
            out[f"run_{rid}"] = {"friendly": str(d.get("name") or d.get("run_name") or ""), "start_t": start, "version": str(d.get("version") or "")}
    except Exception as ex:                                                              # noqa: BLE001 — listing is best effort
        log(f"  (runs collection unreadable: {type(ex).__name__}: {ex})")
    return out


def resolve_run(db, spec):
    """--run hex prefix | run_<hex> | friendly name | 'latest' -> collection name (ValueError when none / ambiguous)."""
    names = [n for n in db.list_collection_names() if n.startswith("run_")]
    idx = run_index(db)
    s = str(spec or "latest").strip()
    if s.lower() == "latest":
        dated = sorted((v["start_t"], n) for n, v in idx.items() if n in names and v["start_t"])
        if dated:
            return dated[-1][1]
        if not names:
            raise ValueError("no run_* collections")
        newest = [(_doc_t(db[n].find_one({}, sort=[("$natural", -1)], projection=_proj()) or {}) or 0.0, n) for n in names]
        return max(newest)[1]
    key = s.lower().replace("-", "")
    key = key if key.startswith("run_") else f"run_{key}"
    cands = ([n for n in names if n.lower().startswith(key)]
             or [n for n in names if idx.get(n, {}).get("friendly", "").lower() == s.lower()]
             or [n for n in names if s.lower() in idx.get(n, {}).get("friendly", "").lower()])
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ValueError(f"no run matches {spec!r} (try --list-runs)")
    raise ValueError(f"ambiguous run {spec!r}: " + ", ".join(f"{c} ({idx.get(c, {}).get('friendly', '')})" for c in cands[:8]))


def list_runs(db, tz, limit):
    idx = run_index(db)
    names = set(n for n in db.list_collection_names() if n.startswith("run_"))
    rows = sorted(((v["start_t"] or 0.0), n, v) for n, v in idx.items())[::-1]
    print(f"{len(names)} run_* collections, {len(idx)} runs docs; newest {limit} (counts = indexed 143 / 106 totals):")
    print(f"{'run8':<9}{'friendly':<22}{'start ' + tz.key:<28}{'version':<18}{'n143':>8}{'n106':>8}  collection")
    for st, n, v in rows[:limit]:
        n143 = db[n].count_documents({"block_type": TRACKS}) if n in names else "-"
        n106 = db[n].count_documents({"block_type": AIR_TRAFFIC}) if n in names else "-"
        start = datetime.fromtimestamp(st, tz).strftime("%Y-%m-%d %H:%M:%S") if st else "?"
        print(f"{run8(n):<9}{v['friendly'][:21]:<22}{start:<28}{v['version'][:17]:<18}{n143!s:>8}{n106!s:>8}  {n}{'' if n in names else '  (no collection)'}")
    orphans = sorted(names - set(idx))
    if orphans:
        print(f"{len(orphans)} collections without a runs doc: " + ", ".join(run8(n) for n in orphans[:20]) + (" ..." if len(orphans) > 20 else ""))


def _tq(t0, t1):
    return {TIME_KEY: {"$gte": int(math.floor(t0)), "$lte": int(math.ceil(t1))}}


def window(col, bt, a, b, proj, hi_inclusive, extra=None):
    """Docs of one block type with a <= t < b (or <= b for the last chunk) through the INDEXED full_sec; yields (t, doc)."""
    for d in col.find({"block_type": int(bt), **_tq(a, b), **(extra or {})}, projection=proj):
        t = _doc_t(d)
        if t is not None and a <= t <= b and (t < b or hi_inclusive):
            yield t, d


def jobs_to_window(col, j0, j1):
    """Job range -> (t0, t1, block_type used, n docs) from block-136 JOB_EVENT docs by job_id (indexed), 103 headers as fallback."""
    for bt in (JOB_EVENT, DWELL_WITH_OBS):
        ts = [_doc_t(d) for d in col.find({"block_type": bt, "job_id": {"$gte": int(j0), "$lte": int(j1)}}, projection=_proj())]
        ts = [t for t in ts if t is not None]
        if ts:
            return min(ts), max(ts), bt, len(ts)
    raise ValueError(f"no block-136 / 103 documents for jobs {j0}-{j1}")


def node_centroid(col, t0, t1, name_sub):
    """(lat, lon, HAE) of the first obs(103) sensor_nodes entry whose node_name contains ``name_sub`` (centroid_loc_ecef -> LLA)."""
    d = col.find_one({"block_type": DWELL_WITH_OBS, **_tq(t0, t1), "payload.sensor_nodes.0": {"$exists": True}}, projection={"payload.sensor_nodes": 1})
    for p in _payloads(d):
        for n in p.get("sensor_nodes") or []:
            if name_sub in str(n.get("node_name", "")).lower():
                for g in n.get("antenna_groups") or []:
                    c = g.get("centroid_loc_ecef") or {}
                    if all(k in c for k in "xyz"):
                        return ecef_to_lla_deg(float(c["x"]), float(c["y"]), float(c["z"])), f"obs(103) sensor_nodes {n.get('node_name')} centroid"
    return None, None


def discover_origin(col, t0, t1, cli_ant):
    """(lat deg, lon deg, HAE m), source — 143 payload origin (window, then any), else 103 rx-node centroid, else --antenna."""
    for q in ({"block_type": TRACKS, **_tq(t0, t1)}, {"block_type": TRACKS}):
        for p in _payloads(col.find_one(q, projection=_proj("antenna_location_origin_latitude_rad", "antenna_location_origin_longitude_rad",
                                                               "antenna_location_origin_altitude_m"))):
            o = _origin_rad(p)
            if o:
                return (math.degrees(o[0]), math.degrees(o[1]), o[2]), "tracks(143) antenna_location_origin"
    rx, src = node_centroid(col, t0, t1, "rx")
    if rx:
        return rx, src
    return (tuple(cli_ant), "--antenna") if cli_ant else (None, "none")


def discover_tx(col, t0, t1):
    """Transmitter (lat, lon, HAE) when published apart from the RX: 103 sensor_nodes 'tx' centroid, explicit 143 TX keys, or 2026.9.x
    contributors[].tx_lla_ddm (> 5 m from rx_lla_ddm).  None = co-located / unknown (bistatic = 2 x mono)."""
    tx, src = node_centroid(col, t0, t1, "tx")
    if tx:
        return tx, src
    for p in _payloads(col.find_one({"block_type": TRACKS, **_tq(t0, t1)}, projection={"payload": 1})):
        for la, lo, al in TX_KEYS:
            if la in p:
                return (math.degrees(float(p[la])), math.degrees(float(p[lo])), float(p[al])), f"tracks(143) {la}"
        for c in p.get("contributors") or []:
            tx, rx = (c or {}).get("tx_lla_ddm"), (c or {}).get("rx_lla_ddm")
            if tx and len(tx) >= 3:
                tx = (float(tx[0]), float(tx[1]), float(tx[2]))
                if not rx or math.hypot((tx[0] - rx[0]) * 111_320, (tx[1] - rx[1]) * 111_320 * math.cos(math.radians(rx[0]))) > 5 or abs(tx[2] - rx[2]) > 5:
                    return tx, "tracks(143) contributors tx_lla_ddm"
    return None, "none (co-located TX/RX or not published)"


# ── the dump ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def write_csv(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


def dump(col, t0, t1, out_dir, ant, tz, *, geoid_n, chunk_s, with_obs, with_adsb, max_range_m, jobs=None):
    """Fetch [t0, t1] in indexed full_sec chunks, write mavlink/ adsb/ tracks/ obs/ jobs/ under out_dir. -> counts dict (+ 'stats')."""
    frame = EnuFrame(ant) if ant else None
    frames, truth, trk, obs, jobs_ev, stats = {}, {}, {}, [], {}, new_stats()
    edges = np.arange(float(t0), float(t1), float(chunk_s)).tolist() + [float(t1)]
    n, tw = len(edges) - 1, time.time()
    local = lambda t: datetime.fromtimestamp(float(t), tz).isoformat()                     # noqa: E731
    for i in range(n):
        a, b, last = edges[i], edges[i + 1], i == n - 1
        c = {AIR_TRAFFIC: 0, TRACKS: 0, DWELL_WITH_OBS: 0}
        for t, d in window(col, AIR_TRAFFIC, a, b, TRUTH_PROJ, last):
            c[AIR_TRAFFIC] += 1
            for src, tid, r in truth_rows(t, d, frame, geoid_n):
                r[1] = local(t)
                truth.setdefault((src, tid), {})[round(t, 3)] = r
        for t, d in window(col, TRACKS, a, b, TRACK_PROJ, last):
            c[TRACKS] += 1
            for tid, r in track_rows(t, d, frames, frame, stats):
                if max_range_m and r[2] and r[3] and math.hypot(float(r[2]), float(r[3])) > max_range_m:
                    stats["tracks_out_of_range"] += 1
                    continue
                r[1] = local(t)
                trk.setdefault(tid, {})[round(t, 3)] = r
        if with_obs:
            for t, d in window(col, DWELL_WITH_OBS, a, b, OBS_PROJ, last):
                c[DWELL_WITH_OBS] += 1
                for r in obs_rows(t, d):
                    r[1] = local(t)
                    obs.append(r)
        if jobs is None:                                                                    # time mode: job events by time
            for t, d in window(col, JOB_EVENT, a, b, JOB_PROJ, last):
                _job_event(jobs_ev, t, d)
        log(f"  chunk {i + 1}/{n}  {local(a)[11:19]}-{local(b)[11:19]}  106:{c[AIR_TRAFFIC]} 143:{c[TRACKS]} 103:{c[DWELL_WITH_OBS]}  [{time.time() - tw:.0f} s]")
    if jobs is not None:                                                                    # job mode: by job_id (indexed)
        for d in col.find({"block_type": JOB_EVENT, "job_id": {"$gte": int(jobs[0]), "$lte": int(jobs[1])}}, projection=JOB_PROJ):
            if (t := _doc_t(d)) is not None:
                _job_event(jobs_ev, t, d)

    for sub in ("mavlink", "tracks", "jobs") + (("obs",) if with_obs else ()) + (("adsb",) if with_adsb else ()):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
    counts = {"mavlink_rows": 0, "mavlink_ids": [], "adsb_rows": 0, "adsb_ids": 0, "adsb_files": 0, "track_rows": 0, "tracks": len(trk),
              "obs_rows": 0, "job_rows": 0, "rows_per_source": {}}
    for (src, tid), rows in sorted(truth.items()):
        counts["rows_per_source"][src] = counts["rows_per_source"].get(src, 0) + len(rows)
        if src == "MAVLINK":
            write_csv(os.path.join(out_dir, "mavlink", f"{safe_label(tid)}.csv"), MAV_COLS, [rows[k] for k in sorted(rows)])
            counts["mavlink_rows"] += len(rows)
            counts["mavlink_ids"].append(tid)
        else:
            counts["adsb_rows"] += len(rows)
            counts["adsb_ids"] += 1
            if with_adsb:
                write_csv(os.path.join(out_dir, "adsb", f"{safe_label(tid)}.csv"), MAV_COLS, [rows[k] for k in sorted(rows)])
                counts["adsb_files"] += 1
    for tid, rows in trk.items():
        write_csv(os.path.join(out_dir, "tracks", f"track_{tid}.csv"), TRK_COLS, [rows[k] for k in sorted(rows)])
        counts["track_rows"] += len(rows)
    if with_obs:
        obs.sort(key=lambda r: (float(r[0]), r[5] if isinstance(r[5], (int, float)) else 0))
        write_csv(os.path.join(out_dir, "obs", "obs.csv"), OBS_COLS, obs)
        counts["obs_rows"] = len(obs)
    if jobs_ev:
        rows = [[j, f"{v['t0']:.3f}", f"{v['t1']:.3f}", v["status"], v["err"] or ""] for j, v in sorted(jobs_ev.items())]
        write_csv(os.path.join(out_dir, "jobs", "jobs.csv"), JOB_COLS, rows)
        counts["job_rows"] = len(rows)
    else:
        log("  no block-136 JOB_EVENT documents in the window: jobs/jobs.csv skipped")
    counts["stats"] = stats
    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mru", type=int, help="MRU number -> mongo host 10.1NN.28.205")
    ap.add_argument("--host", help="mongo host (overrides --mru)")
    ap.add_argument("--port", type=int, default=27017)
    ap.add_argument("--db", default="sensor_store")
    ap.add_argument("--run", default="latest", help="hex prefix | run_<hex> | friendly name | latest")
    ap.add_argument("--t0", help="window start, local time ('2026-09-03 15:25', '15:25' = today) or epoch")
    ap.add_argument("--t1", help="window end")
    ap.add_argument("--jobs", help="job range A-B -> window from block-136 job events")
    ap.add_argument("--label", default="", help="dump folder suffix: <run8>_<label> (default HHMM-HHMM)")
    ap.add_argument("--root", default="./archive", help="archive root (default ./archive)")
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--geoid-n", type=float, default=GEOID_N_DEFAULT, help="HAE - MSL at the site, m (default -31.4)")
    ap.add_argument("--antenna", help="fallback antenna origin 'lat,lon,hae_m' when the run publishes none")
    ap.add_argument("--no-obs", action="store_true", help="skip block-103 observations")
    ap.add_argument("--no-adsb", action="store_true", help="do not write adsb/<id>.csv (ADS-B still counted in meta)")
    ap.add_argument("--max-track-range-m", type=float, default=15_000.0, help="drop tracks farther than this from the antenna (0 = keep all)")
    ap.add_argument("--chunk-s", type=float, default=600.0, help="indexed full_sec query slice (default 600 s)")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing dump directory")
    ap.add_argument("--list-runs", action="store_true", help="list the unit's runs (newest first) and exit")
    ap.add_argument("--list-limit", type=int, default=15)
    ap.add_argument("--selftest", action="store_true", help="unit-test the NED / ECEF converters on synthetic payloads (no mongo)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    tz = ZoneInfo(a.tz)
    host = a.host or (f"10.1{a.mru:02d}.28.205" if a.mru else ap.error("--mru N or --host required"))       # MRU nn -> mx node 10.1nn.28.205
    cl = MongoClient(host, int(a.port), serverSelectionTimeoutMS=SELECT_TIMEOUT_MS, connectTimeoutMS=SELECT_TIMEOUT_MS, socketTimeoutMS=SOCKET_TIMEOUT_MS)
    db = cl[a.db]
    if a.list_runs:
        list_runs(db, tz, a.list_limit)
        return 0
    run = resolve_run(db, a.run)
    col, info = db[run], run_index(db).get(run, {})
    log(f"run {run} ({info.get('friendly') or 'unnamed'}, version {info.get('version') or '?'}) on {host}:{a.port}")
    jobs = None
    if a.jobs:
        j0, j1 = (int(x) for x in re.split(r"[-:,]", a.jobs.strip())[:2])
        jobs = (min(j0, j1), max(j0, j1))
        t0, t1, bt, nj = jobs_to_window(col, *jobs)
        log(f"jobs {jobs[0]}-{jobs[1]}: {nj} block-{bt} docs -> window {datetime.fromtimestamp(t0, tz)} .. {datetime.fromtimestamp(t1, tz)}")
    elif a.t0 and a.t1:
        t0, t1 = sorted((parse_local(a.t0, tz), parse_local(a.t1, tz)))
    else:
        ap.error("give --t0/--t1 or --jobs A-B")
    if t1 - t0 < 1.0:
        raise SystemExit("empty window")
    ant, ant_src = discover_origin(col, t0, t1, tuple(float(x) for x in a.antenna.split(",")) if a.antenna else None)
    tx, tx_src = discover_tx(col, t0, t1)
    log(f"antenna origin {ant} ({ant_src}); tx {tx} ({tx_src})")
    if ant is None:
        log("WARNING: no antenna origin — truth E/N/U blank, ECEF-layout tracks without a payload origin skipped (use --antenna)")
    day = datetime.fromtimestamp(t0, tz).strftime("%Y-%m-%d")
    hm = lambda t: datetime.fromtimestamp(t, tz).strftime("%H%M")            # noqa: E731
    label = safe_label(a.label) if a.label else f"{hm(t0)}-{hm(t1)}"
    out_dir = os.path.join(os.path.abspath(a.root), day, f"{run8(run)}_{label}")
    if os.path.exists(os.path.join(out_dir, "meta.json")) and not a.overwrite:
        log(f"exists: {out_dir} (complete dump; --overwrite to replace)")
        return 0
    work = out_dir + ".partial"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    tw = time.time()
    counts = dump(col, t0, t1, work, ant, tz, geoid_n=a.geoid_n, chunk_s=a.chunk_s, with_obs=not a.no_obs, with_adsb=not a.no_adsb,
                  max_range_m=a.max_track_range_m, jobs=jobs)
    cl.close()
    st = counts.pop("stats")
    lay = [k for k, v in st["layouts"].items() if v]
    now = time.time()
    meta = {"run": run, "run_id8": run8(run), "friendly_name": info.get("friendly") or "", "version": info.get("version") or "", "mru": a.mru,
            "host": host, "port": a.port, "db": a.db, "label": label, "day": day, "jobs_range": list(jobs) if jobs else None,
            "window_epoch": [t0, t1], "window_local": [datetime.fromtimestamp(t0, tz).isoformat(), datetime.fromtimestamp(t1, tz).isoformat()],
            "tz": a.tz, "antenna_origin_lat_lon_haeM": list(ant) if ant else None, "antenna": list(ant) if ant else None,
            "antenna_origin_source": ant_src, "tx_lla": list(tx) if tx else None, "tx_lla_source": tx_src, "geoid_n_m": a.geoid_n,
            "frame": "exact WGS-84 ENU about the antenna origin (geodetic -> ECEF -> tangent plane); truth altitude FEET MSL -> m HAE via ft*0.3048 + geoid_n",
            "143_layout": lay[0] if len(lay) == 1 else ("mixed" if lay else "none"), "layouts": st["layouts"],
            "layout_crosscheck_max_diff_m": st["xcheck_max_m"] if st["xcheck_n"] else None, "layout_crosscheck_n": st["xcheck_n"],
            "tracks_skipped_no_origin": st["no_origin"], "tracks_dropped_out_of_range": st["tracks_out_of_range"], "max_track_range_m": a.max_track_range_m,
            **counts, "columns": {"mavlink": MAV_COLS, "adsb": MAV_COLS, "tracks": TRK_COLS, "obs": OBS_COLS, "jobs": JOB_COLS},
            "chunk_s": a.chunk_s, "elapsed_s": round(now - tw, 1), "saved_at": now, "saved_at_local": datetime.fromtimestamp(now, tz).isoformat(),
            "tool": TOOL, "tool_version": TOOL_VERSION,
            "units_note": "alt_ft_wire=FEET MSL, vert_spd_wire_ftmin=ft/min, speed/vel m/s; E/N/U metres ENU about the antenna origin, WGS-84 HAE frame; "
                          "obs rr_mps = -amb_dop_ms (opening positive), rng/bi_rng metres"}
    with open(os.path.join(work, "meta.json.tmp"), "w") as f:
        json.dump(meta, f, indent=1, default=str)
    os.replace(os.path.join(work, "meta.json.tmp"), os.path.join(work, "meta.json"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.rename(work, out_dir)
    log(f"DUMP {out_dir}\n  mavlink {counts['mavlink_rows']} rows ({', '.join(counts['mavlink_ids']) or 'none'}) · adsb {counts['adsb_rows']} rows / "
        f"{counts['adsb_ids']} ids · tracks {counts['tracks']} / {counts['track_rows']} rows ({meta['143_layout']}; xcheck max "
        f"{st['xcheck_max_m']:.3f} m over {st['xcheck_n']}) · obs {counts['obs_rows']} · jobs {counts['job_rows']} · {meta['elapsed_s']} s")
    return 0


# ── self-test (no mongo): NED and ECEF converters must agree on one synthetic track ──────────────────────────────────
def selftest():
    ant = (33.75, -115.33, 134.2)
    fr = EnuFrame(ant)
    E, N, U, vE, vN, vU = 500.0, 1000.0, 200.0, 5.0, 10.0, 1.0
    P_enu, P_ned = np.diag([9.0, 4.0, 16.0, 0.36, 0.25, 0.49]), np.diag([4.0, 9.0, 16.0, 0.25, 0.36, 0.49])   # var(E,N,U,v..) / var(N,E,D,v..)
    o = (math.radians(ant[0]), math.radians(ant[1]), ant[2])
    ned = {"track_id": 1, "x_state": [N, E, -U, vN, vE, -vU], "p_cov": P_ned.tolist(), "total_associations": 3, "track_state": 2, "track_type": 3,
           "last_update_time": {"full_sec": 1700000000, "frac_sec": 0.25}, "truth_match": {"target_id": "mavlink_1_2", "match_type": "plurality_based, confidence_0.87"},
           "contributors": [{"node_id": 1002}], "antenna_location_origin_latitude_rad": o[0], "antenna_location_origin_longitude_rad": o[1],
           "antenna_location_origin_altitude_m": o[2]}
    lat, lon, h = ecef_to_lla_deg(*(fr.R.T @ np.array([E, N, U]) + fr.o))
    Pe = np.zeros((6, 6))
    Pe[:3, :3], Pe[3:, 3:] = fr.R.T @ P_enu[:3, :3] @ fr.R, fr.R.T @ P_enu[3:, 3:] @ fr.R
    ecef = {"track_id": 2, "x_state_ecef": [*(fr.R.T @ np.array([E, N, U]) + fr.o), *(fr.R.T @ np.array([vE, vN, vU]))], "p_cov_ecef": Pe.tolist(),
            "latitude_rad": math.radians(lat), "longitude_rad": math.radians(lon), "altitude_m": h, "total_associations": 1, "track_state": 1,
            "truth_match": {"target_id": "DAL1556", "match_type": "range_and_el"}}
    stats = new_stats()
    rows = dict(track_rows(1700000000.5, {"payload": [ned, ecef, {**ned, "track_id": 3, "x_state_ecef": ecef["x_state_ecef"]}]}, {}, fr, stats))
    want = ["500.00", "1000.00", "200.00", "5.000", "10.000", "1.000", "3.00", "2.00", "4.00", "0.600", "0.500", "0.700"]
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    r1, r2, r3 = rows[1], rows[2], rows[3]
    check("NED layout E,N,U,vE,vN,vU", r1[2:8] == want[:6])
    check("NED sigmas pos (3,2,4) vel (.6,.5,.7)", r1[8:11] + r1[18:21] == want[6:])
    check("NED meta: assoc/state/lu/truth/conf/source/contrib/type",
          r1[11:18] == [3, 2, "1700000000.250", "mavlink_1_2", "0.87", "MAVLINK", '[{"node_id": 1002}]'] and r1[21] == 3)
    check("ECEF layout E,N,U,vE,vN,vU (fallback origin)", r2[2:8] == want[:6])
    check("ECEF sigmas rotated R P R^T", r2[8:11] + r2[18:21] == want[6:])
    check("ECEF truth_match source inferred ADS-B, conf blank", r2[14:17] == ["DAL1556", "", "ADS-B"])
    check("both layouts: NED written, ECEF cross-check < 1 mm", r3[2:5] == want[:3] and stats["xcheck_n"] == 1 and stats["xcheck_max_m"] < 1e-3)
    check("layout counts", stats["layouts"] == {"ned_xstate": 2, "ecef": 1} and stats["no_origin"] == 0)
    check("ECEF without any origin is skipped", dict(track_rows(1.0, {"payload": [ecef]}, {}, None, new_stats())) == {})
    tr = truth_rows(1700000000.0, {"payload": [{"target_id": "mavlink_1_2", "source": "MAVLINK", "lat": lat, "lon": lon,
                                                 "altitude": (h - GEOID_N_DEFAULT) / M_PER_FT, "validposition": 1, "speed_mps": 2.5},
                                                {"target_id": "UPS2908 ", "source": "ADS-B", "lat": 0.0, "lon": 0.0, "altitude": 36025, "validposition": 0}]},
                    fr, GEOID_N_DEFAULT)
    check("truth ft MSL -> m HAE lands on the track (E,N,U)", tr[0][:2] == ("MAVLINK", "mavlink_1_2") and tr[0][2][5:8] == want[:3] and tr[0][2][8] == 2.5)
    check("ADS-B (0,0) placeholder: ENU blank, flagged invalid, id stripped", tr[1][:2] == ("ADS-B", "UPS2908") and tr[1][2][5:8] == ["", "", ""] and tr[1][2][12] == 0)
    la, lo, hh = ecef_to_lla_deg(*lla_to_ecef(math.radians(33.751837), math.radians(-115.331465), 134.22))
    check("ECEF <-> LLA round trip", abs(la - 33.751837) < 1e-9 and abs(lo + 115.331465) < 1e-9 and abs(hh - 134.22) < 1e-6)
    ob = obs_rows(5.0, {"job_id": 7, "node_id": 0, "payload": {"job_id": 23146, "dwell_id": 23146, "sensor_nodes": [{"node_id": 1002}],
                                                                "observations": [{"obs_id": 9, "az_rad": 1.0, "el_rad": 0.1, "amb_rng_km": 0.5, "amb_dop_ms": -6.0,
                                                                                  "amb_bistatic_rng_km": 1.0, "amb_bistatic_rng_rate_ms": -12.0, "snr_db": 16.7,
                                                                                  "truth_target": {"target_id": "mavlink_1_2", "match_type": "range_and_el"}}]}})
    check("obs row: rng m, rr = -dop, bistatic, node from sensor_nodes", ob[0][2:] == [23146, 23146, 1002, 9, "1.000000", "0.100000", "500.0", "6.000", "1000.0",
                                                                                     "-12.000", "16.70", "mavlink_1_2", "range_and_el"])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
