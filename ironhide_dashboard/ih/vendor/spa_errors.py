"""spa_errors — err_stack-compatible track errors computed BY chaos-spa's grading.

Drop-in replacement for ``corr_lib.match_track`` + ``corr_lib.err_stack`` that
returns chaos-spa's numbers instead of ours::

    import corr_lib, spa_errors
    A  = corr_lib.load_tracks(col, t0, t1)[129]          # 13-col track array
    T  = corr_lib.load_truth(col, frame, t0, t1)["mav14550_1_1"]   # 8-col truth
    errs  = spa_errors.spa_err_stack(A, T, antenna_lla)  # dict, same keys as err_stack
    stats = spa_errors.spa_summary(errs)                 # spa's mean/std/rmse95

What it does (frames -> spa.tracks.grade -> corr_df / corr_meas_df):

  1. Builds a ``TRACKS_RAW`` frame from the track array: antenna-ENU state ->
     geodetic (pymap3d.enu2geodetic at the antenna HAE) -> 6-D ECEF state via
     ``spa.geometry.lla_vel_to_ecef_state``; the diagonal ENU position sigmas
     are rotated to a 6x6 ECEF covariance.  The velocity block is the per-row
     1σ from array cols 13..15 (sigvE, sigvN, sigvU — ih.feed.track_rows /
     the quickdump ``sigv*_mps`` columns) where it is finite and > 0, else the
     constant ``vel_sigma_mps`` fill spa needs to be positive.  The fill never
     touches position/az/el/range errors or sigmas, and the rows it was used on
     are remembered (``SpaGrading.options["vel_sigma_real"]``) so their velocity
     sigmas come back NaN — a fabricated σ never reaches a band or a containment
     rate.
  2. Embeds the truth match spa expects on every update (``truth_match_*``):
     truth interpolated LINEARLY at the update time.  Linear-in-ENU is exactly
     linear-in-ECEF (affine map), i.e. identical to what spa's own
     ``recorrelate_tracks_via_obs_history`` does with ``scipy.interp1d``, except
     that we never extrapolate outside truth coverage and honour an optional
     ``max_gap_s`` guard (corr_lib.interp_truth semantics: both bracketing truth
     samples within ``max_gap_s``).  ``max_gap_s=None`` (default) = spa's
     behaviour (no gap rule).
  3. Builds a minimal ``OBS_RAW`` with two SYNTHETIC unmatched anchor rows whose
     ``sensor_nodes``/``beams`` carry the antenna ECEF as a monostatic
     (tx == rx == ``node_id``) link.  spa takes the RX/TX geometry for its
     measurement-space grading ONLY from obs dwell context; without it the RX
     LLA silently becomes (0, 0, 0) and az/el/range are garbage.
  4. Builds an ``AIR_TRAFFIC_RAW`` truth frame (for spa's coverage metrics) and
     calls ``spa.tracks.grade(load_all_truth=True, recorrelate_tracks=False,
     use_tentative=False, use_extrapolated=False)`` -> only CONFIRMED tracks and
     UPDATED (measurement-bearing) updates are graded.  Coast rows (unchanged
     ``last_update_time``) are published to spa as ``TrackType.PREDICTED`` so
     spa's own type gate removes them (``coast_type='updated'`` grades them too).

Units / conventions (all spa's):
  t         epoch seconds (track publish time)
  az_err    deg, track - truth, wrapped to [-180, 180) via spa.geometry.modneg;
            azimuth = pymap3d.ecef2aer from the RX antenna (clockwise from north)
  el_err    deg, track - truth, elevation from the RX antenna (ecef2aer)
  rng_err   m, track - truth MONOSTATIC straight-line range from the RX antenna
            (spa ``mono_rng_error_km`` * 1000).  spa's bistatic range for a
            monostatic link is exactly 2x this.
  alt_err   m, spa ``u_errors``: Up component of (track - truth) in the local
            ENU frame AT THE TRUTH POINT (pymap3d.geodetic2enu(track, ref=truth)).
            WGS-84 HAE: the truth array's U is already HAE (corr_lib applied
            ft->m + geoid), and it is handed to spa as HAE-in-FEET because spa
            only applies ft->m (no geoid model).  Feeding spa the wire MSL feet
            biases alt_err/el_err by geoid_N (about -31.4 m here).
  e_err, n_err, u_err   m, spa ``e_errors``/``n_errors``/``u_errors``
  ve_err, vn_err, vu_err  m/s, spa ``e_dot_errors``/``n_dot_errors``/``u_dot_errors``:
            TRACK − TRUTH filtered velocity per ENU axis (East, North, Up), the
            track velocity rotated ECEF -> ENU at the track position, the truth
            velocity from the embedded MAVLink velocity_n/e + vertical speed.
            NaN where the track array's own velocity was NaN (spa would have
            graded a zero velocity) — see ``vel_sigma_real``.
  sig_ve, sig_vn, sig_vu  m/s, spa ``e_dot_sigma``/``n_dot_sigma``/``u_dot_sigma``
            = sqrt(cov_enu[3,3] / [4,4] / [5,5]); NaN for every row whose velocity σ
            was the constant ``vel_sigma_mps`` fill (13-col arrays, states without
            a covariance) and when ``errs_from_metrics`` is called without the
            provenance dict.
  sig_az, sig_el        deg, spa numerical-Jacobian 1-sigma of the track cov
  sig_rng               m, spa ``bi_rng_sigma_m`` / 2 (monostatic link)
  sig_alt               m, spa ``u_sigma`` (track cov rotated to ENU)
  track_id, update_id   which track/update each sample belongs to

Notes: spa uppercases target ids (``MAV14550_1_1``).  ``spa_summary`` uses
spa's summary functions and therefore spa's two conventions: measurement-space
(az/el/rng) = sample std (ddof=1) + "nearest"-quantile 95.5 % trimmed RMSE
(``_stats_for_meas_summary_series``); position (alt/e/n/u) = population std
(ddof=0) + linear-percentile trimmed RMSE (``_roll_up_error_field``).
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import polars as pl
import pymap3d as pm

# chaos-spa (private repo) must be importable: ``pip install -e chaos-spa``, PYTHONPATH, or $IH_SPA_SRC=<chaos-spa>/src.
# Importing this module WITHOUT it raises ImportError — callers (ih.engine) catch that and use the legacy grader.
_SPA_SRC = os.environ.get("IH_SPA_SRC", "")
if os.environ.get("IH_NO_SPA", "").strip().lower() in ("1", "true", "yes"):
    raise ImportError("spa_errors: chaos-spa blocked by IH_NO_SPA=1")
try:  # honour an existing PYTHONPATH first
    import spa  # noqa: F401
except ImportError:  # pragma: no cover
    if not (_SPA_SRC and os.path.isdir(_SPA_SRC)):
        raise ImportError("spa_errors needs chaos-spa (pip install -e chaos-spa, or set IH_SPA_SRC=<chaos-spa>/src)")
    sys.path.insert(0, _SPA_SRC)
    import spa  # noqa: F401

from spa.geometry import ecef_to_enu_rotation_matrix, lla_vel_to_ecef_state  # noqa: E402
from spa.obs.grading import calc_trimmed_rmse  # noqa: E402
from spa.schemas import (  # noqa: E402
    _BEAMS_DTYPE,
    _SENSOR_NODES_DTYPE,
    AIR_TRAFFIC_RAW,
    OBS_RAW,
    TRACKS_RAW,
    validate,
)
from spa.tracks import GradedTrackMetrics, grade  # noqa: E402
from spa.tracks.summaries import _roll_up_error_field, _stats_for_meas_summary_series  # noqa: E402
from spa.types import TrackType  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corr_lib  # noqa: E402

M_PER_FT = corr_lib.M_PER_FT              # 0.3048
FPM_TO_MPS = 0.00508                      # spa's ft/min -> m/s (frames._FPM_TO_MPS)
KNOT = 0.514444                           # scipy.constants.knot, as used by spa
DEFAULT_NODE_ID = 91
DEFAULT_TARGET_ID = "mav14550_1_1"

ERR_KEYS = ("t", "az_err", "el_err", "rng_err", "alt_err",
            "sig_az", "sig_el", "sig_rng", "sig_alt", "e_err", "n_err", "u_err",
            "pos3d_err", "sig_pos3d", "pos_eig1_cont", "pos_eig2_cont", "pos_eig3_cont",
            "ve_err", "vn_err", "vu_err", "sig_ve", "sig_vn", "sig_vu")
TK_W = 16                  # full track-array width: cols 13..15 = per-row velocity 1σ (sigvE, sigvN, sigvU) m/s
VEL_SIGMA_COLS = slice(13, 16)
T_MATCH_TOL_S = 1e-3       # corr_df.t (full_sec + frac_sec) -> input row lookup tolerance
# pos3d_err  = spa corr_df.errors_3d (|ENU position error vector|, >= 0)
# sig_pos3d  = sqrt(pos_eig1_sigma² + pos_eig2_sigma² + pos_eig3_sigma²) = sqrt(trace of the ENU position
#              covariance) = the 1σ RADIUS of the covariance ellipsoid (spa's eigen-axis sigmas combined)
# pos_eigN_cont = spa's per-eigen-axis containment (projected error / eigen sigma)


# ── input adapters ───────────────────────────────────────────────────────────

def track_rows_to_array(rows) -> np.ndarray:
    """quickdump ``tracks/track_*.csv`` DictReader rows -> 16-col track array
    (t,E,N,U,sigE,sigN,sigU,lu,assoc,state,vE,vN,vU,sigvE,sigvN,sigvU), sorted by t;
    the velocity sigmas are NaN when the CSV predates the ``sigv*_mps`` columns."""
    out = [(float(r["t_epoch"]), float(r["E_m"]), float(r["N_m"]), float(r["U_m"]),
            float(r["sigE_m"]), float(r["sigN_m"]), float(r["sigU_m"]),
            float(r["last_update_t"]), float(r.get("total_associations") or 0),
            float(r.get("track_state") or 0),
            float(r.get("vE_mps") or "nan"), float(r.get("vN_mps") or "nan"),
            float(r.get("vU_mps") or "nan"),
            float(r.get("sigvE_mps") or "nan"), float(r.get("sigvN_mps") or "nan"),
            float(r.get("sigvU_mps") or "nan")) for r in rows]
    return np.array(sorted(out)) if out else np.zeros((0, TK_W))


def truth_rows_to_array(rows, clean: bool = True) -> np.ndarray:
    """quickdump ``mavlink/mav*.csv`` DictReader rows -> corr_lib 8-col truth
    (t,E,N,U_hae,spd,vN,vE,vU_raw[ft/min]); ``clean`` applies corr_lib.clean_truth."""
    raw = []
    for r in rows:
        if str(r.get("validposition", "1")) in ("0", "False", "false"):
            continue
        vn, ve = float(r["vel_n_mps"]), float(r["vel_e_mps"])
        raw.append((float(r["t_epoch"]), float(r["E_m"]), float(r["N_m"]), float(r["U_m_hae"]),
                    math.hypot(vn, ve), vn, ve, float(r["vert_spd_wire_ftmin"])))
    if not raw:
        return np.zeros((0, 8))
    return corr_lib.clean_truth(raw) if clean else np.array(sorted(raw))


def _as_track_dict(A_or_rows) -> dict[int, np.ndarray]:
    """Accept a 10/13/16-col array, a {track_id: array} dict, or CSV DictReader rows."""
    if isinstance(A_or_rows, dict):
        return {int(k): np.asarray(v, float) for k, v in A_or_rows.items()}
    if isinstance(A_or_rows, np.ndarray):
        return {0: A_or_rows.astype(float)}
    rows = list(A_or_rows)
    if rows and isinstance(rows[0], dict):
        return {0: track_rows_to_array(rows)}
    return {0: np.asarray(rows, float)}


def _with_velocity(A: np.ndarray) -> np.ndarray:
    """Pad a legacy 10-col array with NaN velocities (-> zero velocity in spa)."""
    if A.shape[1] >= 13:
        return A
    return np.column_stack([A, np.full((len(A), 13 - A.shape[1]), np.nan)])


def vel_sigma_real(A: np.ndarray) -> np.ndarray:
    """(n,3) bool: True where the track array carries a finite, positive per-row velocity 1σ
    (cols 13..15 = sigvE, sigvN, sigvU) — the rows whose spa velocity σ is REAL.  Everywhere else
    ``build_tracks_raw`` uses the constant ``vel_sigma_mps`` fill and ``errs_from_metrics`` reports NaN."""
    A = np.asarray(A, float)
    if A.ndim != 2 or A.shape[1] < TK_W:
        return np.zeros((len(A), 3), bool)
    s = A[:, VEL_SIGMA_COLS]
    return np.isfinite(s) & (s > 0)


def vel_provenance(tracks: dict[int, np.ndarray]) -> dict[int, dict]:
    """{track_id: {"t": (n,) publish times, "sigma": (n,3) bool real velocity σ, "vel": (n,3) bool finite velocity}}
    aligned with the INPUT rows — what ``errs_from_metrics`` needs to NaN the fabricated velocity numbers
    (constant-fill σ; zero-filled NaN velocities) on spa's graded rows."""
    out = {}
    for tid, A in tracks.items():
        A = _with_velocity(np.asarray(A, float))
        if not len(A):
            continue
        out[int(tid)] = {"t": A[:, 0].copy(), "sigma": vel_sigma_real(A), "vel": np.isfinite(A[:, 10:13])}
    return out


