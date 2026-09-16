"""Modular 8/26-style tracking-mission report builder.

Mechanical refactor of seawall_0824_data/raw_npz/mru91_0827_cache/mission_report.py:
every figure function is byte-identical to the original; only the module-level
executable statements (data loads, prep, tab/page assembly, file writes) moved into
functions. Mission data lives in module globals populated by init_mission() --
init_mission MUST be called before any other API function.

Public API:
    init_mission(mdir, day=(2026, 8, 26), ant=DEFAULT_ANT, geoid_n=DEFAULT_GEOID_N)
        load truthv.npz / fast2_F*.npz / analysis_stage1.json from mdir, do the
        truth units/frame conversion, windows, matching, bias -- everything the
        original did at import time.
    build_flight_tab(flight_key) -> str
        the exact per-run tab pane HTML; flight_key = "F1R1", "F1 Run 1" or (1, 1).
    build_summary_tab(notes_html="") -> str
        the summary tab pane HTML; notes_html replaces the key-findings +
        obs-addendum narrative block ("" -> the original 8/26 narrative).
    wrap_page(title, sub, tabs) -> str
        the exact page template (CSS + tab JS); tabs = {label: pane_html},
        first tab shown; title -> <title>, sub -> <h1>.

CLI (reproduces the original report):
    python tracking_tab.py --mdir <dir> --out <report.html> [--title ...] [--sub ...]
                           [--day YYYY-MM-DD] [--ant lat,lon,hae] [--geoid-n N]
    plotly.min.js is written next to --out (the page references it relatively).
"""
import json
import math
import os
import sys
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
import plotly.graph_objects as go
import plotly.offline as pyo
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corr_lib as C

# ---------- configuration (set by init_mission; defaults = 8/26 MRU91 mission) ----------
MDIR = None                     # mission data directory
DAY = (2026, 8, 26)             # date that hhmm() clock strings resolve on
DEFAULT_ANT = (33.7480633369788, -115.33920245302231, 139.882350001158)
DEFAULT_GEOID_N = -31.4         # WGS-84 HAE - MSL at the site (antenna HAE vs DEM; NGS GEOID12B -32.8)
ANT_LAT, ANT_LON, ANT_HAE = DEFAULT_ANT
GEOID_N = DEFAULT_GEOID_N
DEFAULT_TITLE = "MRU91 Mission Report 2026-08-26"
DEFAULT_SUB = "MRU91 two-drone-day mission report \u2014 2026-08-26 (drone mav14550_1_1)"

# campaign timezone (DST-aware). init_mission(tz=...) / set_tz() switch it; PDT is
# kept as a compatibility alias. TZ_ABBR is the zone abbreviation printed in every
# "time (PDT)" label — refreshed by init_mission from the first flight window.
TZ_NAME = "America/Los_Angeles"
TZ = ZoneInfo(TZ_NAME)
PDT = TZ
TZ_ABBR = "PDT"


def set_tz(name):
    global TZ_NAME, TZ, PDT
    TZ_NAME = str(name or "America/Los_Angeles")
    TZ = ZoneInfo(TZ_NAME)
    PDT = TZ
    return TZ


def tz_abbr(t=None):
    dt = datetime.fromtimestamp(float(t), TZ) if t is not None else datetime.now(TZ)
    return dt.strftime("%Z") or TZ_NAME


INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET, GREEN, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7", "#008300", "#e34948")
PALETTE = [AQUA, VIOLET, MAGENTA, YELLOW, GREEN, RED, ORANGE, "#7a6a52", "#2a78d6"]

def hhmm(s):
    h, m, *sec = s.split(":")
    return datetime(DAY[0], DAY[1], DAY[2], int(h), int(m), int(sec[0]) if sec else 0,
                    tzinfo=PDT).timestamp()

def pdt(arr):
    return [None if x is None else datetime.fromtimestamp(x, tz=PDT)
            for x in np.atleast_1d(arr)]

def ps(ts):
    return datetime.fromtimestamp(ts, tz=PDT).strftime("%H:%M:%S")

# ---------- mission data (module globals populated by init_mission) ----------
T = None                # truth: t,E,N,U,spd,vN,vE,vUp (units/frame-corrected, AGL-clamped)
tracksV = {}            # 13-col v2 tracks
tracks = {}             # 10-col view for the matching pipeline
OBS = {}                # raw observations per flight
S1 = None               # analysis_stage1.json
FLIGHTS = {}
RUNS = {}
LANDING = {}
INBOUND = {}
FLIGHT_TRACKS = {}
PLOT_TRACKS = {}
M = {}                  # matched samples per track
BIAS = 0.0
DU = 0.0
DU_residual = 0.0
PAD_U = 0.0
AGL_MIN = 20.0
allstats = {}           # per-run stats, filled by build_flight_tab / _ensure_stats

def init_mission(mdir, day=(2026, 8, 26), ant=DEFAULT_ANT, geoid_n=DEFAULT_GEOID_N, spec=None,
                 tz=None):
    """Load + prep everything the original mission_report.py did at import time.
    Must be called before any other API function (populates the module globals).
    tz: IANA zone name for every clock label (default: the current module TZ,
    America/Los_Angeles unless set_tz() was called)."""
    global MDIR, DAY, ANT_LAT, ANT_LON, ANT_HAE, GEOID_N, GENERIC, TZ_ABBR
    GENERIC = spec is not None
    global T, tracksV, tracks, OBS, S1
    global FLIGHTS, RUNS, LANDING, INBOUND, FLIGHT_TRACKS, PLOT_TRACKS
    global M, BIAS, DU, DU_residual, PAD_U, AGL_MIN, allstats
    if tz:
        set_tz(tz)
    MDIR = str(mdir)
    DAY = tuple(day)
    ANT_LAT, ANT_LON, ANT_HAE = ant
    GEOID_N = geoid_n
    allstats = {}
    # ---------- load ----------
    # truth WITH MAVLink-reported velocities: t,E,N,U,spd,vN,vE,vUp (8 cols);
    # prep_from_dump (>= 2026-09-15) appends lat,lon (deg) as cols 8,9
    T = np.load(f"{MDIR}/truthv.npz")["truth"]
    T = T[np.argsort(T[:, 0])]

    # Truth is built by a rigorous WGS-84 conversion into the antenna's ENU frame (the
    # same frame the radar track state lives in). The AIR_TRAFFIC feed reports lat/lon
    # (deg), `altitude` in FEET MSL, and `vertical_speed` in FEET/MIN (per chaotic
    # nexus_air_traffic_utils.py: FEET_TO_METERS, altitude_ft, feet_per_minute_to_meter_
    # per_sec). U = altitude_ft - antenna_HAE in both layouts.
    _alt_hae = (T[:, 3] + ANT_HAE) * 0.3048 + GEOID_N          # ft MSL -> m MSL -> m HAE
    if T.shape[1] >= 10:
        # prep_from_dump layout: geodetic lat/lon carried through -> EXACT WGS-84 ENU
        # (corr_lib.EnuFrame). The legacy inverse below would treat the dump's exact
        # E/N as equirectangular and compress N by ~0.36 % (14 m at 4 km).
        _e, _n, _u = C.EnuFrame((ANT_LAT, ANT_LON, ANT_HAE)).enu(T[:, 8], T[:, 9], _alt_hae)
        T = T[:, :8].copy()
    else:
        # legacy 8/26 extraction: truthv carries equirectangular E/N — recover geodetic
        # (exact inverse of that projection), then rebuild E/N/U with pymap3d.geodetic2enu
        import pymap3d as _pm
        _k = math.cos(math.radians(ANT_LAT))
        _lat = ANT_LAT + T[:, 2] / 111320.0
        _lon = ANT_LON + T[:, 1] / (111320.0 * _k)
        _e, _n, _u = _pm.geodetic2enu(_lat, _lon, _alt_hae, ANT_LAT, ANT_LON, ANT_HAE)
    T[:, 1], T[:, 2], T[:, 3] = _e, _n, _u
    T[:, 7] = T[:, 7] * 0.3048 / 60.0                           # vertical_speed ft/min -> m/s
    # v2 extraction: 13-col tracks (t,E,N,U,vE,vN,vU,sE,sN,sU,assoc,state,lu) + raw obs,
    # one fast2_F<n>.npz per flight (n = 1..N; the legacy 8/26 mission has exactly two)
    import glob as _glob
    _fz = sorted(_glob.glob(f"{MDIR}/fast2_F*.npz"),
                 key=lambda p: int(re.search(r"fast2_F(\d+)\.npz$", p).group(1)))
    if not _fz:
        raise FileNotFoundError(f"{MDIR}/fast2_F<n>.npz — no flight bundles")
    _V = {int(re.search(r"fast2_F(\d+)\.npz$", p).group(1)): np.load(p) for p in _fz}
    tracksV = {}
    for Z in _V.values():
        for k in Z.files:
            if k.startswith("trk_"):
                tid = int(k[4:])
                A = Z[k]
                tracksV[tid] = np.vstack([tracksV[tid], A]) if tid in tracksV else A
    tracksV = {k: v[np.argsort(v[:, 0])] for k, v in tracksV.items()}
    # 10-col view for the existing matching pipeline (lu at col 7)
    tracks = {k: v[:, [0, 1, 2, 3, 7, 8, 9, 12, 10, 11]] for k, v in tracksV.items()}
    OBS = {fi: Z["obs"] for fi, Z in _V.items()}     # t, az_rad, el_rad, rng_m, dop, snr
    S1 = json.load(open(f"{MDIR}/analysis_stage1.json"))

    if spec is None:
        # legacy 8/26 mission structure (verbatim)
        FLIGHTS = {1: (hhmm("08:35:24"), hhmm("08:52:21")), 2: (hhmm("08:58:56"), hhmm("09:18:44"))}
        RUNS = {(1, 1): (hhmm("08:35:24"), hhmm("08:42:44")),
                (1, 2): (hhmm("08:42:44"), hhmm("08:49:44")),
                (2, 1): (hhmm("08:58:56"), hhmm("09:05:57")),
                (2, 2): (hhmm("09:05:57"), hhmm("09:13:31"))}
        LANDING = {1: (hhmm("08:49:44"), hhmm("08:52:21")), 2: (hhmm("09:13:31"), hhmm("09:18:44"))}
        # inbound leg (range apex -> run end) of every run, for the seeker-basket plots
        INBOUND = {(1, 1): (hhmm("08:38:54"), hhmm("08:42:44")),
                   (1, 2): (hhmm("08:46:01"), hhmm("08:49:44")),
                   (2, 1): (hhmm("09:01:46"), hhmm("09:05:57")),
                   (2, 2): (hhmm("09:09:20"), hhmm("09:13:31"))}
        FLIGHT_TRACKS = {1: [58, 94, 305, 367, 391, 501, 631, 687, 689, 733],
                         2: [858, 917, 972, 1168, 1226, 1566, 1623, 1700, 1792, 1867]}
        # leg-carrying tracks only, for all plots & metrics; climb-out / pad-turn /
        # landing fragments (58, 858, 1168, 1566, ...) stay in timelines & drop table.
        # track 305 dropped from plots per test-lead direction (kept in timelines)
        PLOT_TRACKS = {1: [94, 367, 391, 501], 2: [917, 1226]}
    else:
        # GENERIC mission structure from stage1 + optional spec overrides
        FLIGHTS = {i + 1: (float(a), float(b)) for i, (a, b) in enumerate(S1["flights"])}
        RUNS = ({tuple(map(int, k.split(","))): tuple(v) for k, v in spec["runs"].items()}
                if isinstance(spec, dict) and spec.get("runs") else
                {(i, 1): w for i, w in FLIGHTS.items()})
        LANDING = {i: (w[1], w[1]) for i, w in FLIGHTS.items()}
        INBOUND = {k: ((v[0] + v[1]) / 2.0, v[1]) for k, v in RUNS.items()}
        gate = spec.get("gate_m", 350.0) if isinstance(spec, dict) else 350.0
        FLIGHT_TRACKS, PLOT_TRACKS = {}, {}
        for i, (fa, fb) in FLIGHTS.items():
            ids = [int(t) for t, st in S1["tracks"].items()
                   if st["t1"] >= fa and st["t0"] <= fb and st["med"] < gate]
            ids.sort(key=lambda t: -S1["tracks"][str(t)]["n"])
            FLIGHT_TRACKS[i] = ids[:10]
            good = [t for t in ids if S1["tracks"][str(t)]["med"] < 200
                    and S1["tracks"][str(t)]["n"] >= 20]
            PLOT_TRACKS[i] = (good or ids)[:6]

    # ---------- global matching / bias / datum ----------
    M = {}
    for tid in [t for ids in FLIGHT_TRACKS.values() for t in ids]:
        if tid in tracks:
            m = C.match_track(tracks[tid], T)
            if len(m) >= 4:
                M[tid] = m
    if not M:
        print("init_mission: NO matched tracks this mission — bias/datum set to 0")
    BIAS = C.az_bias_deg(list(M.values())) if M else 0.0
    # With the altitude UNITS FIX above, truth and track are in the same frame (WGS-84 HAE,
    # metres). There is NO truth-datum defect to remove -- the old ~-583 m "datum" was purely
    # the feet->metre factor. Show RAW vertical error: DU = 0. (residual median(track-truth)
    # is the real ~tens-of-m radar vertical error, left in.)
    DU_residual = (float(np.median(np.concatenate([m[:, 13] for m in M.values()])))
                   if M else 0.0)
    DU = 0.0
    print(f"global az bias {BIAS:+.2f} deg; RAW vertical residual (track-truth) "
          f"{DU_residual:+.0f} m (NOT removed; DU=0)")

    TZ_ABBR = tz_abbr(min(w[0] for w in FLIGHTS.values())) if FLIGHTS else tz_abbr()

    # user-requested clamp: truth only while airborne above 20 m AGL.
    # AGL reference = parked truth altitude at the pad (pre-takeoff samples).
    parked = T[(T[:, 4] < 1.0) & (T[:, 0] < min(w[0] for w in FLIGHTS.values()))]
    PAD_U = float(np.median(parked[:, 3])) if len(parked) else float(np.percentile(T[:, 3], 3))
    AGL_MIN = 20.0
    n_before = len(T)
    T = T[(T[:, 3] - PAD_U) > AGL_MIN]
    print(f"pad truth-U {PAD_U:.0f} m; altitude clamp >{AGL_MIN:.0f} m AGL: "
          f"truth {n_before} -> {len(T)} pts")
    M = {}
    for tid in [t for ids in FLIGHT_TRACKS.values() for t in ids]:
        if tid in tracks:
            m = C.match_track(tracks[tid], T)
            if len(m) >= 4:
                M[tid] = m

