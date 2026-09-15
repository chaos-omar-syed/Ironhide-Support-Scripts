"""A minimal pymongo-compatible FAKE built from the 8/28 archive CSVs, so the dashboard's LIVE mongo
code path (feed.live_snapshot / probe / run_detail, corr_lib helpers) runs end-to-end without a radar.

Supported surface (what the dashboard + corr_lib call):
  MongoClient(host, port, **kw) -> client[db][coll]; client.admin.command("ping"); client.close()
  db.list_collection_names(); db["runs"].find(...)
  coll.find(filter, projection=..., sort=..., limit=...)   filter: equality (incl. dotted paths into a list
        payload: any element matches, like mongo) and $gte/$lte/$gt/$lt/$in on scalars (time_spec_float,
        time_spec.full_sec, block_type, payload.source); sort: [("$natural", ±1)] or field keys; projection:
        inclusion with dotted paths (applied per array element, like mongo)
  coll.find_one(filter, projection=..., sort=...); coll.count_documents(filter); coll.index_information()
An UNREACHABLE host raises pymongo.errors.ServerSelectionTimeoutError on the first server op after
sleeping the client's serverSelectionTimeoutMS (capped) — the dashboard must show OFFLINE, no exception.

Document shapes mirror MRU91 / MRU39 (2026.3.x - 2026.9.x builds):
  block 106 AIR_TRAFFIC : payload = LIST of dicts for even docs, a DICT for odd docs (both occur); every
        10th list doc also carries ADS-B entries (source "ADS-B") that must be ignored; MAVLINK entries
        {target_id, source, lat, lon, altitude (FEET MSL), velocity_n_mps, velocity_e_mps, vertical_speed
        (ft/min), validposition, speed_mps, timestamp}
  block 143 TRACKS      : payload = LIST of every track published at that time (a DICT when it is one track);
        {track_id, track_state, track_type, x_state NED [N,E,D,vN,vE,vD], p_cov 6x6 (MISSING for track ids
        % 11 == 0 and for every 17th row; velocity block = SIGV² per axis: σvE 0.8 / σvN 0.6 / σvU 1.5 m/s, NED
        order [vN,vE,vD] — the dashboard must surface sqrt(P[4,4]) / sqrt(P[3,3]) / sqrt(P[5,5]) as sigvE / sigvN /
        sigvU), last_update_time {full_sec, frac_sec}, total_associations,
        antenna_location_origin_{latitude,longitude}_rad / _altitude_m, timestamp}; some rows lack x_state.
        RUNTIME TRUTH MATCH (2026.9.x shape, deterministic — the engine tests key off it):
          truth_match  = None                                          when the state is > TM_GATE_M from both drones
                       = {target_id: "mav14550_1_1" | "mav14551_2_0",  MAVLink match: the nearer drone within TM_GATE_M,
                          match_type: "plurality_based, confidence_0.xx", source: "MAVLINK", ...}  conf = 1 - d/TM_GATE_M (>= 0.5)
                       = {target_id: ADSB_CALLSIGN ("N432R"), match_type: "plurality_based, confidence_0.90", source: "ADS-B"}
                                                                       for EVERY state of track ids % 13 == 0 (an airliner: HARD EXCLUDE)
          contributors = [{network_id, cluster_id, tx_id, node_id, obs_id, tx_lla_ddm, rx_lla_ddm}] with tx == rx == ANT (co-located ->
                         feed.tx_origin None) in RUN_COLL; TRACKS_ONLY_COLL carries tx_lla_ddm offset TX_OFFSET_DEG north -> tx_lla derivable.
        Every THIRD document uses the 2026.9.x layout instead (MRU39, version_id 1.7): NO x_state / p_cov,
        but x_state_ecef [X,Y,Z,vX,vY,vZ], p_cov_ecef 6x6 (position AND velocity blocks rotated ENU -> ECEF with the same
        SIGV velocity sigmas), latitude_rad / longitude_rad / altitude_m, track_velocity_n / _e — the dashboard must place
        these through the same ENU frame as the truth and rotate both covariance blocks back.
  block 103 DWELL_WITH_OBS: payload = {"dwell_id", "observations": [ {az_rad, el_rad, amb_rng_km, amb_dop_ms,
        amb_bistatic_rng_km (= 2 x mono), amb_bistatic_rng_rate_ms (= 2 x opening-positive rate = -2·amb_dop),
        snr_db, truth_target}, ... ]} with NON-DICT entries ("junk", 3.0, None) mixed in every 5th doc and amb_dop_ms
        (+ the bistatic rate) omitted on every 7th observation.  amb_dop_ms = −(monostatic opening-positive range rate
        of the TARGET truth) + noise, i.e. the sign convention spa documents (build_obs_df: −2·amb_dop).
        truth_target (per observation, deterministic): {target_id "mav14550_1_1", match_type "range_and_el", source
        "MAVLINK", error_info {range_m}} when the obs is within OBS_AZ_GATE / OBS_RNG_GATE of the target truth; every
        9th other observation -> {target_id "DAL1556", match_type "range_and_el", source "ADS-B"}; else absent.
  runs                  : {run_id_str, name, date_time, config}
Times can be SHIFTED so the archive plays as if it were happening now (``shift_to_now``): documents whose
shifted time is in the future are invisible until the wall clock reaches them — a streaming source.
"""
from __future__ import annotations

