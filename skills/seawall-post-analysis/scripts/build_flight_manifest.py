#!/usr/bin/env python
"""build_flight_manifest.py - per-day flights.json (+ DAY_SUMMARY.md skeleton)
from archived radar-run dumps (seawall_archiver / quickdump CSV layout).

Input:  one or more dump dirs for the SAME day/run, each with
          mavlink/<target_id>.csv   truth feeds (t_epoch, lat, lon, alt_ft_wire,
                                    E_m, N_m, U_m_hae, [speed_mps], vel_n_mps,
                                    vel_e_mps, ...) - ENU about the run antenna,
                                    U in WGS-84 HAE (same frame as tracks)
          tracks/track_<id>.csv     radar track states (E_m,N_m,U_m,...,
                                    last_update_t; unchanged l_u_t == coasting)
          meta.json                 antenna ("antenna_origin_lat_lon_haeM" or
                                    "antenna"), run id ("run_id8" or "run")
Output: <out>/flights.json + <out>/DAY_SUMMARY.md (tables only, narrative TODO)

Method (also emitted in flights.json["method"] with the live parameters):
  target       = feeds matching --target, concatenated, dedup on 0.5 s rounded t;
                 frozen runs > 5 min (dead feed) dropped keeping the first row,
                 shorter hover republishes kept
  interceptor  = feeds matching --interceptor; per feed, frozen runs (>=3
                 identical lat/lon/alt) dropped keeping the first; merged,
                 dedup 0.5 s, >250 m/s teleports dropped
  ground       = median U of parked (<1 m/s) samples of the RAW target feed
  flights      = target airborne (U > ground+5 m at speed > 0.5, or speed > 2)
                 samples split on gaps > 5 min; window trimmed to first/last
                 U > ground+5 sample (--trim-speed X also extends to speed > X);
                 windows whose peak speed < 5 m/s are GPS-acquisition noise and
                 skipped; interceptor sorties with no target flight are emitted
                 as interceptor-only flights (roles swapped, no passes)
  segments     = engagement day: climbout/transit | engagement (interceptor
                 airborne window, same flight rule) | return/land; tracking day:
                 racetrack loops split at the range trough between range apexes
  passes       = 1 Hz grid, 3D separation of interpolated feeds, no
                 interpolation across >2.5 s gaps, local minima < 600 m with
                 80 m prominence per contiguous segment (gap-edge minima with
                 one-sided prominence, and 40-80 m prominence near-misses
                 < 150 m, are kept and flagged); flagged if the
                 interceptor is still grounded; realistic = both >= 3 m/s at the
                 minimum and APPROACH closing speed (90th pct over the 10 s
                 before the minimum, 2 s baselines) >= 8 m/s
  track sides  = target-side if median horiz dist to target < 150 m and
                 <= median dist to interceptor (>=3 truth-valid states);
                 interceptor-side symmetric; on-target set = target-side +
                 riders (>=5 states within 150 m, not interceptor-side)
  coverage     = pct of 1 Hz moving samples (U > ground+5 AND speed > 2 m/s)
                 with a fresh (t - last_update_t <= 1.5 s) on-target state
                 within 1.5 s and 150 m (+ whole-window variant incl. hover)
  steals       = any track born >=10 s before a pass with >=5 states in the
                 120 s before it sitting on craft X whose post-pass (<=120 s)
                 median distance flips to
                 within 120 m of craft Y (both directions are "track steals")
  anomalies    = clutter (matched but never moved: path < 200 m and speed >= 2
                 in < 50 % of matched states), drag-and-die (> 100 m off within
                 30 s after a pass, dead within 30 s), corruption (bias-removed
                 residual > max(2 x median, 80 m) for >= 3 s within +-10 s of a
                 pass, then returns), pinned altitude (>= 30 s constant U while
                 truth varies > 10 m or the track jitters > 5 m elsewhere),
                 speed-filter kill (last state |v| > 100 or |vU| > 50 followed
                 by a hole), feed freezes (>= 10 s listed; >= 5 s within +-30 s
                 of a pass -> pass unreliable; moving freezes dead-reckoned)
  kind         = per flight (engagement = interceptor airborne + a realistic
                 pass); t0_liftoff/t1_touchdown = |vs| > 0.5 or speed > 1 edges
  metrics      = fresh on-target states: median/p90 horiz err, az-bias-removed
                 median, az/el/alt bias (medians), n_measurements = distinct
                 last_update_t, riders, handovers (rider-chain id changes with
                 overlap < 10 s), coverage gaps >= 3 s, per-gap cause hints
                 (turning > 8 deg/s within +-3 s, AGL < 20 m, interceptor
                 < 200 m, else straight cruise)

Usage:
  micromamba run -n sensorenv python build_flight_manifest.py --day 2026-08-28 \
      --dumps DIR [DIR ...] [--target 'mav*_1_*'] [--interceptor 'mav*_2_*'|none]
      [--type auto|tracking|engagement] [--tz America/Los_Angeles] [--out DIR]
      [--trim-speed 1.0] [--windows HH:MM:SS-HH:MM:SS,...]
"""
from __future__ import annotations

import argparse, fnmatch, glob, json, math, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

P = dict(dedup_s=0.5, freeze_min_run=3, teleport_mps=250.0, flight_gap_s=300.0,
         agl_air_m=5.0, moving_mps=2.0, parked_mps=1.0, drift_mps=0.5, min_flight_s=30.0,
         min_peak_mps=5.0, trim_speed_mps=0.0, pass_max_m=600.0, pass_prom_m=80.0, pass_sub_prom_m=40.0, pass_sub_max_m=150.0,
         interp_gap_s=2.5, fresh_s=1.5, cover_dt_s=1.5, cover_r_m=150.0, side_r_m=150.0,
         side_min_n=3, rider_min_n=5, steal_pre_n=5, steal_pre_s=120.0, steal_min_age_s=10.0, steal_post_s=120.0,
         steal_r_m=120.0, real_spd_mps=3.0, real_close_mps=8.0, gap_min_s=3.0,
         turn_rate_dps=8.0, pad_agl_m=20.0, interc_near_m=200.0, apex_prom_m=500.0,
         track_pad_s=15.0, handover_overlap_s=10.0,
         # round-2 anomaly detectors
         freeze_flag_s=5.0, freeze_list_s=10.0, freeze_pass_win_s=30.0, rider_path_m=200.0, rider_vfrac=0.5,
         drag_win_s=30.0, drag_off_m=100.0, drag_die_s=30.0, corr_win_s=10.0, corr_min_s=3.0, corr_floor_m=80.0,
         pin_win_s=30.0, pin_step_m=0.5, pin_std_m=1.0, pin_truth_var_m=10.0, pin_std_else_m=5.0,
         kill_v_mps=100.0, kill_vu_mps=50.0, kill_gap_s=5.0, close_win_s=10.0, close_base_s=2.0,
         lift_vs_mps=0.5, lift_spd_mps=1.0)
TZ = ZoneInfo("America/Los_Angeles")


def iso(t): return datetime.fromtimestamp(float(t), TZ).isoformat(timespec="microseconds")
def hms(t): return datetime.fromtimestamp(float(t), TZ).strftime("%H:%M:%S")
def rnd(x, d=1): return None if x is None or not np.isfinite(x) else round(float(x), d)
def wrap_deg(a): return (np.asarray(a) + 180.0) % 360.0 - 180.0
def finite_lt(a, r): return np.nan_to_num(a, nan=np.inf) < r


