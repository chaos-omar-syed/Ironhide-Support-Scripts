#!/usr/bin/env python
"""Modular ENGAGEMENT tab — the EXACT 8/27 intercept-report format.

This is the tab-2 recipe of today_report_build.py lifted verbatim into a reusable
module: same eng_plots figures (top-down, animated 3-D gif, guidance geometry,
closing distance), same state table, same page template. Any post-process report
imports this and wraps the tabs; only the narrative text is meant to differ.

API
---
  build_engagement_tab(cfg, outdir) -> tab_html      (writes cfg['gif_name'] to outdir)
  wrap_tabs(title, sub, tabs) -> page_html           (exact 8/27 template)
  write_page(path, title, sub, tabs)

cfg keys
  name          h3 heading, e.g. "Flight 1 — best pass (#3, 53 m)"
  interceptor   (N,>=4) t,E,N,U interceptor truth (merged + scrubbed)
  target        (N,>=4) target truth
  track         (N,>=4) target radar track
  track_id      display id for the track
  trkV          optional 13-col t,E,N,U,vE,vN,vU,sigE,sigN,sigU,assoc,state,lu
                (quickdump track csv minus time_pdt) -> state-at-CPA table
  cpa_seed      approximate CPA epoch; true CPA refined via eng_plots.cpa in ±20 s
  (CPA plot window is the BINDING standard t-10 .. t+5; not configurable)
  gif_name      output gif filename (relative, embedded as <img class="gif">)
  summary_html  optional paragraph; may use {cpa_truth} {cpa_track} {track_id} keys
  pre_sections    [(h4_heading, html), ...] inserted after the summary (high-level
                  flight overview / CPA timing go here)
  extra_sections  [(h4_heading, html), ...] appended after the canonical sections
  interceptor_label / target_label   legend ids ("14551" / "14550" defaults; the
                  pipeline passes the ids of the truth files actually loaded)
  fov           optional FOV-player block; any failure inside it (no ffmpeg, no
                  ulog/video) replaces ONLY that section with an 'unavailable' note

Timezone: every clock string / label uses the module TZ (zoneinfo, DST-aware;
set_tz("<IANA name>"), default America/Los_Angeles). epoch_pdt() is the tz-aware
parser (name kept for compatibility); tz_abbr(t) gives 'PDT'/'PST'/... for labels.
"""
import os, sys, math
import numpy as np
import plotly.offline as pyo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eng_plots as E
import corr_lib as _C

MIN_PTS = 4   # refuse to render an engagement tab on fewer interceptor samples


def div(fig):
    return pyo.plot(fig, include_plotlyjs=False, output_type="div",
                    config={"responsive": True})


def gifblock(fn, cap):
    # verbatim from today_report_build.py
    return f'<div class="center"><img class="gif" src="{fn}" alt="animated 3-D"></div><p class="cap center">{cap}</p>'


def state_table(trkV, t_cpa, truth=None, label="", when="CPA"):
    # verbatim from today_report_build.py
    if trkV is None or len(trkV) == 0: return ""
    trkV = np.asarray(trkV, float)
    r = trkV[int(np.argmin(np.abs(trkV[:, 0] - t_cpa)))]
    hspd = math.hypot(r[4], r[5]); hdg = (math.degrees(math.atan2(r[4], r[5])) + 360) % 360
    hsig = math.hypot(r[7], r[8]); dts = np.diff(np.unique(trkV[:, 0]))
    rate = (1.0 / np.median(dts)) if len(dts) else float("nan")
    ps = hms_local                                    # campaign tz (set_tz)
    rows = [(f"time ({tz_abbr(r[0])})", ps(r[0])), ("position E, N, Up (m)", "%.0f, %.0f, %.0f" % (r[1], r[2], r[3])),
            ("horizontal speed (m/s)", "%.1f" % hspd), ("vertical speed (m/s)", "%+.1f" % r[6]),
            ("heading (° from N)", "%.0f" % hdg), ("position 1σ  horiz / vert (m)", "%.1f / %.1f" % (hsig, r[9])),
            ("associations", "%d" % int(r[10])), ("track_state code", "%d" % int(r[11])),
            ("update rate (Hz)", "%.1f" % rate)]
    if truth is not None and len(truth):
        tr = np.asarray(truth, float)[int(np.argmin(np.abs(np.asarray(truth, float)[:, 0] - t_cpa)))]
        rows.append(("target truth E, N, Up (m)", "%.0f, %.0f, %.0f" % (tr[1], tr[2], tr[3])))
    body = "".join(f"<tr><td>{a}</td><td><b>{b}</b></td></tr>" for a, b in rows)
    return f'<table class="st"><caption>Track state at {when} — {label}</caption>{body}</table>'


