#!/usr/bin/env python
"""make_synthetic_day.py - fabricate a realistic MRU radar test day in the
"quickdump" archive layout, with deliberately injected edge cases and a
machine-readable ANSWER_KEY.json, so the seawall-post-analysis skill can be
tested against known truth.  numpy + pandas only (no mongo, no scipy):

  micromamba run -n sensorenv python make_synthetic_day.py --out DIR [--seed 7]
      [--day 2026-10-01] [--start 08:00] [--az-bias -1.5] [--el-bias 0.5]
      [--err-rms 25] [--pass-miss 60 25 45 70] [--no-steal] [--no-freeze]
      [--no-teleport] [--no-corruption] [--no-drag] [--no-pinned] [--no-kill]
      [--no-clutter] [--no-adsb] [--no-dup-ids] [--no-stale]

Layout (identical to seawall_0824_data/<day>/<run>_flightN_quickdump):
  <out>/<day>/<run>_flight1/{mavlink/<id>.csv, tracks/track_<id>.csv, meta.json}
  <out>/<day>/<run>_flight2/...            (same run, second dump)
  <out>/<day>/ANSWER_KEY.json              exact injected truth

Units exactly as the real archive README:
  mavlink: t_epoch, time_pdt (ISO, tz), lat, lon, alt_ft_wire (**FEET MSL**),
           E_m, N_m, U_m_hae (metres ENU about the antenna, U in the WGS-84 HAE
           frame: U = alt_ft*0.3048 + GEOID_N - antenna_hae), vel_n_mps,
           vel_e_mps, vert_spd_wire_ftmin (**ft/min**), validposition
  tracks:  t_epoch, time_pdt, E_m, N_m, U_m, vE_mps, vN_mps, vU_mps, sigE_m,
           sigN_m, sigU_m, total_associations, track_state (1 tentative /
           2 confirmed), last_update_t (advances only on measurement updates;
           unchanged between rows == coasting; publish rate constant 2 Hz)

Scenario (defaults): antenna 33.748056,-115.339204,140.5 m HAE, geoid_n -31.4.
  Flight 1 (tracking, ~15 min): target mav14550_1_1 takes off from the pad
    E=1860,N=247,U=-13, climbs to ~90 m AGL, 2 racetrack laps NE to ~3.9 km
    at 12-18 m/s with 8 deg/s turns, lands.  Radar tracks: az bias, el bias,
    ~25 m rms error, new track id at each FAR turn, one mid-leg speed-filter
    kill (last state vU=150 m/s) with a 30 s hole, one track with U pinned at
    +100 m for its last 30 %, hover/pad untracked (MDV 3.2 m/s), a stationary
    CLUTTER track 60 m from the pad, an ADS-B-like 120 m/s track 8 km away.
    No interceptor feed in flight 1.
  Flight 2 (engagement, ~10 min, 20 min later): interceptor (three duplicate
    ids mav14551_2_0/_2_1/_2_34, ~40 % of samples each, union complete) launches
    from a pad 150 m E of the target pad.  Passes: A parked-interceptor dip
    (~400 m, must NOT count), B 60 m crossing, C 25 m head-on -> target track
    STOLEN (rides interceptor ~40 m), D 45 m crossing with an 8 s / 120 m state
    runaway (identity holds) while the TARGET FEED IS FROZEN (1 Hz identical
    repeats for 25 s), E 70 m head-on mirror steal (track born on the
    interceptor 40 s earlier flips to the target).  Also: 2.8 km GPS teleport
    single sample in the interceptor feed + 20 s union gap after it, drag-and-
    die track (pulled 110 m off target, dies 20 s after pass B), stale 1 Hz
    republish tails after landing (both feeds).
"""
from __future__ import annotations

import argparse, json, math, os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

DT, MDV, FT = 0.5, 3.2, 0.3048
MAV_COLS = ["t_epoch", "time_pdt", "lat", "lon", "alt_ft_wire", "E_m", "N_m", "U_m_hae",
            "vel_n_mps", "vel_e_mps", "vert_spd_wire_ftmin", "validposition"]
TRK_COLS = ["t_epoch", "time_pdt", "E_m", "N_m", "U_m", "vE_mps", "vN_mps", "vU_mps",
            "sigE_m", "sigN_m", "sigU_m", "total_associations", "track_state", "last_update_t"]
TZ = ZoneInfo("America/Los_Angeles")


def iso(t): return datetime.fromtimestamp(float(t), TZ).isoformat(timespec="microseconds")
def hms(t): return datetime.fromtimestamp(float(t), TZ).strftime("%H:%M:%S")
def win(t0, t1): return dict(t0=round(float(t0), 1), t1=round(float(t1), 1), t0_pdt=hms(t0), t1_pdt=hms(t1))


