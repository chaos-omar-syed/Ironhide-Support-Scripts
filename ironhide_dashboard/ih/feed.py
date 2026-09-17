"""Snapshot providers. Both return the SAME structure so the LIVE page has one
code path — the archive replay IS the test harness for the live view.

snapshot(t_now, hist_s) -> {
    ok, err, source, t_now, ant (lat,lon,alt) | None,
    tgt, itc            truth arrays (n,7) TR layout within [t_now-hist, t_now]
    tgt_hist, itc_hist  truth since flight start (archive) / since live start, capped
    tracks              {tid: (m,16) TK layout} within [t_now-hist, t_now]:
                        (t,E,N,U,sigE,sigN,sigU,lu,assoc,state,vE,vN,vU,sigvE,sigvN,sigvU) — cols 13..15 = 1σ of the
                        filtered velocity per ENU axis (m/s; NaN when the state has no covariance, NEVER 0)
    obs                 raw radar obs (n,8) = (t, az_rad, el_rad, rng_m, rr_mps, bi_rng_m, bi_rr_mps, match_code) within the window
                        (archive: F1 npz only, rr / bistatic NaN; live: block 103 rolling 420 s buffer, rr = -amb_dop_ms =
                        MONOSTATIC range rate, OPENING POSITIVE — spa's convention, spa/tracks/frames.py build_obs_df: bistatic
                        rate = -2·amb_dop; bi_* = amb_bistatic_rng_km / amb_bistatic_rng_rate_ms when published, else NaN;
                        match_code: NaN none · 1 target · 2 interceptor · 3 other/ADS-B — the radar's per-obs truth_target)
    obs_meta            CONTRACT: {"truth_target_id": object array (str|None), "truth_match_type": object array (str|None)}
                        ALIGNED row-for-row with ``obs`` (block 103 observations[].truth_target)
    track_meta          CONTRACT: {tid: {"truth_match_id": str|None, "truth_match_conf": float|nan, "contributors": str(JSON)|None,
                        "t": epoch}} — the radar's runtime truth match per track (block 143 payload.truth_match: target_id is a
                        MAVLink id or an ADS-B callsign, match_type 'plurality_based, confidence_0.xx'); newest state wins;
                        only tracks present in ``tracks``
    tx_lla              CONTRACT: transmitter (lat deg, lon deg, HAE m) when the run publishes one (2026.9.x TRACKS
                        contributors[].tx_lla_ddm, or an explicit TX field); None = co-located TX/RX -> bistatic = 2 x mono.
                        ``tx`` carries the same value (older name)
    unit                'MRU91' — label only (live: from the MRU number)
    feeds               [feed_status dicts]
    stale               True when this is a re-served last-good snapshot (live offline)
    data_age            live: seconds between the wall clock and the run's newest document
    anchored            live: True when t_now was pinned to the run's newest radar data (stale run)
    other               CONTRACT: [(target_id, (n,7) TR rows)] — every MAVLink feed that is NOT a role (matches neither
                        auto-assign pattern and is in neither explicit id list, feed.feed_role -> "unassigned"), one entry
                        per target_id, rows in the SAME TR layout as tgt / itc.  Never merged, never graded: the map draws
                        them grey so every live drone is visible.  These feeds also appear in ``feeds`` with role "unassigned".
    has_truth           live: True when a TARGET or INTERCEPTOR MAVLink feed has a block-106 row in the window
    has_any_truth       live: True when ANY MAVLink block-106 row is in the window (unassigned feeds included)
    n_adsb              live: block-106 rows in the window that were NOT MAVLINK (ADS-B etc., ignored)
    truth_unplaced      live: MAVLINK rows exist but the run has no TRACKS document yet (antenna origin unknown,
                        so the truth cannot be placed in ENU) — shown as "truth arriving, no radar origin yet"
}

LIVE PATH (mongo) — what the archive replay cannot exercise, and the traps found on MRU39:
  * Every windowed query filters on ``time_spec.full_sec`` (indexed: block_type_1_time_spec.full_sec_-1_
    time_spec.frac_sec_-1), NEVER on ``time_spec_float`` (unindexed: a range on it degrades to a scan of
    every block-106 document — 139 k × 56 kB of ADS-B payload on MRU39 — and blows the 3 s socket
    timeout on every tick).  The exact float bound is re-applied in Python.
  * Block-106 truth is fetched with ``payload.source == "MAVLINK"`` in the query and a field-level
    projection (lat / lon / altitude / velocities / validposition / target_id), so an ADS-B-only unit
    costs ~40 ms per tick instead of shipping 56 kB per document.  ADS-B rows are never truth.
  * Incremental buffers: the first fetch is bounded to [t_now − max(hist, spec window), t_now]; every
    later tick fetches only (last seen − OVERLAP, t_now] and appends (dedup on (t, id)); rows older
    than HIST_CAP_S are dropped.  Nothing ever re-scans the whole run.
  * Data-anchored clock: when the run's newest document is older than LIVE_STALE_S the view pins
    t_now to the newest RADAR data (newest block-143 document if any, else the newest document) and
    reports ``data_age`` — a stale run renders its last tracks instead of an empty wall-clock window.
  * A run without any TRACKS(143) document has no antenna origin: the snapshot is still "connected"
    (ant None, no truth ENU possible) — never OFFLINE.
  * Payloads are a dict OR a list of dicts (both occur); list entries that are not dicts are skipped;
    missing ``p_cov`` -> position AND velocity sigmas NaN (never 0).
  * TWO block-143 state layouts: 2026.3.x (MRU91 archive) publishes ``x_state`` NED + ``p_cov``; 2026.9.x
    (MRU39, payload version_id 1.7) publishes ONLY ``x_state_ecef`` + ``p_cov_ecef`` + the track's own
    ``latitude_rad / longitude_rad / altitude_m`` (corr_lib.load_tracks returns {} on it).  The ECEF layout
    is placed through the SAME EnuFrame as the truth (track LLA -> ENU about the antenna origin, so no
    curvature bias between truth and track), velocities rotate ECEF -> ENU, sigmas = sqrt(diag(R P R^T)).
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone

import numpy as np
import polars as pl
import streamlit as st

from . import data as D

TIMEOUT_MS = 8000   # 2026-09-15: MRU91 per-tick truth window (payload.source filter over 56 kB ADS-B docs, unindexed) takes 0.8-3.4 s -> 3 s read as OFFLINE "no data"
PROBE_TIMEOUT_MS = 8000   # 2026-09-15: the MRU91 run listing takes ~2.3 s from this host; 3 s read as "unreachable" while the unit was up
AIR_TRAFFIC, TRACKS, DWELL_WITH_OBS = 106, 143, 103
HIST_CAP_S = 15 * 60
TIME_KEY = "time_spec.full_sec"          # the indexed time field (block_type, full_sec, frac_sec)
OVERLAP_S = 2.0                          # incremental fetch overlap (late-arriving documents)
CHUNK_S = 12.0                           # 2026-09-15 MRU91: one windowed fetch per block type covers at most this many seconds
INGEST_BUDGET_S = 1.5                    # wall time a tick may spend back-filling older chunks (the forward page always runs)
RESET_GAP_S = 3 * CHUNK_S                # forward gap (idle tab / long outage) beyond which the buffer restarts at the present
QUERY_MAX_MS = 6000                      # server-side maxTimeMS per windowed query (< socketTimeoutMS: a slow query fails fast, not the tick)
BATCH_SIZE = 20                          # cursor batch size: one reply stays a few hundred kB even at 35 kB per TRACKS document
STALE_HOLD_S = 30.0                      # a connection-level failure renders the filled buffer (ok, stale) for this long after the last good tick, then OFFLINE
LIVE_STALE_S = 10.0                      # newest document older than this -> data-anchored clock
PROBE_LIMIT = 25                         # runs listed (newest first) with block counts
OBS_PROJECTION = {"time_spec_float": 1, "time_spec": 1, "payload.observations.az_rad": 1, "payload.observations.el_rad": 1,
                  "payload.observations.amb_rng_km": 1, "payload.observations.amb_dop_ms": 1,
                  "payload.observations.amb_bistatic_rng_km": 1, "payload.observations.amb_bistatic_rng_rate_ms": 1,
                  "payload.observations.truth_target.target_id": 1, "payload.observations.truth_target.match_type": 1}
TRUTH_PROJECTION = {"time_spec_float": 1, "time_spec": 1, "payload.source": 1, "payload.target_id": 1, "payload.lat": 1, "payload.lon": 1,
                    "payload.altitude": 1, "payload.velocity_n_mps": 1, "payload.velocity_e_mps": 1, "payload.vertical_speed": 1,
                    "payload.validposition": 1}
TRACK_PROJECTION = {"time_spec_float": 1, "time_spec": 1, "payload.track_id": 1, "payload.x_state": 1, "payload.p_cov": 1,
                    "payload.x_state_ecef": 1, "payload.p_cov_ecef": 1, "payload.latitude_rad": 1, "payload.longitude_rad": 1, "payload.altitude_m": 1,
                    "payload.track_velocity_n": 1, "payload.track_velocity_e": 1,
                    "payload.last_update_time": 1, "payload.total_associations": 1, "payload.track_state": 1,
                    "payload.antenna_location_origin_latitude_rad": 1, "payload.antenna_location_origin_longitude_rad": 1,
                    "payload.antenna_location_origin_altitude_m": 1,
                    "payload.truth_match": 1, "payload.contributors": 1}   # the radar's runtime truth match (target_id, match_type "plurality_based, confidence_0.xx")
ECEF_KEYS = ("payload.x_state_ecef", "payload.p_cov_ecef", "payload.latitude_rad", "payload.longitude_rad", "payload.altitude_m")
TRACK_PROJECTION_NED = {k: v for k, v in TRACK_PROJECTION.items() if k not in ECEF_KEYS}   # a unit publishing BOTH layouts: layout A wins in
#                                                                                             track_rows, so the ECEF copy (p_cov_ecef 457 B x 23 payloads) is dead weight (35 kB -> 20 kB per doc)
WGS_A, WGS_F = 6378137.0, 1.0 / 298.257223563
WGS_E2 = WGS_F * (2.0 - WGS_F)


def lla_to_ecef(lat_rad: float, lon_rad: float, h_m: float) -> np.ndarray:
    """WGS-84 geodetic (rad, rad, m HAE) -> ECEF (m)."""
    sl, cl = math.sin(lat_rad), math.cos(lat_rad)
    n = WGS_A / math.sqrt(1.0 - WGS_E2 * sl * sl)
    return np.array([(n + h_m) * cl * math.cos(lon_rad), (n + h_m) * cl * math.sin(lon_rad), (n * (1.0 - WGS_E2) + h_m) * sl])


def enu_rotation(lat_rad: float, lon_rad: float) -> np.ndarray:
    """3x3 matrix R with ENU = R · (ECEF − ECEF_origin); rows = east, north, up unit vectors."""
    sl, cl, so, co = math.sin(lat_rad), math.cos(lat_rad), math.sin(lon_rad), math.cos(lon_rad)
    return np.array([[-so, co, 0.0], [-sl * co, -sl * so, cl], [cl * co, cl * so, sl]])
NEWEST_SORT = [(TIME_KEY, -1), ("time_spec.frac_sec", -1)]


def _client(host: str, port: int, timeout_ms: int = TIMEOUT_MS, socket_timeout_ms: int | None = None):
    from pymongo import MongoClient

    return MongoClient(host, int(port), serverSelectionTimeoutMS=int(timeout_ms), connectTimeoutMS=int(timeout_ms),
                       socketTimeoutMS=int(socket_timeout_ms if socket_timeout_ms is not None else timeout_ms))


_CLIENTS: dict[tuple, tuple] = {}        # (host, port) -> (MongoClient factory in force, client): the live tick reuses one connection


def _shared_client(host: str, port: int):
    """One MongoClient per (host, port) for the live tick (no handshake / server selection per tick).  Re-created when
    pymongo.MongoClient has been swapped (the tests install a fake server per test)."""
    import pymongo

    key = (str(host), int(port))
    ent = _CLIENTS.get(key)
    if ent is not None and ent[0] == pymongo.MongoClient:
        return ent[1]
    if ent is not None:
        try:
            ent[1].close()
        except Exception:
            pass
    cl = _client(host, port)
    _CLIENTS[key] = (pymongo.MongoClient, cl)
    return cl


# ── MRU host convention ──────────────────────────────────────────────────────
MRU_HOST_FMT = "10.1{nn:02d}.28.205"     # mx node of MRU <nn>: MRU91 -> 10.191.28.205 · MRU46 -> 10.146.28.205 · MRU39 -> 10.139.28.205 · MRU48 -> 10.148.28.205


def resolve_mru_host(nn) -> str:
    """MRU number -> mx-node IP by the site convention (10.1NN.28.205).  ValueError outside 1..99."""
    n = int(nn)
    if not 1 <= n <= 99:
        raise ValueError(f"MRU number out of range: {nn}")
    return MRU_HOST_FMT.format(nn=n)


def live_host(s=None) -> str:
    """The host the Data-source page connects to: the Advanced custom host when set, else the MRU convention ('' if neither)."""
    s = st.session_state if s is None else s
    custom = str(s.get("live_custom_host") or "").strip()
    if custom:
        return custom
    try:
        return resolve_mru_host(s.get("mru_number") or 0)
    except (TypeError, ValueError):
        return ""


def live_unit(s=None) -> str:
    """'MRU91' from the MRU number (label only); the host when only a custom host is known."""
    s = st.session_state if s is None else s
    try:
        n = int(s.get("mru_number") or 0)
    except (TypeError, ValueError):
        n = 0
    if n:
        return f"MRU{n}"
    return str(s.get("live_custom_host") or s.get("live_host") or D.UNIT)


def empty_obs_meta(n: int) -> dict:
    """``snap["obs_meta"]`` with no radar truth match: object arrays of None aligned with the obs rows."""
    return {"truth_target_id": np.full(int(n), None, dtype=object), "truth_match_type": np.full(int(n), None, dtype=object)}


# ── Archive ──────────────────────────────────────────────────────────────────
def archive_snapshot(t_now: float, hist_s: float) -> dict:
    b = D.archive_bundle(int(st.session_state["flight"]), D.roles_key())          # the role assignment is part of the cache key
    lo = t_now - hist_s
    lo_obs = t_now - max(hist_s, float(st.session_state.get("spec_window", 120)))
    start = b["t0"] - D.REPLAY_LEAD_S
    obs = D.slice_obs(D.archive_obs(b["flight"]), lo_obs, t_now)
    return {
        "ok": True, "err": None, "source": "archive", "t_now": t_now, "ant": b["ant"], "tx": None, "tx_lla": None, "stale": False,   # None = co-located TX/RX (MRU91)
        "unit": D.UNIT,
        "label": f"ARCHIVE · {D.SITE} {D.DAY} · RUN {D.RUN} · FLIGHT {b['flight']}",
        "tgt": D.slice_truth(b["tgt"], lo, t_now), "itc": D.slice_truth(b["itc"], lo, t_now),
        "tgt_hist": D.slice_truth(b["tgt"], max(start, t_now - HIST_CAP_S), t_now),
        "itc_hist": D.slice_truth(b["itc"], max(start, t_now - HIST_CAP_S), t_now),
        "tracks": D.slice_tracks(b["tracks"], lo, t_now),
        "obs": obs, "obs_meta": empty_obs_meta(len(obs)),
        "feeds": [D.feed_status(r, t_now) for r in b["raw"]], "track_meta": dict(b.get("track_meta") or {}),
        "t_start": start, "data_age": 0.0, "anchored": False, "has_truth": bool(len(b["tgt"]) or len(b["itc"])),
        "has_any_truth": bool(len(b["tgt"]) or len(b["itc"])), "other": [], "n_adsb": 0,   # archive CSVs are role-assigned by D.archive_bundle: no unassigned feeds
    }


# ── Live MRU (mongo): query helpers ──────────────────────────────────────────
def _doc_t(d: dict) -> float | None:
    t = d.get("time_spec_float")
    if t is None:
        ts = d.get("time_spec") or {}
        if "full_sec" not in ts:
            return None
        t = float(ts.get("full_sec", 0)) + float(ts.get("frac_sec", 0.0))
    return float(t)


def _payloads(d: dict) -> list[dict]:
    pls = d.get("payload") or []
    if isinstance(pls, dict):
        pls = [pls]
    return [p for p in pls if isinstance(p, dict)]


def _time_q(t0: float, t1: float | None = None) -> dict:
    q = {"$gte": int(math.floor(t0))}
    if t1 is not None:
        q["$lte"] = int(math.ceil(t1))
    return q


def window(col, block_type: int, t0: float, t1: float, projection: dict, extra: dict | None = None):
    """Documents of one block type with t0 <= time_spec_float <= t1, through the INDEXED
    ``time_spec.full_sec`` (the exact float bound is applied here).  Yields (t, doc)."""
    q = {"block_type": int(block_type), TIME_KEY: _time_q(t0, t1)}
    if extra:
        q.update(extra)
    for d in col.find(q, projection=projection, batch_size=BATCH_SIZE, max_time_ms=QUERY_MAX_MS):
        t = _doc_t(d)
        if t is None or t < t0 or t > t1:
            continue
        yield t, d


def truth_window(col, t0: float, t1: float):
    """MAVLINK block-106 documents in [t0, t1] with the payload array FILTERED server-side to the MAVLINK entries
    (MRU91: a 106 document holds ~120 sub-payloads of which ONE is MAVLINK -> 21 kB projected shrinks to ~300 B) and
    ``n_other`` = the number of non-MAVLINK entries dropped (ADS-B, counted for n_adsb).  Aggregation pipeline on the
    same indexed $match; a collection without ``aggregate`` (the test fake) falls back to the plain projected find."""
    q = {"block_type": AIR_TRAFFIC, TIME_KEY: _time_q(t0, t1), "payload.source": "MAVLINK"}
    if not hasattr(col, "aggregate"):
        yield from window(col, AIR_TRAFFIC, t0, t1, TRUTH_PROJECTION, extra={"payload.source": "MAVLINK"})
        return
    is_arr = {"$isArray": "$payload"}
    pipe = [{"$match": q},
            {"$project": {"time_spec_float": 1, "time_spec": 1,
                          "n_other": {"$cond": [is_arr, {"$size": {"$filter": {"input": "$payload", "cond": {"$ne": ["$$this.source", "MAVLINK"]}}}}, 0]},
                          "payload": {"$cond": [is_arr, {"$filter": {"input": "$payload", "cond": {"$eq": ["$$this.source", "MAVLINK"]}}}, "$payload"]}}},
            {"$project": {**TRUTH_PROJECTION, "n_other": 1}}]
    for d in col.aggregate(pipe, batchSize=BATCH_SIZE, maxTimeMS=QUERY_MAX_MS):
        t = _doc_t(d)
        if t is None or t < t0 or t > t1:
            continue
        yield t, d


def newest_t(col, block_type: int | None = None, extra: dict | None = None) -> float | None:
    """time_spec_float of the newest document (of one block type: indexed sort; any type: $natural)."""
    if block_type is None:
        d = col.find_one({}, sort=[("$natural", -1)], projection={"time_spec_float": 1, "time_spec": 1})
    else:
        q = {"block_type": int(block_type)}
        if extra:
            q.update(extra)
        d = col.find_one(q, sort=NEWEST_SORT, projection={"time_spec_float": 1, "time_spec": 1})
    return _doc_t(d) if d else None


def antenna_origin(col, doc: dict | None = None) -> tuple[float, float, float] | None:
    """(lat deg, lon deg, HAE m) from any TRACKS(143) payload; None when the run has no track yet."""
    d = doc if doc is not None else col.find_one({"block_type": TRACKS}, projection={"payload": 1})
    for p in _payloads(d or {}):
        try:
            return (math.degrees(float(p["antenna_location_origin_latitude_rad"])),
                    math.degrees(float(p["antenna_location_origin_longitude_rad"])),
                    float(p["antenna_location_origin_altitude_m"]))
        except (KeyError, TypeError, ValueError):
            continue
    return None


# ── Live MRU: probe / run listing ────────────────────────────────────────────
def probe(host: str, port: int, db: str, run_filter: str, limit: int = PROBE_LIMIT) -> dict:
    """Reachability + run_* collections newest-first, never raises, every server op <= TIMEOUT_MS.
    Rows: {name, friendly, start_t (runs.date_time, UTC -> epoch), version, last_t}; ``run_filter`` is a substring of the
    collection name or of the friendly name.  Block counts (total 143 / total 106, cheap index counts) only for the newest
    ``limit`` runs — nothing is counted for the other ~250 up front (they are listed with their last-document time)."""
    out = {"ok": False, "err": None, "runs": [], "ts": time.time(), "latency_ms": None, "n_total": 0, "limit": int(limit)}
    if not host:
        out["err"] = "no host configured"
        return out
    t = time.time()
    try:
        cl = _client(host, port, timeout_ms=PROBE_TIMEOUT_MS)
        cl.admin.command("ping")
        out["latency_ms"] = round((time.time() - t) * 1000, 1)
        d = cl[db]
        names = [n for n in d.list_collection_names() if n.startswith("run_")]
        out["n_total"] = len(names)
        idx = run_index(d)
        flt = (run_filter or "").replace("-", "").lower().strip()
        if flt:                                                          # substring of the collection name OR of the friendly run name
            names = [n for n in names if flt in n.lower() or flt in idx.get(n, {}).get("friendly", "").lower()]
        rows = []
        for n in names:
            last = d[n].find_one({}, sort=[("$natural", -1)], projection={"time_spec_float": 1, "time_spec": 1})
            ri = idx.get(n, {})
            rows.append({"name": n, "friendly": ri.get("friendly", ""), "start_t": ri.get("start_t"), "version": ri.get("version", ""),
                         "last_t": _doc_t(last) if last else None})
        rows.sort(key=lambda r: r["last_t"] or 0, reverse=True)
        for r in rows[:limit]:   # lazy: index counts for the visible rows only
            col = d[r["name"]]
            r["n143"] = col.count_documents({"block_type": TRACKS})
            r["n106"] = col.count_documents({"block_type": AIR_TRAFFIC})
        out["runs"], out["ok"] = rows, True
        cl.close()
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:300]
    return out


def run_names(db) -> dict[str, str]:
    """{run_<hex>: friendly name} from the runs collection (best effort, schema varies)."""
    try:
        import corr_lib as C

        return C.run_names(db)
    except Exception:
        return {}


RUNS_DT_FMT = "%Y-%m-%d_%H_%M_%S"          # runs.date_time is UTC (MRU39: start + run_duration == the run's newest document)


def run_index(db) -> dict[str, dict]:
    """{run_<hex>: {friendly, start_t (epoch | None), version}} from the ``runs`` collection — best effort, never raises."""
    out: dict[str, dict] = {}
    try:
        for d in db["runs"].find({}, projection={"run_id_str": 1, "run_id": 1, "name": 1, "run_name": 1, "date_time": 1, "version": 1}):
            rid = str(d.get("run_id_str") or d.get("run_id") or "").replace("-", "")
            if not re.fullmatch(r"[0-9A-Za-z]{8,}", rid):                  # hex on real units (the fake uses readable ids)
                continue
            start = None
            try:
                start = datetime.strptime(str(d.get("date_time")), RUNS_DT_FMT).replace(tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                pass
            out[f"run_{rid.lower()}"] = {"friendly": str(d.get("name") or d.get("run_name") or ""), "start_t": start, "version": str(d.get("version") or "")}
    except Exception:
        pass
    return out


def run_label(r: dict, now: float | None = None) -> str:
    """Run selectbox label '<name> · <id8> · <start PDT> · <last data age>' — e.g. 'Gold_Falcon · 8e3fcceb · 11:56:23 · 3.1 h ago'."""
    now = time.time() if now is None else float(now)
    name = r.get("friendly") or "unnamed"
    id8 = str(r.get("name") or "")[4:12] or "—"
    start = D.pdt_hms(r["start_t"]) if r.get("start_t") else "start —"
    lt = r.get("last_t")
    age = f"{D.fmt_age(now - lt)} ago" if lt else "no data"
    return f"{name} · {id8} · {start} · {age}"


RECENT_S = 600.0                          # Connect auto-select: prefer a run whose newest document is younger than this


def connect(host: str, port: int, db: str, run_filter: str = "", recent_s: float = RECENT_S) -> dict:
    """Data-source CONNECT: probe the unit and pick the run to follow — the NEWEST run with radar-track (143) or air-traffic
    (106) documents, preferring one whose newest document is < ``recent_s`` old (being written right now); else the newest
    run with data; else the newest run.  Never raises.
    -> {ok, err, probe, run (collection | None), row (its probe row | None), reason}"""
    pr = probe(host, port, db, run_filter)
    out = {"ok": bool(pr["ok"]), "err": pr["err"], "probe": pr, "run": None, "row": None, "reason": ""}
    if not pr["ok"]:
        return out
    rows = pr["runs"]
    if not rows:
        out["reason"] = "no run_* collections on this unit"
        return out
    now = time.time()
    with_data = [r for r in rows if (r.get("n143") or 0) + (r.get("n106") or 0) > 0]
    recent = [r for r in with_data if r.get("last_t") and now - r["last_t"] <= recent_s]
    if recent:
        row, out["reason"] = recent[0], f"newest run with data younger than {D.fmt_age(recent_s)}"
    elif with_data:
        row, out["reason"] = with_data[0], "newest run with radar / air-traffic data (no run is being written right now)"
    else:
        row, out["reason"] = rows[0], "newest run (no block counts available)"
    out["run"], out["row"] = row["name"], row
    return out


FEED_PROJECTION = {"time_spec_float": 1, "time_spec": 1, "payload.source": 1, "payload.target_id": 1}
INFO_WINDOW_S = 60.0


def live_info(host: str, port: int, db: str, run: str, window_s: float = INFO_WINDOW_S, start_t: float | None = None) -> dict:
    """LIVE INFO panel (Data-source page, every 2 s): a handful of INDEXED queries over the last ``window_s`` of the run's
    DATA (never a scan): newest document / newest track, block counts in the window, the MAVLink feeds detected (id, rows,
    first/last, age, alive | stale | down), antenna origin, TX LLA, run start (``runs`` collection unless given).
    -> {ok, err, ts, latency_ms, host, port, run, friendly, start_t, newest_t, newest_143_t, data_age, n143_w, n103_w, n106_w,
        n_mav_docs_w, n_adsb_docs_w, feeds: [{id, rows, first_t, last_t, age, state}], ant, tx_lla, window_s}"""
    out = {"ok": False, "err": None, "ts": time.time(), "latency_ms": None, "host": host, "port": int(port or 0), "run": run, "friendly": "",
           "start_t": start_t, "newest_t": None, "newest_143_t": None, "data_age": None, "n143_w": 0, "n103_w": 0, "n106_w": 0,
           "n_mav_docs_w": 0, "n_adsb_docs_w": 0, "feeds": [], "ant": None, "tx_lla": None, "window_s": float(window_s)}
    if not host or not run:
        out["err"] = "not connected"
        return out
    try:
        t = time.time()
        cl = _client(host, port)
        cl.admin.command("ping")
        out["latency_ms"] = round((time.time() - t) * 1000, 1)
        d = cl[db]
        col = d[run]
        newest = newest_t(col)
        if newest is None:
            raise RuntimeError("run collection is empty")
        out["newest_t"], out["data_age"] = newest, max(0.0, time.time() - newest)
        out["newest_143_t"] = newest_t(col, TRACKS)
        lo = newest - float(window_s)
        tq = _time_q(lo, newest)
        out["n143_w"] = col.count_documents({"block_type": TRACKS, TIME_KEY: tq})
        out["n103_w"] = col.count_documents({"block_type": DWELL_WITH_OBS, TIME_KEY: tq})
        out["n106_w"] = col.count_documents({"block_type": AIR_TRAFFIC, TIME_KEY: tq})
        feeds: dict[str, dict] = {}
        n_mav_docs = 0
        for tt, doc in window(col, AIR_TRAFFIC, lo, newest, FEED_PROJECTION, extra={"payload.source": "MAVLINK"}):
            hit = False
            for p in _payloads(doc):
                if p.get("source") != "MAVLINK":
                    continue
                hit = True
                tid = str(p.get("target_id") or "?")
                f = feeds.setdefault(tid, {"id": tid, "rows": 0, "first_t": tt, "last_t": tt})
                f["rows"] += 1
                f["first_t"], f["last_t"] = min(f["first_t"], tt), max(f["last_t"], tt)
            n_mav_docs += hit
        out["n_mav_docs_w"], out["n_adsb_docs_w"] = n_mav_docs, max(0, out["n106_w"] - n_mav_docs)
        for f in feeds.values():
            f["age"] = newest - f["last_t"]
            f["state"] = "alive" if f["age"] <= 2.5 else ("stale" if f["age"] <= 15 else "down")
        out["feeds"] = sorted(feeds.values(), key=lambda f: f["id"])
        out["ant"] = antenna_origin(col)
        out["tx_lla"] = tx_origin(col)
        if start_t is None:
            ri = run_index(d).get(run, {})
            out["friendly"], out["start_t"] = ri.get("friendly", ""), ri.get("start_t")
            if out["start_t"] is None:
                first = col.find_one({}, sort=[("$natural", 1)], projection={"time_spec_float": 1, "time_spec": 1})
                out["start_t"] = _doc_t(first) if first else None
        out["ok"] = True
        cl.close()
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:300]
    return out


def mavlink_span(host: str, port: int, db: str, run: str) -> dict:
    """First / last MAVLINK block-106 document of the run (two indexed find_ones with the source filter) — the 'flight
    window' the SAVE TO ARCHIVE time range is prefilled with.  -> {ok, first_t, last_t, err}"""
    out = {"ok": False, "first_t": None, "last_t": None, "err": None}
    try:
        cl = _client(host, port)
        col = cl[db][run]
        q = {"block_type": AIR_TRAFFIC, "payload.source": "MAVLINK"}
        a = col.find_one(q, sort=[(TIME_KEY, 1), ("time_spec.frac_sec", 1)], projection={"time_spec_float": 1, "time_spec": 1})
        b = col.find_one(q, sort=NEWEST_SORT, projection={"time_spec_float": 1, "time_spec": 1})
        out["first_t"], out["last_t"] = (_doc_t(a) if a else None), (_doc_t(b) if b else None)
        out["ok"] = True
        cl.close()
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:300]
    return out


