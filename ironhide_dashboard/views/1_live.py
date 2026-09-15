"""LIVE — single-screen engagement view, auto-refreshing. Same code path for the
8/28 archive replay and a live MRU mongo feed (see Data source).

Layout (top → bottom): ONE top line (wordmark · crumb · live status strip
"● ARCHIVE F1 · 07:22:45 · 4× · [tgt][int] · 118 samples" · build badge) → THREE
tiles in one row (closest so far · separation · target track) → the self-updating
panel: LEFT map | RIGHT separation card over the 4-card track-quality figure (az ·
el · 3D position · alt) → the MEASUREMENT SPACE quad (truth, tracks & obs vs time) in its
own card → a collapsed "More" expander (coverage, allegiance, tracks/s, feed chips,
predicted miss).  Display controls (screen size, window, trail, map frame Fixed/Follow,
zoom, satellite, blind rings, show obs, refresh, freeze, reset view) live in the sidebar
"Display" section (app.py).  The screen preset picks the panel height (ih.liveserver.layout);
the panel JS fits the real iframe width (one / two / three columns).  The BROWSER owns the map
view: the map figure carries no axis ranges; the frame goes in the envelope ("view") and is
applied on the first paint / explicit changes only, so zoom and pan survive every tick.

Refresh / blink hygiene (ih.liveserver): Streamlit 1.63 remounts st.plotly_chart
on every spec change (its element id hashes the spec), so uirevision alone cannot
stop the flashing. The three figures are therefore rendered inside ONE st.iframe
whose HTML is CONSTANT for the browser session; the page's fragment builds the
figures in Python and pushes their JSON into liveserver.STORE[sid] each tick; the
panel polls :8902/figs.json and calls Plotly.react with a constant
layout.uirevision (zoom / pan / legend survive, no remount, no flash).

Map lag: engagement + error figures follow the clock every tick; the MAP figure
is rebuilt / re-sent only when its trail data changed AND at most every
MAP_MIN_PERIOD_S (~2 s); the vehicle heads {tgt, itc: E, N, hdg} AND their two
layout-image dicts (data coordinates, ih.plots.heads_images) go out every tick
and the panel applies them with Plotly.relayout — the icon is placed by the same
axis transform as its trail, so it cannot teleport off it; the satellite tile is
included in the map layout only when the map box changes.  "Freeze display"
stops the cadence and flags the store so the panel keeps the last picture.  If
:8902 cannot be bound the page falls back to the plain st.plotly_chart path
(stable keys, same figures, the same icon dicts inside the map).

CPA gate (ih.engine.cpa_gate): the "Closest so far" tile always shows the running
minimum; the gold ★ / hairlines / "CPA n m" labels and the gold tile appear only
once the minimum is below the sidebar gate (default 70 m) AND the pass is over.
"""
import os
import secrets
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from ih import data as D  # noqa: E402
from ih import engine as E  # noqa: E402
from ih import feed as F  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402

D.init_state()
s = st.session_state
st.markdown(T.scale_css(T.text_scale()), unsafe_allow_html=True)   # the "Text size" factor (also injected by T.setup in app.py; harmless twice)
if "_sid" not in s:
    s["_sid"] = secrets.token_hex(8)  # per-browser-session token for the panel's STORE slot
SID = s["_sid"]
MAP_MIN_PERIOD_S = 1.9   # the map figure is re-sent at most this often (ticks are 1 s: every other tick)
# LAPTOP engagement frame (1366x768): the stacked map is only 267-448 px tall there, so the 500 m default half-width put a
# 59 m pass inside ~10 px (5.99 m/px).  On the Laptop preset the engagement box may close to 250 m, and while the two
# vehicles are actually together (3D separation < LAPTOP_CLOSE_SEP_M) the box is capped at LAPTOP_CLOSE_HALF_M.
LAPTOP_MIN_HALF_M, LAPTOP_CLOSE_SEP_M, LAPTOP_CLOSE_HALF_M = 250.0, 300.0, 400.0

# ── cadence (2026-09-15) ─────────────────────────────────────────────────────
# A slow unit (MRU91: 0.8-3.4 s per truth window) cannot serve a 1 s fragment: the ticks pile up and every page
# interaction queues behind one.  The EFFECTIVE cadence is max(refresh interval, last tick + TICK_HEADROOM_S), so the
# browser only asks for a tick once the previous one could have finished; app.py's sidebar caption shows it.
OFFLINE_AFTER_S = 30.0     # live: keep serving the LAST GOOD snapshot this long before the red OFFLINE callout
TICK_HEADROOM_S = 0.5
_refresh_s = max(1.0, float(s.get("refresh_s", D.STATE_DEFAULTS.get("refresh_s", 2.0))))
_last_tick_s = float(s.get("_tick_dur_s") or 0.0)
CADENCE_S = round(max(_refresh_s, _last_tick_s + TICK_HEADROOM_S), 1)
s["_cadence_s"] = CADENCE_S
if s.get("freeze"):
    run_every = None
elif D.is_live():
    run_every = CADENCE_S
else:
    run_every = D.REPLAY_TICK_S if s.get("playing") else None