# --------------------------------------------------------------------------- feeds
class Feed:
    """Truth feed as sorted numpy columns with gap-aware linear interpolation."""
    COLS = ("E", "N", "U", "spd", "vN", "vE")

    def __init__(self, df: pd.DataFrame, name: str, ground: float | None = None):
        self.name, self.df = name, df.reset_index(drop=True)
        self.t = df.t_epoch.to_numpy(float)
        self.E, self.N, self.U = (df[c].to_numpy(float) for c in ("E_m", "N_m", "U_m_hae"))
        self.spd, self.vN, self.vE = (df[c].to_numpy(float) for c in ("speed_mps", "vel_n_mps", "vel_e_mps"))
        self.vs = df.vert_spd_wire_ftmin.to_numpy(float) * 0.00508 if "vert_spd_wire_ftmin" in df.columns \
            else (np.gradient(self.U, self.t) if len(self.t) > 1 else np.zeros(len(self.t)))
        self.ground = self.pad_ground(df) if ground is None else ground
        self.freezes = []                       # [(t0, t1, n_rows, feed_id, dead_reckoned)] raw frozen runs (set by builders)

    @staticmethod
    def pad_ground(df):
        """median U of parked (< parked_mps) samples - computed on the RAW feed
        (frozen parked rows included) so it does not shift with freeze cleaning."""
        low = df.speed_mps.to_numpy() < P["parked_mps"]
        return float(np.median(df.U_m_hae.to_numpy()[low])) if low.sum() >= 5 else float(np.percentile(df.U_m_hae, 5))

    def aloft(self):                       # altitude-airborne
        return self.U > self.ground + P["agl_air_m"]

    def airborne(self):                    # aloft and not parked-GPS altitude drift, or moving
        return (self.aloft() & (self.spd > P["drift_mps"])) | (self.spd > P["moving_mps"])

    def interp(self, tq, max_gap=None):
        """Linear interpolation at tq; NaN where tq is outside the feed or the
        bracketing samples are more than max_gap apart (no bridging of gaps)."""
        max_gap = P["interp_gap_s"] if max_gap is None else max_gap
        tq = np.atleast_1d(np.asarray(tq, float)); n = len(self.t)
        idx = np.searchsorted(self.t, tq, "right")
        prev, nxt = np.clip(idx - 1, 0, n - 1), np.clip(idx, 0, n - 1)
        ok = (tq >= self.t[0]) & (tq <= self.t[-1]) & (
            ((self.t[nxt] - self.t[prev]) <= max_gap) | (np.abs(tq - self.t[prev]) < 1e-6))
        out = {"ok": ok}
        for c in self.COLS:
            v = np.interp(tq, self.t, getattr(self, c)); v[~ok] = np.nan; out[c] = v
        return out


def read_feed_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "speed_mps" not in df.columns:            # quickdumps lack it: derive
        df["speed_mps"] = np.hypot(df.vel_n_mps, df.vel_e_mps)
    ok = (df.get("validposition", 1) != 0) & (df.lat.abs() > 1e-3) & \
         (np.hypot(df.E_m, df.N_m) < 30_000) & (df.U_m_hae.abs() < 5_000)
    return df[ok].sort_values("t_epoch")


def dedup(df: pd.DataFrame) -> pd.DataFrame:
    key = np.round(df.t_epoch / P["dedup_s"]) * P["dedup_s"]
    return df.loc[~key.duplicated(keep="first")].sort_values("t_epoch").reset_index(drop=True)


def frozen_runs(df: pd.DataFrame):
    """Runs of >=freeze_min_run consecutive identical (lat,lon,alt).
    Returns (drop_mask for all-but-first rows, [(t_start, t_end, n_rows)])."""
    key = df.lat.round(7).astype(str) + "|" + df.lon.round(7).astype(str) + "|" + df.alt_ft_wire.round(2).astype(str)
    run = key.ne(key.shift()).cumsum()
    ln = run.map(run.value_counts()); frozen = ln >= P["freeze_min_run"]
    drop = frozen & (df.groupby(run).cumcount() > 0)
    runs = [(g.t_epoch.iloc[0], g.t_epoch.iloc[-1], len(g)) for _, g in df[frozen].groupby(run[frozen])]
    return drop.to_numpy(), runs


def drop_teleports(df: pd.DataFrame):
    t, E, N = df.t_epoch.to_numpy(), df.E_m.to_numpy(), df.N_m.to_numpy()
    keep = np.ones(len(df), bool); last = 0
    for i in range(1, len(df)):
        if math.hypot(E[i] - E[last], N[i] - N[last]) / max(t[i] - t[last], 1e-3) > P["teleport_mps"]:
            keep[i] = False
        else:
            last = i
    return df[keep].reset_index(drop=True), int((~keep).sum())


def collect_feeds(dumps, pattern):
    """{target_id: concatenated DataFrame} for mavlink/<id>.csv matching pattern."""
    feeds = {}
    for d in dumps:
        for p in sorted(glob.glob(os.path.join(d, "mavlink", "*.csv"))):
            tid = os.path.splitext(os.path.basename(p))[0]
            if fnmatch.fnmatch(tid, pattern):
                feeds.setdefault(tid, []).append(read_feed_csv(p))
    return {k: pd.concat(v).sort_values("t_epoch") for k, v in feeds.items()}


def build_target(dumps, pattern):
    """Concatenate + dedup the target feeds. Short frozen runs (hover republish
    of the last fix, e.g. 90 s at the pad) are KEPT as in the reference method;
    a freeze longer than the flight-gap threshold is a dead feed (stale republish
    for hours, often with a frozen non-zero wire speed) and is dropped keeping
    its first row, so it can neither look airborne nor be interpolated across."""
    feeds = collect_feeds(dumps, pattern)
    if not feeds:
        sys.exit(f"no target feeds match {pattern!r}")
    df = dedup(pd.concat(feeds.values()))
    drop, runs = frozen_runs(df)
    t = df.t_epoch.to_numpy()
    dead = np.zeros(len(df), bool)
    for s0, s1, _ in runs:
        if s1 - s0 > P["flight_gap_s"]:
            dead |= (t > s0) & (t <= s1)
    drop = drop & dead
    ground, kept = Feed.pad_ground(df), df[~drop].reset_index(drop=True)
    t, fz = kept.t_epoch.to_numpy(), []
    for s0, s1, n in runs:                      # dead-reckon MOVING freezes (flag_s .. gap_s) from the last good fix
        dr = False
        if P["freeze_flag_s"] <= s1 - s0 <= P["flight_gap_s"]:
            i0 = int(np.searchsorted(t, s0))
            if i0 < len(t) and abs(t[i0] - s0) < 1e-3 and kept.speed_mps.iloc[i0] > P["moving_mps"]:
                m = (t > s0) & (t <= s1); dt = t[m] - t[i0]; dr = True
                kept.loc[m, "E_m"] = kept.E_m.iloc[i0] + kept.vel_e_mps.iloc[i0] * dt
                kept.loc[m, "N_m"] = kept.N_m.iloc[i0] + kept.vel_n_mps.iloc[i0] * dt
                if "vert_spd_wire_ftmin" in kept.columns:
                    kept.loc[m, "U_m_hae"] = kept.U_m_hae.iloc[i0] + kept.vert_spd_wire_ftmin.iloc[i0] * 0.00508 * dt
        fz.append((s0, s1, n, "+".join(sorted(feeds)), dr))
    feed = Feed(kept, "+".join(sorted(feeds)), ground); feed.freezes = fz
    return feed, sorted(feeds), runs, int(drop.sum())


def build_interceptor(dumps, pattern):
    feeds = collect_feeds(dumps, pattern) if pattern and pattern.lower() != "none" else {}
    if not feeds:
        return None, {}, 0
    parts, stats, fz = [], {}, []
    for tid, df in feeds.items():
        df = dedup(df); drop, runs = frozen_runs(df)
        fz += [(s0, s1, n, tid, False) for s0, s1, n in runs]
        w = max(runs, key=lambda r: r[1] - r[0], default=None)
        stats[tid] = dict(rows=len(df), stale_dropped=int(drop.sum()), worst_freeze=None if w is None else
                          dict(t0_pdt=hms(w[0]), t1_pdt=hms(w[1]), dur_s=round(w[1] - w[0]), rows=w[2]))
        parts.append(df[~drop])
    merged, n_tele = drop_teleports(dedup(pd.concat(parts)))
    feed = Feed(merged, "+".join(sorted(feeds))); feed.freezes = fz
    return feed, stats, n_tele


def find_flights(F: Feed):
    """Airborne samples split on gaps > flight_gap_s; each group trimmed to its
    first/last aloft (or speed > trim_speed) sample; short or slow windows dropped."""
    ta = F.t[F.airborne()]
    if not len(ta):
        return [], []
    trim = F.aloft() | ((F.spd > P["trim_speed_mps"]) if P["trim_speed_mps"] > 0 else False)
    wins, skipped = [], []
    for g in np.split(np.arange(len(ta)), np.where(np.diff(ta) > P["flight_gap_s"])[0] + 1):
        m = (F.t >= ta[g[0]]) & (F.t <= ta[g[-1]]) & trim
        if m.any() and F.t[m][-1] - F.t[m][0] >= P["min_flight_s"]:
            (wins if F.spd[m].max() >= P["min_peak_mps"] else skipped).append((F.t[m][0], F.t[m][-1]))
    return wins, skipped


# --------------------------------------------------------------------------- tracks
def load_tracks(dumps):
    """{track_id: DataFrame sorted by t, dup states dropped} across all dumps."""
    acc = {}
    for d in dumps:
        for p in glob.glob(os.path.join(d, "tracks", "track_*.csv")):
            acc.setdefault(int(os.path.basename(p)[6:-4]), []).append(pd.read_csv(p))
    out = {tid: pd.concat(v).drop_duplicates("t_epoch").sort_values("t_epoch").reset_index(drop=True) for tid, v in acc.items()}
    return {k: v for k, v in out.items() if len(v)}


