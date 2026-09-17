"""Self-updating LIVE panel backend — the real fix for the "spam flashing", with a
per-figure change protocol and the vehicle icons as layout images in DATA coordinates.

Root cause of the flashing: Streamlit 1.63 registers st.plotly_chart with
key_as_main_identity=False, so the element id hashes the figure spec — every data tick
is a REMOUNT of the chart component, and layout.uirevision cannot survive a remount.
Fix: the Live page emits ONE st.iframe whose HTML string is CONSTANT for the browser
session (only the session token, endpoint host, port and poll cadence are baked in).
That page loads plotly.min.js from this server, polls /figs.json every P ms and calls
Plotly.react(div, data, layout, config) in-browser for the figures that changed.

Map lag fix (this revision):
  * per-figure stamps ("map_stamp", "eng_stamp", "err_stamp"): a push may carry any
    subset of the three figures; the browser sends the stamps it has applied and only
    receives figures newer than that.  The page re-sends the map only when its trail
    data changed AND at most every ~2 s; engagement / error follow the clock.
  * the animated vehicle icons, their 5 s velocity leaders, the live segments (trail tail ->
    tweened head) and the track-number pills are a DOM OVERLAY (this revision, 2026-09-14) inside
    the map div, positioned over the PLOT AREA (left/top = xaxis/yaxis._offset, size = _length,
    overflow hidden = clipped at the plot edge like a layout image, pointer-events none): two <img>
    heads (ONE base SVG per role, ih.icons heading 0, rotated with a CSS transform — the SMIL halo /
    exhaust / rotors keep running inside the <img>), an <svg> with four <line>s, two pill <div>s.
    Every animation frame (<= 30 Hz) the panel converts the tweened E/N to pixels with
    xaxis.l2p / yaxis.l2p read from gd._fullLayout — the browser OWNS the view (fitView strips server
    ranges; ranges only change by user drag / wheel or our own animate), so _fullLayout is always the
    truth and the overlay can never detach from the trails: user drag / wheel-zoom / our animate /
    a resize are followed per frame (plus synchronously on plotly_relayouting / plotly_relayout).
    Between server reacts the map does ZERO Plotly work: tweenFrame never calls Plotly (before this
    revision: one Plotly.relayout({images, shapes}) per tween frame, 242 per 20 s measured in
    headless Firefox — "laggy, everything flashing" on a laptop, pill text re-measured every frame).
    The envelope still carries "heads" = {tgt|itc: {E, N, hdg, spd, U}}, "tails", "pills" (annotation
    dicts: x/y in metres, text, xshift/yshift, bordercolor — converted to pixels in the panel) and
    "head_imgs" (the st.plotly_chart fallback's layout-image dicts; the panel ignores them).  The
    map react's layout.images = the satellite tiles ONLY, layout.shapes = [], annotations = the
    figure's own (pill_* stripped).  A new satellite tile arriving with no map react to carry it is
    applied with ONE Plotly.relayout({images}) — the only Plotly edit outside reacts / view changes.
  * trails are scattergl (ih.plots; the panel downgrades them to SVG scatter when the
    browser has no WebGL); the satellite tile stays externalised (/img/<sha1>)
    and is included in the map layout only when the map box changed — the server
    remembers the current tile per sid ("sat") and hands it to a fresh browser on its
    first fetch; "sat_keep" tells the browser to keep the tile it has.
  * debounce: a poll is skipped while a fetch is in flight; a react chain in flight
    makes the tick skip the figures (they are re-sent next poll) but still moves the
    heads.

Map view is OWNED BY THE BROWSER (this revision): the map figure JSON never carries axis ranges
(push strips them); the frame (FIXED flight footprint or FOLLOW centre) travels in the envelope as
"view" and is applied only on the first paint and when view.rev changes (an EXPLICIT change: preset /
Fixed<->Follow / zoom slider / Reset map view).  Any user zoom / pan / double-click locks the view
("view locked · reset" pill); FOLLOW re-centres with hysteresis (a head within 15 % of the edge) and a
600 ms animate, never while locked.  Vehicle heads are tweened client-side (rAF <= 30 Hz, shortest-arc
heading, forward prediction <= 0.5 period) and drawn by the DOM overlay (CSS transforms + SVG line attributes,
no Plotly call); the overlay update cost is measured (IH.overlay.avg_ms) and the rate drops to 15 Hz above 8 ms.

Responsive geometry (this revision): Streamlit cannot read the viewport, so the sidebar
"Screen size" preset (Laptop 560 / Desktop 1080p 800 / Large 1440p 1040 px) fixes the PANEL
HEIGHT server-side (layout(preset): header 24, eng 28 %, err remainder, map = panel −
header; the st.iframe height follows) and the figures are built at those heights with the
preset's font (ui.font_px, Laptop 12 / Desktop 11 / Large 13).  The panel JS (responsive_js) is
the part that knows the real iframe WIDTH: on load and on resize it measures innerWidth W;
W >= 2400 -> THREE columns map 45 | separation (+ the "More" chips under it) 25 | error 30,
fonts 13-14 px, 3 px lines; W >= 1150 -> two columns 55/45 with the map height min(panel −
header, left column width) (≈ square, never a tall strip) and separation ~26 % / error ~74 %
of that column; W < 1150 (or error cards that would squeeze below 95 px) -> ONE stacked
column (map W×0.62 capped, sep 200, err 420) with the iframe body scrolling.  Fonts 11 px at
W >= 1400 else >= 12 px.
Heights / fonts are applied with Plotly.relayout (uirevision untouched) + Plotly.Plots.resize,
and every figure arriving from the server is re-fitted (fitLayout) before it is drawn.

Server: one threaded HTTP server per process (module singleton, also wrapped in
st.cache_resource by the page), bound 0.0.0.0:PORT (default 8902, env IH_LIVE_PORT).
  GET /figs.json?sid=<token>[&since=<stamp>&map=<s>&sep=<s>&err=<s>&meas=<s>&view=x0,x1,y0,y1&satk=<key>&keys=meas]
        -> {"stamp", "clock", "t_now", "frozen", "map_stamp", "sep_stamp", "err_stamp", "meas_stamp",
            "heads", "head_imgs", "tails", "view": {x0,x1,y0,y1,rev,mode}, "ui": {font_px,line_w,preset}, "more": [...],
            ["sat": {key, imgs}], "figs": {<only figures newer than the client's>}}
        -> {"stamp", "clock", "frozen", "unchanged": true}   when stamp == since
        -> 404 {"error": ...}                                unknown sid (page not yet ticked)
     view = the browser's CURRENT map axis box (it owns the view): the server fetches the satellite tile
     for that box in a worker thread (quantised, cached) and hands it over as "sat" once ready and only
     when its key differs from the client's satk (the client keeps its previous tile until then).
     keys = restrict "figs" to these keys (the measurement-space iframe polls keys=meas, lite envelope).
  GET /plotly.min.js      -> track_correlation/plotly.min.js (cached, gzip, max-age 1 d)
  GET /img/<sha1>.<ext>   -> large layout images (satellite tiles) externalised out of the
                             polled JSON as "@img/<name>" and rehydrated by the panel JS
  GET /health             -> {"ok": true, "sids": n, "port": p}
Every response carries Access-Control-Allow-Origin: *; JSON is Cache-Control: no-store;
bodies are gzip-compressed when the client accepts it.

If the port cannot be bound (in use), start() logs once and returns None; the page
then falls back to the previous st.plotly_chart path (and retries the bind every 30 s).
Firewall: the BROWSER must reach <streamlit host>:PORT (documented on the Data-source
page footer).  Freeze: the page writes STORE[sid]["frozen"]; the panel JS keeps polling
but stops reacting / relayouting while frozen and shows "frozen" (pause icon).
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import icons
from . import theme as T

log = logging.getLogger("ih.liveserver")

DEFAULT_PORT = int(os.environ.get("IH_LIVE_PORT", "8902"))
# plotly.min.js served to the panel iframe: $IH_PLOTLY_JS, else ih/vendor/plotly.min.js (if someone drops one there), else the copy
# BUNDLED WITH THE plotly PYTHON PACKAGE (plotly.offline.get_plotlyjs — same plotly.js the figures are built for), else a CDN redirect.
PLOTLY_JS = os.environ.get("IH_PLOTLY_JS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "plotly.min.js")
PLOTLY_CDN = "https://cdn.plot.ly/plotly-3.6.0.min.js"
IMG_EXTERNALIZE_BYTES = 16_384   # data URIs longer than this leave the JSON (satellite tiles ~200 kB)
IMG_KEEP = 64                    # most recent externalised images kept in memory
STALE_SID_S = 3600.0             # STORE entries not touched for an hour are dropped
RETRY_S = 30.0                   # bind retry cadence after a failure

# one-screen panel geometry (px): LEFT map full height | RIGHT separation over the error panel.
# Streamlit cannot read the viewport, so the sidebar "Screen size" preset picks the PANEL HEIGHT
# (= the st.iframe height) server-side; the panel JS (responsive_js) knows the real iframe WIDTH
# and re-fits the figures inside that height (three / two / one column).
PRESETS = {"Laptop": 600, "Desktop 1080p": 860, "Large 1440p": 1180}   # preset -> panel px (≈ 85–90 % of 768 / 1080 / 1440 minus the top line + tiles); the JS fine-tunes from the parent viewport
DEFAULT_PRESET = "Desktop 1080p"
FONT_PX = {"Laptop": 13, "Desktop 1080p": 14, "Large 1440p": 16}       # figure tick / axis / legend font per preset, x the "Text size" scale (ui.font_px)
LINE_W = {"Laptop": 2.5, "Desktop 1080p": 2.5, "Large 1440p": 3.0}     # series line width per preset
HEADER_PX = 32                                                         # card header row (fits the figure-font title small-caps up to 21 px at text scale 1.3; 2026-09-14 'text is massive')
SEP_FRAC = 0.32                                                        # two-column mode: separation ~32 % of the right column content, error the rest
SEP_FRAC3 = 0.26                                                       # three-column (ultrawide) mode: separation ~66 % of the middle column, the one chip row + track changes below
SPLIT = (62, 38)                                                       # two-column mode: MAP (primary, ~62 %) | metrics column: separation over error
SPLIT3 = (46, 27, 27)                                                  # ultrawide (>= 2400 px): MAP ~55 % | separation + more | error
BREAKPOINT_PX = 1150                                                   # iframe narrower than this -> one stacked column (JS)
ULTRAWIDE_PX = 2400                                                    # iframe at least this wide -> three columns (JS)
MIN_CARD_PX = 53                                                       # never squeeze an error card's PLOT AREA below this (the readouts left the plot area: header strips);
                                                                       # 53 is the most that keeps a fitted 1080p browser panel (688) two-column under the 57 px strips with 36 px card headers:
                                                                       # 4*(53+57) + 8 + 28 + 30 = 506 = 688 - 2*36 - SEP_MIN_PX(110)  (55 would need 28 px headers)
ERR_HDR_PX = 52                                                        # header strip per card AT TEXT SCALE 1 (ih.plots.err_strip_px(14, 11)[0]: readout 14 (line1 21) + containment 11 lines + 2 px row gap);
                                                                       # the real strip grows with the sidebar "Text size" (the readout fonts are px(14) / px(11)): 46 / 52 / 55 px at Normal / Large / X-Large -> err_hdr_px()
ERR_T, ERR_B, ERR_GAP = 8, 28, 10                                      # error figure bottom margin / card gap (ih.plots) -> plot area = (err - hdr - t - b - 3 (gap + hdr)) / 4
ERR_MIN_PX = 4 * (MIN_CARD_PX + ERR_HDR_PX) + ERR_T + ERR_B + 3 * ERR_GAP   # 462: the shortest error panel with 53 px plot areas under 46 px strips AT TEXT SCALE 1 (err_min_px() scales it: 490 / 518)
SEP_MIN_PX = 110                                                       # the shortest separation card worth having next to the map
ONE_MAP_FRAC = 0.62                                                    # (legacy, 2026-09-15: the stacked map is no longer width-driven — see one_row(); kept for the JS constant block)
COL_GAP_PX = 12                                                        # grid column gap (CSS .grid gap / JS COL_GAP)
ONE_RIGHT_MIN_PX = 260                                                 # mode "one" two-up: the separation column is never narrower than this (the map gives up its square plot area first)
MAP_MODEBAR_FALLBACK = 26                                              # ih.plots.MAP_MODEBAR_PX when ih.plots cannot be imported
ONE_SEP_PX, ONE_ERR_PX = 200, ERR_MIN_PX                               # stacked mode card heights AT TEXT SCALE 1 (the iframe scrolls); err = the 53 px plot-area floor under the strips (462);
                                                                       # one_sep_px() / one_err_px() scale both (the separation card's own legend row, x tick row and axis title grow with the text)
MEAS_PX = 640                                                          # measurement-space figure (its own iframe below the panel; 3 rows since 2026-09-17 pm)
VEL_STRIP_PX, VEL_STRIP_MIN_PX, VEL_STRIP_FRAC = 230, 160, 0.24        # two-column mode: the VELOCITY STRIP under the map (3 cards side by side) = 24 % of the panel, 160..230 px
ONE_VEL_PX = 3 * (MIN_CARD_PX + ERR_HDR_PX) + ERR_T + ERR_B + 2 * ERR_GAP   # stacked mode: three velocity cards at the plot-area floor at text scale 1 (353; one_vel_px() scales it)
COMPACT_ERR_PX = 480                                                   # error panel shorter than this: 12 px readouts (ih.plots)


def err_hdr_px(text_scale: float = 1.0) -> int:
    """The per-card HEADER STRIP in px at this sidebar text scale.  ih.plots builds the strip from the readout /
    containment fonts and those are px(14) / px(11), so the DRAWN strip is 46 / 52 / 55 px at Normal / Large /
    X-Large while ERR_HDR_PX alone (46) is only the Normal one — a height budget that ignores the scale squeezes
    every card (2026-09-15: err plot areas ~44 px, velocity 27-32 px, under the MIN_CARD_PX floor).  The linear
    ERR_HDR_PX x scale (46 / 53 / 60) is never shorter than the drawn strip, so the budget cannot under-reserve
    (pinned in tests/test_liveserver.py); the panel JS scales it the same way (hdrPx())."""
    return int(round(ERR_HDR_PX * float(text_scale or 1.0)))


def err_min_px(text_scale: float = 1.0) -> int:
    """The shortest error panel that keeps all four plot areas at MIN_CARD_PX under their strips: 462 / 490 / 518 px
    at Normal / Large / X-Large (ERR_MIN_PX is the Normal one)."""
    return 4 * (MIN_CARD_PX + err_hdr_px(text_scale)) + ERR_T + ERR_B + 3 * ERR_GAP


def one_sep_px(text_scale: float = 1.0) -> int:
    """Stacked mode: the separation card's height — 200 / 230 / 260 px at Normal / Large / X-Large.  It carries no px-exact
    strips (plotly's autoexpand owns its margins: one legend row on top, the x tick row + both axis titles at the bottom), and
    those rows scale with the text: at 200 px and X-Large the drawn card pushed its tick labels 6 px OUT of the figure, ran the
    x axis title over them and put the legend on the plot area (clip_audit 2026-09-15, 1920x1080 X-Large stacked)."""
    return int(round(ONE_SEP_PX * float(text_scale or 1.0)))


def one_err_px(text_scale: float = 1.0) -> int:
    """Stacked mode: the error panel's height = its floor at this text scale (the iframe body scrolls)."""
    return err_min_px(text_scale)


def one_vel_px(text_scale: float = 1.0) -> int:
    """Stacked mode: three velocity cards at the MIN_CARD_PX plot-area floor under their strips — 353 / 371 / 395 px
    at Normal / Large / X-Large (ih.plots.vel_stacked_px takes the same number for its y tick budget)."""
    return 3 * (MIN_CARD_PX + err_hdr_px(text_scale)) + ERR_T + ERR_B + 2 * ERR_GAP


def _fit_two(col_h: int, text_scale: float = 1.0) -> tuple[int, int] | None:
    """Right-column content split for a column ``col_h`` tall: (sep, err) or None when the column is too short
    for MIN_CARD_PX error plot areas (under their text-scaled header strips) AND a >= 110 px separation card
    (-> stacked mode; a fitted 1080p browser panel, 688, lands exactly on the Normal floor: sep 162 / err 462)."""
    content = int(col_h) - 2 * HEADER_PX
    sep = int(round(SEP_FRAC * content))
    err = content - sep
    if (err - ERR_T - ERR_B - 3 * ERR_GAP) / 4.0 - err_hdr_px(text_scale) < MIN_CARD_PX:   # plot areas would squeeze: give the error panel its floor first
        err = err_min_px(text_scale)
        sep = content - err
    if sep < SEP_MIN_PX:
        return None
    return sep, err


def vel_strip_px(panel: int, text_scale: float = 1.0) -> int:
    """Two-column mode: the velocity strip's height for a panel — 24 % of it, clamped 160..230 px, both ends raised by
    the extra header-strip pixels the text scale costs (+0 / +7 / +14), so a card's plot area under the strip is the
    same at every text size (a fitted 1080p browser panel of ~690 px gives 165 at Normal, 179 at X-Large)."""
    extra = err_hdr_px(text_scale) - ERR_HDR_PX
    return int(min(VEL_STRIP_PX + extra, max(VEL_STRIP_MIN_PX + extra, round(VEL_STRIP_FRAC * int(panel)))))


MAP_MARGIN_FALLBACK = dict(l=83, r=12, t=62, b=56)                     # ih.plots.MARGINS["map"] at the default font: the panel JS pre-paint fallback when ih.plots cannot be imported


def map_margin_px(font_px: float, text_scale: float = 1.0) -> dict:
    """The map figure's FIXED margins (ih.plots.map_margin: derived from the font + text scale, autoexpand off).
    Baked into the panel JS as MAP_M so plotPx()'s PRE-PAINT estimate of the plot area — and with it the first
    frame's equal-aspect ranges — uses the real margins instead of the stale {64, 12, 8, 44} guess.
    Imported lazily: ih.liveserver stays importable without plotly / streamlit."""
    try:
        from . import plots as _PL
        m = _PL.map_margin(float(font_px), float(text_scale or 1.0))
        return {k: int(m[k]) for k in ("l", "r", "t", "b")}
    except Exception:                                                  # no ih.plots (or its signature moved): the default-font numbers
        return dict(MAP_MARGIN_FALLBACK)


def map_top_nolegend_px() -> int:
    """The map's top margin in mode "one" (the key row is dropped there — the card header names the series): the hover modebar
    row + the 2 px pad and nothing else (ih.plots.map_margin's t minus its legend_row_px term).  Baked into the panel JS as MAP_T0."""
    try:
        from . import plots as _PL
        return int(_PL.MAP_MODEBAR_PX) + 2
    except Exception:
        return MAP_MODEBAR_FALLBACK + 2


def one_row(panel: int, width: int, font_px: float | None = None, text_scale: float = 1.0) -> dict:
    """Mode "one" (an iframe under BREAKPOINT_PX, i.e. a 1366 / 1536 laptop) — the FIRST ROW is two-up (2026-09-15): a SQUARE-plot-area
    map card on the left and the separation card on the right, both ``panel`` tall (header + figure = the iframe height), then the four
    error cards and the three velocity cards full-width below (the iframe scrolls).  Before this the map ran full-width at 0.62 x W
    capped at panel − 2 headers (829 x 267 px plot area at 1366 x 768: constrain="range" drew 3.1x more desert than the square
    engagement box, 5.99 m/px at the CPA) and the separation card sat below the fold.
    map = panel − header; its plot area = map − MAP_T0 − margin.b (the key row is off in this mode); the map DIV width that makes it
    square = plot + margin.l + margin.r; right = width − mapW − COL_GAP_PX, floored at ONE_RIGHT_MIN_PX (the map narrows first).
    Mirrors the panel JS geometry() one-mode branch (pinned by tests/test_panel_js.py); the plot-area square is what lets the
    engagement box fill the map (aspectRange: a square box in a square area = the box exactly)."""
    m = map_margin_px(float(font_px if font_px is not None else FONT_PX[DEFAULT_PRESET]), float(text_scale or 1.0))
    t0 = map_top_nolegend_px()
    mp = int(panel) - HEADER_PX
    plot_h = mp - t0 - m["b"]
    map_w = plot_h + m["l"] + m["r"]
    right = int(width) - map_w - COL_GAP_PX
    if right < ONE_RIGHT_MIN_PX:
        right = min(ONE_RIGHT_MIN_PX, int(round(int(width) * 0.4)))
        map_w = int(width) - right - COL_GAP_PX
    return {"map": mp, "mapW": map_w, "plot": plot_h, "plot_w": map_w - m["l"] - m["r"], "sep": mp, "right": right, "t0": t0, "margin": m}


def layout(preset: str | None = None, text_scale: float | None = None) -> dict:
    """Panel geometry (px) for a screen preset — the server-side (width-agnostic) picture the figures are
    built for; the panel JS re-fits to the real iframe width with the same rules.
    mode "two": header 36, map = panel − 2 headers − velocity strip, right column = separation ~32 % / error ~68 % of the
    content (error floored at err_min_px so each of the four cards keeps a >= MIN_CARD_PX plot area under its strip); mode "one" (the panel
    is too short for that, i.e. Laptop): the two-up first row (one_row: square-plot map | separation, both panel − header) over
    err one_err_px / vel one_vel_px full-width — the iframe scrolls.  Every card height that sits under a HEADER STRIP scales with ``text_scale`` (the strip does: err_hdr_px), so the plot areas
    stay at the floor at Large / X-Large instead of squeezing.  Unknown / None -> the default preset."""
    name = preset if preset in PRESETS else DEFAULT_PRESET
    panel = int(PRESETS[name])
    if text_scale is None:
        try:
            text_scale = T.text_scale()
        except Exception:
            text_scale = 1.0
    ts = float(text_scale)                      # the card header strips scale with it: the heights must too (else the cards squeeze)
    fit = _fit_two(panel, ts)
    if fit is None:                             # mode "one": the two-up first row (one_row) — map AND separation = panel − header tall; err / vel full-width below
        mode, mp, sep, err, vel = "one", panel - HEADER_PX, panel - HEADER_PX, one_err_px(ts), one_vel_px(ts)
    else:   # two columns: MAP over the VELOCITY STRIP (left) | separation over the error cards (right); the map gives up the strip's height
        vel = vel_strip_px(panel, ts)
        mode, mp, (sep, err) = "two", panel - 2 * HEADER_PX - vel, fit
    return {"preset": name, "mode": mode, "panel": panel, "header": HEADER_PX, "map": mp, "sep": sep, "err": err, "vel": vel, "meas": MEAS_PX, "meas_itc": MEAS_PX,
            "split": SPLIT, "split3": SPLIT3, "font_px": int(round(FONT_PX[name] * float(text_scale))), "line_w": float(LINE_W[name]),
            "text_scale": float(text_scale)}


LAYOUT = layout(DEFAULT_PRESET, 1.0)   # the default preset's numbers at text scale 1 (Desktop 1080p: panel 860, map 582, sep 252, err 536, vel 206)
FIG_KEYS = ("map", "sep", "err", "vel", "meas", "meas_itc")
PANEL_KEYS = ("map", "sep", "err", "vel")    # inside the one-screen iframe; "meas" (target) and "meas_itc" (interceptor) each have their own iframe below
MEAS_CARD_KEYS = ("meas", "meas_itc")
SAT_OPACITY = 0.85   # satellite underlay opacity (= ih.plots.SAT_OPACITY; 2026-09-17: 0.9 -> 0.85 under the brighter series)

HEADERS = {  # constant card headers baked into the panel HTML (left text, right caption)
    # captions are hidden WHOLE by the panel JS (fitCaptions) when a column is too narrow for them, so they are kept short enough
    # for the two-column widths (>= 1600 px); the header's title attribute always carries the full text
    "map": ("Top-down · ENU about the radar", "solid = truth · dashed = radar tracks · thin = 5 s leader · ★ CPA (gated)"),
    "sep": ("Separation · truth & track (m)", ""),   # right side carries the live status
    "err": ("Track quality", "±1σ band · 3D pos = 1σ radius · lighter segments + bottom strip = coasting/tentative (not in stats) · gaps > 3 s = dropout"),
    "vel": ("Velocity states · filtered track vs truth (m/s)",
            "red = MAVLink truth · ink = track filtered state · lighter = coasting/tentative · Δ/σ/containment vs truth · ±1σ band live only"),
    "meas": ("Measurement space · TARGET track vs target truth",
             "solid thick = target truth (red) · thin = radar tracks (★ start ✕ end, labelled) · circles = raw obs · bistatic: TX/RX co-located → 2×mono · "
             "no obs range rate in archive replay (amb_dop not archived)"),
    "meas_itc": ("Measurement space · INTERCEPTOR track vs interceptor truth",
                 "solid thick = interceptor truth (blue) · thin = the interceptor's radar track (★ start ✕ end) · bistatic: TX/RX co-located → 2×mono · not graded, no obs"),
}

STORE: dict[str, dict] = {}                 # sid -> {"stamp", "clock", "t_now", "frozen", "figs": {k: bytes}, "fig_stamps": {k: int}, "heads", "head_imgs", "tails", "view", "ui", "more", "sat_fn", "sat_on", "wall"}
IMAGES: dict[str, tuple[bytes, str]] = {}   # name -> (bytes, mime)
SAT: dict[tuple, list] = {}                 # quantised view box -> [layout image dicts, externalised]  (satellite tiles, per box)
SAT_PENDING: set = set()
SAT_KEEP = 48
_LOCK = threading.RLock()
class _QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not print a 20-line traceback when a browser drops its socket mid-response
    (ConnectionResetError / BrokenPipeError — every tab close or reload does this); anything else is logged in one line."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys as _sys
        et, ev = _sys.exc_info()[0], _sys.exc_info()[1]
        if et is not None and issubclass(et, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        log.warning("live panel server: %s from %s: %s", et.__name__ if et else "error", client_address, ev)


_SERVER: ThreadingHTTPServer | None = None
_FAIL: dict = {"err": None, "at": 0.0, "port": None}
_PLOTLY: dict = {}                          # cached plotly.min.js (raw + gzip)


# ── HTTP ─────────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    server_version = "ih-live/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:  # quiet: one line per poll would flood the dashboard log
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, gz: bool = True) -> None:
        hdrs = {"Content-Type": ctype, "Access-Control-Allow-Origin": "*", **(extra or {})}
        if gz and len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            hdrs["Content-Encoding"] = "gzip"
            hdrs["Vary"] = "Accept-Encoding"
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, obj: dict | None = None, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(obj, separators=(",", ":")).encode()
        self._send(code, body, "application/json", {"Cache-Control": "no-store"})

    def do_OPTIONS(self) -> None:  # CORS preflight (plain GETs never trigger one; harmless)
        self._send(204, b"", "text/plain", {"Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*",
                                            "Access-Control-Max-Age": "86400"}, gz=False)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
        if u.path == "/figs.json":
            sid, since = one("sid"), one("since")
            have = {k: one(k) for k in FIG_KEYS}
            keys = tuple(k for k in one("keys").split(",") if k in FIG_KEYS) or None
            view = parse_view(one("view"))
            with _LOCK:
                ent = STORE.get(sid)
                if ent is None:
                    return self._json(404, {"error": "no data for sid", "sid": sid})
                ent["wall"] = time.time()   # a polling browser keeps its slot (STALE_SID_S prunes abandoned sids only)
                if since and since == str(ent["stamp"]):
                    # nothing new in the store — but the BROWSER's view may have moved (zoom / pan while paused, frozen or on a stale run):
                    # the satellite tile still follows the view (this branch used to skip it: "the edges don't load" when paused)
                    out = {"stamp": ent["stamp"], "clock": ent["clock"], "frozen": bool(ent["frozen"]), "unchanged": True}
                    if keys is None and view is not None:
                        ks, imgs = sat_for(ent, view, one("satk"))
                        if imgs is not None and ks and ks != one("satk"):
                            out["sat"] = {"key": ks, "imgs": imgs}
                    return self._json(200, out)
                body = response_body(ent, have, view=view, satk=one("satk"), keys=keys)
            return self._json(200, raw=body)
        if u.path == "/plotly.min.js":
            js = _plotly_js()
            if js is None:   # no file and no plotly package copy: send the browser to the CDN (same plotly.js version)
                return self._send(302, b"", "text/plain", {"Location": PLOTLY_CDN}, gz=False)
            return self._send(200, js, "application/javascript; charset=utf-8", {"Cache-Control": "public, max-age=86400"})
        if u.path.startswith("/img/"):
            name = u.path[5:]
            with _LOCK:
                im = IMAGES.get(name)
            if im is None:
                return self._send(404, b"no such image", "text/plain", gz=False)
            return self._send(200, im[0], im[1], {"Cache-Control": "public, max-age=86400, immutable"}, gz=False)
        if u.path == "/health":
            with _LOCK:
                n = len(STORE)
            return self._json(200, {"ok": True, "sids": n, "port": port()})
        return self._send(404, b"not found", "text/plain", gz=False)


def parse_view(v: str) -> tuple[float, float, float, float] | None:
    """'x0,x1,y0,y1' (m) -> tuple, None when absent / malformed."""
    try:
        p = [float(x) for x in v.split(",")]
        if len(p) == 4 and all(np_isfinite(x) for x in p) and p[1] > p[0] and p[3] > p[2]:
            return p[0], p[1], p[2], p[3]
    except (ValueError, AttributeError):
        pass
    return None


def np_isfinite(x: float) -> bool:
    return x == x and abs(x) != float("inf")


SAT_MARGIN = 0.5                 # the tile fetch box = the view widened this fraction each way (2x the view: a zoom-out to half scale still has imagery; pans inside it need no new tile)
SAT_KEEP_SCALE = 4.0              # keep the client's tile while it covers the view and is at most this much wider than it (zooming in past that = a sharper tile)


def sat_key(box: tuple[float, float, float, float]) -> tuple:
    """Quantised (100 m) fetch box for a view: the view widened SAT_MARGIN (15 %) each way, so the tile always covers
    what constrain="range" shows and small pans stay inside it.  Same view -> same key -> cached tile."""
    x0, x1, y0, y1 = box
    cx, cy, hx, hy = 0.5 * (x0 + x1), 0.5 * (y0 + y1), (0.5 + SAT_MARGIN) * (x1 - x0), (0.5 + SAT_MARGIN) * (y1 - y0)
    q = 100.0
    return (round((cx - hx) / q) * q, round((cx + hx) / q) * q, round((cy - hy) / q) * q, round((cy + hy) / q) * q)


def sat_covers(satk: str, box: tuple[float, float, float, float]) -> bool:
    """Does the client's current tile (its key string 'x0,x1,y0,y1') still cover the view ``box`` at a usable scale
    (tile width <= SAT_KEEP_SCALE x the view width)?  Then no new tile is fetched / sent: the picture stays put."""
    try:
        kx0, kx1, ky0, ky1 = (float(v) for v in str(satk).split(","))
    except (ValueError, TypeError):
        return False
    x0, x1, y0, y1 = box
    if not (kx0 <= x0 and kx1 >= x1 and ky0 <= y0 and ky1 >= y1):
        return False
    return (kx1 - kx0) <= SAT_KEEP_SCALE * max(1.0, x1 - x0) and (ky1 - ky0) <= SAT_KEEP_SCALE * max(1.0, y1 - y0)


def sat_key_str(key: tuple) -> str:
    return ",".join(f"{v:.0f}" for v in key)


def _sat_worker(key: tuple, fn) -> None:
    try:
        r = fn(*key)
        imgs = []
        if r and r.get("img"):
            imgs = [dict(source=r["img"], xref="x", yref="y", x=r["x0"], y=r["y1"], sizex=r["x1"] - r["x0"], sizey=r["y1"] - r["y0"],
                         xanchor="left", yanchor="top", sizing="stretch", layer="below", opacity=SAT_OPACITY, name="sat")]
            _externalize_images({"layout": {"images": imgs}})
    except Exception as ex:  # offline etc.: an empty tile list, never a crash
        log.warning("satellite fetch failed for %s: %s", key, ex)
        imgs = []
    with _LOCK:
        SAT[key] = imgs
        SAT_PENDING.discard(key)
        while len(SAT) > SAT_KEEP:
            SAT.pop(next(iter(SAT)))


def sat_for(ent: dict, box: tuple | None, satk: str = "") -> tuple[str, list | None]:
    """(key string, image dicts | None) for a view box: the client's tile ``satk`` still covering the view at a usable
    scale -> (satk, None) (keep it, nothing fetched); cached -> the tile; not cached -> a worker is started and None
    is returned (the client keeps its previous tile until the new one lands).  Call under _LOCK."""
    fn = ent.get("sat_fn")
    if not ent.get("sat_on") or fn is None or box is None:
        return "", None
    if satk and sat_covers(satk, box):
        return satk, None
    key = sat_key(box)
    ks = sat_key_str(key)
    if key in SAT:
        return ks, SAT[key]
    if key not in SAT_PENDING:
        SAT_PENDING.add(key)
        threading.Thread(target=_sat_worker, args=(key, fn), daemon=True, name="ih-sat").start()
    return ks, None


def response_body(ent: dict, have: dict, *, view: tuple | None = None, satk: str = "", keys: tuple | None = None) -> bytes:
    """Envelope + only the figures newer than the client's per-figure stamps (call under _LOCK).
    ``view`` = the client's current map box (else the frame view) -> satellite tile for it when cached
    and different from the client's ``satk``.  ``keys`` restricts figures (lite envelope: no heads / tile)."""
    lite = keys is not None
    meta = {"stamp": ent["stamp"], "clock": ent["clock"], "t_now": ent["t_now"], "frozen": bool(ent["frozen"]),
            **{f"{k}_stamp": ent["fig_stamps"].get(k, 0) for k in FIG_KEYS}, "ui": ent.get("ui") or {}}
    if not lite:
        meta.update({"heads": ent.get("heads"), "head_imgs": ent.get("head_imgs") or [], "tails": ent.get("tails") or {},
                     "view": ent.get("view") or {}, "more": ent.get("more") or [], "pills": ent.get("pills") or [], "events": ent.get("events") or [],
                     "hud": ent.get("hud") or {}})
        fv = ent.get("view") or {}
        box = view or ((fv["x0"], fv["x1"], fv["y0"], fv["y1"]) if {"x0", "x1", "y0", "y1"} <= set(fv) else None)
        ks, imgs = sat_for(ent, box, satk)
        if imgs is not None and ks and ks != satk:
            meta["sat"] = {"key": ks, "imgs": imgs}
    parts = []
    for k in FIG_KEYS:
        if keys is not None and k not in keys:
            continue
        b = ent["figs"].get(k)
        if b is None:
            continue
        try:
            client = int(have.get(k) or 0)
        except ValueError:
            client = 0
        if ent["fig_stamps"].get(k, 0) > client:
            parts.append(b'"' + k.encode() + b'":' + b)
    head = json.dumps(meta, separators=(",", ":")).encode()
    return head[:-1] + b',"figs":{' + b",".join(parts) + b"}}"


def _plotly_js() -> bytes | None:
    with _LOCK:
        if "raw" not in _PLOTLY:
            _PLOTLY["raw"] = load_plotly_js()
        return _PLOTLY["raw"]


def load_plotly_js() -> bytes | None:
    """plotly.min.js bytes: PLOTLY_JS file if present, else the plotly python package's bundled copy; None = use the CDN."""
    if os.path.isfile(PLOTLY_JS):
        try:
            with open(PLOTLY_JS, "rb") as f:
                return f.read()
        except OSError as ex:
            log.warning("plotly.min.js unreadable: %s", ex)
    try:
        import plotly.offline as _po

        return _po.get_plotlyjs().encode("utf-8")
    except Exception as ex:  # pragma: no cover — plotly is a hard requirement
        log.warning("bundled plotly.js unavailable (%s); panel falls back to %s", ex, PLOTLY_CDN)
        return None


# ── lifecycle ────────────────────────────────────────────────────────────────
def start(port_: int | None = None) -> ThreadingHTTPServer | None:
    """Start (once per process) the panel server; None when the port cannot be bound."""
    global _SERVER
    p = int(port_ if port_ is not None else DEFAULT_PORT)
    with _LOCK:
        if _SERVER is not None:
            return _SERVER
        if _FAIL["err"] and _FAIL["port"] == p and time.time() - _FAIL["at"] < RETRY_S:
            return None
        try:
            srv = _QuietServer(("0.0.0.0", p), _Handler)
        except OSError as ex:
            if not _FAIL["err"]:
                log.warning("live panel server: cannot bind 0.0.0.0:%d (%s) — falling back to st.plotly_chart", p, ex)
            _FAIL.update(err=str(ex), at=time.time(), port=p)
            return None
        srv.daemon_threads = True
        srv.store = STORE                       # the handler thread and every (re)loaded module share THIS dict
        srv.images = globals().get("IMAGES")
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True, name="ih-liveserver").start()
        _SERVER = srv
        _FAIL.update(err=None, at=0.0, port=None)
        log.info("live panel server on 0.0.0.0:%d (figs.json / plotly.min.js / img)", p)
        return srv


