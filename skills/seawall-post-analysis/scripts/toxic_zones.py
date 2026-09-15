#!/usr/bin/env python
"""Toxic zones — WHERE do target tracks go bad on the range?

Offline analysis over a seawall_archiver dump (mavlink/*.csv truth +
tracks/track_*.csv), no mongo needed.  For each day:

  1. keep tracks whose median horizontal distance to interpolated TARGET
     truth (mav14550*) is < 150 m  ("target-side" tracks),
  2. classify every sample COASTING (|t - last_update_t| > 1.2 s) vs
     measurement-updated, note track_state (1=tentative, 2=confirmed),
  3. plot, at the TARGET TRUTH position at that time:
       a. coasting locations (grey-black dots, opacity ~ local density)
       b. track-death locations (red X, sized by how long the target then
          went untracked before another track picked it up)
       c. coverage grid: fraction of (moving) truth time the target had ANY
          non-coasting CONFIRMED track, 100 m cells, green -> red.
          Hover/ground truth samples (speed < MDV) are excluded from the
          coverage denominator — the 3.2 m/s MDV notch makes hover invisible
          by design, that is not a geolocation problem.

Satellite underlay via live_correlator._satmap_payload (Esri tiles); page
falls back to a plain background offline.

Run (sensorenv python):
  PYTHONPATH=/home/omar.syed/Test_Environment/track_correlation \
  /home/omar.syed/.local/share/mamba/envs/sensorenv/bin/python toxic_zones.py

Re-run after later flights by adding a day spec to DAYS below (or pass
--day LABEL:DATA_DIR on the CLI; target csv auto-detected = biggest
mav14550*.csv, interceptors = mav14551*.csv with >100 rows).
"""

import argparse
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------------------------------------------------------- constants
TARGET_RED = "#e34948"
INTERC_BLUE = "#2a78d6"
COAST_GAP_S = 1.2          # |t_epoch - last_update_t| beyond this = coasting
MEDIAN_GATE_M = 150.0      # track-to-truth median distance gate
MIN_ROWS = 20              # track csv row gate
MIN_VALID = 10             # min truth-matched samples for the median
TRUTH_GAP_S = 5.0          # never interpolate truth across a gap wider than this
TRUTH_NEAR_S = 3.0         # drop samples whose nearest truth time is farther
CELL_M = 100.0             # coverage grid cell
COVER_WIN_S = 0.75         # a confirmed non-coasting sample within +/- this covers t
MOVING_MPS = 2.5           # truth speed gate for the coverage denominator (MDV notch)
MIN_DWELL = 4              # min truth samples in a cell to score it (~4 s;
                           #  a 20 m/s pass leaves only ~5 s in a 100 m cell)
DEATH_MARGIN_S = 10.0      # deaths within this of truth end are ignored (landing)
RANGE_GATE_M = 20e3        # truth farther than this from the antenna = glitch
TELEPORT_MPS = 250.0       # implied truth speed above this = GPS teleport, drop

DAYS = [
    dict(label="2026-08-28 · run e65bd4f9 (Beige_Badger)", short="0828",
         dir="/home/omar.syed/Test_Environment/Seawall_Ironhide_Testing/Seawall_Week_of_8-24/seawall_0824_data/2026-08-28/e65bd4f9"),
    dict(label="2026-08-25 · run 707ccda9 (Crimson_Panda)", short="0825",
         dir="/home/omar.syed/Test_Environment/Seawall_Ironhide_Testing/Seawall_Week_of_8-24/seawall_0824_data/2026-08-25/707ccda9"),
]

OUTDIR = ("/home/omar.syed/Test_Environment/VP_TrackAnalysis/"
          "mru91_track2895/toxic_zones_0828")
PLOTLY_JS = os.path.join(HERE, "plotly.min.js")


