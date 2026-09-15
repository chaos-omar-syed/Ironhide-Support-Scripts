"""Shared report figures for MAVLink-truth vs VP-track analysis.

Everything the 2026-08-26 MRU91 mission report proved out, generalized:
  * RAW errors by default — no bias removal anywhere; the only adjustment is
    the truth altitude datum (median track-truth dU, a truth-reference defect).
  * maps_fig: top-down EN, RAW panel + az-bias-rotated alignment-check panel.
  * angle_stack: az/el error vs time, ±1σ band + ±3σ dotted edges, raw-obs
    overlay, RMSE + 95.5%-containment annotations.
  * posvel_fig: 3D position error + HORIZONTAL |Δv| (filter state velocity vs
    truth-REPORTED velocity — never differentiated positions); vertical rate
    included only when the build publishes a physically plausible one.
  * rate_fig: merged measurement rate with rollup annotation.
  * seeker_quad + seeker_cartoon: off-boresight angle to TRUTH for a seeker
    ahead of the TRACK state along its velocity at N standoffs, FOV circles,
    pAcq rollups.

All figures take a Ctx and per-figure inputs; nothing module-level is
mission-specific. Styling funnel div() bakes in the legibility rules (bold
axes/ticks, darker grid, automargin, one template).
"""
import math
from datetime import datetime

import numpy as np
import plotly.graph_objects as go

import corr_lib as C

INK, INK2, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"
GRID2, AXLINE = "#d6d5d0", "#c4c3be"
BLUE, RED = "#2a78d6", "#e34948"
STRONG = ["#0f766e", "#7c3aed", "#be123c", "#15803d", "#b45309",
          "#0e7490", "#a21caf", "#4d7c0f"]          # deep, saturated
SEQ_STEPS = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf",
             "#184f95", "#0d366b"]

LAYOUT = dict(template="plotly_white",
              font=dict(family="Segoe UI, Roboto, Helvetica, Arial, sans-serif",
                        color=INK, size=14),
              margin=dict(l=64, r=20, t=84, b=52),
              legend=dict(orientation="h", y=1.02, yanchor="bottom",
                          bgcolor="rgba(0,0,0,0)", font=dict(size=13)))


def rgba(h, a):
    return f"rgba({int(h[1:3],16)},{int(h[3:5],16)},{int(h[5:7],16)},{a})"


class Ctx:
    """Everything the figures need, assembled once by the calling tool.

    T        cleaned truth array (t,E,N,U,spd,vN,vE,vU_raw)
    pairs    [(tid, target, Mm_prepared)] — matched arrays already through
             corr_lib.prep_matched (death-coast truncated, excursions masked)
    tracks   {tid: full track array from corr_lib.load_tracks (13 cols)}
    du       truth altitude datum offset, median(track_U - truth_U), m
    bias     MEASURED az bias, deg (annotated everywhere, removed nowhere
             unless remove_bias says so)
    remove_bias  deg actually subtracted from az errors (0 = RAW, the default)
    obs      matched raw-obs errors from corr_lib.match_obs, or None
    vu       truth vertical_speed scale (corr_lib.vu_scale)
    vu_ok    build publishes a usable track vertical rate (track_vu_usable)
    tz       zoneinfo for axis timestamps
    """

    def __init__(self, T, pairs, tracks, du=0.0, bias=0.0, remove_bias=0.0,
                 obs=None, vu=1.0, vu_ok=False, tz=None):
        self.T, self.pairs, self.tracks = T, pairs, tracks
        self.du, self.bias, self.remove_bias = du, bias, remove_bias
        self.obs, self.vu, self.vu_ok = obs, vu, vu_ok
        self.tz = tz or C.LOCAL_TZ
        self.colors = {tid: STRONG[i % len(STRONG)]
                       for i, (tid, _, _) in enumerate(pairs)}

    def pt(self, arr):
        return [None if x is None else datetime.fromtimestamp(x, tz=self.tz)
                for x in np.atleast_1d(arr)]

    def corrected(self, m):
        """RAW radar errors; only the truth alt datum removed from dU."""
        return m[:, 11], m[:, 12], m[:, 13] - self.du