def adopt(srv) -> None:
    """Re-attach a server object cached across a Streamlit module reload: the module-level
    _SERVER / STORE / IMAGES reset on reload while st.cache_resource keeps the live server
    (and its handler thread, which reads the dicts attached in start()).  Without this the
    page sees PANEL=True but port()=None (int(None) crash) and pushes figures into a store
    the server never reads."""
    global _SERVER, STORE, IMAGES
    if srv is None:
        return
    with _LOCK:
        _SERVER = srv
        st_ = getattr(srv, "store", None)
        if isinstance(st_, dict) and st_ is not STORE:
            STORE = st_
        im = getattr(srv, "images", None)
        if isinstance(im, dict) and "IMAGES" in globals() and im is not globals()["IMAGES"]:
            IMAGES = im


def stop() -> None:
    """Tests only."""
    global _SERVER
    with _LOCK:
        srv, _SERVER = _SERVER, None
    if srv is not None:
        srv.shutdown()
        srv.server_close()


def is_up() -> bool:
    return _SERVER is not None


def port() -> int | None:
    return int(_SERVER.server_address[1]) if _SERVER is not None else None


def last_error() -> str | None:
    return _FAIL["err"]


# ── store ────────────────────────────────────────────────────────────────────
def _externalize_images(d: dict) -> dict:
    """Replace large data-URI layout images with '@img/<sha1>.<ext>' (served from /img/)."""
    for im in (d.get("layout") or {}).get("images") or []:
        src = im.get("source")
        if not (isinstance(src, str) and src.startswith("data:") and len(src) > IMG_EXTERNALIZE_BYTES):
            continue
        head, _, payload = src.partition(",")
        if ";base64" not in head:
            continue
        mime = head[5:].split(";")[0] or "application/octet-stream"
        raw = base64.b64decode(payload)
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "bin")
        name = f"{hashlib.sha1(raw).hexdigest()[:20]}.{ext}"
        with _LOCK:
            IMAGES[name] = (raw, mime)
            while len(IMAGES) > IMG_KEEP:
                IMAGES.pop(next(iter(IMAGES)))
        im["source"] = f"@img/{name}"
    return d