# ---------------------------------------------------------------- truth
class TruthInterp:
    """Linear interp of target truth E/N/U, gap- and staleness-aware."""

    def __init__(self, csv_path):
        df = pd.read_csv(csv_path)
        df = df.sort_values("t_epoch").drop_duplicates("t_epoch")
        if "validposition" in df.columns:
            df = df[df["validposition"] != 0]
        # GPS-glitch scrub (8-27 lesson: teleports fabricate geometry):
        # range gate, then reject points implying > TELEPORT_MPS jumps.
        df = df[np.hypot(df["E_m"], df["N_m"]) < RANGE_GATE_M]
        t = df["t_epoch"].to_numpy(float)
        E = df["E_m"].to_numpy(float)
        N = df["N_m"].to_numpy(float)
        keep = np.ones(len(t), bool)
        last = 0
        for j in range(1, len(t)):
            dt = t[j] - t[last]
            if dt <= 0:
                keep[j] = False
                continue
            v = math.hypot(E[j] - E[last], N[j] - N[last]) / dt
            if v > TELEPORT_MPS and dt < 30:
                keep[j] = False
            else:
                last = j
        df = df[keep]
        self.t = df["t_epoch"].to_numpy(float)
        self.E = df["E_m"].to_numpy(float)
        self.N = df["N_m"].to_numpy(float)
        ucol = "U_m_hae" if "U_m_hae" in df.columns else "U_m"
        self.U = df[ucol].to_numpy(float)
        if "speed_mps" in df.columns:
            self.spd = df["speed_mps"].to_numpy(float)
        elif {"vel_n_mps", "vel_e_mps"} <= set(df.columns):
            # quickdump csvs carry velocity components but no speed column
            self.spd = np.hypot(df["vel_n_mps"].to_numpy(float),
                                df["vel_e_mps"].to_numpy(float))
        elif len(self.t) > 2:
            self.spd = np.hypot(np.gradient(self.E, self.t),
                                np.gradient(self.N, self.t))
        else:
            self.spd = np.zeros_like(self.t)
        self.t_end = float(self.t[-1])
        self.t_start = float(self.t[0])

    def query(self, tq):
        """-> (E, N, U, valid).  valid=False outside range, across >5 s gaps,
        or when the nearest truth sample is > 3 s away."""
        tq = np.asarray(tq, float)
        i = np.searchsorted(self.t, tq)
        i = np.clip(i, 1, len(self.t) - 1)
        tl, tr = self.t[i - 1], self.t[i]
        near = np.minimum(np.abs(tq - tl), np.abs(tr - tq))
        valid = ((tq >= self.t_start) & (tq <= self.t_end)
                 & ((tr - tl) <= TRUTH_GAP_S) & (near <= TRUTH_NEAR_S))
        E = np.interp(tq, self.t, self.E)
        N = np.interp(tq, self.t, self.N)
        U = np.interp(tq, self.t, self.U)
        return E, N, U, valid


def pick_truth_csvs(day_dir):
    """target = biggest mav14550*.csv; interceptors = mav14551* with >100 rows."""
    mdir = os.path.join(day_dir, "mavlink")
    tgt = max(glob.glob(os.path.join(mdir, "mav14550*.csv")), key=os.path.getsize)
    ints = [p for p in sorted(glob.glob(os.path.join(mdir, "mav14551*.csv")))
            if os.path.getsize(p) > 100 * 80]
    return tgt, ints


# ---------------------------------------------------------------- tracks
def load_target_tracks(day_dir, truth):
    """Return list of per-track dicts for tracks that follow the target."""
    kept = []
    files = sorted(glob.glob(os.path.join(day_dir, "tracks", "track_*.csv")))
    for fp in files:
        if os.path.getsize(fp) < 2200:          # < ~20 rows, cheap prefilter
            continue
        df = pd.read_csv(fp, usecols=["t_epoch", "E_m", "N_m", "U_m",
                                      "track_state", "last_update_t"])
        if len(df) < MIN_ROWS:
            continue
        t = df["t_epoch"].to_numpy(float)
        tE, tN, tU, ok = truth.query(t)
        if ok.sum() < MIN_VALID:
            continue
        d = np.hypot(df["E_m"].to_numpy(float) - tE,
                     df["N_m"].to_numpy(float) - tN)
        med = float(np.median(d[ok]))
        if med >= MEDIAN_GATE_M:
            continue
        coasting = (np.abs(t - df["last_update_t"].to_numpy(float))
                    > COAST_GAP_S)
        kept.append(dict(
            name=os.path.basename(fp)[:-4].replace("track_", "trk "),
            t=t, ok=ok, tE=tE, tN=tN, tU=tU,
            state=df["track_state"].to_numpy(int),
            coasting=coasting, med=med, err=d))
    return kept