def run_detail(host: str, port: int, db: str, run: str) -> dict:
    """Lazy detail for ONE run (called when it is selected): newest document / newest track times,
    tracks in the last 15 min of its data, MAVLINK truth in the last 60 s of its data (bounded
    find_one), antenna origin present.  Never raises."""
    out = {"ok": False, "err": None, "run": run, "newest_t": None, "newest_143_t": None, "n143_15min": 0, "mavlink_60s": False, "ant": None}
    try:
        cl = _client(host, port)
        col = cl[db][run]
        out["newest_t"] = newest_t(col)
        out["newest_143_t"] = newest_t(col, TRACKS)
        if out["newest_143_t"] is not None:
            out["n143_15min"] = col.count_documents({"block_type": TRACKS, TIME_KEY: _time_q(out["newest_143_t"] - 900)})
        if out["newest_t"] is not None:
            d = col.find_one({"block_type": AIR_TRAFFIC, "payload.source": "MAVLINK", TIME_KEY: _time_q(out["newest_t"] - 60)},
                             projection={"time_spec_float": 1})
            out["mavlink_60s"] = d is not None
        out["ant"] = antenna_origin(col)
        out["ok"] = True
        cl.close()
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:300]
    return out


# ── Live MRU: row parsers (dict-or-list payloads, defensive) ─────────────────
def truth_rows(t: float, doc: dict, frame) -> tuple[list[tuple], int]:
    """Block-106 doc -> ([(tid, row in TRUTH_COLS order)], n_non_mavlink).  U = WGS-84 HAE metres via
    corr_lib.mavlink_alt_hae_m (wire altitude = FEET MSL); vertical_speed stays the wire ft/min
    (D.merge_truth scales it, exactly like the archive CSV column)."""
    import corr_lib as C

    rows, n_other = [], 0
    for p in _payloads(doc):
        if p.get("source") != "MAVLINK":
            n_other += 1
            continue
        lat, lon, alt_ft = p.get("lat"), p.get("lon"), p.get("altitude")
        if lat is None or lon is None or alt_ft is None:
            continue
        try:
            E, N, U = frame.enu(float(lat), float(lon), C.mavlink_alt_hae_m(float(alt_ft)))
            rows.append((str(p.get("target_id") or "?"),
                         (float(t), float(lat), float(lon), float(E), float(N), float(U),
                          float(p.get("velocity_e_mps") or 0.0), float(p.get("velocity_n_mps") or 0.0), float(p.get("vertical_speed") or 0.0),
                          1 if p.get("validposition") else 0)))
        except (TypeError, ValueError):
            continue
    return rows, n_other