def truth_vel_at(ts):
    """MAVLink-reported velocity (vE, vN, vUp) interpolated at times ts."""
    idx = np.clip(np.searchsorted(T[:, 0], ts), 1, len(T) - 1)
    a, b = T[idx - 1], T[idx]
    f = np.clip((ts - a[:, 0]) / np.maximum(b[:, 0] - a[:, 0], 1e-6), 0, 1)
    ok = np.minimum(np.abs(a[:, 0] - ts), np.abs(b[:, 0] - ts)) < 3.0
    vN = a[:, 5] + f * (b[:, 5] - a[:, 5])
    vE = a[:, 6] + f * (b[:, 6] - a[:, 6])
    vU = (a[:, 7] + f * (b[:, 7] - a[:, 7]))   # already m/s (ft/min converted at load)
    return vE, vN, vU, ok

def to_agl(buggy_U):
    """Convert an analysis_stage1 / extraction altitude (feet-as-metres, antenna-
    relative) to true metres AGL above the pad, matching the WGS-84 truth used above."""
    correct_U = (buggy_U + ANT_HAE) * 0.3048 + GEOID_N - ANT_HAE
    return correct_U - PAD_U

def corrected(m):
    """RAW radar errors — no bias removal, no datum removal (DU=0). Truth altitude is
    now unit-corrected (feet->m, MSL->HAE) into the track's frame, so dU is the real
    radar vertical error."""
    return m[:, 11], m[:, 12], m[:, 13] - DU

def ang_raw(m):
    """RAW az/el errors + filter 1-sigma, with NO datum removal (unlike
    corr_lib.angle_errors, which subtracts its own median dU). Truth altitude is
    already the rigorous WGS-84 value, so el error here is the real radar el error.
    match_track cols: 1=trkE 2=trkN 3=trkU 4=sE 5=sN 6=sU 8=tE 9=tN 10=tU 13=dU."""
    Eg, Ng, Ug = m[:, 1], m[:, 2], m[:, 3]
    r_g = np.hypot(Eg, Ng)
    theta = np.arctan2(Eg, Ng)
    az_err = ((np.degrees(theta - np.arctan2(m[:, 8], m[:, 9])) + 180) % 360) - 180
    sig_az = np.degrees(np.sqrt((np.cos(theta) * m[:, 4]) ** 2 +
                                (np.sin(theta) * m[:, 5]) ** 2) / np.maximum(r_g, 1))
    el_err = np.degrees(np.arctan2(Ug, r_g) -
                        np.arctan2(m[:, 10], np.hypot(m[:, 8], m[:, 9])))
    sig_el = np.degrees(m[:, 6] / np.maximum(np.hypot(r_g, Ug), 1))
    # range (slant) + altitude error in meters
    trk_slant = np.hypot(r_g, Ug)
    tru_gr = np.hypot(m[:, 8], m[:, 9])
    tru_slant = np.hypot(tru_gr, m[:, 10])
    rng_err = trk_slant - tru_slant
    alt_err = Ug - m[:, 10]
    sig_rng = np.sqrt((Eg * m[:, 4]) ** 2 + (Ng * m[:, 5]) ** 2 +
                      (Ug * m[:, 6]) ** 2) / np.maximum(trk_slant, 1)
    sig_alt = m[:, 6]
    return {"t": m[:, 0], "az_err": az_err, "el_err": el_err,
            "sig_az": sig_az, "sig_el": sig_el,
            "rng_err": rng_err, "alt_err": alt_err,
            "sig_rng": sig_rng, "sig_alt": sig_alt}


LAYOUT = dict(template="plotly_white",
              font=dict(family="Segoe UI, Roboto, Helvetica, Arial, sans-serif",
                        color=INK, size=14),
              margin=dict(l=64, r=20, t=84, b=52),
              legend=dict(orientation="h", y=1.02, yanchor="bottom",
                          bgcolor="rgba(0,0,0,0)", font=dict(size=13)))

def rgba(h, a):
    return f"rgba({int(h[1:3],16)},{int(h[3:5],16)},{int(h[5:7],16)},{a})"

def win(A, w):
    return A[(A[:, 0] >= w[0]) & (A[:, 0] <= w[1])]

def prep(tid, w, max_horiz=350.0):
    """Windowed matched samples, truncated at the track's FINAL measurement
    (drops the death-coast runaway) and masked of coast excursions beyond
    max_horiz m from truth. Returns None if too little remains."""
    m0 = M.get(tid)
    if m0 is None:
        return None
    m = win(m0, w)
    if len(m) < 6:
        return None
    fr = np.where(m[:, 14] > 0)[0]
    if not len(fr):
        return None
    m = m[:fr[-1] + 1]
    dEc, dNc, dUc = corrected(m)
    keep = np.hypot(dEc, dNc) < max_horiz
    m = m[keep]
    return m if len(m) >= 6 else None

def seg_split(A, gap=6):
    s = 0
    for i in range(1, len(A)):
        if A[i, 0] - A[i-1, 0] > gap:
            yield A[s:i]; s = i
    yield A[s:]

STRONG = ["#0f766e", "#7c3aed", "#be123c", "#15803d", "#b45309"]  # deep, saturated

def color_of(fl):
    c = {tid: STRONG[i % len(STRONG)] for i, tid in enumerate(PLOT_TRACKS[fl])}
    for i, tid in enumerate(FLIGHT_TRACKS[fl]):     # fallback for non-plot tracks
        c.setdefault(tid, PALETTE[i % len(PALETTE)])
    return c

def obs_errors(fl, w):
    """RAW observation az/el errors vs truth for obs geometrically on the drone."""
    O = OBS[fl]
    O = O[(O[:, 0] >= w[0]) & (O[:, 0] <= w[1])]
    rows = []
    for t, az, el, rng, dop, snr in O:
        tr = C.interp_truth(T, t)
        if tr is None:
            continue
        taz = math.atan2(tr[0], tr[1])
        tgr = math.hypot(tr[0], tr[1])
        daz = (az - taz + math.pi) % (2 * math.pi) - math.pi
        tel = math.atan2(tr[2] + DU, tgr)
        el_err = math.degrees(el - tel)
        # Associate the obs to the DRONE in 3-D: az + range (as before) AND elevation.
        # match_obs never gated elevation, so multipath/sidelobe returns at the same
        # az+range but a different elevation (~+8° here, same SNR as the target) leaked
        # in and smeared the obs el plots. A ±3° el window (>4x the ~0.7° real σ) keeps
        # the target returns without biasing the reported accuracy — same footing as the
        # filtered track (which is inherently the drone).
        if abs(daz) > 0.06 or abs(rng - tgr) > 300 or abs(el_err) > 3.0:
            continue
        tslant = math.hypot(tgr, tr[2])          # truth slant range (ground, U)
        rng_err_m = rng - tslant                 # obs range - truth slant
        alt_err_m = rng * math.sin(el) - tr[2]   # obs-implied altitude - truth U
        rows.append((t, math.degrees(daz), el_err, rng_err_m, alt_err_m))
    return np.array(rows) if rows else np.zeros((0, 5))

# ---------- figures per run ----------
def en_map(fl, w, colors, rotate_deg=0.0, show_legend=True):
    fig = go.Figure()
    Tw = win(T, w)
    first = True
    for ch in seg_split(Tw):
        if len(ch) < 2:
            continue
        fig.add_trace(go.Scatter(x=ch[:, 1], y=ch[:, 2], mode="lines",
                                 name="truth (MAVLink)", legendgroup="truth",
                                 showlegend=first, line=dict(color=BLUE, width=2),
                                 text=[f"{t:%H:%M:%S}" for t in pdt(ch[:, 0])],
                                 hovertemplate="%{text}<br>E %{x:.0f} N %{y:.0f}"
                                               "<extra>truth</extra>"))
        first = False
    br = math.radians(rotate_deg)
    for tid in PLOT_TRACKS[fl]:
        mw = prep(tid, w)
        if mw is None:
            continue
        E = mw[:, 1] * math.cos(br) + mw[:, 2] * math.sin(br)
        N = mw[:, 2] * math.cos(br) - mw[:, 1] * math.sin(br)
        fig.add_trace(go.Scatter(
            x=E, y=N, mode="lines+markers", name=f"trk {tid}",
            showlegend=show_legend,
            line=dict(color=colors[tid], width=2.6),
            marker=dict(size=6, color=colors[tid], opacity=mw[:, 14].tolist(),
                        line=dict(width=1, color=SURFACE)),
            text=[f"{t:%H:%M:%S}" for t in pdt(mw[:, 0])],
            hovertemplate="%{text}<br>E %{x:.0f} N %{y:.0f}<extra>trk " + str(tid) + "</extra>"))
    pad = 150
    xr = [Tw[:, 1].min() - pad, Tw[:, 1].max() + pad]
    yr = [Tw[:, 2].min() - pad, Tw[:, 2].max() + pad]
    fig.update_layout(**LAYOUT)
    fig.update_yaxes(scaleanchor="x", scaleratio=1, title="North (m)", range=yr)
    fig.update_xaxes(title="East (m)", range=xr)
    return fig