# ----------------------------------------------------------------------------- kinematic path builder
class Path:
    """2 Hz kinematic path: straights, constant-rate turns, hovers, goto; 3 m/s^2 accel limit."""

    def __init__(self, t0, E, N, U, hdg):
        self.t0, self.p, self.hdg, self.v, self.rows, self.legs = t0, [E, N, U], hdg, 0.0, [], {}

    @property
    def t(self): return self.t0 + DT * len(self.rows)

    def _step(self, v, dU=0.0, dh=0.0):
        self.v = min(v, self.v + 3 * DT) if v > self.v else max(v, self.v - 3 * DT)
        self.hdg = (self.hdg + dh) % 360
        h = math.radians(self.hdg); vE, vN = self.v * math.sin(h), self.v * math.cos(h)
        self.p[0] += vE * DT; self.p[1] += vN * DT; self.p[2] += dU
        self.rows.append((self.p[0], self.p[1], self.p[2], vE, vN, dU / DT))

    def _leg(self, name, fn):
        a = self.t; fn(); self.legs[name] = (a, self.t); return self

    def hover(self, dur, dU=0.0, name="hover"):
        n = int(round(dur / DT)); return self._leg(name, lambda: [self._step(0.0, dU / n) for _ in range(n)])

    def straight(self, dist, v, name):
        return self._leg(name, lambda: [self._step(v) for _ in range(int(round(dist / v / DT)))])

    def turn(self, ang, v, name, rate=8.0):
        n = math.ceil(abs(ang) / rate / DT - 1e-9); s = ang / n          # exact total angle
        return self._leg(name, lambda: [self._step(v, dh=s) for _ in range(n)])

    def goto(self, E, N, v, name, rate=8.0):
        def run():
            while math.hypot(E - self.p[0], N - self.p[1]) > v * DT:
                want = math.degrees(math.atan2(E - self.p[0], N - self.p[1]))
                self._step(v, dh=float(np.clip((want - self.hdg + 180) % 360 - 180, -rate * DT, rate * DT)))
        return self._leg(name, run)

    def arrays(self):
        a = np.array(self.rows)
        return dict(t=self.t0 + DT * np.arange(len(a)), E=a[:, 0], N=a[:, 1], U=a[:, 2], vE=a[:, 3], vN=a[:, 4], vU=a[:, 5])


def leg_t(path, name, frac):
    a, b = path.legs[name]; return float(round(a + frac * (b - a)))


def interp_fn(ar):
    return lambda t: {k: np.interp(t, ar["t"], ar[k]) for k in ("E", "N", "U", "vE", "vN", "vU")}


def hermite(knots, t):
    """Piecewise cubic Hermite through (t, pos[3], vel[3]) knots; clamps outside."""
    kt = np.array([k[0] for k in knots]); P = np.array([k[1] for k in knots], float); V = np.array([k[2] for k in knots], float)
    i = np.clip(np.searchsorted(kt, t, "right") - 1, 0, len(kt) - 2)
    h = (kt[i + 1] - kt[i])[:, None]; s = np.clip((t - kt[i]) / h[:, 0], 0, 1)[:, None]
    pos = (2 * s**3 - 3 * s**2 + 1) * P[i] + (s**3 - 2 * s**2 + s) * h * V[i] + (-2 * s**3 + 3 * s**2) * P[i + 1] + (s**3 - s**2) * h * V[i + 1]
    vel = ((6 * s**2 - 6 * s) * P[i] + (3 * s**2 - 4 * s + 1) * h * V[i] + (-6 * s**2 + 6 * s) * P[i + 1] + (3 * s**2 - 2 * s) * h * V[i + 1]) / h
    return pos, vel


def design_pass(tgt_f, tp, miss, angle_deg, speed, hfrac, vsign):
    """Interceptor passes the target at tp with 3-D miss `miss`: velocity rotated `angle_deg`
    from the target heading (0 tail-chase, 180 head-on, +-90 crossing); the miss vector is
    perpendicular to the relative velocity (hfrac horizontal, rest vertical, sign vsign)."""
    T = tgt_f(np.array([tp])); pT = np.array([T["E"][0], T["N"][0], T["U"][0]]); vT = np.array([T["vE"][0], T["vN"][0], 0.0])
    hT = vT[:2] / np.linalg.norm(vT[:2]); a = math.radians(angle_deg)
    u = np.array([hT[0] * math.cos(a) - hT[1] * math.sin(a), hT[0] * math.sin(a) + hT[1] * math.cos(a)])
    vI = np.array([u[0] * speed, u[1] * speed, 0.0]); rel = vI - vT
    n = np.array([-rel[1], rel[0], 0.0]); n /= np.linalg.norm(n)
    dh = miss * hfrac; dv = vsign * math.sqrt(miss**2 - dh**2)
    return pT + dh * n + np.array([0, 0, dv]), vI, dict(horiz_m=round(dh, 1), vert_m=round(dv, 1), geometry=
                                                        {0: "tail-chase", 180: "head-on"}.get(angle_deg % 360, "crossing"))


# ----------------------------------------------------------------------------- truth feed frames
def mav_frame(t, E, N, U, vN, vE, vU, site):
    lat0, lon0, h0, gn = site
    lat = lat0 + N / 111320.0; lon = lon0 + E / (111320.0 * math.cos(math.radians(lat0)))
    return pd.DataFrame(dict(t_epoch=np.round(t, 3), time_pdt=[iso(x) for x in t], lat=np.round(lat, 7), lon=np.round(lon, 7),
                             alt_ft_wire=(h0 + U - gn) / FT, E_m=np.round(E, 2), N_m=np.round(N, 2), U_m_hae=np.round(U, 2),
                             vel_n_mps=vN, vel_e_mps=vE, vert_spd_wire_ftmin=vU * 60 / FT, validposition=1))[MAV_COLS]