def build_overview_section(cfg, outdir, tc):
    """COMMON overview at the top of every engagement tab — the 8/28 story
    figure, one code path for all: (A) ground-track map over the full
    engagement window with every close pass starred and labeled in metres
    (<75 m only — real engagement attempts; gold star = best), (B)
    interceptor↔target distance vs time with the pass dots ON the curve,
    y-axis capped for CPA readability. No radar marker (report rule)."""
    import datetime as _dtt
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    inter = np.asarray(cfg["interceptor"], float)[:, :4]
    targ = np.asarray(cfg["target"], float)[:, :4]
    if len(inter) < 4 or len(targ) < 4:
        print("  [overview] too few truth samples — overview skipped")
        return ""
    m = (targ[:, 0] >= inter[0, 0]) & (targ[:, 0] <= inter[-1, 0])
    if m.sum() < 4:
        print("  [overview] no truth overlap — overview skipped")
        return ""
    ts = targ[m, 0]
    sep = np.sqrt((targ[m, 1] - np.interp(ts, inter[:, 0], inter[:, 1])) ** 2
                  + (targ[m, 2] - np.interp(ts, inter[:, 0], inter[:, 2])) ** 2
                  + (targ[m, 3] - np.interp(ts, inter[:, 0], inter[:, 3])) ** 2)
    # passes = local minima of separation < 75 m, >= 20 s apart (keep smallest),
    # gated REALISTIC: both craft moving >=3 m/s and closing >=8 m/s into the
    # minimum — kills the pre-takeoff pad cluster and slow formation segments
    vt = np.hypot(np.gradient(targ[m, 1], ts), np.gradient(targ[m, 2], ts))
    vi = np.hypot(np.gradient(np.interp(ts, inter[:, 0], inter[:, 1]), ts),
                  np.gradient(np.interp(ts, inter[:, 0], inter[:, 2]), ts))

    def _closing(j):
        k = int(np.searchsorted(ts, ts[j] - 4.0))
        return (sep[k] - sep[j]) / max(ts[j] - ts[k], 0.5) if k < j else 0.0

    cand = [j for j in range(1, len(ts) - 1)
            if sep[j] < 75.0 and sep[j] <= sep[j - 1] and sep[j] <= sep[j + 1]
            and vt[j] >= 3.0 and vi[j] >= 3.0 and _closing(j) >= 8.0]
    cand.sort(key=lambda j: sep[j])
    picked = []
    for j in cand:
        if all(abs(ts[j] - ts[k]) >= 20.0 for k in picked):
            picked.append(j)
    # refine each pass with the standard CPA routine — the sampled minimum
    # under-reads fast crossings (40 m/s closing x 1 s sampling ≈ 40 m error)
    passes = []
    for j in sorted(picked, key=lambda j: ts[j]):
        c = E.cpa(E._win(inter, ts[j] - 10.0, ts[j] + 10.0), targ)
        passes.append((c[0], c[1]) if c[0] is not None else (float(ts[j]), float(sep[j])))
    best = min(range(len(passes)), key=lambda i: passes[i][1]) if passes else None

    S1, S2, S3, TERM = "#2a78d6", "#eb6834", "#1baf7a", "#e34948"
    SURF, INK, INK2, MUTED, GRIDC, BASE = ("#fcfcfb", "#0b0b0b", "#52514e",
                                           "#898781", "#98a0ac", "#7f8791")
    PDTZ = TZ                                          # campaign tz (set_tz)
    pdt = lambda s: _dtt.datetime.fromtimestamp(s, PDTZ)
    rc = {"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
          "axes.edgecolor": BASE, "axes.labelcolor": INK2, "axes.grid": True,
          "grid.color": GRIDC, "grid.linewidth": 1.1, "xtick.color": MUTED,
          "ytick.color": MUTED, "text.color": INK, "font.size": 11.5,
          "axes.titlesize": 12.5, "axes.titleweight": "600",
          "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False}
    with plt.rc_context(rc):
        fig = plt.figure(figsize=(15.5, 6.2))
        gs = fig.add_gridspec(1, 2, width_ratios=[1.45, 1.0], wspace=0.2,
                              left=0.06, right=0.975, top=0.92, bottom=0.12)
        ax = fig.add_subplot(gs[0, 0])
        ax.plot(targ[m, 1], targ[m, 2], color=S2, lw=1.8, label="target truth")
        ax.plot(inter[:, 1], inter[:, 2], color=S1, lw=1.8, label="interceptor truth")
        for i, (tp, dp) in enumerate(passes):
            isbest = i == best
            ex_, nx_ = (float(np.interp(tp, targ[m, 0], targ[m, 1])),
                        float(np.interp(tp, targ[m, 0], targ[m, 2])))
            ax.plot(ex_, nx_, marker="*", ms=16 if isbest else 11,
                    color="#eda100" if isbest else TERM, mec=INK, mew=0.8,
                    ls="none", zorder=7)
            ax.annotate(f"CPA {dp:.0f} m", (ex_, nx_),
                        textcoords="offset points", xytext=(8, 7), fontsize=8.5,
                        fontweight="700", color=INK, zorder=8,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=BASE,
                                  alpha=0.85), annotation_clip=False)
        if passes:
            ax.plot([], [], marker="*", ms=11, color=TERM, mec=INK, ls="none",
                    label="CPA <75 m (gold = best)")
        ax.set_aspect("equal"); ax.margins(0.10); ax.tick_params(labelsize=9.5)
        ax.set_xlabel("E, m"); ax.set_ylabel("N, m")
        leg = ax.legend(loc="upper left", fontsize=9.5, markerscale=2, frameon=True,
                        facecolor="white", edgecolor=BASE, framealpha=0.95)
        leg.set_zorder(20)
        ax.set_title("Ground tracks — full engagement window")

        ax = fig.add_subplot(gs[0, 1])
        ax.plot([pdt(x) for x in ts], sep, color=S1, lw=1.6)
        for tp, dp in passes:
            ysnap = float(np.interp(tp, ts, sep))          # dot sits ON the curve
            ax.plot(pdt(tp), ysnap, "o", ms=8, color=S2, mec=SURF, mew=1.2, zorder=5)
            ax.annotate(f"{dp:.0f} m", (pdt(tp), ysnap),
                        textcoords="offset points", xytext=(0, 10), ha="center",
                        fontsize=8.5, fontweight="700",
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=BASE,
                                  alpha=0.85))
        # log axis: the full 1–4 km profile stays visible while the CPA dips
        # remain the focus (a linear cap just clips the curve into walls)
        ax.set_yscale("log")
        ax.set_ylim(max(12.0, float(np.min(sep)) * 0.7),
                    min(5000.0, float(np.max(sep)) * 1.15))
        # plain-number ticks — bare 10^2/10^3 leaves the sub-100 m region
        # unlabeled and unreadable
        from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator
        lo_, hi_ = ax.get_ylim()
        ax.yaxis.set_major_locator(FixedLocator(
            [t for t in (15, 25, 50, 75, 100, 150, 250, 500, 1000, 2000, 4000)
             if lo_ <= t <= hi_]))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_locator(NullLocator())
        ax.axhline(75.0, color="#7f8791", lw=1.1, ls="--", zorder=1)
        ax.annotate("75 m — engagement-attempt bar", (0.99, 75.0),
                    xycoords=("axes fraction", "data"), ha="right", va="bottom",
                    fontsize=8, color=INK2)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=PDTZ))
        ax.tick_params(labelsize=9.5)
        ax.set_ylabel("interceptor ↔ target distance, m")
        ax.set_title("Interceptor ↔ target distance — dots = pass CPAs")

        os.makedirs(os.path.join(outdir, "figs"), exist_ok=True)
        tag = f'{cfg["track_id"]}_{_dtt.datetime.fromtimestamp(tc, TZ).strftime("%H%M%S")}'
        fn = f"figs/ovw_{tag}.png"
        fig.savefig(os.path.join(outdir, fn), dpi=130)
        plt.close(fig)
    return ('<h4>Overview — ground tracks &amp; CPA timing</h4>'
            f'<div class="center"><img src="{fn}" style="max-width:100%"></div>'
            '<p class="cap">Full engagement window. Stars = passes under 75 m '
            '(real engagement attempts), labeled in metres, gold = best; the '
            'distance panel caps its axis so the CPA dips stay readable.</p>')


def build_engagement_tab(cfg, outdir):
    inter_all = np.asarray(cfg["interceptor"], float)[:, :4]
    targ = np.asarray(cfg["target"], float)[:, :4]
    trk = np.asarray(cfg["track"], float)[:, :4]
    tid = str(cfg["track_id"])
    pre, post = 10.0, 5.0   # BINDING standard CPA window (t-10 .. t+5)
    seed = float(cfg["cpa_seed"])

    inter_seedwin = E.drop_frozen(E._win(inter_all, seed - 20, seed + 20))
    c_tru = E.cpa(inter_seedwin, targ)
    if c_tru[0] is None:
        raise RuntimeError(f"{cfg['name']}: no valid CPA near seed (interceptor pts "
                           f"in seed window: {len(inter_seedwin)})")
    tc = c_tru[0]
    W = (tc - pre, tc + post)
    inter = E.drop_frozen(E._win(inter_all, *W))
    c_trk = E.cpa(inter_seedwin, trk)
    if c_trk[0] is None:                      # late-born track: search +-120 s for the
        c_trk = E.cpa(E.drop_frozen(E._win(inter_all, tc - 120, tc + 120)), trk)
    n_i, n_t, n_k = len(inter), len(E._win(targ, *W)), len(E._win(trk, *W))
    print(f"[{cfg['name']}] tc={tc:.1f} inter={n_i} targ={n_t} trk{tid}={n_k} "
          f"cpa_truth={c_tru[1]:.0f}m cpa_trk={c_trk[1] if c_trk[0] else float('nan'):.0f}m")
    if n_i < MIN_PTS or n_t < MIN_PTS:
        raise RuntimeError(f"{cfg['name']}: too few samples in CPA window "
                           f"(inter {n_i}, targ {n_t}) — check inputs")

    # legend ids come from the truth files actually loaded (postprocess_report
    # passes interceptor_label / target_label); defaults = the Seawall ports
    ilab = cfg.get("interceptor_label") or "14551"
    tlab = cfg.get("target_label") or "14550"
    objs = [dict(name=f"interceptor {ilab} (truth)", color=E.INTER, arr=inter, leader=True),
            dict(name=f"target {tlab} — TRUTH", color=E.TRUTH,
                 arr=E._win(E.declutter(targ), *W), leader=True),
            dict(name=f"target {tlab} — radar TRACK {tid}", color=E.TRACK,
                 arr=E._win(trk, *W), leader=True)]
    gif = cfg.get("gif_name", "eng_3d.gif")
    E.gif3d(objs, c_tru, os.path.join(outdir, gif), f"{cfg['name']} — 3-D", tc,
            nframes=int(round((pre + post) * 9)), fps=9)   # 1x real-time playback
    td = div(E.topdown(objs, c_tru, f"{cfg['name']} — top-down (approach → CPA)", t0=tc))
    series = [dict(name="to TRUTH", color=E.TRUTH, inter=inter, targ=E._win(targ, *W)),
              dict(name="to TRACK", color=E.TRACK, inter=inter, targ=E._win(trk, *W))]
    cl = div(E.closing([dict(name="interceptor ↔ target TRUTH", color=E.TRUTH,
                             inter=inter, targ=E._win(targ, *W)),
                        dict(name=f"interceptor ↔ target TRACK {tid}", color=E.TRACK,
                             inter=inter, targ=E._win(trk, *W))], "time from CPA (s)", tc))
    cs = div(E.closing_speed_fig(series, "time from CPA (s)", tc))
    hd = div(E.heading_fig(series, "time from CPA (s)", tc))
    st = state_table(cfg.get("trkV"), tc, truth=targ,
                     label=f"radar track {tid} (target)", when="CPA")
    ew = ""
    if cfg.get("trkV") is not None:
        f_ew = E.err_window_fig(cfg["trkV"], targ, tc,
                                pre=cfg.get("err_pre", 12.0), post=cfg.get("err_post", 12.0),
                                title="Track metrics in this window — az/el/range/alt error, "
                                      "filter ±1σ (shaded) / ±3σ (dotted)")
        if f_ew is not None:
            ew = "<h4>Track metrics in this window</h4>" + div(f_ew)

    summary = cfg.get("summary_html",
        "<p>Interceptor closest approach: <b>{cpa_truth:.0f} m to target truth</b>, "
        "<b>{cpa_track:.0f} m to radar track {track_id}</b>. Data after the CPA is cut.</p>")
    summary = summary.format(cpa_truth=c_tru[1],
                             cpa_track=(c_trk[1] if c_trk[0] is not None else float("nan")),
                             track_id=tid)
    # canonical order: summary -> OVERVIEW (top-down + CPA timeline) -> FOV
    # player (when flight-log + IR data exist) -> pre_sections -> 3-D -> 2-D
    # -> closing distance -> closing speed -> heading error -> state -> extras
    ovw = build_overview_section(cfg, outdir, tc)
    fov_html = ""
    if cfg.get("fov"):
        # the FOV block is OPTIONAL: any failure inside it (missing ffmpeg, broken
        # video index, pyulog schema, ...) skips ONLY this block, never the tab
        try:
            fov_html = build_fov_section(cfg["fov"], outdir, tc=tc, target=targ,
                                         ant=cfg.get("ant"), trkV=cfg.get("trkV"),
                                         name=cfg["name"])
        except Exception as e:
            print(f"  [fov] {cfg['name']}: FOV section failed ({e!r}) — section skipped, tab continues")
            fov_html = fov_unavailable_html(f"render failed: {e}")
    html = f"<h3>{cfg['name']}</h3>" + summary + ovw + fov_html
    for heading, extra in cfg.get("pre_sections", []):
        html += f"<h4>{heading}</h4>" + extra
    html += ("<h4>Best pass — 3-D</h4>"
             + gifblock(gif, "Animated 3-D — velocity arrows; heading-error readout shows "
                             "if we were pointed at the target.")
             + "<h4>Best pass — top-down (2-D)</h4>" + td
             + "<h4>Closing distance</h4>" + cl
             + "<h4>Closing speed</h4>" + cs
             + "<h4>Heading error vs line-of-sight</h4>" + hd
             + ew + st)
    for heading, extra in cfg.get("extra_sections", []):
        html += f"<h4>{heading}</h4>" + extra
    return html