def fig_dict(fig) -> dict:
    """Figure (or its dict) -> plain dict with large images externalised (to_dict() deep-copies: the cached figure is untouched)."""
    return _externalize_images(fig.to_dict() if hasattr(fig, "to_dict") else dict(fig))


def fig_json(fig) -> str:
    """Figure (or its dict) -> JSON text, exactly plotly's encoder, with large images externalised."""
    from plotly.io.json import to_json_plotly

    return to_json_plotly(fig_dict(fig))


def strip_map_range(d: dict) -> dict:
    """Remove xaxis / yaxis range + autorange from a map figure dict: the browser owns the view (the frame
    travels in the envelope as "view"); a re-sent range would reset the user's zoom on every tick because
    uirevision only preserves GUI state for attributes whose incoming value is UNCHANGED."""
    lay = d.get("layout") or {}
    for ax in ("xaxis", "yaxis"):
        a = lay.get(ax)
        if isinstance(a, dict):
            a.pop("range", None)
            a.pop("autorange", None)
    return d


def push(sid: str, figs: dict, *, t_now: float, clock: str, frozen: bool, heads: dict | None = None, head_imgs: list | None = None,
         tails: dict | None = None, view: dict | None = None, ui: dict | None = None, more: list | None = None,
         sat_fn=None, sat_on: bool | None = None, pills: list | None = None, events: list | None = None, hud: dict | None = None, **_ignored) -> int:
    """Publish this tick for one browser session.  ``figs`` may hold ANY subset of map / sep / err / meas
    (None or missing = unchanged: the store keeps the previous JSON and its stamp).  Per-tick extras
    (each replaced when given, else kept): ``heads`` = ih.plots.heads(...), ``head_imgs`` =
    ih.plots.heads_images(...), ``tails`` = ih.plots.trail_tails(...) (live-segment anchors), ``view`` =
    {x0,x1,y0,y1,rev,mode} the current frame (applied by the browser on the first paint / rev change
    only), ``ui`` = {font_px, line_w, preset}, ``more`` = the "More" chip items (ultrawide mode draws them
    in the panel), ``sat_fn(x0,x1,y0,y1) -> {img,x0,x1,y0,y1}`` + ``sat_on`` = how to fetch satellite
    tiles for the browser's view; ``pills`` = ih.plots.track_pills(A) (map track-number annotations, relayouted
    with the heads every tick), ``events`` = the track-change rows for the panel's TRACK CHANGES card.  The map
    figure's axis ranges are STRIPPED (the browser owns the view).  Returns the new envelope stamp (ms, strictly increasing)."""
    from plotly.io.json import to_json_plotly

    new: dict[str, bytes] = {}
    for k in FIG_KEYS:
        f = figs.get(k)
        if f is None:
            continue
        d = fig_dict(f)
        if k == "map":
            strip_map_range(d)
        new[k] = to_json_plotly(d).encode()
    now = time.time()
    with _LOCK:
        prev = STORE.get(sid)
        stamp = max(int(now * 1000), int(prev["stamp"]) + 1 if prev else 0)
        keep = lambda k, v: v if v is not None else (prev.get(k) if prev else None)  # noqa: E731
        ent = {"stamp": stamp, "clock": clock, "t_now": float(t_now), "frozen": bool(frozen), "wall": now,
               "figs": dict(prev["figs"]) if prev else {}, "fig_stamps": dict(prev["fig_stamps"]) if prev else {},
               "heads": keep("heads", heads), "head_imgs": keep("head_imgs", head_imgs), "tails": keep("tails", tails),
               "view": keep("view", view), "ui": keep("ui", ui), "more": keep("more", more),
               "pills": keep("pills", pills), "events": keep("events", events), "hud": keep("hud", hud),
               "sat_fn": keep("sat_fn", sat_fn), "sat_on": keep("sat_on", sat_on)}
        for k, b in new.items():
            ent["figs"][k] = b
            ent["fig_stamps"][k] = stamp
        STORE[sid] = ent
        for k in [k for k, v in STORE.items() if now - v["wall"] > STALE_SID_S]:
            STORE.pop(k, None)
    return stamp


def set_frozen(sid: str, frozen: bool) -> None:
    with _LOCK:
        ent = STORE.get(sid)
        if ent is not None:
            ent["frozen"] = bool(frozen)
            ent["wall"] = time.time()


def has(sid: str) -> bool:
    with _LOCK:
        return sid in STORE


def figs_of(sid: str) -> dict:
    """Tests / tooling: the CURRENT figure specs the browser would hold, parsed ({map|sep|err|meas: {data, layout}})."""
    with _LOCK:
        ent = STORE[sid]
        return {k: json.loads(b) for k, b in ent["figs"].items()}


# ── panel HTML (constant per session + preset: sid / host / port / cadence / geometry baked in) ──
def host_from_header(h: str | None) -> str:
    """'172.18.1.28:8901' -> '172.18.1.28'; '[fd7a::1]:8901' -> '[fd7a::1]'; None -> ''."""
    h = str(h or "").strip()
    if h.startswith("["):                     # IPv6 literal
        return h[: h.find("]") + 1] if "]" in h else h
    return h.rsplit(":", 1)[0] if ":" in h else h


def browser_host() -> str:
    """Hostname the browser used to reach Streamlit (st.context Host header, port stripped) — so
    LAN and Tailscale clients each get their own :PORT base; '' when unknown -> the panel JS
    falls back to parent.location.hostname."""
    try:
        import streamlit as st

        return host_from_header(st.context.headers.get("Host"))
    except Exception:
        return ""


def panel_css(L: dict | None = None) -> str:
    """Panel CSS for a layout (default preset when None).  The px heights are the preset's server-side
    geometry — the starting picture before the JS measures the real iframe width; the JS then sets the
    grid class (one / two / three) and inline heights."""
    L = L or LAYOUT
    return f"""<style>
html{{font-size:calc(16px * var(--ih-scale,1));}}
html,body{{margin:0;padding:0;background:{T.SURFACE};color:{T.INK};overflow:hidden;font-family:{T.SANS};}}
html.one,html.one body{{overflow-y:auto;overflow-x:hidden;}}
.grid{{display:grid;gap:0 12px;box-sizing:border-box;}}
.grid.two{{grid-template-columns:{L['split'][0]}fr {L['split'][1]}fr;grid-template-rows:auto auto;}}
.grid.two .cl{{grid-column:1;grid-row:1/3;}} .grid.two .cm{{grid-column:2;grid-row:1;}} .grid.two .cr{{grid-column:2;grid-row:2;}}
.grid.three{{grid-template-columns:{L['split3'][0]}fr {L['split3'][1]}fr {L['split3'][2]}fr;grid-template-rows:auto;}}
.grid.three .cl{{grid-column:1;}} .grid.three .cm{{grid-column:2;}} .grid.three .cr{{grid-column:3;}}
.grid.one{{grid-template-columns:auto minmax(0,1fr);grid-template-rows:auto auto;gap:10px {COL_GAP_PX}px;}}   /* two-up first row (JS sets the map column px): square map | separation; err + vel full-width below */
.grid.one .cl{{grid-column:1;grid-row:1;}} .grid.one .cm{{grid-column:2;grid-row:1;}} .grid.one .cr{{grid-column:1/3;grid-row:2;}}
.col{{display:flex;flex-direction:column;min-width:0;position:relative;}}
.h{{display:flex;align-items:baseline;gap:8px;height:{L['header']}px;box-sizing:border-box;padding:0 10px 4px;background:{T.CARD};border-bottom:1px solid {T.RULE};
    font:500 var(--hf,1rem)/1.3 {T.MONO};letter-spacing:.08em;text-transform:uppercase;color:{T.INK};flex:none;overflow:hidden;white-space:nowrap;}}   /* title = primary ink, --hf >= 18 px (JS) */
.h::before{{content:"";display:inline-block;width:6px;height:6px;background:{T.RED};flex:none;align-self:center;}}
.h>span:first-child{{color:{T.INK};}}
.h .r{{margin-left:auto;min-width:0;flex:0 1 auto;display:inline-flex;align-items:baseline;gap:12px;font-size:calc(var(--hf,1rem) * .85);text-transform:none;letter-spacing:0;color:{T.INK3};overflow:hidden;white-space:nowrap;}}   /* caption / right text: .85 of the title */
.h .r b{{color:{T.INK2};font-weight:500;}}
.h .r .cap[hidden]{{display:none;}}
.fig{{width:100%;min-width:0;flex:none;background:{T.CARD};}}
#map{{height:{L['map']}px;}} #sep{{height:{L['sep']}px;}} #err{{height:{L['err']}px;}} #vel{{height:{L['vel']}px;}}
.blk{{display:flex;flex-direction:column;min-width:0;flex:none;}}
.grid.two #velblock .h{{display:none;}}   /* two-column: the three card strips carry the titles, the legend the series — the map keeps the row */
#st,#tw,#hud{{font:500 var(--hf,1rem)/1 {T.MONO};letter-spacing:.06em;text-transform:none;color:{T.INK2};}}
#hud{{color:{T.INK};}} #hud:empty{{display:none;}}
#tw{{color:{T.INK3};}}
.ov{{position:absolute;left:0;top:0;width:0;height:0;overflow:hidden;pointer-events:none;z-index:2;}}   /* the DOM overlay over the map's PLOT AREA (heads / leaders / live segments / pills): clipped like a layout image, never catches the mouse */
.ov svg{{position:absolute;left:0;top:0;width:100%;height:100%;display:block;}}
.ov .ov-head{{position:absolute;left:0;top:0;width:44px;height:44px;will-change:transform;transform-origin:50% 50%;user-select:none;filter:drop-shadow(0 0 3px #000);}}   /* ONE base SVG per role, rotated with the transform */
.ov .ov-pill{{position:absolute;left:0;top:0;white-space:nowrap;font:500 15px/1.15 {T.MONO};letter-spacing:0;color:{T.INK};background:{T.CARD};border:2px solid {T.RULE};padding:2px 3px;box-sizing:border-box;will-change:transform;}}   /* track-number pill: the annotation look (primary ink, CARD, 2 px role-colour border) */
.g{{display:inline-flex;align-items:center;line-height:1;flex:none;}} .g svg{{width:1em;height:1em;display:block;}}
.gw{{display:inline-flex;align-items:center;gap:.3em;vertical-align:text-bottom;}}
.g.ok{{color:{T.GREEN};}} .g.amber,.g.pause,.g.reset{{color:{T.AMBER};}} .g.fail{{color:{T.FAIL};}} .g.na{{color:{T.INK3};}} .g.red{{color:{T.RED};}}
#pill{{cursor:pointer;background:rgba(26,29,34,.92);border:1px solid {T.RULE};color:{T.INK};flex:none;
      font:500 calc(var(--hf,1rem) * .85)/1 {T.MONO};letter-spacing:.06em;padding:2px 8px;border-radius:2px;text-transform:none;}}   /* view-lock chip: IN the map header row (2026-09-14: it covered the plot) */
#pill:hover{{border-color:{T.RED};}} #pill b{{color:{T.INK};font-weight:500;}}
#more{{display:none;flex:1 1 auto;min-height:0;overflow:auto;padding:10px 0 0;}}
.grid.three #more{{display:none;}}   /* the velocity cards took the middle column; the chips live in the More expander */
.ih-chips{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;}}
.ih-chip{{display:flex;flex-direction:column;gap:4px;background:{T.CARD2};border-top:2px solid {T.RULE};padding:10px 12px;min-width:0;overflow-wrap:anywhere;}}
.ih-chip .k{{font:500 var(--cf,.85rem)/1.3 {T.MONO};color:{T.INK2};}}
.ih-chip .v{{font:600 calc(var(--cf,.85rem) * 1.6)/1.15 {T.SANS};color:{T.INK};letter-spacing:-.01em;}}
.ih-chip .s{{font:500 var(--cf,.85rem)/1.4 {T.MONO};color:{T.INK2};}}
.ih-chip .st{{font:500 var(--cf,.85rem)/1 {T.MONO};letter-spacing:.08em;text-transform:uppercase;color:{T.INK2};padding-top:4px;}}
.ih-chip.ok{{border-top-color:{T.GREEN};}} .ih-chip.amber{{border-top-color:{T.AMBER};}} .ih-chip.fail{{border-top-color:{T.FAIL};}} .ih-chip.na{{border-top-color:{T.RULE};}}
#more .ih-chips+.ih-events{{margin-top:8px;}}
.ih-events{{display:flex;flex-direction:column;gap:2px;background:{T.CARD2};border-top:2px solid {T.RULE};padding:10px 12px;min-width:0;}}
.ih-events .k{{font:500 var(--cf,.85rem)/1.3 {T.MONO};letter-spacing:.08em;text-transform:uppercase;color:{T.INK2};padding-bottom:4px;}}
.ih-events .e{{font:400 calc(var(--cf,.85rem) * 1.1)/1.6 {T.MONO};color:{T.INK2};overflow-wrap:anywhere;}}
.ih-events .e b{{color:{T.INK};font-weight:600;}} .ih-events .e .r{{color:{T.INK3};}} .ih-events .none{{color:{T.INK3};}}
</style>"""


