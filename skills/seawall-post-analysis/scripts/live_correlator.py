#!/usr/bin/env python
"""LIVE MAVLink-truth vs VP-track correlation dashboard — any MRU.

Tails a run's mongo for AIR_TRAFFIC (106) MAVLink truth and TRACKS (143),
auto-associates tracks to truth (or takes --tracks), and serves a
self-refreshing web dashboard: top-down EN, az/el/range/alt error (filter ±1σ band) + meas rate,
and measurement-rate — updating every couple of seconds during a flight.

Mongo host, run collection, truth target, gate, min-duration, and az-bias
handling are all configurable FROM THE DASHBOARD (control bar) — CLI flags
only set the initial values. Errors are RAW by default (bias measured and
shown, not removed).

  micromamba run -n sensorenv python live_correlator.py --host 10.191.28.205
  micromamba run -n sensorenv python live_correlator.py --host 10.146.28.205 --target 14550
  micromamba run -n sensorenv python live_correlator.py --replay flights/f.npz --speed 5

Dashboard: http://<this-box>:8898/   (use --web-port to change)
"""
import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corr_lib as C

STATE = {"truth": {}, "tracks": {}, "corr": {}, "status": "starting",
         "az_rm": 0.0, "az_est": 0.0}
LOCK = threading.Lock()
HIST_S = 900            # keep 15 min of history
TOFF = [0.0]            # replay time shift; displayed times = stamps - TOFF[0]
SAVE_REQ = []           # web button pushes a request; _digest services it
LAST_SAVE = [""]
SEEN_TARGETS = set()    # every MAVLink target_id seen, even when filtered out
FLIGHT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flights")

# Runtime config, editable from the dashboard control bar. Changing a key in
# RECONNECT_KEYS bumps "gen", which makes the poller drop buffers + reconnect
# (fresh backfill); the rest apply on the next digest.
CONFIG = {"host": "", "port": 27017, "db": "sensor_store", "run": "",
          "target": "", "interceptor": "", "tgt_track": "",
          "gate": 150.0, "min_dur": 10.0,
          "az_mode": "raw", "az_fixed": 0.0, "keep_adsb": False, "gen": 0}
RECONNECT_KEYS = ("host", "port", "db", "run", "target", "interceptor", "keep_adsb")

# Engagement mode (interceptor set): running closest-approach so far, per pair.
RUN_MIN = {}          # key -> (sep_m, t) ; reset on save/reset + reconnect
OBS_BUF = deque(maxlen=4000)   # raw DWELL_WITH_OBS rows (t, az_rad, el_rad, rng_m)
ANT_LL = [None]       # (lat, lon) once known — enables the satellite basemap
SAT_CACHE = {}        # quantized EN box -> satmap JSON payload
SAT_MAX_TILES = 48    # tile budget per composite (7x7 at the chosen zoom): wide views drop z until they fit
SAT_WORKERS = 8       # concurrent Esri tile GETs


def _satmap_payload(x0, x1, y0, y1):
    """Fetch+composite Esri World Imagery tiles for an EN box (m about the
    antenna), pre-dimmed so plot lines read on top. Lazy imports — heavy
    modules must never load at server startup (background exit-144 lesson)."""
    key = (round(x0, -2), round(x1, -2), round(y0, -2), round(y1, -2))
    if key in SAT_CACHE:
        return SAT_CACHE[key]
    if ANT_LL[0] is None:
        return {"err": "antenna unknown"}
    import io as _io
    import base64 as _b64
    import urllib.request as _rq
    from PIL import Image, ImageEnhance
    olat, olon = ANT_LL[0]
    _fr = C.EnuFrame((olat, olon, 0.0))                              # exact WGS-84 corners (the old 111 320 m/deg placed the tile ~0.36 % off in N)
    latmn, lonmn, _ = _fr.lla(x0, y0, 0.0)
    latmx, lonmx, _ = _fr.lla(x1, y1, 0.0)

    def d2n(lat, lon, z):
        n = 2 ** z
        xr = (lon + 180.0) / 360.0 * n
        yr = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return xr, yr

    def n2d(x, y, z):
        n = 2 ** z
        return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))),
                x / n * 360.0 - 180.0)
    z = 18   # <= 1.2 m/px: z18 (0.6 m/px at 34° N) for boxes up to ~1.7 km; z drops until the box needs <= SAT_MAX_TILES tiles (wide views)
    while z > 10:
        xa_, ya_ = d2n(latmx, lonmn, z)
        xb_, yb_ = d2n(latmn, lonmx, z)
        xa, xb = int(min(xa_, xb_)), int(max(xa_, xb_))
        ya, yb = int(min(ya_, yb_)), int(max(ya_, yb_))
        if (xb - xa + 1) * (yb - ya + 1) <= SAT_MAX_TILES:
            break
        z -= 1
    im = Image.new("RGB", ((xb - xa + 1) * 256, (yb - ya + 1) * 256), (40, 40, 40))
    got = 0
    from concurrent.futures import ThreadPoolExecutor

    def _get(xy):
        xt, yt = xy
        try:
            url = (f"https://server.arcgisonline.com/ArcGIS/rest/services/"
                   f"World_Imagery/MapServer/tile/{z}/{yt}/{xt}")
            return xy, Image.open(_io.BytesIO(_rq.urlopen(url, timeout=6).read()))
        except Exception:
            return xy, None
    coords = [(xt, yt) for xt in range(xa, xb + 1) for yt in range(ya, yb + 1)]
    with ThreadPoolExecutor(max_workers=SAT_WORKERS) as ex:   # the tiles used to be fetched one after another (~3 s for 30): the whole box lands in well under a second
        for (xt, yt), tile in ex.map(_get, coords):
            if tile is not None:
                im.paste(tile, ((xt - xa) * 256, (yt - ya) * 256))
                got += 1
    if not got:
        return {"err": "no tiles (offline?)"}
    im = ImageEnhance.Brightness(im).enhance(0.38)          # dim harder -> lines pop
    lt, lolf = n2d(xa, ya, z)
    lb, lort = n2d(xb + 1, yb + 1, z)
    buf = _io.BytesIO()
    im.save(buf, format="JPEG", quality=72)
    out = {"img": "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode(),
           "x0": _fr.enu(lt, lolf, 0.0)[0], "x1": _fr.enu(lb, lort, 0.0)[0],           # composite extents through the same exact frame as the corners
           "y0": _fr.enu(lb, lolf, 0.0)[1], "y1": _fr.enu(lt, lolf, 0.0)[1]}
    if len(SAT_CACHE) > 24:
        SAT_CACHE.clear()
    SAT_CACHE[key] = out
    return out


def _subs(spec):
    """Filter spec -> list of substrings (comma-separated, any-match). Lets one
    filter cover BOTH the aliased and un-aliased id forms, e.g.
    '14550,mavlink_1' matches mav14550_1_1 AND mavlink_1_2 (alias wiped)."""
    return [x.strip() for x in str(spec or "").split(",") if x.strip()]


def _match(tgt, spec):
    return any(s in tgt for s in _subs(spec))


def _want_truth(tgt, cfg):
    """Ingest filter: engagement mode keeps BOTH feeds; else the target filter."""
    if cfg.get("interceptor") and _match(tgt, cfg["interceptor"]):
        return True
    return (not cfg["target"]) or _match(tgt, cfg["target"])


def cfg_get():
    with LOCK:
        return dict(CONFIG)


def cfg_set(updates):
    with LOCK:
        bump = any(k in RECONNECT_KEYS and CONFIG.get(k) != v
                   for k, v in updates.items())
        CONFIG.update(updates)
        if bump:
            CONFIG["gen"] += 1
        return CONFIG["gen"]


def newest_run(db):
    spans = C.run_spans(db)
    return max(spans, key=lambda s: s[1])[2] if spans else None


def list_runs():
    """Run collections on the configured host, newest-first, with friendly
    names where the runs collection provides them. Cached for 10 s."""
    cfg = cfg_get()
    now = time.time()
    cache = getattr(list_runs, "_cache", None)
    if cache and cache[0] == cfg["host"] and now - cache[1] < 10:
        return cache[2]
    out = []
    try:
        db = C.connect(cfg["host"], cfg["port"], timeout_ms=4000, db=cfg["db"])
        names = C.run_names(db)
        for a, b, name in sorted(C.run_spans(db), key=lambda s: -s[1]):
            out.append({"id": name, "name": names.get(name, ""),
                        "last": datetime.fromtimestamp(b, tz=C.LOCAL_TZ
                                                       ).strftime("%m-%d %H:%M")})
    except Exception as e:
        out = [{"id": "", "name": f"error: {e!r}", "last": ""}]
    list_runs._cache = (cfg["host"], now, out)
    return out


# poller track-row layout (13): t,E,N,U,sE,sN,sU,lu,state,vE,vN,vU,assoc
# bulk npz layout (13):         t,E,N,U,vE,vN,vU,sE,sN,sU,assoc,state,lu
ROW2BULK = None   # built inline in _save_and_reset