# ---------------------------------------------------------------- products
def coasting_points(tracks):
    """(E, N, alpha, hover) of coasting samples at TRUTH position."""
    Es, Ns, hv = [], [], []
    for tr in tracks:
        m = tr["coasting"] & tr["ok"]
        Es.append(tr["tE"][m]); Ns.append(tr["tN"][m])
        hv += [f'{tr["name"]} · coast' for _ in range(int(m.sum()))]
    if not Es:
        return np.array([]), np.array([]), np.array([]), []
    E, N = np.concatenate(Es), np.concatenate(Ns)
    # opacity by local density, 50 m bins
    bx = np.floor(E / 50).astype(int)
    by = np.floor(N / 50).astype(int)
    _, inv, cnt = np.unique(np.stack([bx, by]), axis=1,
                            return_inverse=True, return_counts=True)
    c = cnt[inv].astype(float)
    alpha = 0.10 + 0.62 * np.sqrt(c / c.max())
    return E, N, alpha, hv


def track_deaths(tracks, truth):
    """Deaths >10 s before truth end; gap until any other track's next sample.
    The gap is capped at the target's last MOVING truth time — a death right
    after the target lands/hovers is expected (MDV), not a toxic zone."""
    all_t = [(k, tr["t"]) for k, tr in enumerate(tracks)]
    mv = truth.t[truth.spd >= MOVING_MPS]
    mv_end = float(mv[-1]) if len(mv) else truth.t_end
    deaths = []
    for k, tr in enumerate(tracks):
        td = float(tr["t"][-1])
        if truth.t_end - td <= DEATH_MARGIN_S:
            continue
        E, N, U, ok = truth.query([td])
        if not ok[0]:
            continue
        nxt = min((float(t[t > td][0]) for j, t in all_t
                   if j != k and (t > td).any()), default=truth.t_end)
        gap = max(0.0, min(nxt, mv_end) - td)
        spd = float(np.interp(td, truth.t, truth.spd))
        deaths.append(dict(E=float(E[0]), N=float(N[0]), U=float(U[0]),
                           t=td, gap=gap, name=tr["name"], spd=spd))
    return deaths


def coverage_grid(tracks, truth):
    """100 m cells over MOVING truth samples -> coverage fraction per cell."""
    conf_t = [tr["t"][(~tr["coasting"]) & (tr["state"] == 2)] for tr in tracks]
    conf_t = (np.sort(np.concatenate(conf_t)) if conf_t and any(len(x) for x in conf_t)
              else np.array([]))
    mv = truth.spd >= MOVING_MPS
    tt, tE, tN = truth.t[mv], truth.E[mv], truth.N[mv]
    if len(conf_t):
        i = np.clip(np.searchsorted(conf_t, tt), 1, len(conf_t) - 1)
        near = np.minimum(np.abs(tt - conf_t[i - 1]), np.abs(conf_t[i] - tt))
        covered = near <= COVER_WIN_S
    else:
        covered = np.zeros(len(tt), bool)
    ix = np.floor(tE / CELL_M).astype(int)
    iy = np.floor(tN / CELL_M).astype(int)
    cells = {}
    for j in range(len(tt)):
        key = (ix[j], iy[j])
        tot, cov, usum = cells.get(key, (0, 0, 0.0))
        cells[key] = (tot + 1, cov + int(covered[j]),
                      usum + float(truth.U[mv][j]))
    out = []
    for (cx, cy), (tot, cov, usum) in cells.items():
        if tot < MIN_DWELL:
            continue
        out.append(dict(E=(cx + .5) * CELL_M, N=(cy + .5) * CELL_M,
                        cov=cov / tot, dwell=tot, alt=usum / tot))
    overall = float(covered.mean()) if len(tt) else 0.0
    return out, overall, int(mv.sum())


def landmark(E, N, alt):
    r = math.hypot(E, N)
    if r < 300:
        where = "right at the antenna"
    else:
        wind = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S",
                "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        b = (math.degrees(math.atan2(E, N)) + 360) % 360
        where = f"{wind[int((b + 11.25) // 22.5) % 16]} leg, {r/1000:.1f} km out"
    band = ("low-alt" if alt < 60 else "mid-alt" if alt < 150 else "high-alt")
    return f"{band} {where}"