def _fov_runs(grid, mask, merge_s=0.7, min_s=0.4):
    """Contiguous True-runs of mask on the time grid -> [(t_start, t_end)].
    Exact semantics of the standard FOV tools: a run ends at the first False
    sample; blips separated <merge_s are merged; runs <min_s dropped."""
    ev, on = [], None
    for j in range(len(grid)):
        if mask[j] and on is None:
            on = grid[j]
        if not mask[j] and on is not None:
            ev.append((on, grid[j]))
            on = None
    if on is not None:
        ev.append((on, grid[-1]))
    merged = []
    for a, b in ev:
        if merged and a - merged[-1][1] < merge_s:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_s]


def _ir_extract(video, idxs, outdir, tag):
    """Sequential-decode IR frame grab (these MKVs have broken indexes — never
    seek). Frames are cached in figs/ keyed on frame index, so rebuilds with
    unchanged timing are free. Returns the HTML cells for the frame strip."""
    import cv2
    import datetime as _dtt
    os.makedirs(os.path.join(outdir, "figs"), exist_ok=True)
    todo = sorted({fi for fi, *_ in idxs
                   if not os.path.exists(os.path.join(outdir, f"figs/fov_{tag}_{fi}.jpg"))})
    if todo:
        cap = cv2.VideoCapture(video)
        cur, last, want = -1, max(todo), set(todo)
        while cur < last:
            if not cap.grab():
                break
            cur += 1
            if cur in want:
                ok, im = cap.retrieve()
                if ok:
                    h, w = im.shape[:2]
                    im = cv2.resize(im, (400, int(h * 400 / w)))
                    cv2.imwrite(os.path.join(outdir, f"figs/fov_{tag}_{cur}.jpg"), im,
                                [cv2.IMWRITE_JPEG_QUALITY, 82])
        cap.release()
    cells = []
    for fi, r_, t_, a_ in idxs:
        p = f"figs/fov_{tag}_{fi}.jpg"
        if os.path.exists(os.path.join(outdir, p)):
            ts = _dtt.datetime.fromtimestamp(t_, TZ).strftime("%H:%M:%S")
            cells.append(
                f'<span style="display:inline-block;margin:4px;text-align:center">'
                f'<img src="{p}" width="400"><br><span class="cap">{ts} {tz_abbr(t_)} · '
                f'rng {r_:.0f} m · {a_:.1f}° off boresight</span></span>')
    return cells


FOV_CONE_HALF_DEG, FOV_CONE_LEN = 6.0, 500.0   # 12° full cone, ≤500 m — standard
FOV_GRID_HZ = 5.0    # the event merge/drop rules (0.7 s / 0.4 s) are tuned to this rate
FFMPEG_FALLBACK = "/home/omar.syed/.local/share/mamba/envs/sensorenv/bin/ffmpeg"   # legacy path


def ffmpeg_bin():
    """The ffmpeg executable the FOV render will use, or None. Order: env
    SEAWALL_FFMPEG (an explicit path — a non-executable value DISABLES ffmpeg, handy
    for testing the skip path), PATH, the running interpreter's bin/ (the
    sensorenv env), the legacy absolute fallback."""
    import shutil
    env = os.environ.get("SEAWALL_FFMPEG")
    if env is not None:
        return env if (os.path.isfile(env) and os.access(env, os.X_OK)) else None
    for cand in (shutil.which("ffmpeg"),
                 os.path.join(os.path.dirname(sys.executable), "ffmpeg"),
                 FFMPEG_FALLBACK):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def fov_unavailable_html(reason):
    """The FOV block's stand-in when it cannot be built (the tab itself is fine)."""
    return ("<h4>Seeker FOV player — flight log + IR</h4>"
            f'<p class="cap">Seeker FOV section unavailable for this build: {reason}.</p>')


