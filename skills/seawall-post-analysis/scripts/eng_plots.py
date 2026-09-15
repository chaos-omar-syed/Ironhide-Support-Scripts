"""Interactive engagement plots for the intercept report (Tabs 2/3).
Responsive Plotly (fills width, zoom/pan/hover, legend-filter) -> fixes 'too small'.
Consistent colors across tabs; clear legend names; ±window around CPA; speed leaders.
Object arrays are [t,E,N,U(,vE,vN,vU...)] in metres about the antenna."""
import numpy as np
import plotly.graph_objects as go

TRUTH  = "#2a78d6"   # target MAVLink truth (blue)  — consistent w/ Tab 1
TRACK  = "#0f9d58"   # radar track (green)          — consistent w/ Tab 1
INTER  = "#e34948"   # interceptor (red)
FROZEN = "#8a8f98"   # frozen truth point (grey)
LEAD_S = 1.5         # velocity-leader = distance travelled in 1.5 s

def _p(a):  return np.asarray(a, float)[:, :4]                       # t,E,N,U
def _v(a):
    a = np.asarray(a, float)
    return a[:, 4:7] if a.shape[1] >= 7 else np.gradient(a[:, 1:4], a[:, 0], axis=0)

def travel_v(a, t, dt=1.5):
    """Direction of travel from the path (robust to bad stored vertical velocity)."""
    a = np.asarray(a, float)
    if len(a) < 2: return np.zeros(3)
    t1 = min(t+dt, a[-1,0]); t0 = max(t-dt, a[0,0]); s = t1-t0
    if s <= 0.1: return np.zeros(3)
    return (np.array([np.interp(t1,a[:,0],a[:,k]) for k in (1,2,3)]) -
            np.array([np.interp(t0,a[:,0],a[:,k]) for k in (1,2,3)]))/s

def resample(a, dt=0.5):
    """Interpolate onto a uniform time grid (fills gaps) so a fragmented track draws as
    one continuous line — used for the dive where telemetry has holes."""
    a = np.asarray(a, float)
    if len(a) < 2: return a
    a = a[np.argsort(a[:,0])]
    g = np.arange(a[0,0], a[-1,0]+dt, dt)
    out = np.zeros((len(g), a.shape[1])); out[:,0] = g
    for k in range(1, a.shape[1]): out[:,k] = np.interp(g, a[:,0], a[:,k])
    return out

def declutter(a, vmax=130.0):
    """Drop single-sample GPS teleports (implied speed to BOTH neighbours > vmax)."""
    a = np.asarray(a, float)
    if len(a) < 3: return a
    keep = np.ones(len(a), bool)
    for i in range(1, len(a) - 1):
        d0 = np.linalg.norm(a[i,1:4]-a[i-1,1:4]) / max(a[i,0]-a[i-1,0], 1e-3)
        d1 = np.linalg.norm(a[i+1,1:4]-a[i,1:4]) / max(a[i+1,0]-a[i,0], 1e-3)
        if d0 > vmax and d1 > vmax: keep[i] = False
    return a[keep]

def drop_frozen(a, eps=1.0, minrun=3):
    """Remove stale/held-telemetry: runs of >=minrun consecutive near-identical positions
    (a MAVLink dropout that freezes pos+vel) are dropped entirely so they don't spike."""
    a = np.asarray(a, float)
    if len(a) < 2: return a
    keep = np.ones(len(a), bool); i = 0
    while i < len(a):
        j = i
        while j+1 < len(a) and np.linalg.norm(a[j+1,1:4]-a[i,1:4]) < eps: j += 1
        if j-i+1 >= minrun: keep[i:j+1] = False
        i = j+1
    return a[keep]

def last_run(a, gap=2.5):
    """Trailing contiguous segment — drops isolated leading samples separated by a big
    time gap (e.g. one stray pre-freeze point), so time-series plots have no blank span."""
    a = np.asarray(a, float)
    if len(a) < 2: return a
    cut = 0
    for i in range(len(a)-1, 0, -1):
        if a[i,0]-a[i-1,0] > gap: cut = i; break
    return a[cut:]

def condition(arr, vmax=130.0, du_max=60.0, u_pad=300.0, resample_dt=None, max_gap=4.0):
    """CANONICAL input hygiene for every figure. Applied inside gif3d/topdown/
    guidance_fig/closing/cpa so ALL callers inherit it:
      sort+dedupe t -> drop frozen-telemetry runs -> horizontal teleport scrub
      -> vertical junk scrub (robust median gate + dU/dt gate; the published
      track vertical channel produces -1000 m rows and +-100 m/s spikes)
      -> optional uniform resample (gap-aware: never interpolates across gaps
      > max_gap) so animated markers move smoothly on sparse feeds."""
    a = np.asarray(arr, float)
    if len(a) < 2: return a[:, :4] if a.ndim == 2 and a.shape[1] >= 4 else a
    a = a[:, :4]
    a = a[np.argsort(a[:, 0])]
    _, u = np.unique(np.round(a[:, 0], 3), return_index=True)
    a = a[u]
    a = drop_frozen(a)
    a = declutter(a, vmax)
    if len(a) < 2: return a
    med = np.median(a[:, 3])
    a = a[np.abs(a[:, 3] - med) <= u_pad]
    if len(a) < 2: return a
    keep = np.ones(len(a), bool); last = 0            # vertical teleport gate
    for j in range(1, len(a)):
        dt = a[j, 0] - a[last, 0]
        if dt <= 0: keep[j] = False; continue
        if abs(a[j, 3] - a[last, 3]) / dt > du_max and dt < 20: keep[j] = False
        else: last = j
    a = a[keep]
    if resample_dt and len(a) >= 2:
        segs, out = np.split(np.arange(len(a)), np.where(np.diff(a[:, 0]) > max_gap)[0] + 1), []
        for ix in segs:
            if len(ix) < 2: continue
            t = np.arange(a[ix[0], 0], a[ix[-1], 0] + 1e-9, resample_dt)
            out.append(np.column_stack([t] + [np.interp(t, a[ix, 0], a[ix, k]) for k in (1, 2, 3)]))
        if out: a = np.vstack(out)
    return a