def seg_split(A, gap=6):
    s = 0
    for i in range(1, len(A)):
        if A[i, 0] - A[i - 1, 0] > gap:
            yield A[s:i]
            s = i
    yield A[s:]


def div(fig, h=460):
    # one funnel = one axis style everywhere: darker grid, outside ticks, bold
    for up in (fig.update_xaxes, fig.update_yaxes):
        up(gridcolor=GRID2, zerolinecolor=AXLINE, linecolor=AXLINE,
           ticks="outside", tickcolor=AXLINE, automargin=True,
           tickfont=dict(size=13, weight="bold"),
           title_font=dict(size=15, weight="bold"))
    return ('<div class="plot">' + fig.to_html(
        full_html=False, include_plotlyjs=False, default_width="100%",
        default_height=f"{h}px", config={"displaylogo": False, "responsive": True})
        + "</div>")


# ---------------------------------------------------------------- top-down EN
def maps_fig(ctx):
    """RAW map (left) and az-bias-rotated map (right), one legend for both."""
    fig = go.Figure()
    Tw = ctx.T
    pad = 150
    xr = [Tw[:, 1].min() - pad, Tw[:, 1].max() + pad]
    yr = [Tw[:, 2].min() - pad, Tw[:, 2].max() + pad]
    n_meas = sum(int(m[:, 14].sum()) for _, _, m in ctx.pairs)
    for panel, rot in ((0, 0.0), (1, -ctx.bias)):
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
                                     hovertemplate="E %{x:.0f} N %{y:.0f}"
                                                   "<extra>truth</extra>"))
            first = False
        br = math.radians(rot)
        for tid, _, mw in ctx.pairs:
            E = mw[:, 1] * math.cos(br) + mw[:, 2] * math.sin(br)
            N = mw[:, 2] * math.cos(br) - mw[:, 1] * math.sin(br)
            fig.add_trace(go.Scatter(
                x=E, y=N, mode="lines+markers", xaxis=ax, yaxis=ay,
                name=f"trk {tid}", legendgroup=f"g{tid}", showlegend=(panel == 0),
                line=dict(color=ctx.colors[tid], width=2.6),
                marker=dict(size=5, color=ctx.colors[tid],
                            opacity=mw[:, 14].tolist()),
                text=[f"{t:%H:%M:%S}" for t in ctx.pt(mw[:, 0])],
                hovertemplate="%{text}<br>E %{x:.0f} N %{y:.0f}<extra>trk " +
                              str(tid) + "</extra>"))
        fig.add_trace(go.Scatter(x=[0], y=[0], mode="markers", xaxis=ax, yaxis=ay,
                                 marker=dict(symbol="triangle-up", size=11, color=INK),
                                 showlegend=False,
                                 hovertemplate="radar<extra></extra>"))
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
                 text=f"{n_meas} measurements in window"),
            dict(x=0.03, y=0.98, xref="x domain", yref="y domain",
                 xanchor="left", yanchor="top", showarrow=False,
                 font=dict(size=14), bgcolor="rgba(255,255,255,0.75)",
                 text="<b>RAW</b> (as the radar reports)"),
            dict(x=0.03, y=0.98, xref="x2 domain", yref="y2 domain",
                 xanchor="left", yanchor="top", showarrow=False,
                 font=dict(size=14), bgcolor="rgba(255,255,255,0.75)",
                 text=f"<b>rotated {ctx.bias:+.2f}°</b> (alignment check only)")])
    return fig


