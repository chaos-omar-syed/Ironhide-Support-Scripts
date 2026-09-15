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
    meta.json                run, window, antenna, tx_lla, counts, layouts, mru / host, saved_at, label, columns
  + a flight entry appended to <day>/flights.json (the 8/28 schema — n, t0, t1, t0_pdt, t1_pdt, drone_ids, segments, passes, tracking,
    issues — plus "dir", "label", "source"); ih.data.append_flight_entry assigns the flight number, ih.data.refresh_registry makes
    it replayable (FLIGHT_WINDOWS / FLIGHT_DIRS).

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
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime

import numpy as np

from . import data as D
from . import feed as F

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


def flight_entry(meta: dict, out_dir: str) -> dict:
    """The flights.json entry for a saved window (8/28 schema fields + dir / label / source); ``n`` is added by
    ih.data.append_flight_entry."""
    t0, t1 = meta["window"]
    tg, ic = meta["roles"]["target"], meta["roles"]["interceptor"]
    return {"label": meta["label"], "dir": out_dir, "t0": t0, "t1": t1, "t0_pdt": iso_pdt(t0), "t1_pdt": iso_pdt(t1),
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
        progress(0.97, "writing meta.json")
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
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    entry = flight_entry(meta, out_dir)
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