def dive_bottom(inter):
    """Time the steep dive levels off: steepest descent point, then first sample where
    the vertical rate recovers above -2 m/s. This is the 'min point of the dive' to
    anchor a 15-s-tgo window on (NOT the later slow-sink trough)."""
    a = np.asarray(inter, float); a = a[np.argsort(a[:,0])]
    a = resample(a, 1.0)                    # uniform grid -> stable gradient
    vr = np.gradient(a[:,3], a[:,0])
    k = int(np.argmin(vr)); kb = k
    while kb+1 < len(a) and vr[kb] < -2.0: kb += 1
    return float(a[kb,0])

def dive_time(inter):
    """Time of the recovering-dive trough (deepest local min followed by a >30 m climb)."""
    a = inter[np.argsort(inter[:,0])]; x = a[:,0]; U = a[:,3]
    cand = [i for i in range(1, len(U)-1) if U[i] <= U[i-1] and U[i] <= U[i+1]
            and len(U[(x > x[i]) & (x <= x[i]+60)]) and U[(x > x[i]) & (x <= x[i]+60)].max()-U[i] > 30]
    return a[min(cand, key=lambda i: U[i]) if cand else int(np.argmin(U)), 0]

def cpa(inter, targ, max_gap=2.0):
    """Closest approach at REAL interceptor samples (truth interp only across small
    gaps) -> honest CPA, not an interpolation artefact across the telemetry dropout."""
    I, Tg = condition(inter), condition(targ)
    tt = Tg[:, 0]; best = (None, np.inf, None, None)
    for r in I:
        t = r[0]
        j = np.searchsorted(tt, t)
        lo, hi = max(j-1,0), min(j, len(tt)-1)
        if abs(tt[lo]-t) > max_gap and abs(tt[hi]-t) > max_gap: continue
        pos = np.array([np.interp(t, tt, Tg[:,k]) for k in (1,2,3)])
        d = np.linalg.norm(r[1:4] - pos)
        if d < best[1]: best = (t, d, r[1:4], pos)
    return best   # t_cpa, d_cpa, inter_pos@cpa, targ_pos@cpa

def _win(a, t0, t1):
    a = np.asarray(a, float); return a[(a[:,0] >= t0) & (a[:,0] <= t1)]

def _leader(fig, p, v, color, name, scene=False):
    q = p + v * LEAD_S
    if scene:
        fig.add_trace(go.Scatter3d(x=[p[0],q[0]], y=[p[1],q[1]], z=[p[2],q[2]], mode="lines",
            line=dict(color=color, width=7), showlegend=False, hoverinfo="skip"))
    else:
        fig.add_annotation(x=q[0], y=q[1], ax=p[0], ay=p[1], xref="x", yref="y",
            axref="x", ayref="y", showarrow=True, arrowhead=2, arrowsize=1.3,
            arrowwidth=3, arrowcolor=color, text="")

def action_box(objs, cpainfo, margin=45.0, R=280.0):
    """Tight box around the TARGET movement + CPA + nearby interceptor, so the view zooms
    on the interaction and skips the interceptor's long approach leg. Robust percentiles
    so a track glitch (a 'massive jump') can't blow up the extent."""
    tc, dc, ip, tp = cpainfo
    ctr = 0.5*(ip+tp) if ip is not None else None
    pts = []
    for o in objs:
        a = o["arr"]
        if not len(a) or o.get("marker_only"): continue
        if "interceptor" in o["name"].lower() and ctr is not None:
            d = np.linalg.norm(a[:,1:4]-ctr, axis=1); sel = a[d <= R, 1:4]
            if len(sel): pts.append(sel)
        else:
            pts.append(a[:,1:4])
    if ip is not None: pts.append(np.array([ip, tp]))
    P = np.vstack(pts) if pts else np.vstack([o["arr"][:,1:4] for o in objs])
    return np.percentile(P, 2, axis=0) - margin, np.percentile(P, 98, axis=0) + margin

def _clip(a, lo, hi, dims=3, jump=60.0):
    """Contiguous in-box segments of a, broken where it exits the box or jumps (glitch)."""
    ins = (a[:,1]>=lo[0])&(a[:,1]<=hi[0])&(a[:,2]>=lo[1])&(a[:,2]<=hi[1])
    if dims > 2: ins &= (a[:,3]>=lo[2])&(a[:,3]<=hi[2])
    seg=[]; prev=None
    for i in range(len(a)):
        if not ins[i]:
            if seg: yield np.array(seg); seg=[]
            prev=None; continue
        if prev is not None and np.linalg.norm(a[i,1:4]-a[prev,1:4]) > jump:
            if seg: yield np.array(seg); seg=[]
        seg.append(a[i]); prev=i
    if seg: yield np.array(seg)

def _inbox(p, lo, hi, dims=3):
    ok = lo[0] <= p[0] <= hi[0] and lo[1] <= p[1] <= hi[1]
    return ok and (dims < 3 or lo[2] <= p[2] <= hi[2])