# --------------------------------------------------------- az/el error stack
def angle_stack(ctx):
    """Az (top) + El (bottom), shared time axis, one legend entry per track,
    raw-obs overlay behind, RMSE + 95.5%-containment annotations."""
    fig = go.Figure()
    lims = {"az": [], "el": []}
    errs = {"az": [], "el": []}
    ob = ctx.obs if ctx.obs is not None and len(ctx.obs) else None
    if ob is not None:
        step = max(1, len(ob) // 500)
        obt = ob[::step]
        for row, ay in (("az", "y"), ("el", "y2")):
            oe = obt[:, 1] if row == "az" else obt[:, 2]
            if row == "az":
                oe = oe - ctx.remove_bias
            fig.add_trace(go.Scatter(x=ctx.pt(obt[:, 0]), y=oe, mode="markers",
                                     yaxis=ay, name="raw observations",
                                     legendgroup="obs", showlegend=(row == "az"),
                                     marker=dict(symbol="x-thin", size=4, opacity=0.45,
                                                 line=dict(width=1, color=INK2)),
                                     hovertemplate="%{x|%H:%M:%S}<br>obs %{y:.2f}°"
                                                   "<extra>obs</extra>"))
            lims[row].append(np.percentile(np.abs(oe), 99))
    for tid, _, m in ctx.pairs:
        ae = C.angle_errors(m, az_bias=ctx.remove_bias)
        t = ctx.pt(m[:, 0])
        c = ctx.colors[tid]
        for row, ay, err, sig in (("az", "y", ae["az_err"], ae["sig_az"]),
                                  ("el", "y2", ae["el_err"], ae["sig_el"])):
            for sgn in (1, -1):
                fig.add_trace(go.Scatter(x=t, y=err + sgn * 3 * sig, mode="lines",
                                         yaxis=ay,
                                         line=dict(width=1, color=rgba(c, 0.45),
                                                   dash="dot"),
                                         showlegend=False, legendgroup=f"g{tid}",
                                         hoverinfo="skip"))
            fig.add_trace(go.Scatter(
                x=t + t[::-1], y=list(err + sig) + list(err - sig)[::-1],
                fill="toself", fillcolor=rgba(c, 0.20), yaxis=ay,
                line=dict(width=0), showlegend=False, legendgroup=f"g{tid}",
                hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=t, y=err, mode="lines+markers", yaxis=ay,
                                     name=f"trk {tid}", legendgroup=f"g{tid}",
                                     showlegend=(row == "az"),
                                     line=dict(color=c, width=2.4),
                                     marker=dict(size=5, color=c,
                                                 opacity=m[:, 14].tolist()),
                                     hovertemplate="%{x|%H:%M:%S}<br>%{y:.2f}°"
                                                   f"<extra>{tid} {row}</extra>"))
            lims[row].append(np.percentile(np.abs(err) + sig, 99))
            errs[row].append(err)

    def lim(row):
        return (min(4.0, max(1.0, math.ceil(max(lims[row]) * 1.15 / 0.5) * 0.5))
                if lims[row] else 2.0)

    la, le = lim("az"), lim("el")
    notes = []
    for row, l, yref in (("az", la, "y domain"), ("el", le, "y2 domain")):
        if ob is not None:
            oe = ob[:, 1] if row == "az" else ob[:, 2]
            off = float(np.mean(np.abs(oe) > l)) * 100
            if off > 2:
                notes.append(dict(x=0.995, y=0.97, xref="paper", yref=yref,
                                  xanchor="right", yanchor="top", showarrow=False,
                                  font=dict(size=12, color=INK2),
                                  bgcolor="rgba(255,255,255,0.75)",
                                  text=f"{off:.0f}% of obs beyond ±{l:g}° scale"))
        if errs[row]:
            e = np.concatenate(errs[row])
            rmse = float(np.sqrt(np.mean(e ** 2)))
            p955 = float(np.percentile(np.abs(e), 95.5))
            notes.append(dict(x=0.995, y=0.03, xref="paper", yref=yref,
                              xanchor="right", yanchor="bottom", showarrow=False,
                              font=dict(size=13, weight="bold"),
                              bgcolor="rgba(255,255,255,0.8)",
                              text=f"track RMSE {rmse:.2f}° · 95.5% of samples "
                                   f"within {p955:.2f}°"))
    fig.update_layout(**LAYOUT, annotations=notes,
        xaxis=dict(anchor="y2", title="time (local)"),
        yaxis=dict(domain=[0.56, 1], range=[-la, la], dtick=0.5,
                   title="az error (deg)"),
        yaxis2=dict(domain=[0, 0.44], range=[-le, le], dtick=0.5,
                    title="el error (deg)"),
        shapes=[dict(type="line", xref="paper", x0=0, x1=1, yref=yr_, y0=0, y1=0,
                     line=dict(color=INK2, width=1, dash="dash"))
                for yr_ in ("y", "y2")])
    return fig


# ------------------------------------------------ 3D pos + velocity error
def posvel_fig(ctx):
    """3D position error (top) + |Δv| velocity error (bottom), one legend.
    Velocity = filter state velocity vs truth-REPORTED velocity. Horizontal
    only unless the build's vertical rate is usable (ctx.vu_ok)."""
    fig = go.Figure()
    e3all, dvall = [], []
    have_vel = False
    for tid, _, m in ctx.pairs:
        dEc, dNc, dUc = ctx.corrected(m)
        e3 = np.sqrt(dEc ** 2 + dNc ** 2 + dUc ** 2)
        e3all.append(e3)
        t = ctx.pt(m[:, 0])
        c = ctx.colors[tid]
        fig.add_trace(go.Scatter(x=t, y=e3, mode="lines", yaxis="y",
                                 name=f"trk {tid}", legendgroup=f"g{tid}",
                                 line=dict(color=c, width=2.4),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.0f} m"
                                               f"<extra>{tid} 3D pos</extra>"))
        A = ctx.tracks.get(tid)
        if A is None or A.shape[1] < 12 or not np.isfinite(A[:, 10]).any():
            continue
        vf = A[np.isfinite(A[:, 10]), 10:12]
        if not len(vf) or float(np.abs(vf).max()) < 0.01:
            continue          # all-zero velocities = old save without them
        idx = np.clip(np.searchsorted(A[:, 0], m[:, 0]), 0, len(A) - 1)
        tvE, tvN, tvU = A[idx, 10], A[idx, 11], A[idx, 12]
        gvE, gvN, gvU, ok = C.truth_vel_at(ctx.T, m[:, 0], vu=ctx.vu)
        ok &= np.isfinite(tvE) & np.isfinite(tvN)
        if ok.sum() < 6:
            continue
        have_vel = True
        tt = m[ok, 0]
        if ctx.vu_ok and np.isfinite(gvU[ok]).all():
            dv = np.sqrt((tvE[ok] - gvE[ok]) ** 2 + (tvN[ok] - gvN[ok]) ** 2 +
                         (tvU[ok] - gvU[ok]) ** 2)
        else:
            dv = np.sqrt((tvE[ok] - gvE[ok]) ** 2 + (tvN[ok] - gvN[ok]) ** 2)
        # 10 s centered rolling mean (GPS-reported velocity is noisy at 1 Hz)
        k = max(3, int(round(10.0 / max(np.median(np.diff(tt)), 0.2))) | 1)
        sm = np.convolve(dv, np.ones(k) / k, mode="same")
        dvall.append((sm, dv))
        fig.add_trace(go.Scatter(x=ctx.pt(tt), y=dv, mode="lines", yaxis="y2",
                                 legendgroup=f"g{tid}", showlegend=False,
                                 line=dict(color=rgba(c, 0.25), width=1),
                                 hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=ctx.pt(tt), y=sm, mode="lines", yaxis="y2",
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
                          text=f"n={len(raw_all)} · RMS "
                               f"{np.sqrt(np.mean(raw_all**2)):.1f} m/s · "
                               f"3σ (99.7%) within "
                               f"{np.percentile(raw_all, 99.7):.1f} m/s"))
    else:
        vl = 15
        if not have_vel:
            notes.append(dict(x=0.5, y=0.5, xref="paper", yref="y2 domain",
                              showarrow=False, font=dict(size=13, color=INK2),
                              text="no filter-state velocity in this data"))
    vtitle = ("|Δv| (m/s)" if ctx.vu_ok else "horizontal |Δv| (m/s)")
    fig.update_layout(**LAYOUT, annotations=notes,
        xaxis=dict(anchor="y2", title="time (local)"),
        yaxis=dict(domain=[0.56, 1], rangemode="tozero", title="3D pos error (m)"),
        yaxis2=dict(domain=[0, 0.44], range=[0, vl], title=vtitle))
    return fig