def truth_df(ar, site, rng, keep=None):
    """Truth arrays -> wire frame with GPS jitter (0.3 m horiz, 0.5 m vert, 10 ms time)."""
    n = len(ar["t"]); m = np.ones(n, bool) if keep is None else keep
    return mav_frame(ar["t"][m] + rng.normal(0, 0.01, m.sum()), ar["E"][m] + rng.normal(0, 0.3, m.sum()),
                     ar["N"][m] + rng.normal(0, 0.3, m.sum()), ar["U"][m] + rng.normal(0, 0.5, m.sum()),
                     ar["vN"][m] + rng.normal(0, 0.05, m.sum()), ar["vE"][m] + rng.normal(0, 0.05, m.sum()), ar["vU"][m], site)


def repeat_rows(ref, times):
    rows = pd.DataFrame([ref] * len(times)); rows["t_epoch"] = np.round(times, 3); rows["time_pdt"] = [iso(x) for x in times]
    return rows


def stale_tail(df, dur):
    """Legacy capture_mavlink: republish the last fix at exactly 1 Hz once the sender goes quiet."""
    last = df.iloc[-1]; return pd.concat([df, repeat_rows(last, last.t_epoch + np.arange(1, int(dur) + 1))], ignore_index=True)


def freeze(df, t0, dur):
    """Replace [t0, t0+dur) with 1 Hz identical repeats of the last good fix (frozen feed)."""
    good = df[df.t_epoch < t0]
    return pd.concat([good, repeat_rows(good.iloc[-1], t0 + np.arange(int(dur))), df[df.t_epoch >= t0 + dur]], ignore_index=True)


def split_ids(df, ids, rng, dup_p=0.2, switch_p=0.15):
    """Spread one feed over duplicate ids: Markov-switching primary id (~3 s runs) + 20 % duplicated
    samples on a second id (t + 4 ms) -> each id carries ~40 %, union complete."""
    n = len(df); prim = np.zeros(n, int); k = rng.integers(len(ids))
    for i in range(n):
        if rng.random() < switch_p: k = (k + rng.integers(1, len(ids))) % len(ids)
        prim[i] = k
    dup = rng.random(n) < dup_p; out = {}
    for j, tid in enumerate(ids):
        d = df[dup & (prim != j) & (rng.random(n) < 0.5)].copy(); d["t_epoch"] = np.round(d.t_epoch + 0.004, 3)
        out[tid] = pd.concat([df[prim == j], d]).sort_values("t_epoch").reset_index(drop=True)
    return out


# ----------------------------------------------------------------------------- radar tracks
def follow_switch(fa, fb, ts, tau=3.0):
    def f(t):
        a, b = fa(t), fb(t); w = np.clip((t - ts) / tau, 0, 1); return {k: a[k] * (1 - w) + b[k] * w for k in a}
    return f


def follow_offset(fn, d):
    def f(t):
        a = dict(fn(t)); a["E"] = a["E"] + d[0]; a["N"] = a["N"] + d[1]; a["U"] = a["U"] + d[2]; return a
    return f


def debias(fn, az_deg, el_deg):
    """Pre-rotate a follow path by -bias so the PUBLISHED (biased) track lands on it (used to pin
    the stolen track's post-pass median distance to the interceptor at the specified ~40 m)."""
    def f(t):
        a = dict(fn(t)); r = np.sqrt(a["E"]**2 + a["N"]**2 + a["U"]**2)
        az = np.arctan2(a["E"], a["N"]) - math.radians(az_deg); el = np.arctan2(a["U"], np.hypot(a["E"], a["N"])) - math.radians(el_deg)
        a["E"], a["N"], a["U"] = r * np.cos(el) * np.sin(az), r * np.cos(el) * np.cos(az), r * np.sin(el); return a
    return f


def pulse(t0, t1, mag, d, ramp=2.0):
    """Displacement (dE,dN,dU)(t): ramps to mag*d over `ramp` s after t0, back down before t1."""
    return lambda t: tuple(np.clip((t - t0) / ramp, 0, 1) * np.clip((t1 - t) / ramp, 0, 1) * mag * c for c in d)