def topdown(objs, cpainfo, title, t0=None):
    objs = [o if o.get("marker_only") else {**o, "arr": condition(o["arr"])}
            for o in objs]
    """2-D E-N, equal aspect, zoomed to the action box (far approach leg clipped)."""
    fig = go.Figure(); lo, hi = action_box(objs, cpainfo)
    for o in objs:
        a = o["arr"]; c = o["color"]
        if o.get("marker_only"):
            if len(a) and _inbox([a[0,1],a[0,2]], lo, hi, 2):
                fig.add_trace(go.Scatter(x=a[:,1], y=a[:,2], mode="markers", name=o["name"],
                    marker=dict(color=c, size=13, symbol="x", line=dict(width=2)),
                    hovertemplate=o["name"]+"<extra></extra>"))
            continue
        first = True
        for seg in _clip(a, lo, hi, dims=2):
            if len(seg) < 2: continue                      # drop isolated points
            rel = seg[:,0]-t0 if t0 is not None else seg[:,0]
            fig.add_trace(go.Scatter(x=seg[:,1], y=seg[:,2], mode="lines+markers", name=o["name"],
                legendgroup=o["name"], showlegend=first, line=dict(color=c, width=2.8),
                marker=dict(size=5, color=c),
                customdata=rel, hovertemplate=o["name"]+"<br>E %{x:.0f}  N %{y:.0f} m<br>t%{customdata:+.1f}s<extra></extra>"))
            first = False
        if o.get("leader") and len(a):
            # speed leader at the END of the drawn path (direction of travel), not mid-path
            inb = [i for i in range(len(a)) if _inbox(a[i,1:4], lo, hi, 2)]
            if inb:
                k = inb[-1]
                _leader(fig, a[k,1:4], travel_v(a, a[k,0]), c, o["name"])
        # ---- MISSING-DATA ANNOTATIONS: mark every in-window time gap > 2.5 s ----
        gk = np.where(np.diff(a[:, 0]) > 2.5)[0]
        gk = gk[np.argsort(-np.diff(a[:, 0])[gk])][:3]      # 3 biggest, avoid clutter
        gshown = False
        for gi in gk:
            p0, p1 = a[gi, 1:3], a[gi + 1, 1:3]
            if not (_inbox(a[gi, 1:4], lo, hi, 2) or _inbox(a[gi + 1, 1:4], lo, hi, 2)):
                continue
            dtg = a[gi + 1, 0] - a[gi, 0]
            fig.add_trace(go.Scatter(x=[p0[0], p1[0]], y=[p0[1], p1[1]], mode="lines",
                line=dict(color=c, width=1.2, dash="dot"), opacity=0.55,
                name="data gap", legendgroup="gaps", showlegend=False, hoverinfo="skip"))
            fig.add_annotation(x=(p0[0] + p1[0]) / 2, y=(p0[1] + p1[1]) / 2,
                text=f"⚠ {dtg:.0f}s gap", showarrow=True, arrowhead=0, ax=0, ay=-26,
                font=dict(size=11, color="#8a4b08"),
                bgcolor="rgba(255,244,222,0.9)", bordercolor="#d9a23c", borderwidth=1)
            gshown = True
        if gshown and not any(getattr(tr, "legendgroup", "") == "gapskey" for tr in fig.data):
            fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines",
                line=dict(color="#8a4b08", width=1.2, dash="dot"),
                name="⚠ data gap (dotted = missing samples)", legendgroup="gapskey"))
    fig.update_layout(template="plotly_white", height=580, title=title,
        margin=dict(l=60,r=20,t=70,b=50), legend=dict(orientation="h", y=1.02, yanchor="bottom"),
        xaxis=dict(title="East of antenna (m)", range=[lo[0],hi[0]], gridcolor="#9aa2ad", gridwidth=1.4),
        yaxis=dict(title="North of antenna (m)", scaleanchor="x", scaleratio=1, range=[lo[1],hi[1]],
                   gridcolor="#9aa2ad", gridwidth=1.4))
    return fig

def view3d(objs, cpainfo, title):
    fig = go.Figure()
    for o in objs:
        a = o["arr"]; c = o["color"]
        if o.get("marker_only"):
            fig.add_trace(go.Scatter3d(x=a[:,1], y=a[:,2], z=a[:,3], mode="markers", name=o["name"],
                marker=dict(color=c, size=5, symbol="x"))); continue
        fig.add_trace(go.Scatter3d(x=a[:,1], y=a[:,2], z=a[:,3], mode="lines+markers", name=o["name"],
            line=dict(color=c, width=5), marker=dict(size=2, color=c),
            hovertemplate=o["name"]+"<br>E %{x:.0f} N %{y:.0f} U %{z:.0f} m<extra></extra>"))
        if o.get("leader") and len(a): _leader(fig, a[-1,1:4], _v(a)[-1], c, o["name"], scene=True)
    tc, dc, ip, tp = cpainfo
    if ip is not None:
        fig.add_trace(go.Scatter3d(x=[ip[0],tp[0]], y=[ip[1],tp[1]], z=[ip[2],tp[2]], mode="lines+markers",
            line=dict(color="#111", width=4, dash="dot"), marker=dict(size=4, color="#111"), name=f"CPA {dc:.0f} m"))
    fig.update_layout(template="plotly_white", height=560, title=title,
        margin=dict(l=0,r=0,t=70,b=0), legend=dict(orientation="h", y=1.02, yanchor="bottom"),
        scene=dict(xaxis_title="East (m)", yaxis_title="North (m)", zaxis_title="Up (m)", aspectmode="data"))
    return fig