def _lu(p: dict) -> float:
    lu = p.get("last_update_time")
    return float(lu.get("full_sec", 0)) + float(lu.get("frac_sec", 0.0)) if isinstance(lu, dict) else float(lu or 0.0)


def _origin_rad(p: dict, ant: tuple | None) -> tuple[float, float, float] | None:
    """Antenna origin (lat rad, lon rad, HAE m) from the payload, else from ``ant`` (degrees)."""
    try:
        return (float(p["antenna_location_origin_latitude_rad"]), float(p["antenna_location_origin_longitude_rad"]),
                float(p["antenna_location_origin_altitude_m"]))
    except (KeyError, TypeError, ValueError):
        if ant:
            return math.radians(float(ant[0])), math.radians(float(ant[1])), float(ant[2])
        return None


def track_rows(t: float, doc: dict, ant: tuple | None = None) -> list[tuple[int, tuple]]:
    """Block-143 doc -> [(tid, TK-layout row (t,E,N,U,sigE,sigN,sigU,lu,assoc,state,vE,vN,vU,sigvE,sigvN,sigvU))] — 16 values.
    Layout A (2026.3.x): ``x_state`` NED [N,E,D,vN,vE,vD] -> (E, N, -D, vE, vN, -vD), position sigmas = sqrt(p_cov diag)
    (corr_lib.load_tracks' math), velocity sigmas sigvE = sqrt(P[4,4]), sigvN = sqrt(P[3,3]), sigvU = sqrt(P[5,5]) (the
    NED velocity block [vN,vE,vD]; a sign flip leaves a variance unchanged).  Layout B (2026.9.x, version_id 1.7): no
    x_state — position from the track's ``latitude_rad / longitude_rad / altitude_m`` through the SAME EnuFrame as the
    truth (antenna origin from the payload, else ``ant``), velocity = R · x_state_ecef[3:6], position sigmas =
    sqrt(diag(R · p_cov_ecef[:3,:3] · R^T)), velocity sigmas = sqrt(diag(R · p_cov_ecef[3:6,3:6] · R^T)) (R rows = east,
    north, up).  Missing covariance -> NaN sigmas (NEVER 0), position and velocity alike; rows without a state are
    skipped.  Velocity sigmas are 1σ per ENU axis in m/s — the inputs of the velocity-state cards (spa grades them)."""
    import corr_lib as C

    out = []
    frame_cache: dict[tuple, object] = {}
    for p in _payloads(doc):
        tid = p.get("track_id")
        if tid is None:
            continue
        try:
            x = p.get("x_state")
            sE = sN = sU = svE = svN = svU = float("nan")
            if x is not None:                                                   # layout A: NED state
                x = [float(v) for v in x]
                if len(x) < 3:
                    continue
                E, N, U = x[1], x[0], -x[2]
                vN = x[3] if len(x) > 3 else float("nan")
                vE = x[4] if len(x) > 4 else float("nan")
                vU = -x[5] if len(x) > 5 else float("nan")
                P = p.get("p_cov")
                if P is not None:
                    P = np.asarray(P, float).reshape(6, 6)
                    sE, sN, sU = math.sqrt(max(P[1, 1], 0.0)), math.sqrt(max(P[0, 0], 0.0)), math.sqrt(max(P[2, 2], 0.0))
                    svE, svN, svU = math.sqrt(max(P[4, 4], 0.0)), math.sqrt(max(P[3, 3], 0.0)), math.sqrt(max(P[5, 5], 0.0))
            elif p.get("latitude_rad") is not None and p.get("longitude_rad") is not None:   # layout B: LLA + ECEF
                o = _origin_rad(p, ant)
                if o is None:
                    continue
                fr = frame_cache.get(o)
                if fr is None:
                    fr = frame_cache[o] = C.EnuFrame((math.degrees(o[0]), math.degrees(o[1]), o[2]))
                alt = p.get("altitude_m")
                E, N, U = fr.enu(math.degrees(float(p["latitude_rad"])), math.degrees(float(p["longitude_rad"])), None if alt is None else float(alt))
                R = enu_rotation(o[0], o[1])
                xe = p.get("x_state_ecef")
                if xe is not None and len(xe) >= 6:
                    vE, vN, vU = (float(v) for v in R @ np.asarray(xe[3:6], float))
                else:
                    vE, vN, vU = float(p.get("track_velocity_e") or 0.0), float(p.get("track_velocity_n") or 0.0), float("nan")
                Pe = p.get("p_cov_ecef")
                if Pe is not None:
                    Pe = np.asarray(Pe, float).reshape(6, 6)
                    Penu = R @ Pe[:3, :3] @ R.T
                    sE, sN, sU = (math.sqrt(max(float(Penu[i, i]), 0.0)) for i in range(3))
                    Pvenu = R @ Pe[3:6, 3:6] @ R.T
                    svE, svN, svU = (math.sqrt(max(float(Pvenu[i, i]), 0.0)) for i in range(3))
            else:
                continue
            out.append((int(tid), (float(t), float(E), float(N), float(U), sE, sN, sU, _lu(p), float(p.get("total_associations") or 0),
                                   float(p.get("track_state") or 0), float(vE), float(vN), float(vU), float(svE), float(svN), float(svU))))
        except (TypeError, ValueError):
            continue
    return out