def panel_body() -> str:
    H = HEADERS

    def hdr(k: str, right_html: str) -> str:
        r = f'<span class="r">{right_html}</span>' if right_html else ""
        return f'<div class="h" title="{T.esc(H[k][0])}{(" — " + T.esc(H[k][1])) if H[k][1] else ""}"><span>{T.esc(H[k][0])}</span>{r}</div>'

    def cap(k: str) -> str:   # the explanatory caption: hidden as a whole by fitCaptions() when the column is too narrow for it
        return f'<span class="cap">{T.esc(H[k][1])}</span>'

    # the vehicle heads / pills live in the DOM overlay the panel JS appends INSIDE the map div after the first paint (ovMake) — the body
    # holds nothing else over the map (the view-lock chip sits in the map HEADER row); the "More" chips (#more) only show in the ultrawide three-column mode
    return (f'<div class="grid two" id="grid">'
            f'<div class="col cl">{hdr("map", cap("map") + "<span id=tw></span>" + f'<span id="pill" hidden title="Your zoom / pan is kept; click to release and re-apply the frame">view locked · <b>{T.glyph("amber", "reset", icon="reset")}</b></span>')}<div id="map" class="fig"></div></div>'
            f'<div class="col cm">{hdr("sep", "<span id=hud></span><span id=st>" + T.glyph("na", "connecting…") + "</span>")}<div id="sep" class="fig"></div>'
            f'<div class="blk" id="velblock">{hdr("vel", cap("vel"))}<div id="vel" class="fig"></div></div><div id="more"></div></div>'
            f'<div class="col cr">{hdr("err", cap("err"))}<div id="err" class="fig"></div></div>'
            f'</div>')


HELPERS_JS = """
// ── vehicle heads / leaders / live segments / track pills: a DOM OVERLAY over the map's PLOT AREA — zero Plotly work between reacts ──
var HEADIMGS=[], PILLS=[];                                 // envelope extras: head_imgs (fallback layout-image dicts, not drawn here) / pill annotation dicts (x/y m, text, shifts, colours)
var OV={el:null,svg:null,img:{},pill:{},line:{},geom:null,relayouts:0,frames:0,cost:[],avg:null,state:{heads:{tgt:null,itc:null},pills:[]},size:0,text:{},box:{}};
var SAT_DIRTY=false, SATN=0;                               // a new satellite tile not yet in the map's layout.images / count of the tile-only relayouts
function mpp(){                                            // metres per pixel of the CURRENT map view (x; equal aspect makes y the same)
  var gd=document.getElementById("map"), fl=gd&&gd._fullLayout;
  if(fl&&fl.xaxis&&fl.xaxis.range&&fl.width){ var w=Math.max(1,fl.width-fl.margin.l-fl.margin.r); return Math.abs(fl.xaxis.range[1]-fl.xaxis.range[0])/w; }
  var v=(typeof VIEW!=="undefined"&&VIEW.applied)||null, p=plotPx(); return v?Math.abs(v.x[1]-v.x[0])/p.w:1.0;
}
function iconPx(){                                        // the icon's ON-SCREEN edge: ICON_PX x text scale, capped at ICON_FRAC (14 %) of the map's PLOT-AREA HEIGHT (>= 32): constant whatever the zoom,
  var sc=(UI&&UI.text_scale)||1, ph=plotPx().h;            // 44 px on a >= 315 px plot area, 37 px on a 267 px laptop map (2026-09-17: the 10 % cap gave 27 px vehicles on a 1366x768 laptop — unreadable on satellite)
  return Math.max(40,Math.min(Math.round(ICON_PX*sc),Math.round(ICON_FRAC*ph)));   // 2026-09-17 pm: 60 px, 18 % cap, floor 40 ("make the icons larger")
}
function iconSizeM(){ return iconPx()*mpp(); }             // the same edge in metres at the current scale (diagnostics)
function isPill(a){ return !!(a&&a.name&&String(a.name).indexOf("pill_")===0); }
function pillRole(a){ var n=String(a&&a.name||""); return n.indexOf("pill_itc")===0?"itc":(n.indexOf("pill_tgt")===0?"tgt":null); }
function axes(){                                           // the map's CURRENT axes (gd._fullLayout is the truth: the browser owns the view) or null before the first paint
  var gd=document.getElementById("map"), fl=gd&&gd._fullLayout, xa=fl&&fl.xaxis, ya=fl&&fl.yaxis;
  if(!xa||!ya||typeof xa.l2p!=="function"||xa._length==null||xa._offset==null) return null;
  return {gd:gd,xa:xa,ya:ya};
}
function ovMake(gd){                                       // once, right after the first newPlot: the overlay div INSIDE the map div (plotly's react never touches foreign children)
  if(OV.el||!gd) return OV.el;
  if(getComputedStyle(gd).position==="static") gd.style.position="relative";
  var el=document.createElement("div"); el.className="ov"; el.id="ov";
  var svg=document.createElementNS(NS,"svg"); svg.setAttribute("class","ov-lines");
  ["tgt","itc"].forEach(function(k){ ["live","leader"].forEach(function(w){ var ln=document.createElementNS(NS,"line"); ln.setAttribute("class","ov-"+w+" ov-"+k); ln.setAttribute("data-name",k+"_"+w);
    ln.setAttribute("stroke",k==="tgt"?TGT_COLOR:ITC_COLOR); ln.setAttribute("stroke-width",w==="live"?String((geo&&geo.lw)||2.5):"1"); ln.setAttribute("visibility","hidden"); svg.appendChild(ln); OV.line[k+"_"+w]=ln; }); });
  el.appendChild(svg); OV.svg=svg;
  ["tgt","itc"].forEach(function(k){ var im=document.createElement("img"); im.className="ov-head ov-"+k; im.id="ov-head-"+k; im.alt=""; im.draggable=false; im.src=HEAD_SRC[k]; im.hidden=true; el.appendChild(im); OV.img[k]=im;
    var p=document.createElement("div"); p.className="ov-pill ov-"+k; p.id="ov-pill-"+k; p.hidden=true; el.appendChild(p); OV.pill[k]=p; });
  gd.appendChild(el); OV.el=el; return el;
}
function ovGeom(ax){                                       // the overlay = the PLOT AREA: left/top = the axes' _offset, size = their _length (re-read every frame: drag / zoom / resize)
  var g={l:ax.xa._offset,t:ax.ya._offset,w:ax.xa._length,h:ax.ya._length}, o=OV.geom;
  if(!o||o.l!==g.l||o.t!==g.t||o.w!==g.w||o.h!==g.h){ OV.el.style.left=g.l+"px"; OV.el.style.top=g.t+"px"; OV.el.style.width=g.w+"px"; OV.el.style.height=g.h+"px"; OV.geom=g; }
  return g;
}
function hideEl(e){ if(e&&!e.hidden) e.hidden=true; }
function hideLine(ln){ if(ln&&ln.getAttribute("visibility")!=="hidden") ln.setAttribute("visibility","hidden"); }
function setLine(ln,x0,y0,x1,y1,w){ ln.setAttribute("x1",x0.toFixed(1)); ln.setAttribute("y1",y0.toFixed(1)); ln.setAttribute("x2",x1.toFixed(1)); ln.setAttribute("y2",y1.toFixed(1));
  if(w!=null&&ln.getAttribute("stroke-width")!==String(w)) ln.setAttribute("stroke-width",String(w)); if(ln.getAttribute("visibility")!=="visible") ln.setAttribute("visibility","visible"); }
function ovPills(xa,ya,g){                                 // the pill annotation dicts -> positioned divs: anchor + xshift/yshift like plotly, hidden when the anchor is out of range
  var used={}, placed=[], out=[];                          // (a data-anchored annotation), clamped inside the plot area, collision-stacked in PIXEL space (a later pill drops under an earlier one)
  (PILLS||[]).forEach(function(a,i){ var k=pillRole(a)||(i===0?"tgt":"itc"); if(used[k]||!OV.pill[k]) return; used[k]=true; var el=OV.pill[k];
    var px=xa.l2p(a.x), py=ya.l2p(a.y);
    if(!(px>=0&&px<=g.w&&py>=0&&py<=g.h)){ hideEl(el); return; }
    var txt=String(a.text==null?"":a.text), bc=a.bordercolor||RULE_COLOR, bg=a.bgcolor||CARD_COLOR, fs=(a.font&&a.font.size)||13, fc=(a.font&&a.font.color)||INK_COLOR, sig=txt+"|"+bc+"|"+bg+"|"+fs+"|"+fc;
    if(OV.text[k]!==sig){ el.textContent=txt; el.style.borderColor=bc; el.style.background=bg; el.style.fontSize=fs+"px"; el.style.color=fc; OV.text[k]=sig; OV.box[k]=null; }   // text / colours only when they change: no re-render jitter
    if(el.hidden) el.hidden=false;
    var b=OV.box[k]; if(!b){ b={w:el.offsetWidth,h:el.offsetHeight}; OV.box[k]=b; }                        // measured once per text (offsetWidth ignores the transform)
    var xs=Number(a.xshift||0), ys=Number(a.yshift||0), xan=a.xanchor||"left", yan=a.yanchor||"bottom";
    var left=px+xs-(xan==="right"?b.w:(xan==="center"?b.w/2:0)), top=py-ys-(yan==="bottom"?b.h:(yan==="middle"?b.h/2:0));
    left=Math.max(0,Math.min(g.w-b.w,left)); top=Math.max(0,Math.min(g.h-b.h,top));
    for(var j=0,n=0;j<placed.length&&n<8;j++){ var q=placed[j]; if(left<q.left+q.w+2&&left+b.w+2>q.left&&top<q.top+q.h+2&&top+b.h+2>q.top){ top=q.top+q.h+4; if(top+b.h>g.h) top=Math.max(0,q.top-b.h-4); n++; j=-1; } }
    el.style.transform="translate("+left.toFixed(1)+"px,"+top.toFixed(1)+"px)";
    placed.push({left:left,top:top,w:b.w,h:b.h}); out.push({role:k,text:txt,left:left,top:top,w:b.w,h:b.h}); });
  ["tgt","itc"].forEach(function(k){ if(!used[k]) hideEl(OV.pill[k]); });
  return out;
}
function ovDraw(st){                                       // ONE overlay update for a tween state: heads (translate + rotate), live segments, leaders, pills.  NO Plotly call.
  var ax=axes(); if(!ax||!made.map||DEAD) return false; if(!OV.el) ovMake(ax.gd);
  var t0=performance.now(), g=ovGeom(ax), xa=ax.xa, ya=ax.ya, sz=iconPx(), m=mpp(), out={heads:{tgt:null,itc:null},pills:[]};
  if(st===undefined) st=tweenState(tweenNow());
  if(sz!==OV.size){ ["tgt","itc"].forEach(function(k){ OV.img[k].style.width=OV.img[k].style.height=sz+"px"; }); OV.size=sz; }
  ["tgt","itc"].forEach(function(k){
    var h=st&&st.heads&&st.heads[k], im=OV.img[k];
    if(!h){ hideEl(im); hideLine(OV.line[k+"_live"]); hideLine(OV.line[k+"_leader"]); return; }
    var px=xa.l2p(h.E), py=ya.l2p(h.N), on=px>-sz&&px<g.w+sz&&py>-sz&&py<g.h+sz;   // wholly outside the plot area (+ one icon): hidden; partly outside: the overflow clip cuts it at the edge
    if(on){ im.style.transform="translate("+(px-sz/2).toFixed(1)+"px,"+(py-sz/2).toFixed(1)+"px) rotate("+h.hdg.toFixed(1)+"deg)"; if(im.hidden) im.hidden=false; } else hideEl(im);
    var tail=TW.tails&&TW.tails[k];
    if(tail) setLine(OV.line[k+"_live"],xa.l2p(tail[0]),ya.l2p(tail[1]),px,py,(geo&&geo.lw)||2.5); else hideLine(OV.line[k+"_live"]);
    var v=h.spd||0;                                        // 5 s velocity leader: data direction, on-screen length floored / capped
    if(v>0.5&&m>0){ var len=Math.min(Math.max(v*5.0/m,LEADER_MIN_PX),LEADER_MAX_PX), hr=h.hdg*Math.PI/180; setLine(OV.line[k+"_leader"],px,py,px+len*Math.sin(hr),py-len*Math.cos(hr),1); } else hideLine(OV.line[k+"_leader"]);
    out.heads[k]={px:px,py:py,hdg:h.hdg,size:sz,visible:on,E:h.E,N:h.N,spd:v};
  });
  out.pills=ovPills(xa,ya,g);
  OV.state=out; OV.frames++; var ms=performance.now()-t0; OV.cost.push(ms); if(OV.cost.length>60) OV.cost.shift(); OV.avg=OV.cost.reduce(function(s,v){ return s+v; },0)/OV.cost.length;
  return true;
}
function ovNow(){ try{ ovDraw(tweenState(tweenNow())); }catch(e){ console.warn("overlay skipped",e); } }   // synchronous re-place (plotly_relayouting / plotly_relayout / after a resize)
function placeHeads(){ ovNow(); return Promise.resolve(); }   // "update the overlay" — a promise for the applyEnvelope chain, no Plotly call
function satFlush(){                                       // a NEW satellite tile with no map react to carry it: ONE Plotly.relayout({images}) — the only Plotly edit outside reacts / view changes
  if(!SAT_DIRTY||!made.map||reacting||fitting>0||!window.Plotly||DEAD) return Promise.resolve();
  var gd=document.getElementById("map"); if(!gd||!gd._fullLayout||!gd._fullLayout._plots) return Promise.resolve();
  SAT_DIRTY=false; SATN++;
  try{ return Plotly.relayout(gd,{images:(satImgs||[]).slice()}).catch(function(e){ console.warn("tile relayout failed",e); }); }catch(e){ console.warn("tile skipped",e); return Promise.resolve(); }
}
function mapMargin(){                                     // the map figure's margins: the drawn ones, else the ones it arrived with, else the server's MAP_M
  var gd=document.getElementById("map"), fl=gd&&gd._fullLayout;
  if(fl&&fl.margin&&fl.margin.l!=null) return fl.margin;
  var lm=gd&&gd.layout&&gd.layout.margin; if(lm&&lm.l!=null) return lm;
  var m=Object.assign({},(typeof MAP_M!=="undefined"&&MAP_M)||{l:83,r:12,t:62,b:56});
  if(typeof geo!=="undefined"&&geo&&geo.mode==="one"&&typeof MAP_T0!=="undefined") m.t=MAP_T0;   // mode "one" drops the key row: the pre-paint plot area must use the reduced top margin too
  return m;
}
function plotPx(){                                        // the map's plot-area size in px (before the first paint: the div minus the map's FIXED margins — ih.plots.map_margin, not a guess)
  var gd=document.getElementById("map"), fl=gd&&gd._fullLayout;
  if(fl&&fl.width&&fl.height&&fl.margin) return {w:Math.max(1,fl.width-fl.margin.l-fl.margin.r),h:Math.max(1,fl.height-fl.margin.t-fl.margin.b)};
  var W=(gd&&gd.clientWidth)||800, H=(geo&&geo.map)||(gd&&gd.clientHeight)||600, m=mapMargin();
  return {w:Math.max(1,W-m.l-m.r),h:Math.max(1,H-m.t-m.b)};
}
function aspectRange(x,y){                                // a box -> axis ranges CONSISTENT with 1:1 pixels (the box fits centred; extra room on the long side)
  var p=plotPx(), m=Math.max((x[1]-x[0])/p.w,(y[1]-y[0])/p.h), cx=(x[0]+x[1])/2, cy=(y[0]+y[1])/2;
  return {x:[cx-m*p.w/2,cx+m*p.w/2],y:[cy-m*p.h/2,cy+m*p.h/2]};
}
function hasWebGL(){   // scattergl needs WebGL; without it (some remote desktops / headless) fall back to SVG scatter, same look
  try{ var c=document.createElement("canvas"); return !!(window.WebGLRenderingContext&&(c.getContext("webgl")||c.getContext("experimental-webgl"))); }catch(e){ return false; }
}
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
function gw(c,w){ return '<span class="gw"><span class="g '+esc(c)+'">'+(ICONS[c]||ICONS.na)+'</span>'+esc(w)+'</span>'; }   // status icon (theme SVG, colour on the icon) + word in the text ink
"""