def _ulog_series(ulog_path, zoff, ant):
    """Parse the interceptor flight log into antenna-frame ENU series.
    ONE parser for every engagement: handles both PX4 GPS schemas
    (latitude_deg/altitude_ellipsoid_m and legacy lat·1e-7/alt_ellipsoid mm),
    discovers the UTC anchor key dynamically, applies the per-log clock offset
    (zoff, measured by position xcorr vs truth — config data). ulog_path may be
    a glob (SMB-mangled filenames); largest match wins (skips boot stubs)."""
    from pyulog import ULog
    import glob as _g
    fs = _g.glob(ulog_path)
    if not fs:
        raise FileNotFoundError(ulog_path)
    fs.sort(key=os.path.getsize)
    u = ULog(fs[-1], ['vehicle_status', 'vehicle_gps_position',
                      'vehicle_local_position', 'vehicle_attitude'],
             disable_str_exceptions=True)
    d = {x.name: x.data for x in u.data_list}
    g = d['vehicle_gps_position']
    utck = [k for k in g if 'utc' in k][0]
    nz = np.nonzero(g[utck])[0]
    off = (g[utck][nz[0]] - g['timestamp'][nz[0]]) / 1e6 + zoff
    if 'latitude_deg' in g:
        la, lo, hae = g['latitude_deg'], g['longitude_deg'], g['altitude_ellipsoid_m']
    else:                                     # legacy schema
        la, lo, hae = g['lat'] / 1e7, g['lon'] / 1e7, g['alt_ellipsoid'] / 1e3
    _e, _n, _u = _C.EnuFrame(ant).enu(np.asarray(la, float), np.asarray(lo, float), np.asarray(hae, float))   # exact WGS-84 ENU (was 111 320 m/deg)
    gps = dict(t=g['timestamp'] / 1e6 + off, E=_e, N=_n, U=_u)
    at = d['vehicle_attitude']
    qw, qx, qy, qz = at['q[0]'], at['q[1]'], at['q[2]'], at['q[3]']
    # body +X (nose = seeker boresight) rotated to ENU
    fN = 1 - 2 * (qy * qy + qz * qz)
    fEc = 2 * (qx * qy + qw * qz)
    fD = 2 * (qx * qz - qw * qy)
    att = dict(t=at['timestamp'] / 1e6 + off, aE=fEc, aN=fN, aU=-fD)
    vs = d['vehicle_status']
    nav = dict(t=vs['timestamp'] / 1e6 + off, nav=vs['nav_state'])
    lp = d['vehicle_local_position']
    tlp = lp['timestamp'] / 1e6 + off
    vel = dict(t=tlp, vE=lp['vy'], vN=lp['vx'], vU=-lp['vz'])   # NED -> ENU
    # takeoff = first sustained climb (vz < -1.0 m/s for >= 2 s) — camera anchor
    climb = lp['vz'] < -1.0
    takeoff, j = None, 0
    while j < len(climb):
        if climb[j]:
            k = j
            while k + 1 < len(climb) and climb[k + 1]:
                k += 1
            if tlp[k] - tlp[j] >= 2.0:
                takeoff = tlp[j]
                break
            j = k + 1
        else:
            j += 1
    return gps, att, nav, vel, u.start_timestamp / 1e6 + off, u.last_timestamp / 1e6 + off, takeoff


def build_fov_section(fov, outdir, tc=None, target=None, ant=None, trkV=None, name=""):
    """COMMON FOV player block for the top of every engagement tab — the
    standard FOV player (rendered animation + IN-FOV skip controls), built
    LIVE from the raw inputs, identically for every engagement:
        fov = {"ulog":    interceptor .ulg path or glob   (required),
               "zoff":    per-log clock offset in seconds (required),
               "video":   IR mkv                          (required),
               "camwarp": npz of per-frame true capture times, or
               "cam":     {"fps": ..., "onset_frame": ...} — frame0 UTC anchored
                          to the ulog takeoff (or {"fps": ..., "utc0": abs}),
               "target_label": legend name for the truth source}
    target = the SAME truth array the engagement tab plots (t,E,N,U);
    ant    = the day's antenna origin; trkV = the tab's radar track (13-col).
    Returns "" (with a console note) when the flight log or IR video is
    missing, so engagement days without that data build unchanged."""
    import datetime as _dtt

    import glob as _g
    if target is None or ant is None or not fov.get("ulog") or not _g.glob(fov["ulog"]):
        print(f"  [fov] flight log not available ({fov.get('ulog')}) — FOV section skipped")
        return ""
    # ffmpeg is only needed when the mp4 is not already rendered — decide BEFORE
    # parsing the flight log / decoding the IR video so a box without ffmpeg
    # skips the block in milliseconds instead of failing after a 2-minute render
    video = fov.get("video")
    if video:
        mp4_cached = os.path.exists(os.path.join(
            outdir, f"figs/fov_{os.path.splitext(os.path.basename(video))[0]}.mp4"))
        if not mp4_cached and ffmpeg_bin() is None:
            print("  [fov] ffmpeg not found (PATH, interpreter bin/, SEAWALL_FFMPEG) and no "
                  f"cached mp4 — FOV section skipped for {name or os.path.basename(video)}")
            return fov_unavailable_html("ffmpeg not found on this machine and no cached "
                                        "render (install ffmpeg or run under the sensorenv "
                                        "interpreter, then rebuild)")
    try:
        gps, att, nav, vel, t0u, t1u, takeoff = _ulog_series(fov["ulog"], float(fov["zoff"]), ant)
    except Exception as e:
        print(f"  [fov] flight log unreadable ({e}) — FOV section skipped")
        return fov_unavailable_html(f"flight log unreadable ({e})")
    targ = np.asarray(target, float)
    half, clen = FOV_CONE_HALF_DEG, FOV_CONE_LEN
    # grid = ulog span ∩ target-truth span (np.interp extrapolates flat — clip it out)
    g0, g1 = max(t0u + 2.0, float(targ[0, 0])), min(t1u, float(targ[-1, 0]))
    if g1 - g0 < 10.0:
        print(f"  [fov] <10 s of ulog/truth overlap — FOV section skipped")
        return ""
    grid = np.arange(g0, g1, 1.0 / FOV_GRID_HZ)
    iE, iN, iU = (np.interp(grid, gps['t'], gps[k]) for k in ("E", "N", "U"))
    aE, aN, aU = (np.interp(grid, att['t'], att[k]) for k in ("aE", "aN", "aU"))
    an = np.sqrt(aE**2 + aN**2 + aU**2)
    aE, aN, aU = aE / an, aN / an, aU / an
    fE, fN_, fU = (np.interp(grid, targ[:, 0], targ[:, c]) for c in (1, 2, 3))
    rE, rN, rU = fE - iE, fN_ - iN, fU - iU
    rng = np.sqrt(rE**2 + rN**2 + rU**2)
    cos_a = (rE * aE + rN * aN + rU * aU) / np.maximum(rng, 1e-6)
    ang = np.degrees(np.arccos(np.clip(cos_a, -1, 1)))
    infov = (rng <= clen) & (ang <= half)
    ni = np.searchsorted(nav['t'], grid, side='right') - 1
    offb = np.asarray(nav['nav'], float)[np.clip(ni, 0, len(nav['nav']) - 1)] == 14

    vgE, vgN, vgU = (np.interp(grid, vel['t'], vel[k]) for k in ("vE", "vN", "vU"))

    ev = _fov_runs(grid, infov)
    dt_med = float(np.median(np.diff(grid))) if len(grid) > 1 else 0.0
    tot = float(np.count_nonzero(infov) * dt_med)
    min_rin = float(np.min(rng[infov])) if infov.any() else float("nan")
    rin_txt = f"{min_rin:.0f} m" if np.isfinite(min_rin) else "—"
    hmsu = lambda s: _dtt.datetime.fromtimestamp(s, _dtt.timezone.utc).strftime("%H:%M:%S")

    # mode segments (sample-held nav_state), in player-relative seconds
    t_rel = grid - grid[0]
    segs = []
    cur, s0 = bool(offb[0]), 0.0
    for j in range(1, len(grid)):
        if bool(offb[j]) != cur:
            segs.append([s0, float(t_rel[j]), "OFFBOARD" if cur else "ONBOARD"])
            cur, s0 = bool(offb[j]), float(t_rel[j])
    segs.append([s0, float(t_rel[-1]), "OFFBOARD" if cur else "ONBOARD"])

    # camera time model: measured per-frame capture times (camwarp) or uniform
    # fps anchored to the ulog takeoff / an absolute frame-0 UTC — all config data
    video = fov.get("video")
    tcorr = None
    cam = dict(fov.get("cam") or {})
    cw = fov.get("camwarp")
    if cw and os.path.exists(cw):
        tcorr = np.asarray(np.load(cw)["tcorr"], float)
    elif cam.get("utc0") is None and cam.get("onset_frame") is not None \
            and cam.get("fps") and takeoff is not None:
        cam["utc0"] = takeoff - float(cam["onset_frame"]) / float(cam["fps"])
    if not (video and os.path.exists(video)
            and (tcorr is not None or (cam.get("utc0") and cam.get("fps")))):
        print(f"  [fov] IR video / camera model not available ({video}) — FOV player skipped")
        return ""

    tag = os.path.splitext(os.path.basename(video))[0]
    mp4 = f"figs/fov_{tag}.mp4"
    if os.path.exists(os.path.join(outdir, mp4)):
        print(f"  [fov] reusing rendered {mp4} (delete it to force a re-render)")
    else:
        series = dict(grid=grid, iE=iE, iN=iN, iU=iU, vE=vgE, vN=vgN, vU=vgU,
                      aE=aE, aN=aN, aU=aU, fE=fE, fN=fN_, fU=fU,
                      rng=rng, infov=infov, offb=offb)
        _fov_render(series, trkV, video, tcorr, cam, os.path.join(outdir, mp4),
                    name, fov.get("target_label", "target truth"), ant)

    evd = dict(t0=float(grid[0]), t1=float(grid[-1]),
               fox_name=fov.get("target_label", "target truth"),
               in_fov_events=[[round(float(a - grid[0]), 2), round(float(b - grid[0]), 2),
                               hmsu(a), hmsu(b)] for a, b in ev],
               mode_segments=segs)
    import json as _json
    uid = f"fovp_{tag}"
    return (
        "<h4>Seeker FOV player — flight log + IR</h4>"
        f"<p><b>Target in the seeker cone ({2 * half:.0f}° full angle, ≤{clen:.0f} m): "
        f"{tot:.1f} s across {len(ev)} event(s)</b> · min range {np.nanmin(rng):.0f} m · "
        f"min off-boresight {np.nanmin(ang):.2f}° · closest while in FOV {rin_txt}</p>"
        + _FOVP_CSS
        + f'<div class="fovp" id="{uid}"></div>'
        + _FOVP_JS.replace("__ID__", uid).replace("__SRC__", mp4)
                  .replace("__EV__", _json.dumps(evd)))