def angle_fig(fl, w, colors, which):
    fig = go.Figure()
    lims = []
    # obs first so they sit BEHIND the bands/lines; thinned + faint = texture
    ob = obs_errors(fl, w)
    if len(ob):
        step = max(1, len(ob) // 500)
        obt = ob[::step]
        oe = obt[:, 1] if which == "az" else obt[:, 2]
        fig.add_trace(go.Scatter(x=pdt(obt[:, 0]), y=oe, mode="markers",
                                 name="raw observations",
                                 marker=dict(symbol="x-thin", size=4, opacity=0.45,
                                             line=dict(width=1, color=INK2)),
                                 hovertemplate="%{x|%H:%M:%S}<br>obs %{y:.2f}°"
                                               "<extra>obs</extra>"))
        lims.append(np.percentile(np.abs(oe), 99))
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        ae = ang_raw(m)      # RAW: no bias removal, no datum removal
        err = ae["az_err"] if which == "az" else ae["el_err"]
        sig = ae["sig_az"] if which == "az" else ae["sig_el"]
        t = pdt(m[:, 0])
        c = colors[tid]
        # ±3σ: thin dotted boundary lines only (no fill — kills the band soup)
        for sgn in (1, -1):
            fig.add_trace(go.Scatter(x=t, y=err + sgn*3*sig, mode="lines",
                                     line=dict(width=1, color=rgba(c, 0.45), dash="dot"),
                                     showlegend=False, legendgroup=f"g{tid}",
                                     hoverinfo="skip"))
        # ±1σ: the single filled band
        fig.add_trace(go.Scatter(
            x=t + t[::-1], y=list(err + sig) + list(err - sig)[::-1],
            fill="toself", fillcolor=rgba(c, 0.22),
            line=dict(width=0),
            showlegend=False, legendgroup=f"g{tid}", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=t, y=err, mode="lines+markers",
                                 name=f"trk {tid}", legendgroup=f"g{tid}",
                                 line=dict(color=c, width=2.4),
                                 marker=dict(size=5, color=c, opacity=m[:, 14].tolist()),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.2f}°<extra>" +
                                               f"{tid}</extra>"))
        lims.append(np.percentile(np.abs(err) + sig, 99))
    fig.add_hline(y=0, line=dict(color=INK2, width=1, dash="dash"))
    fig.update_layout(**LAYOUT)
    lim = min(4.0, max(1.0, math.ceil(max(lims) * 1.15 / 0.5) * 0.5)) if lims else 2.0
    fig.update_yaxes(title=f"{which} error (deg)", dtick=0.5, range=[-lim, lim])
    fig.update_xaxes(title=f"time ({TZ_ABBR})")
    return fig

def enu3d_fig(fl, w, colors):
    fig = go.Figure()
    e3all = []
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        dEc, dNc, dUc = corrected(m)
        e3 = np.sqrt(dEc**2 + dNc**2 + dUc**2)
        e3all.append(e3)
        t = pdt(m[:, 0])
        c = colors[tid]
        fig.add_trace(go.Scatter(x=t, y=e3, mode="lines", name=f"{tid} 3D",
                                 legendgroup=f"g{tid}", line=dict(color=c, width=2.4),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.0f} m<extra>" +
                                               f"{tid} 3D</extra>"))
        for nm, v, dash in ((f"{tid} dE", dEc, "dot"), (f"{tid} dN", dNc, "dash"),
                            (f"{tid} dU", dUc, "longdash")):
            fig.add_trace(go.Scatter(x=t, y=v, mode="lines", name=nm,
                                     legendgroup=f"g{tid}", visible="legendonly",
                                     line=dict(color=c, width=1.4, dash=dash),
                                     hovertemplate="%{x|%H:%M:%S}<br>%{y:.0f} m<extra>" +
                                                   nm + "</extra>"))
    fig.add_hline(y=0, line=dict(color=INK2, width=1, dash="dash"))
    fig.update_layout(**LAYOUT)
    lim = min(400, max(60, np.percentile(np.concatenate(e3all), 99) * 1.2)) if e3all else 200
    fig.update_yaxes(title="3D ENU error (m) — RAW (truth alt datum removed)",
                     range=[-lim * 0.4, lim])
    fig.update_xaxes(title=f"time ({TZ_ABBR})")
    return fig

def vel_fig(fl, w, colors):
    """RAW velocity error: track velocity vs truth velocity (3D magnitude default)."""
    fig = go.Figure()
    dvall = []
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        V = tracksV[tid]
        idx = np.clip(np.searchsorted(V[:, 0], m[:, 0]), 0, len(V) - 1)
        tvE, tvN, tvU = V[idx, 4], V[idx, 5], V[idx, 6]     # filter state velocity
        gvE, gvN, gvU, ok = truth_vel_at(m[:, 0])           # MAVLink-reported velocity
        m, tvE, tvN, tvU = m[ok], tvE[ok], tvN[ok], tvU[ok]
        gvE, gvN, gvU = gvE[ok], gvN[ok], gvU[ok]
        if len(m) < 6:
            continue
        dvE, dvN, dvU = tvE - gvE, tvN - gvN, tvU - gvU
        dv3 = np.sqrt(dvE**2 + dvN**2 + dvU**2)
        dvall.append(dv3)
        t = pdt(m[:, 0])
        c = colors[tid]
        fig.add_trace(go.Scatter(x=t, y=dv3, mode="lines", name=f"{tid} |Δv|",
                                 legendgroup=f"g{tid}", line=dict(color=c, width=2.4),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.1f} m/s<extra>" +
                                               f"{tid}</extra>"))
        for nm, v, dash in ((f"{tid} ΔvE", dvE, "dot"), (f"{tid} ΔvN", dvN, "dash"),
                            (f"{tid} ΔvU", dvU, "longdash")):
            fig.add_trace(go.Scatter(x=t, y=v, mode="lines", name=nm,
                                     legendgroup=f"g{tid}", visible="legendonly",
                                     line=dict(color=c, width=1.4, dash=dash),
                                     hovertemplate="%{x|%H:%M:%S}<br>%{y:.1f} m/s<extra>" +
                                                   nm + "</extra>"))
    fig.add_hline(y=0, line=dict(color=INK2, width=1, dash="dash"))
    fig.update_layout(**LAYOUT)
    lim = (min(45, max(8, np.percentile(np.concatenate(dvall), 99.5) * 1.3))
           if dvall else 15)
    fig.update_yaxes(title="velocity error (m/s) — RAW", range=[-lim * 0.15, lim])
    fig.update_xaxes(title=f"time ({TZ_ABBR})")
    return fig