def _opt(v) -> float:
    try:
        return float("nan") if v is None else float(v)
    except (TypeError, ValueError):
        return float("nan")


_CONF_RE = re.compile(r"confidence[_ =:]*([0-9]*\.?[0-9]+)")


def match_conf(match_type) -> float | None:
    """'plurality_based, confidence_0.87' -> 0.87 (None when the string carries no confidence)."""
    m = _CONF_RE.search(str(match_type or ""))
    return float(m.group(1)) if m else None


def track_meta(doc: dict) -> dict[int, dict]:
    """{track_id: {truth_match_id: str|None, truth_match_conf: float|nan, contributors: str(JSON)|None}} from a block-143
    doc's payload.truth_match (the radar's runtime truth correlation: target_id = a MAVLink id or an ADS-B callsign such
    as CSG2537, match_type 'plurality_based, confidence_0.xx') and payload.contributors (JSON-encoded, MRU39 shape
    [{network_id, cluster_id, tx_id, node_id, obs_id, tx_lla_ddm, rx_lla_ddm}]); tracks without a match -> id None."""
    out = {}
    for p in _payloads(doc):
        tid = p.get("track_id")
        if tid is None:
            continue
        tm = p.get("truth_match")
        mid = conf = None
        if isinstance(tm, dict):
            mid = tm.get("target_id")
            mid = None if mid in (None, "") else str(mid)
            conf = match_conf(tm.get("match_type"))
        contrib = p.get("contributors")
        if contrib is not None and not isinstance(contrib, str):
            contrib = json.dumps(contrib, default=str)
        try:
            out[int(tid)] = {"truth_match_id": mid, "truth_match_conf": float("nan") if conf is None else float(conf), "contributors": contrib or None}
        except (TypeError, ValueError):
            continue
    return out