def _fov_render(S, trkV, video, tcorr, cam, mp4_path, name, fox_label, ant):
    """Render the standard FOV animation for ONE engagement — the exact
    fov_render recipe: left = interceptor + seeker cone cartoon (ground range
    vs altitude, mode flash, IN FOV label), right = time-aligned onboard IR,
    bottom = mode/IN-FOV strip. 5 fps grid -> real-time video. IR mkv indexes
    are broken -> strictly sequential decode. MJPG intermediate -> H.264."""
    import subprocess, shutil
    import datetime as _dtt
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon, Patch
    from matplotlib.lines import Line2D
    import cv2

    grid = S["grid"]
    iE, iN, iU = S["iE"], S["iN"], S["iU"]
    vE, vN, vU = S["vE"], S["vN"], S["vU"]
    aE, aN, aU = S["aE"], S["aN"], S["aU"]
    fE, fN, fU = S["fE"], S["fN"], S["fU"]
    rng, infov, offb = S["rng"], S["infov"], S["offb"]
    HALF, CLEN = FOV_CONE_HALF_DEG, FOV_CONE_LEN

    # radar target track of THIS engagement, sample-held on the grid (<0.6 s)
    kE = np.full(len(grid), np.nan); kN = kE.copy(); kU = kE.copy()
    if trkV is not None and len(trkV):
        A = np.asarray(trkV, float)[:, :4]
        A = A[np.argsort(A[:, 0])]
        idx = np.searchsorted(A[:, 0], grid)
        for j, ii in enumerate(idx):
            for cand in (ii - 1, ii):
                if 0 <= cand < len(A) and abs(A[cand, 0] - grid[j]) < 0.6:
                    kE[j], kN[j], kU[j] = A[cand, 1], A[cand, 2], A[cand, 3]

    GEOID_N = -29.4                       # display only: HAE-relative U -> m MSL
    msl = lambda u: u + ant[2] - GEOID_N
    gr_i = np.hypot(iE, iN); al_i = msl(iU)
    gr_f = np.hypot(fE, fN); al_f = msl(fU)
    gr_k = np.hypot(kE, kN); al_k = msl(kU)
    grdot = (iE * vE + iN * vN) / np.maximum(gr_i, 1)
    a_gr = (iE * aE + iN * aN) / np.maximum(gr_i, 1)

    SURF, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
    GRIDC, BASE = "#e1e0d9", "#c3c2b7"
    S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
    SEQ_DARK, TERM = "#1c5cab", "#e34948"
    OFFB_C, ONB_C = "#1c5cab", "#a04000"
    hms = lambda s: _dtt.datetime.fromtimestamp(s, _dtt.timezone.utc).strftime("%H:%M:%S")

    plt.rcParams.update({"font.size": 11, "axes.edgecolor": BASE, "axes.labelcolor": INK2,
                         "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK})
    fig = plt.figure(figsize=(16, 6.4), dpi=100, facecolor=SURF)
    gsp = fig.add_gridspec(2, 2, width_ratios=[1.35, 1.0], height_ratios=[1, 0.09],
                           left=0.05, right=0.99, top=0.88, bottom=0.07, wspace=0.12, hspace=0.3)
    ax = fig.add_subplot(gsp[0, 0]); ax.set_facecolor(SURF)
    axc = fig.add_subplot(gsp[0, 1]); axc.set_axis_off()
    axt = fig.add_subplot(gsp[1, :])
    fig.suptitle(f"{name} — interceptor + {2*HALF:.0f}° seeker cone vs {fox_label} and "
                 f"VP radar track   (all sources time-aligned, true UTC)",
                 x=0.05, y=0.975, ha="left", fontsize=15, fontweight="700", color=INK)

    xmax = max(np.nanmax(gr_i), np.nanmax(gr_f)) + 400
    ax.set_xlim(0, xmax); ax.set_ylim(80, max(np.nanmax(al_i), np.nanmax(al_f)) + 120)
    ax.grid(color=GRIDC, lw=0.8)
    ax.set_xlabel("ground range from radar, m"); ax.set_ylabel("altitude, m MSL")

    cone_poly = Polygon(np.zeros((3, 2)), closed=True, facecolor=S1, alpha=0.30,
                        edgecolor=SEQ_DARK, lw=1.2, zorder=3)
    ax.add_patch(cone_poly)
    dart = Polygon(np.zeros((3, 2)), closed=True, facecolor=SEQ_DARK, edgecolor=SURF,
                   lw=1.2, zorder=6)
    ax.add_patch(dart)
    vel_ln, = ax.plot([], [], color=INK2, lw=1.4, ls='--', zorder=5)
    fox_m, = ax.plot([], [], 'o', ms=15, color=S2, mec=SURF, mew=2, zorder=6)
    fox_x, = ax.plot([], [], '+', ms=8, color=SURF, mew=1.8, zorder=7)
    trk_m, = ax.plot([], [], 'D', ms=10, mfc='none', mec=S3, mew=2.4, zorder=5)
    fox_lb = ax.annotate("TARGET (truth)", (0, 0), textcoords="offset points", xytext=(12, 8),
                         fontsize=9.5, fontweight="600", color=S2, zorder=8)
    trk_lb = ax.annotate("VP track", (0, 0), textcoords="offset points", xytext=(12, -14),
                         fontsize=9.5, fontweight="600", color=S3, zorder=8)
    int_lb = ax.annotate("INTERCEPTOR", (0, 0), textcoords="offset points", xytext=(-10, 12),
                         ha='right', fontsize=9.5, fontweight="700", color=SEQ_DARK, zorder=8)
    infov_lb = ax.annotate("IN FOV", (0, 0), textcoords="offset points", xytext=(0, 26),
                           ha='center', fontsize=17, fontweight="800", color=TERM, zorder=9)
    infov_lb.set_visible(False)
    mode_tx = ax.annotate("", (0.5, 1.03), xycoords='axes fraction', ha='center',
                          fontsize=24, fontweight="800")
    clock_tx = ax.annotate("", (0.01, 1.04), xycoords='axes fraction', fontsize=12,
                           fontweight="600", color=INK2)
    rng_tx = ax.annotate("", (0.99, 1.04), xycoords='axes fraction', ha='right', fontsize=12,
                         fontweight="600", color=INK2)
    ax.legend(handles=[
        Patch(facecolor=SEQ_DARK, label='interceptor'),
        Line2D([], [], color=S2, ls='none', marker='o', ms=10, label=fox_label),
        Line2D([], [], color=S3, ls='none', marker='D', ms=8, mfc='none', mew=2,
               label='VP radar track of target'),
        Patch(facecolor=S1, alpha=0.3,
              label=f'{2*HALF:.0f}° seeker cone about NOSE (attitude), {CLEN:.0f} m'),
        Patch(facecolor=TERM, alpha=0.45, label='cone when target IN FOV'),
        Line2D([], [], color=INK2, lw=1.4, ls='--', label='velocity direction (vs nose = AoA/crab)'),
    ], loc='upper left', fontsize=9, frameon=False)

    img_h = axc.imshow(np.zeros((512, 640), np.uint8), cmap='gray', vmin=0, vmax=255,
                       aspect='equal')
    axc.set_title("onboard IR (time-aligned)", fontsize=11, color=INK2)

    t_rel = grid - grid[0]
    axt.set_xlim(0, t_rel[-1]); axt.set_ylim(0, 1); axt.set_yticks([])
    axt.tick_params(labelsize=8)
    axt.set_xlabel("seconds from start · blue = OFFBOARD, sand = ONBOARD, red = target IN FOV",
                   fontsize=9)
    j = 0
    while j < len(grid):
        k2 = j
        while k2 + 1 < len(grid) and offb[k2 + 1] == offb[j]:
            k2 += 1
        axt.axvspan(t_rel[j], t_rel[min(k2 + 1, len(grid) - 1)],
                    color="#b7d3f6" if offb[j] else "#f0e4d0", lw=0)
        j = k2 + 1
    for j in range(len(grid)):
        if infov[j]:
            axt.axvspan(t_rel[j] - 0.15, t_rel[j] + 0.15, color=TERM, lw=0)
    playhead = axt.axvline(0, color=INK, lw=2)

    def dart_xy(j):
        xs = ax.get_xlim(); ys = ax.get_ylim()
        sx, sy = (xs[1] - xs[0]) / 40, (ys[1] - ys[0]) / 40
        th = math.atan2(aU[j] / sy if sy else 0, a_gr[j] / sx if sx else 1)
        c, s = math.cos(th), math.sin(th)
        pts = np.array([[1.4, 0], [-0.8, 0.55], [-0.8, -0.55]]) @ np.array([[c, -s], [s, c]]).T
        return np.column_stack([gr_i[j] + pts[:, 0] * sx, al_i[j] + pts[:, 1] * sy])

    def cone_xy(j):
        u = np.array([a_gr[j], aU[j]])
        n = np.hypot(*u)
        if n < 0.05:
            u, n = np.array([1.0, 0.0]), 1.0
        u = u / n
        a0 = math.atan2(u[1], u[0])
        arc = [a0 + math.radians(a) for a in np.linspace(-HALF, HALF, 7)]
        return np.array([(gr_i[j], al_i[j])] +
                        [(gr_i[j] + CLEN * math.cos(a), al_i[j] + CLEN * math.sin(a))
                         for a in arc])

    def vel_xy(j):
        u = np.array([grdot[j], vU[j]])
        n = np.hypot(*u)
        if n < 2:
            return np.array([[gr_i[j], al_i[j]], [gr_i[j], al_i[j]]])
        u = u / n
        return np.array([[gr_i[j], al_i[j]], [gr_i[j] + 250 * u[0], al_i[j] + 250 * u[1]]])

    FPSV = int(round(FOV_GRID_HZ))
    cap = cv2.VideoCapture(video)
    assert cap.isOpened(), video
    cam_n, cam_eof = -1, False
    cam_frame = np.zeros((512, 640), np.uint8)
    tmp_avi = mp4_path + ".tmp.avi"
    out = cv2.VideoWriter(tmp_avi, cv2.VideoWriter_fourcc(*"MJPG"), FPSV, (1600, 640))
    assert out.isOpened()

    for j in range(len(grid)):
        t = grid[j]
        want = (int(np.searchsorted(tcorr, t)) if tcorr is not None
                else int(round((t - float(cam["utc0"])) * float(cam["fps"]))))
        if want >= 0 and not cam_eof and cam_n < want:
            got = False
            while cam_n < want:
                if not cap.grab():
                    cam_eof = True
                    break
                cam_n += 1; got = True
            if got and cam_n == want:
                ok, fr = cap.retrieve()
                if ok:
                    cam_frame = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        img_h.set_data(cam_frame)

        cone_poly.set_xy(cone_xy(j))
        if infov[j]:
            cone_poly.set_facecolor(TERM); cone_poly.set_alpha(0.45); cone_poly.set_edgecolor(TERM)
            infov_lb.set_visible(True); infov_lb.xy = (gr_f[j], al_f[j])
        else:
            cone_poly.set_facecolor(S1); cone_poly.set_alpha(0.30); cone_poly.set_edgecolor(SEQ_DARK)
            infov_lb.set_visible(False)
        dart.set_xy(dart_xy(j))
        vxy = vel_xy(j); vel_ln.set_data(vxy[:, 0], vxy[:, 1])
        fox_m.set_data([gr_f[j]], [al_f[j]]); fox_x.set_data([gr_f[j]], [al_f[j]])
        fox_lb.xy = (gr_f[j], al_f[j]); int_lb.xy = (gr_i[j], al_i[j])
        if np.isfinite(gr_k[j]):
            trk_m.set_data([gr_k[j]], [al_k[j]]); trk_lb.xy = (gr_k[j], al_k[j])
            trk_m.set_visible(True); trk_lb.set_visible(True)
        else:
            trk_m.set_visible(False); trk_lb.set_visible(False)
        mode_tx.set_text("OFFBOARD" if offb[j] else "ONBOARD")
        mode_tx.set_color(OFFB_C if offb[j] else ONB_C)
        mode_tx.set_alpha(1.0 if int(t * 2) % 2 == 0 else 0.25)
        clock_tx.set_text(hms(t) + " UTC")
        rng_tx.set_text(f"interceptor↔target {rng[j]:.0f} m")
        playhead.set_xdata([t_rel[j]])

        fig.canvas.draw()
        buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
        bgr = cv2.cvtColor(buf, cv2.COLOR_RGB2BGR)
        if bgr.shape[:2] != (640, 1600):
            bgr = cv2.resize(bgr, (1600, 640))
        out.write(bgr)
        if j % 600 == 0:
            print(f"  [fov] {os.path.basename(mp4_path)} frame {j}/{len(grid)}", flush=True)
    out.release(); cap.release(); plt.close(fig)

    ffmpeg = ffmpeg_bin()
    if ffmpeg is None:
        raise RuntimeError(f"ffmpeg not found — MJPG intermediate kept at {tmp_avi}")
    r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", tmp_avi,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                        "-pix_fmt", "yuv420p", "-g", str(FPSV * 2),
                        "-movflags", "+faststart", mp4_path])
    if r.returncode == 0:
        os.remove(tmp_avi)
        print(f"  [fov] wrote {os.path.basename(mp4_path)} ({len(grid)} frames @{FPSV} fps real-time)")
    else:
        raise RuntimeError(f"ffmpeg transcode failed rc={r.returncode} ({tmp_avi} kept)")