def geometry(inter, targ):
    """Per interceptor sample: heading error (interceptor velocity vs line-of-sight to
    target, 0°=straight at it), closing speed (+=closing), separation. Gap-aware."""
    if len(inter) < 2 or len(targ) < 2: return np.zeros((0,4))
    I, Tg = declutter(inter), declutter(targ)
    if len(I) < 2 or len(Tg) < 2: return np.zeros((0,4))
    Iv, Tv = _v(I), _v(Tg); tt = Tg[:, 0]; rows = []; last = None
    for i, r in enumerate(I):
        t = r[0]; j = np.searchsorted(tt, t); lo, hi = max(j-1,0), min(j,len(tt)-1)
        if abs(tt[lo]-t) > 2 and abs(tt[hi]-t) > 2: continue
        tp = np.array([np.interp(t, tt, Tg[:,k]) for k in (1,2,3)])
        tv = np.array([np.interp(t, tt, Tv[:,k]) for k in range(3)])
        los = tp - r[1:4]; L = np.linalg.norm(los)
        iv = Iv[i]; ivn = np.linalg.norm(iv)
        if L < 1e-6 or ivn < 1e-6: continue
        head = np.degrees(np.arccos(np.clip(np.dot(iv/ivn, los/L), -1, 1)))
        closing = float(np.dot(iv - tv, los/L))
        if last is not None and t-last > 2.5: rows.append((np.nan,)*4)   # break line across gaps
        rows.append((t, head, closing, L)); last = t
    return np.array(rows) if rows else np.zeros((0,4))

GRID = "#b6bcc6"
def main_cluster(arr, t0, gap=4.0):
    """Trim a (t,...) sample array to the contiguous cluster (gaps > gap split)
    containing t0 (or nearest to it). Kills isolated feed-hole orphans that
    stretch CPA-window plots (8/27 eng-1 stray points at t-17s)."""
    a = np.asarray(arr, float)
    if len(a) < 2: return a
    cuts = np.where(np.diff(a[:, 0]) > gap)[0]
    segs = np.split(np.arange(len(a)), cuts + 1)
    def score(ix):
        lo, hi = a[ix[0], 0], a[ix[-1], 0]
        return 0.0 if lo <= t0 <= hi else min(abs(lo - t0), abs(hi - t0))
    best = min(segs, key=score)
    return a[best]


def _guidance_panel(series, xlabel, t0, col, ytitle, title):
    fig = go.Figure()
    for s in series:
        G = geometry(main_cluster(condition(s["inter"]), t0), condition(s["targ"]))
        if not len(G): continue
        fig.add_trace(go.Scatter(x=G[:,0]-t0, y=G[:,col], name=s["name"],
            mode="lines+markers", line=dict(color=s["color"], width=2.6), marker=dict(size=5),
            connectgaps=False,
            hovertemplate="t%{x:+.1f}s<br>%{y:.1f}<extra>"+s["name"]+"</extra>"))
    fig.add_vline(x=0, line=dict(color="#111", width=1.4, dash="dash"))
    fig.update_yaxes(title_text=ytitle, gridcolor=GRID, zeroline=False)
    fig.update_xaxes(title_text=xlabel, gridcolor=GRID, zeroline=False)
    fig.update_layout(template="plotly_white", height=380, font=dict(size=13),
        title=dict(text=title, font=dict(size=14)),
        margin=dict(l=68, r=26, t=64, b=54), plot_bgcolor="white",
        legend=dict(orientation="h", y=1.14, yanchor="bottom", x=0.5, xanchor="center",
                    bgcolor="rgba(0,0,0,0)"))
    return fig


def closing_speed_fig(series, xlabel, t0):
    """Closing speed only (+ = closing on target)."""
    fig = _guidance_panel(series, xlabel, t0, 2, "closing (m/s)",
                          "Closing speed  (+ = closing on target)")
    fig.add_hline(y=0, line=dict(color="#9aa0a6", dash="dash"))
    return fig


def heading_fig(series, xlabel, t0):
    """Heading error vs line-of-sight only (0 deg = pointed straight at target)."""
    fig = _guidance_panel(series, xlabel, t0, 1, "heading err (°)",
                          "Heading error vs line-of-sight  (0° = pointed straight at target)")
    fig.add_hline(y=30, line=dict(color="#c0392b", dash="dot"),
                  annotation_text="30° on-target", annotation_position="top right",
                  annotation_font_size=11)
    fig.update_yaxes(rangemode="tozero")
    return fig


def guidance_fig(series, xlabel, t0):
    """series: list of dict(name,color,inter,targ). Two clean panels: heading error vs LOS
    (0°=on target) and closing speed. Directly answers 'were we going the right way'."""
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.14,
        subplot_titles=("Heading error vs line-of-sight  (0° = pointed straight at target)",
                        "Closing speed  (+ = closing on target)"))
    for s in series:
        G = geometry(main_cluster(condition(s["inter"]), t0), condition(s["targ"]))
        if not len(G): continue
        x = G[:,0]-t0
        fig.add_trace(go.Scatter(x=x, y=G[:,1], name=s["name"], legendgroup=s["name"],
            mode="lines+markers", line=dict(color=s["color"], width=2.6), marker=dict(size=5),
            connectgaps=False, hovertemplate="t%{x:+.1f}s<br>heading err %{y:.1f}°<extra></extra>"), row=1, col=1)
        fig.add_trace(go.Scatter(x=x, y=G[:,2], name=s["name"], legendgroup=s["name"], showlegend=False,
            mode="lines+markers", line=dict(color=s["color"], width=2.6), marker=dict(size=5),
            connectgaps=False, hovertemplate="t%{x:+.1f}s<br>closing %{y:.1f} m/s<extra></extra>"), row=2, col=1)
    fig.add_hline(y=30, line=dict(color="#c0392b", dash="dot"), row=1, col=1,
                  annotation_text="30° on-target", annotation_position="top right", annotation_font_size=11)
    fig.add_hline(y=0, line=dict(color="#9aa0a6", dash="dash"), row=2, col=1)
    fig.add_vline(x=0, line=dict(color="#111", width=1.4, dash="dash"))
    fig.update_yaxes(title_text="heading err (°)", rangemode="tozero", gridcolor=GRID, zeroline=False, row=1, col=1)
    fig.update_yaxes(title_text="closing (m/s)", gridcolor=GRID, zeroline=False, row=2, col=1)
    fig.update_xaxes(gridcolor=GRID, zeroline=False, row=1, col=1)
    fig.update_xaxes(title_text=xlabel, gridcolor=GRID, zeroline=False, row=2, col=1)
    fig.update_layout(template="plotly_white", height=600, font=dict(size=13),
        margin=dict(l=68, r=26, t=92, b=54), plot_bgcolor="white",
        legend=dict(orientation="h", y=1.16, yanchor="bottom", x=0.5, xanchor="center", bgcolor="rgba(0,0,0,0)"))
    fig.update_annotations(font_size=13)
    return fig