class TrackView:
    """One track evaluated against the truth feeds: distances, freshness, side."""

    def __init__(self, tid, df, tgt: Feed, itc: Feed | None):
        self.id, self.t = tid, df.t_epoch.to_numpy(float)
        self.E, self.N, self.U = (df[c].to_numpy(float) for c in ("E_m", "N_m", "U_m"))
        self.lut = df.last_update_t.to_numpy(float)
        self.fresh = (self.t - self.lut) <= P["fresh_s"]
        self.tgt = tgt.interp(self.t)
        self.dT = np.hypot(self.E - self.tgt["E"], self.N - self.tgt["N"])
        i = itc.interp(self.t) if itc is not None else None
        self.dI = np.hypot(self.E - i["E"], self.N - i["N"]) if i else np.full(len(self.t), np.nan)
        self.on_tgt, self.on_itc = finite_lt(self.dT, P["side_r_m"]), finite_lt(self.dI, P["side_r_m"])
        self.vE, self.vN, self.vU = (df[c].to_numpy(float) for c in ("vE_mps", "vN_mps", "vU_mps"))
        dE, dN = self.E - self.tgt["E"], self.N - self.tgt["N"]          # error vector minus its median = residual
        self.res = np.hypot(dE - np.nanmedian(dE), dN - np.nanmedian(dN)) if np.isfinite(dE).any() else np.full(len(self.t), np.nan)
        self.side = self.side_in(np.ones(len(self.t), bool))
        m = self.on_tgt                                                    # rider movement gate (clutter rejection)
        self.path_m = float(np.sum(np.hypot(np.diff(self.E[m]), np.diff(self.N[m])))) if m.sum() > 1 else 0.0
        self.vfrac = float(np.mean(np.hypot(self.vE[m], self.vN[m]) >= P["moving_mps"])) if m.any() else 0.0
        self.moves = self.path_m >= P["rider_path_m"] or self.vfrac >= P["rider_vfrac"]

    def side_in(self, m, r=None):
        """'target' / 'interceptor' / None by median horiz distance over states m."""
        r = P["side_r_m"] if r is None else r
        v = m & (np.isfinite(self.dT) | np.isfinite(self.dI))
        if v.sum() < P["side_min_n"]:
            return None
        mT, mI = self.med(self.dT, v), self.med(self.dI, v)
        return "target" if (mT < r and mT <= mI) else "interceptor" if (mI < r and mI < mT) else None

    def med(self, arr, mask):
        a = arr[mask & np.isfinite(arr)]
        return float(np.median(a)) if len(a) else float("inf")

    def span(self, mask):                  # (first, last, n) over masked states
        return (self.t[mask][0], self.t[mask][-1], int(mask.sum()))


# --------------------------------------------------------------------------- flight analysis
def heading_rate(g, T):
    rate = np.abs(wrap_deg(np.gradient(np.degrees(np.arctan2(T["vE"], T["vN"])), g)))
    return np.where(np.isfinite(rate), rate, 0.0)


def cause_hint(t, grid, hrate, agl, dTI):
    """Drop/handover classification hints at time t (+-3 s)."""
    w = (grid >= t - 3) & (grid <= t + 3); tags = []
    if w.any() and np.nanmax(hrate[w]) > P["turn_rate_dps"]: tags.append("turning")
    if w.any() and np.nanmin(agl[w]) < P["pad_agl_m"]: tags.append("low-alt/pad")
    if w.any() and np.isfinite(dTI[w]).any() and np.nanmin(dTI[w]) < P["interc_near_m"]: tags.append("interceptor<200m")
    return tags or ["straight cruise"]


def approach_closing(sep, i):
    """90th percentile of -d(sep)/dt over the close_win_s before minimum i, close_base_s baselines."""
    b = int(P["close_base_s"])
    rates = [(sep[j] - sep[j + b]) / b for j in range(max(i - int(P["close_win_s"]), 0), i - b + 1)
             if np.isfinite(sep[j]) and np.isfinite(sep[j + b])]
    return float(np.percentile(rates, 90)) if rates else float("nan")


def find_passes(grid, T, I, itc: Feed, freezes=()):
    sep = np.sqrt((T["E"] - I["E"]) ** 2 + (T["N"] - I["N"]) ** 2 + (T["U"] - I["U"]) ** 2)
    horiz, valid, out = np.hypot(T["E"] - I["E"], T["N"] - I["N"]), np.isfinite(sep), []
    for seg in np.split(np.arange(len(grid)), np.where(np.diff(valid.astype(int)) != 0)[0] + 1):
        if not valid[seg[0]] or len(seg) < 2:
            continue
        pk, props = find_peaks(-sep[seg], prominence=P["pass_sub_prom_m"])   # sub-prominence near-misses kept if < pass_sub_max_m
        cands = [(k, prom, False) for k, prom in zip(pk, props["prominences"]) if prom >= P["pass_prom_m"] or sep[seg[k]] < P["pass_sub_max_m"]]
        for k in (0, len(seg) - 1):              # gap-edge minima: one-sided prominence only
            if seg[k] in (0, len(grid) - 1):     # the flight-window boundary is not a feed gap
                continue
            side = sep[seg][:min(len(seg), 10)] if k == 0 else sep[seg][-min(len(seg), 10):]
            prom = float(np.max(side) - sep[seg[k]])
            if prom >= P["pass_prom_m"] and sep[seg[k]] == np.min(side) and not any(abs(c[0] - k) <= 3 for c in cands):
                cands.append((k, prom, True))
        for k, prom, edge in sorted(cands):
            i, j = seg[k], seg[max(k - 3, 0)]
            if sep[i] >= P["pass_max_m"]:
                continue
            closing_min = (sep[j] - sep[i]) / max(grid[i] - grid[j], 1.0)
            closing = approach_closing(sep, i)
            grounded = bool(I["U"][i] < itc.ground + P["agl_air_m"])
            realistic = bool(min(T["spd"][i], I["spd"][i]) >= P["real_spd_mps"] and closing >= P["real_close_mps"])
            tp = grid[i]
            fzn = [f for f in freezes if f[1] - f[0] >= P["freeze_flag_s"] and f[0] < tp + P["freeze_pass_win_s"] and f[1] > tp - P["freeze_pass_win_s"]]
            fz = max(fzn, key=lambda f: f[1] - f[0], default=None)
            note = ["computed offline (truth-truth 3D minimum)"]
            if fz: note.append(f"FROZEN FEED {fz[3]} {hms(fz[0])}-{hms(fz[1])} ({fz[1] - fz[0]:.0f} s) within +-{P['freeze_pass_win_s']:.0f} s - "
                               + ("dead-reckoned through the freeze; " if fz[4] else "") + "CPA unreliable")
            if edge: note.append("at the edge of a feed gap: one-sided prominence, true minimum may lie inside the gap")
            if prom < P["pass_prom_m"]: note.append(f"sub-prominence near-miss (prominence {prom:.0f} m < {P['pass_prom_m']:.0f} m gate; tail-chase convergence)")
            if grounded: note.append(f"interceptor still grounded (iU={I['U'][i]:.1f}) - dip vs parked interceptor")
            elif prom < 100: note.append(f"prominence {prom:.0f} m (threshold-marginal)")
            if not realistic: note.append("fails realistic gate")
            out.append(dict(n=len(out) + 1, t_pdt=hms(grid[i]), t_epoch=round(float(grid[i]), 1), miss_m=int(round(sep[i])),
                            horiz_miss_m=int(round(horiz[i])), prominence_m=int(round(prom)),
                            source="truth-truth (FROZEN FEED - unreliable)" if fz else "truth-truth",
                            frozen_feed=None if fz is None else dict(feed=fz[3], t0_pdt=hms(fz[0]), t1_pdt=hms(fz[1]), dur_s=round(fz[1] - fz[0], 1), dead_reckoned=fz[4]),
                            interceptor_grounded=grounded, realistic=realistic, at_gap_edge=edge, sub_prominence=bool(prom < P["pass_prom_m"]),
                            tgt_speed_mps=rnd(T["spd"][i]), itc_speed_mps=rnd(I["spd"][i]),
                            closing_speed_mps=rnd(closing), closing_at_min_mps=rnd(closing_min), note="; ".join(note)))
    return out


def liftoff_edges(F: Feed, t0, t1):
    """Extend [t0,t1] outward through contiguous samples with |vertical speed| > lift_vs or speed > lift_spd."""
    act = (np.abs(F.vs) > P["lift_vs_mps"]) | (F.spd > P["lift_spd_mps"])
    i = int(np.clip(np.searchsorted(F.t, t0), 0, len(F.t) - 1))
    while i > 0 and act[i - 1] and F.t[i] - F.t[i - 1] <= 3: i -= 1
    j = int(np.clip(np.searchsorted(F.t, t1, "right") - 1, 0, len(F.t) - 1))
    while j < len(F.t) - 1 and act[j + 1] and F.t[j + 1] - F.t[j] <= 3: j += 1
    return float(F.t[i]), float(F.t[j])