def obs_rows(t: float, doc: dict) -> list[tuple]:
    """Block-103 doc -> [(t, az_rad, el_rad, rng_m, rr_mps, bi_rng_m, bi_rr_mps, truth_target_id, truth_match_type)] with
    rr = -amb_dop_ms (monostatic, OPENING positive; NaN when absent), the BISTATIC pair from amb_bistatic_rng_km /
    amb_bistatic_rng_rate_ms when the unit publishes them (NaN otherwise -> the engine uses 2 x mono for a co-located
    TX) and the radar's per-observation truth match (str | None each, from truth_target.target_id / .match_type, e.g.
    'DAL1556' / 'range_and_el'); observation entries that are not dicts / lack az or range are skipped."""
    out = []
    for p in _payloads(doc):
        for o in p.get("observations") or []:
            if not isinstance(o, dict):
                continue
            az, rk = o.get("az_rad"), o.get("amb_rng_km")
            if az is None or rk is None:
                continue
            try:
                dop = o.get("amb_dop_ms")
                rr = float("nan") if dop is None else -float(dop)
                bi_rng = _opt(o.get("amb_bistatic_rng_km")) * 1000.0
                bi_rr = _opt(o.get("amb_bistatic_rng_rate_ms"))
                tt = o.get("truth_target") if isinstance(o.get("truth_target"), dict) else {}
                tm, mt = tt.get("target_id"), tt.get("match_type")
                out.append((float(t), float(az), float(o.get("el_rad") or 0.0), float(rk) * 1000.0, rr, bi_rng, bi_rr,
                            None if tm in (None, "") else str(tm), None if mt in (None, "") else str(mt)))
            except (TypeError, ValueError):
                continue
    return out