# ---------------------------------------------------------------- satmap
def satmap(meta, x0, x1, y0, y1):
    try:
        import live_correlator as LC
        lat, lon = meta["antenna_origin_lat_lon_haeM"][:2]
        LC.ANT_LL[0] = (lat, lon)
        pl = LC._satmap_payload(x0, x1, y0, y1)
        return pl if pl and "img" in pl else None
    except Exception as e:                                  # offline is fine
        print(f"  satmap unavailable ({e})", file=sys.stderr)
        return None


# ---------------------------------------------------------------- figures
AX = dict(color="#c9c9c2", gridcolor="rgba(255,255,255,0.10)",
          zerolinecolor="rgba(255,255,255,0.18)")


def base_layout(title, sat):
    lay = {
        "title": {"text": title, "font": {"size": 15, "color": "#e8e8e3"}},
        "paper_bgcolor": "#14171a", "plot_bgcolor": "#1c2126",
        "font": {"color": "#c9c9c2", "size": 12},
        "xaxis": dict(AX, title={"text": "East of antenna (m)"}),
        "yaxis": dict(AX, title={"text": "North of antenna (m)"},
                      scaleanchor="x", scaleratio=1),
        "legend": {"orientation": "h", "y": -0.12,
                   "bgcolor": "rgba(0,0,0,0)"},
        "margin": {"l": 70, "r": 20, "t": 48, "b": 60},
        "hovermode": "closest",
    }
    if sat:
        lay["images"] = [{
            "source": sat["img"], "xref": "x", "yref": "y",
            "x": sat["x0"], "y": sat["y1"],
            "sizex": sat["x1"] - sat["x0"], "sizey": sat["y1"] - sat["y0"],
            "xanchor": "left", "yanchor": "top",
            "sizing": "stretch", "layer": "below", "opacity": 1.0}]
    return lay


def _gap_line(t, E, N, decim, gap_s=30.0):
    """Decimated x/y lists with None inserted at time gaps (no chords)."""
    t, E, N = t[::decim], E[::decim], N[::decim]
    xs, ys = [], []
    for j in range(len(t)):
        if j and (t[j] - t[j - 1]) > gap_s:
            xs.append(None)
            ys.append(None)
        xs.append(round(float(E[j]), 1))
        ys.append(round(float(N[j]), 1))
    return xs, ys


def path_traces(truth, interceptors, decim):
    x, y = _gap_line(truth.t, truth.E, truth.N, decim)
    tr = [{
        "type": "scatter", "mode": "lines", "name": "target truth (mav14550)",
        "x": x, "y": y,
        "line": {"color": TARGET_RED, "width": 1.3},
        "opacity": 0.55, "hoverinfo": "skip", "connectgaps": False}]
    for nm, itr in interceptors:
        ix, iy = _gap_line(itr.t, itr.E, itr.N, decim)
        tr.append({
            "type": "scatter", "mode": "lines", "name": f"interceptor ({nm})",
            "x": ix, "y": iy,
            "line": {"color": INTERC_BLUE, "width": 1.0},
            "opacity": 0.45, "hoverinfo": "skip", "connectgaps": False})
    tr.append({"type": "scatter", "mode": "markers", "name": "antenna",
               "x": [0], "y": [0],
               "marker": {"symbol": "diamond", "size": 11, "color": "#ffffff",
                          "line": {"color": "#000000", "width": 1}},
               "hovertext": ["MRU91 antenna"], "hoverinfo": "text"})
    return tr