def analyse_flight(t0, t1, tgt, itc, tracks, kind, itc_wins=()):
    """Full per-flight record: segments, passes, tracking metrics, steals, anomalies, issues.
    kind: 'auto' (decided per flight) | 'engagement' | 'tracking' | 'solo' (interceptor-only sortie)."""
    grid = np.arange(math.ceil(t0), math.floor(t1) + 1, dtype=float)
    T = tgt.interp(grid)
    I = itc.interp(grid) if itc is not None else None
    agl, hrate, rng = T["U"] - tgt.ground, heading_rate(grid, T), np.hypot(T["E"], T["N"])
    dTI = np.hypot(T["E"] - I["E"], T["N"] - I["N"]) if I is not None else np.full(len(grid), np.nan)
    lo, hi = liftoff_edges(tgt, t0, t1)
    fl = dict(n=0, kind=None, t0=round(t0, 1), t1=round(t1, 1), t0_pdt=iso(t0), t1_pdt=iso(t1),
              t0_liftoff=round(lo, 1), t1_touchdown=round(hi, 1), t0_liftoff_pdt=hms(lo), t1_touchdown_pdt=hms(hi),
              drone_ids=[f"{tgt.name} (target)"] + ([f"{itc.name} merged (interceptor)"] if itc is not None else []),
              airborne_minutes=round((t1 - t0) / 60, 1))

    # ---- passes first (they decide the per-flight kind), then segments
    freezes = list(tgt.freezes) + (list(itc.freezes) if itc is not None else [])
    eng = [w for w in itc_wins if w[0] < t1 and w[1] > t0]
    passes = find_passes(grid, T, I, itc, freezes) if (I is not None and eng and kind != "tracking") else []
    if kind in ("auto", "engagement"):
        kind = "engagement" if eng and any(p["realistic"] and not p["interceptor_grounded"] for p in passes) else "tracking"
    fl["kind"] = kind
    fl["passes"] = passes if kind == "engagement" else []

    def seg(typ, k, a, b, **kw):
        return dict(type=typ, n=k, t0=round(a, 3), t1=round(b, 3), t0_pdt=iso(a), t1_pdt=iso(b), **kw)
    apex_idx, _ = find_peaks(np.where(np.isfinite(rng), rng, 0), prominence=P["apex_prom_m"])
    if kind == "engagement":
        a, b = max(t0, min(w[0] for w in eng)), min(t1, max(w[1] for w in eng))
        segs = [seg("climbout/transit", 1, t0, a), seg("engagement", 1, a, b, note="interceptor airborne window"), seg("return/land", 1, b, t1)]
    elif kind == "tracking" and len(apex_idx):
        cuts = [t0] + [float(grid[apex_idx[k] + int(np.nanargmin(rng[apex_idx[k]:apex_idx[k + 1]]))])
                       for k in range(len(apex_idx) - 1)] + [t1]
        segs = [seg("loop", k + 1, cuts[k], cuts[k + 1]) for k in range(len(cuts) - 1)]
    else:
        segs = [seg("transit", 1, t0, t1)]
    fl["segments"] = segs
    passes = fl["passes"]

    # ---- tracks in the (padded) window, sides, clutter gate, on-target set
    pad = P["track_pad_s"]
    views = [TrackView(tid, df[m], tgt, itc) for tid, df in tracks.items()
             for m in [(df.t_epoch >= t0 - pad) & (df.t_epoch <= t1 + pad)] if m.any()]
    cand = [v for v in views if v.on_tgt.sum() >= P["rider_min_n"] and v.side != "interceptor"]
    clutter = [v for v in cand if not v.moves]           # matched the target but never moved with it
    tside = [v for v in views if v.side == "target" and v not in clutter]
    iside = [v for v in views if v.side == "interceptor"]
    riders = sorted((v for v in cand if v.moves), key=lambda v: v.span(v.on_tgt)[0])
    ontgt = list({v.id: v for v in tside + riders}.values())

    # ---- coverage on the 1 Hz grid (fresh on-target states of the on-target set)
    S = np.sort(np.concatenate([v.t[v.fresh & v.on_tgt] for v in ontgt] or [np.zeros(0)]))
    covered = np.zeros(len(grid), bool)
    if len(S):
        k = np.clip(np.searchsorted(S, grid), 1, max(len(S) - 1, 1))
        covered = np.minimum(np.abs(S[k % len(S)] - grid), np.abs(S[k - 1] - grid)) <= P["cover_dt_s"]
    moving = T["ok"] & (T["U"] > tgt.ground + P["agl_air_m"]) & (T["spd"] > P["moving_mps"])
    cov_pct = 100.0 * covered[moving].sum() / max(moving.sum(), 1)
    cov_win = 100.0 * covered[T["ok"]].sum() / max(T["ok"].sum(), 1)

    # ---- coverage gaps (>= gap_min_s runs of uncovered truth-valid grid points)
    gaps, unc = [], T["ok"] & ~covered
    for run in np.split(np.arange(len(grid)), np.where(np.diff(unc.astype(int)) != 0)[0] + 1):
        if not unc[run[0]] or len(run) < P["gap_min_s"]:
            continue
        ga, gb = grid[run[0]], grid[run[-1]]
        g = dict(t0_pdt=hms(ga), t1_pdt=hms(gb), dur_s=float(len(run)), med_spd=rnd(np.nanmedian(T["spd"][run])),
                 min_spd=rnd(np.nanmin(T["spd"][run])), cause_hint=cause_hint(ga, grid, hrate, agl, dTI), _t0=ga)
        if len(run) >= 10:                       # tentative tracks forming on the drone mid-gap?
            near = {int(v.id): int(((v.t >= ga) & (v.t <= gb) & finite_lt(v.dT, 300)).sum()) for v in views}
            g["tentative_rows_nearby"] = sum(near.values()); g["tentative_ids"] = sorted(k for k, n in near.items() if n)
        gaps.append(g)

    # ---- speed-filter kills: last published state runs away (|v| or |vU|), followed by a coverage hole
    kills = []
    for v in ontgt:
        v3, vu = float(np.sqrt(v.vE[-1] ** 2 + v.vN[-1] ** 2 + v.vU[-1] ** 2)), float(v.vU[-1])
        if v3 > P["kill_v_mps"] or abs(vu) > P["kill_vu_mps"]:
            g = next((g for g in gaps if 0 <= g["_t0"] - v.t[-1] <= P["kill_gap_s"]), None)
            if g is not None:
                g["cause_hint"].append(f"speed-filter kill (vU={vu:.0f})")
                kills.append(dict(track=int(v.id), last_state_pdt=hms(v.t[-1]), v_mps=rnd(v3), vU_mps=rnd(vu), hole_s=g["dur_s"], hole_t0_pdt=g["t0_pdt"]))
    for g in gaps:
        g.pop("_t0")

    # ---- accuracy metrics over fresh on-target states
    fr = [(v.E[m], v.N[m], v.U[m], v.tgt["E"][m], v.tgt["N"][m], v.tgt["U"][m], v.dT[m])
          for v in ontgt for m in [v.fresh & v.on_tgt] if m.any()]
    tr = dict(target=tgt.name, n_tracks=len(tside), coverage_pct=round(cov_pct, 1), coverage_pct_window=round(cov_win, 1),
              med_horiz_err_m=None, med_horiz_err_azcorr_m=None, p90_horiz_err_m=None, az_bias_deg=None, el_bias_deg=None, alt_bias_m=None)
    if fr:
        E, N, U, tE, tN, tU, d = (np.concatenate(x) for x in zip(*fr))
        az_err = wrap_deg(np.degrees(np.arctan2(E, N) - np.arctan2(tE, tN)))
        az_b = float(np.median(az_err)); b = np.radians(az_b)
        Ec, Nc = E * np.cos(b) - N * np.sin(b), E * np.sin(b) + N * np.cos(b)   # rotate bearing by -bias
        el_err = np.degrees(np.arctan2(U, np.hypot(E, N)) - np.arctan2(tU, np.hypot(tE, tN)))
        tr.update(med_horiz_err_m=rnd(np.median(d)), med_horiz_err_azcorr_m=rnd(np.median(np.hypot(Ec - tE, Nc - tN))),
                  p90_horiz_err_m=rnd(np.percentile(d, 90)), az_bias_deg=rnd(az_b, 2), el_bias_deg=rnd(np.median(el_err), 2),
                  alt_bias_m=rnd(np.median(U - tU)), _az_err=az_err)
    n_meas = len(set(np.concatenate([v.lut[v.on_tgt] for v in ontgt] or [np.zeros(0)]).tolist()))
    tr.update(meas_rate_hz=rnd(n_meas / max(t1 - t0, 1), 2), n_measurements=n_meas, fragmentation_track_count=len(ontgt),
              rider_track_ids=[int(v.id) for v in riders],
              rider_spans_pdt={str(v.id): [hms(s[0]), hms(s[1]), s[2]] for v in riders for s in [v.span(v.on_tgt)]},
              clutter_tracks=[dict(track=int(v.id), n_on_target=int(v.on_tgt.sum()), median_offset_m=rnd(v.med(v.dT, v.on_tgt)),
                                   path_m=rnd(v.path_m), t0_pdt=hms(v.t[0]), t1_pdt=hms(v.t[-1])) for v in clutter],
              range_apexes=[dict(t_pdt=hms(grid[i]), rng_m=round(float(rng[i]))) for i in apex_idx],
              feed_freezes=[dict(feed=f[3], t0_pdt=hms(f[0]), t1_pdt=hms(f[1]), dur_s=round(f[1] - f[0], 1), rows=f[2], dead_reckoned=f[4])
                            for f in freezes if f[1] - f[0] >= P["freeze_list_s"] and f[0] < t1 + 60 and f[1] > t0 - 60])

    # ---- handovers along the rider chain (riders nested inside another's span are not chain links)
    spans = sorted((*v.span(v.on_tgt)[:2], v.id) for v in riders)
    chain = [s for s in spans if not any(o[0] <= s[0] and o[1] >= s[1] and o[2] != s[2] for o in spans)]
    hand = []
    for a, b_ in zip(chain, chain[1:]):
        gap = b_[0] - a[1]
        if gap > -P["handover_overlap_s"]:
            k, hint = int(np.argmin(np.abs(grid - b_[0]))), cause_hint(b_[0], grid, hrate, agl, dTI)
            hand.append(dict(t_pdt=hms(b_[0]), from_track=int(a[2]), to_track=int(b_[2]), coverage_gap_s=rnd(gap),
                             during_turn="turning" in hint, truth_speed_mps=rnd(T["spd"][k]), truth_range_m=rnd(rng[k]), cause_hint=hint))
    tr.update(handovers=hand, coverage_gaps=gaps, gap_count=len(gaps),
              gap_total_s=float(sum(g["dur_s"] for g in gaps)), gap_max_s=float(max([g["dur_s"] for g in gaps] or [0])))

    # ---- pinned altitude: >= pin_win_s of near-constant published U while truth varies or the track jitters elsewhere
    pinned = []
    for v in ontgt:
        ok = np.isfinite(v.tgt["U"]); t, U, tU = v.t[ok], v.U[ok], v.tgt["U"][ok]
        flat = np.concatenate([[False], np.abs(np.diff(U)) < P["pin_step_m"]])
        for run in np.split(np.arange(len(t)), np.where(np.diff(flat.astype(int)) != 0)[0] + 1):
            if not (len(run) and flat[run[0]]) or t[run[-1]] - t[run[0]] < P["pin_win_s"] or np.std(U[run]) >= P["pin_std_m"]:
                continue
            rest = np.ones(len(t), bool); rest[run] = False
            std_else = float(np.std(U[rest])) if rest.sum() > 5 else 0.0
            if np.ptp(tU[run]) > P["pin_truth_var_m"] or std_else > P["pin_std_else_m"]:
                pinned.append(dict(track=int(v.id), t0_pdt=hms(t[run[0]]), t1_pdt=hms(t[run[-1]]), dur_s=rnd(t[run[-1]] - t[run[0]]),
                                   U_m=rnd(np.median(U[run])), truth_alt_var_m=rnd(np.ptp(tU[run])), u_std_elsewhere_m=rnd(std_else)))
    tr.update(pinned_altitude_tracks=pinned, speed_filter_kills=kills)

    # ---- pass-related track anomalies: steals (both directions), drag-and-die, corruption
    steals, drag, corr = [], [], []
    for p in passes:
        tp = p["t_epoch"]
        for v in views:
            pre, post = (v.t >= tp - P["steal_pre_s"]) & (v.t < tp), (v.t > tp) & (v.t <= tp + P["steal_post_s"])
            if pre.sum() < P["steal_pre_n"] or post.sum() < 3 or v.t[0] > tp - P["steal_min_age_s"] \
                    or any(s["track"] == v.id for s in steals):     # identity must predate the pass
                continue
            s_pre, s_post = v.side_in(pre), v.side_in(post, P["steal_r_m"])
            if s_pre and s_post and s_pre != s_post:
                steals.append(dict(track=int(v.id), direction=f"{s_pre}->{s_post}", after_pass=p["n"], pass_t_pdt=p["t_pdt"],
                                   pre_pass_median_dist_to_interceptor_m=rnd(v.med(v.dI, pre)), pre_pass_median_dist_to_target_m=rnd(v.med(v.dT, pre)),
                                   post_pass_median_dist_to_interceptor_m=rnd(v.med(v.dI, post)), post_pass_median_dist_to_target_m=rnd(v.med(v.dT, post)),
                                   track_end_pdt=hms(v.t[-1]), label="track steal"))
        for v in ([] if p["interceptor_grounded"] else ontgt):      # drag/corruption only at real (airborne) passes
            pre = (v.t >= tp - P["steal_pre_s"]) & (v.t < tp) & v.on_tgt
            if pre.sum() < P["steal_pre_n"] or v.t[-1] <= tp or v.t[0] > tp - P["steal_min_age_s"]:
                continue
            post = (v.t > tp) & (v.t <= tp + P["drag_win_s"]) & np.isfinite(v.dT)   # drag-and-die
            if post.any() and np.max(v.dT[post]) > P["drag_off_m"] and not any(d["track"] == v.id for d in drag):
                t_ex = v.t[post][np.argmax(v.dT[post] > P["drag_off_m"])]
                if v.t[-1] <= t_ex + P["drag_die_s"]:
                    drag.append(dict(track=int(v.id), after_pass=p["n"], pass_t_pdt=p["t_pdt"], max_offset_m=rnd(np.max(v.dT[post])),
                                     first_exceed_pdt=hms(t_ex), track_end_pdt=hms(v.t[-1]), also_steal=False))
            thr = max(2 * v.med(v.res, np.ones(len(v.t), bool)), P["corr_floor_m"])      # corruption: excursion that returns
            exc = np.where(finite_lt(-v.res, -thr))[0]
            for run in np.split(exc, np.where(np.diff(exc) > 1)[0] + 1):
                if not len(run) or run[-1] >= len(v.t) - 1 or run[0] == 0:
                    continue                                       # must start after birth and end before the track ends
                ts, te = v.t[run[0]], v.t[run[-1]]
                if abs(ts - tp) <= P["corr_win_s"] and te - ts >= P["corr_min_s"] and not any(c["track"] == v.id and c["pass_t_pdt"] == p["t_pdt"] for c in corr):
                    corr.append(dict(track=int(v.id), pass_t_pdt=p["t_pdt"], t0_pdt=hms(ts), t1_pdt=hms(te), dur_s=rnd(te - ts),
                                     peak_err_m=rnd(np.max(v.res[run])), threshold_m=rnd(thr)))
    for d in drag:
        d["also_steal"] = any(s["track"] == d["track"] for s in steals)
    if kind == "engagement":
        allm = lambda v: np.ones(len(v.t), bool)
        tr.update(interceptor_track_count=len(iside), fight_track_count=len(tside) + len(iside),
                  fight_track_count_any_contact=sum(1 for v in views if v.on_tgt.any() or v.on_itc.any()),
                  steal_events=steals, drag_and_die=drag, corruption_events=corr,
                  target_track_spans={str(v.id): f"{hms(v.t[0])}-{hms(v.t[-1])}, med {v.med(v.dT, allm(v)):.1f} m off target" for v in tside},
                  interceptor_track_spans={str(v.id): f"{hms(v.t[0])}-{hms(v.t[-1])}, med {v.med(v.dI, allm(v)):.1f} m off interceptor" for v in iside})
    fl["tracking"] = tr

    # ---- issues (quantified, auto-generated)
    iss = [f"track {s['track']} TRACK STEAL ({s['direction']}) after pass {s['after_pass']} ({s['pass_t_pdt']}): post-pass median "
           f"{s['post_pass_median_dist_to_interceptor_m']} m to interceptor vs {s['post_pass_median_dist_to_target_m']} m to target; "
           f"track ended {s['track_end_pdt']}" for s in steals]
    iss += [f"track {d['track']} DRAG-AND-DIE after pass {d['after_pass']} ({d['pass_t_pdt']}): pulled to {d['max_offset_m']} m off the target "
            f"by {d['first_exceed_pdt']}, dead {d['track_end_pdt']}" + (" (also a track steal)" if d["also_steal"] else "") for d in drag]
    iss += [f"track {c['track']} STATE CORRUPTION at pass {c['pass_t_pdt']}: error residual peaked {c['peak_err_m']} m for {c['dur_s']} s "
            f"({c['t0_pdt']}-{c['t1_pdt']}) then returned; identity held" for c in corr]
    for p in passes:
        if p["frozen_feed"]:
            f = p["frozen_feed"]
            iss.append(f"pass {p['n']} ({p['miss_m']} m @{p['t_pdt']}) is within +-{P['freeze_pass_win_s']:.0f} s of a FROZEN {f['feed']} feed "
                       f"{f['t0_pdt']}-{f['t1_pdt']} ({f['dur_s']} s){' - dead-reckoned' if f['dead_reckoned'] else ''}; CPA unreliable")
        if p["interceptor_grounded"]:
            iss.append(f"pass {p['n']} ({p['miss_m']} m @{p['t_pdt']}) computed while the interceptor was still grounded - dip vs parked interceptor")
        elif not p["realistic"]:
            iss.append(f"pass {p['n']} ({p['miss_m']} m @{p['t_pdt']}) fails the realistic gate (tgt {p['tgt_speed_mps']} m/s, "
                       f"itc {p['itc_speed_mps']} m/s, approach closing {p['closing_speed_mps']} m/s)")
    iss += [f"CLUTTER track {c['track']} matched the target for {c['n_on_target']} states (median {c['median_offset_m']} m) but never moved "
            f"(path {c['path_m']} m) - excluded from riders/coverage" for c in tr["clutter_tracks"]]
    iss += [f"track {q['track']} PINNED ALTITUDE U={q['U_m']} m for {q['dur_s']} s ({q['t0_pdt']}-{q['t1_pdt']}); truth altitude varied "
            f"{q['truth_alt_var_m']} m, track U std elsewhere {q['u_std_elsewhere_m']} m" for q in pinned]
    iss += [f"track {k['track']} SPEED-FILTER KILL: last state {k['last_state_pdt']} |v|={k['v_mps']} m/s vU={k['vU_mps']} m/s, "
            f"followed by a {k['hole_s']:.0f} s coverage hole from {k['hole_t0_pdt']}" for k in kills]
    if tr["az_bias_deg"] is not None:
        iss.append(f"azimuth bias {tr['az_bias_deg']:+.2f} deg: median horiz err {tr['med_horiz_err_m']} m raw -> "
                   f"{tr['med_horiz_err_azcorr_m']} m after removing the bias")
        iss.append(f"elevation/altitude bias: median el err {tr['el_bias_deg']:+.2f} deg, median track altitude {tr['alt_bias_m']:+.1f} m vs truth")
    iss.append(f"track fragmentation: {len(ontgt)} track ids rode the target ({', '.join(str(v.id) for v in ontgt)}) in a {t1 - t0:.0f} s flight "
               f"({len(tside)} target-side by median, {len(riders)} with >={P['rider_min_n']} on-target states); {len(hand)} handovers"
               + (f" ({sum(h['during_turn'] for h in hand)} during turns)" if hand else ""))
    if iside:
        iss.append(f"interceptor-side tracks: {len(iside)} ({', '.join(str(v.id) for v in iside)})")
    if gaps:
        tags = [t for g in gaps for t in g["cause_hint"]]; worst = max(gaps, key=lambda g: g["dur_s"])
        iss.append(f"coverage gaps: {len(gaps)} gaps >= {P['gap_min_s']:.0f} s totaling {tr['gap_total_s']:.0f} s (max {worst['dur_s']:.0f} s at "
                   f"{worst['t0_pdt']}); cause hints: " + ", ".join(f"{k} {tags.count(k)}" for k in
                   ("turning", "low-alt/pad", "interceptor<200m", "straight cruise") if tags.count(k)))
        tent = [g for g in gaps if g.get("tentative_ids")]
        if tent:
            iss.append(f"{len(tent)} gap(s) >= 10 s have track states within 300 m of the drone mid-gap (ids "
                       f"{sorted({i for g in tent for i in g['tentative_ids']})}) - detections continuing, association/confirmation lagging")
        if min(gaps[0]["min_spd"] or 9, gaps[-1]["min_spd"] or 9) < 1:
            iss.append("takeoff/landing hover uncovered: first/last gap of the flight has truth min speed < 1 m/s")
    if tgt.t[-1] < t1 + 10:
        iss.append(f"dump/feeds end {hms(tgt.t[-1])} - flight window may be truncated")
    fl["issues"] = iss
    return fl