import glob
import math
import os
import sys
import time

import numpy as np
import polars as pl

# the built-in 8/28 quickdump day (ih.data.BASE = $IH_QUICKDUMP_DIR, default <repo>/data/2026-08-28) — data is not shipped
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from ih import data as _D  # noqa: E402

ARCHIVE = os.path.join(_D.BASE, "e65bd4f9_flight1_quickdump")
OBS_NPZ = _D.OBS_NPZ[1]
ANT = (33.748055963056764, -115.33920402695068, 140.51095000131696)
RUN_HEX = "e65bd4f9" + "0" * 24
RUN_COLL = f"run_{RUN_HEX}"
ADSB_ONLY_COLL = "run_adsb00000000000000000000000000"     # air traffic without any MAVLINK row, no tracks
ECEF_EVERY = 3                                            # every 3rd block-143 document in the 2026.9.x ECEF/LLA layout
TRACKS_ONLY_COLL = "run_trk000000000000000000000000000"    # radar tracks + obs, no block 106 at all
UNREACHABLE = {"10.255.255.1", "unreachable"}
GEOID_N = -31.4
M_PER_FT = 0.3048
TM_GATE_M = 150.0                                         # runtime truth match: a state within this (horizontal) of a drone matches it
ADSB_CALLSIGN, ADSB_TRACK_MOD = "N432R", 13               # track ids % 13 == 0 match an ADS-B callsign on EVERY state (airliner -> hard exclude)
TX_OFFSET_DEG = 0.001                                     # TRACKS_ONLY_COLL: contributors tx_lla_ddm = ANT shifted north by this (~111 m) -> tx_lla derivable
OBS_AZ_GATE, OBS_RNG_GATE = 0.06, 300.0                   # obs truth_target: within this of the target truth -> MAVLink match
OBS_ADSB_EVERY = 9                                        # every 9th unmatched obs carries an ADS-B truth_target
SIGV = (0.8, 0.6, 1.5)                                    # 1σ filtered velocity per ENU axis (E, N, U) m/s written into every p_cov / p_cov_ecef


# ── document builders ────────────────────────────────────────────────────────
def _tspec(t: float) -> dict:
    return {"full_sec": int(math.floor(t)), "frac_sec": float(t - math.floor(t))}


def _doc(bt: int, t: float, payload, job_id: int = 1) -> dict:
    return {"block_type": bt, "time_spec": _tspec(t), "time_spec_float": float(t), "job_id": job_id, "node_id": 0, "payload": payload}


def _adsb(t: float, k: int) -> dict:
    return {"version_id": 3.0, "target_id": f"N{1000 + k}", "source": "ADS-B", "lat": 34.1 + 0.01 * k, "lon": -117.9, "validposition": 1,
            "altitude": 6400.0 + 100 * k, "speed_mps": 84.9, "velocity_n_mps": 12.3, "velocity_e_mps": -84.0, "vertical_speed": 64.0,
            "timestamp": t, "hex": f"adcc{k:02x}", "emitter_category": "A1"}