_MAV_RE = re.compile(r"mav", re.I)


def match_role(mid, tgt_ids=(), itc_ids=()) -> str | None:
    """Role of a radar truth-match id: 'target' / 'interceptor' when it is one of the assigned MAVLink ids
    (else, for a MAVLink-looking id, by D.role_of), 'other' for anything else (an ADS-B callsign such as
    N432R -> the track is an airliner: excluded from both roles), None for no match."""
    if mid in (None, ""):
        return None
    m = str(mid)
    if m in set(map(str, tgt_ids or ())):
        return "target"
    if m in set(map(str, itc_ids or ())):
        return "interceptor"
    if _MAV_RE.search(m):
        return D.role_of(m)
    return "other"


MATCH_CODE = {None: float("nan"), "target": 1.0, "interceptor": 2.0, "other": 3.0}


TX_KEYS = (("tx_antenna_location_latitude_rad", "tx_antenna_location_longitude_rad", "tx_antenna_location_altitude_m"),
           ("transmitter_location_latitude_rad", "transmitter_location_longitude_rad", "transmitter_location_altitude_m"))


CO_LOCATED_M = 5.0                        # TX within this of the RX (horizontal or vertical) -> co-located -> tx_lla None


def tx_from_contributors(contrib) -> tuple[float, float, float] | None:
    """2026.9.x TRACKS payloads carry ``contributors: [{tx_lla_ddm: [lat deg, lon deg, alt m], rx_lla_ddm: [...], ...}]``
    (MRU39).  Returns the TX LLA when it differs from the RX by more than CO_LOCATED_M, None when co-located (bistatic =
    2 x mono) or absent.  Accepts the list or its JSON string (snap["track_meta"][tid]["contributors"])."""
    if isinstance(contrib, str):
        try:
            contrib = json.loads(contrib)
        except ValueError:
            return None
    for c in contrib or []:
        if not isinstance(c, dict):
            continue
        tx, rx = c.get("tx_lla_ddm"), c.get("rx_lla_ddm")
        try:
            tx = (float(tx[0]), float(tx[1]), float(tx[2]))
        except (TypeError, ValueError, IndexError):
            continue
        try:
            rx = (float(rx[0]), float(rx[1]), float(rx[2]))
        except (TypeError, ValueError, IndexError):
            return tx
        dn = (tx[0] - rx[0]) * 111_320.0
        de = (tx[1] - rx[1]) * 111_320.0 * math.cos(math.radians(rx[0]))
        return tx if (math.hypot(dn, de) > CO_LOCATED_M or abs(tx[2] - rx[2]) > CO_LOCATED_M) else None
    return None


def tx_origin(col) -> tuple[float, float, float] | None:
    """Best-effort TRANSMITTER LLA (deg, deg, HAE m) for the bistatic geometry of the measurement-space quad — the
    snapshot's ``tx_lla``: the newest TRACKS(143) / DWELL(103) payload carrying an explicit TX location (TX_KEYS) or a
    2026.9.x ``contributors[].tx_lla_ddm`` distinct from ``rx_lla_ddm``.  None when the run publishes none / TX and RX
    coincide -> the engine assumes TX co-located with RX (every vprime MRU: bistatic = 2 x monostatic) and the quad's
    caption says so.  Bounded: two indexed find_one calls."""
    for bt in (TRACKS, DWELL_WITH_OBS):
        try:
            d = col.find_one({"block_type": bt}, sort=NEWEST_SORT, projection={"payload": 1})
        except Exception:
            return None
        for p in _payloads(d or {}):
            for la, lo, al in TX_KEYS:
                try:
                    return (math.degrees(float(p[la])), math.degrees(float(p[lo])), float(p[al]))
                except (KeyError, TypeError, ValueError):
                    continue
            tx = tx_from_contributors(p.get("contributors"))
            if tx is not None:
                return tx
    return None


# ── Live MRU: incremental per-session buffers ────────────────────────────────
def _empty_rows() -> dict:
    return {"truth": {}, "tracks": {}, "obs": {}, "obs_tm": {}, "meta": {}, "lo": None, "hi": None, "n_adsb": 0}


def _buf(run: str) -> dict:
    """The per-session live buffer (st.session_state["_live_buf"]: survives Streamlit reruns, dropped on a run change /
    source switch).  Rows in [lo, hi]; per-run constants ``ant`` / ``tx`` / ``proj143`` are fetched once per run."""
    s = st.session_state
    b = s.get("_live_buf")
    if b is None or b.get("run") != run:
        b = {"run": run, **_empty_rows()}
        s["_live_buf"] = b
        s.pop("_obs_buf", None)   # legacy key
    return b