def nosedive_fig(inter, tc, title, ground=None, trk=None, inter_label="interceptor truth"):
    """Interceptor altitude (Up) + vertical rate vs time-from-CPA, annotating the dive
    trough and the recovery. If trk (t,Up) is given, overlays the radar track's altitude
    so you can see whether the track was anywhere near the interceptor during the dive."""
    from plotly.subplots import make_subplots
    a = inter[np.argsort(inter[:,0])]; x = a[:,0]-tc; U = a[:,3]; vU = _v(a)[:,2]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.09,
        subplot_titles=("interceptor altitude — Up about antenna (m)", "vertical rate (m/s, − = diving)"))
    fig.add_trace(go.Scatter(x=x, y=U, mode="lines+markers", name=inter_label,
        line=dict(color=INTER, width=2.6), marker=dict(size=4),
        hovertemplate="t%{x:+.0f}s<br>Up %{y:.0f} m<extra></extra>"), row=1, col=1)
    if trk is not None and len(trk):
        fig.add_trace(go.Scatter(x=trk[:,0]-tc, y=trk[:,1], mode="markers", name="radar track of interceptor (altitude)",
            marker=dict(color=TRACK, size=6, symbol="x", line=dict(width=1)),
            hovertemplate="t%{x:+.0f}s<br>track Up %{y:.0f} m<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=vU, mode="lines", name="vertical rate",
        line=dict(color=INTER, width=2, dash="dot"),
        hovertemplate="t%{x:+.0f}s<br>%{y:.0f} m/s<extra></extra>"), row=2, col=1)
    # the RECOVERING dive: deepest local min followed by a climb >30 m (not the final landing)
    cand = [i for i in range(1, len(U)-1) if U[i] <= U[i-1] and U[i] <= U[i+1]
            and len(U[(x > x[i]) & (x <= x[i]+60)]) and U[(x > x[i]) & (x <= x[i]+60)].max()-U[i] > 30]
    imin = min(cand, key=lambda i: U[i]) if cand else int(np.argmin(U))
    fig.add_annotation(x=x[imin], y=U[imin], text=f"dive to {U[imin]:.0f} m", showarrow=True,
        arrowhead=2, ay=34, row=1, col=1, font=dict(color=INTER, size=12))
    mask = (x > x[imin]) & (x <= x[imin]+60)
    if mask.any():
        idx = np.where(mask)[0]; rmax = idx[int(np.argmax(U[idx]))]
        if U[rmax]-U[imin] > 15:
            fig.add_annotation(x=x[rmax], y=U[rmax], text=f"recovered to {U[rmax]:.0f} m", showarrow=True,
                arrowhead=2, ay=-30, row=1, col=1, font=dict(color="#0f9d58", size=12))
    if ground is not None:
        fig.add_hline(y=ground, line=dict(color="#a1887f", dash="dash"), row=1, col=1,
                      annotation_text="ground", annotation_position="bottom left")
    fig.add_vline(x=0, line=dict(color="#111", dash="dash"))
    fig.update_yaxes(title="Up (m)", gridcolor=GRID, zeroline=False, row=1, col=1)
    fig.update_yaxes(title="m/s", gridcolor=GRID, zeroline=False, row=2, col=1)
    fig.update_xaxes(gridcolor=GRID, zeroline=False, row=1, col=1)
    fig.update_xaxes(title="time from nosedive (s)", gridcolor=GRID, zeroline=False, row=2, col=1)
    fig.update_layout(template="plotly_white", height=620, font=dict(size=13), plot_bgcolor="white",
        margin=dict(l=66,r=26,t=96,b=52),
        legend=dict(orientation="h", y=1.15, yanchor="bottom", x=0.5, xanchor="center", bgcolor="rgba(0,0,0,0)"))
    fig.update_annotations(font_size=13)
    return fig