def mavlink_payload(name: str, row: dict) -> dict:
    return {"version_id": 3.0, "target_id": name, "source": "MAVLINK", "lat": float(row["lat"]), "lon": float(row["lon"]),
            "validposition": int(row["validposition"]), "altitude": float(row["alt_ft_wire"]),
            "speed_mps": float(math.hypot(row["vel_n_mps"], row["vel_e_mps"])), "velocity_n_mps": float(row["vel_n_mps"]),
            "velocity_e_mps": float(row["vel_e_mps"]), "vertical_speed": float(row["vert_spd_wire_ftmin"]), "timestamp": float(row["t_epoch"])}


def build_air_traffic(t0: float, t1: float, feeds: dict[str, pl.DataFrame] | None = None, freeze: dict[str, float] | None = None) -> list[dict]:
    """Block-106 docs from the mavlink CSVs in [t0, t1].  ``freeze`` = {feed name: t_from}: rows of that feed
    after t_from repeat the last position (a frozen GPS) — the dashboard must flag the feed FROZEN."""
    feeds = feeds if feeds is not None else load_feeds()
    rows = []
    for name, df in feeds.items():
        d = df.filter((pl.col("t_epoch") >= t0) & (pl.col("t_epoch") <= t1))
        last = None
        for r in d.iter_rows(named=True):
            if freeze and name in freeze and r["t_epoch"] >= freeze[name] and last is not None:
                r = {**r, "lat": last["lat"], "lon": last["lon"], "alt_ft_wire": last["alt_ft_wire"]}
            else:
                last = r
            rows.append((float(r["t_epoch"]), name, r))
    rows.sort(key=lambda x: (x[0], x[1]))
    docs = []
    for i, (t, name, r) in enumerate(rows):
        p = mavlink_payload(name, r)
        if i % 2 == 0:
            pl_ = [p] + ([_adsb(t, i % 7), _adsb(t, (i + 3) % 7)] if i % 10 == 0 else [])
        else:
            pl_ = p
        docs.append(_doc(106, t, pl_))
    return docs


def truth_pos_fn(feeds: dict[str, pl.DataFrame], role: str):
    """(t) -> (E, N) of one drone's truth (target = mav14550_1_1; interceptor = the mav14551_2_* feeds merged), NaN outside."""
    names = [n for n in feeds if (n.startswith("mav14550") if role == "target" else n.startswith("mav14551"))]
    df = pl.concat([feeds[n].filter(pl.col("validposition") == 1) for n in names]).sort("t_epoch").unique(subset="t_epoch", keep="first", maintain_order=True)
    t = df["t_epoch"].to_numpy().astype(float)
    E, N = df["E_m"].to_numpy().astype(float), df["N_m"].to_numpy().astype(float)

    def fn(tt: float) -> tuple[float, float]:
        if not len(t) or tt < t[0] or tt > t[-1]:
            return float("nan"), float("nan")
        i = int(np.searchsorted(t, tt))
        if i < len(t) and abs(t[i] - tt) > 2.5 and (i == 0 or abs(t[i - 1] - tt) > 2.5):   # never interpolate across a gap
            return float("nan"), float("nan")
        return float(np.interp(tt, t, E)), float(np.interp(tt, t, N))
    return fn


def truth_match_for(tid: int, t: float, E: float, N: float, pos_t, pos_i) -> dict | None:
    """The deterministic runtime truth match of one track state (see the module docstring)."""
    if tid % ADSB_TRACK_MOD == 0:
        return {"target_id": ADSB_CALLSIGN, "lat_deg": 33.95, "lon_deg": -118.2, "match_type": "plurality_based, confidence_0.90", "hex": "a1b2c3",
                "source": "ADS-B", "error_info": None, "truth_info": None}
    best = None
    for name, fn in (("mav14550_1_1", pos_t), ("mav14551_2_0", pos_i)):
        e, n_ = fn(t)
        if not (np.isfinite(e) and np.isfinite(n_)):
            continue
        d = math.hypot(E - e, N - n_)
        if d <= TM_GATE_M and (best is None or d < best[1]):
            best = (name, d)
    if best is None:
        return None
    conf = max(0.5, 1.0 - best[1] / TM_GATE_M)
    return {"target_id": best[0], "lat_deg": None, "lon_deg": None, "match_type": f"plurality_based, confidence_{conf:.2f}", "hex": None,
            "source": "MAVLINK", "error_info": {"range_m": round(best[1], 1)}, "truth_info": None}