def _rows_flags(df: pl.DataFrame, prov: dict[int, dict] | None, key: str) -> np.ndarray:
    """(len(df),3) bool: the ``key`` flags of the input row each graded (track_id, t) row came from;
    False when the provenance is unknown or no input row sits within T_MATCH_TOL_S of spa's t."""
    n = df.height
    out = np.zeros((n, 3), bool)
    if not prov or n == 0:
        return out
    tids = df["track_id"].to_numpy().astype(int)
    ts = df["t"].cast(pl.Float64).to_numpy().astype(float)
    for tid in np.unique(tids):
        p = prov.get(int(tid))
        if p is None:
            continue
        order = np.argsort(p["t"], kind="stable")
        pt, flags = p["t"][order], np.asarray(p[key], bool)[order]
        rows = np.flatnonzero(tids == tid)
        j = np.clip(np.searchsorted(pt, ts[rows]), 0, len(pt) - 1)
        jm = np.clip(j - 1, 0, len(pt) - 1)
        j = np.where(np.abs(pt[jm] - ts[rows]) < np.abs(pt[j] - ts[rows]), jm, j)
        hit = np.abs(pt[j] - ts[rows]) <= T_MATCH_TOL_S
        out[rows[hit]] = flags[j[hit]]
    return out


# ── truth interpolation (spa-equivalent linear, optional gap guard) ─────────