# THE FIRST LIVE FETCH BELONGS TO THE FRAGMENT TICK, NEVER TO THIS SCRIPT RUN.  A fragment body also runs inline during the
# main script run, so F.snapshot() there blocked the whole page paint (blank main area for 30-60 s after Connect, and every
# page switch / sidebar click paid for a full mongo round trip).  This flag is set by the MAIN script run only (a fragment
# rerun does not re-execute the module); live_view() pops it and serves the last snapshot instead of fetching.
def fetch_inline() -> bool:
    """TEST HOOK: True when nothing will drive the fragment's auto-rerun, so the MAIN script run has to do the fetching.
    st.fragment(run_every=…) needs a BROWSER timer; under AppTest (streamlit.testing imported, no browser) a deferred
    first fetch would never happen and every live-path pin would render an empty page.  IH_FETCH_INLINE=1 forces it for
    a bare harness, IH_FETCH_INLINE=0 forces the browser behaviour (deferred) inside AppTest."""
    env = os.environ.get("IH_FETCH_INLINE", "")
    if env:
        return env.lower() in ("1", "true", "yes")
    return "streamlit.testing" in sys.modules or bool(s.get("_ih_fetch_inline"))


# Deferred on EVERY main script run (page switch, sidebar click, widget change) AND on the first one after Connect: the
# first live fetch belongs to the fragment tick, which paints "CONNECTING… first data in a few s" in the status row while
# it runs.  fetch_inline() is the AppTest escape hatch (no browser -> no fragment tick -> the main run must fetch).
s["_live_main_run"] = bool(D.is_live()) and run_every is not None and not fetch_inline()
CHART_CFG = {"scrollZoom": True, "displayModeBar": True, "displaylogo": False}


@st.cache_resource(show_spinner=False)
def _panel_server(port: int):
    """One panel server per process (cache_resource + the module singleton inside start())."""
    return LS.start(port)


SERVER = _panel_server(LS.DEFAULT_PORT) or LS.start(LS.DEFAULT_PORT)   # second call = 30 s bind retry after a failure
LS.adopt(SERVER)                                                      # re-bind after a Streamlit module reload (see LS.adopt)
PORT = int(SERVER.server_address[1]) if SERVER is not None else None  # from the cached server, never from a resettable module global
PANEL = PORT is not None
PERIOD_MS = int(CADENCE_S * 1000) if D.is_live() else int(D.REPLAY_TICK_S * 1000)
L = LS.layout(s.get("screen"), T.text_scale())   # sidebar "Screen size" preset x "Text size" -> panel geometry / fonts / line width
UI = {"font_px": L["font_px"], "line_w": L["line_w"], "preset": L["preset"], "text_scale": L["text_scale"]}
if "meas_open" not in s:                                  # MEASUREMENT SPACE quad: collapsed by default, remembered per session + ?meas=1
    try:
        s["meas_open"] = str(st.query_params.get("meas", "")).lower() in ("1", "true", "yes")
    except Exception:
        s["meas_open"] = False
MEAS_MIN_PERIOD_S = 1.9                   # the measurement-space quad is re-sent at most this often (like the map)
BADGE = T.build_badge()
CRUMB = "Ironhide · Live · 2 / 2"         # pages: Data source (1 / 2, the landing page) · Live (2 / 2)


def _fmt(v, f="{:.0f}", none="—"):
    return none if v is None else f.format(v)


def _sig_common(A: dict, snap: dict, P: dict, W: int) -> tuple:
    # the preset / frame / zoom / view revision are part of EVERY signature: an explicit change rebuilds every figure at once
    # (and bypasses the map's 2 s re-send throttle, so the envelope's "view" never carries an old range with a new rev)
    static = P.get("frame") if P.get("frame_mode") in ("fit_flight", "fixed") else None   # a drifting engagement / follow box never forces a rebuild (the view rides the envelope)
    return (A["tgt_tid"], A.get("alt_tid"), A["itc_tid"], A["cpa"], bool(snap["ok"]), bool(snap.get("stale")), bool(A.get("has_truth", True)),
            bool(A.get("no_tgt_truth")), W,
            L["preset"], static, P.get("frame_mode"), float(P["map_half"]), int(s.get("view_rev", 0)))


def _sig_events(A: dict) -> tuple:
    """Track-change log + flash flags: re-sign the TIME-SERIES figures (handover ticks) — the map's pills ride the envelope."""
    return (len(A.get("track_events") or ()), bool(A.get("tgt_flash")), bool(A.get("itc_flash")))


FRAME_MODES = {"Engagement": "engagement", "Fit whole flight": "fit_flight", "Fixed (custom half-width)": "fixed", "Fixed": "fit_flight", "Follow": "follow"}


def _frame_mode() -> str:
    """engagement (default) | follow | fit_flight | fixed — the sidebar's frame_mode (B), else the legacy map_frame labels."""
    m = s.get("frame_mode") or FRAME_MODES.get(str(s.get("map_frame", "Engagement")), "engagement")
    return m if m in ("engagement", "follow", "fit_flight", "fixed") else "engagement"


def _clamp_half(box: tuple, half_max: float) -> tuple:
    """The same box, centre kept, each half-width capped at ``half_max`` m (never widened)."""
    x0, x1, y0, y1 = (float(v) for v in box)
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    hx, hy = min(0.5 * (x1 - x0), float(half_max)), min(0.5 * (y1 - y0), float(half_max))
    return (cx - hx, cx + hx, cy - hy, cy + hy)