def coverage_fig(cells, sat, truth, interceptors, title, decim):
    # complete regular grids — sparse centers would make plotly stretch
    # cells to the midpoint of their nearest neighbour (giant blocks)
    ex = [c["E"] for c in cells] or [0.0]
    ny = [c["N"] for c in cells] or [0.0]
    xs = (np.arange(round(min(ex) / CELL_M - .5), round(max(ex) / CELL_M + .5))
          * CELL_M + CELL_M / 2).tolist()
    ys = (np.arange(round(min(ny) / CELL_M - .5), round(max(ny) / CELL_M + .5))
          * CELL_M + CELL_M / 2).tolist()
    xi = {round(v, 1): i for i, v in enumerate(xs)}
    yi = {round(v, 1): i for i, v in enumerate(ys)}
    z = [[None] * len(xs) for _ in ys]
    txt = [[""] * len(xs) for _ in ys]
    for c in cells:
        z[yi[round(c["N"], 1)]][xi[round(c["E"], 1)]] = round(c["cov"], 3)
        txt[yi[round(c["N"], 1)]][xi[round(c["E"], 1)]] = (
            f'coverage {c["cov"]*100:.0f}% · dwell {c["dwell"]} s · '
            f'alt {c["alt"]:.0f} m · {landmark(c["E"], c["N"], c["alt"])}')
    heat = {
        "type": "heatmap", "x": xs, "y": ys, "z": z, "text": txt,
        "name": "coverage",
        "hovertemplate": "E %{x:.0f} m, N %{y:.0f} m<br>%{text}<extra></extra>",
        "hoverongaps": False, "zmin": 0, "zmax": 1, "opacity": 0.88,
        "colorscale": [[0.0, "#b3261e"], [0.25, "#d95926"],
                       [0.5, "#e6a700"], [0.75, "#7a9a2e"], [1.0, "#1a7f37"]],
        "colorbar": {"title": {"text": "coverage", "font": {"color": "#c9c9c2"}},
                     "tickformat": ".0%", "tickfont": {"color": "#c9c9c2"},
                     "outlinewidth": 0, "thickness": 14, "len": 0.7},
    }
    data = [heat] + path_traces(truth, interceptors, decim)
    return {"data": data, "layout": base_layout(title, sat)}


def overlay_fig(coast, deaths, sat, truth, interceptors, title, decim):
    E, N, alpha, hv = coast
    data = path_traces(truth, interceptors, decim)
    if len(E):
        data.append({
            "type": "scatter", "mode": "markers",
            "name": "coasting sample (at truth pos)",
            "x": np.round(E, 1).tolist(), "y": np.round(N, 1).tolist(),
            "marker": {"size": 5,
                       "color": [f"rgba(12,12,12,{a:.2f})" for a in alpha],
                       "line": {"color": "rgba(255,255,255,0.35)", "width": 0.5}},
            "hovertext": hv, "hoverinfo": "text"})
    if deaths:
        gmax = max(d["gap"] for d in deaths) or 1.0
        data.append({
            "type": "scatter", "mode": "markers",
            "name": "track death (X size = untracked gap)",
            "x": [round(d["E"], 1) for d in deaths],
            "y": [round(d["N"], 1) for d in deaths],
            "marker": {"symbol": "x",
                       "size": [7 + 21 * math.sqrt(d["gap"] / gmax)
                                for d in deaths],
                       "color": TARGET_RED,
                       "line": {"color": "#ffffff", "width": 1}},
            "hovertext": [f'{d["name"]} died · untracked {d["gap"]:.1f} s '
                          f'(while target moving) · truth spd {d["spd"]:.1f} '
                          f'm/s · alt {d["U"]:.0f} m' for d in deaths],
            "hoverinfo": "text"})
    return {"data": data, "layout": base_layout(title, sat)}