def make_track(follow, t0, t1, rng, az_bias, el_bias, err, p_meas=0.8, coast=(), pinned=None, offset=None,
               kill_vU=None, sig0=(20, 60)):
    """Tracker emulation: ride follow(t) (+offset), measure in polar with az/el bias and AR(1) noise
    (~err m rms horizontal), 2 Hz publish, measurement updates w.p. p_meas (else coast: extrapolate,
    last_update_t frozen, sigmas inflate), tentative 3 s then confirmed."""
    t = np.arange(t0, t1, DT) + rng.normal(0, 0.02, len(np.arange(t0, t1, DT))); n = len(t); f = follow(t)
    E, N, U = f["E"].copy(), f["N"].copy(), f["U"].copy()
    if offset is not None:
        dE, dN, dU = offset(t); E, N, U = E + dE, N + dN, U + dU
    r = np.sqrt(E**2 + N**2 + U**2); az = np.arctan2(E, N); el = np.arctan2(U, np.hypot(E, N))

    def ar1(sig):
        x = np.zeros(n); x[0] = rng.normal(0, sig)
        for i in range(1, n): x[i] = 0.7 * x[i - 1] + rng.normal(0, sig * math.sqrt(1 - 0.49))
        return x
    s = err / 25.0
    rm, azm, elm = r + ar1(12 * s), az + math.radians(az_bias) + ar1(math.radians(0.42 * s)), el + math.radians(el_bias) + ar1(math.radians(0.3 * s))
    Em, Nm, Um = rm * np.cos(elm) * np.sin(azm), rm * np.cos(elm) * np.cos(azm), rm * np.sin(elm)
    vE, vN, vU = f["vE"] + rng.normal(0, 0.6, n), f["vN"] + rng.normal(0, 0.6, n), f["vU"] + rng.normal(0, 0.4, n)
    meas = rng.random(n) < p_meas; meas[0] = True
    for a, b in coast: meas &= ~((t >= a) & (t <= b))
    lut, sig, assoc = np.zeros(n), np.zeros((n, 3)), np.zeros(n, int)
    base = sig0[0] + (sig0[1] - sig0[0]) * np.clip(r / 5000, 0, 1)
    for i in range(n):
        if meas[i]:
            lut[i] = t[i] - rng.uniform(0.2, 0.6); assoc[i] = (assoc[i - 1] if i else 0) + 1; sig[i] = base[i] * rng.uniform(0.8, 1.2, 3)
        else:
            lut[i], assoc[i], sig[i] = lut[i - 1], assoc[i - 1], sig[i - 1] * 1.15
            Em[i], Nm[i], Um[i] = Em[i - 1] + vE[i - 1] * DT, Nm[i - 1] + vN[i - 1] * DT, Um[i - 1] + vU[i - 1] * DT
    if pinned:
        k = int(n * (1 - pinned)); Um[k:] = 100.0 + rng.normal(0, 0.03, n - k); vU[k:] = rng.normal(0, 0.05, n - k)
    if kill_vU is not None: vU[-1] = kill_vU
    return pd.DataFrame(dict(t_epoch=np.round(t, 3), time_pdt=[iso(x) for x in t], E_m=np.round(Em, 2), N_m=np.round(Nm, 2),
                             U_m=np.round(Um, 2), vE_mps=np.round(vE, 2), vN_mps=np.round(vN, 2), vU_mps=np.round(vU, 2),
                             sigE_m=np.round(sig[:, 0], 2), sigN_m=np.round(sig[:, 1], 2), sigU_m=np.round(sig[:, 2], 2),
                             total_associations=assoc, track_state=np.where(t - t[0] < 3.0, 1, 2), last_update_t=np.round(lut, 2)))[TRK_COLS]


# ----------------------------------------------------------------------------- answer-key helpers
def flight_window(ar, ground):
    t = ar["t"][ar["U"] > ground + 5]; return float(t[0]), float(t[-1])


def slow_intervals(ar, t0, t1):
    m = (ar["t"] >= t0) & (ar["t"] <= t1); t = ar["t"][m]; slow = np.hypot(ar["vE"], ar["vN"])[m] <= MDV
    return [win(t[r[0]], t[r[-1]]) for r in np.split(np.arange(len(t)), np.where(np.diff(slow.astype(int)) != 0)[0] + 1)
            if slow[r[0]] and len(r) >= 4]


def coverage_pct(ar, ground, tracks, t0, t1):
    """Builder definition: % of 1 Hz moving samples with a fresh (t-lut<=1.5 s) on-target (<150 m)
    state from a target-side track within 1.5 s."""
    grid = np.arange(math.ceil(t0), math.floor(t1) + 1.0); f = interp_fn(ar)(grid)
    moving = (f["U"] > ground + 5) & (np.hypot(f["vE"], f["vN"]) > 2)
    S = [df.t_epoch.to_numpy()[((df.t_epoch - df.last_update_t) <= 1.5).to_numpy() &
                               (np.hypot(df.E_m - np.interp(df.t_epoch, ar["t"], ar["E"]), df.N_m - np.interp(df.t_epoch, ar["t"], ar["N"])) < 150).to_numpy()]
         for df in tracks]
    S = np.sort(np.concatenate(S)); near = np.abs(grid[:, None] - S[None, :]).min(1)
    return round(100 * ((near <= 1.5) & moving).sum() / max(moving.sum(), 1), 1)


def min_sep(fa, fb, t0, t1):
    g = np.arange(math.ceil(t0), math.floor(t1) + 1.0); a, b = fa(g), fb(g)
    sep = np.sqrt((a["E"] - b["E"])**2 + (a["N"] - b["N"])**2 + (a["U"] - b["U"])**2); i = int(np.argmin(sep))
    return float(g[i]), round(float(sep[i]), 1)


def dips(fa, fb, t0, t1, max_m=600.0, prom_m=80.0):
    """Builder-equivalent pass finder on clean truth: 1 Hz 3-D separation local minima < max_m
    with >= prom_m prominence -> [(t, sep_m, prominence_m)]."""
    g = np.arange(math.ceil(t0), math.floor(t1) + 1.0); a, b = fa(g), fb(g)
    sep = np.sqrt((a["E"] - b["E"])**2 + (a["N"] - b["N"])**2 + (a["U"] - b["U"])**2); out = []
    for i in range(1, len(sep) - 1):
        if not (sep[i] < max_m and sep[i] <= sep[i - 1] and sep[i] < sep[i + 1]): continue
        side = []
        for arr in (sep[:i][::-1], sep[i + 1:]):
            lower = np.where(arr < sep[i])[0]; side.append(np.max(arr[:lower[0]] if len(lower) else arr))
        if min(side) - sep[i] >= prom_m: out.append((float(g[i]), round(float(sep[i]), 1), round(float(min(side) - sep[i]))))
    return out