def interp_truth_linear(T: np.ndarray, ts: np.ndarray, max_gap_s: float | None = None):
    """Linear interpolation of truth cols 1..7 at times ``ts`` -> (vals (n,7), ok).

    Linear-in-ENU == linear-in-ECEF (affine), i.e. spa's ``interp1d`` recorrelate
    interpolation. Never extrapolates. ``max_gap_s`` enforces
    corr_lib.interp_truth's rule (both bracketing samples within max_gap_s)."""
    ts = np.asarray(ts, float)
    n = len(ts)
    if len(T) < 2:
        return np.full((n, 7), np.nan), np.zeros(n, bool)
    tt, uniq = np.unique(T[:, 0], return_index=True)
    T = T[uniq]
    ok = (ts >= tt[0]) & (ts <= tt[-1])
    if max_gap_s is not None:
        i = np.clip(np.searchsorted(tt, ts), 1, len(tt) - 1)
        ok &= ((ts - tt[i - 1]) <= max_gap_s) & ((tt[i] - ts) <= max_gap_s)
        ok &= np.searchsorted(tt, ts) > 0          # corr_lib drops ts == T[0] exactly
    vals = np.column_stack([np.interp(ts, tt, T[:, c]) for c in range(1, 8)])
    return vals, ok


# ── spa raw-frame builders ───────────────────────────────────────────────────