def rate_fig(fl, w, colors):
    """One line: merged measurement rate on the drone, Hz, 30 s sliding window."""
    fts = []
    for tid in PLOT_TRACKS[fl]:
        A = tracks.get(tid)
        if A is None:
            continue
        ft = C.meas_times(A)
        fts.append(ft[(ft >= w[0]) & (ft <= w[1])])
    allft = np.sort(np.concatenate(fts)) if fts else np.array([])
    fig = go.Figure()
    if len(allft) > 3:
        grid = np.arange(w[0], w[1] + 2.5, 5.0)
        WIN = 30.0
        cnt = np.searchsorted(allft, grid + WIN/2) - np.searchsorted(allft, grid - WIN/2)
        lo = np.maximum(grid - WIN/2, w[0])
        hi = np.minimum(grid + WIN/2, w[1])
        hz = cnt / np.maximum(hi - lo, 1.0)
        fig.add_trace(go.Scatter(x=pdt(grid), y=hz, mode="lines",
                                 name="measurement rate on drone (any track)",
                                 line=dict(color=BLUE, width=2.6, shape="spline"),
                                 fill="tozeroy", fillcolor=rgba(BLUE, 0.10),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.2f} Hz<extra></extra>"))
    fig.add_hline(y=2, line=dict(color=INK2, width=1, dash="dot"),
                  annotation_text="2 Hz = every dwell", annotation_font_color=INK2)
    n_obs = len(obs_errors(fl, w))
    note = (dict(x=0.995, y=0.05, xref="paper", yref="paper",
                 xanchor="right", yanchor="bottom", showarrow=False,
                 font=dict(size=13, weight="bold"), bgcolor="rgba(255,255,255,0.85)",
                 text=f"{len(allft)} track measurements · avg "
                      f"{len(allft)/max(w[1]-w[0],1):.2f} Hz · {n_obs} matched raw obs")
            if len(allft) else None)
    fig.update_layout(**LAYOUT, annotations=[note] if note else [])
    fig.update_yaxes(title="Hz", range=[0, 2.3])
    fig.update_xaxes(title=f"time ({TZ_ABBR})")
    return fig

SEQ_STEPS = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
STANDOFFS = (600, 450, 300, 150)

def seeker_quad(fl, rn, colors):
    """Seeker-vertex angular error, quad of standoffs. Seeker sits AHEAD of the
    TRACK state along the track's velocity at standoff R, boresight on the track
    state; plotted is the 2D angle from boresight to TRUTH (az=horizontal ⊥,
    el=vertical), i.e. what the seeker must see off-boresight if guided to the
    track. 12° circle per panel."""
    w = INBOUND[(fl, rn)]
    samples = []      # t, ec (truth-track cross m), ev (vert m), epar (along m)
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        m2 = m[m[:, 15] > 4]
        if len(m2) < 4:
            continue
        V = tracksV[tid]
        idx = np.clip(np.searchsorted(V[:, 0], m2[:, 0]), 0, len(V) - 1)
        tvE, tvN = V[idx, 4], V[idx, 5]           # TRACK state velocity
        vmag = np.maximum(np.hypot(tvE, tvN), 0.5)
        uE, uN = tvE / vmag, tvN / vmag
        dE, dN, dUc = corrected(m2)               # track - truth
        eE, eN, eU = -dE, -dN, -dUc               # truth - track
        ec = eE * (-uN) + eN * uE
        epar = eE * uE + eN * uN
        for i in range(len(m2)):
            samples.append((m2[i, 0], ec[i], eU[i], epar[i]))
    fig = go.Figure()
    stats = {}
    if samples:
        S = np.array(sorted(samples))
        t0 = S[0, 0]
        th = np.linspace(0, 2 * np.pi, 120)
        panels = (("x", "y"), ("x2", "y2"), ("x3", "y3"), ("x4", "y4"))
        annots = []
        for k, (R, (ax, ay)) in enumerate(zip(STANDOFFS, panels)):
            denom = R - S[:, 3]
            okd = denom > 20
            az = np.degrees(np.arctan2(S[okd, 1], denom[okd]))
            el = np.degrees(np.arctan2(S[okd, 2], denom[okd]))
            inside = float(np.mean(np.hypot(az, el) <= 12.0)) * 100
            stats[R] = round(inside, 1)
            fig.add_trace(go.Scatter(
                x=az, y=el, mode="markers", xaxis=ax, yaxis=ay,
                marker=dict(size=6, color=(S[okd, 0] - t0) / 60,
                            colorscale=[[i/(len(SEQ_STEPS)-1), c]
                                        for i, c in enumerate(SEQ_STEPS)],
                            showscale=(k == 0),
                            colorbar=dict(title=dict(text="min into<br>inbound",
                                                     font=dict(size=12)),
                                          thickness=13, x=1.0, xanchor="left",
                                          len=0.9)),
                text=[f"{p:%H:%M:%S}" for p in pdt(S[okd, 0])],
                hovertemplate="%{text}<br>az %{x:.1f}° el %{y:.1f}°<extra>" +
                              f"{R} m</extra>", showlegend=False))
            fig.add_trace(go.Scatter(x=12*np.cos(th), y=12*np.sin(th), mode="lines",
                                     xaxis=ax, yaxis=ay, showlegend=False,
                                     line=dict(color=RED, width=1.6, dash="dash"),
                                     hoverinfo="skip"))
            annots.append(dict(x=0.04, y=0.96, xref=f"{ax} domain",
                               yref=f"{ay} domain", xanchor="left", yanchor="top",
                               showarrow=False,
                               font=dict(size=15, weight="bold"),
                               bgcolor="rgba(255,255,255,0.85)",
                               text=f"standoff {R} m — {inside:.0f}% inside 12°"))
        LIM = 30
        axd = dict(range=[-LIM, LIM], dtick=10, constrain="domain",
                   showline=True, mirror=True)
        fig.update_layout(**LAYOUT, annotations=annots,
            shapes=[dict(type="line", xref="paper", yref="paper", x0=0.5, x1=0.5,
                         y0=0, y1=1, line=dict(color=AXLINE, width=1.5)),
                    dict(type="line", xref="paper", yref="paper", x0=0, x1=1,
                         y0=0.5, y1=0.5, line=dict(color=AXLINE, width=1.5))],
            xaxis=dict(domain=[0, 0.46], **axd),
            xaxis2=dict(domain=[0.54, 1.0], **axd),
            xaxis3=dict(domain=[0, 0.46], anchor="y3",
                        title="seeker az off-boresight (deg)", **axd),
            xaxis4=dict(domain=[0.54, 1.0], anchor="y4",
                        title="seeker az off-boresight (deg)", **axd),
            yaxis=dict(domain=[0.55, 1], scaleanchor="x",
                       title="seeker el off-boresight (deg)", **axd),
            yaxis2=dict(domain=[0.55, 1], anchor="x2", scaleanchor="x2", **axd),
            yaxis3=dict(domain=[0, 0.45], anchor="x3", scaleanchor="x3",
                        title="seeker el off-boresight (deg)", **axd),
            yaxis4=dict(domain=[0, 0.45], anchor="x4", scaleanchor="x4", **axd))
        fig.update_layout(margin=dict(r=100))   # room for the colorbar
    return fig, stats

def seeker_cartoon(qstats):
    pacq = {r: f"{qstats.get(r, 0):.0f}%" for r in STANDOFFS}
    return f"""
<svg viewBox="0 0 920 330" style="max-width:920px;display:block;margin:.5rem auto;
     background:#fff;border:1px solid #e4e3df;border-radius:6px"
     xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI,Arial" font-size="14">
  <defs><marker id="ah" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L6,3 L0,6 z" fill="#52514e"/></marker></defs>
  <!-- FOV cone (shaded) -->
  <path d="M118,150 L760,78 L760,222 Z" fill="rgba(227,73,72,0.07)"
        stroke="#e34948" stroke-width="1.5" stroke-dasharray="6,4"/>
  <text x="560" y="66" fill="#e34948" font-weight="bold">12° seeker FOV</text>
  <!-- missile -->
  <g>
    <rect x="52" y="142" width="52" height="16" rx="3" fill="#0b0b0b"/>
    <polygon points="104,142 122,150 104,158" fill="#0b0b0b"/>
    <polygon points="52,142 40,132 52,150" fill="#0b0b0b"/>
    <polygon points="52,158 40,168 52,150" fill="#0b0b0b"/>
    <text x="34" y="188" fill="#0b0b0b" font-weight="bold">interceptor / seeker</text>
  </g>
  <!-- standoff range ticks: dropped all the way to the pAcq row -->
  <g stroke="#9a9994" stroke-dasharray="4,4">
    <line x1="278" y1="95" x2="278" y2="290"/>
    <line x1="438" y1="95" x2="438" y2="290"/>
    <line x1="598" y1="95" x2="598" y2="290"/>
    <line x1="758" y1="95" x2="758" y2="290"/>
  </g>
  <g fill="#52514e" font-size="13" text-anchor="middle" font-weight="bold">
    <text x="278" y="90">150 m</text>
    <text x="438" y="90">300 m</text>
    <text x="565" y="90">450 m</text>
    <text x="726" y="56">600 m</text>
  </g>
  <!-- boresight to TRACK -->
  <line x1="122" y1="150" x2="742" y2="150" stroke="#52514e" stroke-width="2"
        marker-end="url(#ah)"/>
  <text x="300" y="142" fill="#52514e" font-size="13">boresight — guidance flew to the
    TRACK state</text>
  <rect x="744" y="141" width="18" height="18" fill="none" stroke="#7c3aed"
        stroke-width="3"/>
  <text x="700" y="132" fill="#7c3aed" font-weight="bold">TRACK</text>
  <!-- TRUTH UAV (quadcopter) -->
  <line x1="122" y1="150" x2="714" y2="232" stroke="#2a78d6" stroke-width="2"
        marker-end="url(#ah)"/>
  <g stroke="#2a78d6" stroke-width="2.5" fill="none">
    <line x1="712" y1="222" x2="736" y2="242"/><line x1="736" y1="222" x2="712" y2="242"/>
    <circle cx="712" cy="222" r="6"/><circle cx="736" cy="222" r="6"/>
    <circle cx="712" cy="242" r="6"/><circle cx="736" cy="242" r="6"/>
  </g>
  <text x="724" y="268" fill="#2a78d6" font-weight="bold"
        text-anchor="middle">TRUTH UAV</text>
  <!-- delta angle arc -->
  <path d="M 252,150 A 130,130 0 0 1 246,167" stroke="#0b0b0b" stroke-width="2"
        fill="none"/>
  <rect x="258" y="153" width="228" height="17" fill="#ffffff" opacity="0.9" rx="3"/>
  <text x="262" y="166" fill="#0b0b0b" font-weight="bold"
        font-size="13">Δ = off-boresight angle (plotted below)</text>
  <!-- per-standoff acquisition probability (this run) -->
  <line x1="40" y1="278" x2="840" y2="278" stroke="#e4e3df"/>
  <text x="44" y="308" fill="#0b0b0b" font-weight="bold" font-size="16">pAcq
    (12° FOV):</text>
  <g font-size="21" font-weight="bold" fill="#e34948" text-anchor="middle">
    <text x="278" y="312">{pacq[150]}</text>
    <text x="438" y="312">{pacq[300]}</text>
    <text x="598" y="312">{pacq[450]}</text>
    <text x="758" y="312">{pacq[600]}</text>
  </g>
</svg>"""

def perp_fig(fl, rn, colors):
    """Angle-space seeker basket: RAW error ⊥ to velocity, in degrees
    (perp meters ÷ slant range), on this run's inbound leg. Dots colored by
    time; dashed 12° reference circle."""
    w = INBOUND[(fl, rn)]
    rows = []
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        m = m[m[:, 15] > 4]           # moving truth only
        if len(m) < 4:
            continue
        vE, vN, _vU, ok = truth_vel_at(m[:, 0])     # TRUE target frame: MAVLink velocity
        m, vE, vN = m[ok], vE[ok], vN[ok]
        if len(m) < 4:
            continue
        vmag = np.maximum(np.hypot(vE, vN), 0.5)
        uE, uN = vE / vmag, vN / vmag
        dEc, dNc, dUc = corrected(m)
        cross = dEc * (-uN) + dNc * uE      # meters, ⊥ velocity (MAVLink frame)
        for i in range(len(m)):
            rows.append((m[i, 0], cross[i], dUc[i], tid))
    fig = go.Figure()
    stats = {}
    if rows:
        rows.sort()
        t = np.array([r[0] for r in rows])
        cx = np.array([r[1] for r in rows])
        cy = np.array([r[2] for r in rows])
        tidv = [str(int(r[3])) for r in rows]
        tmin = t[0]
        fig.add_trace(go.Scatter(
            x=cx, y=cy, mode="markers",
            marker=dict(size=7, color=(t - tmin) / 60,
                        colorscale=[[i/(len(SEQ_STEPS)-1), c] for i, c in enumerate(SEQ_STEPS)],
                        colorbar=dict(title="min into<br>inbound", thickness=14),
                        line=dict(width=0.5, color=SURFACE)),
            text=[f"{p:%H:%M:%S} · trk {tv}" for p, tv in zip(pdt(t), tidv)],
            hovertemplate="%{text}<br>cross %{x:.2f}° · vert %{y:.2f}°<extra></extra>",
            showlegend=False))
        rad = np.hypot(cx, cy)
        stats = dict(n=len(cx), r50=float(np.percentile(rad, 50)),
                     r95=float(np.percentile(rad, 95)),
                     mean_cross=float(np.mean(cx)), mean_vert=float(np.mean(cy)))
        th = np.linspace(0, 2*np.pi, 120)
        for r, nm, col, dash in ((stats["r50"], f"R50 = {stats['r50']:.0f} m", INK2, None),
                                 (stats["r95"], f"R95 = {stats['r95']:.0f} m", INK2, "dot")):
            fig.add_trace(go.Scatter(x=r*np.cos(th), y=r*np.sin(th), mode="lines",
                                     name=nm, line=dict(color=col, width=1.6, dash=dash),
                                     hoverinfo="skip"))
        # 12° seeker FOV footprint at assumed seeker->target acquisition ranges
        for racq in (500, 1000, 2000):
            r = math.tan(math.radians(12.0)) * racq
            fig.add_trace(go.Scatter(x=r*np.cos(th), y=r*np.sin(th), mode="lines",
                                     name=f"12° FOV @ {racq/1000:g} km acq",
                                     line=dict(color=RED, width=1.4, dash="dash"),
                                     opacity=0.35 + 0.2*(racq == 1000),
                                     hoverinfo="skip"))
    fig.add_hline(y=0, line=dict(color=GRID, width=1))
    fig.add_vline(x=0, line=dict(color=GRID, width=1))
    lim = 470
    fig.update_layout(**LAYOUT)
    fig.update_yaxes(title="vertical error (m)", scaleanchor="x", scaleratio=1,
                     range=[-lim, lim])
    fig.update_xaxes(title="cross-track error (m)  (⊥ to MAVLink velocity)",
                     range=[-lim, lim])
    return fig, stats

def maps_fig(fl, w, colors):
    """RAW map (left) and az-bias-rotated map (right) in ONE figure, one legend."""
    fig = go.Figure()
    Tw = win(T, w)
    pad = 150
    xr = [Tw[:, 1].min() - pad, Tw[:, 1].max() + pad]
    yr = [Tw[:, 2].min() - pad, Tw[:, 2].max() + pad]
    for panel, rot in ((0, 0.0), (1, -BIAS)):
        ax, ay = ("x", "y") if panel == 0 else ("x2", "y2")
        first = True
        for ch in seg_split(Tw):
            if len(ch) < 2:
                continue
            fig.add_trace(go.Scatter(x=ch[:, 1], y=ch[:, 2], mode="lines",
                                     xaxis=ax, yaxis=ay, name="truth (MAVLink)",
                                     legendgroup="truth",
                                     showlegend=(panel == 0 and first),
                                     line=dict(color=BLUE, width=2),
                                     hovertemplate="E %{x:.0f} N %{y:.0f}<extra>truth</extra>"))
            first = False
        br = math.radians(rot)
        for tid in PLOT_TRACKS[fl]:
            mw = prep(tid, w)
            if mw is None:
                continue
            E = mw[:, 1] * math.cos(br) + mw[:, 2] * math.sin(br)
            N = mw[:, 2] * math.cos(br) - mw[:, 1] * math.sin(br)
            fig.add_trace(go.Scatter(
                x=E, y=N, mode="lines+markers", xaxis=ax, yaxis=ay,
                name=f"trk {tid}", legendgroup=f"g{tid}", showlegend=(panel == 0),
                line=dict(color=colors[tid], width=2.6),
                marker=dict(size=5, color=colors[tid], opacity=mw[:, 14].tolist()),
                text=[f"{t:%H:%M:%S}" for t in pdt(mw[:, 0])],
                hovertemplate="%{text}<br>E %{x:.0f} N %{y:.0f}<extra>trk " +
                              str(tid) + "</extra>"))
    fig.update_layout(**LAYOUT,
        xaxis=dict(domain=[0, 0.485], range=xr, title="East (m)", constrain="domain"),
        xaxis2=dict(domain=[0.515, 1], range=xr, title="East (m)", constrain="domain"),
        yaxis=dict(range=yr, scaleanchor="x", title="North (m)", constrain="domain"),
        yaxis2=dict(range=yr, scaleanchor="x2", anchor="x2", showticklabels=False,
                    constrain="domain"),
        annotations=[
            dict(x=0.97, y=0.02, xref="x domain", yref="y domain",
                 xanchor="right", yanchor="bottom", showarrow=False,
                 font=dict(size=13, weight="bold"), bgcolor="rgba(255,255,255,0.85)",
                 text=f"{sum(int(prep(t_, w)[:, 14].sum()) for t_ in PLOT_TRACKS[fl] if prep(t_, w) is not None)} measurements in window"),
            dict(x=0.03, y=0.98, xref="x domain", yref="y domain",
                 xanchor="left", yanchor="top", showarrow=False,
                 font=dict(size=14), bgcolor="rgba(255,255,255,0.75)",
                 text="<b>RAW</b> (as the radar reports)"),
            dict(x=0.03, y=0.98, xref="x2 domain", yref="y2 domain",
                 xanchor="left", yanchor="top", showarrow=False,
                 font=dict(size=14), bgcolor="rgba(255,255,255,0.75)",
                 text=f"<b>rotated {BIAS:+.2f}°</b> (alignment check only)")])
    return fig

def angle_stack(fl, w, colors):
    """Az / El / Range / Altitude error, shared time axis, ONE legend entry per track."""
    # (key, yaxis, title, unit, obs-col in obs_errors, ang_raw err key, ang_raw sig key)
    ROWS = [("az",  "y",  "az error (deg)",     "deg", 1, "az_err",  "sig_az"),
            ("el",  "y2", "el error (deg)",     "deg", 2, "el_err",  "sig_el"),
            ("rng", "y3", "range error (m)",    "m",   3, "rng_err", "sig_rng"),
            ("alt", "y4", "altitude error (m)", "m",   4, "alt_err", "sig_alt")]
    DOM = {"az": [0.79, 1.0], "el": [0.53, 0.74], "rng": [0.27, 0.48], "alt": [0.0, 0.21]}
    fig = go.Figure()
    lims = {r[0]: [] for r in ROWS}
    errs = {r[0]: [] for r in ROWS}
    ob = obs_errors(fl, w)
    if len(ob):
        step = max(1, len(ob) // 500)
        obt = ob[::step]
        for key, ay, _t, unit, oc, _e, _s in ROWS:
            oe = obt[:, oc]
            uu = "°" if unit == "deg" else " m"
            fig.add_trace(go.Scatter(x=pdt(obt[:, 0]), y=oe, mode="markers", yaxis=ay,
                                     name="raw observations", legendgroup="obs",
                                     showlegend=(key == "az"),
                                     marker=dict(symbol="x-thin", size=4, opacity=0.45,
                                                 line=dict(width=1, color=INK2)),
                                     hovertemplate="%{x|%H:%M:%S}<br>obs %{y:.2f}" + uu +
                                                   "<extra>obs</extra>"))
            lims[key].append(np.percentile(np.abs(oe), 99))
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        ae = ang_raw(m)
        t = pdt(m[:, 0])
        c = colors[tid]
        for key, ay, _t, unit, oc, ek, sk in ROWS:
            err, sig = ae[ek], ae[sk]
            uu = "°" if unit == "deg" else " m"
            for sgn in (1, -1):
                fig.add_trace(go.Scatter(x=t, y=err + sgn*3*sig, mode="lines", yaxis=ay,
                                         line=dict(width=1, color=rgba(c, 0.45), dash="dot"),
                                         showlegend=False, legendgroup=f"g{tid}",
                                         hoverinfo="skip"))
            fig.add_trace(go.Scatter(
                x=t + t[::-1], y=list(err + sig) + list(err - sig)[::-1],
                fill="toself", fillcolor=rgba(c, 0.20), yaxis=ay,
                line=dict(width=0), showlegend=False, legendgroup=f"g{tid}",
                hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=t, y=err, mode="lines+markers", yaxis=ay,
                                     name=f"trk {tid}", legendgroup=f"g{tid}",
                                     showlegend=(key == "az"),
                                     line=dict(color=c, width=2.4),
                                     marker=dict(size=5, color=c,
                                                 opacity=m[:, 14].tolist()),
                                     hovertemplate="%{x|%H:%M:%S}<br>%{y:.2f}" + uu +
                                                   "<extra>" + f"{tid} {key}</extra>"))
            lims[key].append(np.percentile(np.abs(err) + sig, 99))
            errs[key].append(err)
    # AUTO-FIT scales: p99 of |err|+sigma (and obs) per panel, padded, with sane
    # floors — panels always show the data instead of clipping bias-era errors.
    def _nice(x):
        e = math.floor(math.log10(max(x, 1e-6))); f = x / 10 ** e
        n = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
        return n * 10 ** e
    FLOOR = {"az": 1.0, "el": 1.0, "rng": 10.0, "alt": 15.0}
    L = {k: (max(FLOOR[k], 1.15 * max(v)) if v else FLOOR[k] * 3) for k, v in lims.items()}
    DT = {k: _nice(L[k] / 3.0) for k in L}
    notes = [dict(x=0.004, y=0.996, xref="paper", yref="paper", xanchor="left",
                  yanchor="top", showarrow=False, font=dict(size=11, color=INK2),
                  bgcolor="rgba(255,255,255,0.7)",
                  text="shaded = filter ±1σ · dotted = ±3σ")]
    for key, ay, _t, unit, oc, _e, _s in ROWS:
        yref = "y domain" if ay == "y" else ay + " domain"
        uu = "°" if unit == "deg" else " m"
        if len(ob):
            oe = ob[:, oc]
            off = float(np.mean(np.abs(oe) > L[key])) * 100
            if off > 2:
                notes.append(dict(x=0.995, y=0.97, xref="paper", yref=yref,
                                  xanchor="right", yanchor="top", showarrow=False,
                                  font=dict(size=11, color=INK2),
                                  bgcolor="rgba(255,255,255,0.75)",
                                  text=f"{off:.0f}% of obs beyond ±{L[key]:g}{uu} scale"))
        if errs[key]:
            e = np.concatenate(errs[key])
            # Robust median ± σ: RMSE over the raw set is dominated by a minority of
            # filter-initiation and doppler-notch turn transients (the brief spikes
            # visible on the lines), which misrepresents the steady-state accuracy.
            med = float(np.median(e))
            rsig = float(0.5 * (np.percentile(e, 84) - np.percentile(e, 16)))
            txt = f"track {med:+.2f}{uu} ± {rsig:.2f}{uu}"
            if len(ob):
                oe2 = ob[:, oc]
                omed = float(np.median(oe2))
                orsig = float(0.5 * (np.percentile(oe2, 84) - np.percentile(oe2, 16)))
                txt += f"   ·   obs {omed:+.2f}{uu} ± {orsig:.2f}{uu}"
            notes.append(dict(x=0.995, y=0.03, xref="paper", yref=yref,
                              xanchor="right", yanchor="bottom", showarrow=False,
                              font=dict(size=15, weight="bold"),
                              bgcolor="rgba(255,255,255,0.85)",
                              text=txt))
    fig.update_layout(**LAYOUT, annotations=notes, height=980,
        xaxis=dict(anchor="y4", title=f"time ({TZ_ABBR})"),
        yaxis=dict(domain=DOM["az"], range=[-L["az"], L["az"]], dtick=DT["az"], title="az err (deg)"),
        yaxis2=dict(domain=DOM["el"], range=[-L["el"], L["el"]], dtick=DT["el"], title="el err (deg)"),
        yaxis3=dict(domain=DOM["rng"], range=[-L["rng"], L["rng"]], dtick=DT["rng"], title="range err (m)"),
        yaxis4=dict(domain=DOM["alt"], range=[-L["alt"], L["alt"]], dtick=DT["alt"], title="alt err (m)"),
        shapes=[dict(type="line", xref="paper", x0=0, x1=1, yref=yr_, y0=0, y1=0,
                     line=dict(color=INK2, width=1, dash="dash"))
                for yr_ in ("y", "y2", "y3", "y4")])
    return fig

def posvel_fig(fl, w, colors):
    """3D position error (top) + |Δv| velocity error (bottom), one legend."""
    fig = go.Figure()
    e3all, dvall = [], []
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        dEc, dNc, dUc = corrected(m)
        e3 = np.sqrt(dEc**2 + dNc**2 + dUc**2)
        e3all.append(e3)
        t = pdt(m[:, 0])
        c = colors[tid]
        fig.add_trace(go.Scatter(x=t, y=e3, mode="lines", yaxis="y",
                                 name=f"trk {tid}", legendgroup=f"g{tid}",
                                 line=dict(color=c, width=2.4),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.0f} m<extra>" +
                                               f"{tid} 3D pos</extra>"))
        V = tracksV[tid]
        idx = np.clip(np.searchsorted(V[:, 0], m[:, 0]), 0, len(V) - 1)
        tvE, tvN, tvU = V[idx, 4], V[idx, 5], V[idx, 6]
        gvE, gvN, gvU, ok = truth_vel_at(m[:, 0])
        if ok.sum() >= 6:
            tt = m[ok, 0]
            # HORIZONTAL velocity error only: this build's x_state[5] is not a
            # usable vertical rate (|vU| 100-200 "m/s" — see key findings)
            dv3 = np.sqrt((tvE[ok]-gvE[ok])**2 + (tvN[ok]-gvN[ok])**2)
            # 10 s centered rolling mean (MAVLink GPS velocity is noisy at 1 Hz)
            k = max(3, int(round(10.0 / max(np.median(np.diff(tt)), 0.2))) | 1)
            sm = np.convolve(dv3, np.ones(k) / k, mode="same")
            dvall.append((sm, dv3))
            fig.add_trace(go.Scatter(x=pdt(tt), y=dv3, mode="lines", yaxis="y2",
                                     legendgroup=f"g{tid}", showlegend=False,
                                     line=dict(color=rgba(c, 0.25), width=1),
                                     hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=pdt(tt), y=sm, mode="lines", yaxis="y2",
                                     legendgroup=f"g{tid}", showlegend=False,
                                     line=dict(color=c, width=2.4),
                                     hovertemplate="%{x|%H:%M:%S}<br>%{y:.1f} m/s "
                                                   "(10 s mean)<extra>" +
                                                   f"{tid} |Δv|</extra>"))
    notes = []
    if e3all:
        e = np.concatenate(e3all)
        notes.append(dict(x=0.995, y=0.05, xref="paper", yref="y domain",
                          xanchor="right", yanchor="bottom", showarrow=False,
                          font=dict(size=13, weight="bold"),
                          bgcolor="rgba(255,255,255,0.85)",
                          text=f"n={len(e)} · RMS {np.sqrt(np.mean(e**2)):.0f} m · "
                               f"3σ (99.7%) within {np.percentile(e, 99.7):.0f} m"))
    if dvall:
        sm_all = np.concatenate([s for s, _ in dvall])
        raw_all = np.concatenate([r for _, r in dvall])
        vl = min(30, max(6.0, float(sm_all.max()) * 1.25,
                         float(np.percentile(raw_all, 95)) * 1.2))
        off = float(np.mean(raw_all > vl)) * 100
        if off > 1:
            notes.append(dict(x=0.995, y=0.97, xref="paper", yref="y2 domain",
                              xanchor="right", yanchor="top", showarrow=False,
                              font=dict(size=12, color=INK2),
                              bgcolor="rgba(255,255,255,0.8)",
                              text=f"{off:.0f}% of raw samples beyond {vl:.0f} m/s "
                                   "(smoothed line unclipped)"))
        notes.append(dict(x=0.995, y=0.05, xref="paper", yref="y2 domain",
                          xanchor="right", yanchor="bottom", showarrow=False,
                          font=dict(size=13, weight="bold"),
                          bgcolor="rgba(255,255,255,0.85)",
                          text=f"n={len(raw_all)} · RMS {np.sqrt(np.mean(raw_all**2)):.1f} m/s · "
                               f"3σ (99.7%) within {np.percentile(raw_all, 99.7):.1f} m/s"))
    else:
        vl = 15
    fig.update_layout(**LAYOUT, annotations=notes,
        xaxis=dict(anchor="y2", title=f"time ({TZ_ABBR})"),
        yaxis=dict(domain=[0.56, 1], rangemode="tozero", title="3D pos error (m)"),
        yaxis2=dict(domain=[0, 0.44], range=[0, vl],
                    title="horizontal |Δv| (m/s)"))
    return fig

# ---------- timelines ----------
def timeline(fl):
    ev = []
    f0, f1 = FLIGHTS[fl]
    ev.append((f0, "TAKEOFF", f"truth departs pad"))
    ev.append((LANDING[fl][0], "LANDING PHASE", "final approach / pattern work begins (excluded from lap metrics)"))
    ev.append((f1, "LANDED", "truth stationary at pad"))
    tr = S1["tracks"]
    prev_last = None
    for tid in FLIGHT_TRACKS[fl]:
        r = tr.get(str(tid))
        if not r or r.get("init") is None:
            continue
        ev.append((r["first_meas"], f"TRACK {tid} INIT",
                   f"first measurement at rng {r['init'][0]:.0f} m, "
                   f"alt ~{to_agl(r['init'][1]):.0f} m AGL"))
        for t0c, gap in r["coasts"]:
            if gap > 5:
                ev.append((t0c, f"TRACK {tid} COAST", f"{gap:.0f} s without measurements"))
        d = r["drop"]
        if d:
            ev.append((r["last_meas"], f"TRACK {tid} LAST MEAS",
                       f"rng {d[0]:.0f} m, alt ~{to_agl(d[1]):.0f} m AGL, rdot {d[2]:+.0f} m/s "
                       f"(track coasts ~8 s then drops)"))
    ev.sort()
    return ev

def timeline_html(fl):
    rows = "".join(f"<tr><td>{ps(t)}</td><td>{k}</td>"
                   f"<td style='text-align:left'>{d}</td></tr>"
                   for t, k, d in timeline(fl))
    return (f"<table class='tl'><tr><th>time ({TZ_ABBR})</th><th>event</th><th>detail</th></tr>"
            f"{rows}</table>")

# ---------- per-run stats ----------
def run_stats(fl, w):
    horiz_c, e3d, azres = [], [], []
    fts = []
    for tid in PLOT_TRACKS[fl]:
        m = prep(tid, w)
        if m is None:
            continue
        dEc, dNc, dUc = corrected(m)
        horiz_c.append(np.hypot(dEc, dNc))
        e3d.append(np.sqrt(dEc**2 + dNc**2 + dUc**2))
        A = tracks[tid]
        ft = C.meas_times(A)
        fts.append(ft[(ft >= w[0]) & (ft <= w[1])])
    h = np.concatenate(horiz_c) if horiz_c else np.array([0])
    e = np.concatenate(e3d) if e3d else np.array([0])
    allft = np.sort(np.concatenate(fts)) if fts else np.array([])
    # coverage: fraction of the run's WALL CLOCK with a radar measurement on the
    # drone within the last 2 s (1 s grid; truth-independent), + longest gap.
    # coverage & gaps evaluated between the run's first and last measurement —
    # climb-out (before first detection) and the window edges don't count.
    if len(allft) >= 2:
        grid = np.arange(allft[0], allft[-1], 1.0)
        idx = np.clip(np.searchsorted(allft, grid), 1, len(allft) - 1)
        near = np.minimum(np.abs(allft[idx] - grid), np.abs(allft[idx - 1] - grid))
        cov = float(np.mean(near < 2.0))
        difs = np.diff(allft)
        gi = int(np.argmax(difs))
        max_gap, gap_t = float(difs[gi]), float(allft[gi])
    else:
        cov, max_gap, gap_t = 0.0, float(w[1] - w[0]), w[0]
    return dict(horiz_med=float(np.median(h)), horiz_p95=float(np.percentile(h, 95)),
                e3d_med=float(np.median(e)), e3d_p95=float(np.percentile(e, 95)),
                meas=int(len(allft)),
                rate=float(len(allft) / max(w[1]-w[0], 1)), cov=cov,
                max_gap=max_gap, gap_t=gap_t)

# ---------- build page ----------
GRID2, AXLINE = "#d6d5d0", "#c4c3be"

def div(fig, h=460):
    # one funnel = one axis style everywhere: darker grid, outside ticks, same fonts
    fig.update_xaxes(gridcolor=GRID2, zerolinecolor=AXLINE, linecolor=AXLINE,
                     ticks="outside", tickcolor=AXLINE, automargin=True,
                     tickfont=dict(size=13, weight="bold"),
                     title_font=dict(size=15, weight="bold"))
    fig.update_yaxes(gridcolor=GRID2, zerolinecolor=AXLINE, linecolor=AXLINE,
                     ticks="outside", tickcolor=AXLINE, automargin=True,
                     tickfont=dict(size=13, weight="bold"),
                     title_font=dict(size=15, weight="bold"))
    return ('<div class="plot">' + fig.to_html(
        full_html=False, include_plotlyjs=False, default_width="100%",
        default_height=f"{h}px", config={"displaylogo": False, "responsive": True})
        + "</div>")

def build_flight_tab(flight_key):
    """EXACT tab-pane HTML for one flight/run window. flight_key: "F1R1",
    "F1 Run 1", or a (flight, run) tuple. Stores this run's stats in allstats."""
    if isinstance(flight_key, (tuple, list)):
        fl, rn = int(flight_key[0]), int(flight_key[1])
    else:
        nums = re.findall(r"\d+", str(flight_key))
        fl, rn = int(nums[0]), int(nums[1])
    w = RUNS[(fl, rn)]
    colors = color_of(fl)
    s = run_stats(fl, w)
    allstats[(fl, rn)] = s
    inbound_note = ""
    if GENERIC:
        # leg description from the data: truth ground range inside the window
        _tw = win(T, w)
        _r = np.hypot(_tw[:, 1], _tw[:, 2]) if len(_tw) else np.array([0.0])
        leg_txt = f"truth range {_r.min() / 1000:.1f}–{_r.max() / 1000:.1f} km"
    else:
        leg_txt = "outbound to ~3.9 km and back"          # legacy 8/26 mission text
    parts = [f"<p><b>Window:</b> {ps(w[0])}–{ps(w[1])} {TZ_ABBR} · {leg_txt} · "
             f"<b>horiz err med/p95 (RAW):</b> {s['horiz_med']:.0f} / {s['horiz_p95']:.0f} m · "
             f"<b>3D ENU med/p95:</b> {s['e3d_med']:.0f} / {s['e3d_p95']:.0f} m · "
             f"<b>measurements:</b> {s['meas']} ({s['rate']:.2f} Hz avg) · "
             f"<b>tracked coverage:</b> {s['cov']*100:.0f}% (meas within 2 s, "
             f"first→last detection) · <b>longest mid-lap gap:</b> {s['max_gap']:.0f} s "
             f"@ {ps(s['gap_t'])}</p>"]
    parts.append("<div class='cap'>Top-down EN — RAW left, az-bias-rotated right "
                 "(one legend controls both panels)</div>" +
                 div(maps_fig(fl, w, colors), 470) +
                 "<p style='color:" + INK2 + ";font-size:.88rem'>Reading the plots: solid "
                 "line = filter track state · filled dots on the line = filter measurement "
                 "updates · grey × = raw radar observations (detections on the drone) · "
                 "radar sits at (0,0) — pan/zoom out to see it.</p>")
    parts.append("<div class='cap'>Az (top) / El (bottom) error — RAW, no bias removal "
                 f"(az bias {BIAS:+.2f}° appears as the offset; el uses units-corrected truth "
                 "alt) · band ±1σ, dotted edges ±3σ · shared time axis, one legend</div>" +
                 div(angle_stack(fl, w, colors), 640))
    parts.append("<div class='cap'>3D position error (top) / HORIZONTAL velocity error "
                 "|Δv| (bottom) — RAW; filter state vs MAVLink-reported truth velocity "
                 "(solid = 10 s rolling mean, faint = per-sample; vertical rate excluded "
                 "— this build's track state does not publish a usable one, see Summary) "
                 "· shared time axis, one legend</div>" +
                 div(posvel_fig(fl, w, colors), 600))
    parts.append("<div class='cap'>Tracking measurement rate (all tracks merged, "
                 "30 s smoothed)</div>" + div(rate_fig(fl, w, colors), 320))
    qfig, qstats = seeker_quad(fl, rn, colors)
    if qstats:
        inbound_note = ("<p><b>Could a seeker guided to the TRACK state see the actual "
                        "drone?</b> Inbound leg "
                        f"{ps(INBOUND[(fl,rn)][0])}–{ps(INBOUND[(fl,rn)][1])}, RAW incl. "
                        "all biases. Inside 12° FOV: "
                        + " · ".join(f"{r} m: <b>{v:.0f}%</b>"
                                     for r, v in qstats.items()) + "</p>")
        allstats[(fl, rn, "seeker")] = qstats
    parts.append("<div class='cap'>Seeker off-boresight angle to TRUTH — seeker stood "
                 "off ahead of the TRACK state along the track velocity at 600/450/300/"
                 "150 m, boresight on the track state; dots colored by time; dashed red "
                 "circle = 12° FOV</div>" + seeker_cartoon(qstats)
                 + inbound_note + div(qfig, 700))
    parts.append(f"<div class='cap'>Flight {fl} full timeline</div>" + timeline_html(fl))
    return "\n".join(parts)

# ---------- summary tab ----------
def tracks_in(fl, w):
    out = []
    for tid in FLIGHT_TRACKS[fl]:
        r = S1["tracks"].get(str(tid))
        if r and r.get("first_meas") and r["first_meas"] < w[1] and r["last_meas"] > w[0]:
            out.append(str(tid))
    return ", ".join(out) or "—"

def _ensure_stats():
    """Fill allstats for any run whose flight tab hasn't been built yet.
    run_stats and seeker_quad are deterministic, so the numbers are identical to
    the ones build_flight_tab stores; no-op when all flight tabs were built first."""
    for (fl, rn), w in RUNS.items():
        if (fl, rn) not in allstats:
            allstats[(fl, rn)] = run_stats(fl, w)
        if (fl, rn, "seeker") not in allstats:
            _, qstats = seeker_quad(fl, rn, color_of(fl))
            if qstats:
                allstats[(fl, rn, "seeker")] = qstats

def build_summary_tab(notes_html=""):
    """Summary tab pane HTML. notes_html replaces the narrative block (the
    "Key findings" list + obs-level addendum); "" keeps the original 8/26 text.
    Requires init_mission; flight tabs need not have been built first."""
    _ensure_stats()
    SEGS = []
    if GENERIC:
        for fl in sorted(FLIGHTS):
            f0, f1 = FLIGHTS[fl]
            SEGS.append((f"F{fl} takeoff", f"{ps(f0)}", "flight window start", "—"))
            for k in sorted(kk for kk in RUNS if kk[0] == fl):
                r0, r1 = RUNS[k]
                SEGS.append((f"F{fl} Lap {k[1]}", f"{ps(r0)}–{ps(r1)}",
                             "lap window", tracks_in(fl, RUNS[k])))
    else:
        for fl in sorted(FLIGHTS):                     # legacy 8/26 mission: flights 1, 2
            f0, f1 = FLIGHTS[fl]
            apex = {1: ("08:38:54", "08:46:01"), 2: ("09:01:46", "09:09:20")}[fl]
            SEGS.append((f"F{fl} takeoff", f"{ps(f0)}", "truth departs pad (E~1850, N~250)", "—"))
            SEGS.append((f"F{fl} Lap 1", f"{ps(RUNS[(fl,1)][0])}–{ps(RUNS[(fl,1)][1])}",
                         f"outbound to 3.93 km (apex {apex[0]}), inbound to 0.95 km",
                         tracks_in(fl, RUNS[(fl, 1)])))
            SEGS.append((f"F{fl} Lap 2", f"{ps(RUNS[(fl,2)][0])}–{ps(RUNS[(fl,2)][1])}",
                         f"outbound to 3.91 km (apex {apex[1]}), inbound "
                         "(head-on seeker-basket leg)", tracks_in(fl, RUNS[(fl, 2)])))
            SEGS.append((f"F{fl} landing", f"{ps(LANDING[fl][0])}–{ps(LANDING[fl][1])}",
                         "pattern work / approach / touchdown — EXCLUDED from metrics & plots",
                         tracks_in(fl, LANDING[fl])))
    seg_rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td style='text-align:left'>{c}</td>"
                       f"<td style='text-align:left'>{d}</td></tr>" for a, b, c, d in SEGS)

    drops = []
    for fl in sorted(FLIGHTS):
        for tid in FLIGHT_TRACKS[fl]:
            r = S1["tracks"].get(str(tid))
            if r and r.get("drop") and r["meas"] > 10:
                d = r["drop"]
                drops.append((fl, tid, r["last_meas"], d[0], to_agl(d[1]), d[2]))
    drop_rows = "".join(
        f"<tr><td>F{fl}</td><td>{tid}"
        + (" <i>(landing phase)</i>" if t >= LANDING[fl][0] - 30 else "")
        + f"</td><td>{ps(t)}</td><td>{rng:.0f}</td>"
        f"<td>{alt:.0f}</td><td>{rd:+.0f}</td></tr>"
        for fl, tid, t, rng, alt, rd in drops)
    srows = "".join(
        f"<tr><td>F{fl} Lap {rn}</td><td>{ps(w[0])}–{ps(w[1])}</td>"
        f"<td>{allstats[(fl,rn)]['horiz_med']:.0f} / {allstats[(fl,rn)]['horiz_p95']:.0f}</td>"
        f"<td>{allstats[(fl,rn)]['e3d_med']:.0f} / {allstats[(fl,rn)]['e3d_p95']:.0f}</td>"
        f"<td>{allstats[(fl,rn)]['meas']}</td>"
        f"<td>{allstats[(fl,rn)]['rate']:.2f}</td><td>{allstats[(fl,rn)]['cov']*100:.0f}%</td>"
        f"<td>{allstats[(fl,rn)]['max_gap']:.0f} @ {ps(allstats[(fl,rn)]['gap_t'])}</td></tr>"
        for (fl, rn), w in RUNS.items())
    sk_line = " · ".join(
        f"F{fl} Lap {rn}: " + "/".join(f"{allstats[(fl, rn, 'seeker')].get(r, 0):.0f}%"
                                   for r in STANDOFFS)
        for (fl, rn) in RUNS if (fl, rn, "seeker") in allstats)
    if GENERIC:
        # prep_from_dump days: header from the mission structure, not the 8/26 text
        fl_txt = " ".join(f"Flight {fl}: {ps(a)}–{ps(b)} ({(b - a) / 60:.1f} min)."
                          for fl, (a, b) in sorted(FLIGHTS.items()))
        _pl = lambda n, w: f"{n} {w}{'s' if n != 1 else ''}"
        intro = (f"<h2>Mission rollup — {DAY[0]:04d}-{DAY[1]:02d}-{DAY[2]:02d}</h2>"
                 f"<p>{_pl(len(FLIGHTS), 'flight')}, {_pl(len(RUNS), 'lap window')}. {fl_txt}</p>")
    else:
        # legacy 8/26 mission text, byte-for-byte as served (leading newline included)
        intro = """
<h2>Mission rollup — 2026-08-26, MRU91 run Turquoise_Emu, drone mav14550_1_1</h2>
<p>Two flights, each flying two out-and-back racetrack loops to ~3.9 km (min range ~0.9 km).
Flight 1: 08:35:24–08:52:21 (17.0 min). Flight 2: 08:58:56–09:18:44 (19.8 min).
Landing phases (last ~3–5 min of each flight) are excluded from lap metrics.</p>"""
    head = intro + f"""
<div class='cap'>Mission structure at a glance (for anyone picking up the data)</div>
<table class="tl"><tr><th>segment</th><th>window ({TZ_ABBR})</th><th>profile</th>
<th>tracks with measurements in window</th></tr>{seg_rows}</table>
<p><b>All errors in this report are RAW radar output — no bias removal anywhere.</b>
The measured azimuth bias of <b>{BIAS:+.2f}°</b> (consistent at track and raw-observation
level) is INCLUDED in every error figure and statistic. Truth is converted from the MAVLink
feed to the radar's WGS-84 ENU frame by rigorous geodesy (lat/lon → ENU; altitude feet-MSL →
metres, ellipsoidal); no altitude offset is applied. The resulting track-vs-truth vertical
error is <b>{DU_residual:+.0f} m</b> median (the radar reads ~{abs(DU_residual):.0f} m high).</p>
<table class="tl"><tr><th>Lap</th><th>window ({TZ_ABBR})</th><th>horiz err med/p95 (m)</th>
<th>3D ENU med/p95 (m)</th><th>measurements</th><th>meas rate (Hz)</th><th>tracked coverage
(meas ≤2 s old)</th><th>longest mid-lap gap (s @ time)</th></tr>{srows}</table>
<div class='cap'>Seeker-view geometry used on the lap tabs</div>
<p>The seeker is placed ahead of the radar TRACK state along the track's velocity vector at
standoffs of 600/450/300/150 m, with its boresight aimed at the track state (i.e. guidance
flew to the cue). Each run tab's quad plot shows the resulting 2D off-boresight angle to the
TRUE (MAVLink) drone position — if the point is inside the dashed 12° circle, the seeker
sees the target. <b>Percent of inbound samples inside 12° FOV
(600/450/300/150 m standoff):</b> {sk_line}.</p>
<div class='cap'>Where tracks stopped getting measurements (sustained tracks only)</div>
<table class="tl"><tr><th>flight</th><th>track</th><th>last meas</th><th>range (m)</th>
<th>alt AGL≈ (m)</th><th>rdot (m/s)</th></tr>{drop_rows}</table>"""
    if not notes_html and GENERIC:
        # a prep day with no narrative yet: placeholder, never the 8/26 findings
        notes_html = ('<p class="cap"><b>Key findings:</b> not yet written — '
                      'fill <code>notes_html</code> in the campaign config.</p>')
    if not notes_html:
        notes_html = f"""<p><b>Key findings:</b></p><ul>
<li>Track continuity is the limiting factor, not accuracy or detection: 10 IDs (F1) and
~9 (F2). Handovers cluster at the racetrack turns (doppler notch), but several major drops
occurred mid-leg at strong closing rates (94 @ 2451 m, −15 m/s; 917 @ 1526 m, −9 m/s)
<b>while detections continued at ~2/s</b> — association/continuity failures in the tracker
(see obs addendum below).</li>
<li><b>Elevation accuracy is good:</b> raw observations are essentially <b>unbiased</b> vs truth
(median ~0.0° F1 / +0.1° F2, σ≈0.7°), and the filtered track sits a consistent <b>~+0.4° high</b>
across all four passes (median +0.34–0.39°, σ≈0.5°) — both well inside <b>±0.5°</b> of the full
WGS-84 antenna→drone geometry, holding across range. Obs and track share the same reference frame
(RX antenna origin, NED, WGS-84 HAE — verified against the tracker's own <code>el_angle_rad</code>),
so the small obs↔track offset is a minor filter-state bias, not a datum/frame error. The drone flew
~90 m AGL; elevation is well observed and is NOT a limiter on this profile.</li>
<li><b>Systematic bias — declared only where it clears a threshold</b> (|median| ≥ 0.5σ, above a
per-axis floor, and same sign in both flights; robust median ± σ over all correlated drone tracks):
<ul>
<li><b>Azimuth: −1.45°</b> — real and strong, and present at BOTH raw-obs (−1.42°) and track (−1.49°)
level, so it is a sensor pointing/yaw bias, not the filter.</li>
<li><b>Elevation / altitude: +0.37° ≈ +20 m, filtered track only</b> — the raw radar obs are unbiased
(el +0.05°, alt +1.7 m); the small high bias is introduced by the track filter state.</li>
<li><b>Range: no consistent bias</b> — track range error changes sign between flights (F1 +24 m, F2 −5 m),
so nothing is declared; raw obs show only a weak −18 m (0.6σ) offset.</li>
</ul></li>
<li><b>The published track state's vertical velocity element is not usable on this build:</b>
x_state positions and horizontal velocities check out against truth (horiz Δv RMS ~6 m/s),
but the 6th state element reads a smooth 100–200 "m/s" — not a physical vertical rate.
Velocity-error figures therefore show horizontal |Δv| only. (Truth-side note: the
AIR_TRAFFIC <code>altitude</code> is feet MSL and <code>vertical_speed</code> is feet/min;
both are converted to metric WGS-84 here.)</li>
<li>Raw per-lap accuracy (all biases included): lap medians
{min(v['horiz_med'] for v in allstats.values() if isinstance(v, dict) and 'horiz_med' in v):.0f}–{max(v['horiz_med'] for v in allstats.values() if isinstance(v, dict) and 'horiz_med' in v):.0f} m horizontal;
the {BIAS:+.2f}° az bias contributes ~{abs(BIAS)*math.pi/180*2500:.0f} m cross-track at 2.5 km.</li>
<li>Field-logged tracks 57 and 73 (flight 1): 57 was NOT the drone (single detection 6.6 km
away — display coincidence); 73 was a marginal 3-detection fragment near climb-out (~320 m).
The actual first track was 58.</li>
<li>Live-flagged "telemetry dropouts" in flight 2 were hover periods (near-identical GPS
positions stripped as stale), not link loss — post-processed truth has no gaps &gt;15 s
in either flight.</li>
</ul>
<h2>Obs-level (DWELL_WITH_OBS) drop-cause addendum</h2>
<ul>
<li><b>Detections on the drone were continuous all flight:</b> ~100–120 matched
observations per minute (≈2/s) in every airborne minute of both flights
(1,798 matched obs F1, 2,153 F2; median SNR ~50 dB).</li>
<li><b>Every mid-leg track drop was therefore an association/continuity failure,
not detection loss</b> — e.g., track 94's drop at 2,451 m (−15 m/s closing) and 917's at
1,526 m happened while the radar was detecting the drone ~2×/second. The tracker lost a
well-detected target; the detection chain is not the limiter on this profile.</li>
<li><b>MDV notch reconfirmed:</b> minimum matched |doppler| 3.7–3.9 m/s across 25k+ obs —
hover/turn apexes remain undetectable by design.</li>
<li><b>Truth was continuous at ~1 Hz throughout</b> (max gap 1.01 s) — confirming the
live "stale" flags were hover artifacts, not telemetry loss.</li>
<li>Data: <code>obs_matched.npz</code> / <code>obs_summary.json</code> in this folder.</li>
</ul>
"""
    return head + "\n" + notes_html