def contributors_for(obs_id: int, tx_offset_deg: float = 0.0) -> list[dict]:
    """MRU39-shaped contributors list; tx == rx (co-located) unless ``tx_offset_deg`` shifts the TX north."""
    return [{"network_id": 1, "cluster_id": 139, "tx_id": 0, "node_id": 1002, "obs_id": int(obs_id),
             "tx_lla_ddm": [ANT[0] + tx_offset_deg, ANT[1], ANT[2]], "rx_lla_ddm": [ANT[0], ANT[1], ANT[2]]}]


def build_tracks(t0: float, t1: float, tracks: pl.DataFrame | None = None, feeds: dict[str, pl.DataFrame] | None = None) -> list[dict]:
    """Block-143 docs from track_*.csv rows in [t0, t1]: one doc per publish time with EVERY track at that
    time (list payload; a single track -> dict payload).  ENU -> NED x_state, p_cov from the sigmas; truth_match /
    contributors per the module docstring (``feeds`` -> the drones' truth positions; None -> no MAVLink matches)."""
    from ih import feed as F

    tr = tracks if tracks is not None else load_tracks()
    tr = tr.filter((pl.col("t_epoch") >= t0) & (pl.col("t_epoch") <= t1)).sort(["t_epoch", "tid"])
    docs = []
    lat_r, lon_r = math.radians(ANT[0]), math.radians(ANT[1])
    R = F.enu_rotation(lat_r, lon_r)
    ant_ecef = F.lla_to_ecef(lat_r, lon_r, ANT[2])
    import corr_lib as C
    frame = C.EnuFrame(ANT)                                             # exact inverse of the frame the dashboard places tracks with
    nan_fn = lambda tt: (float("nan"), float("nan"))  # noqa: E731
    pos_t = truth_pos_fn(feeds, "target") if feeds else nan_fn
    pos_i = truth_pos_fn(feeds, "interceptor") if feeds else nan_fn
    n = 0
    for k, (t, g) in enumerate(tr.group_by("t_epoch", maintain_order=True)):
        t = float(t[0])
        ecef_layout = (k % ECEF_EVERY) == ECEF_EVERY - 1
        pls = []
        for r in g.iter_rows(named=True):
            n += 1
            tid = int(r["tid"])
            E, N, U = float(r["E_m"]), float(r["N_m"]), float(r["U_m"])
            vE, vN, vU = float(r["vE_mps"]), float(r["vN_mps"]), float(r["vU_mps"])
            P = np.zeros((6, 6))
            P[0, 0], P[1, 1], P[2, 2] = float(r["sigN_m"]) ** 2, float(r["sigE_m"]) ** 2, float(r["sigU_m"]) ** 2   # NED order
            P[3, 3], P[4, 4], P[5, 5] = SIGV[1] ** 2, SIGV[0] ** 2, SIGV[2] ** 2                                          # [vN, vE, vD] variances
            has_cov = tid % 11 != 0 and n % 17 != 0   # missing covariance: whole tracks (id % 11 == 0) and every 17th row
            p = {"tracker_name": "MacTracker", "track_id": tid, "track_state": int(r["track_state"]), "track_type": 1, "timestamp": _tspec(t),
                 "version_id": 1.7 if ecef_layout else 1.5,
                 "last_update_time": _tspec(float(r["last_update_t"])), "total_associations": int(r["total_associations"]),
                 "antenna_location_origin_latitude_rad": lat_r, "antenna_location_origin_longitude_rad": lon_r,
                 "antenna_location_origin_altitude_m": ANT[2], "classification": {"classifier": "", "prediction": None},
                 "truth_match": truth_match_for(tid, t, E, N, pos_t, pos_i), "contributors": contributors_for(n)}
            if ecef_layout:
                lat, lon, alt = frame.lla(E, N, U)
                pos_ecef = ant_ecef + R.T @ np.array([E, N, U])          # (the dashboard places the LLA; the ECEF state carries the velocity)
                vel_ecef = R.T @ np.array([vE, vN, vU])
                p.update({"latitude_rad": math.radians(lat), "longitude_rad": math.radians(lon), "altitude_m": alt,
                          "x_state_ecef": [*map(float, pos_ecef), *map(float, vel_ecef)], "track_velocity_n": vN, "track_velocity_e": vE,
                          "range_m": float(math.sqrt(E * E + N * N + U * U)), "az_angle_rad": float(math.atan2(E, N) % (2 * math.pi))})
                if has_cov:
                    Penu = np.diag([P[1, 1], P[0, 0], P[2, 2]])          # E, N, U variances
                    Pvenu = np.diag([SIGV[0] ** 2, SIGV[1] ** 2, SIGV[2] ** 2])   # vE, vN, vU variances
                    Pe = np.zeros((6, 6)); Pe[:3, :3] = R.T @ Penu @ R; Pe[3:, 3:] = R.T @ Pvenu @ R
                    p["p_cov_ecef"] = Pe.tolist()
            else:
                p["x_state"] = [N, E, -U, vN, vE, -vU]
                if has_cov:
                    p["p_cov"] = P.tolist()
                if n % 53 == 0:
                    p.pop("x_state")                 # a row without a state (must be skipped, not crash)
            pls.append(p)
        docs.append(_doc(143, t, pls if len(pls) != 1 else pls[0], job_id=2))
    return docs