def _frame(P: dict, A: dict | None = None) -> tuple | None:
    """The map frame box for the current mode: ENGAGEMENT (default) = ih.plots.engagement_box (archive: the
    passes ± 20 s + the vehicles; live: the last 120 s of both truths; padded 25 %, half-width >= 500 m);
    fit_flight = the whole flight footprint (live: the Seawall box); fixed / follow = the vehicles' centroid
    ± the sidebar half-width.  None before the first analysis (the panel keeps its view)."""
    mode = P.get("frame_mode") or _frame_mode()
    if mode == "fit_flight":
        return D.LIVE_FRAME if D.is_live() else D.flight_frame(int(s.get("flight", 1)))
    if A is None:
        return None
    if mode == "engagement":
        pt = [] if D.is_live() else [p["t"] for p in D.passes(int(s.get("flight", 1))) if p.get("verified")]
        lap = str(L.get("preset") or "") == "Laptop"
        box = PL.engagement_box({**A, "flight": int(s.get("flight", 1))}, pt, live=D.is_live(),
                                min_half=(LAPTOP_MIN_HALF_M if lap else PL.ENGAGE_MIN_HALF_M))
        sep = A.get("sep_now")
        if lap and sep is not None and float(sep) < LAPTOP_CLOSE_SEP_M:
            box = _clamp_half(box, LAPTOP_CLOSE_HALF_M)      # they are together NOW: never frame wider than 800 m across
        return box
    half = float(P["map_half"])
    heads = [A.get(k) for k in ("tgt_now", "itc_now") if A.get(k) is not None]
    if heads:
        cx = PL._quant(float(np.mean([h[D.TR["E"]] for h in heads])), D.FRAME_Q); cy = PL._quant(float(np.mean([h[D.TR["N"]] for h in heads])), D.FRAME_Q)
    else:
        cx, cy, _ = PL.map_box(A, half)
    return (cx - half, cx + half, cy - half, cy + half)


def _view(fig, P: dict) -> dict:
    """The envelope's "view": the CURRENT frame box (P["frame"], else the map's axis box) + the explicit-change
    revision + mode (fixed = apply on rev change only; follow = re-centre near the edge; engage = re-frame to
    this box near the edge)."""
    mode = {"engagement": "engage", "follow": "follow"}.get(P.get("frame_mode") or _frame_mode(), "fixed")
    box = P.get("frame")
    v = {"x0": float(box[0]), "x1": float(box[1]), "y0": float(box[2]), "y1": float(box[3])} if box is not None else PL.map_view(fig)
    return {**v, "rev": int(s.get("view_rev", 0)), "mode": mode}


def _figures(A: dict, snap: dict, P: dict, W: int) -> tuple[dict, dict]:
    """Rebuild each figure only when ITS inputs changed. Returns (figs, changed) where figs[k] is None
    for an unchanged figure (the panel keeps what it has) and changed[k] says which were rebuilt.
    The map and the measurement quad additionally wait ~2 s between rebuilds (panel path only)."""
    t_now = round(float(A["t_now"]), 3)
    common = _sig_common(A, snap, P, W)
    tt, it = A.get("tgt_trail"), A.get("itc_trail")
    tk, ik = A.get("tgt_track"), A.get("itc_track")
    box = PL.frame_box(P.get("frame"), A, float(P["map_half"]))
    n_last = lambda a: (int(len(a)) if a is not None else 0, float(a[-1, 0]) if a is not None and len(a) else 0.0)  # noqa: E731
    free = A.get("free_tracks") or {}
    static_box = box if P.get("frame_mode") in ("fit_flight", "fixed") else None   # a drifting engagement / follow box rides the envelope: never a re-send trigger
    opts = (static_box, P["trail_s"], P["show_sat"], P["show_blind"])   # display options: a change here always bypasses the map's re-send throttle
    sig = {
        "map": (common, opts, n_last(tt), n_last(it), n_last(tk), n_last(ik), (len(free), sum(int(len(a)) for a in free.values()))),
        "sep": (common, t_now, int(len(A["sep"]["t"]))),
        "err": (common, t_now, P["show_obs"], int(len(A["errors"]["t"])), int(len(A.get("errors_alt", {}).get("t", ()))), int(len(snap.get("obs", ()))), _sig_events(A)),
        "vel": (common, t_now, n_last(tk), int(len(A["errors"]["t"])), n_last(snap.get("tgt")), _sig_events(A)),
        "meas": (common, t_now, int(len(snap.get("tracks") or {})), int(len(snap.get("obs", ()))), n_last(snap.get("tgt")), _sig_events(A)),
    }
    old = s.setdefault("_fig_sig", {})
    cache = s.setdefault("_figs", {})
    changed = {k: old.get(k) != sig[k] or cache.get(k) is None for k in LS.FIG_KEYS}
    now = time.time()
    if changed["map"] and PANEL and cache.get("map") is not None and now - s.get("_map_wall", 0.0) < MAP_MIN_PERIOD_S and old.get("map", ())[:2] == (common, opts):
        changed["map"] = False                         # ONLY the trail moved, and the map was re-sent < 2 s ago: wait a tick (a frame / preset / satellite /
                                                       # rings / trail-length change is never delayed — while paused there is no next tick to catch it)
    if changed["meas"] and PANEL and cache.get("meas") is not None and now - s.get("_meas_wall", 0.0) < MEAS_MIN_PERIOD_S and old.get("meas", ())[:1] == (common,):
        changed["meas"] = False
    fp = {**P, "font_px": L["font_px"], "line_w": L["line_w"], "text_scale": L["text_scale"], "yrng_mem": s.setdefault("_yrng_mem", {})}   # per-session axis-range memory: no tick-to-tick jitter
    if changed["map"]:
        # panel path: the satellite tile travels in the envelope for the BROWSER's view (never inside the figure)
        cache["map"] = PL.map_fig(A, {**fp, "map_height": L["map"], "sat_include": not PANEL, "icons_in_fig": not PANEL})
        s["_map_wall"] = now
        old["map"] = sig["map"]
    if changed["sep"]:
        cache["sep"] = PL.separation_fig(A, {**fp, "sep_height": L["sep"]})
        old["sep"] = sig["sep"]
    if changed["err"]:
        cache["err"] = PL.error_fig(A, {**fp, "err_height": L["err"]}, window_s=float(W))
        old["err"] = sig["err"]
    if changed["vel"]:   # velocity states: truth (snapshot window) vs the target track's filtered velocity, spa-graded Δ / σ / containment
        cache["vel"] = PL.velocity_fig(A, {**fp, "vel_height": L["vel"]}, window_s=float(W), truth=snap.get("tgt"))
        old["vel"] = sig["vel"]
    if changed["meas"] and (s.get("meas_open") or not PANEL):   # collapsed quad: not built, not pushed (no background cost)
        cache["meas"] = PL.meas_fig(E.meas_space(snap, A, float(W)), A, {**fp, "meas_height": L["meas"]})
        s["_meas_wall"] = now
        old["meas"] = sig["meas"]
    elif changed["meas"]:
        changed["meas"] = False
        cache.setdefault("meas", None)
    return cache, changed