def _save_and_reset(truth_raw, trk, label):
    """Dump buffers to a bulk-format npz (usable by post_correlate --npz and
    --replay), then clear them for the next flight."""
    os.makedirs(FLIGHT_DIR, exist_ok=True)
    stamp = datetime.fromtimestamp(time.time() - TOFF[0],
                                   tz=C.LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(FLIGHT_DIR, f"flight_{stamp}.npz")
    arrs = {}
    for tgt, rows in truth_raw.items():
        if len(rows) >= 3:
            A = np.array(sorted(rows))
            A[:, 0] -= TOFF[0]                      # store original data times
            arrs[f"mav_{tgt}"] = A
    for tid, dq in trk.items():
        A = np.array(sorted(dq))
        if not len(A):
            continue
        B = np.zeros((len(A), 13))
        B[:, 0] = A[:, 0] - TOFF[0]
        B[:, 1:4] = A[:, 1:4]                       # E,N,U
        B[:, 7:10] = A[:, 4:7]                      # sigmas
        B[:, 12] = A[:, 7] - TOFF[0]                # last_update_time
        if A.shape[1] > 8:
            B[:, 11] = A[:, 8]                      # track_state
        if A.shape[1] > 12:
            B[:, 4:7] = A[:, 9:12]                  # filter velocities
            B[:, 10] = A[:, 12]                     # total_associations
        arrs[f"trk_{tid}"] = B
    if arrs:
        np.savez_compressed(path, **arrs)
        LAST_SAVE[0] = os.path.basename(path)
    else:
        LAST_SAVE[0] = "(nothing to save)"
    truth_raw.clear()
    trk.clear()
    RUN_MIN.clear()


def _merge_streams(truth_raw, sub, invert=False):
    """Union of raw truth streams whose id contains sub (or doesn't, invert=True),
    deduped by rounded timestamp — duplicate MAVLink ids for one drone carry
    complementary gaps (mav14551_2_0/_1/_34 lesson), so the merge recovers samples.
    clean_truth then strips teleports and frozen-telemetry repeats."""
    rows = {}
    for tgt, rr in truth_raw.items():
        hit = _match(tgt, sub)
        if hit == invert:
            continue
        for r in rr:
            rows.setdefault(round(r[0], 1), r)
    return C.clean_truth(list(rows.values())) if len(rows) >= 3 else np.zeros((0, 8))


def _pair_series(A, B, t1, span=600.0, max_gap=3.0):
    """1 Hz separation + closing-speed series between two (t,E,N,U,..) arrays,
    NaN where either feed has no sample within max_gap (never interp across
    dropouts — that fabricates passes)."""
    t0 = max(A[0, 0], B[0, 0], t1 - span)
    if t1 - t0 < 3.0:
        return None
    g = np.arange(t0, t1 + 0.5, 1.0)

    def onto(M):
        P = np.column_stack([np.interp(g, M[:, 0], M[:, k]) for k in (1, 2, 3)])
        idx = np.clip(np.searchsorted(M[:, 0], g), 1, len(M) - 1)
        near = np.minimum(np.abs(M[idx, 0] - g), np.abs(M[idx - 1, 0] - g)) <= max_gap
        return P, near
    Pa, na = onto(A)
    Pb, nb = onto(B)
    ok = na & nb
    if ok.sum() < 3:
        return None
    sep = np.linalg.norm(Pa - Pb, axis=1)
    sep[~ok] = np.nan
    clo = np.full(len(g), np.nan)
    clo[1:-1] = -(sep[2:] - sep[:-2]) / (g[2:] - g[:-2])   # central diff, NaN-safe
    if len(clo) >= 5:                                       # light smoothing
        k = np.ones(3) / 3.0
        v = np.copy(clo)
        m = np.isnan(v)
        v[m] = 0.0
        w = np.convolve((~m).astype(float), k, "same")
        s = np.convolve(v, k, "same")
        clo = np.where(w > 0.5, s / np.maximum(w, 1e-9), np.nan)
    # instantaneous prediction at the newest common time
    last = np.where(ok)[0]
    nowd = {}
    if len(last):
        i = int(last[-1])
        r = Pb[i] - Pa[i]
        j = max(0, i - 4)
        vrel = ((Pb[i] - Pb[j]) - (Pa[i] - Pa[j])) / max(g[i] - g[j], 1e-6)
        vv = float(np.dot(vrel, vrel))
        rv = float(np.dot(r, vrel))
        nowd = {"t": float(g[i]), "sep": float(sep[i]),
                "closing": float(-rv / max(math.sqrt(max(np.dot(r, r), 1e-9)), 1e-6))}
        if vv > 0.25 and rv < 0:                            # actually closing
            t_go = -rv / vv
            miss = float(np.linalg.norm(r + vrel * t_go))
            if t_go < 120:
                nowd["t_go"] = round(t_go, 1)
                nowd["pred_miss"] = round(miss, 1)
    # running closest approach (persist beyond the history buffer)
    fin = np.where(np.isfinite(sep))[0]
    mn = mn_pos = None
    if len(fin):
        im = int(fin[int(np.nanargmin(sep[fin]))])
        mn = (float(sep[im]), float(g[im]))
        mid = (Pa[im] + Pb[im]) / 2.0            # CPA midpoint E,N,U on grid g
        mn_pos = [float(mid[0]), float(mid[1]), float(mid[2])]
    return {"g": g, "sep": sep, "clo": clo, "now": nowd,
            "min": mn, "min_pos": mn_pos}


def _best_corr_tid(corr, tracks, role):
    """Most-recent, tightest track correlated to the given merged stream."""
    cand = [(tid, b) for tid, b in corr.items() if b[0] == role]
    if not cand:
        return None
    def key(item):
        tid, b = item
        A = tracks.get(tid)
        return (-(A[-1, 0] if A is not None and len(A) else 0), b[2])
    return sorted(cand, key=key)[0][0]


def _engagement(cfg, now, truths, corr, tracks):
    """Live engagement metrics: interceptor truth vs target truth AND vs the
    target's radar track — separation, closing speed, predicted miss, and
    running closest approach. Returns dict for STATE['eng']."""
    Ti = truths.get("INTERCEPTOR")
    if Ti is None or len(Ti) < 3:
        return None
    out = {"pairs": {}, "roles": {}}
    int_tid = _best_corr_tid(corr, tracks, "INTERCEPTOR")
    tgt_tid = _best_corr_tid(corr, tracks, "TARGET")
    # Manual override — essential when the target's MAVLink truth freezes/dies
    # (8-27 failure mode): no truth -> no correlation -> no target role without this.
    try:
        forced = int(cfg.get("tgt_track") or 0)
    except (TypeError, ValueError):
        forced = 0
    if forced and forced in tracks:
        tgt_tid = forced
    if int_tid is not None:
        out["roles"][str(int_tid)] = "interceptor"
        out["int_trk"] = {"tid": int_tid, "med": round(corr[int_tid][2], 1)}
    if tgt_tid is not None:
        out["roles"][str(tgt_tid)] = "target"
    pairs = []
    Tt = truths.get("TARGET")
    if Tt is not None and len(Tt) >= 3:
        pairs.append(("tt", Tt, "target TRUTH"))
    if tgt_tid is not None:
        A = tracks[tgt_tid]
        if len(A) >= 3:
            pairs.append(("tk", A, f"target TRACK {tgt_tid}"))
    for key, B, name in pairs:
        # anchor on newest common DATA time (== now live; replay stamps at
        # speed>1 outrun the wall clock, so wall-now would miss the data)
        r = _pair_series(Ti, B, min(Ti[-1, 0], B[-1, 0]))
        if r is None:
            continue
        if r["min"] is not None:
            old = RUN_MIN.get(key)
            if old is None or r["min"][0] < old[0]:
                RUN_MIN[key] = (r["min"][0], r["min"][1], r.get("min_pos"))
        rm = RUN_MIN.get(key)
        out["pairs"][key] = {"name": name, "g": r["g"], "sep": r["sep"],
                             "clo": r["clo"], "now": r["now"],
                             "min": rm,
                             "min_pos": (rm[2] if rm and len(rm) > 2 else None)}
    return out          # even with no pairs yet: roles/chips show mode is armed


def _digest(cfg, now, truth_raw, trk, manual, label="",
            truth_raw_adsb=None, adsb_ids=None):
    """Trim history, clean truth, auto/manual-correlate, publish STATE."""
    if SAVE_REQ:
        SAVE_REQ.clear()
        _save_and_reset(truth_raw, trk, label)
        if truth_raw_adsb:
            truth_raw_adsb.clear()
    cutoff = now - HIST_S
    for raw in (truth_raw, truth_raw_adsb or {}):
        for tgt in list(raw):
            raw[tgt] = [r for r in raw[tgt] if r[0] > cutoff][-6000:]
            if not raw[tgt]:
                del raw[tgt]
    for tid in list(trk):
        while trk[tid] and trk[tid][0][0] < cutoff:
            trk[tid].popleft()
        if not trk[tid]:
            del trk[tid]
    eng_on = bool(cfg.get("interceptor"))
    if eng_on:
        # ENGAGEMENT MODE: two merged feeds — duplicate MAVLink ids per drone
        # are unioned so feed holes fill; correlation runs vs the merged streams.
        truths = {}
        Ti = _merge_streams(truth_raw, cfg["interceptor"])
        Tt = (_merge_streams(truth_raw, cfg["target"]) if cfg["target"]
              else _merge_streams(truth_raw, cfg["interceptor"], invert=True))
        if len(Tt) >= 3:
            truths["TARGET"] = Tt
        if len(Ti) >= 3:
            truths["INTERCEPTOR"] = Ti
    else:
        truths = {k: C.clean_truth(v) for k, v in truth_raw.items()}
        truths = {k: v for k, v in truths.items() if len(v) >= 3}
    adsb = {k: C.clean_truth(v) for k, v in (truth_raw_adsb or {}).items()}
    adsb = {k: v for k, v in adsb.items() if len(v) >= 3}
    corr, n_excl = {}, 0
    sorted_trk = {}
    for tid, dq in trk.items():
        A = np.array(dq)
        A = A[np.argsort(A[:, 0], kind="stable")]   # never trust arrival order
        sorted_trk[tid] = A
        if len(A) < (1 if manual else 8):
            continue
        if not manual and A[-1, 0] - A[0, 0] < cfg["min_dur"]:
            continue          # spurious few-second fragments: real tracks outlive this
        if not manual and adsb_ids and tid in adsb_ids:
            n_excl += 1
            continue
        best = None
        for tgt, T in truths.items():
            Mm = C.match_track(A, T)
            if len(Mm) < (1 if manual else 8):
                continue
            if manual:
                med = float(np.median(np.hypot(Mm[:, 11], Mm[:, 12])))
            else:
                med = C.gate_metric(Mm)   # moving-truth samples only: kills pad clutter
                if med is None:
                    continue
            if best is None or med < best[2]:
                best = (tgt, Mm, med)
        if not (best and (manual or best[2] < cfg["gate"])):
            continue
        if not manual and adsb:
            stolen = False
            for tgt, T in adsb.items():
                Mm = C.match_track(A, T)
                if len(Mm) < 8:
                    continue
                med = C.gate_metric(Mm)
                if med is not None and med < best[2]:
                    stolen = True
                    break
            if stolen:
                n_excl += 1
                continue
        corr[tid] = best
    est = C.az_bias_deg([b[1] for b in corr.values()]) if corr else 0.0
    if cfg["az_mode"] == "auto":
        rm = est
    elif cfg["az_mode"] == "fixed":
        rm = cfg["az_fixed"]
    else:
        rm = 0.0                                   # RAW: measured, not removed
    eng = _engagement(cfg, now, truths, corr, sorted_trk) if eng_on else None
    while OBS_BUF and OBS_BUF[0][0] < now - 420:   # raw obs: keep last 7 min
        OBS_BUF.popleft()
    with LOCK:
        STATE["obs"] = list(OBS_BUF)   # copy: snapshot() reads from HTTP threads
        STATE["truth"] = truths
        STATE["tracks"] = sorted_trk
        STATE["corr"] = corr
        STATE["eng"] = eng
        STATE["az_rm"] = rm
        STATE["az_est"] = est
        STATE["meta"] = {"label": label, "truth": len(truths), "tracks": len(trk),
                         "corr": len(corr), "excl": n_excl,
                         "bias_est": round(est, 2), "bias_rm": round(rm, 2),
                         "az_mode": cfg["az_mode"],
                         "saved": LAST_SAVE[0],
                         "targets": sorted(SEEN_TARGETS),
                         "cfg": {k: cfg[k] for k in
                                 ("host", "run", "target", "interceptor", "tgt_track", "gate",
                                  "min_dur", "az_mode", "az_fixed", "keep_adsb")}}
        STATE["status"] = (f"{label} · {len(truths)} truth stream(s) · "
                           f"{len(trk)} live track(s) · {len(corr)} correlated · "
                           f"{n_excl} ADS-B-excluded · az bias {est:+.2f}°"
                           + ("" if rm else " (RAW, not removed)"))


def replay_source(args):
    """Generator yielding (now, truth_rows, track_rows) from a recorded bulk npz,
    timestamps shifted to 'now'. Loops forever. Layouts match the mongo poller."""
    Z = np.load(args.replay)
    truths = {k[4:]: Z[k] for k in Z.files if k.startswith("mav_")}
    trks = {int(k[4:]): Z[k] for k in Z.files if k.startswith("trk_")}
    t_min = min(min(A[0, 0] for A in truths.values()),
                min(A[0, 0] for A in trks.values())) + args.replay_skip
    while True:
        offset = time.time() - t_min
        TOFF[0] = offset
        cursor = t_min
        t_max = max(A[-1, 0] for A in truths.values())
        yield "RESET", None, None     # clear state so loop wraps can't mix stamps
        while cursor < t_max:
            now = time.time()
            new_cursor = t_min + (now - offset - t_min) * args.speed
            tr_rows, tk_rows = {}, {}
            for tgt, A in truths.items():
                m = (A[:, 0] > cursor) & (A[:, 0] <= new_cursor)
                if m.any():
                    B = A[m].copy()
                    B[:, 0] = A[m][:, 0] + offset
                    if B.shape[1] == 7:            # legacy save: no vU column
                        B = np.column_stack([B, np.zeros(len(B))])
                    tr_rows[tgt] = B
            for tid, A in trks.items():
                m = (A[:, 0] > cursor) & (A[:, 0] <= new_cursor)
                if m.any():
                    # bulk -> poller row layout (velocities + assoc appended)
                    B = A[m][:, [0, 1, 2, 3, 7, 8, 9, 12, 11, 4, 5, 6, 10]].copy()
                    B[:, 0] += offset
                    B[:, 7] += offset              # last_update_time
                    tk_rows[tid] = B
            cursor = new_cursor
            yield now, tr_rows, tk_rows
            time.sleep(args.interval)
        # loop the recording


def poller(args):
    manual = set(int(x) for x in args.tracks.split(",")) if args.tracks else None
    truth_raw = {}                       # target -> list rows
    trk = {}                             # tid -> deque rows (13-col poller layout)
    if args.replay:
        src = replay_source(args)
        with LOCK:
            STATE["status"] = f"REPLAY {os.path.basename(args.replay)} x{args.speed}"
        for now, tr_rows, tk_rows in src:
            try:
                cfg = cfg_get()
                if now == "RESET":
                    truth_raw.clear()
                    trk.clear()
                    continue
                for tgt, B in tr_rows.items():
                    SEEN_TARGETS.add(tgt)
                    if not _want_truth(tgt, cfg):
                        continue
                    truth_raw.setdefault(tgt, []).extend(map(tuple, B))
                for tid, B in tk_rows.items():
                    if manual and tid not in manual:
                        continue
                    trk.setdefault(tid, deque(maxlen=4000)).extend(map(tuple, B))
                _digest(cfg, now, truth_raw, trk, manual,
                        label=f"REPLAY x{args.speed}")
            except Exception as e:
                with LOCK:
                    STATE["status"] = f"replay error: {e!r}"
        return

    while True:                          # reconnect loop: one pass per config gen
        cfg = cfg_get()
        gen = cfg["gen"]
        truth_raw.clear()
        trk.clear()
        RUN_MIN.clear()                  # fresh closest-approach bookkeeping
        truth_raw_adsb = {}   # positional ADS-B exclusion; radar class untrusted
        try:
            db = C.connect(cfg["host"], cfg["port"], db=cfg["db"])
            run = cfg["run"] or newest_run(db)
            if not run:
                raise RuntimeError("no run_* collections found")
            col = db[run]
            ant = C.antenna_origin(col)
            if ant is None:
                raise RuntimeError(f"{run} has no TRACKS yet")
            ANT_LL[0] = (ant[0], ant[1])
            frame = C.EnuFrame(ant)
        except Exception as e:
            if "ServerSelectionTimeoutError" in type(e).__name__:
                msg = (f"can't reach mongo at {cfg['host']}:{cfg['port']} — "
                       "unit offline / not on network? (no auth needed; "
                       "retrying — switch host above to use another MRU)")
            else:
                msg = f"connect error: {e!r} (retrying)"
            with LOCK:
                STATE["status"] = msg
            for _ in range(6):
                time.sleep(0.5)
                if cfg_get()["gen"] != gen:
                    break
            continue
        last_t = time.time() - args.backfill
        with LOCK:
            STATE["status"] = (f"watching {cfg['host']} {run} "
                               f"(antenna {ant[0]:.5f},{ant[1]:.5f})")
        while cfg_get()["gen"] == gen:
            cfg = cfg_get()
            try:
                now = time.time()
                for d in col.find({"block_type": C.AIR_TRAFFIC,
                                   "time_spec_float": {"$gt": last_t}},
                                  projection={"time_spec_float": 1, "payload": 1}):
                    pl = d.get("payload") or []
                    pl = [pl] if isinstance(pl, dict) else pl
                    for p in pl:
                        if not p.get("validposition"):
                            continue
                        src = p.get("source")
                        if src == "ADS-B":
                            if cfg["keep_adsb"]:
                                continue
                            E, N, U = frame.enu(p["lat"], p["lon"],
                                                C.mavlink_alt_hae_m(p.get("altitude")))
                            if math.hypot(E, N) > 12_000:   # far traffic can't steal
                                continue
                            truth_raw_adsb.setdefault(
                                "adsb:" + str(p.get("target_id") or "?"), []).append(
                                (d["time_spec_float"], E, N, U, p.get("speed_mps") or 0,
                                 p.get("velocity_n_mps") or 0,
                                 p.get("velocity_e_mps") or 0))
                            continue
                        if src != "MAVLINK":
                            continue
                        tgt = p.get("target_id") or "?"
                        SEEN_TARGETS.add(tgt)
                        if not _want_truth(tgt, cfg):
                            continue
                        E, N, U = frame.enu(p["lat"], p["lon"],
                                            C.mavlink_alt_hae_m(p.get("altitude")))
                        if abs(E) > 50_000 or abs(N) > 50_000:
                            continue
                        # U is now m HAE (track frame); vertical_speed stays wire
                        # ft/min in col 7 — vu_scale() converts it to m/s.
                        truth_raw.setdefault(tgt, []).append(
                            (d["time_spec_float"], E, N, U, p.get("speed_mps") or 0,
                             p.get("velocity_n_mps") or 0,
                             p.get("velocity_e_mps") or 0,
                             p.get("vertical_speed") or 0))
                for d in col.find({"block_type": C.TRACKS,
                                   "time_spec_float": {"$gt": last_t}},
                                  projection={"time_spec_float": 1, "payload": 1}):
                    pl = d.get("payload") or []
                    pl = [pl] if isinstance(pl, dict) else pl
                    for p in pl:
                        tid = p.get("track_id")
                        if manual and tid not in manual:
                            continue
                        x = p.get("x_state")
                        if x is None:
                            continue
                        P = p.get("p_cov")
                        sE = sN = sU = float("nan")
                        if P is not None:
                            P = np.asarray(P, float).reshape(6, 6)
                            sE, sN, sU = (math.sqrt(max(P[1, 1], 0)),
                                          math.sqrt(max(P[0, 0], 0)),
                                          math.sqrt(max(P[2, 2], 0)))
                        lu = p.get("last_update_time")
                        lu = (lu["full_sec"] + lu["frac_sec"]
                              if isinstance(lu, dict) else 0.0)
                        vE = x[4] if len(x) > 4 else 0.0
                        vN = x[3] if len(x) > 3 else 0.0
                        vU = -x[5] if len(x) > 5 else 0.0
                        trk.setdefault(tid, deque(maxlen=4000)).append(
                            (d["time_spec_float"], x[1], x[0], -x[2], sE, sN, sU,
                             lu, p.get("track_state") or 0, vE, vN, vU,
                             p.get("total_associations") or 0))
                for d in col.find({"block_type": C.DWELL_WITH_OBS,
                                   "time_spec_float": {"$gt": last_t}},
                                  projection={"time_spec_float": 1,
                                              "payload.observations.az_rad": 1,
                                              "payload.observations.el_rad": 1,
                                              "payload.observations.amb_rng_km": 1}):
                    pl = d.get("payload") or []
                    pl = [pl] if isinstance(pl, dict) else pl
                    for p in pl:
                        if not isinstance(p, dict):
                            continue
                        for o in (p.get("observations") or []):
                            if not isinstance(o, dict):
                                continue
                            az = o.get("az_rad")
                            rk = o.get("amb_rng_km")
                            if az is None or rk is None:
                                continue
                            OBS_BUF.append((d["time_spec_float"], az,
                                            o.get("el_rad") or 0.0, rk * 1000.0))
                last_t = max(last_t, now - 2)
                _digest(cfg, now, truth_raw, trk, manual, label=run,
                        truth_raw_adsb=truth_raw_adsb)
            except Exception as e:  # keep the dashboard alive through hiccups
                with LOCK:
                    STATE["status"] = f"poller error: {e!r} (retrying)"
            time.sleep(args.interval)


STATE_NAMES = {0: "?", 1: "tentative", 2: "confirmed", 3: "dropped"}


def _gap_null(t, cols, gap):
    """Insert None at time gaps > gap so plotly breaks the line instead of
    drawing a chord (stale-stripped truth, handovers, replay loop wraps)."""
    out = [[] for _ in range(len(cols) + 1)]
    prev = None
    for i, tv in enumerate(t):
        if prev is not None and tv - prev > gap:
            for o in out:
                o.append(None)
        out[0].append(tv)
        for j, c in enumerate(cols):
            out[0 + j + 1].append(c[i])
        prev = tv
    return out


def snapshot():
    with LOCK:
        truths, corr, status = (dict(STATE["truth"]), dict(STATE["corr"]),
                                STATE["status"])
        az_rm = STATE["az_rm"]
        tracks = dict(STATE["tracks"])
        meta = dict(STATE.get("meta") or {})
        eng = STATE.get("eng")
        obs = STATE.get("obs") or []
    toff = TOFF[0]     # replay: display original recording times; live: 0
    sh = lambda L: [None if x is None else round(x - toff, 3) for x in L]
    meta["dtime"] = datetime.fromtimestamp(time.time() - toff,
                                           tz=C.LOCAL_TZ).strftime("%H:%M:%S")
    # cfg/targets straight from CONFIG so the control bar seeds even when the
    # poller has never digested (e.g. initial host down — the UI is the fix)
    cfg = cfg_get()
    meta["cfg"] = {k: cfg[k] for k in ("host", "run", "target", "interceptor", "tgt_track",
                                       "gate", "min_dur", "az_mode", "az_fixed",
                                       "keep_adsb")}
    meta["targets"] = sorted(SEEN_TARGETS)
    meta.setdefault("az_mode", cfg["az_mode"])
    def _dec(A, cap):
        """Stride-decimate long series — keeps redraws snappy late in a flight."""
        if len(A) <= cap:
            return A
        return A[np.linspace(0, len(A) - 1, cap).astype(int)]

    out = {"status": status, "az_bias": az_rm, "meta": meta,
           "truth": {}, "corr": {}, "rate": {}}
    allE, allN = [], []
    for tgt, T in truths.items():
        Td = _dec(T, 800)
        t, E, N = _gap_null(Td[:, 0].tolist(),
                            [np.round(Td[:, 1], 1).tolist(),
                             np.round(Td[:, 2], 1).tolist()], gap=6.0)
        out["truth"][tgt] = {"t": sh(t), "E": E, "N": N}
        # frame the view on the RECENT action (last 4 min), not all history —
        # otherwise long transits shrink the engagement to a corner
        Tr = T[T[:, 0] >= T[-1, 0] - 240]
        allE += Tr[:, 1].tolist()
        allN += Tr[:, 2].tolist()
    if allE:   # padded & quantized (250 m) so it only reframes on real movement
        q = 250.0
        out["view"] = {
            "xr": [math.floor((min(allE) - q) / q) * q, math.ceil((max(allE) + q) / q) * q],
            "yr": [math.floor((min(allN) - q) / q) * q, math.ceil((max(allN) + q) / q) * q]}
    for tid, (tgt, Mm, med) in corr.items():
        Mm = _dec(Mm, 500)                    # visual decimation only
        ae = C.err_stack(Mm, az_bias=az_rm)   # RAW: no datum removal (matches report)
        A = tracks.get(tid)
        if A is not None and A.shape[1] > 8:
            idx = np.clip(np.searchsorted(A[:, 0], Mm[:, 0]), 0, len(A) - 1)
            states = [STATE_NAMES.get(int(s), "?") for s in A[idx, 8]]
        else:
            states = ["?"] * len(Mm)
        t, E, N, azE, elE, rgE, alE, sA, sE_, sR, sAl, fr, st = _gap_null(
            Mm[:, 0].tolist(),
            [np.round(Mm[:, 1], 1).tolist(), np.round(Mm[:, 2], 1).tolist(),
             np.round(ae["az_err"], 3).tolist(), np.round(ae["el_err"], 3).tolist(),
             np.round(ae["rng_err"], 1).tolist(), np.round(ae["alt_err"], 1).tolist(),
             np.round(ae["sig_az"], 3).tolist(), np.round(ae["sig_el"], 3).tolist(),
             np.round(ae["sig_rng"], 1).tolist(), np.round(ae["sig_alt"], 1).tolist(),
             [int(f) for f in Mm[:, 14]], states], gap=5.0)
        out["corr"][str(tid)] = {
            "truth": tgt, "med": round(med, 1), "t": sh(t), "E": E, "N": N,
            "az_err": azE, "el_err": elE, "rng_err": rgE, "alt_err": alE,
            "sig_az": sA, "sig_el": sE_, "sig_rng": sR, "sig_alt": sAl,
            "fresh": [f if f is not None else 0 for f in fr],
            "state": [s if s is not None else "" for s in st]}
    RATE_WIN, RATE_STEP = 30.0, 5.0
    for tid, A in tracks.items():
        if str(tid) not in out["corr"] and tid not in corr:
            continue
        ft = C.meas_times(A)
        if len(ft) < 2 or A[-1, 0] - A[0, 0] < RATE_STEP:
            continue
        grid = np.arange(A[0, 0], A[-1, 0] + RATE_STEP / 2, RATE_STEP)
        cnt = (np.searchsorted(ft, grid + RATE_WIN / 2)
               - np.searchsorted(ft, grid - RATE_WIN / 2))
        lo = np.maximum(grid - RATE_WIN / 2, A[0, 0])
        hi = np.minimum(grid + RATE_WIN / 2, A[-1, 0])
        hz = cnt / np.maximum(hi - lo, 1.0)
        out["rate"][str(tid)] = {"t": sh(grid.tolist()),
                                 "hz": np.round(hz, 2).tolist()}

    # ---------------- engagement payload (interceptor mode only) ----------------
    def _nn(a):     # NaN -> None for strict-JSON / plotly gap-breaks
        return [None if (x is None or (isinstance(x, float) and math.isnan(x)))
                else round(float(x), 2) for x in a]

    def _vec(M, name, role):
        """Latest position + short-baseline velocity (speed leader) for one entity."""
        if M is None or len(M) < 2:
            return None
        i = len(M) - 1
        j = i
        while j > 0 and M[i, 0] - M[j, 0] < 3.0:
            j -= 1
        dt = max(M[i, 0] - M[j, 0], 1e-3)
        vE, vN, vU = (M[i, 1] - M[j, 1]) / dt, (M[i, 2] - M[j, 2]) / dt, (M[i, 3] - M[j, 3]) / dt
        rng = math.hypot(M[i, 1], M[i, 2])
        vr = (M[i, 1] * vE + M[i, 2] * vN) / max(rng, 1.0)
        return {"name": name, "role": role, "t": round(M[i, 0] - toff, 1),
                "E": round(M[i, 1], 1), "N": round(M[i, 2], 1),
                "vE": round(vE, 1), "vN": round(vN, 1),
                "rng": round(rng, 1), "alt": round(M[i, 3], 1),
                "vr": round(vr, 1), "vU": round(vU, 1),
                "spd": round(math.hypot(vE, vN), 1)}

    def _gralt(M, gap=6.0, datum=0.0, secs=10.0):
        """Recent ground-range / altitude series with gap nulls — TRAILED like the
        map (default 90 s) so the profile doesn't accumulate spaghetti. Track
        vertical can be junk (-1000 m fragments) — drop implausible altitudes.
        Altitudes shifted to AGL (pad ground is BELOW the antenna reference)."""
        M = M[M[:, 0] >= M[-1, 0] - secs] if len(M) else M
        M = M[np.abs(M[:, 3]) < 450] if len(M) else M
        if len(M) and datum:
            M = M.copy(); M[:, 3] = M[:, 3] - datum
        t, rng, alt = _gap_null(M[:, 0].tolist(),
                                [np.round(np.hypot(M[:, 1], M[:, 2]), 1).tolist(),
                                 np.round(M[:, 3], 1).tolist()], gap=gap)
        return {"t": sh(t), "rng": rng, "alt": alt}

    if eng:
        E = {"pairs": {}, "roles": eng.get("roles", {}),
             "int_trk": eng.get("int_trk")}
        for key, p in eng["pairs"].items():
            E["pairs"][key] = {
                "name": p["name"], "t": sh([float(x) for x in p["g"]]),
                "sep": _nn(p["sep"]), "clo": _nn(p["clo"]), "now": p["now"],
                "min": ([round(p["min"][0], 1), round(p["min"][1] - toff, 1)]
                        if p.get("min") else None),
                "min_pos": ([round(float(x), 1) for x in p["min_pos"]]
                            if p.get("min_pos") else None)}
        # AGL datum: lowest truth samples (parked/landing sit at ground level)
        uall = [M[:, 3] for M in (truths.get("TARGET"), truths.get("INTERCEPTOR"))
                if M is not None and len(M)]
        g_datum = float(min(0.0, np.percentile(np.concatenate(uall), 1))) if uall else 0.0
        E["agl_datum"] = round(g_datum, 1)
        gr, vec = {}, []
        for nm, role in (("TARGET", "ttru"), ("INTERCEPTOR", "itru")):
            M = truths.get(nm)
            if M is not None and len(M) >= 2:
                gr[role] = {"name": "truth " + nm.lower(), **_gralt(M, datum=g_datum)}
                v = _vec(M, "truth " + nm.lower(), role)
                if v:
                    v["alt"] = round(v["alt"] - g_datum, 1)
                    vec.append(v)
        for tid_s, role in eng.get("roles", {}).items():
            A = tracks.get(int(tid_s))
            if A is not None and len(A) >= 2:
                rl = "ttrk" if role == "target" else "itrk"
                gr[rl] = {"name": f"track {tid_s} ({role})", **_gralt(A, datum=g_datum, secs=10.0)}
                v = _vec(A, f"track {tid_s} ({role})", rl)
                if v:
                    v["alt"] = round(v["alt"] - g_datum, 1)
                    vec.append(v)
        E["gralt"] = gr
        E["vec"] = vec
        # role tracks with no truth correlation (forced target track after the
        # target feed dies) still need to show on the map
        extra = {}
        for tid_s, role in eng.get("roles", {}).items():
            if tid_s in out["corr"]:
                continue
            A = tracks.get(int(tid_s))
            if A is None or len(A) < 2:
                continue
            t, Ee, Nn = _gap_null(A[:, 0].tolist(),
                                  [np.round(A[:, 1], 1).tolist(),
                                   np.round(A[:, 2], 1).tolist()], gap=5.0)
            extra[tid_s] = {"role": role, "t": sh(t), "E": Ee, "N": Nn}
        E["extra_trk"] = extra
        # RAW OBS overlay: pre-tracker detections vs the TARGET truth stream
        Tt = truths.get("TARGET")
        if obs and Tt is not None and len(Tt) >= 3:
            orows = []
            for t, az, el, rng in obs:
                tru = C.interp_truth(Tt, t)
                if tru is None:
                    continue
                gr_t = math.hypot(tru[0], tru[1])          # truth is [E,N,U,...]
                az_t = math.atan2(tru[0], tru[1])
                el_t = math.atan2(tru[2], gr_t)
                slant_t = math.hypot(gr_t, tru[2])
                daz = math.degrees((az - az_t + math.pi) % (2 * math.pi) - math.pi)
                dele = math.degrees(el - el_t)
                drng = rng - slant_t
                # positive gate: NaNs fail the < tests and are dropped too
                if not (abs(daz) < 8.0 and abs(dele) < 8.0 and abs(drng) < 800.0):
                    continue
                orows.append((t, daz, dele, drng, rng * math.sin(el) - tru[2]))
            if orows:
                Ob = _dec(np.array(orows), 400)
                E["obs"] = {"t": sh(Ob[:, 0].tolist()),
                            "az": np.round(Ob[:, 1], 2).tolist(),
                            "el": np.round(Ob[:, 2], 2).tolist(),
                            "rng": np.round(Ob[:, 3], 2).tolist(),
                            "alt": np.round(Ob[:, 4], 2).tolist()}
        out["eng"] = E
    return out


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Live Track Correlator</title><script src="/plotly.min.js"></script>
<style>
:root{--ink:#14161a;--ink2:#5b6169;--line:#e3e5e8;--card:#fff;--bg:#f6f7f8;
 --blue:#2a78d6;--shadow:0 1px 2px rgba(20,22,26,.05),0 2px 8px rgba(20,22,26,.05)}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif;padding:1.1rem}
main{max-width:1220px;margin:0 auto}
h1{font-size:1.22rem;margin:.1rem 0 .6rem;letter-spacing:.01em}
#ctrl{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin:.3rem 0 .6rem;
 background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:.5rem .7rem;box-shadow:var(--shadow)}
#ctrl label{color:var(--ink2);font-size:.78rem;margin-left:.35rem;
 text-transform:uppercase;letter-spacing:.05em}