def build_obs(t0: float, t1: float, truth_rr=None, truth_pos=None) -> list[dict]:
    """Block-103 docs from the F1 obs npz in [t0, t1] (dict payload with an observations list; non-dict
    entries mixed in every 5th doc; amb_dop_ms omitted on every 7th observation).  amb_dop_ms =
    -truth_rr(t) + noise where truth_rr is the TARGET truth's monostatic opening-positive range rate;
    amb_bistatic_rng_rate_ms = 2 x the opening-positive rate (co-located TX).  ``truth_pos(t)`` -> (E, N) of the
    target truth drives the per-observation truth_target (module docstring)."""
    if not os.path.exists(OBS_NPZ):
        return []
    o = np.asarray(np.load(OBS_NPZ, allow_pickle=True)["obs"], float)
    o = o[(o[:, 0] >= t0) & (o[:, 0] <= t1)]
    rng = np.random.default_rng(7)
    docs, k, n_free = [], 0, 0
    for t in np.unique(o[:, 0]):
        rows = o[o[:, 0] == t]
        obs = []
        te, tn = truth_pos(t) if truth_pos is not None else (float("nan"), float("nan"))
        for r in rows:
            k += 1
            e = {"obs_id": k, "az_rad": float(r[1]), "el_rad": float(r[2]), "amb_rng_km": float(r[3]) / 1000.0, "snr_db": 20.0,
                 "amb_bistatic_rng_km": 2.0 * float(r[3]) / 1000.0}
            if k % 7 != 0 and truth_rr is not None:
                rr = float(truth_rr(t))
                if np.isfinite(rr):
                    e["amb_dop_ms"] = -rr + float(rng.normal(0, 0.5))
                    e["amb_bistatic_rng_rate_ms"] = -2.0 * e["amb_dop_ms"]
            matched = False
            if np.isfinite(te) and np.isfinite(tn):
                taz, trng = math.atan2(te, tn) % (2 * math.pi), math.hypot(te, tn)
                daz = abs((float(r[1]) - taz + math.pi) % (2 * math.pi) - math.pi)
                if daz <= OBS_AZ_GATE and abs(float(r[3]) - trng) <= OBS_RNG_GATE:
                    e["truth_target"] = {"target_id": "mav14550_1_1", "lat_deg": None, "lon_deg": None, "match_type": "range_and_el", "hex": None,
                                         "source": "MAVLINK", "error_info": {"range_m": round(float(r[3]) - trng, 1)}}
                    matched = True
            if not matched:
                n_free += 1
                if n_free % OBS_ADSB_EVERY == 0:
                    e["truth_target"] = {"target_id": "DAL1556", "lat_deg": 33.9548, "lon_deg": -118.3756, "match_type": "range_and_el", "hex": "a441fb",
                                         "source": "ADS-B", "error_info": None}
            obs.append(e)
        if len(docs) % 5 == 0:
            obs = obs[:1] + ["junk", 3.0, None] + obs[1:]
        docs.append(_doc(103, float(t), {"dwell_id": len(docs) + 1, "observations": obs}, job_id=2))
    return docs