def _sw(cls: str, word: str) -> str:
    """Status WORD, no icon (2026-09-15: "remove all the random icons"): bold primary ink; the state is the word itself."""
    return f"<b>{T.esc(word)}</b>"


def _status_parts(A: dict, snap: dict, t_now: float) -> tuple[list[str], list[str]]:
    """The two DELIBERATE rows of the fixed-height status box (2026-09-15):
      row 1  `● ARCHIVE F1 · 07:22:45 · 4× · PAUSED · trk #177 ✓ CONF` (+ feed words / roles / trk/s / FROZEN / fallback)
      row 2  `CONFIRMED 230 · TENTATIVE 0 · COASTING 7 · 2.0 Hz · 228 samples`
    Row 2 is the TARGET TRACK's own published-state history over the metrics window (engine A["tgt_counts"]) in FULL
    WORDS: "trk #177 ✓ CONF · conf 230" read as "CONF CONF" on one line, and "2.0 Hz 228 samples" ran together.
    With no target track row 2 says "no target track"."""
    if D.is_live():                                                 # friendly run name (runs collection), else the run id8 — never the 36-char collection name (it wrapped the top line)
        run = s.get("live_run") or ""
        src = f'{_sw("red", "LIVE")} <b>{T.esc(s.get("live_run_name") or (run[4:12] if run else "—"))}</b>'
    else:
        src = ""                                                       # archive: the sidebar already says "8/28 · Flight 1" (row width at X-Large)
    # LAST-GOOD semantics (2026-09-15): a failed / slow tick keeps the previous picture on screen; the row says how old it is
    # and only turns OFFLINE after OFFLINE_AFTER_S seconds without data.  _pending = the first fetch has not run yet.
    down, ever = snap.get("_down_for"), bool(snap.get("_ever_ok"))
    if not snap["ok"]:
        if snap.get("_pending") and not ever:
            src += " " + _sw("amber", "CONNECTING… first data in a few s")
        elif D.is_live() and ever and down is not None and down < OFFLINE_AFTER_S:
            src += " " + _sw("amber", f"LAST DATA {D.fmt_age(down)} AGO")
        else:
            src += " " + _sw("fail", "OFFLINE")
    elif D.is_live() and snap.get("stale"):                             # ih.feed: a failed chunk / connection blip — the filled buffer is still on screen
        src += " " + _sw("amber", f"LAST DATA {D.fmt_age(down if down is not None else snap.get('data_age'))} AGO")
    elif snap.get("_pending") and D.is_live() and down is not None and down > max(CADENCE_S, F.LIVE_STALE_S):
        src += " " + _sw("amber", f"LAST DATA {D.fmt_age(down)} AGO")
    parts = ([src] if src else []) + [f"<b>{D.pdt_hms(t_now)}</b>"]
    if not D.is_live():
        # RESERVE the "· PAUSED" slot while playing (2026-09-15, Chrome layout-shift run): inserting it mid-strip pushed
        # everything after it ~109 px and at 1366x768 Large the trk chip dropped onto the second row.  The placeholder is
        # EMPTY in the DOM and paints its width from a CSS ::after, so .ih-status textContent still says PAUSED only when paused.
        if float(s["speed"]) != 1.0:                                     # "1×" says nothing; the row must fit 938 px at X-Large
            parts.append(f"{s['speed']:g}×")
    feeds = A.get("feeds", [])
    # MAVLink feed health as WORDS, and only when something is wrong (2026-09-14: "showing the picture icons here is useless")
    order = {"alive": 0, "stale": 1, "frozen": 2, "down": 3, "none": 4}
    for role, short in (("target", "TGT"), ("interceptor", "INT")):
        fs = [f for f in feeds if f["role"] == role]
        if not fs:
            if A.get("has_truth", True) and (D.is_live() or role == "target"):
                parts.append(_sw("na", f"{short} FEED NONE"))
            continue
        best = min(fs, key=lambda f: order[f["state"]])
        if best["state"] != "alive":
            age = best.get("age")
            parts.append(_sw("amber" if best["state"] == "stale" else "fail", f"{short} FEED {best['state'].upper()}" + (f" {age:.0f} s" if age is not None else "")))
    # the target track id + its STATE (CONF / TENT / COASTING; amber TRACK CHANGED for 10 s after an id change) — 2026-09-14: the three
    # banner tiles left the page ("only the target track is useful, and it is on the plot"); the state word lives here now.
    # ORDER (2026-09-15): the track block comes BEFORE "n samples" so the fixed 2-row status box still PACKS at 1366 px /
    # X-Large: source + clock + speed leave room for the long "trk #1291 TRACK CHANGED" on row 1, and the wide state
    # counter + "n samples" then share row 2.  With "n samples" ahead of it the track block was pushed onto row 2 and
    # the counter spilled into a third (clipped) row.
    if A.get("tgt_tid") is not None:
        parts.append("<b>#{}</b> {}".format(A["tgt_tid"], _sw("amber" if A.get("tgt_flash") else A["track_cls"],
                                                                         "TRACK CHANGED" if A.get("tgt_flash") else str(A["track_state"]))))
    else:
        parts.append("trk <b>—</b> " + _sw(A.get("track_cls") or "na", str(A.get("track_state") or "NO TRACK")))
    roles = getattr(D, "roles_label", None)
    try:
        lab = roles() if callable(roles) else ""
    except Exception:
        lab = ""
    if lab and lab.replace("TGT", "").replace("INT", "").replace("—", "").replace("·", "").strip():   # never the empty "TGT — · INT —" (archive / roles unset)
        parts.append(T.esc(lab))                                                                                  # "TGT mav14550_1_1 · INT mav14551_2_*"
    if D.is_live() and snap["ok"]:
        parts.append(f"<b>{A.get('tracks_per_s', 0.0):.1f}</b> trk/s")
        if not A.get("has_truth", True):
            parts.append(_sw("na", "NO TRUTH FEED"))
        age = A.get("data_age")
        if age is not None and age > F.LIVE_STALE_S:
            parts.append(f"last data <b>{D.fmt_age(age)}</b> ago")
    try:
        bf = float(snap.get("backfill_s") or 0.0)                      # ih.feed: seconds of history still to back-fill (chunked first fetch)
    except (TypeError, ValueError):
        bf = 0.0
    if bf > 0:
        parts.append(f'FILLING <b>{bf:.0f}</b> s…')                 # the history still missing, not an error: the view is already usable
    if s.get("freeze"):
        parts.append(f'{_sw("amber", "FROZEN")}')
    if not PANEL:
        parts.append(f'PANEL {_sw("amber", "FALLBACK CHARTS")}')
    # ── row 2: the target track's update counter, FULL WORDS, one part per number (the box adds the " · " separators) ──
    n_samples = int(A["errors"].get("n_graded", 0))
    c = A.get("tgt_counts") or {}
    if A.get("tgt_tid") is None:
        parts2 = []
    elif not c.get("n"):
        parts2 = []
    else:   # 2026-09-15: ONE row ("2 rows just squishes the plot") — the counter inline, compact words, numbers bold
        parts2 = ['<b class="c-ok">{}</b> confirmed'.format(c["conf"]), '<b class="c-amber">{}</b> tentative'.format(c["tent"]), '<b class="c-fail">{}</b> coasting'.format(c["coast"]), "<b>{:.1f}</b> Hz".format(c["hz"])]   # full words (user 2026-09-15)   # green / yellow / red (user 2026-09-15)
    parts.extend(parts2)                                              # ("n samples" dropped: it is the confirmed count, and the row must fit 945 px at Large)
    if not D.is_live() and not s["playing"]:
        parts.append("<b>PAUSED</b>")                                  # LAST: inserting it moves nothing before it
    return parts, []