def responsive_js(L: dict | None = None) -> str:
    """The in-iframe geometry: the ONE piece that knows the real iframe width.  geometry(W) ->
    {mode: one|two|three, map, sep, err, col, font, lw}; applyGeometry() sets the grid class + inline heights,
    relayouts height / font sizes on the drawn figures and calls Plotly.Plots.resize on all of them.
    Same rules as layout(): sep ~26 % of the right column, error floored so each card keeps >= 95 px,
    else the stacked column.  Depends on globals made{}, UI{}, Plotly."""
    L = L or LAYOUT
    return f"""
// ── responsive geometry (the iframe knows its width; Streamlit only knows the preset's height) ──
var PANEL={int(L['panel'])}, HEADER={int(L['header'])}, SPLIT=[{int(L['split'][0])},{int(L['split'][1])}], SPLIT3=[{int(L['split3'][0])},{int(L['split3'][1])},{int(L['split3'][2])}];
var SEP_FRAC={SEP_FRAC}, SEP_FRAC3={SEP_FRAC3}, BP={int(BREAKPOINT_PX)}, UW={int(ULTRAWIDE_PX)}, MIN_CARD={int(MIN_CARD_PX)}, ERR_HDR={int(ERR_HDR_PX)}, ERR_T={ERR_T}, ERR_B={ERR_B}, ERR_GAP={ERR_GAP}, ERR_MIN={ERR_MIN_PX}, SEP_MIN={SEP_MIN_PX};
var ICON_PX=60, ICON_FRAC=0.18, LEADER_MAX_PX=90, LEADER_MIN_PX=18;     // vehicle icon edge ON SCREEN (x text scale, <= ICON_FRAC x the plot-area height) and the 5 s velocity leader's on-screen length cap / floor
var COL_GAP={COL_GAP_PX}, MAP_T0={map_top_nolegend_px()}, ONE_RIGHT_MIN={ONE_RIGHT_MIN_PX}, ONE_SEP={ONE_SEP_PX}, ONE_ERR={ONE_ERR_PX}, ONE_VEL={ONE_VEL_PX}, VEL_STRIP={VEL_STRIP_PX}, VEL_STRIP_MIN={VEL_STRIP_MIN_PX}, VEL_STRIP_FRAC={VEL_STRIP_FRAC}, ONE_MAP_FRAC={ONE_MAP_FRAC}, FONT_BP=1400, CARD_PAD_X=0.004, VEL_COL_GAP=0.03;
var MAP_M={json.dumps(map_margin_px(L['font_px'], L.get('text_scale', 1.0)))};   // the map figure's FIXED margins (ih.plots.map_margin at this font / text scale): plotPx()'s pre-paint fallback
var UI={{font_px:{int(L['font_px'])},line_w:{float(L['line_w'])},preset:{json.dumps(L['preset'])},text_scale:{float(L.get('text_scale', 1.0))}}};
var geo=null, fitting=0;                                                // fitting > 0: an applyGeometry relayout / resize chain is in flight (the tween waits: interleaving relayouts raced inside plotly)
function fontFor(W){{ var b=(UI&&UI.font_px)||14, sc=(UI&&UI.text_scale)||1; if(W>=UW) return Math.max(b,Math.round(15*sc)); return Math.max(b,Math.round(13*sc)); }}  // ultrawide >= 15 px x scale, never under 13 x scale
function applyScale(){{ var sc=(UI&&UI.text_scale)||1; document.documentElement.style.setProperty("--ih-scale",String(sc)); }}
function scrollerOf(el){{                                             // the nearest scrolling ancestor in the PARENT document (Streamlit: section[data-testid=stMain], overflow:auto)
  try{{ var n=el&&el.parentElement; while(n){{ var o=getComputedStyle(n).overflowY; if(o==="auto"||o==="scroll") return n; n=n.parentElement; }} }}catch(e){{}}
  return null;
}}
function viewportPanel(){{                                             // A15: the panel fills the browser height — measured from the parent (same-origin Streamlit iframe)
  try{{ var fe=window.frameElement, ph=window.parent&&window.parent.innerHeight; if(!fe||!ph) return PANEL;
    var sc=scrollerOf(fe), top=fe.getBoundingClientRect().top+(sc?sc.scrollTop:0);   // the iframe's offset in the page CONTENT, not the viewport: scrolling the page must never inflate the panel
    var avail=Math.floor(ph-top-20); if(avail<420) return PANEL;
    if(Math.abs(avail-PANEL)>24){{ PANEL=avail; }}
    setFrameHeight(fe, PANEL+4); }}catch(e){{}}
  return PANEL;
}}
var _fhObs=null, _fhWant=0, _fhSeen=[], _fhTimer=null;
function frameChain(fe){{                                              // the iframe + its ancestors up to Streamlit's ELEMENT CONTAINER: Streamlit sizes that wrapper from the element's
  var out=[fe], n=fe&&fe.parentElement;                                 // declared (preset) height (emotion class + a height attribute), so a grown iframe overflowed it and the elements
  while(n&&n.tagName!=="BODY"){{                                        // below (quad toggle, More, footer) overlaid the panel's bottom — every node here takes the iframe's height
    var tag=String(n.getAttribute("data-testid")||"")+" "+String(n.className||"");
    var isEC=/stElementContainer|element-container|stIFrame/.test(tag);
    var hasPx=/^\\d+(\\.\\d+)?px$/.test(n.style.height||"")||/^\\d+(\\.\\d+)?(px)?$/.test(n.getAttribute("height")||"");
    if(isEC||hasPx) out.push(n);
    if(isEC) break;
    n=n.parentElement;
  }}
  return out;
}}
function setFrameHeight(fe, h){{                                        // assert our height on the iframe AND its wrapper chain, and keep re-asserting: Streamlit re-applies the preset
  _fhWant=h;                                                            // height on every rerun without remounting the iframe (byte-identical srcdoc)
  var apply=function(){{ if(!_fhWant) return; var chain=frameChain(fe);
    chain.forEach(function(n){{ var attr=n.getAttribute("height")||"", cur=parseInt(n.style.height||attr||"0",10);
      if(Math.abs(cur-_fhWant)>2||!n.style.height||n.style.getPropertyPriority("height")!=="important"){{
        n.style.setProperty("height", _fhWant+"px", "important"); n.setAttribute("height", n===fe?String(_fhWant):(_fhWant+"px"));
        if(n!==fe){{ n.style.setProperty("min-height", _fhWant+"px", "important"); n.style.setProperty("flex-basis", _fhWant+"px", "important"); }} }}   // the element container is a FLEX ITEM (flex: 0 0 <preset>px): its main size is the flex-basis, not the height
      if(_fhObs&&_fhSeen.indexOf(n)<0){{ _fhSeen.push(n); try{{ _fhObs.observe(n, {{attributes:true, attributeFilter:["style","height","class"]}}); }}catch(e){{}} }} }}); }};
  if(!_fhObs){{ try{{ _fhObs=new MutationObserver(function(){{ requestAnimationFrame(apply); }}); }}catch(e){{}} }}
  apply();
  if(!_fhTimer) _fhTimer=setInterval(apply, 1000);                      // guard: a wrapper node Streamlit re-creates is picked up within a second
}}
function bindParentKeys(){{                                            // "m" in the parent page toggles the MEASUREMENT SPACE section (clicks its toggle button)
  try{{ var pd=window.parent&&window.parent.document; if(!pd) return;
    if(pd.__ihKeyHandler){{ try{{ pd.removeEventListener("keydown", pd.__ihKeyHandler); }}catch(e){{}} }}   // the previous panel iframe's handler dies with its realm on a remount (preset / text size): re-bind from THIS one
    pd.__ihKeyHandler=function(ev){{ if(ev.key!=="m"||ev.ctrlKey||ev.metaKey||ev.altKey) return; var tgt=ev.target&&ev.target.tagName; if(tgt==="INPUT"||tgt==="TEXTAREA") return;
      var btns=pd.querySelectorAll("button"); for(var i=0;i<btns.length;i++){{ if(/MEASUREMENT SPACE/i.test(btns[i].textContent||"")&&btns[i].getBoundingClientRect().height>0){{ btns[i].click(); break; }} }} }};   // the first VISIBLE match (Streamlit keeps a stale twin for a moment during a rerun)
    pd.__ihKeys=true; pd.addEventListener("keydown", pd.__ihKeyHandler); }}catch(e){{}}
}}
function hdrPx(){{ return Math.round(ERR_HDR*((UI&&UI.text_scale)||1)); }}                  // the card header strip AT THIS TEXT SCALE (server: err_hdr_px) — 46 / 53 / 60 px
function errMinPx(){{ return 4*(MIN_CARD+hdrPx())+ERR_T+ERR_B+3*ERR_GAP; }}                 // ... and the four / three card floors built on it (server: err_min_px / one_err_px / one_vel_px)
function oneErrPx(){{ return errMinPx(); }}
function oneSepPx(){{ return Math.round(ONE_SEP*((UI&&UI.text_scale)||1)); }}                // the separation card's autoexpand rows scale with the text too (server: one_sep_px)
function oneVelPx(){{ return 3*(MIN_CARD+hdrPx())+ERR_T+ERR_B+2*ERR_GAP; }}
function velStripPx(){{ var d=hdrPx()-ERR_HDR; return Math.min(VEL_STRIP+d,Math.max(VEL_STRIP_MIN+d,Math.round(VEL_STRIP_FRAC*PANEL))); }}   // the two-column strip grows by the extra strip px (server: vel_strip_px)
function fitTwo(colH){{
  var content=colH-2*HEADER, sep=Math.round(SEP_FRAC*content), err=content-sep;
  if((err-ERR_T-ERR_B-3*ERR_GAP)/4-hdrPx()<MIN_CARD){{ err=errMinPx(); sep=content-err; }}
  return sep<SEP_MIN?null:{{sep:sep,err:err}};
}}
function geometry(W){{
  var font=fontFor(W), lw=W>=UW?Math.max((UI&&UI.line_w)||2.5,3):((UI&&UI.line_w)||2.5);
  if(W>=UW){{                                                            // ultrawide: MAP | separation over the VELOCITY cards | the four error cards (user pick 2026-09-11)
    var lw3=Math.floor((W-2*COL_GAP)*SPLIT3[0]/100), mapH3=Math.min(PANEL-HEADER,lw3), col3=mapH3+HEADER, sep3=Math.round(SEP_FRAC3*(col3-2*HEADER));
    return {{mode:"three",map:mapH3,sep:sep3,vel:col3-2*HEADER-sep3,velCols:1,err:col3-HEADER,col:col3,font:font,lw:lw,W:W}};
  }}
  if(W>=BP){{                                                            // two columns: MAP over the VELOCITY STRIP (3 cards side by side) | sep over err
    var vs=velStripPx();                                                                                  // the strip scales with the panel (fitted 1080p browser: ~166 px) and with the text size
    var leftW=Math.floor((W-COL_GAP)*SPLIT[0]/100), mapH=Math.min(PANEL-HEADER-vs,Math.round(leftW*1.0)), colH=mapH+HEADER+vs, f=fitTwo(colH);   // no block header in two-column mode
    if(f) return {{mode:"two",map:mapH,sep:f.sep,err:f.err,vel:vs,velCols:3,col:colH,font:font,lw:lw,W:W}};
  }}                                                                     // narrow (or the cards would squeeze): mode "one" — TWO-UP first row (server: one_row), err + vel full-width below, scrolls
  var mp=PANEL-HEADER, mm=mapMargin(), ph=mp-MAP_T0-mm.b, mapW=ph+mm.l+mm.r, right=W-mapW-COL_GAP;   // the map DIV width that makes its plot area SQUARE (the key row is off: MAP_T0)
  if(right<ONE_RIGHT_MIN){{ right=Math.min(ONE_RIGHT_MIN,Math.round(W*0.4)); mapW=W-right-COL_GAP; }}   // a very narrow iframe: the separation column keeps its floor, the map narrows (taller than wide)
  return {{mode:"one",map:mp,mapW:mapW,right:right,sep:mp,err:oneErrPx(),vel:oneVelPx(),velCols:1,col:mp+HEADER,font:font,lw:lw,W:W}};
}}
function fontUpdate(gd,f){{                                              // every explicit font size the server set (tickfont / axis title / legend / global)
  var u={{"font.size":f,"legend.font.size":f,"hoverlabel.font.size":f}}, lay=(gd&&gd.layout)||{{}};
  Object.keys(lay).forEach(function(k){{ if(/^[xy]axis\\d*$/.test(k)){{ u[k+".tickfont.size"]=f; u[k+".title.font.size"]=f; }} }});
  return u;
}}
function fitFonts(layout,f){{
  if(!layout||!layout.font||layout.font.size===f) return;
  layout.font.size=f; if(layout.legend&&layout.legend.font) layout.legend.font.size=f; if(layout.hoverlabel&&layout.hoverlabel.font) layout.hoverlabel.font.size=f;
  Object.keys(layout).forEach(function(a){{ if(/^[xy]axis\\d*$/.test(a)){{ var ax=layout[a]; if(ax.tickfont) ax.tickfont.size=f; if(ax.title&&ax.title.font) ax.title.font.size=f; }} }});
}}
function fitLines(data,lw){{                                             // series lines drawn at the server's width take the measured one (3 px on ultrawide)
  var base=(UI&&UI.line_w)||2.5; if(!data||lw===base) return data;
  data.forEach(function(t){{ if(t&&t.line&&t.line.width===base) t.line.width=lw; }}); return data;
}}
function stripOneLine(layout,me,colW,u){{                                 // 2026-09-15 "there is still 2 rows": the card header is ONE row, always — title left,
  var A=(layout&&layout.annotations)||[]; if(!A.length||!me||!(colW>0)) return me?me.hdr:0;   // readout right; containment + CPA/handover labels only when they fit
  var plain=function(a){{ return String(a&&a.text||"").replace(/<[^>]+>/g,""); }}, px=function(a){{ return (a&&a.font&&a.font.size)||13; }}, W=function(a){{ return a?plain(a).length*0.625*px(a):0; }};
  var keys=me.keys||[], line=0;
  keys.forEach(function(k){{ var t=null,r=null,c=null,l=null;
    A.forEach(function(a,j){{ var n=String(a&&a.name||""); if(n==="title_"+k) t=[a,j]; else if(n==="readout_"+k) r=[a,j]; else if(n==="contain_"+k||n==="delta_"+k) c=[a,j]; else if(n==="tick_labels"&&k===keys[0]) l=[a,j]; }});
    if(!t||!r) return;
    var wT=W(t[0]), wR=W(r[0]), wC=W(c&&c[0]), wL=W(l&&l[0]), pad=40;
    if(wT+wR+pad>colW&&/ [(]1σ[)]$/.test(plain(r[0]))){{ u["annotations["+r[1]+"].text"]=String(r[0].text).replace(" (1σ)",""); wR-=5*0.625*px(r[0]); }}
    var one=Math.round(Math.max(px(t[0]),px(r[0]))*1.35)+8;
    if(wT+wR+pad>colW){{                                                 // NARROW card (two-column desktop, ~450 px): title on the top line, readout on the second — never overlapping
      u["annotations["+t[1]+"].yshift"]=one+3; u["annotations["+r[1]+"].yshift"]=3; var showC2=!!c&&(wR+wC+pad+16<=colW);
      u["annotations["+r[1]+"].xshift"]=showC2?-(Math.round(wC)+16):-2;
      if(c){{ u["annotations["+c[1]+"].visible"]=showC2; u["annotations["+c[1]+"].yshift"]=3; u["annotations["+c[1]+"].xshift"]=-2; }}
      if(l){{ u["annotations["+l[1]+"].visible"]=(wT+wL+pad<=colW); u["annotations["+l[1]+"].yshift"]=one+3; u["annotations["+l[1]+"].xshift"]=Math.round(wT)+16; }}
      line=Math.max(line,2*one); return; }}
    var showC=!!c&&(wT+wR+wC+pad+16<=colW), showL=!!l&&(wT+wL+wR+(showC?wC+16:0)+pad+16<=colW);
    u["annotations["+t[1]+"].yshift"]=3; u["annotations["+r[1]+"].yshift"]=3; u["annotations["+r[1]+"].xshift"]=showC?-(Math.round(wC)+16):-2;
    if(c){{ u["annotations["+c[1]+"].visible"]=showC; u["annotations["+c[1]+"].yshift"]=3; u["annotations["+c[1]+"].xshift"]=-2; }}
    if(l){{ u["annotations["+l[1]+"].visible"]=showL; u["annotations["+l[1]+"].yshift"]=3; u["annotations["+l[1]+"].xshift"]=Math.round(wT)+16; }}
    line=Math.max(line,one); }});
  return line||me.hdr;
}}
function errRows(layout,H){{                                             // the error panel's row geometry for height H: the server baked PX header strips / gaps (layout.meta.err)
  var me=(layout&&layout.meta&&layout.meta.err)||null, m=(layout&&layout.margin)||{{t:ERR_T,b:ERR_B}}; if(!me) return null;   // into fractions of ITS plot height; re-derive
  var rows=me.rows||4, u={{}}, colW=(layout.width||((document.getElementById("err")||{{}}).clientWidth)||0)-((m.l||0)+(m.r||0));   // strip text lives in the PLOT-AREA width (x domain)
  var hdr=stripOneLine(layout,me,colW,u), mt=m.t-(me.hdr-hdr); if(hdr!==me.hdr) u["margin.t"]=mt;                                 // one-line strip: the saved px go to the plots
  var ph=Math.max(1,H-mt-m.b), rowH=(ph-(rows-1)*(me.gap+hdr))/rows;                                                              // them for OURS so the strips stay exactly hdr px
  if(rowH<20) return null;
  var f3=(geo&&geo.font)||(layout.font&&layout.font.size)||13;
  for(var i=0;i<rows;i++){{ var top=i*(rowH+me.gap+hdr), d1=1-top/ph, d0=1-(top+rowH)/ph, ax="yaxis"+(i?String(i+1):"");
    u[ax+".domain"]=[Math.max(0,d0),Math.min(1,d1)];
    var rg=layout[ax]&&layout[ax].range; if(rg&&rowH>=3.2*1.35*f3){{ u[ax+".tickvals"]=(rg[0]<0&&rg[1]>0)?[rg[0]/2,0,rg[1]/2]:[rg[0],(rg[0]+rg[1])/2,rg[1]]; }}   // 2026-09-15: the one-row header makes the drawn row taller than the server's estimate -> the middle ("0") label fits again
    (layout.shapes||[]).forEach(function(sh,j){{ if(sh&&sh.name==="card_"+(me.keys||[])[i]){{ u["shapes["+j+"].y0"]=d0-me.pad/ph; u["shapes["+j+"].y1"]=d1+(hdr+me.pad)/ph; }} if(sh&&sh.name==="hdr_"+(me.keys||[])[i]){{ u["shapes["+j+"].y0"]=d1; u["shapes["+j+"].y1"]=d1+(hdr+me.pad)/ph; }} }}); }}
  if(layout.showlegend) u["legend.y"]=1+(hdr+me.pad)/ph;
  return u;
}}
function cardGrid(layout,me,H,cols){{                                  // a card figure (meta.err / meta.vel) for height H: stacked rows (cols 1) or side by side (cols n)
  var m=(layout&&layout.margin)||{{t:ERR_T,b:ERR_B}}; if(!me) return null;
  var rows=me.rows||3, ph=Math.max(1,H-m.t-m.b), u={{}}, keys=me.keys||[], axis=me.axis||{{}};
  (layout.annotations||[]).forEach(function(a,j){{ var n=String(a&&a.name||""); var m=n.match(/^(readout|delta|title)_([a-z0-9]+?)(_s)?$/); if(!m) return;   // long readouts + titles stacked, the short ones side by side (one row)
    u["annotations["+j+"].visible"]=(!!m[3])===(!!cols&&cols>=2);
    if(m[3]&&cols>=2){{ var gdw=(layout.width||(document.getElementById("vel")||{{}}).clientWidth||900), colW=gdw/cols; if(m[1]==="readout") u["annotations["+j+"].font.size"]=Math.max(12,Math.min(18,Math.floor((colW-24)/(26*0.625)))); if(m[1]==="title") u["annotations["+j+"].yshift"]=Math.round(me.hdr*0.5)+4; }} }});   // title box clear of the numbers box   // numbers line sized to the card (26 glyphs incl. the unit); title keeps its size   // one row just above the plot: 4-glyph title + 21-glyph numbers + gaps, 12..18 px   // 2026-09-15: the compact line fills the card width
  if(!cols||cols<2){{                                                      // STACKED: hdr px strips + gaps re-derived as fractions of OUR plot height (like errRows)
    var colW=(layout.width||((document.getElementById("vel")||{{}}).clientWidth)||0)-((m.l||0)+(m.r||0)), hdr=stripOneLine(layout,me,colW,u), mt=m.t-(me.hdr-hdr);
    if(hdr!==me.hdr){{ u["margin.t"]=mt; ph=Math.max(1,H-mt-m.b); }}
    var rowH=(ph-(rows-1)*(me.gap+hdr))/rows; if(rowH<20) return null;
    for(var i=0;i<rows;i++){{ var top=i*(rowH+me.gap+hdr), d1=1-top/ph, d0=1-(top+rowH)/ph, ax="yaxis"+(i?String(i+1):""), xa="xaxis"+(i?String(i+1):"");
      u[ax+".domain"]=[Math.max(0,d0),Math.min(1,d1)]; u[xa+".domain"]=[0,1]; u[xa+".showticklabels"]=(i===rows-1); u[xa+".nticks"]=null; u[xa+".tickangle"]=null; if(axis[keys[i]]!=null) u[ax+".title.text"]=axis[keys[i]];
      (layout.shapes||[]).forEach(function(sh,j){{ if(sh&&sh.name==="card_"+keys[i]){{ u["shapes["+j+"].x0"]=-CARD_PAD_X; u["shapes["+j+"].x1"]=1+CARD_PAD_X; u["shapes["+j+"].y0"]=d0-me.pad/ph; u["shapes["+j+"].y1"]=d1+(hdr+me.pad)/ph; }} if(sh&&sh.name==="hdr_"+keys[i]){{ u["shapes["+j+"].x0"]=-CARD_PAD_X; u["shapes["+j+"].x1"]=1+CARD_PAD_X; u["shapes["+j+"].y0"]=d1; u["shapes["+j+"].y1"]=d1+(hdr+me.pad)/ph; }} }}); }}
  }} else {{                                                                // SIDE BY SIDE: every card takes the full height; its strip sits in the top margin; own x tick labels
    var g=VEL_COL_GAP, w=(1-(cols-1)*g)/cols, hdr1=me.hdr;                                          // 2026-09-15: the SAME two-line strip as the error cards (title / numbers), no legend
    var f0=(geo&&geo.font)||(layout.font&&layout.font.size)||13, mb=Math.round(1.35*f0+12), ml=Math.round(4*0.625*f0+14);   // margins from the font the BROWSER draws (fontUpdate bumps it): "-20" tick label left, one HH:MM:SS row below
    u["margin.t"]=hdr1+me.pad; u["margin.b"]=mb; u["margin.l"]=ml; u["margin.r"]=Math.round(4*0.625*f0); u["showlegend"]=false; ph=Math.max(1,H-(hdr1+me.pad)-mb); if(ph<20) return null;
    for(var i=0;i<rows;i++){{ var x0=i*(w+g), x1=x0+w, ax="yaxis"+(i?String(i+1):""), xa="xaxis"+(i?String(i+1):"");
      u[ax+".domain"]=[0,1]; u[xa+".domain"]=[x0,x1]; u[xa+".showticklabels"]=true; u[xa+".nticks"]=3; u[xa+".tickangle"]=0; u[ax+".title.text"]="";    // the strip title names the card; ~3 horizontal time ticks per narrow card
      (layout.shapes||[]).forEach(function(sh,j){{ if(sh&&sh.name==="card_"+keys[i]){{ u["shapes["+j+"].x0"]=x0-CARD_PAD_X; u["shapes["+j+"].x1"]=x1+CARD_PAD_X; u["shapes["+j+"].y0"]=-me.pad/ph; u["shapes["+j+"].y1"]=1+(hdr1+me.pad)/ph; }} if(sh&&sh.name==="hdr_"+keys[i]){{ u["shapes["+j+"].x0"]=x0-CARD_PAD_X; u["shapes["+j+"].x1"]=x1+CARD_PAD_X; u["shapes["+j+"].y0"]=1; u["shapes["+j+"].y1"]=1+(hdr1+me.pad)/ph; }} }}); }}
  }}
  if(layout.showlegend&&!(cols>=2)) u["legend.y"]=1+(me.hdr+me.pad)/ph;
  return u;
}}
function velRows(layout,H){{ return cardGrid(layout,(layout&&layout.meta&&layout.meta.vel)||null,H,(geo&&geo.velCols)||1); }}
function placeVel(mode){{                                               // the velocity block lives in the MIDDLE column (three), UNDER THE MAP (two) or after the error cards (stacked)
  var b=document.getElementById("velblock"), grid=document.getElementById("grid"); if(!b||!grid) return;
  var host=mode==="two"?grid.querySelector(".cl"):(mode==="three"?grid.querySelector(".cm"):grid.querySelector(".cr")); if(!host||b.parentElement===host) return;
  var more=document.getElementById("more"); if(mode==="three"&&more&&more.parentElement===host) host.insertBefore(b,more); else host.appendChild(b);
}}
function setPath(o,path,v){{                                             // "yaxis2.domain" / "shapes[3].y0" -> nested set on a layout object (the react path)
  var parts=path.replace(/\\[(\\d+)\\]/g,".$1").split("."), n=o; for(var i=0;i<parts.length-1;i++){{ if(n[parts[i]]==null) n[parts[i]]={{}}; n=n[parts[i]]; }} n[parts[parts.length-1]]=v;
}}
var ORIG={{}};                                                          // per figure: the server's own showlegend / margin.t (restored when the panel leaves mode "one")
function legendPatch(k,mode){{                                           // mode "one": the map key row + the separation key duplicate the card headers -> off; the map's top margin keeps only the modebar row
  if(k!=="map"&&k!=="sep") return null;
  var o=ORIG[k]||{{}}, one=mode==="one", u={{showlegend:one?false:(o.showlegend==null?true:o.showlegend)}};
  if(k==="map") u["margin.t"]=one?MAP_T0:(o.t!=null?o.t:MAP_M.t);
  return u;
}}
function fitLayout(k,layout){{                                           // a figure arriving from the server: give it the measured geometry before it is drawn
  if(!geo||!layout) return layout;
  if(geo[k]) layout.height=geo[k];
  if(k==="map"||k==="sep"){{ ORIG[k]={{showlegend:layout.showlegend,t:layout.margin&&layout.margin.t}}; var lp=legendPatch(k,geo.mode); if(lp) Object.keys(lp).forEach(function(p){{ setPath(layout,p,lp[p]); }}); }}
  if(k==="sep"&&layout.margin){{ var hf=Math.round(4.5*0.625*(geo.font||13))+6; if((layout.margin.r||0)<hf) layout.margin.r=hf; var bf=Math.round(1.35*(geo.font||13))+16; if((layout.margin.b||0)<bf) layout.margin.b=bf; }}   // + a full tick-label row below (labels ran 3 px past the card)   // half an HH:MM:SS tick label at the browser font (clipped at 446 px)
  if(k==="err"){{ var u=errRows(layout,layout.height); if(u) Object.keys(u).forEach(function(p){{ setPath(layout,p,u[p]); }}); }}
  if(k==="vel"){{ var v=velRows(layout,layout.height); if(v) Object.keys(v).forEach(function(p){{ setPath(layout,p,v[p]); }}); }}
  fitFonts(layout,geo.font);
  return layout;
}}
function fitCaptions(){{                                                 // a header caption that would overflow its column is hidden WHOLE (the header title keeps the full text on hover)
  var hs=document.querySelectorAll(".h"); for(var i=0;i<hs.length;i++){{ var r=hs[i].querySelector(".r"), c=hs[i].querySelector(".cap"); if(!r||!c) continue;
    c.hidden=false; if(r.scrollWidth>r.clientWidth+1) c.hidden=true; }}
  fitStatus();                                                          // the separation header carries no caption: its status / CPA text degrades instead
}}
var STAT={{cls:"na",word:"connecting…",clock:""}}, HUDT="";              // the separation header's right group: live/frozen + clock (#st) and the validated CPA (#hud)
function statHtml(lvl){{                                                 // 0 full · 1 no seconds · 2 no clock · 3 the status ICON alone (its colour still carries the state)
  var w=STAT.word, c=String(STAT.clock||"");
  if(lvl>=3) return gw(STAT.cls,"");
  if(lvl>=2||!c) return gw(STAT.cls,w);
  return gw(STAT.cls,w+" · "+(lvl>=1?c.slice(0,5):c));
}}
function hudText(lvl){{ var t=String(HUDT||""); return lvl<=0?t:(lvl===1?t.split(" · ")[0]:""); }}   // "CPA 59 m · 07:22:31" -> "CPA 59 m" -> hidden (#hud:empty)
var STAT_STEPS=[[0,0],[1,0],[2,0],[2,1],[3,1],[3,2]];                    // the ladder: the clock's seconds, then the clock, then the CPA's time, then the words
function fitStatus(){{                                                   // the right group NEVER overflows its box and NEVER changes the header's height: it drops detail in a fixed order
  var el=document.getElementById("st"), hd=document.getElementById("hud"), r=el&&el.parentElement; if(!el||!r) return;
  el.title=STAT.word+(STAT.clock?" · "+STAT.clock:""); if(hd) hd.title=HUDT||"";    // whatever the ladder drops stays on hover
  for(var i=0;i<STAT_STEPS.length;i++){{
    el.innerHTML=statHtml(STAT_STEPS[i][0]); if(hd) hd.textContent=hudText(STAT_STEPS[i][1]);
    if(r.scrollWidth<=r.clientWidth+1) return;                           // fits: stop degrading (the earliest step that fits is the one shown)
  }}
}}
function setStatus(cls,word,clock){{ STAT={{cls:cls||"na",word:word==null?"":String(word),clock:clock==null?"":String(clock)}}; fitStatus(); }}
function setHud(t){{ HUDT=t==null?"":String(t); fitStatus(); }}
function applyGeometry(){{
  var W=document.documentElement.clientWidth||window.innerWidth; if(!W||DEAD) return Promise.resolve();   // clientWidth: the grid's real width (mode "one" scrolls -> a 12 px scrollbar innerWidth would count)
  applyScale(); viewportPanel();
  var g=geometry(W), grid=document.getElementById("grid"), changed=!geo||geo.mode!==g.mode||geo.map!==g.map||geo.mapW!==g.mapW||geo.sep!==g.sep||geo.err!==g.err||geo.vel!==g.vel||geo.velCols!==g.velCols||geo.font!==g.font||geo.lw!==g.lw;
  var p0=made.map?plotPx():null;                                         // the map's plot area BEFORE the fit: a changed one re-applies the frame below (the box keeps filling a re-shaped map)
  geo=g;
  document.documentElement.style.setProperty("--hf",Math.max(14,g.font)+"px");        // header title / status text = the measured figure font (never under 14 px; 2026-09-14: the +4 read as "massive")
  document.documentElement.style.setProperty("--cf",Math.max(13,g.font-1)+"px");      // "More" chips one step under it
  grid.className="grid "+g.mode;
  grid.style.gridTemplateColumns=g.mode==="one"?(g.mapW+"px minmax(0,1fr)"):"";   // mode "one": the map column is the px width that squares its plot area; the separation takes the rest
  document.documentElement.className=g.mode==="one"?"one":"";           // stacked mode: the iframe body scrolls (nothing clipped)
  placeVel(g.mode);
  ["map","sep","err","vel"].forEach(function(k){{ var d=document.getElementById(k); if(d) d.style.height=g[k]+"px"; }});
  var cl=grid.querySelector(".cl"), cm=grid.querySelector(".cm"), cr=grid.querySelector(".cr");
  if(g.mode==="one"){{ cl.style.height=cm.style.height=g.col+"px"; cr.style.height="auto"; grid.style.height="auto"; }}   // first row = the iframe height (header + map = header + sep = PANEL)
  else{{ cl.style.height=g.col+"px"; cr.style.height=(g.mode==="three"?g.col:HEADER+g.err)+"px"; cm.style.height=(g.mode==="three"?g.col:HEADER+g.sep)+"px"; grid.style.height=g.col+"px"; }}
  fitCaptions();
  if(!window.Plotly) return Promise.resolve();
  var seq=Promise.resolve(); fitting++;
  ["map","sep","err","vel"].forEach(function(k){{ if(!made[k]) return; var gd=document.getElementById(k);
    seq=seq.then(function(){{ var u=changed?fontUpdate(gd,g.font):{{}}; u.height=g[k]; u.width=Math.round(gd.clientWidth||gd.getBoundingClientRect().width)||null;   // WIDTH PINNED to the div: Plots.resize wrote an explicit layout.width once and never updated it (2026-09-15: every figure stayed 940 px after a sidebar collapse)
      var lp=legendPatch(k,g.mode); if(lp) Object.assign(u,lp);
      if(k==="err"){{ var r=errRows(gd.layout,g[k]); if(r) Object.assign(u,r); }} if(k==="vel"){{ var v=velRows(gd.layout,g[k]); if(v) Object.assign(u,v); }} return Plotly.relayout(gd,u); }})   // GUI state untouched: zoom / pan survive; err rows re-derived for the new height
          .then(function(){{ return Plotly.Plots.resize(gd); }}).catch(function(e){{ console.warn("fit failed",k,e); }}); }});
  var done=function(){{ fitting=Math.max(0,fitting-1); }};
  return seq.then(done,done).then(function(){{ ovNow(); return refitView(p0); }});   // the plot area moved / resized: re-place the overlay now (the frame loop follows anyway); a re-shaped map re-takes its frame
}}
function refitView(p0){{                                                 // the map's plot area changed shape / size under a fit (mode change, sidebar collapse, first two-up row): re-apply OUR frame at 1:1 so the box
  if(!p0||!made.map||typeof VIEW==="undefined"||VIEW.userView||VIEW.prog>0) return;   // keeps filling the map — never while the user holds a zoom / pan (view lock) or our own animate is in flight
  var p1=plotPx(); if(Math.abs(p1.w-p0.w)<=1&&Math.abs(p1.h-p0.h)<=1) return;
  var r=(VIEW.frame&&VIEW.frame.mode!=="follow")?frameRange():null; r=r||VIEW.applied; if(!r) return;
  return applyRange(r.x,r.y,false);
}}
var _rz=null, _rzW=0;
window.addEventListener("resize",function(){{ clearTimeout(_rz); _rz=setTimeout(applyGeometry,120); }});
try{{ if(window.ResizeObserver){{ new ResizeObserver(function(es){{ var w=es&&es[0]&&es[0].contentRect?es[0].contentRect.width:0;   // the grid's width also moves WITHOUT a window resize event settling first (Streamlit's ~300 ms animated
  if(Math.abs(w-_rzW)<1) return; _rzW=w; clearTimeout(_rz); _rz=setTimeout(applyGeometry,120); }}).observe(document.getElementById("grid")); }} }}catch(e){{}}   // sidebar collapse): observe it, debounced like the resize
"""