def side_profile(inter, tn, title, ground=None, inter_label="interceptor truth"):
    """SIDE elevation view: interceptor altitude (Up) vs horizontal distance along the
    path (0 = nosedive). Shows the dive-and-recover geometry from the side."""
    a = inter[np.argsort(inter[:,0])]
    if len(a) < 2: return go.Figure()
    arc = np.r_[0, np.cumsum(np.hypot(np.diff(a[:,1]), np.diff(a[:,2])))]
    arc = arc - np.interp(tn, a[:,0], arc)
    U = a[:,3]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=arc, y=U, mode="lines+markers", name=inter_label,
        line=dict(color=INTER, width=2.8), marker=dict(size=4),
        hovertemplate="downrange %{x:.0f} m<br>Up %{y:.0f} m<extra></extra>"))
    cand = [i for i in range(1, len(U)-1) if U[i] <= U[i-1] and U[i] <= U[i+1]
            and len(U[i+1:]) and U[i+1:].max()-U[i] > 30]
    imin = min(cand, key=lambda i: U[i]) if cand else int(np.argmin(U))
    fig.add_annotation(x=arc[imin], y=U[imin], text=f"dive to {U[imin]:.0f} m", showarrow=True,
        arrowhead=2, ay=36, font=dict(color=INTER, size=12))
    post = [i for i in range(imin+1, len(U))]
    if post:
        rmax = max(post, key=lambda i: U[i])
        if U[rmax]-U[imin] > 15:
            fig.add_annotation(x=arc[rmax], y=U[rmax], text=f"recovered to {U[rmax]:.0f} m", showarrow=True,
                arrowhead=2, ay=-30, font=dict(color="#0f9d58", size=12))
    if ground is not None:
        fig.add_hline(y=ground, line=dict(color="#a1887f", dash="dash"),
                      annotation_text="ground", annotation_position="bottom left")
    fig.add_vline(x=0, line=dict(color="#111", width=1.4, dash="dash"),
                  annotation_text="nosedive", annotation_position="top")
    fig.update_layout(template="plotly_white", height=470, font=dict(size=13), plot_bgcolor="white",
        margin=dict(l=66,r=26,t=40,b=54),
        legend=dict(orientation="h", y=1.03, yanchor="bottom", x=0.5, xanchor="center", bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(title="horizontal distance along path from nosedive (m)", gridcolor=GRID, zeroline=False),
        yaxis=dict(title="altitude AGL (m)", gridcolor=GRID, zeroline=False))
    return fig

def separation_fig(inter, targ, t0, events=None, title=""):
    """Horizontal separation interceptor<->target-track vs time, with event markers.
    Answers 'were they closing?' — shows dive -> closing run -> pass -> egress."""
    I = declutter(np.asarray(inter, float)); Tg = declutter(np.asarray(targ, float))
    t0a, t1a = max(I[0,0], Tg[0,0]), min(I[-1,0], Tg[-1,0])
    g = np.arange(t0a, t1a, 1.0)
    ie = np.interp(g, I[:,0], I[:,1]); iN = np.interp(g, I[:,0], I[:,2])
    te = np.interp(g, Tg[:,0], Tg[:,1]); tN = np.interp(g, Tg[:,0], Tg[:,2])
    sep = np.hypot(ie-te, iN-tN)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=g-t0, y=sep, mode="lines", name="horizontal separation",
        line=dict(color="#5c6bc0", width=2.8),
        hovertemplate="t%{x:+.0f}s<br>sep %{y:.0f} m<extra></extra>"))
    k = int(np.argmin(sep))
    fig.add_trace(go.Scatter(x=[g[k]-t0], y=[sep[k]], mode="markers+text",
        marker=dict(color="#5c6bc0", size=13, symbol="star", line=dict(width=1, color="white")),
        text=[f"  pass {sep[k]:.0f} m"], textposition="top right", textfont=dict(size=12), showlegend=False))
    for tv, lab in (events or []):
        if t0a <= tv <= t1a:
            fig.add_vline(x=tv-t0, line=dict(color="#111", width=1.4, dash="dash"),
                          annotation_text=lab, annotation_position="top")
    fig.update_layout(template="plotly_white", height=430, font=dict(size=13), plot_bgcolor="white",
        title=title, margin=dict(l=66,r=26,t=60,b=54), showlegend=False,
        xaxis=dict(title="time from dive bottom (s)", gridcolor="#c7ccd4", zeroline=False),
        yaxis=dict(title="separation (m)", rangemode="tozero", gridcolor="#c7ccd4", zeroline=False))
    return fig