def write_dump(d, mav, tracks, meta):
    os.makedirs(os.path.join(d, "mavlink"), exist_ok=True); os.makedirs(os.path.join(d, "tracks"), exist_ok=True)
    for tid, df in mav.items(): df.to_csv(os.path.join(d, "mavlink", f"{tid}.csv"), index=False)
    for tid, df in tracks.items(): df.to_csv(os.path.join(d, "tracks", f"track_{tid}.csv"), index=False)
    meta.update(mavlink_rows=int(sum(len(v) for v in mav.values())), track_rows=int(sum(len(v) for v in tracks.values())), tracks=len(tracks))
    json.dump(meta, open(os.path.join(d, "meta.json"), "w"), indent=1)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True); ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--day", default="2026-10-01"); ap.add_argument("--start", default="08:00", help="flight-1 feed start, local HH:MM")
    ap.add_argument("--tz", default="America/Los_Angeles"); ap.add_argument("--run", default="synth001")
    ap.add_argument("--antenna", nargs=3, type=float, default=[33.748056, -115.339204, 140.5], metavar=("LAT", "LON", "HAE"))
    ap.add_argument("--geoid-n", type=float, default=-31.4)
    ap.add_argument("--az-bias", type=float, default=-1.5); ap.add_argument("--el-bias", type=float, default=0.5)
    ap.add_argument("--err-rms", type=float, default=25.0); ap.add_argument("--p-meas", type=float, default=0.8)
    ap.add_argument("--pass-miss", nargs=4, type=float, default=[60, 25, 45, 70], metavar=("B", "C", "D", "E"))
    ap.add_argument("--pass-a-offset", type=float, default=390.0, help="horizontal offset of the L3 leg from the interceptor pad")
    for k in ("steal", "freeze", "teleport", "corruption", "drag", "pinned", "kill", "clutter", "adsb", "dup-ids", "stale"):
        ap.add_argument(f"--no-{k}", action="store_true")
    a = ap.parse_args()
    global TZ; TZ = ZoneInfo(a.tz)
    rng = np.random.default_rng(a.seed); site = (*a.antenna, a.geoid_n)
    lat0, lon0, h0 = a.antenna; PAD = (1860.0, 247.0, -13.0); IPAD = (PAD[0] + 150.0, PAD[1], PAD[2])
    T1 = datetime.strptime(f"{a.day} {a.start}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    bias = dict(az_bias=a.az_bias, el_bias=a.el_bias, err=a.err_rms, p_meas=a.p_meas)
    trk = lambda follow, t0, t1, **kw: make_track(follow, t0, t1, rng, **{**bias, **kw})
    daydir = os.path.join(a.out, a.day); tgt_id, itc_ids = "mav14550_1_1", (["mav14551_2_1"] if a.no_dup_ids else ["mav14551_2_0", "mav14551_2_1", "mav14551_2_34"])
    meta_base = dict(run=a.run, run_id8=a.run, antenna=list(a.antenna), antenna_origin_lat_lon_haeM=list(a.antenna),
                     geoid_n=a.geoid_n, synthetic=True, generator="make_synthetic_day.py", seed=a.seed)
    key = dict(day=a.day, tz=a.tz, run=a.run, seed=a.seed, antenna=list(a.antenna), geoid_n=a.geoid_n, pads=dict(target=PAD, interceptor=IPAD),
               injected_bias=dict(az_deg=a.az_bias, el_deg=a.el_bias, horiz_err_rms_m=a.err_rms, sign_convention="track minus truth"),
               tracker=dict(publish_hz=2.0, meas_rate_hz=round(2.0 * a.p_meas, 2), mdv_mps=MDV, tentative_s=3.0, sigma_range_m=[20, 60]),
               target_id=tgt_id, interceptor_ids=itc_ids, flights=[])

    # ======================================================================= FLIGHT 1 - tracking racetrack
    p = Path(T1, *PAD, 45.0).hover(60, name="parked_pre").hover(40, dU=90, name="climb")
    for lap, (vo, vi) in enumerate(((15, 18), (12, 16)), 1):
        p.straight(2300, vo, f"lap{lap}_out").turn(180, 15, f"lap{lap}_farturn").straight(2300, vi, f"lap{lap}_in").turn(180, 15, f"lap{lap}_nearturn")
    p.goto(PAD[0], PAD[1], 8, "repos").hover(40, dU=-90, name="descend").hover(60, name="parked_post")
    A1 = p.arrays(); f1 = interp_fn(A1); w1 = flight_window(A1, PAD[2])
    mv = A1["t"][np.hypot(A1["vE"], A1["vN"]) > MDV]; mv0, mv1 = float(mv[0]), float(mv[-1])
    L = p.legs; ft1, ft2, in2 = L["lap1_farturn"][0], L["lap2_farturn"][0], L["lap2_in"]
    t_kill = float(round(in2[0] + 0.4 * (in2[1] - in2[0]))); coast1 = (L["lap1_in"][0] + 40, L["lap1_in"][0] + 48)
    tracks1 = {101: trk(f1, mv0 + 2, ft1 + 8),
               117: trk(f1, ft1 + 14, ft2 + 8, coast=[coast1], pinned=None if a.no_pinned else 0.3)}
    if a.no_kill:
        tracks1[133] = trk(f1, ft2 + 14, mv1 - 1)
    else:
        tracks1[133] = trk(f1, ft2 + 14, t_kill, kill_vU=150.0); tracks1[152] = trk(f1, t_kill + 30, mv1 - 1)
    tgt_side1 = list(tracks1)
    if not a.no_clutter:
        cpos = dict(E=PAD[0] + 42.4, N=PAD[1] + 42.4, U=PAD[2] + 1, vE=0, vN=0, vU=0)
        tracks1[96] = trk(lambda t: {k: np.full(len(t), v, float) for k, v in cpos.items()}, T1 + 5, A1["t"][-1] + 60, err=2.0, p_meas=0.6)
    if not a.no_adsb:
        ads = lambda t: dict(E=-6000 + 120 * (t - (mv0 + 200)), N=np.full(len(t), 6500.0), U=np.full(len(t), 1500.0), vE=np.full(len(t), 120.0), vN=np.zeros(len(t)), vU=np.zeros(len(t)))
        tracks1[140] = trk(ads, mv0 + 200, mv0 + 350, err=60.0, sig0=(80, 200))
    mav1 = {tgt_id: truth_df(A1, site, rng)}
    if not a.no_stale: mav1[tgt_id] = stale_tail(mav1[tgt_id], 90)
    d1 = os.path.join(daydir, f"{a.run}_flight1")
    write_dump(d1, mav1, tracks1, dict(meta_base, what=f"flight-1 quickdump {a.day} {hms(T1)}-{hms(A1['t'][-1])} tracking racetrack (synthetic)"))
    key["flights"].append(dict(
        n=1, kind="tracking", dump=os.path.basename(d1), window=win(*w1), moving_window=win(mv0, mv1), interceptor_feed_present=False,
        laps=[dict(n=k, **win(L[f"lap{k}_out"][0], L[f"lap{k}_nearturn"][1]), far_turn=win(*L[f"lap{k}_farturn"]), speeds_mps=[vo, vi])
              for k, (vo, vi) in enumerate(((15, 18), (12, 16)), 1)],
        target_side_tracks=tgt_side1,
        turn_handovers=[dict(t_pdt=hms(ft1 + 14), from_track=101, to_track=117, hole=win(ft1 + 8, ft1 + 14), hole_s=6.0, at="lap1_farturn"),
                        dict(t_pdt=hms(ft2 + 14), from_track=117, to_track=133, hole=win(ft2 + 8, ft2 + 14), hole_s=6.0, at="lap2_farturn")],
        speed_filter_kill=None if a.no_kill else dict(track=133, last_state_t=round(t_kill, 1), last_state_pdt=hms(t_kill), last_vU_mps=150.0,
                                                      hole=win(t_kill, t_kill + 30), hole_s=30.0, next_track=152, leg="lap2_in"),
        forced_coast=dict(track=117, **win(*coast1), note="last_update_t frozen 8 s -> uncovered (coasting states published)"),
        pinned_altitude_track=None if a.no_pinned else dict(track=117, last_fraction=0.3, U_m=100.0,
                                                            **win(tracks1[117].t_epoch.iloc[int(len(tracks1[117]) * 0.7)], tracks1[117].t_epoch.iloc[-1])),
        clutter_track=None if a.no_clutter else dict(track=96, E_m=round(PAD[0] + 42.4, 1), N_m=round(PAD[1] + 42.4, 1), dist_from_pad_m=60.0,
                                                     trap="within 150 m of the target during pad hover/climb -> naive on-target counting makes it a rider spanning the whole flight"),
        adsb_like_track=None if a.no_adsb else dict(track=140, speed_mps=120.0, U_m=1500.0, range_km="~8-9"),
        mdv_untracked_intervals=slow_intervals(A1, *w1),
        expected_coverage_pct=coverage_pct(A1, PAD[2], [tracks1[i] for i in tgt_side1], *w1),
        stale_tail=None if a.no_stale else dict(feed=tgt_id, **win(A1["t"][-1] + 1, A1["t"][-1] + 90), rate_hz=1.0)))

    # ======================================================================= FLIGHT 2 - engagement
    T2 = float(A1["t"][-1]) + 20 * 60
    q = Path(T2, *PAD, 270.0).hover(60, name="parked_pre").hover(40, dU=90, name="climb").straight(800, 15, "L1_W").turn(90, 15, "T1")
    q.straight(a.pass_a_offset - 2 * 15 / math.radians(8), 15, "L2_N").turn(90, 15, "T2").straight(3600, 15, "L3_E").turn(180, 15, "T3")
    q.straight(3600, 15, "L4_W").goto(PAD[0], PAD[1], 15, "return").hover(40, dU=-90, name="descend").hover(60, name="parked_post")
    A2 = q.arrays(); f2 = interp_fn(A2); w2 = flight_window(A2, PAD[2]); Lq = q.legs
    mv = A2["t"][np.hypot(A2["vE"], A2["vN"]) > MDV]; mv0, mv1 = float(mv[0]), float(mv[-1])
    pad_f = lambda t: dict(E=np.full(len(t), IPAD[0]), N=np.full(len(t), IPAD[1]), U=np.full(len(t), IPAD[2]), vE=0 * t, vN=0 * t, vU=0 * t)
    tA, missA = min_sep(f2, pad_f, *Lq["L3_E"]); tL = tA + 15.0
    tB, tC = tL + 65.0, tL + 143.0; tD = leg_t(q, "L4_W", 0.30); tE = leg_t(q, "L4_W", 0.65)
    assert tC <= Lq["L3_E"][1] - 15, "pass C must sit on the straight L3 leg"
    W = 12.0; spec = [("B", tB, a.pass_miss[0], 90, 22, 0.8, -1), ("C", tC, a.pass_miss[1], 180, 25, 0.8, 1),
                      ("D", tD, a.pass_miss[2], -90, 22, 0.8, -1), ("E", tE, a.pass_miss[3], 180, 25, 0.85, 1)]
    knots = [(tL, IPAD, (0, 0, 0)), (tL + 12, (IPAD[0], IPAD[1], 60.0), (0, 0, 0))]; passes = []
    for name, tp, miss, ang, spd, hf, vs in spec:
        pos, vI, info = design_pass(f2, tp, miss, ang, spd, hf, vs)
        knots += [(tp - W, pos - W * vI, vI), (tp + W, pos + W * vI, vI)]
        passes.append(dict(name=name, t_epoch=tp, t_pdt=hms(tp), miss_m=miss, **info, interceptor_speed_mps=spd, realistic=True, interceptor_grounded=False))
    t_iland = tE + W + 80.0
    knots += [(tE + W + 60, (IPAD[0], IPAD[1], 30.0), (0, 0, 0)), (t_iland, IPAD, (0, 0, 0))]
    ti = np.arange(T2, t_iland + 30, DT); ipos, ivel = hermite(knots, ti)
    I2 = dict(t=ti, E=ipos[:, 0], N=ipos[:, 1], U=ipos[:, 2], vE=ivel[:, 0], vN=ivel[:, 1], vU=ivel[:, 2]); fi = interp_fn(I2)
    for pz in passes:  # exact 1 Hz truth-truth minimum (what an analyst with clean feeds recovers)
        tmin, smin = min_sep(f2, fi, pz["t_epoch"] - 20, pz["t_epoch"] + 20); pz.update(true_min_sep_1hz_m=smin, true_min_t_pdt=hms(tmin))
    designed = [pz["t_epoch"] for pz in passes] + [tA]
    incidental = [dict(t_epoch=t, t_pdt=hms(t), sep_m=sm, prominence_m=pm, note="NOT a designed pass: interceptor manoeuvring between passes")
                  for t, sm, pm in dips(f2, fi, tL, t_iland) if min(abs(t - d) for d in designed) > 5]
    passes.insert(0, dict(name="A", t_epoch=tA, t_pdt=hms(tA), miss_m=missA, geometry="target overflight of the PARKED interceptor",
                          realistic=False, interceptor_grounded=True, reason="interceptor still on its pad (lifts off %s) - dip vs parked interceptor, must NOT count" % hms(tL)))
    if not a.no_freeze:
        pD = next(pz for pz in passes if pz["name"] == "D"); pD["truth_frozen"] = True
        pD["reason"] = "target feed FROZEN %s-%s (1 Hz identical repeats) - CPA not recomputable from the feed; flag as unreliable" % (hms(tD - 12), hms(tD + 12))
    # ---- interceptor feed: teleport + 20 s gap, jitter, stale tail, duplicate ids
    Iw = {k: v.copy() for k, v in I2.items()}; keep = np.ones(len(ti), bool); tele = None
    if not a.no_teleport:
        k = int(np.argmin(np.abs(ti - (tL + 15)))); Iw["E"][k] += 2800.0; keep &= ~((ti > ti[k]) & (ti <= ti[k] + 20))
        tele = dict(feed="interceptor (whichever duplicate id carries the sample)", t_epoch=round(float(ti[k]), 1), t_pdt=hms(ti[k]),
                    displacement_m=2800.0, direction="E", gap_after=win(ti[k] + DT, ti[k] + 20), gap_s=20.0)
    idf = truth_df(Iw, site, rng, keep)
    if not a.no_stale: idf = stale_tail(idf, 60)
    mav2 = {tgt_id: truth_df(A2, site, rng)}
    if not a.no_freeze: mav2[tgt_id] = freeze(mav2[tgt_id], tD - 12, 25)
    if not a.no_stale: mav2[tgt_id] = stale_tail(mav2[tgt_id], 60)
    mav2.update(split_ids(idf, itc_ids, rng) if len(itc_ids) > 1 else {itc_ids[0]: idf})
    # ---- tracks
    tB_pos, iB = f2(np.array([tB])), fi(np.array([tB])); u = np.array([iB["E"][0] - tB_pos["E"][0], iB["N"][0] - tB_pos["N"][0], 0.0]); u /= np.linalg.norm(u)
    tracks2, tgt_side2, itc_side2 = {}, [201, 214, 231], [209, 236, 244]
    tracks2[201] = trk(f2, mv0 + 2, tB + 20, offset=None if a.no_drag else pulse(tB, 1e12, 110, u, ramp=20))
    tracks2[209] = trk(fi, tB - 10, tB + 12)
    steal_dir = np.array([0.7, 0.7, 0.14]) * 30 / np.linalg.norm([0.7, 0.7, 0.14])
    tracks2[214] = trk(f2 if a.no_steal else follow_switch(f2, debias(follow_offset(fi, steal_dir), a.az_bias, a.el_bias), tC), tB - 5, tC + 40)
    tracks2[231] = trk(f2, tC + 2, tE + 10, offset=None if a.no_corruption else pulse(tD + 2, tD + 10, 120, (-0.6, 0.8, 0.0)))
    tracks2[236] = trk(fi, tC + 20, tC + 55); tracks2[244] = trk(fi, tD - 30, tD - 5)
    tracks2[260] = trk(f2 if a.no_steal else follow_switch(fi, f2, tE), tE - 40 if not a.no_steal else tE + 8, mv1 - 1)
    tgt_side2.append(260)
    d2 = os.path.join(daydir, f"{a.run}_flight2")
    write_dump(d2, mav2, tracks2, dict(meta_base, what=f"flight-2 quickdump {a.day} {hms(T2)}-{hms(max(A2['t'][-1], ti[-1]))} engagement (synthetic)"))
    steals = [] if a.no_steal else [
        dict(track=214, after_pass="C", pass_t_pdt=hms(tC), direction="target->interceptor", ride_offset_m=30.0, ride_offset_note="specified in PUBLISHED space (bias-compensated) so the post-pass median is ~40 m",
             expected_post_pass_median_dist_to_interceptor_m="~40", rides_until=hms(tC + 40), track_end_pdt=hms(tC + 40)),
        dict(track=260, after_pass="E", pass_t_pdt=hms(tE), direction="interceptor->target", born_on_interceptor_at=hms(tE - 40),
             born_s_before_pass=40.0, rides_target_until=hms(mv1 - 1), note="mirror / reverse capture")]
    key["flights"].append(dict(
        n=2, kind="engagement", dump=os.path.basename(d2), window=win(*w2), moving_window=win(mv0, mv1), interceptor_feed_present=True,
        interceptor=dict(launch=win(tL, t_iland), launch_pdt=hms(tL), land_pdt=hms(t_iland), pad=IPAD, pad_dist_from_target_pad_m=150.0,
                         duplicate_id_map=dict(ids=itc_ids, share_per_id="~0.40 (1/3 primary + ~7 % duplicated samples, +4 ms)", union="complete at 2 Hz")),
        legs={k: win(*v) for k, v in Lq.items()}, passes=passes, incidental_dips=incidental, steal_events=steals,
        corruption_event=None if a.no_corruption else dict(track=231, during_pass="D", **win(tD + 2, tD + 10), displacement_m=120.0, duration_s=8.0,
                                                          note="state runs away 120 m and returns; identity holds (no steal)"),
        drag_and_die_track=None if a.no_drag else dict(track=201, after_pass="B", pulled_m=110.0, toward="interceptor", **win(tB, tB + 20),
                                                       dies_pdt=hms(tB + 20), successor=214),
        frozen_interval=None if a.no_freeze else dict(feed=tgt_id, **win(tD - 12, tD + 12), rows=25, rate_hz=1.0, around_pass="D"),
        teleport=tele, target_side_tracks=tgt_side2, interceptor_side_tracks=itc_side2,
        mdv_untracked_intervals=slow_intervals(A2, *w2),
        expected_coverage_pct=coverage_pct(A2, PAD[2], [tracks2[i] for i in tgt_side2], *w2),
        stale_tails=[] if a.no_stale else [dict(feed=tgt_id, **win(A2["t"][-1] + 1, A2["t"][-1] + 60), rate_hz=1.0),
                                           dict(feed="interceptor ids", **win(ti[-1] + 1, ti[-1] + 60), rate_hz=1.0)]))
    key["notes"] = ["truth-feed velocities are wire values (vel_n/e m/s, vert_spd ft/min); alt_ft_wire is FEET MSL",
                    "flight-1 dump has NO interceptor feed; parked interceptor in flight 2 has GPS jitter (not frozen)",
                    "expected_coverage_pct uses the builder definition on the clean truth (freeze not applied)"]
    json.dump(key, open(os.path.join(daydir, "ANSWER_KEY.json"), "w"), indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))

    # ----------------------------------------------------------------------- summary
    print(f"wrote {daydir}  (run {a.run}, seed {a.seed}, az {a.az_bias:+.2f} el {a.el_bias:+.2f} err {a.err_rms:.0f} m)")
    for fl in key["flights"]:
        print(f" F{fl['n']} {fl['kind']:<10} {fl['window']['t0_pdt']}-{fl['window']['t1_pdt']}  target-side tracks {fl['target_side_tracks']}"
              f"  expected coverage ~{fl['expected_coverage_pct']}%  MDV-untracked {[(m['t0_pdt'], m['t1_pdt']) for m in fl['mdv_untracked_intervals']]}")
    for pz in passes:
        print(f"   pass {pz['name']} {pz['t_pdt']} miss {pz['miss_m']:>5} m  {pz.get('geometry', '')}  grounded={pz['interceptor_grounded']} "
              f"realistic={pz['realistic']} 1Hz-true-min={pz.get('true_min_sep_1hz_m', '-')}" + (f"  [{pz['reason']}]" if pz.get("reason") else ""))
    print(f"   incidental (non-designed) dips <600 m/80 m prom: {[(d['t_pdt'], d['sep_m'], d['prominence_m']) for d in incidental]}"
          f"   interceptor max speed {np.hypot(I2['vE'], I2['vN']).max():.0f} m/s")
    for s in steals: print(f"   steal: track {s['track']} {s['direction']} after pass {s['after_pass']} ({s['pass_t_pdt']})")
    F1, F2 = key["flights"]
    for lbl, v in (("turn handovers", [(h['from_track'], h['to_track'], h['t_pdt']) for h in F1['turn_handovers']]), ("speed-filter kill", F1["speed_filter_kill"]),
                   ("pinned altitude", F1["pinned_altitude_track"]), ("clutter", F1["clutter_track"]), ("adsb-like", F1["adsb_like_track"]),
                   ("corruption", F2["corruption_event"]), ("drag-and-die", F2["drag_and_die_track"]), ("frozen target feed", F2["frozen_interval"]),
                   ("teleport", F2["teleport"])):
        print(f"   {lbl}: {v if not isinstance(v, dict) else {k: v[k] for k in v if k not in ('t0', 't1')}}")


if __name__ == "__main__":
    main()