def _tiles(A: dict) -> list[str]:
    run, cpa = A.get("cpa_run"), A["cpa"]
    if run is None:
        t1 = T.tile_html("Closest so far · 3D", "—", "", "no pair yet", tone="na")
    elif A.get("cpa_valid"):        # the running minimum passed the CPA gate: gold top rule + "CPA" status row, "CPA n m" in the sub
        t1 = T.tile_html("Closest so far · 3D", f"{run[0]:.0f}", "m", f'<span class="cpa">CPA {run[0]:.0f} m</span> · {run[4]:.0f} m horiz · {D.pdt_hms(run[1])}', tone="gold",
                         word=f"CPA · gated < {A['cpa_gate_m']:.0f} m")
    else:                           # not (yet) a CPA: muted note, plus the last validated CPA when there is one
        note = f'<span class="muted">closest so far (no CPA yet · gate {A["cpa_gate_m"]:.0f} m)</span> · {run[4]:.0f} m horiz · {D.pdt_hms(run[1])}'
        if cpa is not None:
            note += f" · last CPA <b>{cpa[0]:.0f} m</b> {D.pdt_hms(cpa[1])}"
        t1 = T.tile_html("Closest so far · 3D", f"{run[0]:.0f}", "m", note, tone="na")
    c = A["closing_now"]
    if c is None:
        closing = "closing —"
    else:
        closing = f'<span class="{"closing" if c > 0 else "opening"}">closing {c:+.0f} m/s</span>'
    t2 = T.tile_html("Separation · 3D truth-to-truth", _fmt(A["sep_now"]), "m" if A["sep_now"] is not None else "", closing, tone="")
    if A["tgt_tid"] is not None:
        sub = f"age {_fmt(A['update_age'], '{:.1f} s')} · horiz err {_fmt(A['horiz_med30'])} m"
        ch = A.get("tgt_change")
        if ch is not None:
            t_ch, _, old, _new, _rule = T.event_row(ch)
            sub += f" · prev #{old} · changed {D.pdt_hms(t_ch)}" if old is not None else f" · acquired {D.pdt_hms(t_ch)}"
        if A.get("tgt_rule"):
            sub += f" · {T.esc(str(A['tgt_rule']))}"
        # status row word = the track state itself (CONF / TENT / COASTING); a fresh id change flashes amber "CHANGED" for 10 s
        t3 = T.tile_html("Target track", f"#{A['tgt_tid']} {A['track_state']}", "", sub, tone="amber" if A.get("tgt_flash") else A["track_cls"],
                         word="TRACK CHANGED" if A.get("tgt_flash") else str(A["track_state"]))
    elif A.get("no_tgt_truth"):          # truth IS arriving but none of it is TARGET truth (interceptor feed only): nothing to grade a target track against
        t3 = T.tile_html("Target track", A["track_state"], "", f"{A.get('n_tracks_active', 0)} radar tracks active · no TARGET MAVLink truth "
                         "(interceptor feed only) — assign a target id on the Data source page", tone=A["track_cls"], word="NO TARGET TRUTH")
    elif not A.get("has_truth", True):   # nothing to correlate against: the radar's tracks are shown grey on the map
        t3 = T.tile_html("Target track", A["track_state"], "", f"{A.get('n_tracks_active', 0)} radar tracks active · no MAVLink truth to correlate", tone=A["track_cls"])
    else:
        t3 = T.tile_html("Target track", A["track_state"], "", "none within 150 m", tone=A["track_cls"], word="NO TRACK IN GATE")
    return [t3, t2, t1]   # user 2026-09-11: TARGET TRACK (state · coasting) first, closest-so-far last — the state is what matters most live