def load_feeds() -> dict[str, pl.DataFrame]:
    return {os.path.splitext(os.path.basename(f))[0]: pl.read_csv(f, infer_schema_length=100000)
            for f in sorted(glob.glob(os.path.join(ARCHIVE, "mavlink", "mav*.csv")))}


def load_tracks() -> pl.DataFrame:
    parts = []
    for f in glob.glob(os.path.join(ARCHIVE, "tracks", "track_*.csv")):
        tid = int(os.path.splitext(os.path.basename(f))[0].split("_")[1])
        parts.append(pl.read_csv(f, infer_schema_length=100000).with_columns(pl.lit(tid).alias("tid")))
    return pl.concat(parts)


def target_rr_fn(feeds: dict[str, pl.DataFrame]):
    """Monostatic opening-positive range rate of the TARGET truth vs time (linear interpolation)."""
    df = feeds["mav14550_1_1"].filter(pl.col("validposition") == 1).sort("t_epoch")
    t = df["t_epoch"].to_numpy().astype(float)
    E, N, U = (df[c].to_numpy().astype(float) for c in ("E_m", "N_m", "U_m_hae"))
    vE, vN = df["vel_e_mps"].to_numpy().astype(float), df["vel_n_mps"].to_numpy().astype(float)
    vU = df["vert_spd_wire_ftmin"].to_numpy().astype(float) * 0.00508
    rng = np.sqrt(E * E + N * N + U * U)
    rr = (E * vE + N * vN + U * vU) / np.where(rng > 0, rng, np.nan)

    def fn(tt: float) -> float:
        if tt < t[0] or tt > t[-1]:
            return float("nan")
        return float(np.interp(tt, t, rr))
    return fn


# ── query engine ─────────────────────────────────────────────────────────────
def _resolve(node, parts: list[str]) -> list:
    """Values at a dotted path (mongo semantics: an intermediate array fans out to its elements)."""
    if not parts:
        return [node]
    if isinstance(node, list):
        out = []
        for x in node:
            out.extend(_resolve(x, parts))
        return out
    if isinstance(node, dict) and parts[0] in node:
        return _resolve(node[parts[0]], parts[1:])
    return []


def _cmp(v, op: str, arg) -> bool:
    try:
        if op == "$gte":
            return v >= arg
        if op == "$lte":
            return v <= arg
        if op == "$gt":
            return v > arg
        if op == "$lt":
            return v < arg
        if op == "$in":
            return v in arg
        if op == "$ne":
            return v != arg
        if op == "$eq":
            return v == arg
    except TypeError:
        return False
    raise NotImplementedError(op)


def _match(doc: dict, flt: dict | None) -> bool:
    for k, cond in (flt or {}).items():
        vals = _resolve(doc, k.split("."))
        if isinstance(cond, dict) and cond and all(str(c).startswith("$") for c in cond):
            if not any(all(_cmp(v, op, arg) for op, arg in cond.items()) for v in vals):
                return False
        elif not any(v == cond for v in vals):
            return False
    return True


def _include(node, paths: list[list[str]]):
    if isinstance(node, list):
        return [_include(x, paths) for x in node]
    if not isinstance(node, dict):
        return node
    if any(len(p) == 0 for p in paths):
        return node
    heads: dict[str, list] = {}
    for p in paths:
        heads.setdefault(p[0], []).append(p[1:])
    out = {}
    for h, subs in heads.items():
        if h in node:
            out[h] = node[h] if any(len(sp) == 0 for sp in subs) else _include(node[h], subs)
    return out