# --------------------------------------------------------------------------- output
def method_block(target_pat, itc_pat, kind):
    g5 = f"ground+{P['agl_air_m']:.0f}"
    return dict(
        target=(f"{target_pat}.csv concatenated across dumps, dedup on {P['dedup_s']} s rounded t_epoch; frozen runs longer than "
                f"{P['flight_gap_s']/60:.0f} min (dead feed) dropped keeping the first row, shorter hover republishes kept"),
        interceptor=(f"{itc_pat}.csv per-feed frozen runs >={P['freeze_min_run']} identical positions dropped (first kept), merged, dedup on "
                     f"{P['dedup_s']} s rounded t_epoch, >{P['teleport_mps']:.0f} m/s teleports dropped") if itc_pat else "none (tracking-only day)",
        flights=(f"split on target-drone airborne gaps > {P['flight_gap_s']/60:.0f} min (airborne = U_hae > {g5} m at speed > {P['drift_mps']} m/s "
                 f"[parked-GPS altitude drift ignored] or speed > {P['moving_mps']:.0f} m/s; ground = median U of parked <{P['parked_mps']:.0f} m/s "
                 f"samples of the raw feed); windows trimmed to first/last U > {g5} sample"
                 + (f" or speed > {P['trim_speed_mps']} m/s" if P["trim_speed_mps"] > 0 else "")
                 + f"; windows with peak speed < {P['min_peak_mps']:.0f} m/s skipped (GPS acquisition); interceptor sorties without a target flight "
                 f"emitted as interceptor-only flights" + ("; racetrack loops split at the range trough between range apexes" if kind == "tracking" else "")),
        anomalies=(f"clutter = >={P['rider_min_n']} on-target states but horizontal path < {P['rider_path_m']:.0f} m and track speed >= {P['moving_mps']:.0f} m/s "
                   f"in < {P['rider_vfrac']*100:.0f}% of matched states (excluded from riders/coverage); drag-and-die = on-target track pulled > "
                   f"{P['drag_off_m']:.0f} m within {P['drag_win_s']:.0f} s after a pass and dead within {P['drag_die_s']:.0f} s of that; corruption = "
                   f"bias-removed error residual > max(2 x its median, {P['corr_floor_m']:.0f} m) for >= {P['corr_min_s']:.0f} s starting within "
                   f"+-{P['corr_win_s']:.0f} s of a pass, then returning; pinned altitude = >= {P['pin_win_s']:.0f} s of published U steps < "
                   f"{P['pin_step_m']} m (std < {P['pin_std_m']:.0f} m) while truth altitude varies > {P['pin_truth_var_m']:.0f} m or the track's U std "
                   f"elsewhere > {P['pin_std_else_m']:.0f} m; speed-filter kill = last state |v| > {P['kill_v_mps']:.0f} or |vU| > {P['kill_vu_mps']:.0f} m/s "
                   f"followed within {P['kill_gap_s']:.0f} s by a coverage hole; feed freezes >= {P['freeze_list_s']:.0f} s listed, >= {P['freeze_flag_s']:.0f} s "
                   f"within +-{P['freeze_pass_win_s']:.0f} s of a pass flag it unreliable; moving target freezes ({P['freeze_flag_s']:.0f} s..{P['flight_gap_s']/60:.0f} min, "
                   f"last good speed > {P['moving_mps']:.0f} m/s) are dead-reckoned from the last good fix"),
        flight_kind=(f"per flight: engagement if the interceptor is airborne during the flight and a realistic, non-grounded pass exists, else tracking "
                     f"(racetrack loops at range troughs); day type = majority of flights; t0/t1 = altitude-trimmed window, t0_liftoff/t1_touchdown = "
                     f"first/last contiguous sample with |vertical speed| > {P['lift_vs_mps']} m/s or speed > {P['lift_spd_mps']:.0f} m/s"),
        passes=(f"1 Hz grid, 3D separation of interpolated feeds, no interpolation across >{P['interp_gap_s']} s gaps in either feed, local minima "
                f"< {P['pass_max_m']:.0f} m with {P['pass_prom_m']:.0f} m prominence (per contiguous segment; a segment-edge minimum with one-sided "
                f"prominence >= {P['pass_prom_m']:.0f} m is kept and flagged at_gap_edge; a {P['pass_sub_prom_m']:.0f}-{P['pass_prom_m']:.0f} m prominence "
                f"minimum below {P['pass_sub_max_m']:.0f} m is kept and flagged sub_prominence); grounded-interceptor dips flagged; realistic = both craft "
                f">= {P['real_spd_mps']:.0f} m/s at the minimum and approach closing speed (90th pct of -d sep/dt over the {P['close_win_s']:.0f} s "
                f"before the minimum, {P['close_base_s']:.0f} s baselines) >= {P['real_close_mps']:.0f} m/s"),
        coverage=(f"pct of 1 Hz moving samples (U > {g5} m AND speed > {P['moving_mps']:.0f} m/s) with a fresh (t - last_update_t <= {P['fresh_s']} s) "
                  f"on-target track state within {P['cover_dt_s']} s and {P['cover_r_m']:.0f} m; coverage_pct_window = same over every truth-valid "
                  f"1 Hz sample in the flight window (hover included)"),
        track_sides=(f"track is target-side if median horiz dist to target < {P['side_r_m']:.0f} m and <= median dist to interceptor "
                     f"(>={P['side_min_n']} truth-valid samples); interceptor-side symmetric; on-target set for coverage/metrics = target-side tracks "
                     f"+ riders (>={P['rider_min_n']} states within {P['side_r_m']:.0f} m, not interceptor-side)"),
        steals=(f"any track born >={P['steal_min_age_s']:.0f} s before a pass with >={P['steal_pre_n']} states in the {P['steal_pre_s']:.0f} s before it sitting on one craft whose post-pass "
                f"(<={P['steal_post_s']:.0f} s) median distance flips to within {P['steal_r_m']:.0f} m of the other craft; both directions are 'track steals'"),
        metrics=(f"fresh on-target states of the on-target set: median/p90 horiz err, az-bias-removed median, az/el/alt bias medians; n_measurements = "
                 f"distinct last_update_t; handovers = rider-chain id changes with overlap < {P['handover_overlap_s']:.0f} s; coverage gaps >= "
                 f"{P['gap_min_s']:.0f} s; cause hints: turning (>{P['turn_rate_dps']:.0f} deg/s within +-3 s), low-alt/pad (AGL < {P['pad_agl_m']:.0f} m), "
                 f"interceptor < {P['interc_near_m']:.0f} m, else straight cruise"))