def wrap_page(title, sub, tabs):
    """EXACT page template (CSS + tab JS) from the original. tabs = {label:
    pane_html} in display order (first tab shown). Page references plotly.min.js
    relatively -- write pyo.get_plotlyjs() next to the output file."""
    btns = "".join(f'<button class="tab" onclick="show({i})">{name}</button>'
                   for i, name in enumerate(tabs))
    panes = "".join(f'<div class="pane" id="pane{i}" style="display:{"block" if i==0 else "none"}">'
                    f'{html}</div>' for i, (name, html) in enumerate(tabs.items()))
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><script src="plotly.min.js"></script>
<style>
body{{margin:0;background:{SURFACE};color:{INK};font:15px/1.55 system-ui,sans-serif;padding:1.5rem}}
main{{max-width:1120px;margin:0 auto}}
h1{{font-size:1.4rem;margin:.2rem 0 .6rem}} h2{{font-size:1.15rem}}
.tab{{font:inherit;padding:.4rem 1rem;margin:0 .3rem .8rem 0;cursor:pointer;
border:1px solid {GRID};border-radius:8px;background:#f4f3f0}}
.tab.on{{background:{BLUE};color:#fff;border-color:{BLUE}}}
.cap{{font-weight:600;margin:1.1rem 0 .2rem}}
.row{{display:flex;gap:.6rem;flex-wrap:wrap;align-items:flex-end}}
.col{{flex:1;min-width:430px}}
.col .cap{{min-height:3.4em;display:flex;align-items:flex-end}}
.plot{{border:1px solid {GRID};border-radius:6px;margin:.2rem 0 .8rem;padding:.2rem}}
table.tl{{border-collapse:collapse;font-size:.9rem;margin:.4rem 0 1rem}}
.tl th,.tl td{{border:1px solid {GRID};padding:.25rem .6rem;text-align:right;white-space:nowrap}}
.tl td[style*="text-align:left"]{{white-space:normal}}
.tl th:first-child,.tl td:first-child{{text-align:left}}
.tl th{{background:#f4f3f0}}
</style></head><body><main>
<h1>{sub}</h1>
<div>{btns}</div>
{panes}
<script>
function show(i){{
 document.querySelectorAll(".pane").forEach((p,j)=>p.style.display=(i===j)?"block":"none");
 document.querySelectorAll(".tab").forEach((b,j)=>b.classList.toggle("on",i===j));
 window.dispatchEvent(new Event("resize"));
}}
show(0);
</script></main></body></html>"""
    return page

def main(argv=None):
    import argparse
    import os
    ap = argparse.ArgumentParser(
        description="Build the 8/26-style tracking mission report (Summary + one tab per run).")
    ap.add_argument("--mdir", required=True,
                    help="mission data dir (truthv.npz, fast2_F1/F2.npz, analysis_stage1.json)")
    ap.add_argument("--out", required=True, help="output report .html path")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="page <title>")
    ap.add_argument("--sub", default=DEFAULT_SUB, help="page <h1> heading")
    ap.add_argument("--day", default="2026-08-26", help="mission date YYYY-MM-DD")
    ap.add_argument("--ant", default=None, help="antenna lat,lon,hae (default MRU91)")
    ap.add_argument("--geoid-n", type=float, default=DEFAULT_GEOID_N)
    a = ap.parse_args(argv)
    day = tuple(int(x) for x in a.day.split("-"))
    ant = tuple(float(x) for x in a.ant.split(",")) if a.ant else DEFAULT_ANT
    init_mission(a.mdir, day=day, ant=ant, geoid_n=a.geoid_n)
    tabs = {}
    for (fl, rn) in RUNS:
        tabs[f"F{fl} Lap {rn}"] = build_flight_tab((fl, rn))
    tabs = {"Summary": build_summary_tab(), **tabs}
    out_path = os.path.abspath(a.out)
    with open(os.path.join(os.path.dirname(out_path), "plotly.min.js"), "w") as f:
        f.write(pyo.get_plotlyjs())
    page = wrap_page(a.title, a.sub, tabs)
    with open(out_path, "w") as f:
        f.write(page)
    print("report written:", out_path)
    print("run stats:", json.dumps({f"F{k[0]}R{k[1]}": v for k, v in allstats.items()
                                    if isinstance(k, tuple) and isinstance(k[1], int)}, indent=1))


if __name__ == "__main__":
    main()


# ---------------- data prep: build a mission dir from an archiver dump ----------------
def prep_from_dump(dump_dir, out_mdir, target_pattern, flights, ant_hae_m,
                   match_gate_m=350.0, fresh_s=1.5):
    """Synthesize the tracking-mission inputs (truthv.npz, fast2_FN.npz,
    analysis_stage1.json) from an archiver dump (mavlink/*.csv + tracks/*.csv),
    so ANY day can run through this module — not only days whose bundles were
    saved live. obs is written empty (0,6) when raw detections were not archived
    (obs-based panels simply show no observation layer).

    flights: [(t0_epoch, t1_epoch), ...]  (one fast2_FN per entry, N=1..)
    ant_hae_m: the antenna HAE constant used by init_mission's truth conversion
               (truthv U column convention: alt_ft_wire - ant_hae_m).
    """
    import glob as _g, os as _os, json as _json
    import pandas as _pd
    _os.makedirs(out_mdir, exist_ok=True)
    cands = _g.glob(f"{dump_dir}/mavlink/{target_pattern}")
    if not cands:
        have = ", ".join(sorted(_os.path.basename(p) for p in _g.glob(f"{dump_dir}/mavlink/*.csv")))
        raise FileNotFoundError(f"prep_from_dump: no mavlink csv matches {target_pattern!r} in "
                                f"{dump_dir}/mavlink/ (present: {have or 'none'})")
    tf = max(cands, key=_os.path.getsize)
    if len(cands) > 1:
        print(f"  [prep] {len(cands)} truth files match {target_pattern!r}; using the largest: "
              f"{_os.path.basename(tf)}")
    df = _pd.read_csv(tf)                          # columns by NAME (both dump layouts)
    if "validposition" in df: df = df[df["validposition"] != 0]
    df = df.sort_values("t_epoch").drop_duplicates("t_epoch")
    n = len(df)
    col = lambda name: (df[name].to_numpy(float) if name in df.columns else np.zeros(n))
    vel_n, vel_e = col("vel_n_mps"), col("vel_e_mps")
    truth = np.column_stack([
        df["t_epoch"].to_numpy(float), df["E_m"].to_numpy(float), df["N_m"].to_numpy(float),
        df["alt_ft_wire"].to_numpy(float) - ant_hae_m,   # init_mission: (U + ant_hae) ft -> m HAE
        # quickdump layout has no speed_mps: derive it (the moving gate needs it)
        (df["speed_mps"].to_numpy(float) if "speed_mps" in df.columns
         else np.hypot(vel_n, vel_e)),
        vel_n, vel_e, col("vert_spd_wire_ftmin"),
        # geodetic position (deg) so init_mission can rebuild E/N/U with the EXACT
        # WGS-84 corr_lib.EnuFrame instead of the legacy equirectangular inverse
        df["lat"].to_numpy(float), df["lon"].to_numpy(float)])
    np.savez_compressed(f"{out_mdir}/truthv.npz", truth=truth)

    cols = ["t_epoch", "E_m", "N_m", "U_m", "vE_mps", "vN_mps", "vU_mps",
            "sigE_m", "sigN_m", "sigU_m", "total_associations", "track_state",
            "last_update_t"]
    tstats = {}
    for fi, (t0, t1) in enumerate(flights, start=1):
        arrs = {"obs": np.zeros((0, 6))}
        for fp in _g.glob(f"{dump_dir}/tracks/track_*.csv"):
            if _os.path.getsize(fp) < 1200: continue
            td = _pd.read_csv(fp, usecols=cols)       # extra columns of newer dumps ignored
            td = td[(td["t_epoch"] >= t0) & (td["t_epoch"] <= t1)]
            if len(td) < 8: continue
            tid = _os.path.basename(fp)[6:-4]
            A = td[cols].to_numpy()
            arrs[f"trk_{tid}"] = A
            dT = np.hypot(A[:, 1] - np.interp(A[:, 0], truth[:, 0], truth[:, 1]),
                          A[:, 2] - np.interp(A[:, 0], truth[:, 0], truth[:, 2]))
            fresh = (A[:, 0] - A[:, 12]) <= fresh_s
            tstats[tid] = dict(
                n=int(len(A)), matched=int((dT < match_gate_m).sum()),
                med=float(np.median(dT)), t0=float(A[0, 0]), t1=float(A[-1, 0]),
                meas=int(fresh.sum()),
                first_meas=float(A[fresh][0, 0]) if fresh.any() else float(A[0, 0]),
                last_meas=float(A[fresh][-1, 0]) if fresh.any() else float(A[-1, 0]),
                coasts=[], init=[float(A[0, 1]), float(A[0, 2])],
                drop=[float(A[-1, 1]), float(A[-1, 2]), float(A[-1, 3])])
        np.savez_compressed(f"{out_mdir}/fast2_F{fi}.npz", **arrs)
    _json.dump(dict(flights=[[float(a), float(b)] for a, b in flights],
                    gaps=[], tracks=tstats),
               open(f"{out_mdir}/analysis_stage1.json", "w"))
    return out_mdir