def _more_items(A: dict) -> list[tuple[str, str, str, str]]:
    pred = A.get("pred") or {}
    if pred.get("miss") is not None:
        pm, pm_sub = f"{pred['miss']:.0f} m", f"T-go {pred['tgo']:.0f} s · straight line"
    elif pred.get("tgo") is not None:
        pm, pm_sub = "diverging", "range opening"
    else:
        pm, pm_sub = "—", "needs both truth heads"
    cov = A["coverage60"]
    cov_cls = "na" if cov is None else ("ok" if cov >= 90 else "amber" if cov >= 70 else "fail")
    tps = A["tracks_per_s"]
    # ONE row of <= 5 chips: the two feed chips are merged (the status line already shows both vehicle glyphs; graded n is in every error card)
    ft, fi = E.feed_chip(A, "target"), E.feed_chip(A, "interceptor")
    order = {"ok": 0, "amber": 1, "fail": 2, "na": 3}
    worst = max((ft[3] or "na", fi[3] or "na"), key=lambda c: order.get(c, 3))
    ages = " / ".join(x[2].split("age ")[-1] if "age " in x[2] else "—" for x in (ft, fi))
    feeds = ("MAVLink feeds", f"TGT {ft[1]} · INT {fi[1]}", f"age {ages}", worst)
    rule = A.get("tgt_rule")
    return [
        ("Coverage · 60 s", _fmt(cov, "{:.0f} %"), "fresh ≤1.5 s / 150 m", cov_cls),
        ("Allegiance", A["allegiance"], f"tgt {_fmt(A['dT'])} m · int {_fmt(A['dI'])} m" + (f" · {rule}" if rule else ""), A["alleg_cls"]),
        ("Predicted miss", pm, pm_sub, ""),
        ("Radar tracks", f"{tps:.1f} /s", f"{A['n_tracks_active']} active", "ok" if tps > 0 else "fail"),
        feeds,
    ]


def _event_rows(A: dict) -> list[list]:
    return T.events_rows(A.get("track_events"), A.get("tgt_tid"), A.get("itc_tid"), hms=D.pdt_hms)


def _blank_snap(t_now: float, hist_s: float) -> dict:
    """An EMPTY live snapshot carrying ih.feed's contract keys: what the first paint renders while the fragment's first
    fetch has not happened yet (so this script run makes NO mongo call)."""
    return {"ok": False, "err": None, "source": "live", "t_now": t_now, "ant": None, "stale": False,
            "label": f"LIVE · {s.get('live_host')}:{s.get('live_port')} · {s.get('live_run') or '—'}",
            "tgt": np.zeros((0, 7)), "itc": np.zeros((0, 7)), "tgt_hist": np.zeros((0, 7)), "itc_hist": np.zeros((0, 7)),
            "tracks": {}, "obs": np.zeros((0, D.OBS_COLS)), "obs_meta": F.empty_obs_meta(0), "feeds": [], "tx": None,
            "tx_lla": None, "track_meta": {}, "unit": F.live_unit(s), "t_start": t_now - hist_s, "data_age": None,
            "anchored": False, "has_truth": False, "n_adsb": 0, "truth_unplaced": False}


def _tick(t_now: float, hist_s: float) -> dict:
    """The snapshot for this run + the last-good / cadence bookkeeping.

    (1) MAIN SCRIPT RUN in live mode -> no fetch at all: the last good snapshot (or a blank one) flagged ``_pending``, so the
        top bar, the panel iframe and the previous figures paint immediately and the FRAGMENT's next tick does the mongo work.
    (2) A fragment tick fetches and records its duration (feeds the cadence) and the wall clock of the last GOOD tick.
    (3) A failed / slow tick re-serves the previous snapshot (ih.feed returns it with stale=True): ``_down_for`` is how long
        the feed has been dark, and the page only goes OFFLINE past OFFLINE_AFTER_S.
    """
    live = D.is_live()
    if live and s.pop("_live_main_run", False):
        last = s.get("last_snap")
        snap = dict(last) if last else _blank_snap(t_now, hist_s)
        snap["_pending"] = True
    else:
        t0 = time.perf_counter()
        snap = dict(F.snapshot(t_now, hist_s))
        if live:
            s["_tick_dur_s"] = round(time.perf_counter() - t0, 2)
        snap["_pending"] = False
        if snap.get("ok") and not snap.get("stale"):        # ih.feed re-serves its filled buffer as (ok, stale) for STALE_HOLD_S: not a fresh tick
            s["_last_ok_wall"] = time.time()
    if live:
        ok_wall = s.get("_last_ok_wall")
        ref = ok_wall or s.get("_live_follow_wall")
        snap["_ever_ok"] = bool(ok_wall)
        snap["_down_for"] = (time.time() - float(ref)) if ref else None
        age = snap.get("data_age")                                     # age of the newest document NOW = the age that tick measured + its own age
        s["_snap_age_now"] = snap["_age_now"] = (float(age) + max(0.0, snap["_down_for"] or 0.0)) if age is not None else None
    return snap