def err_window_fig(trkV, truth, t_mark, pre=12.0, post=12.0, mark_label="CPA",
                   title="Track metrics in this window"):
    """COMMON az/el/range/alt error window (engagement AND tracking use this one).
    trkV: 13-col t,E,N,U,vE,vN,vU,sigE,sigN,sigU,assoc,state,lu · truth: (t,E,N,U).
    Plotly: dynamic axes (robust range, nothing silently clipped -> no fake gaps),
    responsive sizing, units on every stat. Filter ±1σ shaded, ±3σ dotted."""
    from plotly.subplots import make_subplots
    T = condition(truth)
    V = np.asarray(trkV, float)
    V = V[np.argsort(V[:, 0])]
    V = V[(V[:, 0] >= t_mark - pre) & (V[:, 0] <= t_mark + post)]
    if len(V) < 4 or len(T) < 4: return None
    tt = T[:, 0]
    tp = np.column_stack([np.interp(V[:, 0], tt, T[:, k]) for k in (1, 2, 3)])
    ok = np.ones(len(V), bool)          # no truth interp across >2.5 s truth gaps
    for i, t in enumerate(V[:, 0]):
        j = np.searchsorted(tt, t); lo, hi = max(j - 1, 0), min(j, len(tt) - 1)
        if abs(tt[lo] - t) > 2.5 and abs(tt[hi] - t) > 2.5: ok[i] = False
    V, tp = V[ok], tp[ok]
    if len(V) < 4: return None
    rh, rht = np.hypot(V[:, 1], V[:, 2]), np.hypot(tp[:, 0], tp[:, 1])
    az = np.degrees(np.arctan2(V[:, 1], V[:, 2]) - np.arctan2(tp[:, 0], tp[:, 1]))
    az = (az + 180) % 360 - 180
    el = np.degrees(np.arctan2(V[:, 3], rh) - np.arctan2(tp[:, 2], rht))
    rng = np.linalg.norm(V[:, 1:4], axis=1) - np.linalg.norm(tp, axis=1)
    alt = V[:, 3] - tp[:, 2]
    sh = np.hypot(V[:, 7], V[:, 8])
    sig = {"az": np.degrees(sh / np.maximum(rh, 50)) / np.sqrt(2),
           "el": np.degrees(V[:, 9] / np.maximum(rh, 50)),
           "rng": sh / np.sqrt(2), "alt": V[:, 9]}
    rows = [("az", az, "az err (deg)", "°"), ("el", el, "el err (deg)", "°"),
            ("rng", rng, "range err (m)", " m"), ("alt", alt, "alt err (m)", " m")]
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.05)
    RED, BAND = "#b02a26", "rgba(233,179,176,0.5)"
    x = [datetime.fromtimestamp(t, LOCTZ) for t in V[:, 0]] if False else list(V[:, 0] - t_mark)
    for ri, (key, y, lab, unit) in enumerate(rows, start=1):
        sgm = sig[key]
        fig.add_trace(go.Scatter(x=x, y=y - sgm, mode="lines", line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"), row=ri, col=1)
        fig.add_trace(go.Scatter(x=x, y=y + sgm, mode="lines", line=dict(width=0),
                                 fill="tonexty", fillcolor=BAND,
                                 name="filter ±1σ", showlegend=(ri == 1),
                                 hoverinfo="skip"), row=ri, col=1)
        for sgn in (-3, 3):
            fig.add_trace(go.Scatter(x=x, y=y + sgn * sgm, mode="lines",
                                     line=dict(color="#d98984", width=1, dash="dot"),
                                     name="±3σ", showlegend=(ri == 1 and sgn == 3),
                                     hoverinfo="skip"), row=ri, col=1)
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines+markers", name="track",
                                 showlegend=(ri == 1), connectgaps=False,
                                 line=dict(color=RED, width=2.2), marker=dict(size=4),
                                 hovertemplate="t%{x:+.1f}s  %{y:.2f}" + unit
                                               + "<extra>" + lab + "</extra>"), row=ri, col=1)
        m = np.isfinite(y)
        band = np.concatenate([y[m] - 1.5 * sgm[m], y[m] + 1.5 * sgm[m]])
        ylo, yhi = np.percentile(band, 1), np.percentile(band, 99)
        pad = 0.12 * max(yhi - ylo, 1e-3)
        fig.update_yaxes(title_text=lab, range=[ylo - pad, yhi + pad],
                         gridcolor=GRID, zeroline=False, row=ri, col=1)
        fig.add_hline(y=0, line=dict(color="#9aa0a6", dash="dash", width=1), row=ri, col=1)
        fig.add_annotation(x=0.995, y=0.06, xref="x domain", yref="y domain",
                           row=ri, col=1, showarrow=False, xanchor="right",
                           font=dict(size=15),
                           text=f"<b>track {np.nanmean(y):+.2f}{unit.strip()} ± "
                                f"{np.nanstd(y):.2f}{unit.strip()}</b>")
    fig.add_vline(x=0, line=dict(color="#111", width=1.5, dash="dash"),
                  annotation_text=mark_label, annotation_position="top right")
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_xaxes(title_text=f"time from {mark_label} (s)", row=4, col=1)
    fig.update_layout(template="plotly_white", height=880, font=dict(size=13),
                      title=dict(text=title, font=dict(size=14)),
                      margin=dict(l=70, r=26, t=70, b=54), plot_bgcolor="white",
                      legend=dict(orientation="h", y=1.05, yanchor="bottom",
                                  x=0.5, xanchor="center", bgcolor="rgba(0,0,0,0)"))
    return fig