def _fresh_mask(A: np.ndarray) -> np.ndarray:
    """True where last_update_time (col 7) changed vs the previous row = UPDATED."""
    return np.concatenate([[True], np.diff(A[:, 7]) > 1e-6])


def build_tracks_raw(tracks: dict[int, np.ndarray], T: np.ndarray, ant, *,
                     target_id: str = DEFAULT_TARGET_ID, coast_type: str = "predicted",
                     vel_sigma_mps: float = 5.0, max_gap_s: float | None = None,
                     job_bin_s: float = 0.5) -> pl.DataFrame:
    """TRACKS_RAW v4 frame for spa from corr_lib track arrays + HAE truth array.
    Velocity covariance: per-row 1σ from cols 13..15 (sigvE, sigvN, sigvU) where finite and > 0
    (``vel_sigma_real``), else ``vel_sigma_mps`` (spa needs a positive σ); ENU order [vE, vN, vU]
    before the ENU -> ECEF rotation, so spa's e_dot_sigma / n_dot_sigma / u_dot_sigma read back
    exactly sigvE / sigvN / sigvU on the real rows."""
    lat0, lon0, h0 = float(ant[0]), float(ant[1]), float(ant[2])
    vu = corr_lib.vu_scale(T) if len(T) else corr_lib.VS_FTMIN_TO_MPS
    parts = []
    for tid, A in sorted(tracks.items()):
        A = _with_velocity(np.asarray(A, float))
        if not len(A):
            continue
        n = len(A)
        t = A[:, 0]
        lat, lon, alt = pm.enu2geodetic(A[:, 1], A[:, 2], A[:, 3], lat0, lon0, h0, deg=True)
        v = np.nan_to_num(A[:, 10:13], nan=0.0)
        states = lla_vel_to_ecef_state(lat, lon, alt, v[:, 0], v[:, 1], v[:, 2])
        r3 = np.array([ecef_to_enu_rotation_matrix(float(la), float(lo)) for la, lo in zip(lat, lon)])
        r6 = np.zeros((n, 6, 6)); r6[:, :3, :3] = r3; r6[:, 3:, 3:] = r3
        c = np.zeros((n, 6, 6))
        c[:, 0, 0], c[:, 1, 1], c[:, 2, 2] = A[:, 4] ** 2, A[:, 5] ** 2, A[:, 6] ** 2
        sv = np.full((n, 3), float(vel_sigma_mps))                 # velocity 1σ [vE, vN, vU]: per-row where REAL, else the fill
        real = vel_sigma_real(A)
        if A.shape[1] >= TK_W:
            sv[real] = A[:, VEL_SIGMA_COLS][real]
        c[:, 3, 3], c[:, 4, 4], c[:, 5, 5] = sv[:, 0] ** 2, sv[:, 1] ** 2, sv[:, 2] ** 2
        b_ecef = np.einsum("nji,njk,nkl->nil", r6, c, r6)      # R^T C R
        # embedded truth match
        tr, ok = interp_truth_linear(T, t, max_gap_s)
        tlat, tlon, talt = pm.enu2geodetic(tr[:, 0], tr[:, 1], tr[:, 2], lat0, lon0, h0, deg=True)
        vs_ftmin = tr[:, 6] * vu / FPM_TO_MPS                  # wire -> m/s -> spa ft/min
        opt = lambda x: [float(a) if m else None for a, m in zip(x, ok)]  # noqa: E731
        fresh = _fresh_mask(A)
        ttype = np.where(fresh, int(TrackType.UPDATED),
                         int(TrackType.UPDATED) if coast_type == "updated" else int(TrackType.PREDICTED))
        full = np.floor(t).astype(np.int64)
        parts.append(pl.DataFrame({
            "track_id": pl.Series(np.full(n, tid), dtype=pl.Int64),
            "track_state": pl.Series(A[:, 9].astype(np.int64), dtype=pl.Int64),
            "track_type": pl.Series(ttype.astype(np.int64), dtype=pl.Int64),
            "timestamp_full_sec": pl.Series(full, dtype=pl.Int64),
            "timestamp_frac_sec": pl.Series(t - full, dtype=pl.Float64),
            "job_id": pl.Series(np.floor(t / job_bin_s).astype(np.int64), dtype=pl.Int64),
            "x_state_ecef": pl.Series([s.tolist() for s in states], dtype=pl.List(pl.Float64)),
            "p_cov_ecef": pl.Series([b.tolist() for b in b_ecef], dtype=pl.List(pl.List(pl.Float64))),
            "truth_match_target_id": pl.Series([target_id if m else None for m in ok], dtype=pl.Utf8),
            "truth_match_lat_deg": pl.Series(opt(tlat), dtype=pl.Float64),
            "truth_match_lon_deg": pl.Series(opt(tlon), dtype=pl.Float64),
            "truth_match_altitude": pl.Series(opt(talt / M_PER_FT), dtype=pl.Float64),   # HAE, FEET
            "truth_match_velocity_n_mps": pl.Series(opt(tr[:, 4]), dtype=pl.Float64),
            "truth_match_velocity_e_mps": pl.Series(opt(tr[:, 5]), dtype=pl.Float64),
            "truth_match_vertical_speed": pl.Series(opt(vs_ftmin), dtype=pl.Float64),    # ft/min
        }))
    if not parts:
        raise ValueError("no track rows")
    return validate(pl.concat(parts), TRACKS_RAW)