# ----------------------------------------------------------- measurement rate
def rate_fig(ctx, t0, t1, obs_count=None):
    """Merged measurement rate on the target (any correlated track), Hz,
    30 s sliding window, with rollup annotation."""
    fts = []
    for tid, _, _ in ctx.pairs:
        A = ctx.tracks.get(tid)
        if A is None:
            continue
        ft = C.meas_times(A)
        fts.append(ft[(ft >= t0) & (ft <= t1)])
    allft = np.sort(np.concatenate(fts)) if fts else np.array([])
    fig = go.Figure()
    if len(allft) > 3:
        grid = np.arange(t0, t1 + 2.5, 5.0)
        WIN = 30.0
        cnt = (np.searchsorted(allft, grid + WIN / 2)
               - np.searchsorted(allft, grid - WIN / 2))
        lo = np.maximum(grid - WIN / 2, t0)
        hi = np.minimum(grid + WIN / 2, t1)
        hz = cnt / np.maximum(hi - lo, 1.0)
        fig.add_trace(go.Scatter(x=ctx.pt(grid), y=hz, mode="lines",
                                 name="measurement rate on target (any track)",
                                 line=dict(color=BLUE, width=2.6, shape="spline"),
                                 fill="tozeroy", fillcolor=rgba(BLUE, 0.10),
                                 hovertemplate="%{x|%H:%M:%S}<br>%{y:.2f} Hz"
                                               "<extra></extra>"))
    fig.add_hline(y=2, line=dict(color=INK2, width=1, dash="dot"),
                  annotation_text="2 Hz = every dwell", annotation_font_color=INK2)
    txt = (f"{len(allft)} track measurements · avg "
           f"{len(allft)/max(t1-t0,1):.2f} Hz")
    if obs_count is not None:
        txt += f" · {obs_count} matched raw obs"
    note = (dict(x=0.995, y=0.05, xref="paper", yref="paper",
                 xanchor="right", yanchor="bottom", showarrow=False,
                 font=dict(size=13, weight="bold"), bgcolor="rgba(255,255,255,0.85)",
                 text=txt) if len(allft) else None)
    fig.update_layout(**LAYOUT, annotations=[note] if note else [])
    fig.update_yaxes(title="Hz", range=[0, 2.3])
    fig.update_xaxes(title="time (local)")
    return fig