def gif3d(objs, cpainfo, out_path, title, t0, nframes=80, fps=9, zlabel="Up (m)"):
    """Clear animated 3-D GIF: trailing paths + real-sample dots + velocity arrows
    (quiver) + line-of-sight to each target + live heading-error/closing readout so the
    'are we pointed at the target' question is visible. Gentle rotation for depth."""
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image
    objs = [o if o.get("marker_only") else {**o, "arr": condition(o["arr"], resample_dt=0.25)}
            for o in objs]
    mv = [o for o in objs if not o.get("marker_only") and len(o["arr"]) >= 2]
    inter = next((o for o in mv if "interceptor" in o["name"].lower()), None)
    targs = [o for o in mv if o is not inter]
    tmin = min(o["arr"][:,0].min() for o in mv); tmax = max(o["arr"][:,0].max() for o in mv)
    g = np.linspace(tmin, tmax, nframes)
    lo, hi = action_box(objs, cpainfo)               # zoom to the interaction, skip the approach leg / glitch
    xl, yl, zl = (lo[0],hi[0]), (lo[1],hi[1]), (lo[2],hi[2])
    tc, dc, ip, tp = cpainfo
    def P(a,t):
        if t < a[0,0]: return None
        t = min(t, a[-1,0])                 # clamp: keep the marker/arrow through hold frames
        return np.array([np.interp(t,a[:,0],a[:,k]) for k in (1,2,3)])
    def V(a,t):
        # direction of TRAVEL from the path (robust to bad stored vertical velocity on tracks)
        dt=1.5; t1=min(t+dt,a[-1,0]); t0=max(t-dt,a[0,0]); s=t1-t0
        if s<=0.1: return np.zeros(3)
        return (np.array([np.interp(t1,a[:,0],a[:,k]) for k in (1,2,3)]) -
                np.array([np.interp(t0,a[:,0],a[:,k]) for k in (1,2,3)]))/s
    frames = []
    for fi, gt in enumerate(g):
        fig = plt.figure(figsize=(8.2,6.6), dpi=100); ax = fig.add_subplot(111, projection="3d")
        ax.view_init(elev=24, azim=-58 + 18*(fi/max(nframes-1,1)))
        for o in objs:
            a = o["arr"]; c = o["color"]
            if not o.get("marker_only") and len(a) == 0:
                continue
            if o.get("marker_only"):
                if len(a) and _inbox(a[0,1:4], lo, hi, 3):
                    ax.scatter(a[:,1], a[:,2], a[:,3], color=c, marker="X", s=95, edgecolors="k", linewidths=1, label=o["name"])
                continue
            if o.get("static"):                                    # reference (e.g. target track) — FULL path every frame
                ax.plot([], [], [], color=c, lw=2.6, label=o["name"])
                for seg in _clip(a, lo, hi, dims=3):
                    if len(seg) < 2: continue
                    ax.plot(seg[:,1], seg[:,2], seg[:,3], color=c, lw=2.4)
                    ax.scatter(seg[::2,1], seg[::2,2], seg[::2,3], color=c, s=16, alpha=0.75)
                continue
            ax.plot([], [], [], color=c, lw=2.8, label=o["name"])   # stable legend entry
            hist = a[a[:,0] <= gt]
            for seg in _clip(hist, lo, hi, dims=3):                 # in-box, glitch-broken
                if len(seg) < 2: continue                           # drop isolated (noisy) points
                ax.plot(seg[:,1], seg[:,2], seg[:,3], color=c, lw=2.8)
                ax.scatter(seg[::2,1], seg[::2,2], seg[::2,3], color=c, s=12, alpha=0.55)
            p = P(a, gt)
            if p is not None and _inbox(p, lo, hi, 3):
                ax.scatter(*p, color=c, s=80, edgecolors="k", linewidths=1.1, zorder=6)
                v = V(a, gt)
                if np.linalg.norm(v) > 0.1:
                    vv = v*LEAD_S
                    ax.quiver(*p, *vv, color=c, lw=2.8, arrow_length_ratio=0.32, zorder=7)
        # heading-error readout only — no drawn LOS/CPA line (it read as the target chasing the interceptor)
        lines = [f"t {gt-t0:+.1f} s"]
        if inter is not None:
            pi = P(inter["arr"], gt); vi = V(inter["arr"], gt)
            for to in targs:
                pt = P(to["arr"], gt)
                if pi is None or pt is None: continue
                los = pt - pi; L = np.linalg.norm(los)
                lab = "truth" if "truth" in to["name"].lower() else ("track" if "track" in to["name"].lower() else to["name"])
                if L > 1e-3 and np.linalg.norm(vi) > 0.1:
                    he = np.degrees(np.arccos(np.clip(np.dot(vi/np.linalg.norm(vi), los/L), -1, 1)))
                    lines.append(f"→ {lab}: {L:4.0f} m   heading err {he:3.0f}°")
        ax.text2D(0.02, 0.98, "\n".join(lines), transform=ax.transAxes, va="top", ha="left",
                  family="monospace", fontsize=10,
                  bbox=dict(boxstyle="round", fc="white", ec="#bbb", alpha=0.88))
        ax.set_xlim(*xl); ax.set_ylim(*yl); ax.set_zlim(*zl)
        ax.set_xlabel("East (m)", fontsize=10); ax.set_ylabel("North (m)", fontsize=10); ax.set_zlabel(zlabel, fontsize=10)
        ax.tick_params(labelsize=8)
        ax.set_title(title, fontsize=13, fontweight="bold")
        ax.legend(loc="upper right", fontsize=9, framealpha=0.9)
        fig.tight_layout()
        fig.canvas.draw(); w,h = fig.canvas.get_width_height()
        buf = np.frombuffer(fig.canvas.buffer_rgba(), dtype=np.uint8).reshape(h,w,4)[...,:3].copy()
        frames.append(buf); plt.close(fig)
    frames += [frames[-1]]*int(fps*1.2)  # hold CPA/end
    imgs = [Image.fromarray(f) for f in frames]
    imgs[0].save(out_path, save_all=True, append_images=imgs[1:],
                 duration=int(1000/fps), loop=0, disposal=2, optimize=True)
    return out_path

def closing(pairs, xlabel, t0):
    """pairs: list of dict(name,color,inter,targ). Separation vs time, min marked, gap-broken."""
    fig = go.Figure()
    for p in pairs:
        I, Tg = main_cluster(condition(p["inter"]), t0), condition(p["targ"])
        if len(I) < 1 or len(Tg) < 1: continue
        tt = Tg[:,0]; ts, ds = [], []; last = None
        for r in I:
            j = np.searchsorted(tt, r[0]); lo,hi = max(j-1,0), min(j,len(tt)-1)
            if abs(tt[lo]-r[0])>2.0 and abs(tt[hi]-r[0])>2.0: continue
            pos = np.array([np.interp(r[0], tt, Tg[:,k]) for k in (1,2,3)])
            if last is not None and r[0]-last > 2.5: ts.append(np.nan); ds.append(np.nan)
            ts.append(r[0]-t0); ds.append(np.linalg.norm(r[1:4]-pos)); last = r[0]
        if not ts: continue
        ts, ds = np.array(ts), np.array(ds)
        k = int(np.nanargmin(ds))
        fig.add_trace(go.Scatter(x=ts, y=ds, mode="lines+markers", name=p["name"], connectgaps=False,
            line=dict(color=p["color"], width=2.6), marker=dict(size=5),
            hovertemplate=p["name"]+"<br>t%{x:+.1f}s  sep %{y:.0f} m<extra></extra>"))
        fig.add_trace(go.Scatter(x=[ts[k]], y=[ds[k]], mode="markers+text",
            marker=dict(color=p["color"], size=13, symbol="star", line=dict(width=1, color="white")),
            text=[f"  {ds[k]:.0f} m"], textposition="middle right", textfont=dict(size=12), showlegend=False,
            hovertemplate=f"min {ds[k]:.0f} m<extra></extra>"))
    fig.add_vline(x=0, line=dict(color="#111", width=1.4, dash="dash"))
    fig.update_layout(template="plotly_white", height=420, font=dict(size=13),
        margin=dict(l=66,r=26,t=30,b=54), plot_bgcolor="white",
        legend=dict(orientation="h", y=1.03, yanchor="bottom", x=0.5, xanchor="center", bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(title=xlabel, gridcolor=GRID, zeroline=False),
        yaxis=dict(title="separation (m)", rangemode="tozero", gridcolor=GRID, zeroline=False))
    return fig