VIEW_JS = r"""
// ── the browser OWNS the map view ────────────────────────────────────────────
// applied = the last range WE set (first paint / frame change / follow / reset); every server map react re-inserts the CURRENT
// range unchanged so the user's zoom is kept.  userView = the user zoomed / panned / double-clicked -> nothing moves the view until reset.
var VIEW={applied:null,rev:null,userView:false,prog:0,frame:null};
var SATK="";                                             // key of the satellite tile currently shown (the server sends a tile only when it differs)
function curRange(){ var gd=document.getElementById("map"); var fl=gd&&gd._fullLayout; if(!fl||!fl.xaxis||!fl.xaxis.range) return null; return {x:fl.xaxis.range.slice(),y:fl.yaxis.range.slice()}; }
function viewParam(){ var r=made.map?curRange():null; if(!r) return ""; return [r.x[0],r.x[1],r.y[0],r.y[1]].map(function(v){ return Math.round(v); }).join(","); }
function pill(on){ var p=document.getElementById("pill"); if(p&&p.hidden!==!on){ p.hidden=!on; fitCaptions(); } }   // the chip shares the map header's right group with the caption
function lockView(){ if(!VIEW.userView){ VIEW.userView=true; pill(true); } }
function unlockView(){ VIEW.userView=false; pill(false); }
function onMapRelayout(ev){                              // plotly_relayout from the USER (drag / wheel / double-click autorange) -> lock; the overlay is re-placed at once
  var ks=Object.keys(ev||{}), rangeEdit=ks.some(function(k){ return /^[xy]axis\.(range|autorange)/.test(k); });
  if(rangeEdit) ovNow();
  if(VIEW.prog>0) return;                                // our own relayout / animate in flight: not a user edit
  if(rangeEdit) lockView();
}
function applyRange(x,y,animate){                        // OUR range change (never counts as a user edit): first paint / frame rev / follow / reset
  var r=aspectRange(x,y); x=r.x; y=r.y;                   // ALWAYS 1:1 pixels: never hand plotly an x + y pair it cannot honour with scaleanchor (squashed tile)
  VIEW.applied={x:x.slice(),y:y.slice()};
  var gd=document.getElementById("map"); if(!made.map||!window.Plotly) return Promise.resolve();
  VIEW.prog++; var done=function(){ VIEW.prog=Math.max(0,VIEW.prog-1); };
  var p=animate ? Plotly.animate(gd,{layout:{xaxis:{range:x},yaxis:{range:y}}},{transition:{duration:600,easing:"cubic-in-out"},frame:{duration:600,redraw:false}})
                : Plotly.relayout(gd,{"xaxis.range":x,"yaxis.range":y});
  return Promise.resolve(p).catch(function(e){ console.warn("range failed",e); }).then(done,done).then(function(){ satSoon(); });
}
var _satT=null;
function satSoon(){ clearTimeout(_satT); _satT=setTimeout(function(){ if(typeof tick==="function") tick(); },800); }   // a tile for the NEW view, debounced 800 ms (the old one stays up until it lands)
function frameRange(){ var f=VIEW.frame; return f&&f.x0!=null ? {x:[f.x0,f.x1],y:[f.y0,f.y1]} : null; }
function resetView(){ unlockView(); var r=frameRange(); if(r) return applyRange(r.x,r.y,true); return Promise.resolve(); }
function fitView(layout){                                // a map figure from the server: NO ranges of its own (belt and braces: delete them); the range that
  if(!layout) return layout;                             // goes in is the one the user SEES right now (_fullLayout) — so a react can never move the view,
  var r=(made.map&&curRange())||VIEW.applied||frameRange();   // whatever plotly's GUI-state rules do; before the first paint it is the frame from the envelope
  ["xaxis","yaxis"].forEach(function(ax){ if(!layout[ax]) layout[ax]={}; delete layout[ax].autorange; delete layout[ax].range; });
  if(r){ VIEW.applied=VIEW.applied||{x:r.x.slice(),y:r.y.slice()}; layout.xaxis.range=r.x.slice(); layout.yaxis.range=r.y.slice(); }
  return layout;
}
function takeView(v){                                    // envelope "view": remember the frame; apply it only on the first paint / an explicit rev change
  if(!v||v.x0==null) return false;
  VIEW.frame=v;
  if(VIEW.rev===null){ VIEW.rev=v.rev; if(!VIEW.applied) VIEW.applied={x:[v.x0,v.x1],y:[v.y0,v.y1]}; return false; }
  if(v.rev!==VIEW.rev){ VIEW.rev=v.rev; unlockView(); return true; }   // explicit change (preset / Fixed<->Follow / zoom slider / Reset): animate to it
  return false;
}
var EDGE=0.15;
function followCheck(heads){                             // FOLLOW / ENGAGE hysteresis: re-frame (animated) only when a head is within 15 % of the view edge
  if(!VIEW.frame||(VIEW.frame.mode!=="follow"&&VIEW.frame.mode!=="engage")||VIEW.userView||VIEW.prog>0||!made.map) return false;
  var r=curRange(); if(!r) return false;
  var nowT=performance.now(); if(VIEW.lastFrameAt&&nowT-VIEW.lastFrameAt<10000){{ var inside=true; ["tgt","itc"].forEach(function(k){{ var c=heads&&heads[k]; if(c&&(c.E<r.x[0]||c.E>r.x[1]||c.N<r.y[0]||c.N>r.y[1])) inside=false; }}); if(inside) return false; }}   // 2026-09-15: RE-FRAME at most every 10 s unless a head actually LEAVES the view (3 animates / 20 s read as jitter)
  var w=r.x[1]-r.x[0], h=r.y[1]-r.y[0], near=false, xs=[], ys=[];
  ["tgt","itc"].forEach(function(k){ var c=heads&&heads[k]; if(!c) return; xs.push(c.E); ys.push(c.N);
    if(c.E<r.x[0]+EDGE*w||c.E>r.x[1]-EDGE*w||c.N<r.y[0]+EDGE*h||c.N>r.y[1]-EDGE*h) near=true; });
  if(!near||!xs.length) return false;
  VIEW.lastFrameAt=nowT;                                  // the cooldown starts at a RE-FRAME, not at a no-op check
  if(VIEW.frame.mode==="engage"){ var f=frameRange(); if(f) applyRange(f.x,f.y,true); return !!f; }   // ENGAGE: the server's engagement box (passes + vehicles)
  var cx=xs.reduce(function(a,b){ return a+b; },0)/xs.length, cy=ys.reduce(function(a,b){ return a+b; },0)/ys.length;
  applyRange([cx-w/2,cx+w/2],[cy-h/2,cy+h/2],true);       // FOLLOW: same size, re-centred on the vehicles
  return true;
}
// ── client-side tweening: heads interpolated between server ticks, drawn by the DOM overlay (CSS transforms), never by Plotly ──
var TW={prev:null,cur:null,P:1000,rate:30,last:0,cost:[],avg:null,tails:{},frozen:false,tFrozen:null,ticks:0}, SHOW_TW=false;
function quant5(h){ return ((Math.round(h/5)*5)%360+360)%360; }
function lerpAng(a,b,t){ var d=((b-a+540)%360)-180; return a+d*t; }   // shortest arc
function setFrozen(f){ f=!!f; if(f===TW.frozen) return; TW.frozen=f; TW.tFrozen=f?performance.now():null; }   // frozen: the heads stop at THIS instant (still following a pan / zoom)
function tweenNow(){ return TW.frozen&&TW.tFrozen!=null?TW.tFrozen:performance.now(); }
function takeHeads(j){                                   // every envelope with heads: shift the 2-slot buffer; tails / pills / (unused) head_imgs
  if(!j.heads) return;
  TW.prev=TW.cur; TW.cur={t:performance.now(),heads:j.heads};
  if(j.tails) TW.tails=j.tails;
  if(j.head_imgs) HEADIMGS=j.head_imgs;                   // the st.plotly_chart fallback's layout-image dicts ride the envelope; the panel draws the overlay instead
  if(j.pills) PILLS=j.pills;                              // track-number pills follow the newest track point (and flash amber after an id change)
}
function tweenState(now){                                // {heads:{tgt|itc:{E,N,hdg,spd}|null}} at ``now``: lerp prev->cur over one period, predict <= 0.5 period beyond it
  var c=TW.cur, p=TW.prev; if(!c) return null;
  var tau=now-c.t, a=Math.max(0,Math.min(tau/TW.P,1)), extra=Math.max(0,Math.min(tau-TW.P,0.5*TW.P))/1000, out={heads:{tgt:null,itc:null}};
  ["tgt","itc"].forEach(function(k){
    var h=c.heads[k]; if(!h) return;
    var q=p&&p.heads[k], E,N,hd;
    if(q){ E=q.E+(h.E-q.E)*a; N=q.N+(h.N-q.N)*a; hd=lerpAng(q.hdg,h.hdg,a); } else { E=h.E; N=h.N; hd=h.hdg; }
    if(extra>0){ var hr=h.hdg*Math.PI/180, v=h.spd||0; E+=v*Math.sin(hr)*extra; N+=v*Math.cos(hr)*extra; }
    out.heads[k]={E:E,N:N,hdg:((hd%360)+360)%360,spd:h.spd||0};
  });
  return out;
}
function tweenFrame(now){
  if(DEAD) return;
  requestAnimationFrame(tweenFrame);
  if(!made.map||!TW.cur) return;
  if(now-TW.last<1000/TW.rate) return; TW.last=now;
  try{ if(!ovDraw(tweenState(tweenNow()))) return; }catch(e){ console.warn("tween skipped",e); return; }   // a synchronous throw (teardown race) is never an uncaught error
  TW.ticks++; TW.avg=OV.avg;
  if(OV.cost.length>=20&&OV.avg>8&&TW.rate>15) TW.rate=15;                  // the overlay update itself costs > 8 ms (a very weak machine): 15 Hz
  var el=document.getElementById("tw"); if(el&&TW.ticks%30===0){ el.textContent="overlay "+TW.rate+" Hz · "+(OV.avg||0).toFixed(2)+" ms"; el.hidden=!SHOW_TW; if(SHOW_TW) fitCaptions(); }   // diagnostics only (SHOW_TW)
}
function eventsCard(ev){                                 // TRACK CHANGES: the last 5 track-id changes, newest on top, current ids bold; rows = [hms, role, old, new, current, rule]
  var rows=(ev||[]).slice(-5).reverse().map(function(e){ var o=e[2]==null?"—":"#"+e[2], n=e[3]==null?"—":"#"+e[3];
    return '<div class="e">'+esc(e[0])+' · '+esc(e[1])+' track '+esc(o)+' → '+(e[4]?'<b>'+esc(n)+'</b>':esc(n))+(e[5]?' <span class="r">· '+esc(e[5])+'</span>':'')+'</div>'; });
  return '<div class="ih-events"><div class="k">Track changes</div>'+(rows.length?rows.join(""):'<div class="e none">no track-id change yet</div>')+'</div>';
}
function renderMore(items,events){                       // ultrawide mode: ONE chip row + the TRACK CHANGES card under the (taller) separation card
  var el=document.getElementById("more"); if(!el||!items) return;
  var G={}; Object.keys(STATUS_WORDS).forEach(function(k){ G[k]=gw(k,STATUS_WORDS[k]); });
  el.innerHTML='<div class="ih-chips">'+items.map(function(it){ var cls=it[3]||"";
    return '<div class="ih-chip '+esc(cls)+'"><div class="k">'+esc(it[0])+'</div><div class="v">'+esc(it[1])+'</div>'+(it[2]?'<div class="s">'+esc(it[2])+'</div>':'')+(G[cls]?'<div class="st">'+G[cls]+'</div>':'')+'</div>'; }).join("")+'</div>'
    +eventsCard(events);
}
"""