#ctrl input,#ctrl select{font:inherit;font-size:.9rem;padding:.2rem .35rem;
 border:1px solid var(--line);border-radius:7px;background:#fff}
#ctrl input:focus,#ctrl select:focus{outline:2px solid #bcd6f5;outline-offset:0}
#ctrl input[type=number]{width:4.5rem}
#host{width:9.5rem} #runsel{max-width:15rem}
#chips,#engrow{display:flex;flex-wrap:wrap;gap:.5rem;margin:.45rem 0 .25rem}
.chip{background:var(--card);border:1px solid var(--line);border-radius:9px;
 padding:.3rem .75rem;font-size:.78rem;color:var(--ink2);box-shadow:var(--shadow);
 text-transform:uppercase;letter-spacing:.04em}
.chip b{display:inline-block;color:var(--ink);font-size:1.06rem;margin-left:.35rem;
 font-variant-numeric:tabular-nums;text-transform:none;letter-spacing:0}
.chip small{font-size:.75rem;text-transform:none;letter-spacing:0}
#engrow .chip{border-left:3px solid var(--blue)}
#engrow .chip.warn{border-left-color:#e34948}
#engrow .chip.good{border-left-color:#0f9d58}
#mode{background:var(--blue);color:#fff;border-color:var(--blue)}
#mode b{color:#fff}
.hint{color:var(--ink2);font-size:.8rem;margin:.15rem 0 .7rem}
.cap{font-weight:600;margin:1.05rem 0 .25rem;font-size:.95rem}
.cap small{font-weight:400;color:var(--ink2)}
.plot{background:var(--card);border:1px solid var(--line);border-radius:10px;
 margin:.2rem 0 .7rem;padding:.25rem;box-shadow:var(--shadow)}