# ------------------------------------------------------------- seeker basket
def seeker_quad(ctx, standoffs=(600, 450, 300, 150), fov_deg=12.0,
                window=None, min_speed=4.0):
    """Seeker-vertex angular error, one panel per standoff (2 columns).
    Seeker sits AHEAD of the TRACK state along the track's velocity at
    standoff R, boresight on the track state; plotted is the 2D angle from
    boresight to TRUTH. By default uses CLOSING samples only (truth range-rate
    < -1 m/s — the inbound geometry a seeker would fly); pass window=(t0,t1)
    to restrict further. -> (fig, {standoff: pct inside FOV})."""
    samples = []
    for tid, _, m in ctx.pairs:
        m2 = m if window is None else m[(m[:, 0] >= window[0]) & (m[:, 0] <= window[1])]
        if len(m2) < 4:
            continue
        # closing samples only: truth moving toward the radar
        rng = np.hypot(m2[:, 8], m2[:, 9])
        rr = np.gradient(rng, m2[:, 0], edge_order=1)
        m2 = m2[rr < -1.0]
        if len(m2) < 4:
            continue
        S = C.seeker_samples(m2, ctx.tracks[tid], du=ctx.du, min_speed=min_speed)
        if len(S):
            samples.append(S)
    fig = go.Figure()
    stats = {}
    if samples:
        S = np.vstack(samples)
        S = S[np.argsort(S[:, 0])]
        t0 = S[0, 0]
        th = np.linspace(0, 2 * np.pi, 120)
        ncol = 2
        nrow = math.ceil(len(standoffs) / ncol)
        annots, shapes = [], []
        layout_axes = {}
        LIMD = max(30, math.ceil(fov_deg * 2.5 / 10) * 10)
        for k, R in enumerate(standoffs):
            r, c = divmod(k, ncol)
            ax = "x" if k == 0 else f"x{k+1}"
            ay = "y" if k == 0 else f"y{k+1}"
            az, el, tt = C.seeker_angles(S, R)
            inside = (float(np.mean(np.hypot(az, el) <= fov_deg)) * 100
                      if len(az) else 0.0)
            stats[R] = round(inside, 1)
            fig.add_trace(go.Scatter(
                x=az, y=el, mode="markers", xaxis=ax, yaxis=ay,
                marker=dict(size=6, color=(tt - t0) / 60,
                            colorscale=[[i / (len(SEQ_STEPS) - 1), cc]
                                        for i, cc in enumerate(SEQ_STEPS)],
                            showscale=(k == 0),
                            colorbar=dict(title=dict(text="min into<br>window",
                                                     font=dict(size=12)),
                                          thickness=13, x=1.0, xanchor="left",
                                          len=0.9)),
                text=[f"{p:%H:%M:%S}" for p in ctx.pt(tt)],
                hovertemplate="%{text}<br>az %{x:.1f}° el %{y:.1f}°<extra>" +
                              f"{R} m</extra>", showlegend=False))
            fig.add_trace(go.Scatter(x=fov_deg * np.cos(th), y=fov_deg * np.sin(th),
                                     mode="lines", xaxis=ax, yaxis=ay,
                                     showlegend=False,
                                     line=dict(color=RED, width=1.6, dash="dash"),
                                     hoverinfo="skip"))
            annots.append(dict(x=0.04, y=0.96, xref=f"{ax} domain",
                               yref=f"{ay} domain", xanchor="left", yanchor="top",
                               showarrow=False, font=dict(size=15, weight="bold"),
                               bgcolor="rgba(255,255,255,0.85)",
                               text=f"standoff {R} m — {inside:.0f}% inside "
                                    f"{fov_deg:g}°"))
            xd = [0.54, 1.0] if c else [0, 0.46]
            row_h = 1.0 / nrow
            yd = [1 - (r + 1) * row_h + (0.05 * row_h * 2 if r < nrow - 1 else 0),
                  1 - r * row_h - (0.05 * row_h * 2 if r > 0 else 0)]
            axd = dict(range=[-LIMD, LIMD], dtick=10, constrain="domain",
                       showline=True, mirror=True)
            bottom_row = (r == nrow - 1)
            layout_axes["xaxis" if k == 0 else f"xaxis{k+1}"] = dict(
                domain=xd, anchor=ay,
                title=("seeker az off-boresight (deg)" if bottom_row else None),
                **axd)
            layout_axes["yaxis" if k == 0 else f"yaxis{k+1}"] = dict(
                domain=yd, anchor=ax, scaleanchor=ax,
                title=("seeker el off-boresight (deg)" if c == 0 else None),
                **axd)
        # panel divider lines
        shapes.append(dict(type="line", xref="paper", yref="paper", x0=0.5, x1=0.5,
                           y0=0, y1=1, line=dict(color=AXLINE, width=1.5)))
        for r in range(1, nrow):
            yv = 1 - r / nrow
            shapes.append(dict(type="line", xref="paper", yref="paper", x0=0, x1=1,
                               y0=yv, y1=yv, line=dict(color=AXLINE, width=1.5)))
        fig.update_layout(**LAYOUT, annotations=annots, shapes=shapes, **layout_axes)
        fig.update_layout(margin=dict(r=100))   # room for the colorbar
    return fig, stats