def build_truth_raw(T: np.ndarray, ant, *, target_id: str = DEFAULT_TARGET_ID) -> pl.DataFrame:
    """AIR_TRAFFIC_RAW v1 frame (altitude = HAE in FEET, see module docstring)."""
    lat0, lon0, h0 = float(ant[0]), float(ant[1]), float(ant[2])
    lat, lon, alt = pm.enu2geodetic(T[:, 1], T[:, 2], T[:, 3], lat0, lon0, h0, deg=True)
    vu = corr_lib.vu_scale(T)
    n = len(T)
    frame = pl.DataFrame({
        "target_id": pl.Series([target_id] * n, dtype=pl.Utf8),
        "source": pl.Series(["MAVLINK"] * n, dtype=pl.Utf8),
        "timestamp": pl.Series(T[:, 0], dtype=pl.Float64),
        "lat": pl.Series(lat, dtype=pl.Float64),
        "lon": pl.Series(lon, dtype=pl.Float64),
        "altitude": pl.Series(alt / M_PER_FT, dtype=pl.Float64),
        "heading": pl.Series(np.degrees(np.arctan2(T[:, 6], T[:, 5])) % 360.0, dtype=pl.Float64),
        "speed": pl.Series(np.hypot(T[:, 5], T[:, 6]) / KNOT, dtype=pl.Float64),
        "vertical_speed": pl.Series(np.nan_to_num(T[:, 7], nan=0.0) * vu / FPM_TO_MPS, dtype=pl.Float64),
        "validposition": pl.Series(np.ones(n, np.int64), dtype=pl.Int64),
        "heading_valid": pl.Series(np.ones(n, np.int64), dtype=pl.Int64),
    })
    return validate(frame, AIR_TRAFFIC_RAW)