# the standard FOV player block (verbatim port of the fov_player controls),
# scoped under .fovp so several instances coexist on one tabbed page
_FOVP_CSS = """<style>
.fovp { background:#fcfcfb; border:1px solid #e1e0d9; border-radius:8px; padding:12px; margin:14px 0 20px; }
.fovp video { display:block; width:100%; background:#000; border-radius:4px; }
.fovp .timeline { position:relative; height:26px; width:100%; margin-top:8px; cursor:pointer;
  border:1px solid #e1e0d9; border-radius:4px; overflow:hidden; background:#fcfcfb; user-select:none; }
.fovp .seg { position:absolute; top:0; height:100%; }
.fovp .seg.OFFBOARD { background:#b7d3f6; }
.fovp .seg.ONBOARD  { background:#f0e4d0; }
.fovp .fovseg { position:absolute; top:0; height:100%; background:#e34948; }
.fovp .playhead { position:absolute; top:0; width:2px; height:100%; background:#0b0b0b; pointer-events:none; }
.fovp .btnrow { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:10px; }
.fovp .btnrow button { font:14px system-ui,-apple-system,"Segoe UI",sans-serif; color:#0b0b0b;
  background:#f9f9f7; border:1px solid #e1e0d9; border-radius:6px; padding:5px 12px; cursor:pointer; }
.fovp .btnrow button:hover { background:#e1e0d9; }
.fovp .btnrow button.active { background:#0b0b0b; color:#f9f9f7; border-color:#0b0b0b; }
.fovp .spacer { flex:1; }
.fovp .clock { color:#52514e; font-size:13px; font-variant-numeric:tabular-nums; }
.fovp .chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
.fovp .chip { font-size:13px; color:#0b0b0b; background:#f9f9f7; border:1px solid #e34948;
  border-radius:14px; padding:3px 12px; cursor:pointer; }
.fovp .chip::before { content:"\\25CF"; color:#e34948; margin-right:6px; }
.fovp .chip:hover { background:#fbeaea; }
.fovp .legend { color:#52514e; font-size:13px; margin-top:10px; }
.fovp .legend .sw { display:inline-block; width:11px; height:11px; border-radius:2px;
  vertical-align:-1px; margin-right:3px; border:1px solid #e1e0d9; }
</style>"""