ENVELOPE_JS = r"""
// ── one envelope (figs.json body) -> the page.  Shared by the live poller, the static preview and the JS test harness. ──
function applyEnvelope(j){
  if(!j||DEAD) return Promise.resolve();
  if(document.getElementById("st")) setStatus(j.frozen?"pause":"ok", j.frozen?"frozen":"live", j.clock);   // degraded by fitStatus when the column is too narrow for the clock
  setFrozen(j.frozen);
  if(j.sat&&j.sat.imgs){ satImgs=stackTiles(satImgs,fixImgs(j.sat.imgs)); SATK=j.sat.key; SAT_DIRTY=true; }   // a tile for OUR view (also on an "unchanged" reply: the view moved while paused); the previous tiles stay UNDER it
  if(j.unchanged||j.stamp===stamp) return satFlush();                 // nothing new: no react, no repaint (a fresh tile alone -> one images relayout)
  if(j.frozen&&stamp!==null) return satFlush();                        // frozen: keep the last picture (first paint allowed)
  if(j.ui&&(j.ui.font_px!==UI.font_px||j.ui.line_w!==UI.line_w)){ UI=Object.assign({},UI,j.ui); geo=null; applyGeometry(); }   // display hints from the server (preset font / line width)
  if(j.hud) setHud(j.hud.cpa||"");
  var revChanged=takeView(j.view);
  takeHeads(j);
  if(j.more) renderMore(j.more,j.events);
  var figs=j.figs||{}, keys=["map","sep","err","vel"].filter(function(k){ return !!figs[k]; });
  var after=function(){ if(revChanged){ var r=frameRange(); if(r) return applyRange(r.x,r.y,true); } else if(j.heads) followCheck(j.heads); };   // FOLLOW / ENGAGE hysteresis after EVERY tick (2026-09-15: gated on heads-only ticks, it never ran while playing — sep / err / vel react every tick — and the vehicles left a stale 1000 m view as the engagement box grew to 1600 m)
  if(!keys.length){ stamp=j.stamp; placeHeads(); return satFlush().then(after); }   // heads-only tick: the overlay takes the new heads at once (the frame loop tweens); no Plotly unless a tile landed
  if(reacting) return Promise.resolve();                              // a react chain is still running; figures are re-fetched next poll (stamp not advanced)
  if(VIEW.prog>0&&figs.map){ placeHeads(); return satFlush().then(after); }   // our 600 ms range animation is in flight: a map react now would bake a mid-transition range -> next poll
  stamp=j.stamp; reacting=true;
  var seq=Promise.resolve();
  keys.forEach(function(k){ var f=figs[k];
    if(k==="map"){ f.layout.images=(satImgs||[]).slice(); f.layout.shapes=[]; f.layout.annotations=(f.layout.annotations||[]).filter(function(a){ return !isPill(a); }); SAT_DIRTY=false; fitView(f.layout); }   // Plotly draws the tiles + trails ONLY: heads / segments / pills are the overlay's
    fitLayout(k,f.layout); fitLines(f.data,(geo&&geo.lw)||UI.line_w);
    var dat=(k==="map")?svgOnly(f.data):noGL(f.data);              // MAP: always SVG — a WebGL context re-created on every react flashes the map in/out
    seq=seq.then(function(){ var cfg=cfgFor(k); return made[k] ? Plotly.react(k,dat,f.layout,cfg)
                                          : Plotly.newPlot(k,dat,f.layout,cfg).then(function(){ made[k]=true; if(k==="map") bindMap(); }); })
          .then(function(){ have[k]=String(j[k+"_stamp"]||""); }); });
  return seq.catch(function(e){ console.warn("react failed",e); }).then(function(){ reacting=false; ovNow(); }).then(after);   // the react may have moved the plot area (automargin): re-place the overlay
}
var _satHealT=0;
function satHeal(){                                       // 2026-09-15 "when you drag to pan only the track moves, not the background": re-apply the satellite
  var gd=document.getElementById("map"); if(!gd||!gd._fullLayout||!satImgs||!satImgs.length||reacting||fitting>0) return;   // tile positions (data coords -> px) during / after a drag
  var now=performance.now(); if(now-_satHealT<80) return; _satHealT=now;
  try{ Plotly.relayout(gd,{images:(satImgs||[]).slice()}).catch(function(){}); }catch(e){}
}
function bindMap(){ var gd=document.getElementById("map"); ovMake(gd); gd.on("plotly_relayout",onMapRelayout); gd.on("plotly_relayouting",ovNow); gd.on("plotly_relayouting",satHeal); gd.on("plotly_relayout",function(ev){ var ks=Object.keys(ev||{}); if(ks.some(function(k){ return /^[xy]axis\.(range|autorange)/.test(k); })) setTimeout(satHeal,0); });   // USER range edits only — never our own images relayout (that fed back into a 12 Hz loop)   // drag / wheel in progress: the overlay follows the axes synchronously
  gd.addEventListener("dblclick",function(ev){ ev.preventDefault(); resetView(); });   // CFG.doubleClick=false: plotly's autoscale (which would fit the satellite tile) is off — double-click = OUR reset to the default frame
  var p=document.getElementById("pill"); if(p) p.onclick=function(){ resetView(); }; }
function noGL(data){ if(GL) return data; for(var i=0;i<data.length;i++) if(data[i].type==="scattergl") data[i].type="scatter"; return data; }
function svgOnly(data){ for(var i=0;i<data.length;i++) if(data[i].type==="scattergl") data[i].type="scatter"; return data; }   // map: few hundred points, SVG is cheaper than a GL re-init
function fixImgs(ims){ ims=ims||[]; for(var i=0;i<ims.length;i++){ var s=ims[i].source;
  if(typeof s==="string"&&s.indexOf("@img/")===0) ims[i].source=BASE+"/"+s.slice(1); } return ims; }
var SAT_STACK=3;
function tileBox(im){ return {x0:im.x,x1:im.x+im.sizex,y1:im.y,y0:im.y-im.sizey}; }
function covers(a,b){ return a.x0<=b.x0+1&&a.x1>=b.x1-1&&a.y0<=b.y0+1&&a.y1>=b.y1-1; }   // tile a covers tile b (1 m slack)
function stackTiles(old,fresh){                           // newest LAST (drawn on top); older tiles stay underneath unless the newest covers them; at most SAT_STACK
  fresh=fresh||[]; if(!fresh.length) return old||[];
  var nb=tileBox(fresh[fresh.length-1]);
  var keep=(old||[]).filter(function(im){ return !(im.name==="sat"&&covers(nb,tileBox(im))); });
  keep=keep.filter(function(im){ return im.name==="sat"; }).slice(-(SAT_STACK-1));
  return keep.concat(fresh);
}
// debug / e2e handle (the live panel wraps this core in an IIFE): read-mostly access to the state machine from window.IH
try{ window.IH={get VIEW(){return VIEW;},get geo(){return geo;},get PANEL(){return PANEL;},get want(){return _fhWant;},get made(){return made;},get satImgs(){return satImgs;},
  get stamp(){return stamp;},get have(){return have;},get TW(){return TW;},get SATK(){return SATK;},get HEADIMGS(){return HEADIMGS;},get PILLS(){return PILLS;},get UI(){return UI;},
  get reacting(){return reacting;},
  get overlay(){ return {heads:OV.state.heads,pills:OV.state.pills,relayouts:OV.relayouts,frames:OV.frames,avg_ms:OV.avg,geom:OV.geom,tile_relayouts:SATN,rate:TW.rate}; },   // relayouts = Plotly.relayout calls made by the overlay / tween path: none exist, must read 0
  curRange:curRange,resetView:resetView,applyEnvelope:applyEnvelope,frameChain:frameChain,setFrameHeight:setFrameHeight,geometry:geometry,viewportPanel:viewportPanel,mpp:mpp,iconSizeM:iconSizeM,iconPx:iconPx,
  stackTiles:stackTiles,placeHeads:placeHeads,ovDraw:ovDraw,tweenState:tweenState,errRows:errRows}; }catch(e){}
"""