# ---------------------------------------------------------------- per day
def analyze_day(spec):
    day_dir = spec["dir"]
    meta = json.load(open(os.path.join(day_dir, "meta.json")))
    tgt_csv, int_csvs = pick_truth_csvs(day_dir)
    truth = TruthInterp(tgt_csv)
    interceptors = [(os.path.basename(p)[:-4], TruthInterp(p))
                    for p in int_csvs]
    print(f"[{spec['short']}] truth={os.path.basename(tgt_csv)} "
          f"({len(truth.t)} pts), {len(int_csvs)} interceptor csv(s)")

    tracks = load_target_tracks(day_dir, truth)
    print(f"[{spec['short']}] target-side tracks kept: {len(tracks)}")
    coast = coasting_points(tracks)
    deaths = track_deaths(tracks, truth)
    cells, overall, n_mv = coverage_grid(tracks, truth)

    xs = [truth.E, [0.0]] + [i[1].E for i in interceptors]
    ys = [truth.N, [0.0]] + [i[1].N for i in interceptors]
    x_all, y_all = np.concatenate(xs), np.concatenate(ys)
    sat = satmap(meta, float(x_all.min()) - 200, float(x_all.max()) + 200,
                 float(y_all.min()) - 200, float(y_all.max()) + 200)
    decim = max(1, len(truth.t) // 8000)

    worst = sorted((c for c in cells if c["dwell"] >= 5),
                   key=lambda c: (c["cov"], -c["dwell"]))[:3]
    n_coast = int(sum(((tr["coasting"] & tr["ok"]).sum()) for tr in tracks))
    n_samp = int(sum(tr["ok"].sum() for tr in tracks))
    stats = dict(label=spec["label"], short=spec["short"],
                 n_tracks=len(tracks), overall=overall, n_moving=n_mv,
                 n_deaths=len(deaths),
                 max_gap=max((d["gap"] for d in deaths), default=0.0),
                 coast_frac=(n_coast / n_samp if n_samp else 0.0),
                 worst=worst, sat_ok=sat is not None)
    figs = dict(
        cov=coverage_fig(cells, sat, truth, interceptors,
                         f"Coverage grid — {spec['label']}", decim),
        ovl=overlay_fig(coast, deaths, sat, truth, interceptors,
                        f"Coasting + track deaths — {spec['label']}", decim))
    return stats, figs


# ---------------------------------------------------------------- page
def build_html(days_out, plotly_src):
    css = """
    body{background:#101214;color:#e8e8e3;font:15px/1.55 system-ui,-apple-system,
      Segoe UI,sans-serif;margin:0;padding:0 0 60px}
    .wrap{max-width:1180px;margin:0 auto;padding:0 22px}
    h1{font-size:26px;margin:28px 0 4px} h2{font-size:19px;margin:34px 0 10px;
      color:#f0f0ea} .sub{color:#9a9a90;font-size:13.5px;margin-bottom:10px}
    .fig{background:#14171a;border:1px solid #262b30;border-radius:10px;
      padding:8px;margin:14px 0}
    .plot{width:100%;height:640px}
    .findings{background:#181c20;border:1px solid #2a3036;border-radius:10px;
      padding:14px 20px;margin:14px 0}
    .findings li{margin:7px 0}
    .kpis{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
    .kpi{background:#181c20;border:1px solid #2a3036;border-radius:8px;
      padding:10px 16px;min-width:130px}
    .kpi b{display:block;font-size:22px;color:#fff}
    .kpi span{font-size:12px;color:#9a9a90}
    .bad{color:#ff8a80}.good{color:#8fd694}
    code{background:#22262b;padding:1px 5px;border-radius:4px;font-size:13px}
    .note{color:#9a9a90;font-size:13px}
    """
    parts = ['<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             "<title>Toxic zones — where target tracks go bad</title>",
             f"<style>{css}</style>",
             f"<script>{plotly_src}</script>",
             '<div class="wrap">',
             "<h1>Toxic zones — where target tracks go bad</h1>",
             '<div class="sub">MRU91 · target = mav14550 drone (red '
             f'<span style="color:{TARGET_RED}">&#9632;</span>), interceptor blue '
             f'<span style="color:{INTERC_BLUE}">&#9632;</span> · everything below is '
             "plotted at the <b>target truth position</b>, so red = places on the "
             "range where the radar loses the target. Coverage denominator uses "
             f"moving truth only (speed &ge; {MOVING_MPS} m/s — the 3.2 m/s MDV "
             "notch makes hover invisible by design).</div>"]
    div_i = 0
    js = []
    for stats, figs in days_out:
        parts.append(f"<h2>{stats['label']}</h2>")
        parts.append(
            '<div class="kpis">'
            f'<div class="kpi"><b>{stats["n_tracks"]}</b><span>target-side tracks '
            f'(median &lt; {MEDIAN_GATE_M:.0f} m)</span></div>'
            f'<div class="kpi"><b>{stats["overall"]*100:.0f}%</b><span>moving-time '
            f'confirmed coverage</span></div>'
            f'<div class="kpi"><b>{stats["coast_frac"]*100:.0f}%</b><span>of track '
            f'samples coasting (&gt;{COAST_GAP_S} s stale)</span></div>'
            f'<div class="kpi"><b>{stats["n_deaths"]}</b><span>track deaths '
            f'(&gt;{DEATH_MARGIN_S:.0f} s before truth end)</span></div>'
            f'<div class="kpi"><b>{stats["max_gap"]:.0f} s</b><span>longest '
            f'untracked gap after a death</span></div></div>')
        for key, headline in (("cov", "Coverage grid (the money view — red "
                                      "cells = toxic geolocations)"),
                              ("ovl", "Coasting density + track deaths")):
            div_i += 1
            parts.append(f'<div class="fig"><div class="sub">{headline}</div>'
                         f'<div id="fig{div_i}" class="plot"></div></div>')
            js.append(f'Plotly.newPlot("fig{div_i}",'
                      f'{json.dumps(figs[key]["data"])},'
                      f'{json.dumps(figs[key]["layout"])},'
                      f'{{responsive:true,displaylogo:false}});')
        if stats["n_tracks"] == 0:
            parts.append(
                '<div class="findings"><b class="bad">No target-side tracks at '
                'all this day</b> — no track ever held a median &lt; '
                f'{MEDIAN_GATE_M:.0f} m to the mav14550 target (nearest big '
                'track stayed &gt;1 km away). Per the 8-25 two-drone '
                'correlation study, every track that day followed the '
                'interceptor-role drone (mav14551_2_2, ~90–130 m error, '
                'measurement-starved). The all-red strip below is the whole '
                'finding: the 14550 target went untracked for the entire day, '
                'everywhere it flew — day-level, not geolocation-specific.</div>')
        elif stats["worst"]:
            items = "".join(
                f'<li><b class="bad">{c["cov"]*100:.0f}% coverage</b> at '
                f'E&nbsp;{c["E"]:+.0f}&nbsp;m, N&nbsp;{c["N"]:+.0f}&nbsp;m '
                f'({c["dwell"]} s dwell, mean alt {c["alt"]:.0f} m) — '
                f'<i>{landmark(c["E"], c["N"], c["alt"])}</i></li>'
                for c in stats["worst"])
            parts.append('<div class="findings"><b>Top 3 toxic cells '
                         f'({stats["short"]})</b><ol>{items}</ol></div>')
        if not stats["sat_ok"]:
            parts.append('<div class="note">Satellite tiles unavailable when '
                         'this page was built — plain background.</div>')
    parts.append(
        '<div class="note">Method: tracks (&ge;20 rows) matched to interpolated '
        'target truth (no interp across &gt;5 s truth gaps; samples &gt;3 s from '
        f'truth dropped); target-side if median horiz err &lt; {MEDIAN_GATE_M:.0f} m. '
        f'COASTING = |t &minus; last_update_t| &gt; {COAST_GAP_S} s. Coverage: a '
        f'truth second counts covered if any confirmed (state 2) non-coasting '
        f'sample lies within &plusmn;{COVER_WIN_S} s; {CELL_M:.0f} m cells, '
        f'&ge;{MIN_DWELL} s dwell scored. Built by '
        '<code>track_correlation/toxic_zones.py</code>.</div>')
    parts.append("</div><script>" + "\n".join(js) + "</script>")
    return "\n".join(parts)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", action="append", default=None, metavar="LABEL:DIR",
                    help="override day list (repeatable)")
    ap.add_argument("--out", default=os.path.join(OUTDIR, "report.html"))
    args = ap.parse_args()
    days = DAYS
    if args.day:
        days = [dict(label=s.split(":", 1)[0], short=s.split(":", 1)[0],
                     dir=s.split(":", 1)[1]) for s in args.day]

    days_out = []
    for spec in days:
        try:
            days_out.append(analyze_day(spec))
        except Exception as e:
            print(f"[{spec['short']}] FAILED: {e}", file=sys.stderr)
    if not days_out:
        sys.exit("no day produced output")

    if os.path.exists(PLOTLY_JS):
        plotly_src = open(PLOTLY_JS, encoding="utf-8").read()
    else:                                   # skill copy ships no bundle: take it from the installed plotly
        import plotly.offline as _pyo
        plotly_src = _pyo.get_plotlyjs()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    html = build_html(days_out, plotly_src)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB)")

    # findings summary
    for stats, _ in days_out:
        w = stats["worst"][0] if stats["worst"] else None
        wtxt = (f'worst cell {w["cov"]*100:.0f}% @ E{w["E"]:+.0f}/N{w["N"]:+.0f} '
                f'({landmark(w["E"], w["N"], w["alt"])})' if w else "no scored cells")
        print(f'[{stats["short"]}] {stats["n_tracks"]} target tracks · coverage '
              f'{stats["overall"]*100:.0f}% · coasting {stats["coast_frac"]*100:.0f}% '
              f'· {stats["n_deaths"]} deaths (max gap {stats["max_gap"]:.0f}s) · {wtxt}')


if __name__ == "__main__":
    main()