def build_obs_anchor_raw(t0: float, t1: float, ant, *, node_id: int = DEFAULT_NODE_ID,
                         job_bin_s: float = 0.5) -> pl.DataFrame:
    """Two SYNTHETIC unmatched obs rows carrying the monostatic antenna geometry.

    Not detections: they only give spa the replay window and the (tx, rx) link
    (sensor_nodes centroid = antenna ECEF at HAE) for measurement-space grading."""
    t = np.array([float(t0) - 0.1, float(t1) + 0.1])
    n = 2
    ax, ay, az = pm.geodetic2ecef(float(ant[0]), float(ant[1]), float(ant[2]), deg=True)
    centroid = {"x": float(ax), "y": float(ay), "z": float(az)}
    nodes = [{"node_id": node_id, "node_name": f"mru{node_id}-rxtx",
              "antenna_groups": [{"antenna_group_id": 0, "centroid_loc_ecef": centroid}]}]
    beams = [{"beam_id": 0, "beam_type": bt, "node_id": node_id, "antenna_group_id": 0,
              "beam_center_az_rad": 0.0, "beam_center_el_rad": 0.0,
              "beam_3db_az_width_rad": 2 * math.pi, "beam_3db_el_width_rad": math.pi} for bt in (0, 1)]
    full = np.floor(t).astype(np.int64)
    nul = pl.Series([None] * n, dtype=pl.Float64)
    info = ("range_m", "bi_range_m", "range_rate_ms", "bi_range_rate_ms", "az_deg", "el_deg", "cone_deg")
    frame = pl.DataFrame({
        "job_id": pl.Series(np.floor(t / job_bin_s).astype(np.int64), dtype=pl.Int64),
        "node_id": pl.Series(np.full(n, node_id), dtype=pl.Int64),
        "obs_id": pl.Series(np.arange(n), dtype=pl.Int64),
        "timestamp_full_sec": pl.Series(full, dtype=pl.Int64),
        "timestamp_frac_sec": pl.Series(t - full, dtype=pl.Float64),
        "amb_rng_km": pl.Series(np.full(n, 10.9)), "amb_bistatic_rng_km": pl.Series(np.full(n, 21.8)),
        "amb_dop_ms": pl.Series(np.zeros(n)), "amb_bistatic_rng_rate_ms": pl.Series(np.zeros(n)),
        "az_rad": pl.Series(np.zeros(n)), "el_rad": pl.Series(np.full(n, 0.05)),
        "snr_db": pl.Series(np.full(n, 20.0)), "rng_var_m": pl.Series(np.full(n, 25.0)),
        "dop_var_ms": pl.Series(np.ones(n)), "az_var_rad": pl.Series(np.full(n, 1e-4)),
        "el_var_rad": pl.Series(np.full(n, 1e-4)),
        "truth_target_target_id": pl.Series([None] * n, dtype=pl.Utf8),
        "truth_target_match_type": pl.Series([None] * n, dtype=pl.Utf8),
        **{f"truth_target_truth_info_{f}": nul for f in info},
        **{f"truth_target_error_info_{f}": nul for f in info},
        "beam_id": pl.Series(np.zeros(n, np.int64), dtype=pl.Int64),
        "sensor_nodes": pl.Series([nodes] * n, dtype=_SENSOR_NODES_DTYPE),
        "beams": pl.Series([beams] * n, dtype=_BEAMS_DTYPE),
    })
    return validate(frame, OBS_RAW)


# ── grading ──────────────────────────────────────────────────────────────────

@dataclass
class SpaGrading:
    metrics: GradedTrackMetrics
    tracks_raw: pl.DataFrame
    truth_raw: pl.DataFrame
    obs_raw: pl.DataFrame
    options: dict = field(default_factory=dict)


def spa_grade(A_or_rows, T: np.ndarray, ant, *, target_id: str = DEFAULT_TARGET_ID,
              node_id: int = DEFAULT_NODE_ID, coast_type: str = "predicted",
              use_tentative: bool = False, use_extrapolated: bool = False,
              max_gap_s: float | None = None, vel_sigma_mps: float = 5.0,
              load_all_truth: bool = True) -> SpaGrading:
    """Frames -> ``spa.tracks.grade`` (recorrelate_tracks=False, embedded truth).
    See the module docstring for every option."""
    tracks = _as_track_dict(A_or_rows)
    T = np.asarray(T, float)
    tracks_raw = build_tracks_raw(tracks, T, ant, target_id=target_id, coast_type=coast_type,
                                  vel_sigma_mps=vel_sigma_mps, max_gap_s=max_gap_s)
    t_all = np.concatenate([np.asarray(A)[:, 0] for A in tracks.values()])
    obs_raw = build_obs_anchor_raw(t_all.min(), t_all.max(), ant, node_id=node_id)
    truth_raw = build_truth_raw(T, ant, target_id=target_id) if load_all_truth else None
    metrics = grade(tracks_raw, obs_raw=obs_raw, truth_raw=truth_raw, load_all_truth=load_all_truth,
                    recorrelate_tracks=False, use_tentative=use_tentative,
                    use_extrapolated=use_extrapolated)
    return SpaGrading(metrics, tracks_raw, truth_raw, obs_raw,
                      dict(target_id=target_id, node_id=node_id, coast_type=coast_type,
                           use_tentative=use_tentative, use_extrapolated=use_extrapolated,
                           max_gap_s=max_gap_s, vel_sigma_mps=vel_sigma_mps,
                           load_all_truth=load_all_truth,
                           # per-track, per-INPUT-row provenance of the velocity numbers: which rows carried a real
                           # velocity σ (else the vel_sigma_mps fill) and a finite velocity (else spa graded 0 m/s)
                           vel_sigma_real=vel_provenance(tracks)))