def _project(doc: dict, proj: dict | None) -> dict:
    if not proj:
        return doc
    inc = [k.split(".") for k, v in proj.items() if v]
    out = _include(doc, inc)
    if "_id" in doc and proj.get("_id", 1):
        out["_id"] = doc["_id"]
    return out


class FakeCursor:
    def __init__(self, docs: list[dict], proj: dict | None):
        self._docs, self._proj, self._limit = docs, proj, 0

    def sort(self, key, direction=None):
        keys = key if isinstance(key, list) else [(key, direction or 1)]
        for k, d in reversed(keys):
            if k == "$natural":
                if d < 0:
                    self._docs = list(reversed(self._docs))
            else:
                self._docs = sorted(self._docs, key=lambda x: (_resolve(x, k.split(".")) or [0])[0], reverse=d < 0)
        return self

    def limit(self, n: int):
        self._limit = int(n)
        return self

    def __iter__(self):
        docs = self._docs[: abs(self._limit)] if self._limit else self._docs
        for d in docs:
            yield _project(d, self._proj)

    def __next__(self):
        return next(iter(self))


class FakeCollection:
    def __init__(self, name: str, docs: list[dict], now_fn=None):
        self.name, self._docs, self._now = name, list(docs), now_fn
        for i, d in enumerate(self._docs):
            d.setdefault("_id", i)
        self.calls: list[tuple] = []

    def _visible(self) -> list[dict]:
        if self._now is None:
            return self._docs
        now = self._now()
        return [d for d in self._docs if d.get("time_spec_float", 0.0) <= now]

    def find(self, filter=None, projection=None, sort=None, limit=0, **_):
        self.calls.append(("find", filter, projection, sort))
        c = FakeCursor([d for d in self._visible() if _match(d, filter)], projection)
        if sort:
            c.sort(sort)
        if limit:
            c.limit(limit)
        return c

    def find_one(self, filter=None, projection=None, sort=None, **_):
        for d in self.find(filter, projection=projection, sort=sort, limit=1):
            return d
        return None

    def count_documents(self, filter=None, **_):
        self.calls.append(("count", filter))
        return sum(1 for d in self._visible() if _match(d, filter))

    def index_information(self):
        return {"_id_": {"key": [("_id", 1)]}, "block_type_1_time_spec.full_sec_-1_time_spec.frac_sec_-1": {"key": [("block_type", 1), ("time_spec.full_sec", -1), ("time_spec.frac_sec", -1)]}}


class FakeDB:
    def __init__(self, colls: dict[str, FakeCollection]):
        self._c = colls

    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self._c:
            self._c[name] = FakeCollection(name, [])
        return self._c[name]

    def list_collection_names(self) -> list[str]:
        return list(self._c)


class _Admin:
    def __init__(self, client):
        self._cl = client

    def command(self, name, *a, **k):
        self._cl._check()
        return {"ok": 1.0}


class FakeClient:
    def __init__(self, server: "FakeMongo", host: str, port: int, **kw):
        self._srv, self.host, self.port = server, host, int(port)
        self._timeout = min(float(kw.get("serverSelectionTimeoutMS", 3000)) / 1000.0, 3.0)
        self.admin = _Admin(self)
        server.clients += 1

    def _check(self):
        if self.host in UNREACHABLE or self.port != self._srv.port:
            from pymongo.errors import ServerSelectionTimeoutError

            time.sleep(self._timeout)
            raise ServerSelectionTimeoutError(f"{self.host}:{self.port}: [Errno 113] No route to host (fake), Timeout: {self._timeout:.1f}s")

    def __getitem__(self, db: str) -> FakeDB:
        self._check()
        return self._srv.dbs.setdefault(db, FakeDB({}))

    def close(self):
        self._srv.closed += 1