def _run_consts(col, b: dict, s) -> tuple[tuple | None, tuple | None, dict]:
    """(antenna origin, TX LLA, block-143 projection) for the run — fetched ONCE per run into the buffer (and mirrored to
    session state live_ant / live_tx as before); a run without a TRACKS document re-checks the (indexed, empty) find_one
    each tick until one appears.  The 143 probe document also picks the projection: a payload carrying BOTH ``x_state``
    and ``x_state_ecef`` (MRU91 2026.9.x) gets TRACK_PROJECTION_NED, anything else the full TRACK_PROJECTION."""
    ant = b.get("ant") or s.get("live_ant")
    if not ant or "proj143" not in b:
        d = col.find_one({"block_type": TRACKS}, sort=NEWEST_SORT, projection={"payload": 1}, max_time_ms=QUERY_MAX_MS)
        if d is not None:
            pls = _payloads(d)
            both = bool(pls) and all(("x_state" in p and "x_state_ecef" in p) for p in pls)
            b["proj143"] = TRACK_PROJECTION_NED if both else TRACK_PROJECTION
            ant = ant or antenna_origin(col, doc=d)
    if ant:
        b["ant"] = tuple(ant)
        s["live_ant"] = tuple(ant)
    if "tx" not in b:
        if "live_tx" in s:
            b["tx"] = s.get("live_tx")
        else:
            b["tx"] = tx_origin(col)                                       # once per run: explicit TX location, else co-located (None)
            s["live_tx"] = b["tx"]
    return (tuple(ant) if ant else None), b.get("tx"), (b.get("proj143") or TRACK_PROJECTION)


def _ingest(col, frame, b: dict, t0: float, t1: float, proj143: dict | None = None) -> list[str]:
    """Fetch [t0, t1] of truth (MAVLINK only) / tracks / obs into the buffers (dedup on (t, id)).  Each block type is its
    own guarded query: a slow / failed one is reported (returned as 'block: error') and retried next tick, the others land."""
    errs: list[str] = []
    if t1 < t0:
        return errs
    if frame is not None:
        n_other = 0
        try:
            for t, d in truth_window(col, t0, t1):
                rows, n_o = truth_rows(t, d, frame)
                n_other += int(d.get("n_other", n_o))
                for tid, r in rows:
                    b["truth"][(round(t, 3), tid)] = (tid, r)
        except Exception as ex:
            errs.append(f"truth: {type(ex).__name__}: {ex}"[:160])
        b["n_adsb"] += n_other
    ant = tuple(frame.ant) if frame is not None else None
    meta = b.setdefault("meta", {})
    try:
        for t, d in window(col, TRACKS, t0, t1, proj143 or TRACK_PROJECTION):
            for tid, r in track_rows(t, d, ant):
                b["tracks"][(round(t, 3), tid)] = (tid, r)
            for tid, m in track_meta(d).items():                            # newest truth_match per track wins
                prev = meta.get(tid)
                if prev is None or t >= prev.get("t", -1.0):
                    meta[tid] = {**m, "t": float(t)}
    except Exception as ex:
        errs.append(f"tracks: {type(ex).__name__}: {ex}"[:160])
    otm = b.setdefault("obs_tm", {})
    try:
        for t, d in window(col, DWELL_WITH_OBS, t0, t1, OBS_PROJECTION):
            for i, r in enumerate(obs_rows(t, d)):
                k = (round(t, 3), i, r[1], r[3])
                b["obs"][k] = r[:7]
                if r[7] is not None or r[8] is not None:
                    otm[k] = (r[7], r[8])                                       # (truth_target_id, truth_match_type)
    except Exception as ex:
        errs.append(f"obs: {type(ex).__name__}: {ex}"[:160])
    return errs


def _fill(col, frame, b: dict, lo: float, t_now: float, proj143: dict) -> list[str]:
    """Chunked ingest of [lo, t_now] into the buffer.  First tick: the newest CHUNK_S only (a foothold within ~1 s on
    MRU91).  Later ticks: the forward page (hi - OVERLAP_S, t_now] FIRST, then older CHUNK_S chunks back to ``lo`` while
    INGEST_BUDGET_S lasts (an in-process fake covers the whole span in one tick; a slow unit fills the window over a few
    ticks — ``b["lo"] - lo`` is what is still missing).  A forward gap > RESET_GAP_S (idle tab, outage) restarts the
    buffer at the present.  Errors are returned, never raised; a failed chunk is retried next tick."""
    t_wall = time.monotonic()
    errs: list[str] = []
    if b["hi"] is not None and t_now - b["hi"] > RESET_GAP_S:
        b.update(_empty_rows())
    if b["hi"] is None:
        c0 = max(lo, t_now - CHUNK_S)
        errs = _ingest(col, frame, b, c0, t_now, proj143)
        if errs:
            return errs                                                    # no foothold yet: retry next tick
        b["lo"], b["hi"] = c0, t_now
    elif t_now > b["hi"] - OVERLAP_S:
        errs = _ingest(col, frame, b, max(b["hi"] - OVERLAP_S, lo), t_now, proj143)   # page forward first
        if not errs:
            b["hi"] = max(b["hi"], t_now)
    while not errs and b["lo"] > lo + 1e-6 and time.monotonic() - t_wall < INGEST_BUDGET_S:
        c0 = max(lo, b["lo"] - CHUNK_S)
        errs = _ingest(col, frame, b, c0, b["lo"], proj143)             # back-fill one older chunk
        if not errs:
            b["lo"] = c0
    return errs


def _prune(b: dict, keep_from: float) -> None:
    for k in ("truth", "tracks", "obs", "obs_tm"):
        b[k] = {kk: v for kk, v in b[k].items() if kk[0] >= keep_from}
    b["lo"] = max(b["lo"], keep_from) if b["lo"] is not None else None


def _roles_lists() -> tuple[tuple, tuple]:
    """The Data-source page's MAVLink id assignment (target ids, interceptor ids); empty when unassigned."""
    fn = getattr(D, "role_assignment", None)
    try:
        r = fn() if callable(fn) else {}
    except Exception:
        r = {}
    return tuple(r.get("target") or ()), tuple(r.get("interceptor") or ())


UNASSIGNED = "unassigned"


def _patterns() -> tuple[str, str]:
    """(target pattern, interceptor pattern) from the session — the Data-source page's auto-assign patterns."""
    fn = getattr(D, "_roles_from_state", None)
    try:
        if callable(fn):
            _, _, tp, ip = fn()
            return str(tp), str(ip)
    except Exception:
        pass
    return D.TGT_PATTERN_DEFAULT, D.ITC_PATTERN_DEFAULT


def feed_role(tid: str) -> str:
    """'target' | 'interceptor' | 'unassigned' for one MAVLink target_id.

    D.role_of NEVER returns "unassigned" (its built-in rule falls through to "interceptor"), so a third drone
    whose id matches neither auto-assign pattern used to be merged into the interceptor truth.  Here: an id in
    an explicit Data-source list keeps that role; an id matching either pattern gets D.role_of's answer;
    anything else is UNASSIGNED — shown on the map, never graded, never given a role.
    """
    tg, ic = _roles_lists()
    s_ = str(tid)
    if s_ in set(map(str, tg)):
        return "target"
    if s_ in set(map(str, ic)):
        return "interceptor"
    tp, ip = _patterns()
    if D.pattern_matches(s_, tp) or D.pattern_matches(s_, ip):
        return D.role_of(s_)
    return UNASSIGNED