_VEL_COLS = ("e_dot_errors", "n_dot_errors", "u_dot_errors", "e_dot_sigma", "n_dot_sigma", "u_dot_sigma")


def errs_from_metrics(metrics: GradedTrackMetrics, vel_sigma_real: dict[int, dict] | None = None) -> dict:
    """corr_df + corr_meas_df -> err_stack-compatible dict (spa's numbers, unchanged — except the velocity
    provenance rule).  ``vel_sigma_real`` = ``SpaGrading.options["vel_sigma_real"]`` (``vel_provenance``): the
    velocity sigmas sig_ve / sig_vn / sig_vu are NaN on every graded row whose input σ was the constant fill,
    and the velocity errors ve_err / vn_err / vu_err are NaN where the input velocity was NaN (spa graded a
    zero velocity).  Without the dict (None) every velocity σ is NaN (no provenance, no σ) and the
    velocity errors are spa's raw numbers."""
    corr, meas = metrics.corr_df, metrics.corr_meas_df
    if corr.is_empty():
        return {k: np.zeros(0) for k in ERR_KEYS} | {"track_id": np.zeros(0, int), "update_id": np.zeros(0, int)}
    keep = ["track_id", "update_id", "t", "e_errors", "n_errors", "u_errors", "e_sigma", "n_sigma", "u_sigma"]
    extra = [c for c in ("errors_3d", "pos_eig1_sigma", "pos_eig2_sigma", "pos_eig3_sigma",
                         "pos_eig1_containment", "pos_eig2_containment", "pos_eig3_containment", *_VEL_COLS) if c in corr.columns]
    df = corr.select(keep + extra)
    for c in ("errors_3d", "pos_eig1_sigma", "pos_eig2_sigma", "pos_eig3_sigma", "pos_eig1_containment", "pos_eig2_containment", "pos_eig3_containment",
              *_VEL_COLS):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    if not meas.is_empty():
        m = meas.select(["track_id", "update_id", "az_error_deg", "el_error_deg", "mono_rng_error_km",
                         "az_sigma_deg", "el_sigma_deg", "bi_rng_sigma_m", "tx_node_id", "rx_node_id"])
        df = df.join(m, on=["track_id", "update_id"], how="left")
    else:
        df = df.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in
                              ("az_error_deg", "el_error_deg", "mono_rng_error_km",
                               "az_sigma_deg", "el_sigma_deg", "bi_rng_sigma_m")]
                             + [pl.lit(None, dtype=pl.Int64).alias("tx_node_id"),
                                pl.lit(None, dtype=pl.Int64).alias("rx_node_id")])
    df = df.sort(["t", "track_id"])
    f = lambda c: df[c].cast(pl.Float64).to_numpy().astype(float)  # noqa: E731
    mono = (df["tx_node_id"] == df["rx_node_id"]).fill_null(False).to_numpy()
    sig_rng = np.where(mono, f("bi_rng_sigma_m") / 2.0, np.nan)
    pos3d = f("errors_3d")
    if not np.isfinite(pos3d).any():   # older spa without errors_3d: the same magnitude from the ENU components
        pos3d = np.sqrt(f("e_errors") ** 2 + f("n_errors") ** 2 + f("u_errors") ** 2)
    sig3 = np.sqrt(f("pos_eig1_sigma") ** 2 + f("pos_eig2_sigma") ** 2 + f("pos_eig3_sigma") ** 2)
    if not np.isfinite(sig3).any():    # no eigen sigmas: trace from the diagonal sigmas (identical quantity)
        sig3 = np.sqrt(f("e_sigma") ** 2 + f("n_sigma") ** 2 + f("u_sigma") ** 2)
    sig3 = np.where(sig3 > 0, sig3, np.nan)   # a zero covariance is "unknown", never "perfect"
    # velocity states: spa's e_dot/n_dot/u_dot (track − truth, ENU, m/s); σ only where the INPUT row carried a real one
    v_err = np.column_stack([f("e_dot_errors"), f("n_dot_errors"), f("u_dot_errors")])
    v_sig = np.column_stack([f("e_dot_sigma"), f("n_dot_sigma"), f("u_dot_sigma")])
    v_sig = np.where(v_sig > 0, v_sig, np.nan)
    if vel_sigma_real is None:                                                     # no provenance: no σ (errors stay spa's raw numbers)
        v_sig = np.full_like(v_sig, np.nan)
    else:
        v_sig = np.where(_rows_flags(df, vel_sigma_real, "sigma"), v_sig, np.nan)  # the constant fill never leaves the adapter
        v_err = np.where(_rows_flags(df, vel_sigma_real, "vel"), v_err, np.nan)    # a NaN input velocity was graded as 0 m/s: not an error
    return {"t": f("t"), "az_err": f("az_error_deg"), "el_err": f("el_error_deg"),
            "rng_err": f("mono_rng_error_km") * 1000.0, "alt_err": f("u_errors"),
            "sig_az": f("az_sigma_deg"), "sig_el": f("el_sigma_deg"), "sig_rng": sig_rng,
            "sig_alt": f("u_sigma"), "e_err": f("e_errors"), "n_err": f("n_errors"), "u_err": f("u_errors"),
            "pos3d_err": pos3d, "sig_pos3d": sig3,
            "pos_eig1_cont": f("pos_eig1_containment"), "pos_eig2_cont": f("pos_eig2_containment"), "pos_eig3_cont": f("pos_eig3_containment"),
            "ve_err": v_err[:, 0], "vn_err": v_err[:, 1], "vu_err": v_err[:, 2],
            "sig_ve": v_sig[:, 0], "sig_vn": v_sig[:, 1], "sig_vu": v_sig[:, 2],
            "track_id": df["track_id"].to_numpy().astype(int),
            "update_id": df["update_id"].to_numpy().astype(int)}