@st.fragment(run_every=run_every)
def live_view() -> None:
    P = {k: s[k] for k in ("map_half", "trail_s", "show_sat", "show_blind", "show_obs", "spec_window", "cpa_gate_m")}
    P["frame_mode"] = _frame_mode()
    P["role_ids"] = s.get("role_ids") or {}
    W = int(s.get("spec_window", D.STATE_DEFAULTS.get("spec_window", 2 if "spec_window"=="refresh_s" else 120)))
    t_now = D.now_t()
    snap = _tick(t_now, float(s.get("hist_s", D.STATE_DEFAULTS["hist_s"])))   # fragment reruns skip init_state; never index a possibly-cleaned key
    t_now = float(snap.get("t_now", t_now))                         # live: pinned to the run's newest radar data when the run is stale
    A = E.analyze(snap, P)
    P["frame"] = _frame(P, A)                                       # the map frame box for the mode (engagement box by default)
    figs, changed = _figures(A, snap, P, W)
    heads = PL.heads(A, s.setdefault("_last_hdg", {}))
    view_w = (P["frame"][1] - P["frame"][0]) if P["frame"] is not None else 2.0 * float(P["map_half"])
    head_imgs = PL.heads_images(heads, view_w)                      # icons in DATA coordinates, sized to the frame
    tails = PL.trail_tails(A)                                       # live-segment anchors (trail tail -> tweened head)
    pills = PL.track_pills(A, {"text_scale": L["text_scale"]})     # map track-number pills (follow the newest track point; amber for 10 s after an id change)
    events = _event_rows(A)
    s["_more"], s["_events"] = _more_items(A), events

    st_row1, st_row2 = _status_parts(A, snap, t_now)                # the two fixed rows of the status box
    if D.is_live():                                                 # LIVE: the "Save data to archive…" button lives at the BOTTOM of the page now (2026-09-15: "messes up the spacing")
        T.topbar(CRUMB, badge=BADGE, status_parts=st_row1, status_parts2=st_row2)
    else:
        T.topbar(CRUMB, badge=BADGE, status_parts=st_row1, status_parts2=st_row2)
    slot = st.container()  # fixed slot: the tree order below never shifts
    down, ever = snap.get("_down_for"), bool(snap.get("_ever_ok"))
    recent = D.is_live() and down is not None and down < OFFLINE_AFTER_S
    if not snap["ok"] and not recent:                                   # OFFLINE only after OFFLINE_AFTER_S seconds without data
        with slot:
            T.callout("OFFLINE", "No data from the selected source", T.esc(snap.get("err") or "unknown error") +
                      (f" — last good data {D.fmt_age(down)} ago." if ever and down is not None else
                       " — configure the source on the Data source page."), tone="red")
    elif not snap["ok"] and not ever:                                   # the very first fetch is still running (fragment tick)
        with slot:
            T.callout("CONNECTING", "Waiting for the first data from the unit",
                      f"reading <code>{T.esc(str(s.get('live_host') or ''))}:{s.get('live_port')}</code> · "
                      f"<code>{T.esc(str(s.get('live_run') or '')[:16])}</code> — first data in a few seconds on a slow unit. "
                      "The map, tiles and plots fill in as soon as the first tick returns.", tone="grey")
    elif not snap["ok"]:
        pass                                                            # a failed / slow tick inside the window: the LAST GOOD picture stays up (the status row carries its age)
    elif D.is_live() and snap.get("truth_unplaced"):
        with slot:
            T.callout("CONNECTED", "MAVLink truth arriving, no radar origin yet",
                      "Block-106 MAVLINK rows are in the window but the run has no TRACKS (143) document — the antenna origin that places truth in ENU "
                      "comes from block 143. Truth, tracks and grading appear with the first radar track.", tone="grey")
    elif D.is_live() and not A.get("has_truth", True):
        with slot:
            age = A.get("data_age")
            bits = [f"<b>{A.get('n_tracks_active', 0)}</b> radar tracks active (grey on the map, no truth match)",
                    f"<b>{A.get('n_adsb', 0)}</b> non-MAVLink (ADS-B) air-traffic rows ignored"]
            if age is not None and age > F.LIVE_STALE_S:
                bits.append(f"newest document <b>{D.fmt_age(age)}</b> ago — the view is pinned to the run's last radar data")
            T.callout("CONNECTED", "No MAVLink truth on this run", " · ".join(bits) + ". Truth-to-track grading needs a MAVLink feed (block 106, source MAVLINK).", tone="grey")
    s["_tiles"] = _tiles(A)                                        # the three detail tiles render inside the More expander (more_view), not above the panel

    if PANEL:
        # publish this tick for the panel: only the figures that changed (+ heads / tails / view every tick the clock moved); the freeze flag always
        any_changed = any(changed.values())
        view = _view(figs["map"], P)
        ant = A.get("ant") or D.ANT_LL
        hud = {"cpa": PL.cpa_hud(A)}
        heads_moved = s.get("_heads_pushed") != (heads, view) or s.get("_pills_pushed") != (pills, events, hud)
        s["_pills_pushed"] = (pills, events, hud)
        if any_changed or heads_moved or not LS.has(SID):
            LS.push(SID, {k: (figs.get(k) if (changed[k] or not LS.has(SID)) and figs.get(k) is not None else None) for k in LS.FIG_KEYS}, heads=heads, head_imgs=head_imgs, tails=tails,
                    view=view, ui=UI, more=[list(x) for x in s["_more"]], pills=pills, events=events, hud=hud, t_now=t_now, clock=D.pdt_hms(t_now),
                    frozen=bool(s.get("freeze")), sat_on=bool(P["show_sat"]), sat_fn=_sat_fn((float(ant[0]), float(ant[1]))))
            s["_heads_pushed"] = (heads, view)
        else:
            LS.set_frozen(SID, bool(s.get("freeze")))
    else:
        # fallback: plain Streamlit charts in the same one-screen arrangement, stable keys (icons + tile inside the map figure)
        ml, mr = st.columns(list(L["split"]), gap="small")
        with ml:
            T.card_header(*LS.HEADERS["map"])
            st.plotly_chart(figs["map"], key="live_map", width="stretch", theme=None, config={**CHART_CFG, "doubleClick": "reset"})
        with mr:
            T.card_header(LS.HEADERS["sep"][0], "since flight start · gold star = CPA (gated)")
            st.plotly_chart(figs["sep"], key="live_sep", width="stretch", theme=None, config=CHART_CFG)
            T.card_header(*LS.HEADERS["err"])
            st.plotly_chart(figs["err"], key="live_err", width="stretch", theme=None, config=CHART_CFG)
        if figs.get("vel") is not None:
            T.card_header(*LS.HEADERS["vel"])
            st.plotly_chart(figs["vel"], key="live_vel", width="stretch", theme=None, config=CHART_CFG)
        if figs.get("meas") is not None:
            T.card_header(*LS.HEADERS["meas"])
            st.plotly_chart(figs["meas"], key="live_meas", width="stretch", theme=None, config=CHART_CFG)