def panel_core_js(L: dict | None = None) -> str:
    """Everything the panel JS needs EXCEPT the poller: shared state, helpers, responsive geometry, view /
    tween state machine and applyEnvelope.  The live panel wraps it with the fetch loop; the static
    preview and the JS test harness feed applyEnvelope() synthetic envelopes."""
    L = L or LAYOUT
    return (f"""
var BASE="", CFG={{scrollZoom:true,displayModeBar:false,displaylogo:false,responsive:false,doubleClick:false}}, GL=false;
function cfgFor(k){{ return k==="map" ? CFG : Object.assign({{}},CFG,{{displayModeBar:false}}); }}   // 2026-09-14: the hover modebar sat over the first card's readout — the card figures (sep / err / vel) show none; the map keeps it   // responsive:false — WE resize (applyGeometry -> Plots.resize); plotly's own window listener fired on a plot being torn down by an iframe remount (uncaught "_plots is undefined")
var stamp=null, have={{map:"",sep:"",err:"",vel:"",meas:""}}, made={{}}, fetching=false, reacting=false, satImgs=null, DEAD=false;
try{{ window.addEventListener("pagehide",function(){{ DEAD=true; }}); }}catch(e){{}}                                                 // the iframe is being replaced (preset / text size): stop touching plotly
var TGT_COLOR={json.dumps(T.TARGET)}, ITC_COLOR={json.dumps(T.INTERCEPTOR)}, CARD_COLOR={json.dumps(T.CARD)}, INK_COLOR={json.dumps(T.INK)}, RULE_COLOR={json.dumps(T.RULE)}, NS="http://www.w3.org/2000/svg";
var HEAD_SRC={{tgt:{json.dumps(icons.icon_uri("tgt", 0.0, T.TARGET, T.SURFACE, True))},itc:{json.dumps(icons.icon_uri("itc", 0.0, T.INTERCEPTOR, T.SURFACE, True))}}};   // ONE animated base SVG per role (heading 0 = north); the overlay rotates it with CSS
var ICONS={json.dumps(T.ICONS)}, STATUS_WORDS={json.dumps({k: v[2] for k, v in T.STATUS.items()})};   // theme status icons (inline stroke SVG) + words
""" + HELPERS_JS + responsive_js(L) + VIEW_JS + ENVELOPE_JS)


def panel_script(sid: str, host: str, port_: int, period_ms: int, L: dict | None = None) -> str:
    """Loader + poller around panel_core_js.  Nothing dynamic besides sid / host / port / cadence and the
    preset's geometry constants."""
    L = L or LAYOUT
    return f"""<script>
(function(){{
  var SID={json.dumps(sid)}, HOST={json.dumps(host)}, PORT={int(port_)}, P={int(period_ms)};
  if(!HOST){{ try{{ HOST=parent.location.hostname; }}catch(e){{ HOST=location.hostname; }} }}
  {panel_core_js(L)}
  BASE="http://"+HOST+":"+PORT; GL=hasWebGL(); TW.P=P;
  function status(cls,txt){{ setStatus(cls||"na",txt,""); }}
  function tick(){{
    if(fetching||!window.Plotly) return; fetching=true;
    var q="sid="+encodeURIComponent(SID)+"&since="+(stamp===null?"":stamp)+"&map="+have.map+"&sep="+have.sep+"&err="+have.err+"&meas=999999999999999"
          +"&view="+viewParam()+"&satk="+encodeURIComponent(SATK);
    fetch(BASE+"/figs.json?"+q,{{cache:"no-store"}})
    .then(function(r){{ if(r.status===404){{ status("na","waiting for data"); return null; }} return r.json(); }})
    .then(function(j){{ return applyEnvelope(j); }})
    .catch(function(e){{ status("fail",BASE+" unreachable"); }})
    .then(function(){{ fetching=false; }});
  }}
  applyGeometry(); bindParentKeys();                      // size the empty cards to the real iframe width / parent viewport before the first fetch
  try{{ window.parent.addEventListener("resize",function(){{ clearTimeout(_rz); _rz=setTimeout(applyGeometry,120); }}); }}catch(e){{}}
  var s=document.createElement("script"); s.src=BASE+"/plotly.min.js";
  s.onload=function(){{ tick(); setInterval(tick,P); requestAnimationFrame(tweenFrame); }};
  s.onerror=function(){{ status("fail","cannot load "+BASE+"/plotly.min.js — open port "+PORT); }};
  document.head.appendChild(s);
}})();
</script>"""


def panel_html(sid: str, host: str, port_: int, period_ms: int, L: dict | None = None) -> str:
    """The one-screen iframe HTML: constant per browser session for a given preset (sid / host / port /
    cadence / preset geometry baked in).  Changing the preset changes the HTML -> Streamlit remounts the
    iframe once, which is the intended effect of that control."""
    return panel_css(L) + panel_body() + panel_script(sid, host, port_, period_ms, L)


# ── measurement-space iframe (its own card below the one-screen panel; polls keys=meas) ──
def meas_css(L: dict | None = None) -> str:
    L = L or LAYOUT
    return f"""<style>
html{{font-size:calc(16px * {float(L.get('text_scale', 1.0)):g});}}
html,body{{margin:0;padding:0;background:{T.SURFACE};color:{T.INK};overflow:hidden;font-family:{T.SANS};}}
.h{{display:flex;align-items:baseline;gap:8px;height:{L['header']}px;box-sizing:border-box;padding:0 10px 4px;background:{T.CARD};border-bottom:1px solid {T.RULE};
    font:500 .85rem/1.3 {T.MONO};letter-spacing:.08em;text-transform:uppercase;color:{T.INK2};overflow:hidden;white-space:nowrap;}}
.h::before{{content:"";display:inline-block;width:6px;height:6px;background:{T.RED};flex:none;align-self:center;}}
.h .r{{margin-left:auto;min-width:0;flex:0 1 auto;text-transform:none;letter-spacing:0;color:{T.INK3};overflow:hidden;white-space:nowrap;}}
.h .r .cap[hidden]{{display:none;}}
#meas,#meas_itc{{width:100%;height:{L['meas']}px;background:{T.CARD};}}
</style>"""


def meas_html(sid: str, host: str, port_: int, period_ms: int, L: dict | None = None, key: str = "meas") -> str:
    """The measurement-space card iframe for ONE figure key: "meas" (target) or "meas_itc" (interceptor) — same markup, its own
    header words, polls figs.json?keys=<key> and draws into <div id=<key>>."""
    L = L or LAYOUT
    assert key in MEAS_CARD_KEYS, key
    H = HEADERS[key]
    body = (f'<div class="h" title="{T.esc(H[0])} — {T.esc(H[1])}"><span>{T.esc(H[0])}</span>'
            f'<span class="r"><span class="cap">{T.esc(H[1])}</span></span></div><div id="{key}"></div>')
    js = f"""<script>
(function(){{
  var SID={json.dumps(sid)}, HOST={json.dumps(host)}, PORT={int(port_)}, P={int(max(period_ms, 1000))}, HF={int(L['meas'])}, F={int(L['font_px'])}, K={json.dumps(key)};
  if(!HOST){{ try{{ HOST=parent.location.hostname; }}catch(e){{ HOST=location.hostname; }} }}
  var BASE="http://"+HOST+":"+PORT, CFG={{scrollZoom:true,displayModeBar:false,displaylogo:false,responsive:false}};   // resized by our own listener below; 2026-09-15: displayModeBar false like the other card figures — the hover buttons' row sat over this figure's key
  var have="", stamp=null, made=false, fetching=false;
  function fontFor(W){{ return W>=2400?Math.max(F,15):Math.max(F,13); }}
  function fit(layout){{ var f=fontFor(window.innerWidth||1600); layout.height=HF; if(layout.font) layout.font.size=f; if(layout.legend&&layout.legend.font) layout.legend.font.size=Math.max(f,12);
    Object.keys(layout).forEach(function(a){{ if(/^[xy]axis\\d*$/.test(a)){{ var ax=layout[a]; if(ax.tickfont) ax.tickfont.size=f; if(ax.title&&ax.title.font) ax.title.font.size=f; }} }}); return layout; }}
  function tick(){{
    if(fetching||!window.Plotly) return; fetching=true;
    fetch(BASE+"/figs.json?sid="+encodeURIComponent(SID)+"&keys="+K+"&"+K+"="+have+"&since="+(stamp===null?"":stamp),{{cache:"no-store"}})
    .then(function(r){{ return r.status===404?null:r.json(); }})
    .then(function(j){{ if(!j||j.unchanged||!j.figs||!j.figs[K]) return; stamp=j.stamp; var f=j.figs[K]; fit(f.layout);
      return (made?Plotly.react(K,f.data,f.layout,CFG):Plotly.newPlot(K,f.data,f.layout,CFG).then(function(){{ made=true; }})).then(function(){{ have=String(j[K+"_stamp"]||""); }}); }})
    .catch(function(e){{ console.warn(K,e); }}).then(function(){{ fetching=false; }});
  }}
  function fitCap(){{                                                    // the caption is hidden WHOLE when the header row is too narrow for it (the panel does the same; the full text stays in the header's title)
    var r=document.querySelector(".h .r"), c=document.querySelector(".h .cap"); if(!r||!c) return;
    c.hidden=false; if(r.scrollWidth>r.clientWidth+1) c.hidden=true;
  }}
  fitCap();
  var s=document.createElement("script"); s.src=BASE+"/plotly.min.js"; s.onload=function(){{ tick(); setInterval(tick,Math.max(P,2000)); }}; document.head.appendChild(s);
  window.addEventListener("resize",function(){{ fitCap(); if(made) Plotly.Plots.resize(K); }});
}})();
</script>"""
    return meas_css(L) + body + js


__all__ = ["adopt", "DEFAULT_PORT", "LAYOUT", "PRESETS", "DEFAULT_PRESET", "FONT_PX", "LINE_W", "layout", "err_hdr_px", "err_min_px", "one_sep_px", "one_err_px", "one_vel_px", "one_row", "map_top_nolegend_px", "vel_strip_px", "map_margin_px", "HEADERS", "FIG_KEYS", "PANEL_KEYS", "MEAS_CARD_KEYS", "STORE",
           "IMAGES", "SAT", "start", "stop", "is_up", "port", "last_error", "push", "set_frozen", "has", "figs_of", "fig_dict", "fig_json",
           "strip_map_range", "response_body", "parse_view", "sat_key", "browser_host", "host_from_header", "panel_css", "panel_body",
           "panel_script", "panel_html", "panel_core_js", "responsive_js", "VIEW_JS", "ENVELOPE_JS", "HELPERS_JS", "meas_html", "meas_css",
           "BREAKPOINT_PX", "ULTRAWIDE_PX", "MIN_CARD_PX", "COMPACT_ERR_PX", "SEP_FRAC", "SEP_FRAC3", "ERR_MIN_PX", "SEP_MIN_PX", "MEAS_PX", "COL_GAP_PX", "ONE_RIGHT_MIN_PX"]