def _buffers_to_snapshot(b: dict, lo: float, t_now: float) -> tuple[np.ndarray, np.ndarray, list[tuple[str, np.ndarray]], dict, np.ndarray, dict, list[dict], bool, bool]:
    """Buffers -> (tgt, itc, other, tracks, obs, obs_meta, feeds, has_truth, has_any_truth).

    ``other`` = [(target_id, (n,7) TR rows)] for every MAVLink feed that is NOT assigned to a role and matches
    NEITHER auto-assign pattern (feed_role -> "unassigned"): the id is KEPT per feed (never merged with another
    drone, never given a role).  ``has_truth`` is the target / interceptor truth (what the grading gates read);
    ``has_any_truth`` is True when ANY MAVLink feed published a row in the window, unassigned ones included.
    """
    feeds: dict[str, list[pl.DataFrame]] = {"target": [], "interceptor": []}
    oth: dict[str, list[pl.DataFrame]] = {}
    raw = []
    by_tid: dict[str, list] = {}
    for (_, tid), (tid_, r) in b["truth"].items():
        by_tid.setdefault(tid_, []).append(r)
    for tid, rs in by_tid.items():
        df = pl.DataFrame(sorted(rs), schema=D.TRUTH_COLS, orient="row")
        role = feed_role(tid)
        v = df.filter(pl.col("validposition") == 1)
        raw.append({"name": tid, "role": role, "t": v["t_epoch"].to_numpy().astype(float), "lat": v["lat"].to_numpy(), "lon": v["lon"].to_numpy()})
        if role == UNASSIGNED:
            oth.setdefault(str(tid), []).append(D.clean_feed(df))
        else:
            feeds[role].append(D.clean_feed(df))
    tgt = D.merge_truth(feeds["target"], lo, t_now)
    itc = D.merge_truth(feeds["interceptor"], lo, t_now)
    other = [(k, a) for k, a in ((k, D.merge_truth(v, lo, t_now)) for k, v in sorted(oth.items())) if len(a)]
    trk: dict[int, list] = {}
    for (_, tid), (tid_, r) in b["tracks"].items():
        if lo <= r[0] <= t_now:
            trk.setdefault(int(tid_), []).append(r)
    tracks = {k: np.array(sorted(v), float) for k, v in trk.items()}
    obs, obs_meta = np.zeros((0, D.OBS_COLS)), empty_obs_meta(0)
    if b["obs"]:
        tg, ic = _roles_lists()
        otm = b.get("obs_tm") or {}
        keys = sorted(b["obs"], key=lambda k: b["obs"][k])
        tm = [otm.get(k) or (None, None) for k in keys]
        obs = np.array([(*b["obs"][k], MATCH_CODE[match_role(m[0], tg, ic)]) for k, m in zip(keys, tm)], float)
        i0 = int(np.searchsorted(obs[:, 0], t_now - D.OBS_KEEP_S, side="left"))
        i1 = int(np.searchsorted(obs[:, 0], t_now, side="right"))
        obs = obs[i0:i1]
        obs_meta = {"truth_target_id": np.array([m[0] for m in tm[i0:i1]], dtype=object),      # aligned with obs rows
                    "truth_match_type": np.array([m[1] for m in tm[i0:i1]], dtype=object)}
    has_truth = any(r["role"] in ("target", "interceptor") for r in raw)
    return tgt, itc, other, tracks, obs, obs_meta, [D.feed_status(r, t_now) for r in raw], has_truth, bool(by_tid)


def live_snapshot(t_now: float, hist_s: float) -> dict:
    s = st.session_state
    host, port, db, run = s.get("live_host"), s.get("live_port"), s.get("live_db"), s.get("live_run")
    out = {"ok": False, "err": None, "source": "live", "t_now": t_now, "ant": None, "stale": False,
           "label": f"LIVE · {host}:{port} · {run or '—'}", "tgt": np.zeros((0, 7)), "itc": np.zeros((0, 7)),
           "tgt_hist": np.zeros((0, 7)), "itc_hist": np.zeros((0, 7)), "tracks": {}, "obs": np.zeros((0, D.OBS_COLS)), "obs_meta": empty_obs_meta(0),
           "feeds": [], "tx": None, "tx_lla": None, "track_meta": {}, "unit": live_unit(s),
           "t_start": t_now - hist_s, "data_age": None, "anchored": False, "has_truth": False, "has_any_truth": False,
           "other": [], "n_adsb": 0, "truth_unplaced": False, "backfill_s": None}
    if not host or not run:
        out["err"] = "live source not configured (Data source page: host, port, run)"
        return out
    b = _buf(run)
    span = max(float(hist_s), float(s.get("spec_window", 120)))
    try:
        import corr_lib as C

        cl = _shared_client(host, port)
        col = cl[db][run]
        wall = float(t_now)
        # newest radar data: pin the clock to it when the run is stale (never re-scan a 9-day-old run)
        newest = newest_t(col)
        if newest is None:
            raise RuntimeError("run collection is empty")
        b["newest"] = newest
        out["data_age"] = max(0.0, time.time() - newest)
        if wall - newest > LIVE_STALE_S:
            n143 = newest_t(col, TRACKS)
            t_now = float(n143 if n143 is not None else newest)
            out["anchored"] = True
        out["t_now"] = t_now
        out["t_start"] = t_now - hist_s
        ant, tx, proj143 = _run_consts(col, b, s)                          # once per run (buffer + session state)
        frame = C.EnuFrame(ant) if ant else None
        out["tx"] = tx
        lo = t_now - span
        errs = _fill(col, frame, b, lo, t_now, proj143)                    # chunked: forward page, then back-fill within budget
        _prune(b, t_now - HIST_CAP_S)
        tgt, itc, other, tracks, obs, obs_meta, feeds, has_truth, has_any = _buffers_to_snapshot(b, lo, t_now)
        unplaced = False
        if frame is None:   # no TRACKS document yet: is MAVLink truth already arriving? (cheap indexed find_one)
            unplaced = col.find_one({"block_type": AIR_TRAFFIC, "payload.source": "MAVLINK", TIME_KEY: _time_q(lo, t_now)},
                                    projection={"time_spec_float": 1}, max_time_ms=QUERY_MAX_MS) is not None
        out.update(ok=True, ant=ant, tgt=tgt, itc=itc, tgt_hist=tgt, itc_hist=itc, tracks=tracks, obs=obs, obs_meta=obs_meta,
                   feeds=feeds, has_truth=has_truth or unplaced, has_any_truth=has_any or unplaced, other=other,
                   n_adsb=int(b["n_adsb"]), truth_unplaced=unplaced, tx_lla=tx,
                   track_meta={k: v for k, v in (b.get("meta") or {}).items() if k in tracks},
                   backfill_s=float(max(0.0, b["lo"] - lo)) if b["lo"] is not None else float(span),
                   stale=bool(errs), err="; ".join(errs) if errs else None)   # a failed chunk: last-good rows stay, ok stays True
        b["last_ok_wall"] = time.time()
        s["last_snap"] = out
        return out
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:300]
        if b.get("hi") is not None and b.get("ant") is not None and time.time() - float(b.get("last_ok_wall") or 0.0) <= STALE_HOLD_S:
            # connection-level failure shortly after a good tick: render the filled buffer (ok, stale); past STALE_HOLD_S -> the
            # last-good snapshot with ok=False (the page's OFFLINE path), exactly as before
            try:
                lo = float(t_now) - span
                tgt, itc, other, tracks, obs, obs_meta, feeds, has_truth, has_any = _buffers_to_snapshot(b, lo, float(t_now))
                out.update(ok=True, stale=True, ant=tuple(b["ant"]), tx=b.get("tx"), tx_lla=b.get("tx"), tgt=tgt, itc=itc, tgt_hist=tgt, itc_hist=itc,
                           tracks=tracks, obs=obs, obs_meta=obs_meta, feeds=feeds, has_truth=has_truth,
                           has_any_truth=has_any, other=other, n_adsb=int(b["n_adsb"]),
                           track_meta={k: v for k, v in (b.get("meta") or {}).items() if k in tracks},
                           data_age=(max(0.0, time.time() - b["newest"]) if b.get("newest") else None),
                           backfill_s=float(max(0.0, b["lo"] - lo)) if b.get("lo") is not None else float(span))
                s["last_snap"] = out
                return out
            except Exception:
                pass
        last = s.get("last_snap")
        if last:
            stale = dict(last)
            stale.update(ok=False, err=out["err"], stale=True, t_now=last.get("t_now", t_now))
            return stale
        return out


def snapshot(t_now: float, hist_s: float) -> dict:
    return live_snapshot(t_now, hist_s) if D.is_live() else archive_snapshot(t_now, hist_s)