def seeker_cartoon(qstats, standoffs=(600, 450, 300, 150), fov_deg=12.0):
    """Inline SVG explaining the seeker geometry, with pAcq rollups per
    standoff. Works for any standoff list (ticks spaced by range)."""
    sts = sorted(standoffs)                       # ascending left->right
    x0, x1 = 200, 758                             # tick span in the drawing
    xs = {r: x0 + (x1 - x0) * (r - sts[0]) / max(sts[-1] - sts[0], 1)
          for r in sts} if len(sts) > 1 else {sts[0]: x1}
    ticks = "".join(f'<line x1="{xs[r]:.0f}" y1="95" x2="{xs[r]:.0f}" y2="290"/>'
                    for r in sts)
    # labels: keep clear of the FOV cone edge near the right side
    labels = ""
    for r in sts:
        x = xs[r]
        if x > 700:
            labels += f'<text x="{min(x - 32, 726):.0f}" y="56">{r} m</text>'
        elif x > 540:
            labels += f'<text x="{x - 33:.0f}" y="90">{r} m</text>'
        else:
            labels += f'<text x="{x:.0f}" y="90">{r} m</text>'
    pacq = "".join(f'<text x="{xs[r]:.0f}" y="312">{qstats.get(r, 0):.0f}%</text>'
                   for r in sts)
    return f"""
<svg viewBox="0 0 920 330" style="max-width:920px;display:block;margin:.5rem auto;
     background:#fff;border:1px solid #e4e3df;border-radius:6px"
     xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI,Arial" font-size="14">
  <defs><marker id="ah" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L6,3 L0,6 z" fill="#52514e"/></marker></defs>
  <!-- FOV cone (shaded) -->
  <path d="M118,150 L760,78 L760,222 Z" fill="rgba(227,73,72,0.07)"
        stroke="#e34948" stroke-width="1.5" stroke-dasharray="6,4"/>
  <text x="560" y="66" fill="#e34948" font-weight="bold">{fov_deg:g}° seeker FOV</text>
  <!-- missile -->
  <g>
    <rect x="52" y="142" width="52" height="16" rx="3" fill="#0b0b0b"/>
    <polygon points="104,142 122,150 104,158" fill="#0b0b0b"/>
    <polygon points="52,142 40,132 52,150" fill="#0b0b0b"/>
    <polygon points="52,158 40,168 52,150" fill="#0b0b0b"/>
    <text x="34" y="188" fill="#0b0b0b" font-weight="bold">interceptor / seeker</text>
  </g>
  <!-- standoff range ticks: dropped all the way to the pAcq row -->
  <g stroke="#9a9994" stroke-dasharray="4,4">{ticks}</g>
  <g fill="#52514e" font-size="13" text-anchor="middle" font-weight="bold">
    {labels}
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
  <!-- per-standoff acquisition probability -->
  <line x1="40" y1="278" x2="840" y2="278" stroke="#e4e3df"/>
  <text x="44" y="308" fill="#0b0b0b" font-weight="bold" font-size="16">pAcq
    ({fov_deg:g}° FOV):</text>
  <g font-size="21" font-weight="bold" fill="#e34948" text-anchor="middle">
    {pacq}
  </g>
</svg>"""