class FakeMongo:
    """The fake server: build once (a few seconds: 35 k track rows), install into pymongo, drive tests."""

    def __init__(self, t0: float, t1: float, *, shift_to_now: float | None = None, freeze: dict[str, float] | None = None,
                 port: int = 27017, with_obs: bool = True, run_name: str = "Red_Mandrill"):
        self.port, self.clients, self.closed = int(port), 0, 0
        feeds = load_feeds()
        rr_fn = target_rr_fn(feeds)
        air = build_air_traffic(t0, t1, feeds, freeze=freeze)
        trk = build_tracks(t0, t1, feeds=feeds)
        obs = build_obs(t0, t1, rr_fn, truth_pos_fn(feeds, "target")) if with_obs else []
        self.shift = 0.0 if shift_to_now is None else (time.time() - float(shift_to_now))
        if self.shift:
            for d in air + trk + obs:
                _shift_doc(d, self.shift)
        now_fn = (lambda: time.time()) if self.shift else None
        by_t = sorted(air + trk + obs, key=lambda d: d["time_spec_float"])
        adsb_only = [_doc(106, d["time_spec_float"], [_adsb(d["time_spec_float"], i % 5), _adsb(d["time_spec_float"], (i + 2) % 5)]) for i, d in enumerate(air[::3])]
        tracks_only = sorted([_with_tx_offset(d, TX_OFFSET_DEG) for d in trk] + [dict(d) for d in obs], key=lambda d: d["time_spec_float"])
        runs = [{"run_id_str": RUN_HEX, "name": run_name, "date_time": "2026-08-28_14_00_00", "config": {"site": "Seawall"}},
                {"run_id_str": ADSB_ONLY_COLL[4:], "name": "Silver_Swordfish", "date_time": "2026-09-10_19_57_36", "config": {}},
                {"run_id_str": TRACKS_ONLY_COLL[4:], "name": "Copper_Heron", "date_time": "2026-09-09_16_07_00", "config": {}}]
        self.colls = {RUN_COLL: FakeCollection(RUN_COLL, by_t, now_fn), ADSB_ONLY_COLL: FakeCollection(ADSB_ONLY_COLL, adsb_only, now_fn),
                      TRACKS_ONLY_COLL: FakeCollection(TRACKS_ONLY_COLL, tracks_only, now_fn), "runs": FakeCollection("runs", runs)}
        self.dbs = {"sensor_store": FakeDB(self.colls)}
        self.n_docs = {"106": len(air), "143": len(trk), "103": len(obs)}

    def client(self, host: str, port: int = 27017, **kw) -> FakeClient:
        return FakeClient(self, host, port, **kw)

    def install(self, monkeypatch=None):
        """pymongo.MongoClient -> this fake (the dashboard imports MongoClient inside feed._client at call time)."""
        import pymongo

        if monkeypatch is not None:
            monkeypatch.setattr(pymongo, "MongoClient", self.client)
            return None
        real = pymongo.MongoClient
        pymongo.MongoClient = self.client
        return lambda: setattr(pymongo, "MongoClient", real)


def _with_tx_offset(d: dict, dlat: float) -> dict:
    """A deep-enough copy of a block-143 doc whose contributors carry a TX shifted ``dlat`` degrees north of the RX."""
    pls = d["payload"]
    def cp(p):
        if not isinstance(p, dict):
            return p
        q = dict(p)
        if q.get("contributors") is not None:
            q["contributors"] = contributors_for(q["contributors"][0]["obs_id"], dlat)
        return q
    out = dict(d)
    out["payload"] = [cp(p) for p in pls] if isinstance(pls, list) else cp(pls)
    return out


def _shift_doc(d: dict, dt: float) -> None:
    d["time_spec_float"] += dt
    d["time_spec"] = _tspec(d["time_spec_float"])
    pls = d["payload"] if isinstance(d["payload"], list) else [d["payload"]]
    for p in pls:
        if not isinstance(p, dict):
            continue
        if isinstance(p.get("timestamp"), dict):
            p["timestamp"] = _tspec(p["timestamp"]["full_sec"] + p["timestamp"]["frac_sec"] + dt)
        elif isinstance(p.get("timestamp"), float):
            p["timestamp"] += dt
        if isinstance(p.get("last_update_time"), dict):
            lu = p["last_update_time"]
            p["last_update_time"] = _tspec(lu["full_sec"] + lu["frac_sec"] + dt)


def hae_m(alt_ft: float) -> float:
    """The conversion the dashboard must apply to a block-106 altitude (FEET MSL) -> m WGS-84 HAE."""
    return alt_ft * M_PER_FT + GEOID_N