def write_summary(path, man, kind):
    r0, fls = man["runs"][0], man["runs"][0]["flights"]
    L = [f"# Seawall {man['day']} - run {r0['run']} ({kind} day)", "",
         f"Auto-generated skeleton from build_flight_manifest.py; dumps: {', '.join(r0['dumps'])}. Antenna {r0['antenna']}. "
         "All times local. Machine-readable version: `flights.json`.", "", "## Narrative", "", "TODO: analyst narrative goes here.", "",
         "## Flights", "", "| Flight | Kind | Window (liftoff-touchdown) | Air min | Segments | Passes (m) | Best | Cov % (moving / window) | "
         "Med herr (raw/azcorr) | Az bias | Tgt tracks | Riders | Steals |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for f in fls:
        tr, ps = f["tracking"], f["passes"]; ok = [p for p in ps if not p["interceptor_grounded"]]
        L.append(f"| {f['n']} | {f['kind']} | {f['t0_pdt'][11:19]}-{f['t1_pdt'][11:19]} ({f['t0_liftoff_pdt']}-{f['t1_touchdown_pdt']}) | "
                 f"{f['airborne_minutes']} | {', '.join(s['type'] for s in f['segments'])} | "
                 f"{' / '.join(str(p['miss_m']) + ('*' if p['interceptor_grounded'] or not p['realistic'] or p['sub_prominence'] or p['at_gap_edge'] else '') for p in ps) or '-'} | "
                 f"{(str(min(p['miss_m'] for p in ok)) + ' m') if ok else '-'} | {tr['coverage_pct']} / {tr['coverage_pct_window']} | "
                 f"{tr['med_horiz_err_m']} / {tr['med_horiz_err_azcorr_m']} m | {tr['az_bias_deg']} | {tr['n_tracks']} | {len(tr['rider_track_ids'])} | "
                 f"{len(tr.get('steal_events', []))} |")
    L += ["", "\\* = interceptor grounded, fails the realistic gate, sub-prominence or gap-edge minimum (see pass table).", ""]
    if any(f["passes"] for f in fls):
        L += ["## Passes", "", "| Flight | # | Time | Miss 3D | Horiz | Prom | Tgt spd | Itc spd | Closing (approach / at min) | Grounded | Realistic | Gap edge | Sub-prom | Frozen feed |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {f['n']} | {p['n']} | {p['t_pdt']} | {p['miss_m']} m | {p['horiz_miss_m']} m | {p['prominence_m']} m | {p['tgt_speed_mps']} | "
              f"{p['itc_speed_mps']} | {p['closing_speed_mps']} / {p['closing_at_min_mps']} | {p['interceptor_grounded']} | {p['realistic']} | {p['at_gap_edge']} | "
              f"{p['sub_prominence']} | {(p['frozen_feed'] or {}).get('feed', '')} {(p['frozen_feed'] or {}).get('t0_pdt', '')}-{(p['frozen_feed'] or {}).get('t1_pdt', '')} |"
              for f in fls for p in f["passes"]]
        L += ["", "## Track steals", "", "| Flight | Track | Direction | After pass | Pass time | Pre med tgt/itc | Post med tgt/itc | Track end |",
              "|---|---|---|---|---|---|---|---|"]
        L += [f"| {f['n']} | {s['track']} | {s['direction']} | {s['after_pass']} | {s['pass_t_pdt']} | {s['pre_pass_median_dist_to_target_m']} / "
              f"{s['pre_pass_median_dist_to_interceptor_m']} m | {s['post_pass_median_dist_to_target_m']} / {s['post_pass_median_dist_to_interceptor_m']} m | "
              f"{s['track_end_pdt']} |" for f in fls for s in f["tracking"].get("steal_events", [])] + [""]
    L += ["## Rider chains", ""]
    for f in fls:
        tr = f["tracking"]
        L.append(f"- **F{f['n']}** ({len(tr['rider_track_ids'])}): " + (" -> ".join(f"{k} ({v[0]}-{v[1]}, {v[2]} st)" for k, v in tr["rider_spans_pdt"].items()) or "none"))
        L += [f"  - handover {h['from_track']} -> {h['to_track']} @{h['t_pdt']}: gap {h['coverage_gap_s']} s, {'/'.join(h['cause_hint'])}, "
              f"truth {h['truth_speed_mps']} m/s @ {h['truth_range_m']} m" for h in tr["handovers"]]
    L += ["", "## Track anomalies", ""]
    for f in fls:
        tr = f["tracking"]
        items = ([f"drag-and-die trk {d['track']} after pass {d['after_pass']} ({d['pass_t_pdt']}): {d['max_offset_m']} m, dead {d['track_end_pdt']}"
                  + (" (also steal)" if d["also_steal"] else "") for d in tr.get("drag_and_die", [])]
                 + [f"corruption trk {c['track']} at {c['pass_t_pdt']}: {c['peak_err_m']} m for {c['dur_s']} s" for c in tr.get("corruption_events", [])]
                 + [f"clutter trk {c['track']}: {c['n_on_target']} matched states, median {c['median_offset_m']} m, path {c['path_m']} m" for c in tr["clutter_tracks"]]
                 + [f"pinned altitude trk {q['track']}: U={q['U_m']} m {q['t0_pdt']}-{q['t1_pdt']} ({q['dur_s']} s)" for q in tr["pinned_altitude_tracks"]]
                 + [f"speed-filter kill trk {k['track']} @{k['last_state_pdt']} (vU={k['vU_mps']}), hole {k['hole_s']:.0f} s" for k in tr["speed_filter_kills"]]
                 + [f"feed freeze {z['feed']} {z['t0_pdt']}-{z['t1_pdt']} ({z['dur_s']} s{', dead-reckoned' if z['dead_reckoned'] else ''})" for z in tr["feed_freezes"]])
        L.append(f"- **F{f['n']}**: " + ("; ".join(items) if items else "none"))
    L += ["", "## Coverage gaps (>= 3 s)", "", "| Flight | Start | End | Dur s | Med spd | Min spd | Cause hint | Tentative ids |", "|---|---|---|---|---|---|---|---|"]
    L += [f"| {f['n']} | {g['t0_pdt']} | {g['t1_pdt']} | {g['dur_s']:.0f} | {g['med_spd']} | {g['min_spd']} | {'/'.join(g['cause_hint'])} | "
          f"{g.get('tentative_ids', '')} |" for f in fls for g in f["tracking"]["coverage_gaps"]]
    L += ["", "## Issues (auto-quantified)", ""]
    for f in fls:
        L += [f"**Flight {f['n']}**"] + [f"- {i}" for i in f["issues"]] + [""]
    L += ["**Day**"] + [f"- {i}" for i in man["day_issues"]] + ["", "Provenance: " + "; ".join(f"{k}: {v}" for k, v in man["method"].items())]
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True); ap.add_argument("--dumps", nargs="+", required=True)
    ap.add_argument("--target", default="mav*_1_*"); ap.add_argument("--interceptor", default="mav*_2_*", help="glob or 'none'")
    ap.add_argument("--type", default="auto", choices=["auto", "tracking", "engagement"])
    ap.add_argument("--tz", default="America/Los_Angeles"); ap.add_argument("--out", default=None)
    ap.add_argument("--windows", default=None, help="force flight windows: HH:MM:SS-HH:MM:SS[,...] (local --tz on --day)")
    ap.add_argument("--trim-speed", type=float, default=0.0, help="also extend windows to first/last truth speed > X m/s (0 = altitude-only trim)")
    a = ap.parse_args()
    P["trim_speed_mps"] = a.trim_speed
    global TZ; TZ = ZoneInfo(a.tz)
    out = a.out or os.path.dirname(os.path.abspath(a.dumps[0])); os.makedirs(out, exist_ok=True)

    metas = [json.load(open(os.path.join(d, "meta.json"))) for d in a.dumps if os.path.exists(os.path.join(d, "meta.json"))]
    run = next((m.get("run_id8") or (m["run"][4:12] if str(m.get("run", "")).startswith("run_") else m.get("run")) for m in metas), None)
    antenna = next((m.get("antenna_origin_lat_lon_haeM") or m.get("antenna") for m in metas), None)

    tgt, tgt_ids, tgt_freezes, tgt_dropped = build_target(a.dumps, a.target)
    itc, itc_stats, n_tele = build_interceptor(a.dumps, a.interceptor)
    tracks = load_tracks(a.dumps)
    if a.windows:
        loc = lambda s: datetime.strptime(f"{a.day} {s}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
        wins, skipped = [tuple(loc(x) for x in w.split("-")) for w in a.windows.split(",")], []
    else:
        wins, skipped = find_flights(tgt)
    itc_wins, _ = find_flights(itc) if itc is not None else ([], [])
    solo = [] if a.windows else [w for w in itc_wins if not any(w[0] < t1 + 60 and w[1] > t0 - 60 for t0, t1 in wins)]
    flights = [analyse_flight(w0, w1, tgt, itc, tracks, a.type, itc_wins) for w0, w1 in wins]
    kinds = [f["kind"] for f in flights]
    kind = a.type if a.type != "auto" else ("engagement" if kinds.count("engagement") >= max(kinds.count("tracking"), 1) else "tracking")
    print(f"[{a.day}] run {run} day-kind={kind} flight-kinds={kinds} target={tgt.name} ({len(tgt.t)} rows, ground U={tgt.ground:.1f} m) "
          f"interceptor={itc.name if itc else None} tracks={len(tracks)} flights={len(wins)} interceptor-only={len(solo)}")
    for w0, w1 in solo:                          # interceptor-only sorties: roles swapped
        f = analyse_flight(w0, w1, itc, None, tracks, "solo")
        f["drone_ids"] = [f"{itc.name} merged (interceptor - analysed as the tracked drone)"]
        f["note"] = "interceptor-only sortie: target truth frozen/absent, so no passes; tracking metrics are vs the interceptor"
        f["issues"].insert(0, "target truth frozen/absent during this sortie - passes not verifiable from feeds")
        flights.append(f)
    flights.sort(key=lambda f: f["t0"])
    for i, f in enumerate(flights):
        f["n"] = i + 1

    # ---- day-level issues
    day = []
    az_all = np.concatenate([f["tracking"].pop("_az_err") for f in flights if "_az_err" in f["tracking"]] or [np.zeros(0)])
    if len(az_all):
        day.append(f"az bias: day median {np.median(az_all):+.2f} deg (per flight "
                   + "/".join(f"{f['tracking']['az_bias_deg']:+.2f}" for f in flights if f["tracking"]["az_bias_deg"] is not None) + ")")
    day.append("day coverage (per flight): " + " / ".join(f"{f['tracking']['coverage_pct']}%" for f in flights))
    if skipped:
        day.append("target windows skipped as GPS-acquisition noise (peak speed < "
                   f"{P['min_peak_mps']:.0f} m/s): " + ", ".join(f"{hms(a_)}-{hms(b_)}" for a_, b_ in skipped))
    long_fz = [(s, e, n) for s, e, n in tgt_freezes if e - s >= 60]
    if long_fz:
        day.append(f"target truth feed stale-freezes >= 60 s (runs > {P['flight_gap_s']/60:.0f} min = dead feed, {tgt_dropped} rows dropped; shorter "
                   "runs kept): " + ", ".join(f"{hms(s)}->{hms(e)} ({(e - s)/60:.1f} min, {n} rows)" for s, e, n in long_fz[:8]))
    if itc_stats:
        worst = [f"{k} frozen {v['worst_freeze']['dur_s']} s {v['worst_freeze']['t0_pdt']}->{v['worst_freeze']['t1_pdt']}"
                 for k, v in itc_stats.items() if v["worst_freeze"] and v["worst_freeze"]["dur_s"] >= 60]
        day.append(f"interceptor truth: {sum(v['stale_dropped'] for v in itc_stats.values())} stale rows dropped across {'/'.join(itc_stats)}"
                   + (f" (worst: {'; '.join(worst)})" if worst else "") + f"; {n_tele} teleports >{P['teleport_mps']:.0f} m/s dropped after merge")
    cnt = lambda key: sum(len(f["tracking"].get(key, [])) for f in flights)
    day.append(f"track anomalies: {cnt('clutter_tracks')} clutter, {cnt('drag_and_die')} drag-and-die, {cnt('corruption_events')} corruption, "
               f"{cnt('pinned_altitude_tracks')} pinned-altitude, {cnt('speed_filter_kills')} speed-filter kills; "
               f"{sum(1 for f in flights for p in f['passes'] if p['frozen_feed'])} passes flagged FROZEN FEED")
    all_steals = [(f["n"], s) for f in flights for s in f["tracking"].get("steal_events", [])]
    if all_steals:
        day.append("track steals: " + ", ".join(f"F{n} trk {s['track']} ({s['direction']}, after pass {s['after_pass']})" for n, s in all_steals))

    man = dict(day=a.day, runs=[dict(run=run, antenna=antenna, dumps=[os.path.basename(os.path.abspath(d)) for d in a.dumps],
                                     target_feeds=tgt_ids, interceptor_feeds=itc_stats, flights=flights)],
               day_issues=day, method=method_block(a.target, None if itc is None else a.interceptor, kind))
    with open(os.path.join(out, "flights.json"), "w") as fh:
        json.dump(man, fh, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    write_summary(os.path.join(out, "DAY_SUMMARY.md"), man, kind)

    for f in flights:                            # compact console summary
        tr = f["tracking"]; ps = [p for p in f["passes"] if not p["interceptor_grounded"]]
        print(f"  F{f['n']} [{f['kind']}] {f['t0_pdt'][11:19]}-{f['t1_pdt'][11:19]} (lift {f['t0_liftoff_pdt']}-{f['t1_touchdown_pdt']}, {f['airborne_minutes']} min) cov {tr['coverage_pct']}% "
              f"herr {tr['med_horiz_err_m']}/{tr['med_horiz_err_azcorr_m']} m az {tr['az_bias_deg']} el {tr['el_bias_deg']} "
              f"tgt-tracks {tr['n_tracks']} riders {len(tr['rider_track_ids'])} gaps {tr['gap_count']}/{tr['gap_total_s']:.0f}s"
              + (f" passes {[(p['t_pdt'], p['miss_m']) for p in f['passes']]} best {min(p['miss_m'] for p in ps) if ps else '-'}" if f["passes"] else "")
              + (f" steals {[(s['track'], s['direction'], s['after_pass']) for s in tr['steal_events']]}" if tr.get("steal_events") else "")
              + (f" drag {[d['track'] for d in tr['drag_and_die']]}" if tr.get("drag_and_die") else "") + (f" corr {[c['track'] for c in tr['corruption_events']]}" if tr.get("corruption_events") else "")
              + (f" clutter {[c['track'] for c in tr['clutter_tracks']]}" if tr["clutter_tracks"] else "") + (f" pinned {[q['track'] for q in tr['pinned_altitude_tracks']]}" if tr["pinned_altitude_tracks"] else "")
              + (f" kills {[k['track'] for k in tr['speed_filter_kills']]}" if tr["speed_filter_kills"] else ""))
    print(f"wrote {out}/flights.json and DAY_SUMMARY.md")


if __name__ == "__main__":
    main()