_FOVP_JS = """<script>
(function () {
"use strict";
const ev = __EV__;
const container = document.getElementById("__ID__");
const videoSrc = "__SRC__";

function fmtMMSS(t) {
  t = Math.max(0, t);
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return m + ":" + String(s).padStart(2, "0");
}
function fmtUTC(epoch_s) {
  const d = new Date(epoch_s * 1000);
  return String(d.getUTCHours()).padStart(2, "0") + ":" +
         String(d.getUTCMinutes()).padStart(2, "0") + ":" +
         String(d.getUTCSeconds()).padStart(2, "0");
}

const nominalDur = Math.max(
  ev.t1 - ev.t0,
  ev.mode_segments.length ? ev.mode_segments[ev.mode_segments.length - 1][1] : 0);

// python http.server has no Range support -> blob-load for instant local seeking
const video = document.createElement("video");
video.preload = "metadata";
video.controls = true;
video.playsInline = true;
container.appendChild(video);

const loadNote = document.createElement("div");
loadNote.className = "legend";
loadNote.textContent = "loading video…";
container.appendChild(loadNote);

(async () => {
  try {
    const resp = await fetch(videoSrc);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const total = +resp.headers.get("Content-Length") || 0;
    const reader = resp.body.getReader();
    const chunks = []; let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value); got += value.length;
      loadNote.textContent = "loading video… " +
        (total ? Math.round(100 * got / total) + "%" : (got >> 20) + " MB");
    }
    video.src = URL.createObjectURL(new Blob(chunks, { type: "video/mp4" }));
    loadNote.textContent = "loaded " + (got >> 20) + " MB — fully seekable";
  } catch (e) {
    loadNote.textContent = "blob load failed (" + e.message + ") — falling back to streaming (seeking may not work)";
    video.src = videoSrc;
  }
})();

const dur = () => (isFinite(video.duration) && video.duration > 0)
                  ? video.duration : nominalDur;

const bar = document.createElement("div");
bar.className = "timeline";
bar.title = "click to seek";
container.appendChild(bar);

for (const seg of ev.mode_segments) {
  const d = document.createElement("div");
  d.className = "seg " + seg[2];
  d.style.left = (100 * seg[0] / nominalDur) + "%";
  d.style.width = (100 * (seg[1] - seg[0]) / nominalDur) + "%";
  d.title = seg[2] + "  " + fmtMMSS(seg[0]) + "–" + fmtMMSS(seg[1]);
  bar.appendChild(d);
}
for (const fe of ev.in_fov_events) {
  const d = document.createElement("div");
  d.className = "fovseg";
  d.style.left = (100 * fe[0] / nominalDur) + "%";
  d.style.width = "max(" + (100 * (fe[1] - fe[0]) / nominalDur) + "%, 4px)";
  d.title = "IN FOV " + fe[2] + "–" + fe[3];
  bar.appendChild(d);
}
const playhead = document.createElement("div");
playhead.className = "playhead";
playhead.style.left = "0%";
bar.appendChild(playhead);

function seekTo(t) {
  video.currentTime = Math.min(Math.max(0, t), dur());
}
bar.addEventListener("click", (e) => {
  const r = bar.getBoundingClientRect();
  const frac = Math.min(Math.max(0, (e.clientX - r.left) / r.width), 1);
  seekTo(frac * dur());
});

const row = document.createElement("div");
row.className = "btnrow";
container.appendChild(row);

function mkBtn(label, fn) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.addEventListener("click", fn);
  row.appendChild(b);
  return b;
}

const jumps = ev.in_fov_events.map(fe => Math.max(0, fe[0] - 2));
mkBtn("\\u23EE prev IN FOV", () => {
  const t = video.currentTime;
  const cands = jumps.filter(j => j < t - 0.5);
  if (cands.length) seekTo(cands[cands.length - 1]);
  else if (jumps.length) seekTo(jumps[0]);
});
const playBtn = mkBtn("\\u25B6", () => {
  if (video.paused) video.play(); else video.pause();
});
mkBtn("\\u23ED next IN FOV", () => {
  const t = video.currentTime;
  const cands = jumps.filter(j => j > t + 0.5);
  if (cands.length) seekTo(cands[0]);
  else if (jumps.length) seekTo(jumps[jumps.length - 1]);
});

const speedBtns = [];
for (const sp of [0.5, 1, 2, 4]) {
  const b = mkBtn(sp + "\\u00D7", () => {
    video.playbackRate = sp;
    speedBtns.forEach(x => x.classList.toggle("active", x === b));
  });
  if (sp === 1) b.classList.add("active");
  speedBtns.push(b);
}
video.addEventListener("ratechange", () => {
  const sp = video.playbackRate;
  speedBtns.forEach((b, i) => b.classList.toggle("active", [0.5, 1, 2, 4][i] === sp));
});

const spacer = document.createElement("div");
spacer.className = "spacer";
row.appendChild(spacer);

const clock = document.createElement("span");
clock.className = "clock";
row.appendChild(clock);

function updateUI() {
  const t = video.currentTime;
  playhead.style.left = (100 * Math.min(t / dur(), 1)) + "%";
  clock.textContent = "t+" + fmtMMSS(t) + " / " + fmtMMSS(dur()) +
                      " · " + fmtUTC(ev.t0 + t) + " UTC";
}
video.addEventListener("timeupdate", updateUI);
video.addEventListener("seeked", updateUI);
video.addEventListener("loadedmetadata", updateUI);
video.addEventListener("play",  () => { playBtn.textContent = "\\u23F8"; });
video.addEventListener("pause", () => { playBtn.textContent = "\\u25B6"; });
updateUI();

const chips = document.createElement("div");
chips.className = "chips";
container.appendChild(chips);
for (const fe of ev.in_fov_events) {
  const c = document.createElement("button");
  c.type = "button";
  c.className = "chip";
  c.textContent = "IN FOV " + fe[2] + "–" + fe[3] +
                  " (" + (fe[1] - fe[0]).toFixed(1) + "s)";
  c.addEventListener("click", () => {
    seekTo(Math.max(0, fe[0] - 2));
    video.play();
  });
  chips.appendChild(c);
}
if (!ev.in_fov_events.length) {
  const none = document.createElement("span");
  none.className = "legend";
  none.textContent = "no IN-FOV events";
  chips.appendChild(none);
}

const legend = document.createElement("div");
legend.className = "legend";
legend.innerHTML =
  'timeline: <span class="sw" style="background:#b7d3f6"></span>blue = OFFBOARD guidance' +
  ' · <span class="sw" style="background:#f0e4d0"></span>sand = ONBOARD (pilot)' +
  ' · <span class="sw" style="background:#e34948"></span>red = target inside the seeker cone' +
  ' · target source: ' + ev.fox_name;
container.appendChild(legend);
})();
</script>"""