button{font:inherit;padding:.28rem .95rem;cursor:pointer;border:1px solid var(--line);
 border-radius:8px;background:var(--card);box-shadow:var(--shadow)}
button:hover{background:#eef1f4}
#apply{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:600}
</style></head><body><main>
<h1>Live MAVLink ↔ track correlation<span style="font-size:.7rem;color:#5b6169"> · build 0828b</span>
 <span style="float:right">
  <button id="save">💾 save &amp; reset flight</button>
  <button id="pause">⏸ pause</button>
 </span></h1>
<div id="ctrl">
 <label>host</label><input id="host" placeholder="10.1xx.28.205">
 <label>run</label><select id="runsel"><option value="">auto (newest)</option></select>
 <button id="runrefresh" title="refresh run list">⟳</button>
 <label>target</label><select id="tgtsel"><option value="">all MAVLink</option></select>
 <label>interceptor</label><select id="intsel" title="setting this enables engagement mode"><option value="">(none)</option></select>
 <label>tgt track</label><input id="tgttrk" type="number" style="width:5rem" placeholder="auto" title="force target track id (when target truth dies)">
 <label>gate m</label><input id="gate" type="number" step="10" min="10">
 <label>min dur s</label><input id="mindur" type="number" step="1" min="0">
 <label>az bias</label><select id="azmode">
   <option value="raw">RAW (not removed)</option>
   <option value="auto">auto-remove</option>
   <option value="fixed">fixed:</option></select>
 <input id="azfixed" type="number" step="0.1" style="display:none" title="deg to remove">
 <label><input id="keepadsb" type="checkbox" style="width:auto"> keep ADS-B</label>
 <button id="apply">apply</button>
</div>
<div id="chips">
 <span class="chip" id="mode">—</span>
 <span class="chip">correlated<b id="c-corr">0</b></span>
 <span class="chip">live tracks<b id="c-trk">0</b></span>
 <span class="chip">truth streams<b id="c-tru">0</b></span>
 <span class="chip">ADS-B excluded<b id="c-excl">0</b></span>
 <span class="chip">az bias measured<b id="c-bias">—</b></span>
 <span class="chip">data time<b id="c-dtime">—</b></span>
 <span class="chip" id="chip-saved" style="display:none">saved<b id="c-saved"></b></span>
</div>
<div class="hint">dots = radar measurement correlated into the track · plain line = coasting ·
 errors are RAW unless az bias is set to auto/fixed · click a legend entry to hide that track ·
 drag to zoom (time plots share one axis) · double-click to reset</div>
<div id="engchips" style="display:none">
 <div id="engrow" style="display:flex;flex-wrap:wrap;gap:.5rem;margin:.4rem 0 .2rem"></div>
</div>
<div class="cap">Top-down East/North <small>(satellite; red = target, blue = interceptor; solid = truth, dashed = track; trails: truth 30 s, tracks 10 s; arrows = 3 s speed leaders; fixed frame — pan/zoom manually)</small></div>
<div id="map" class="plot" style="height:540px"></div>
<div id="engsec" style="display:none">
 <div class="cap" id="engtscap">Engagement — separation &amp; closing speed
  <small>(red = target pair; solid = vs truth, dashed = vs radar track; ★ = closest so far)</small></div>
 <div class="hint" id="engtswait" style="display:none">— waiting for interceptor + target data before the separation panel draws —</div>
 <div id="engts" class="plot" style="height:480px"></div>
 <div class="cap">Ground range vs altitude <small>(altitude AGL — ground datum from lowest truth samples; arrows = speed leaders; fixed frame)</small></div>
 <div id="gralt" class="plot" style="height:420px"></div>
</div>
<div class="cap">Az error · El error · Measurement rate
 <small>(shared time axis; bands = filter ±1σ)</small></div>
<div id="tseries" class="plot" style="height:1100px"></div>
</main>
<script>
const COLORS=["#1baf7a","#4a3aa7","#e87ba4","#eda100","#008300","#e34948"];
const TRU=["#2a78d6","#eb6834","#008300"];
const CFG={responsive:true,displaylogo:false};
const BASE={paper_bgcolor:"#ffffff",plot_bgcolor:"#ffffff",font:{color:"#14161a",size:13},
 autosize:true,uirevision:"keep",hovermode:"closest",
 legend:{orientation:"h",y:1.01,yanchor:"bottom",bgcolor:"rgba(0,0,0,0)",font:{size:12}}};
const GRID="#e9ebee",INK2="#5b6169";
const ts=a=>a.map(x=>x==null?null:new Date(x*1000));
const colorMap={};let colorNext=0;
const colorFor=tid=>colorMap[tid]??(colorMap[tid]=COLORS[(colorNext++)%COLORS.length]);
const hex2rgb=h=>`rgb(${parseInt(h.slice(1,3),16)},${parseInt(h.slice(3,5),16)},${parseInt(h.slice(5,7),16)})`;
// WebGL for big traces: SVG re-render of thousands of nodes is THE lag source.
// Small traces (stars, notes) stay SVG — scattergl's symbol set is limited.
const gl=a=>a.map(t=>((t.x&&t.x.length>50&&!t.fill&&!(t.marker&&t.marker.symbol==="x-thin"))?{...t,type:"scattergl"}:t));
let lastPayload="",paused=false,viewLock=null,cfgSeeded=false,tickN=0;
let satImg=null,satBusy=false;
async function fetchSat(){
  if(!viewLock||satBusy)return;
  satBusy=true;
  try{
    // equal-aspect plots EXPAND the short axis to fill the div — fetch imagery
    // for the box the plot will actually SHOW, not just the data box, else the
    // expansion shows as blank bars beside the image
    const el=document.getElementById("map");
    const aw=Math.max(el.clientWidth-70,100),ah=Math.max(el.clientHeight-98,100);
    let [x0,x1]=viewLock.xr,[y0,y1]=viewLock.yr;
    const cx=(x0+x1)/2,cy=(y0+y1)/2;
    let sx=x1-x0,sy=y1-y0;
    if(sx/sy < aw/ah) sx=sy*aw/ah; else sy=sx*ah/aw;   // match plot aspect
    sx*=1.06; sy*=1.06;                                 // small safety margin
    const q=new URLSearchParams({x0:cx-sx/2,x1:cx+sx/2,y0:cy-sy/2,y1:cy+sy/2});
    const r=await (await fetch("/satmap?"+q)).json();
    if(r&&r.img)satImg=r;              // else: offline / antenna unknown -> plain grid
  }catch(e){}finally{satBusy=false;}
}
document.getElementById("save").onclick=async e=>{
  e.target.disabled=true;
  try{await fetch("/save_reset");}finally{
    setTimeout(()=>{e.target.disabled=false;lastPayload="";},1200);}
};
document.getElementById("pause").onclick=e=>{
  paused=!paused;
  e.target.textContent=paused?"▶ resume":"⏸ pause";
  e.target.style.background=paused?"#eda100":"#f4f3f0";
};
document.getElementById("azmode").onchange=e=>{
  document.getElementById("azfixed").style.display=
    e.target.value==="fixed"?"":"none";};
async function loadRuns(){
  const sel=document.getElementById("runsel");
  const cur=sel.value;
  try{
    const rs=await (await fetch("/runs")).json();
    sel.innerHTML='<option value="">auto (newest)</option>'+rs.map(r=>
      `<option value="${r.id}">${r.name?r.name+" · ":""}${r.id.slice(0,12)}… · ${r.last}</option>`).join("");
    sel.value=cur;
  }catch(e){}
}
document.getElementById("runrefresh").onclick=loadRuns;
document.getElementById("apply").onclick=async()=>{
  const q=new URLSearchParams({
    host:document.getElementById("host").value.trim(),
    run:document.getElementById("runsel").value,
    target:document.getElementById("tgtsel").value,
    interceptor:document.getElementById("intsel").value,
    tgt_track:document.getElementById("tgttrk").value,
    gate:document.getElementById("gate").value||150,
    min_dur:document.getElementById("mindur").value||10,
    az_mode:document.getElementById("azmode").value,
    az_fixed:document.getElementById("azfixed").value||0,
    keep_adsb:document.getElementById("keepadsb").checked?1:0});
  await fetch("/set?"+q.toString());
  lastPayload="";viewLock=null;
};
function seedCtrl(cfg,targets){
  const tsel=document.getElementById("tgtsel");
  const cur=tsel.value||cfg.target||"";
  const opts=['<option value="">all MAVLink</option>'].concat(
    (targets||[]).map(t=>`<option value="${t}">${t}</option>`));
  if(cur&&!(targets||[]).includes(cur))opts.push(`<option value="${cur}">${cur}</option>`);
  tsel.innerHTML=opts.join("");tsel.value=cur;
  const isel=document.getElementById("intsel");
  const icur=isel.value||cfg.interceptor||"";
  const iopts=['<option value="">(none)</option>'].concat(
    (targets||[]).map(t=>`<option value="${t}">${t}</option>`));
  if(icur&&!(targets||[]).includes(icur))iopts.push(`<option value="${icur}">${icur}</option>`);
  isel.innerHTML=iopts.join("");isel.value=icur;
  if(cfgSeeded)return;
  cfgSeeded=true;
  document.getElementById("host").value=cfg.host||"";
  document.getElementById("runsel").value=cfg.run||"";
  document.getElementById("tgttrk").value=cfg.tgt_track||"";
  document.getElementById("gate").value=cfg.gate;
  document.getElementById("mindur").value=cfg.min_dur;
  document.getElementById("azmode").value=cfg.az_mode;
  document.getElementById("azfixed").value=cfg.az_fixed;
  document.getElementById("azfixed").style.display=cfg.az_mode==="fixed"?"":"none";
  document.getElementById("keepadsb").checked=!!cfg.keep_adsb;
  loadRuns();
}
const pm=(e,s,sign)=>e.map((v,i)=>(v==null||s[i]==null)?null:v+sign*s[i]);
function band(t,e,s,c,yax,grp){return{x:t.concat([...t].reverse()),
 y:pm(e,s,1).concat(pm(e,s,-1).reverse()),
 fill:"toself",fillcolor:c.replace(")",",0.22)").replace("rgb","rgba"),
 line:{width:0},hoverinfo:"skip",showlegend:false,yaxis:yax,legendgroup:grp};}
function hovertxt(c){return c.t.map((v,i)=>v==null?"":
 (c.fresh[i]? "MEASUREMENT":"coasting")+(c.state[i]?" · "+c.state[i]:""));}
async function tick(){
 if(paused) return;
 try{
  const raw=await (await fetch("/data.json")).text();
  if(raw===lastPayload) return;
  lastPayload=raw;
  const d=JSON.parse(raw);
  const m=d.meta||{};
  if(m.cfg)seedCtrl(m.cfg,m.targets);
  document.getElementById("mode").textContent=m.label||d.status;
  document.getElementById("c-corr").textContent=m.corr??0;
  document.getElementById("c-trk").textContent=m.tracks??0;
  document.getElementById("c-tru").textContent=m.truth??0;
  document.getElementById("c-excl").textContent=m.excl??0;
  document.getElementById("c-bias").textContent=(m.bias_est??0).toFixed(2)+"°"+
    (m.az_mode==="raw"?" (kept)":" (−"+((m.bias_rm??0).toFixed(2))+"°)");
  document.getElementById("c-dtime").textContent=m.dtime||"—";
  if(m.saved){document.getElementById("chip-saved").style.display="";
              document.getElementById("c-saved").textContent=m.saved;}

  // ---------- EN map ----------
  // TEAM colors: red = target, blue = interceptor. SOLID = truth, DASHED = track.
  const eng=d.eng||null;
  const ROLE={ttru:"#e34948",itru:"#2a78d6",ttrk:"#e34948",itrk:"#2a78d6"};
  const truColor=(tgt,i)=>eng?(tgt==="INTERCEPTOR"?ROLE.itru:ROLE.ttru):TRU[i%TRU.length];
  const trkColor=(tid,c)=>{
    if(eng)return (c&&c.truth==="INTERCEPTOR")?ROLE.itrk:ROLE.ttrk;   // team by correlated stream
    return colorFor(tid);};
  // map trails: only recent data so the scope stays readable (tracks 10 s, truth 30 s)
  const trail=(o,fields,secs)=>{
    const t=o.t||[];let tm=null;
    for(let i=t.length-1;i>=0;i--)if(t[i]!=null){tm=t[i];break;}
    if(tm==null)return o;
    const keep=t.map(x=>x!=null&&x>=tm-secs);
    const r={};fields.forEach(f=>{r[f]=(o[f]||[]).filter((_,i)=>keep[i]);});
    return r;};
  let map=[];
  Object.entries(d.truth).forEach(([tgt,T],i)=>{
    const s=eng?trail(T,["t","E","N"],30):T;
    map.push({uid:"tru-"+tgt,x:s.E,y:s.N,mode:"lines",name:"truth "+tgt,
              line:{color:truColor(tgt,i),width:3.2},
              hovertemplate:"E %{x:.0f} N %{y:.0f}<extra>"+tgt+"</extra>"});});
  Object.entries(d.corr).forEach(([tid,c])=>{
    if(eng&&c.truth==="INTERCEPTOR")return;   // interceptor TRACK hidden on the map (metrics still shown)
    const col=trkColor(tid,c);
    const role=eng&&eng.roles&&eng.roles[tid]?" · "+eng.roles[tid]:"";
    const s=eng?trail(c,["t","E","N"],10):c;
    map.push({uid:"trk-"+tid,x:s.E,y:s.N,mode:"lines",
              name:`track ${tid}${role} (${c.med} m)`,legendgroup:"g"+tid,
              line:{color:col,width:2.6,dash:eng?"dash":undefined},
              hovertemplate:"E %{x:.0f} N %{y:.0f}<extra>track "+tid+"</extra>"});});
  Object.entries((eng&&eng.extra_trk)||{}).forEach(([tid,c])=>{
    if(c.role!=="target")return;              // only the target track gets drawn
    const s=trail(c,["t","E","N"],10);
    map.push({uid:"xtrk-"+tid,x:s.E,y:s.N,mode:"lines",
      name:`track ${tid} · ${c.role} (forced)`,
      line:{color:ROLE.ttrk,width:2.6,dash:"dash"},
      hovertemplate:"E %{x:.0f} N %{y:.0f}<extra>track "+tid+"</extra>"});});
  // ★ running closest-approach midpoint per engagement pair
  Object.entries((d.eng&&d.eng.pairs)||{}).forEach(([k,p])=>{
    if(!p.min_pos||!p.min)return;
    map.push({uid:"cpa-"+k,x:[p.min_pos[0]],y:[p.min_pos[1]],mode:"markers+text",
      marker:{size:15,symbol:"star",color:"#111",line:{width:1,color:"#fff"}},
      text:["  CPA "+Math.round(p.min[0])+" m"],textposition:"top right",
      name:"closest so far",showlegend:false,
      hovertemplate:"CPA "+Math.round(p.min[0])+" m<br>E %{x:.0f} N %{y:.0f}<extra>"+(p.name||k)+"</extra>"});});
  // FIXED view whenever engagement mode is CONFIGURED (interceptor filter set) —
  // pre-computed from the 8-27 engagement footprint, applied even before data
  // flows so the frame never flashes/reframes as feeds come and go.
  const EVIEW={xr:[-300,4100],yr:[-800,1600]};
  const engCfg=!!(m.cfg&&m.cfg.interceptor);
  if(engCfg){if(JSON.stringify(viewLock)!==JSON.stringify(EVIEW)){viewLock=EVIEW;fetchSat();}}
  else if(d.view&&JSON.stringify(d.view)!==JSON.stringify(viewLock)){viewLock=d.view;fetchSat();}
  const v=viewLock||{};
  const LEAD=3;   // speed leader = 3 s of travel
  const mapAnn=(eng&&eng.vec?eng.vec:[]).filter(o=>o.spd>0.5&&o.role!=="itrk").map(o=>({
    x:o.E+o.vE*LEAD,y:o.N+o.vN*LEAD,ax:o.E,ay:o.N,
    xref:"x",yref:"y",axref:"x",ayref:"y",showarrow:true,
    arrowhead:2,arrowsize:1.2,arrowwidth:2.6,arrowcolor:ROLE[o.role],text:""}));
  const mapImgs=(satImg&&satImg.img)?[{source:satImg.img,xref:"x",yref:"y",
    x:satImg.x0,y:satImg.y1,sizex:satImg.x1-satImg.x0,sizey:satImg.y1-satImg.y0,
    xanchor:"left",yanchor:"top",sizing:"stretch",layer:"below",opacity:1}]:[];
  Plotly.react("map",gl(map),{...BASE,
    margin:{l:55,r:15,t:58,b:40},annotations:mapAnn,images:mapImgs,
    plot_bgcolor:mapImgs.length?"#20242a":"#ffffff",
    xaxis:{gridcolor:mapImgs.length?"rgba(255,255,255,0.18)":GRID,title:"East of antenna (m)",range:v.xr},
    yaxis:{gridcolor:mapImgs.length?"rgba(255,255,255,0.18)":GRID,title:"North of antenna (m)",range:v.yr,
           scaleanchor:"x",scaleratio:1}},CFG);

  // Heavy panels redraw every OTHER tick (map + chips stay live every tick) —
  // halves the periodic main-thread jank from full Plotly relayouts.
  tickN++;
  const heavy=(tickN%2===0)||tickN<3;
  if(heavy){
  // ---------- stacked time series: az / el / range / alt error + meas rate ----------
  // Fixed scales (shared with the post-process report) so panels read the same everywhere.
  const ESC={az:[3,1],el:[3,1],rng:[40,20],alt:[60,20]};
  const ECAP={az:12,el:12,rng:200,alt:200};       // auto-expand ceiling
  const emax={az:0,el:0,rng:0,alt:0};
  let tr=[];
  Object.entries(d.corr).forEach(([tid,c])=>{
    if(eng&&c.truth==="INTERCEPTOR")return;   // interceptor track NEVER plotted (chip only)
    [["az",c.az_err,c.sig_az],["el",c.el_err,c.sig_el],
     ["rng",c.rng_err,c.sig_rng],["alt",c.alt_err,c.sig_alt]].forEach(([k,e,s])=>{
      (e||[]).forEach((v,i)=>{if(v!=null){const m=Math.abs(v)+((s&&s[i])||0);
        if(m>emax[k])emax[k]=m;}});});
    const col=trkColor(tid,c),rc=hex2rgb(col),t=ts(c.t),g="g"+tid;
    const rows=[["az","y",c.az_err,c.sig_az,"°"],["el","y2",c.el_err,c.sig_el,"°"],
                ["range","y3",c.rng_err,c.sig_rng," m"],["alt","y4",c.alt_err,c.sig_alt," m"]];
    rows.forEach(([key,ay,err,sig,unit],ri)=>{
      tr.push({uid:key+"b-"+tid,...band(t,err,sig,rc,ay,g)});
      tr.push({uid:key+"-"+tid,x:t,y:err,mode:"lines+markers",yaxis:ay,
               name:`track ${tid}`,legendgroup:g,showlegend:(ri===0),
               line:{color:col,width:2.2},
               marker:{size:6,color:col,opacity:c.fresh},text:hovertxt(c),
               hovertemplate:"%{x|%H:%M:%S}<br>"+key+" err %{y:.2f}"+unit+"<br>%{text}<extra>"+tid+"</extra>"});
    });
    const r=d.rate[tid];
    if(r) tr.push({uid:"rate-"+tid,x:ts(r.t),y:r.hz,mode:"lines",yaxis:"y5",
             legendgroup:g,showlegend:false,line:{color:col,width:2.2,shape:"spline"},
             hovertemplate:"%{x|%H:%M:%S}<br>%{y:.2f} Hz<extra>"+tid+"</extra>"});});
  if(tr.length)   // legend note: what the shaded band means
    tr.push({x:[null],y:[null],mode:"markers",yaxis:"y",name:"filter ±1σ (shaded band)",
             marker:{size:11,symbol:"square",color:"rgba(120,120,120,0.35)"},showlegend:true});
  const tsAnn=[];
  if(!tr.length)tsAnn.push({x:0.5,y:0.92,xref:"paper",yref:"paper",showarrow:false,
    font:{size:13,color:INK2},
    text:eng?"no TARGET-track ↔ truth correlation yet (target feed down/frozen, or no track) — errors draw automatically once both exist":"no correlated tracks yet"});
  // RAW OBS overlay (engagement mode): pre-tracker detections vs TARGET truth.
  // x-thin markers stay SVG — gl() skips them (symbol unsupported in scattergl).
  if(eng&&eng.obs&&eng.obs.t&&eng.obs.t.length){
    const ot=ts(eng.obs.t);
    [["az","y",eng.obs.az],["el","y2",eng.obs.el],
     ["rng","y3",eng.obs.rng],["alt","y4",eng.obs.alt]].forEach(([k,ay,yv],i)=>{
      tr.push({uid:"obs-"+k,x:ot,y:yv,mode:"markers",yaxis:ay,name:"raw obs",
        legendgroup:"obs",showlegend:(i===0),hoverinfo:"skip",
        marker:{symbol:"x-thin",size:4,opacity:0.45,line:{width:1,color:"#5b6169"}}});
    });
  }
  const zero=y=>({type:"line",xref:"paper",x0:0,x1:1,yref:y,y0:0,y1:0,
                  line:{color:INK2,width:1,dash:"dash"}});
  const azTitle=(d.meta&&d.meta.az_mode!=="raw")?"azimuth error (deg, bias rm)":"azimuth error (deg)";
  // fixed scales by default; AUTO-EXPAND (round steps, capped) when data would clamp
  const nice=x=>{const e=Math.pow(10,Math.floor(Math.log10(x)));const f=x/e;
    return (f<=1?1:f<=2?2:f<=5?5:10)*e;};
  const lim=k=>{let L=ESC[k][0];
    if(emax[k]>L)L=Math.min(ECAP[k],nice(emax[k]*1.05));
    return [L,nice(L/3)];};
  const [Laz,Daz]=lim("az"),[Lel,Del]=lim("el"),[Lr,Dr]=lim("rng"),[La,Da]=lim("alt");
  Plotly.react("tseries",gl(tr),{...BASE,
    margin:{l:62,r:15,t:58,b:45},
    xaxis:{gridcolor:GRID,anchor:"y5",title:"time (PDT)"},
    yaxis :{gridcolor:GRID,domain:[0.82,1.0],  title:azTitle,        zeroline:false,range:[-Laz,Laz],dtick:Daz},
    yaxis2:{gridcolor:GRID,domain:[0.615,0.79],title:"elevation error (deg)",zeroline:false,range:[-Lel,Lel],dtick:Del},
    yaxis3:{gridcolor:GRID,domain:[0.41,0.585],title:"range error (m)",zeroline:false,range:[-Lr,Lr],dtick:Dr},
    yaxis4:{gridcolor:GRID,domain:[0.205,0.38],title:"altitude error (m)",zeroline:false,range:[-La,La],dtick:Da},
    yaxis5:{gridcolor:GRID,domain:[0,0.175],   title:"meas rate (Hz)",range:[0,2.3]},
    shapes:[zero("y"),zero("y2"),zero("y3"),zero("y4"),
            {type:"line",xref:"paper",x0:0,x1:1,yref:"y5",y0:2,y1:2,
             line:{color:INK2,width:1,dash:"dot"}}],
    annotations:tsAnn.concat([{x:1,y:2,xref:"paper",yref:"y5",text:"2 Hz = every dwell",
                  showarrow:false,font:{color:INK2,size:10},xanchor:"right",yshift:8}])},CFG);

  // ---------- engagement mode: chips + separation/closing + ground-range/alt ----------
  document.getElementById("engchips").style.display=eng?"":"none";
  document.getElementById("engsec").style.display=eng?"":"none";
  if(eng){
    const P=eng.pairs||{};
    const chip=(lab,val,extra,cls)=>`<span class="chip ${cls||""}">${lab}<b>${val}</b>${extra?` <small style="color:#5b6169">${extra}</small>`:""}</span>`;
    let ch="";
    const fm=x=>x==null?"—":Math.round(x)+" m";
    const cldir=n=>n.closing!=null?((n.closing>0?"closing ":"opening ")+Math.abs(n.closing).toFixed(0)+" m/s"):"";
    [["tt","target TRUTH"],["tk","target TRACK"]].forEach(([k,lab])=>{
      const p=P[k]; if(!p||!p.now)return;
      ch+=chip("sep ↔ "+lab,fm(p.now.sep),cldir(p.now),p.now.closing>0.5?"good":"");
      if(p.now.pred_miss!=null)ch+=chip("pred miss ("+lab.split(" ")[1]+")",fm(p.now.pred_miss),"t-go "+p.now.t_go+" s","warn");
      if(p.min)ch+=chip("closest so far ("+lab.split(" ")[1]+")",fm(p.min[0]),new Date(p.min[1]*1000).toLocaleTimeString());
    });
    if(eng.int_trk)ch+=chip("interceptor track err",eng.int_trk.med+" m","track "+eng.int_trk.tid);
    document.getElementById("engrow").innerHTML=ch||'<span class="chip">engagement armed — waiting for both feeds…</span>';

    // no pair data yet -> hide the (empty) separation figure instead of whitespace
    const hasPairs=Object.keys(P).length>0;
    document.getElementById("engts").style.display=hasPairs?"":"none";
    document.getElementById("engtswait").style.display=hasPairs?"none":"";
    let et=[];
    const PC={tt:ROLE.ttru,tk:ROLE.ttrk};
    Object.entries(P).forEach(([k,p])=>{
      const t=ts(p.t),c=PC[k]||"#555",dsh=(k==="tk")?"dash":"solid";  // solid=vs truth, dashed=vs track
      et.push({uid:"sep-"+k,x:t,y:p.sep,mode:"lines",name:"vs "+p.name,legendgroup:k,
               line:{color:c,width:2.6,dash:dsh},connectgaps:false,yaxis:"y",
               hovertemplate:"%{x|%H:%M:%S}<br>sep %{y:.0f} m<extra>"+p.name+"</extra>"});
      et.push({uid:"clo-"+k,x:t,y:p.clo,mode:"lines",legendgroup:k,showlegend:false,
               line:{color:c,width:2.2,dash:dsh},connectgaps:false,yaxis:"y2",
               hovertemplate:"%{x|%H:%M:%S}<br>closing %{y:.1f} m/s<extra>"+p.name+"</extra>"});
      if(p.min)et.push({uid:"min-"+k,x:[new Date(p.min[1]*1000)],y:[p.min[0]],mode:"markers+text",
               marker:{color:c,size:13,symbol:"star",line:{width:1,color:"#fcfcfb"}},
               text:["  "+Math.round(p.min[0])+" m"],textposition:"middle right",
               legendgroup:k,showlegend:false,yaxis:"y",hoverinfo:"skip"});});
    // scale sep axis to the data (nice cap), clamp closing to physical ±60 m/s
    let smax=0;Object.values(P).forEach(p=>(p.sep||[]).forEach(x=>{if(x!=null&&x>smax)smax=x;}));
    const scap=Math.max(200,Math.ceil(smax/250)*250);
    if(hasPairs)Plotly.react("engts",gl(et),{...BASE,
      margin:{l:62,r:15,t:58,b:45},
      xaxis:{gridcolor:GRID,anchor:"y2",title:"time (PDT)",nticks:9},
      yaxis :{gridcolor:GRID,domain:[0.56,1.0],title:"separation (m)",range:[0,scap],nticks:6,zeroline:false},
      yaxis2:{gridcolor:GRID,domain:[0,0.44],title:"closing (m/s, + = closing)",range:[-60,60],dtick:20,zeroline:false},
      shapes:[{type:"line",xref:"paper",x0:0,x1:1,yref:"y2",y0:0,y1:0,
               line:{color:INK2,width:1,dash:"dash"}}]},CFG);

    let gt=[];
    const GORDER=["ttru","itru","ttrk"];
    GORDER.forEach(rl=>{
      const s=(eng.gralt||{})[rl];
      if(!s)return;
      gt.push({uid:"gr-"+rl,x:s.rng,y:s.alt,mode:"lines",name:s.name,
               line:{color:ROLE[rl],width:rl.endsWith("tru")?4.5:3.6,
                     dash:rl.endsWith("trk")?"dash":"solid"},connectgaps:false,
               hovertemplate:"rng %{x:.0f} m<br>alt %{y:.0f} m<extra>"+s.name+"</extra>"});});
    // ★ CPA midpoint on the profile (skip if altitude unknown — needs U)
    Object.entries(P).forEach(([k,p])=>{
      if(!p.min_pos||p.min_pos.length<3||p.min_pos[2]==null||!p.min)return;
      gt.push({uid:"cpagr-"+k,
        x:[Math.hypot(p.min_pos[0],p.min_pos[1])],
        y:[p.min_pos[2]-(eng.agl_datum||0)],mode:"markers+text",
        marker:{size:15,symbol:"star",color:"#111",line:{width:1,color:"#fff"}},
        text:["  CPA "+Math.round(p.min[0])+" m"],textposition:"top right",
        name:"closest so far",showlegend:false,
        hovertemplate:"CPA "+Math.round(p.min[0])+" m<br>rng %{x:.0f} m alt %{y:.0f} m<extra>"+(p.name||k)+"</extra>"});});
    const grAnn=(eng.vec||[]).filter(o=>o.spd>0.5&&o.role!=="itrk").map(o=>({
      x:o.rng+o.vr*LEAD,y:o.alt+o.vU*LEAD,ax:o.rng,ay:o.alt,
      xref:"x",yref:"y",axref:"x",ayref:"y",showarrow:true,
      arrowhead:2,arrowsize:1.2,arrowwidth:2.6,arrowcolor:ROLE[o.role],text:""}));
    // FIXED profile frame (pre-computed from the 8-27 engagement envelope):
    // range 0-4.5 km, alt -30..300 m — no auto-resizing; pan/zoom manually.
    Plotly.react("gralt",gl(gt),{...BASE,
      margin:{l:62,r:15,t:58,b:45},annotations:grAnn,
      xaxis:{gridcolor:GRID,title:"ground range from antenna (m)",range:[0,4500],dtick:500},
      yaxis:{gridcolor:GRID,title:"altitude AGL (m)",range:[-10,300],dtick:50}},CFG);
  }
  } // end heavy
 }catch(e){document.getElementById("mode").textContent="error: "+e;}
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    plotlyjs = None
    replay = False

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html"
        elif u.path == "/data.json":
            body, ctype = json.dumps(snapshot()).encode(), "application/json"
        elif u.path == "/save_reset":
            SAVE_REQ.append(time.time())
            body, ctype = b'{"ok": true}', "application/json"
        elif u.path == "/runs":
            rs = ([{"id": "", "name": "(replay mode)", "last": ""}]
                  if Handler.replay else list_runs())
            body, ctype = json.dumps(rs).encode(), "application/json"
        elif u.path == "/set":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            upd = {}
            if "host" in q and q["host"]:
                upd["host"] = q["host"]
            if "run" in q:
                upd["run"] = q["run"]
            if "target" in q:
                upd["target"] = q["target"]
            if "interceptor" in q:
                upd["interceptor"] = q["interceptor"]
            if "tgt_track" in q:
                upd["tgt_track"] = q["tgt_track"].strip()
            for k, cast in (("gate", float), ("min_dur", float),
                            ("az_fixed", float)):
                if k in q:
                    try:
                        upd[k] = cast(q[k])
                    except ValueError:
                        pass
            if q.get("az_mode") in ("raw", "auto", "fixed"):
                upd["az_mode"] = q["az_mode"]
            if "keep_adsb" in q:
                upd["keep_adsb"] = q["keep_adsb"] in ("1", "true")
            gen = cfg_set(upd)
            body, ctype = json.dumps({"ok": True, "gen": gen}).encode(), \
                "application/json"
        elif u.path == "/satmap":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                pl = _satmap_payload(float(q["x0"]), float(q["x1"]),
                                     float(q["y0"]), float(q["y1"]))
            except Exception as e:
                pl = {"err": repr(e)}
            body, ctype = json.dumps(pl).encode(), "application/json"
        elif u.path == "/plotly.min.js":
            body, ctype = Handler.plotlyjs, "application/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if u.path != "/plotly.min.js":       # page/data always fresh; big lib may cache
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="10.191.28.205",
                    help="initial mongo host (mx node) — changeable in the UI")
    ap.add_argument("--port", type=int, default=27017)
    ap.add_argument("--db", default="sensor_store")
    ap.add_argument("--run", default=None,
                    help="initial run collection (default newest; UI dropdown)")
    ap.add_argument("--target", default=None,
                    help="initial truth target_id substring filter (UI dropdown)")
    ap.add_argument("--interceptor", default=None,
                    help="interceptor target_id substring — setting this enables "
                         "ENGAGEMENT MODE (two merged feeds, closing/CPA metrics)")
    ap.add_argument("--antenna-ll", default=None,
                    help="lat,lon for the satellite basemap in REPLAY mode "
                         "(live mode reads it from the run automatically)")
    ap.add_argument("--tracks", default=None, help="manual track ids, comma-separated")
    ap.add_argument("--gate", type=float, default=150.0,
                    help="auto-associate gate (m, median over moving-truth samples)")
    ap.add_argument("--az-bias", default="raw",
                    help="'raw' (measure, don't remove — default), 'auto' "
                         "(estimate & remove), or fixed deg to remove")
    ap.add_argument("--tz", default="America/Los_Angeles",
                    help="IANA tz for displayed times")
    ap.add_argument("--backfill", type=float, default=300,
                    help="seconds of history to preload")
    ap.add_argument("--interval", type=float, default=1.5, help="mongo poll seconds")
    ap.add_argument("--web-port", type=int, default=8898)
    ap.add_argument("--replay", default=None,
                    help="offline demo/dev: bulk npz (mav_*/trk_* arrays) replayed "
                         "with now-shifted timestamps instead of mongo")
    ap.add_argument("--replay-skip", type=float, default=0,
                    help="seconds to skip into the recording")
    ap.add_argument("--keep-adsb", action="store_true",
                    help="do NOT exclude tracks identified as ADS-B aircraft")
    ap.add_argument("--min-dur", type=float, default=10.0,
                    help="hide auto-correlated tracks shorter than this (s)")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    args = ap.parse_args()

    C.set_local_tz(args.tz)
    if args.antenna_ll:
        try:
            la, lo = (float(x) for x in args.antenna_ll.split(","))
            ANT_LL[0] = (la, lo)
        except ValueError:
            print(f"bad --antenna-ll {args.antenna_ll!r} (want lat,lon)")
    if args.az_bias == "raw":
        az_mode, az_fixed = "raw", 0.0
    elif args.az_bias == "auto":
        az_mode, az_fixed = "auto", 0.0
    else:
        az_mode, az_fixed = "fixed", float(args.az_bias)
    cfg_set({"host": args.host, "port": args.port, "db": args.db,
             "run": args.run or "", "target": args.target or "",
             "interceptor": args.interceptor or "",
             "gate": args.gate, "min_dur": args.min_dur,
             "az_mode": az_mode, "az_fixed": az_fixed,
             "keep_adsb": args.keep_adsb})

    # Read the pre-built plotly bundle instead of importing plotly in this process.
    # (Importing plotly at startup is heavy enough to get the backgrounded server
    # signal-killed in some sandboxes; a plain file read is lightweight.)
    _pjs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plotly.min.js")
    if os.path.exists(_pjs):
        with open(_pjs, "rb") as _f:
            Handler.plotlyjs = _f.read()
    else:
        Handler.plotlyjs = C.get_plotlyjs().encode()   # fallback (builds the bundle)
    Handler.replay = bool(args.replay)
    threading.Thread(target=poller, args=(args,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.web_port), Handler)
    print(f"live dashboard: http://<this-box>:{args.web_port}/  (ctrl-c to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    import traceback
    try:
        main()
    except BaseException:
        with open("/tmp/lc_crash.log", "w") as _f:
            _f.write(traceback.format_exc())
        raise