# ------------------------------------------------------------- rollup stats
def run_stats(ctx, t0, t1):
    """Window rollup: RAW horiz/3D error, meas count/rate, wall-clock tracked
    coverage (meas within 2 s, first->last detection), longest mid-window gap."""
    horiz, e3d, fts = [], [], []
    for tid, _, m in ctx.pairs:
        dEc, dNc, dUc = ctx.corrected(m)
        horiz.append(np.hypot(dEc, dNc))
        e3d.append(np.sqrt(dEc ** 2 + dNc ** 2 + dUc ** 2))
        A = ctx.tracks.get(tid)
        if A is not None:
            ft = C.meas_times(A)
            fts.append(ft[(ft >= t0) & (ft <= t1)])
    h = np.concatenate(horiz) if horiz else np.array([0.0])
    e = np.concatenate(e3d) if e3d else np.array([0.0])
    allft = np.sort(np.concatenate(fts)) if fts else np.array([])
    if len(allft) >= 2:
        grid = np.arange(allft[0], allft[-1], 1.0)
        idx = np.clip(np.searchsorted(allft, grid), 1, len(allft) - 1)
        near = np.minimum(np.abs(allft[idx] - grid), np.abs(allft[idx - 1] - grid))
        cov = float(np.mean(near < 2.0))
        difs = np.diff(allft)
        gi = int(np.argmax(difs))
        max_gap, gap_t = float(difs[gi]), float(allft[gi])
    else:
        cov, max_gap, gap_t = 0.0, float(t1 - t0), t0
    return dict(horiz_med=float(np.median(h)), horiz_p95=float(np.percentile(h, 95)),
                e3d_med=float(np.median(e)), e3d_p95=float(np.percentile(e, 95)),
                meas=int(len(allft)), rate=float(len(allft) / max(t1 - t0, 1)),
                cov=cov, max_gap=max_gap, gap_t=gap_t)