def spa_err_stack(A_or_rows, T: np.ndarray, ant, **opts) -> dict:
    """err_stack-compatible dict of spa's per-update errors (see module docstring).

    ``A_or_rows``: corr_lib track array (10, 13 or 16 cols — 16 = with per-row velocity
    sigmas), {track_id: array}, or quickdump CSV DictReader rows.  ``T``: corr_lib 8-col
    truth (U = HAE m).  ``ant``: (lat_deg, lon_deg, HAE_m) antenna = tracker NED origin."""
    g = spa_grade(A_or_rows, T, ant, **opts)
    return errs_from_metrics(g.metrics, g.options.get("vel_sigma_real"))


# ── summaries (spa's own functions) ──────────────────────────────────────────

_MEAS_DIMS = ("az_err", "el_err", "rng_err")             # meas-summary convention
_POS_DIMS = ("alt_err", "e_err", "n_err", "u_err")       # corr_df roll-up convention


def spa_summary(errs: dict) -> dict:
    """{dim: {n, mean, std, rmse, rmse95, convention}} via spa's summary helpers.

    az/el/rng -> ``spa.tracks.summaries._stats_for_meas_summary_series`` (ddof=1,
    nearest-quantile 95.5 % trim) = the numbers in spa's ``meas_summary_df``;
    alt/e/n/u -> ``_roll_up_error_field`` (ddof=0, linear-percentile trim) = spa's
    ``target_summary_df``/``summary_df``.  ``rmse`` is the plain untrimmed RMSE
    (spa's obs grading convention; the track summaries do not report it)."""
    out = {}
    for dim in _MEAS_DIMS + _POS_DIMS:
        if dim not in errs:
            continue
        x = np.asarray(errs[dim], float)
        x = x[np.isfinite(x)]
        if not len(x):
            out[dim] = {"n": 0, "mean": None, "std": None, "rmse": None, "rmse95": None}
            continue
        if dim in _MEAS_DIMS:
            mean, std, rmse95 = _stats_for_meas_summary_series(pl.Series(x))
            conv = "spa meas_summary: std ddof=1, rmse95 nearest-quantile 95.5%"
        else:
            mean, std, rmse95 = _roll_up_error_field(x.tolist())
            conv = "spa target_summary: std ddof=0, rmse95 linear-percentile 95.5%"
        out[dim] = {"n": int(len(x)), "mean": mean, "std": std,
                    "rmse": float(np.sqrt(np.mean(x ** 2))), "rmse95": rmse95, "convention": conv}
    return out


__all__ = ["spa_err_stack", "spa_summary", "spa_grade", "errs_from_metrics", "SpaGrading",
           "build_tracks_raw", "build_truth_raw", "build_obs_anchor_raw", "interp_truth_linear",
           "track_rows_to_array", "truth_rows_to_array", "calc_trimmed_rmse", "ERR_KEYS",
           "vel_sigma_real", "vel_provenance", "TK_W", "VEL_SIGMA_COLS"]