def _sat_fn(ant_ll: tuple[float, float]):
    """Satellite fetcher for the panel server's worker thread (browser view box -> tile payload)."""
    def fn(x0: float, x1: float, y0: float, y1: float) -> dict:
        return D.sat_payload(x0, x1, y0, y1, ant_ll)
    return fn


@st.fragment(run_every=run_every)
def more_view() -> None:
    """Everything that left the banner, below the panel, collapsed: renders the last analysis (no recompute)."""
    with st.expander("More details: coverage, allegiance, radar tracks, MAVLink feeds, predicted miss, track changes", expanded=False):
        items = s.get("_more")
        if s.get("_tiles"):
            T.tiles(s["_tiles"])                                        # target track · separation · closest so far (left the banner 2026-09-14)
        if items:
            T.chips(items)
            T.events_card(s.get("_events") or [])
        else:
            st.caption("waiting for the first tick")


def _toggle_meas() -> None:
    s["meas_open"] = not bool(s.get("meas_open"))
    try:
        if s["meas_open"]:
            st.query_params["meas"] = "1"
        else:
            st.query_params.pop("meas", None)
    except Exception:
        pass


live_view()
if PANEL:
    # ONE constant iframe per session + preset (sid / host / port / cadence / geometry) — emitted by the main script, never by the fragment
    host = LS.browser_host()
    st.iframe(LS.panel_html(SID, host, PORT, PERIOD_MS, L), height=L["panel"] + 4)
# MEASUREMENT SPACE: a collapsible section — one header-row button toggles it ("m" on the keyboard too); collapsed = only this row,
# the quad iframe is not rendered at all (and the figure is not built / pushed)
_open = bool(s.get("meas_open"))
st.button(LS.HEADERS["meas"][0].upper() + ("" if _open else "  ·  click or press m to expand"), key="meas_toggle", on_click=_toggle_meas, width="stretch",
          icon=":material/expand_less:" if _open else ":material/expand_more:",      # Streamlit's Material icon, never a dingbat in the label
          help="Show / hide the measurement-space quad (bistatic range, range rate, azimuth, elevation vs time). Keyboard: m")
if _open and PANEL:
    st.iframe(LS.meas_html(SID, host, PORT, PERIOD_MS, L), height=L["meas"] + L["header"] + 4)
more_view()
if D.is_live():                                                     # LIVE: primary "Save data to archive…" at the bottom (user 2026-09-15: at the top it "messes up all the spacing")
    c_save, _sp = st.columns([1, 2], gap="small")
    with c_save:
        # D.goto_save flags ds_mode=live + save_focus + _goto_save; the ENTRY script (app.py) switches to the Data-source page before this
        # page would rerun, so the body below only runs when this page is served standalone (AppTest)
        if st.button("Save data to archive…", type="primary", key="save_jump", on_click=D.goto_save, width="stretch",
                     help="Save a time range of this run (MAVLink + tracks + obs) as a replayable archive flight — opens the save card on the Data source page."):
            try:
                st.switch_page("views/3_data_source.py")
            except Exception:                                       # standalone page (no st.navigation): the state flags are enough
                pass
# 2026-09-15: the legend was 5 lines (Normal) / 6 lines (Large) of a 682 px laptop viewport.  Kept: the gold-star/CPA rule and
# the track-state key — neither is written anywhere else.  Dropped: what the plots already label themselves (±1σ band, 3D POS =
# 1σ radius, crosses = raw obs, the error-sample provenance note, the panel port).
T.footer(f"{D.UNIT} · {D.SITE} · GOLD STAR = CPA ON THE MAP AND SEPARATION ONCE GATED (< {s['cpa_gate_m']:.0f} M AND PASS OVER)"
         f" · LIGHTER + STRIP = TENTATIVE/COASTING · GAP > 3 S = DROPOUT"
         + ("" if PANEL else " · PANEL PORT UNAVAILABLE — FALLBACK CHARTS"))