def wrap_tabs(title, sub, tabs):
    plj = pyo.get_plotlyjs()
    btns = "".join(f'<div class="tab{" on" if i == 0 else ""}" onclick="sel({i})">{t}</div>'
                   for i, (t, _) in enumerate(tabs))
    panes = "".join(f'<div class="pane{" on" if i == 0 else ""}" id="p{i}">{c}</div>'
                    for i, (_, c) in enumerate(tabs))
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><script>{plj}</script><style>
body{{margin:0;background:#fbfbfa;color:#16181c;font:15px/1.6 system-ui,sans-serif;padding:1.2rem}}main{{max-width:1200px;margin:0 auto}}
h1{{font-size:1.5rem}}h3{{color:#333}}.sub{{color:#5b6169}}
.tabs{{display:flex;gap:.4rem;border-bottom:2px solid #e3e4e7;margin-top:1rem;flex-wrap:wrap}}
.tab{{padding:.6rem 1.1rem;border:1px solid #e3e4e7;border-bottom:none;border-radius:8px 8px 0 0;background:#eff0f1;cursor:pointer;font-weight:600}}
.tab.on{{background:#fff;color:#2a78d6;box-shadow:0 -2px 0 #2a78d6 inset}}
.pane{{display:none;background:#fff;border:1px solid #e3e4e7;border-top:none;padding:1rem;border-radius:0 0 10px 10px}}.pane.on{{display:block}}
.center{{text-align:center}}img.gif{{max-width:min(100%,900px);margin:0 auto;display:block}}
.st{{border-collapse:collapse;margin:.6rem 0}}.st td{{border:1px solid #e3e4e7;padding:4px 12px}}.st caption{{font-weight:600;text-align:left;margin-bottom:5px;color:#2a3038}}
.cap{{font-size:12.5px;color:#5b6169}}img{{max-width:100%;border:1px solid #e3e4e7;border-radius:8px}}
h4{{margin:1.3rem 0 .3rem;color:#2a3038}}.js-plotly-plot,.plotly-graph-div{{width:100%!important}}</style></head><body><main>
<h1>{title}</h1><p class="sub">{sub}</p>
<div class="tabs">{btns}</div>
{panes}
<script>function sel(i){{for(var k=0;k<{len(tabs)};k++){{document.getElementById('p'+k).classList.toggle('on',k==i);document.getElementsByClassName('tab')[k].classList.toggle('on',k==i);}}
var pane=document.getElementById('p'+i);
if(window.Plotly){{pane.querySelectorAll('.js-plotly-plot').forEach(function(d){{try{{Plotly.Plots.resize(d);}}catch(e){{}}}});}}}}</script>
</main></body></html>"""


def write_page(path, title, sub, tabs):
    html = wrap_tabs(title, sub, tabs)
    with open(path, "w") as f: f.write(html)
    print(f"WROTE {path} ({os.path.getsize(path)/1e6:.1f} MB)")


# ---------------- standard data access (archiver-dump convention) ----------------
import glob as _glob, datetime as _dt, re as _re
import pandas as _pd
from zoneinfo import ZoneInfo as _ZoneInfo

# Campaign timezone — DST-aware (zoneinfo), settable from the campaign config
# ("tz"). Default America/Los_Angeles reproduces the Seawall reports exactly
# (PDT = UTC-7 in August). `PDT` is kept as a compatibility alias of TZ.
TZ_NAME = "America/Los_Angeles"
TZ = _ZoneInfo(TZ_NAME)
PDT = TZ


def set_tz(name):
    """Select the campaign timezone (IANA name) for every clock-string parse /
    format in this module and in postprocess_report. Returns the ZoneInfo."""
    global TZ_NAME, TZ, PDT
    TZ_NAME = str(name or "America/Los_Angeles")
    TZ = _ZoneInfo(TZ_NAME)
    PDT = TZ
    return TZ


def tz_abbr(t=None):
    """Zone abbreviation ('PDT', 'PST', 'CEST', ...) at epoch t (default: now)."""
    dt = (_dt.datetime.fromtimestamp(float(t), TZ) if t is not None
          else _dt.datetime.now(TZ))
    return dt.strftime("%Z") or TZ_NAME


def epoch_pdt(date_str, hms):
    """'2026-08-28', '08:20:28' (campaign-local wall clock, see set_tz) -> epoch
    seconds. Name kept for compatibility; tz-aware since the campaign 'tz' key.
    Accepts 'HH:MM:SS', 'HH:MM:SS.f' and 'HH:MM'."""
    s = str(hms).strip()
    fmt = "%H:%M:%S.%f" if "." in s else ("%H:%M:%S" if s.count(":") == 2 else "%H:%M")
    return _dt.datetime.strptime(f"{date_str} {s}",
                                 f"%Y-%m-%d {fmt}").replace(tzinfo=TZ).timestamp()


epoch_local = epoch_pdt


def hms_local(t):
    """epoch -> 'HH:MM:SS' in the campaign timezone."""
    return _dt.datetime.fromtimestamp(float(t), TZ).strftime("%H:%M:%S")


def truth_ids(dump_dir, pattern):
    """Truth ids (csv basenames without .csv) that `pattern` matches in <dump>/mavlink/."""
    return sorted(os.path.basename(p)[:-4]
                  for p in _glob.glob(f"{dump_dir}/mavlink/{pattern}"))


def id_label(ids, fallback=""):
    """Short legend label for a set of truth ids as actually loaded: several
    compids of one MAVLink port ('mav14551_2_0', 'mav14551_2_1', ...) -> '14551';
    anything else -> the id(s) themselves ('mavlink_1_2', 'a+b')."""
    ids = sorted({str(i)[:-4] if str(i).endswith(".csv") else str(i) for i in ids})
    if not ids:
        return fallback
    ports = [_re.match(r"mav(\d+)_", i) for i in ids]
    if all(ports) and len({m.group(1) for m in ports}) == 1:
        return ports[0].group(1)
    return "+".join(ids) if len(ids) <= 3 else f"{ids[0]}+{len(ids) - 1} more"


def load_dump_truth(dump_dir, pattern, t0, t1, freeze_scrub=True):
    """Merged, scrubbed truth ENU (t,E,N,U_hae) from dump mavlink csvs (archiver,
    quickdump or dump_run_window layout — columns are addressed by NAME, extra
    columns such as speed_mps/source and the time_pdt/time_local column are ignored)."""
    rows = []
    for fp in sorted(_glob.glob(f"{dump_dir}/mavlink/{pattern}")):
        df = _pd.read_csv(fp)
        if "validposition" in df: df = df[df["validposition"] != 0]
        df = df[(df["t_epoch"] >= t0 - 60) & (df["t_epoch"] <= t1 + 60)]
        if not len(df): continue
        df = df.sort_values("t_epoch")
        if freeze_scrub:
            mv = np.concatenate([[True], (np.abs(np.diff(df["E_m"])) +
                                          np.abs(np.diff(df["N_m"]))) > 0.01])
            df = df[mv]
        ucol = "U_m_hae" if "U_m_hae" in df.columns else "U_m"
        rows.append(df[["t_epoch", "E_m", "N_m", ucol]].to_numpy())
    if not rows:
        have = ", ".join(truth_ids(dump_dir, "*.csv")) or "none"
        raise FileNotFoundError(f"no truth rows for {pattern} in {dump_dir} "
                                f"(window {hms_local(t0)}-{hms_local(t1)} {tz_abbr(t0)}; "
                                f"mavlink ids present: {have})")
    A = np.vstack(rows); A = A[np.argsort(A[:, 0])]
    _, u = np.unique(np.round(A[:, 0], 1), return_index=True)
    return A[u]


TRACK13_COLS = ["t_epoch", "E_m", "N_m", "U_m", "vE_mps", "vN_mps", "vU_mps",
                "sigE_m", "sigN_m", "sigU_m", "total_associations", "track_state",
                "last_update_t"]


def load_dump_track13(dump_dir, tid):
    """13-col t,E,N,U,vE,vN,vU,sigE,sigN,sigU,assoc,state,lu from a dump track csv
    (by column NAME — the extra truth_match_*/contributors/sigv*/track_type columns
    of newer dumps are ignored)."""
    df = _pd.read_csv(f"{dump_dir}/tracks/track_{tid}.csv")
    missing = [c for c in TRACK13_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"track_{tid}.csv lacks columns {missing}")
    return df[TRACK13_COLS].to_numpy(float)


def load_traj_csv_truth(csv_path, ant):
    """Truth from a lat/lon trajectory csv (e.g. an onboard-GPS export with columns
    utc_s, lat, lon, alt_hae_m) -> (t,E,N,U_hae-rel-antenna) in the dump ENU frame."""
    import math as _m
    z = _pd.read_csv(csv_path)
    _e, _n, _u = _C.EnuFrame(ant).enu(z["lat"].to_numpy(float), z["lon"].to_numpy(float), z["alt_hae_m"].to_numpy(float))   # exact WGS-84 ENU
    return np.column_stack([z["utc_s"], _e, _n, _u])
