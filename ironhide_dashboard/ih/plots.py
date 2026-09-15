"""Plotly figures for the LIVE page: top-down map, separation card, 4-panel
track-quality (error) figure — plus the vehicle "heads" the panel draws itself.

Screen presets / responsive: every figure takes P["font_px"] (Laptop 12 / Desktop 11 / Large
13) and P["line_w"] (2.5, 3 on the Large preset); heights come from ih.liveserver.layout(preset)
and the panel JS re-fits them to the real iframe width.  The map's axis box is either the FIXED
flight footprint (P["frame"], ih.data.flight_frame) or FOLLOW (map_box about the heads); the
panel applies that box only on the first paint / explicit frame changes and owns the view
afterwards (zoom / pan survive every tick).  Trails are decimated to <= 2 pts/s.

Every figure goes through base_layout(): explicit CARD backgrounds (the figures ARE
the cards of the panel), solid hairline grid, mono 11 px secondary-ink ticks/titles,
tight per-figure margins, a legend only when the caller counts >= 2 series, and a
CONSTANT layout.uirevision per figure so Plotly.react keeps the user's zoom / pan /
legend toggles across live updates.  CPA (closest so far) is marked on all three
figures in one visual language: gold ★ with a 2 px card ring + ink label, and gold
vertical hairlines (layout shapes, uirevision-safe) on the time-series figures.

Map (truth vs track only): truth trails + the target's radar track as scattergl
lines (the track a 2.5 px dashed line, no per-state markers), a thin 5 s velocity
leader per vehicle, ★ CPA (only once the engine's CPA gate validates it), optional
blind rings, satellite underlay (externalised by ih.liveserver, included in the layout
only when the map box changes — P["sat_include"]).  The heading-rotated vehicle icons
are Plotly LAYOUT IMAGES IN DATA COORDINATES (heads_images(): xref/yref x/y, x/y = the
head's E/N, sizex/sizey ≈ 6 % of the view width, min 40 m) — the same axis transform
that draws the trails positions the icons, so they cannot detach.  On the panel path
they travel in the figs.json envelope every tick and the panel JS applies them with
Plotly.relayout(div, {images}) (an "arraydraw" edit: no trace re-render, uirevision
untouched); the st.plotly_chart fallback puts the same dicts into the figure
(P["icons_in_fig"]).  Heading is quantised to 5° and the data URI cached per bucket
(ih.icons.icon_uri) so the image source — and with it the SMIL clocks — only changes
when the heading really moves a step.

Track-quality panel (spa containment style): four visually boxed cards (a card-coloured
rect with a 1 px rule border per panel on the page-surface paper, 14 px gaps), each
with a bold mono title top-left ("AZIMUTH ERROR" …) and a takeaway readout top-right
("now +0.8° ± 1.1° (1σ)" in 13 px primary ink over "1σ 79% · 3σ 100% · n 118").  Per
target track: graded samples (CONFIRMED + UPDATED, spa's default gate) as the solid
2.5 px line with ONLY the ±1σ band (.30 fill, 1 px .8 edge — the 3σ band swamped it; its
containment rate stays in the readout), broken across coasting; the OTHER published states (tentative / coasting / extrapolated) as a thin
dotted line in the same hue at .5 alpha through the holes — no bands, never in the
stats.  Muted zero hairline, robust ±nice(max(floor, 2·median σ, 1.25·P95(|err| + 1σ))) y-range
clamped per quantity (ERR_CLAMP) so bands always read and no spike blows the scale, explicit
ticks (±half/2, 0), short rotated y titles, the CPA
hairline + time label (only when valid), raw radar obs as small ✕ in secondary ink at
.55 (obs − target truth).  Text never wears a series colour.

VERTICAL ELEMENTS on the time-series cards (2026-09-11 round 3, "what are these random vertical
lines"): nothing vertical without a visible label or an obvious meaning.  Kept: the gold CPA
hairline (labelled "CPA 59 m · 07:22:31" at its top in the FIRST card), the secondary-ink
handover tick at the top edge (labelled "→ #177 · 07:21:57" in the FIRST card; a label within
LABEL_RIGHT_FRAC of the right edge anchors LEFT of its tick; labels closer than LABEL_SEP_FRAC
of the window stack downwards), the coast strip (a horizontal bar, hover = "coasting 4.2 s")
and the faint SOLID x grid (T.GRID, every 30 s).  Removed: the red "now" hairline — the right
edge of the time axis IS now (X_PAD_FRAC right padding keeps the newest sample off the frame).
AXIS RANGES are robust: error cards ±nice(max(floor, 1.25 · P95(|err| + 1σ))) CLAMPED per
quantity (ERR_CLAMP: az / el ±5°, alt ±150 m, 3D 0..300 m — a spike is clipped, the readout
still shows the number); velocity cards = the truth + track span padded 15 %, floored at
±VEL_FLOOR, clamped to ±VEL_CLAMP (an unphysical −80 m/s state pins at the edge: visibly
wrong).  stable_range() holds a range against jitter and the clamp is re-applied after it.
"""
from __future__ import annotations

import re

import numpy as np
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from . import data as D
from . import icons
from . import theme as T

TR, TK = D.TR, D.TK
LEADER_S = 5.0
LINE_W = 2.5                                                  # every series line
MARGIN = dict(l=48, r=12, t=28, b=36)                       # default; each figure passes a tighter one
UIREV = {"map": "live-map", "sep": "live-sep", "err": "live-err", "vel": "live-vel", "meas": "live-meas"}  # constants — never derived from time/data
FONT_PX = 14                                                  # default figure font (ticks / titles / legend); the screen preset x text scale overrides (ui.font_px)
# ── text metrics (Firefox / JetBrains Mono, measured in the panel: 18 px -> 11.25 px per glyph, 24 px line box) ──
GLYPH_W = 0.625                                               # mono glyph width / font px
LINE_H = 1.35                                                 # text line box height / font px (= ERR_BOX_LH)
LEGEND_ROW_PAD = 14                                           # a plotly legend ROW is LINE_H x font + this (measured: 12 px -> 29, 14 px -> 32, 18 px -> 37)
LEGEND_EXPAND_PAD = 12                                        # ... and margin.autoexpand reserves this much MORE around the legend block: a figure that keeps autoexpand ON (err / vel need it for the y-axis automargin) must reserve row + this, or plotly grows the top margin under it and every px-exact strip / card rect slides (measured 2026-09-14: 18 px font, the first honoured margin.t was row + 10)
JS_FONT_MIN = 15                                              # the panel JS may RAISE a figure's font (liveserver fontFor: >= 13 x text scale, >= 15 x scale on an ultrawide panel)


def legend_row_px(font_px: float = FONT_PX) -> int:
    """Height of ONE horizontal legend row at this font — the room a figure must reserve in its top margin.
    Under-reserving is what let plotly's margin.autoexpand grow the top margin: the px-exact header strips
    (and the panel's errRows / cardGrid, which re-derive the row domains from layout.margin) then disagreed
    with the drawn plot area and the card rects slid out from under their strip annotations."""
    return int(round(LINE_H * float(font_px))) + LEGEND_ROW_PAD


def map_margin(font_px: float = FONT_PX, text_scale: float = 1.0) -> dict:
    """The map's FIXED margins (autoexpand OFF — see MARGINS): enough for the rotated axis titles, the widest
    y tick label (MAP_TICK_GLYPHS glyphs, e.g. "−2500") and, at the top, the hover modebar's row PLUS one
    legend row.  Derived from the font only (never from the data / the legend's content), so the plot area —
    and with it the equal-aspect ranges — never changes while the map is live."""
    f = max(float(font_px), round(JS_FONT_MIN * float(text_scale or 1.0)))    # the panel may raise the font above the built one
    tick_w = GLYPH_W * f * MAP_TICK_GLYPHS + 2.0
    title_h = LINE_H * f + MAP_TITLE_STANDOFF
    return dict(l=int(round(title_h + tick_w + MAP_TICK_GAP)), r=12,
                t=int(MAP_MODEBAR_PX + legend_row_px(f) + 2), b=int(round(MAP_TICK_GAP + LINE_H * f + title_h + 2.0)),
                autoexpand=False)


def card_bottom_px(base_b: float, font_px: float) -> int:
    """Bottom margin of a card figure: the x tick labels' line box (x automargin is OFF — the row geometry is
    exact pixels) plus the tick length.  14 px -> 28, 18 px -> 32 (at 18 the old 6 + 1.3 x font left the labels
    4 px outside the figure: clip_audit "tick-outside-figure")."""
    return int(max(float(base_b), round(LINE_H * float(font_px)) + MAP_TICK_GAP))


MAP_TICK_GLYPHS = 5                                           # widest y tick label budgeted in the left margin ("−2500"); the slack also absorbs the panel's font bump
MAP_TICK_GAP = 8.0                                            # ticklen (4) + the gap between a tick and its label
MAP_TITLE_STANDOFF = 6.0                                      # axis title_standoff
MAP_MODEBAR_PX = 26                                           # plotly's hover modebar row at the figure's top-right (24 px + a 2 px offset): the legend row sits BELOW it
MAP_MODEBAR_DROP = ("select2d", "lasso2d")                    # modebar buttons a map has no use for — dropped: a shorter modebar leaves more room beside the legend row
CARD_TICK_GLYPHS = 5                                          # widest y tick label budgeted in a CARD's left margin ("−12.5" velocity / "10000" separation; the error cards' "−150" / "−2.5" are 4)
X_TICK_GLYPHS = 8                                             # "07:21:40": the right margin holds HALF of the newest label (fixed dtick: the labels SLIDE to the edge instead of re-spacing)
CARD_T_PX, CARD_B_PX = 8, 28                                  # a card figure's base top / bottom margin (the drawn t = strip + pad + legend row, b = card_bottom_px at the font)


def card_font(font_px: float = FONT_PX, text_scale: float = 1.0) -> float:
    """The font the panel may RAISE a card figure to (liveserver fontFor: >= JS_FONT_MIN x text scale on an ultrawide panel, >= 13 x
    scale elsewhere) — the margins are budgeted for it, so a font bump never moves the plot area either."""
    return max(float(font_px), round(JS_FONT_MIN * float(text_scale or 1.0)))


def card_margin(font_px: float = FONT_PX, text_scale: float = 1.0, t: int = CARD_T_PX, autoexpand: bool = False) -> dict:
    """The FIXED margins of a time-series card figure (sep / err / vel): automargin + autoexpand OFF, the left margin = the rotated axis
    title + the widest y tick label (CARD_TICK_GLYPHS) + the tick gap, the right margin = half an x tick label, the bottom = one x tick
    label row — all from the font alone (never from the data / the labels / the legend), so the plot area NEVER moves while the card is
    live (2026-09-15 user: "make these plots jump a little less" — with l = 52 + automargin the plot area slid every time a label changed
    width, and the labels were cut at the figure edge: "200" read "00", "−2.5" lost its sign)."""
    f = card_font(font_px, text_scale)
    tick_w = GLYPH_W * f * CARD_TICK_GLYPHS + 2.0
    title_h = LINE_H * f + MAP_TITLE_STANDOFF
    m = dict(l=int(round(title_h + tick_w + MAP_TICK_GAP)), r=int(round(0.5 * X_TICK_GLYPHS * GLYPH_W * f + 2.0)), t=int(t), b=card_bottom_px(CARD_B_PX, f))
    if not autoexpand:
        m["autoexpand"] = False
    return m


MARGINS = {"map": map_margin(FONT_PX), "sep": card_margin(FONT_PX, 1.0, t=22, autoexpand=True), "err": card_margin(FONT_PX, 1.0), "vel": card_margin(FONT_PX, 1.0)}   # ALL FIXED (automargin + autoexpand off: a tick-label- or legend-driven margin change re-constrained the map's equal-aspect ranges / slid the cards' plot areas = a visible "snap"); the card values are the text-scale-1 ones — the figures rebuild them with card_margin(font, scale)
TRAIL_MAX_HZ = 2.0                                            # map trails are decimated to <= 2 points / s (cheaper reacts)
ITC_TRACK_ALPHA = 0.5                                         # interceptor radar track on the map: faded, secondary to the target track
ITC_TRACK_W = 2.0
LIVE_SEG = ("tgt_live", "itc_live")                          # names of the live-segment layout SHAPES the panel JS draws (trail tail -> tweened head)
COMPACT_ERR_PX = 480                                          # error panel shorter than this: 12 px readouts
READOUT_COMPACT_PX = 12
EMPTY_READOUT = "no samples yet"                       # every card always carries its title + this muted readout when there is nothing to grade
NO_TGT_READOUT = "no target truth feed"                # ... and THIS one when there is truth but not for the target: nothing to grade AGAINST (never "no samples yet")
NO_TGT_SEP_READOUT = "no target truth"                 # the separation card (truth-to-truth): it needs BOTH heads
NO_TRUTH_SEP_READOUT = "no truth feed"


def no_tgt_truth(A: dict) -> bool:
    """True when the engine has TRUTH but none for the TARGET (MRU91 2026-09-15: interceptor-only MAVLink
    feed) — the cards that grade a track against target truth are empty for a REASON, and say so.  Legacy A
    dicts without the key fall back to False (the no-truth-at-all state has its own wording)."""
    return bool(A.get("no_tgt_truth", False))


def ungraded_view(A: dict) -> bool:
    """True when nothing on the map can be correlated: no target truth (either no feed at all or the
    interceptor-only case) -> the radar's tracks are drawn grey, dashed and ungraded."""
    if "has_tgt_truth" in A:
        return not bool(A["has_tgt_truth"])
    return not bool(A.get("has_truth", True))
TRACK_COLORS = (T.TARGET, T.TARGET_DARK)                     # primary target track, second target-side track
BAND_ALPHA = {1: 0.30}                                        # ONLY the ±1σ band is drawn (3σ stays in the readout text: containment rates)
BAND_EDGE_ALPHA = 0.8
BANDS_DRAWN = (1,)
PANEL_GAP_PX = 10.0                                           # vertical gap between the cards (each card = header strip + plot area); the card frame (CARD_RULE) does the sectioning
STRIP_ROW_GAP = 2.0                                           # clear pixels between the two header-strip rows (2026-09-14: the boxes overlapped by 2 px in Firefox)
ERR_BOX_LH, ERR_BOX_PAD = 1.35, 4.0                           # a plotly annotation box ≈ ERR_BOX_LH x font + ERR_BOX_PAD px tall (measured in Firefox: 14 px -> 22, 12 px -> 21)
ERR_HDR_PAD = 14.0                                            # (legacy name; the strip geometry is err_strip_px())
X_PAD_FRAC = 0.02                                             # time axes: right padding so the newest sample (t = now) is never glued to the frame edge
LEGEND_PX = legend_row_px(FONT_PX) + LEGEND_EXPAND_PAD        # room above the first header strip for the (>= 2 tracks) legend AT THE DEFAULT FONT (43 px); every card figure reserves legend_row_px(its font) + LEGEND_EXPAND_PAD
CARD_PAD_PX = 3.0                                             # card rect reaches this far above / below its plot area
CARD_PAD_X = 0.004                                            # ... and this paper-fraction left / right (≈ 3 px)
ERR_KEYS = ("az", "el", "pos3d", "alt")                       # range / range-rate live in the measurement-space quad
ERR_AXIS = {"az": "az °", "el": "el °", "pos3d": "3D m", "alt": "alt m"}
ERR_TITLE = {"az": "AZIMUTH ERROR", "el": "ELEVATION ERROR", "pos3d": "3D POSITION ERROR", "alt": "ALTITUDE ERROR"}
ERR_UNIT = {"az": "°", "el": "°", "pos3d": " m", "alt": " m"}
ERR_NUM = {"az": "{:+.1f}", "el": "{:+.1f}", "pos3d": "{:.0f}", "alt": "{:+.0f}"}          # readout: current error
ERR_SIG_NUM = {"az": "{:.1f}", "el": "{:.1f}", "pos3d": "{:.0f}", "alt": "{:.0f}"}          # readout: current 1σ
ERR_FLOOR = {"az": 0.5, "el": 0.5, "pos3d": 10.0, "alt": 10.0}    # smallest ± half-range per panel
ERR_HOVER = {"az": "%{y:.2f}°", "el": "%{y:.2f}°", "pos3d": "%{y:.0f} m", "alt": "%{y:.0f} m"}
ERR_SIG_FMT = {"az": "%{customdata:.2f}°", "el": "%{customdata:.2f}°", "pos3d": "%{customdata:.0f} m", "alt": "%{customdata:.0f} m"}
ONE_SIDED = {"pos3d"}                                         # |error| >= 0: band 0 .. +σ (1σ radius of the covariance ellipsoid), axis floor at 0
OBS_KEYS = ("az", "el", "alt")                                # raw obs ✕ overlay only where an obs has the quantity (no 3D position from one sensor)
CONTAIN_LABEL = {"az": "az", "el": "el", "pos3d": "3D pos · 1σ radius", "alt": "alt"}
ERR_COLORS_META = None
STAR = dict(symbol="star", size=14, color=T.GOLD, line=dict(width=2, color=T.CARD))
TAG_BG = "rgba(26,29,34,.8)"                                  # card at .8 behind in-panel ink tags
GAP_FACTOR = 3.0                                              # a > 3x-median update gap also breaks the error line
UNGRADED_W, UNGRADED_ALPHA = 1.0, 0.5                         # (legacy) dotted tentative / coasting line — replaced by the lighter bridge + coast strip
BRIDGE_S = 3.0                                                # a hole in the published states up to this long is bridged (lighter segment, σ interpolated); longer = dropout break
LIGHT_ALPHA = 0.6                                             # the bridged (coasting / tentative) segments of the error line: same hue, this alpha
STRIP_W, STRIP_FRAC = 4, 0.035                                # coast indicator strip: 4 px bar this fraction of the axis span above the card floor
READOUT_PX, READOUT_SUB_PX, TITLE_PX = 18, 12, 16             # takeaway readout / its second line / card title (x the text scale, see px()) — 2026-09-14: 'text is massive even at Normal' -> 14 / 11 / 14 -> err_strip_px == (46, 21) (ih.liveserver.ERR_HDR_PX = 46)
LABEL_PX_DELTA = 3                                            # handover list in the header strip = TITLE_PX - 3 = 11 px (= READOUT_SUB_PX, x scale)
LABEL_RIGHT_FRAC = 0.85                                       # a label whose time is past this fraction of the window anchors LEFT of its tick
STRIP_LABELS_MAX = 3                                          # the header-strip tick list keeps the newest three handovers
LABEL_SEP_FRAC = 0.2                                          # two top labels closer than this fraction of the window stack (the later one one line lower)
ERR_CLAMP = {"az": 5.0, "el": 5.0, "pos3d": 300.0, "alt": 150.0}   # hard axis limits per error card (± for two-sided, 0..max for pos3d): "don't need ±20 angle error"
ERR_P, ERR_P_SCALE = 95.0, 1.25                               # robust half-range: 1.25 x the 95th percentile of |err| + 1σ over the window (one spike never blows the range)
PILL_PX = 13                                                  # track-id pills (map + measurement quad): 13 px mono on a CARD pill with a RULE border (x scale)
TICK_PX = 11                                                  # handover tick labels ("→ #177") at the top edge of the time-series cards (x scale)


def px(base: float, P: dict | None = None) -> int:
    """A text size in px: ``base`` x the sidebar "Text size" scale (P["text_scale"], default 1)."""
    try:
        sc = float((P or {}).get("text_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        sc = 1.0
    return int(round(float(base) * sc))
ENGAGE_PAD, ENGAGE_MIN_HALF_M, ENGAGE_PASS_S, ENGAGE_LIVE_S = 0.25, 500.0, 20.0, 120.0   # default map frame = ENGAGEMENT BOX (see engagement_box)
NOW_MAX_AGE_S = 3.0                                           # "now" readout: newest graded sample no older than this, else "last … s ago"
ICON_FRAC, ICON_MIN_M = 0.06, 40.0                            # map icon: 6 % of the view width, never under 40 m


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha:g})"


NICE = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def nice_ceil(v: float) -> float:
    """Smallest {1,1.5,2,2.5,3,4,5,6,8} × 10^k >= v (v > 0) — the y half-range of an error panel."""
    v = float(v)
    if not np.isfinite(v) or v <= 0:
        return 1.0
    k = 10.0 ** np.floor(np.log10(v))
    for m in NICE:
        if m * k >= v * (1 - 1e-9):
            return float(m * k)
    return float(10.0 * k)


def err_ticks(half: float) -> list[float]:
    """Three explicit y ticks for a ±half error panel: ±half/2 and 0 — plotly's nticks auto-picker
    collapses to a lone "0" on e.g. ±400 (dtick 500), and labels AT the panel edges collide across
    the 10 px gap between panels; mid + zero always reads (half is a "nice" number)."""
    return [-half / 2.0, 0.0, half / 2.0]


TICK_SEP_FRAC = 1.0                                           # two y tick labels are clear of each other at this x font px apart (the INK of a mono digit measures ~0.75 x font; the 1.35 line box overstates it)
PANEL_FIT_SLACK = 0.85                                        # the panel re-fits every figure to the REAL iframe, up to ~15 % shorter than the height the server built for (2026-09-14: err built 541 -> drawn 462): the tick budget assumes the short end


X_DTICK_LADDER_S = (5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0)


def x_dtick_ms(span_s: float, want: int = 4) -> int:
    """A FIXED time-axis tick step (ms) for a window ``span_s`` long: the smallest ladder step giving <= ``want`` labels.  With an
    explicit dtick the labels slide with the window instead of plotly re-picking (and re-spacing) the set every second."""
    span = max(float(span_s), 1.0)
    for st in X_DTICK_LADDER_S:
        if span / st <= want:
            return int(st * 1000)
    return int(X_DTICK_LADDER_S[-1] * 1000)


def nice_ticks(lo: float, hi: float, want: int = 3) -> list[float]:
    """'Nice' ticks inside [lo, hi] at the step nice_ceil(span / want) — a pure function of the HELD range, so the tick set can only
    change when the range does (never flickers between "2.5 / 0 / −2.5" and "5 / 0 / −5" on a tiny range change)."""
    lo, hi = float(lo), float(hi)
    span = max(hi - lo, 1e-9)
    step = nice_ceil(span / float(want))
    t0 = np.ceil(lo / step - 1e-9) * step
    vals = [float(np.round(v, 10)) for v in np.arange(t0, hi + step * 0.01, step)]
    vals = [v + 0.0 for v in vals if lo - 1e-9 <= v <= hi + 1e-9]
    return vals if len(vals) >= 2 else [lo, hi]


def quant_range(lo: float, hi: float, want: int = 4) -> tuple[float, float]:
    """[lo, hi] widened outwards to the nice tick step of its own span: equal data gives equal ranges and a jittering raw span
    lands on the same grid most of the time (the velocity cards' centre ± half is otherwise never twice the same number)."""
    lo, hi = float(lo), float(hi)
    span = max(hi - lo, 1e-9)
    step = nice_ceil(span / float(want))
    return float(np.floor(lo / step + 1e-9) * step), float(np.ceil(hi / step - 1e-9) * step)


def thin_ticks(vals: list[float], lo: float, hi: float, plot_px: float, font_px: float) -> list[float]:
    """``vals`` thinned (every other one, the ends kept) until neighbouring labels are TICK_SEP_FRAC x font apart in a
    ``plot_px`` tall card — a 32 px card at the X-Large text scale cannot carry three labels, and plotly does NOT drop
    y tick labels that collide (2026-09-14, laptop X-Large: "2.5 / 0 / −2.5" printed on top of each other)."""
    span = float(hi) - float(lo)
    vals = [float(v) for v in vals]
    if span <= 0 or plot_px <= 0 or len(vals) < 2:
        return vals
    need = TICK_SEP_FRAC * float(font_px)
    while len(vals) > 2 and min(abs(b - a) for a, b in zip(vals, vals[1:])) / span * float(plot_px) < need:
        vals = vals[::2] if len(vals) % 2 else vals[::2] + [vals[-1]]
    if len(vals) == 2 and abs(vals[1] - vals[0]) / span * float(plot_px) < need:
        vals = [vals[-1]]                                      # room for one label only: keep the top one (the zero line still reads)
    return vals


def base_layout(fig: go.Figure, *, height: int, uirevision: str, legend: bool, time_x: bool = False,
                unified: bool = False, margin: dict | None = None, font_px: int = FONT_PX, fixed: bool = False) -> go.Figure:
    """Shared template. legend=True only when the caller counts >= 2 series.  ``font_px`` (from the
    screen preset: Laptop 12 / Desktop 11 / Large 13) sets EVERY explicit font size — global, ticks,
    axis titles, legend, hover — so the panel JS can bump them all with one relayout on small screens."""
    f = int(font_px)
    leg = dict(orientation="h", x=0.0, y=1.0, xanchor="left", yanchor="bottom", bgcolor="rgba(0,0,0,0)", borderwidth=0,
               font=dict(family=T.MONO, size=f, color=T.INK2), itemsizing="constant", itemwidth=30, tracegroupgap=0)
    # (2026-09-14: the map's key used to be legend_inside — a translucent card INSIDE the plot, over the imagery and the trails,
    #  wrap-dependent in height and running into the hover modebar.  Every figure now keeps its key in its own top margin.)
    fig.update_layout(
        template="none",
        paper_bgcolor=T.CARD, plot_bgcolor=T.CARD,
        font=dict(family=T.MONO, size=f, color=T.INK2),
        margin=dict(margin or MARGIN), height=int(height),
        showlegend=bool(legend), legend=leg,
        hovermode="x unified" if unified else "closest",
        hoverlabel=dict(bgcolor=T.CARD2, bordercolor=T.RULE, font=dict(family=T.MONO, size=f, color=T.INK), align="left"),
        colorway=[T.TARGET, T.INTERCEPTOR, T.GOLD, T.GREY_TRACK],
        uirevision=uirevision,
    )
    axis = dict(gridcolor=T.GRID, gridwidth=1, griddash="solid", showgrid=True, zeroline=True, zerolinecolor=T.ZERO, zerolinewidth=1,
                showline=False, linecolor=T.RULE, ticks="outside", ticklen=4, tickcolor=T.RULE,
                tickfont=dict(family=T.MONO, size=f, color=T.INK2), title_font=dict(family=T.MONO, size=f, color=T.INK2),
                title_standoff=6, automargin=True, uirevision=uirevision)
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    if time_x:
        fig.update_xaxes(tickformat="%H:%M:%S", hoverformat="%H:%M:%S", showspikes=False)
    if fixed:   # a CARD: the margins are card_margin's (glyph-derived) — no axis may push them (see card_margin)
        fig.update_xaxes(automargin=False)
        fig.update_yaxes(automargin=False)
    return fig


def _quant(v: float, q: float) -> float:
    return float(np.round(v / q) * q)


def map_box(A: dict, half: float) -> tuple[float, float, float]:
    pts = [p for p in (A.get("tgt_now"), A.get("itc_now")) if p is not None]
    if not pts:
        tr = A.get("tgt_trail")
        if tr is not None and len(tr):
            pts = [tr[-1]]
    if not pts:
        return 0.0, 0.0, max(half, 3000.0)
    c = np.mean([p[1:3] for p in pts], axis=0)
    return _quant(c[0], 250.0), _quant(c[1], 250.0), float(half)


def frame_box(frame: tuple[float, float, float, float] | None, A: dict, half: float) -> tuple[float, float, float, float]:
    """The map's axis box (x0, x1, y0, y1).  FIXED frame given -> exactly that (the flight footprint,
    square, quantised); else FOLLOW -> the quantised centre of the truth heads ± half (map_box)."""
    if frame is not None:
        x0, x1, y0, y1 = (float(v) for v in frame)
        return x0, x1, y0, y1
    cx, cy, h = map_box(A, float(half))
    return cx - h, cx + h, cy - h, cy + h


def downsample(tr: np.ndarray, max_hz: float = TRAIL_MAX_HZ) -> np.ndarray:
    """Decimate a time-sorted (t, ...) array to at most ``max_hz`` samples / s, always keeping the
    first and the LAST row (the trail must still end at the head)."""
    if tr is None or len(tr) < 3 or max_hz <= 0:
        return tr
    dt_min = 1.0 / float(max_hz)
    t = np.asarray(tr[:, 0], float)
    keep = np.zeros(len(t), bool)
    last = -np.inf
    for i, v in enumerate(t):
        if v - last >= dt_min - 1e-9:
            keep[i] = True
            last = v
    keep[-1] = True
    return tr[keep]


@st.cache_data(show_spinner=False)
def blind_rings() -> list[tuple[str, float]]:
    try:
        from blindzone_map import DEFAULT_MODES, blind_radius_m, load_modes

        lib = load_modes()
        # dedupe coincident radii (e.g. adv_medium == adv_long at 40 µs): one ring, joined label
        by_r: dict[int, list[str]] = {}
        for m in DEFAULT_MODES:
            if m in lib:
                by_r.setdefault(int(round(blind_radius_m(lib[m]))), []).append(m)
        return sorted(((" / ".join(ms), float(r)) for r, ms in by_r.items()), key=lambda x: x[1])
    except Exception:
        return []


def _circle(r: float, n: int = 121):
    th = np.linspace(0, 2 * np.pi, n)
    return r * np.sin(th), r * np.cos(th)


def _hover_t(t: np.ndarray) -> list[str]:
    return [D.pdt_hms(x) for x in t]


ALT_HOVER = "<br>alt %{customdata[0]:.0f} m HAE · %{customdata[1]:.1f} m/s"   # map hover, every vehicle: altitude + ground speed


def _alt_spd(rows: np.ndarray, L: dict, ant_hae: float = 0.0) -> np.ndarray:
    """customdata (n,2) = (altitude HAE m, horizontal speed m/s) for map hovers.  ``L`` is the row layout
    (D.TR for truth, D.TK for tracks).  BOTH layouts carry U as metres above the RADAR (feed.truth_rows projects
    the MAVLink HAE altitude through the EnuFrame — the column name U_m_hae means "from an HAE altitude", not an
    absolute one; block-143 x_state is NED about the same origin), so the antenna's own HAE is added to both and
    the hover reads one absolute scale for truth and tracks alike."""
    a = np.asarray(rows, float)
    if not len(a):
        return np.zeros((0, 2))
    alt = a[:, L["U"]] + float(ant_hae)
    spd = np.hypot(a[:, L["vE"]], a[:, L["vN"]])
    return np.column_stack([alt, spd])


def _dt(t: float):
    return D.to_pdt_dt64(np.array([float(t)]))[0]


def _x_gaps(t: np.ndarray) -> list:
    """epoch seconds (NaN = gap) -> ISO strings with explicit None gaps for Plotly."""
    dt = D.to_pdt_dt64(t)
    return [None if not np.isfinite(v) else str(d) for v, d in zip(np.asarray(t, float), dt)]


def with_breaks(t: np.ndarray, coast_t: np.ndarray, *ys: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
    """Insert a NaN sample wherever a coasting state was published between two graded
    samples (or the gap exceeds GAP_FACTOR x the median update interval) so the error
    line and its bands break there instead of bridging the coast."""
    t = np.asarray(t, float)
    ys = [np.asarray(y, float) for y in ys]
    if len(t) < 2:
        return t, ys
    ct = np.sort(np.asarray(coast_t, float)) if coast_t is not None and len(coast_t) else np.zeros(0)
    brk = np.zeros(len(t) - 1, bool)
    if len(ct):
        idx = np.searchsorted(ct, t)          # coasts strictly before each sample
        brk |= idx[1:] != idx[:-1]            # a coast lies in [t_i, t_{i+1})
    dt = np.diff(t)
    med = float(np.median(dt)) if len(dt) else 0.0
    if med > 0:
        brk |= dt > max(3.0, GAP_FACTOR * med)
    where = np.flatnonzero(brk) + 1
    if not len(where):
        return t, ys
    return np.insert(t, where, np.nan), [np.insert(y, where, np.nan) for y in ys]


def _segments(t: np.ndarray) -> list[slice]:
    """Contiguous finite runs of a NaN-broken array."""
    ok = np.isfinite(t)
    out, start = [], None
    for i, o in enumerate(ok):
        if o and start is None:
            start = i
        elif not o and start is not None:
            out.append(slice(start, i))
            start = None
    if start is not None:
        out.append(slice(start, len(t)))
    return out


# ── vehicle heads -> Plotly layout images in DATA coordinates ────────────────
def heads(A: dict, last_hdg: dict | None = None) -> dict:
    """{tgt|itc: {E, N, U, spd, hdg} | None} from the current truth heads.  hdg = degrees clockwise
    from north (atan2(vE, vN)); below 0.5 m/s the last known heading is kept (``last_hdg`` is
    updated in place so a hovering vehicle does not spin)."""
    out = {}
    for key in ("tgt", "itc"):
        p = A.get(f"{key}_now")
        if p is None:
            out[key] = None
            continue
        spd = float(np.hypot(p[TR["vE"]], p[TR["vN"]]))
        if spd > 0.5:
            hdg = float(np.degrees(np.arctan2(p[TR["vE"]], p[TR["vN"]])))
            if last_hdg is not None:
                last_hdg[key] = hdg
        else:
            hdg = float((last_hdg or {}).get(key, 0.0))
        out[key] = {"E": round(float(p[TR["E"]]), 1), "N": round(float(p[TR["N"]]), 1), "U": round(float(p[TR["U"]]), 1),
                    "spd": round(spd, 1), "hdg": round(hdg, 1)}
    return out


HEAD_COLOR = {"tgt": T.TARGET, "itc": T.INTERCEPTOR}


def icon_size_m(view_w_m: float) -> float:
    """Icon edge in metres for a map view ``view_w_m`` wide: 6 % of it, never under 40 m."""
    return float(max(ICON_MIN_M, ICON_FRAC * float(view_w_m)))


def heads_images(heads: dict, view_w_m: float, surface: str = T.SURFACE) -> list[dict]:
    """The two vehicle icons as Plotly layout-image dicts in DATA coordinates, fixed order
    (tgt, itc) so Plotly's index-based <image> join keeps each element between relayouts:
    x/y = the head's E/N (metres about the radar), sizex = sizey = icon_size_m(view_w_m),
    centred, sizing "contain", layer "above"; heading quantised to 5° buckets with the data
    URI cached per bucket (ih.icons.icon_uri) so ``source`` only changes when the heading
    crosses a step.  A missing head keeps its slot with visible=False."""
    out = []
    sz = icon_size_m(view_w_m)
    for key in ("tgt", "itc"):
        h = (heads or {}).get(key)
        if not h:
            out.append(dict(source="data:image/svg+xml;utf8,", xref="x", yref="y", x=0.0, y=0.0, sizex=sz, sizey=sz,
                            xanchor="center", yanchor="middle", sizing="contain", layer="above", visible=False, name=f"{key}_icon"))
            continue
        hq = icons.heading_q(float(h["hdg"]))
        out.append(dict(source=icons.icon_uri(key, hq, HEAD_COLOR[key], surface, True), xref="x", yref="y",
                        x=float(h["E"]), y=float(h["N"]), sizex=sz, sizey=sz, xanchor="center", yanchor="middle",
                        sizing="contain", layer="above", visible=True, name=f"{key}_icon"))
    return out


# ── map ──────────────────────────────────────────────────────────────────────
def map_fig(A: dict, P: dict) -> go.Figure:
    """Top-down map.  P keys: map_half, trail_s, show_sat, show_blind, map_height, sat_include,
    icons_in_fig, frame (FIXED footprint box or None = FOLLOW), font_px, line_w.
    Truth trails (decimated to <= 2 pts/s, scattergl) + a 5 s leader per head, the TARGET's radar
    track (dashed, team red) and the INTERCEPTOR's radar track when one exists (dashed, team blue,
    2 px at .5 alpha: secondary), ★ CPA once gated, optional blind rings, satellite underlay
    (fallback path only — the panel receives tiles through the envelope for its own view).  The panel
    JS adds the "live segments" (trail tail -> tweened head) as layout shapes at tween rate."""
    x0, x1, y0, y1 = frame_box(P.get("frame"), A, float(P["map_half"]))
    lw = float(P.get("line_w", LINE_W))
    _ant = A.get("ant") or (D.ANT_LAT, D.ANT_LON, D.ANT_HAE)
    ant_hae = float(_ant[2]) if len(_ant) > 2 and np.isfinite(float(_ant[2])) else 0.0   # track U (above the radar) -> HAE
    fig = go.Figure()
    n_series = 0
    if P.get("show_sat") and P.get("sat_include", True):
        ant = A.get("ant") or D.ANT_LL
        # square box a little wider than the axis range (constrain="range" shows what fits, quantised -> cached)
        cx, cy, hx = 0.5 * (x0 + x1), 0.5 * (y0 + y1), 0.5 * (x1 - x0) * 1.15
        r = D.sat_payload(cx - hx, cx + hx, cy - hx, cy + hx, (ant[0], ant[1]))
        if r.get("img"):
            fig.add_layout_image(dict(source=r["img"], xref="x", yref="y", x=r["x0"], y=r["y1"], sizex=r["x1"] - r["x0"],
                                      sizey=r["y1"] - r["y0"], xanchor="left", yanchor="top", sizing="stretch", layer="below", opacity=0.9))
    if P.get("show_blind"):
        rings = blind_rings()
        for i, (m, rad) in enumerate(rings):
            x, y = _circle(rad)
            fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line=dict(color=T.FAIL, width=1, dash="dot"), opacity=0.8, name="blind zone (c·pw/2)",
                                     legendgroup="blind", showlegend=(i == 0), hovertemplate=f"{m}<br>blind radius {rad:.0f} m<extra></extra>"))
            fig.add_annotation(x=0, y=rad, text=f"{rad/1000:.2f} km", showarrow=False, yanchor="bottom", font=dict(size=10, color=T.INK2, family=T.MONO))
        n_series += bool(rings)
    # radar origin: hollow ink ring + ink label
    # (marker only — the "RADAR" text clipped at the plot edge whenever the origin sat just outside the view; "I don't need to see
    # where the radar is": the hollow ring is enough for orientation)
    fig.add_trace(go.Scatter(x=[0], y=[0], mode="markers", name="radar", marker=dict(symbol="circle", size=10, color=T.CARD, line=dict(width=2, color=T.INK)),
                             hoverinfo="skip", showlegend=False))
    # truth trails (scattergl, <= 2 pts/s) + a thin 5 s velocity leader from each head (may lag the icon by <= 2 s: fine)
    for key, col, name in (("itc", T.INTERCEPTOR, "interceptor truth"), ("tgt", T.TARGET, "target truth")):
        tr = A.get(f"{key}_trail")
        if tr is not None and len(tr):
            tr = downsample(tr)
            fig.add_trace(go.Scattergl(x=tr[:, TR["E"]], y=tr[:, TR["N"]], mode="lines", name=name, line=dict(color=col, width=lw),
                                       text=_hover_t(tr[:, 0]), customdata=_alt_spd(tr, TR, ant_hae),
                                       hovertemplate="%{text}<br>E %{x:.0f} m · N %{y:.0f} m" + ALT_HOVER + "<extra>" + name + "</extra>"))
            n_series += 1
        p = A.get(f"{key}_now")
        if p is not None and P.get("icons_in_fig", True):   # fallback charts only: the panel JS draws the leader as a layout shape (5 s travel, capped on screen)
            fig.add_trace(go.Scatter(x=[p[TR["E"]], p[TR["E"]] + p[TR["vE"]] * LEADER_S], y=[p[TR["N"]], p[TR["N"]] + p[TR["vN"]] * LEADER_S], mode="lines",
                                     name=f"{key}_leader", line=dict(color=col, width=1), hoverinfo="skip", showlegend=False))
    if P.get("icons_in_fig"):   # st.plotly_chart fallback: the icons live inside the figure (same dicts the panel relayouts)
        for im in heads_images(heads(A), x1 - x0):
            if im.get("visible"):
                fig.add_layout_image(im)
    # radar tracks: dashed estimate lines (no per-state markers — coasting shows on the error panel as gaps);
    # target = team red; interceptor = team blue, thinner and faded (secondary) — map only, never in the error stats
    for key, tid_key, col, w, alpha in (("tgt_track", "tgt_tid", T.TARGET, lw, 1.0), ("itc_track", "itc_tid", T.INTERCEPTOR, ITC_TRACK_W, ITC_TRACK_ALPHA)):
        tk = A.get(key)
        if tk is None or not len(tk) or A.get(tid_key) is None:
            continue
        tr = tk[tk[:, 0] >= A["t_now"] - float(P["trail_s"])]
        if not len(tr):
            continue
        tr = downsample(tr)
        role = "target" if key == "tgt_track" else "interceptor"
        name = f"{role} track #{A[tid_key]}"
        # the INTERCEPTOR track stays OUT of the legend (showlegend False): it is the secondary series (thin, faded), its "#200" pill
        # names it on the map and the card caption says "dashed = radar tracks" — and the key must fit ONE row in the top margin
        legend_it = key == "tgt_track"
        fig.add_trace(go.Scattergl(x=tr[:, TK["E"]], y=tr[:, TK["N"]], mode="lines", name=name, opacity=alpha, showlegend=legend_it,
                                   line=dict(color=col, width=w, dash="dash"), text=_hover_t(tr[:, 0]), customdata=_alt_spd(tr, TK, ant_hae),
                                   hovertemplate="%{text}<br>E %{x:.0f} m · N %{y:.0f} m" + ALT_HOVER + "<extra>" + name + "</extra>"))
        n_series += int(legend_it)
    # track-number pills ("#177" red / "#203" blue) at the newest point of each track line — the panel relayouts the same dicts every tick
    for pill in track_pills(A, P):
        fig.add_annotation(**pill)
    # NO TRUTH (live unit without a MAVLink feed): every active radar track as a grey dashed trail with its id at the
    # head — the radar is visibly working even though nothing can be correlated or graded (one legend entry)
    free = A.get("free_tracks") or {}
    if free and ungraded_view(A):      # no TARGET truth (no feed at all, or the interceptor-only case)
        hx, hy, ht, hcd = [], [], [], []
        for j, tid in enumerate(sorted(free)):
            tk = free[tid]
            tr = tk[tk[:, 0] >= A["t_now"] - float(P["trail_s"])]
            if not len(tr):
                continue
            tr = downsample(tr)
            fig.add_trace(go.Scattergl(x=tr[:, TK["E"]], y=tr[:, TK["N"]], mode="lines", name="radar tracks (no truth)", legendgroup="free",
                                       showlegend=not hx, opacity=0.8, line=dict(color=T.GREY_TRACK, width=ITC_TRACK_W, dash="dash"),
                                       text=_hover_t(tr[:, 0]), customdata=_alt_spd(tr, TK, ant_hae),
                                       hovertemplate=f"track #{tid}<br>%{{text}}<br>E %{{x:.0f}} m · N %{{y:.0f}} m" + ALT_HOVER + "<extra></extra>"))
            hx.append(float(tr[-1, TK["E"]])); hy.append(float(tr[-1, TK["N"]])); ht.append(f"#{tid}")
            hcd.append([float(tr[-1, TK["U"]]) + ant_hae, float(np.hypot(tr[-1, TK["vE"]], tr[-1, TK["vN"]]))])
        if hx:
            fig.add_trace(go.Scatter(x=hx, y=hy, mode="markers+text", name="radar track heads", legendgroup="free", showlegend=False, text=ht,
                                     textposition="top center", textfont=dict(color=T.INK2, family=T.MONO, size=10), customdata=hcd,
                                     marker=dict(symbol="circle", size=8, color=T.GREY_TRACK, line=dict(width=2, color=T.CARD)),
                                     hovertemplate="track %{text}<br>E %{x:.0f} m · N %{y:.0f} m" + ALT_HOVER + "<extra></extra>"))
            n_series += 1
    # UNASSIGNED live MAVLink drones (an id matching neither auto-assign pattern): a GREY trail + a grey head with the
    # id on a pill — ALWAYS drawn (never gated on truth / grading): the operator must see every drone in the air.
    # Same axes, same clipping and the same equal-aspect ranges as the target / interceptor truth.
    oth = A.get("other_truth") or []
    if oth:
        ox, oy, first = [], [], True
        for o in oth:
            tr = o.get("trail")
            tr = np.asarray(tr, float) if tr is not None else np.zeros((0, 7))
            if len(tr):
                tr = downsample(tr)
                fig.add_trace(go.Scattergl(x=tr[:, TR["E"]], y=tr[:, TR["N"]], mode="lines", name="unassigned drones",
                                           legendgroup="oth", showlegend=first, opacity=0.9,
                                           line=dict(color=T.GREY_TRACK, width=lw), text=_hover_t(tr[:, 0]), customdata=_alt_spd(tr, TR, ant_hae),
                                           hovertemplate=f"{o['id']} · unassigned<br>%{{text}}<br>E %{{x:.0f}} m · N %{{y:.0f}} m" + ALT_HOVER + "<extra></extra>"))
                first = False
            ox.append(float(o["E"])); oy.append(float(o["N"]))
        # the legend entry rides on the head markers when no trail had 2+ points yet (a drone parked on the pad:
        # one row in the window -> an invisible line and, before this, a legend entry for nothing)
        n_series += 1
        fig.add_trace(go.Scatter(x=ox, y=oy, mode="markers", name="unassigned drone heads", legendgroup="oth", showlegend=first,
                                 marker=dict(symbol="diamond", size=10, color=T.GREY_TRACK, line=dict(width=2, color=T.CARD)),
                                 text=[f"{o['id']} · unassigned · age {o['age']:.1f} s" for o in oth],
                                 customdata=[[float(o["U"]) + ant_hae, float(o["spd"])] for o in oth],
                                 hovertemplate="%{text}<br>E %{x:.0f} m · N %{y:.0f} m" + ALT_HOVER + "<extra></extra>"))
        for pill in other_pills(A, P):
            fig.add_annotation(**pill)
    # CPA (validated by the engine's gate): gold star + ink label
    cpa = A.get("cpa")
    if cpa is not None:
        fig.add_trace(go.Scatter(x=[cpa[2]], y=[cpa[3]], mode="markers+text", name="CPA", text=[f"  CPA {cpa[0]:.0f} m"], textposition="middle right",
                                 marker=dict(STAR), textfont=dict(color=T.INK, family=T.MONO, size=11),
                                 hovertemplate=f"CPA {cpa[0]:.0f} m (3D) · {cpa[4]:.0f} m horiz<br>{D.pdt_hms(cpa[1])}<extra></extra>"))
        n_series += 1
    # (the "live segments" trail tail -> tweened head are layout SHAPES the panel JS owns: an arraydraw edit, no trace re-render)
    font_px = int(P.get("font_px", FONT_PX))
    margin = map_margin(font_px, float(P.get("text_scale", 1.0) or 1.0))
    base_layout(fig, height=int(P.get("map_height", 640)), uirevision=UIREV["map"], legend=n_series >= 2, margin=margin, font_px=font_px)
    # TRACK STATUS on the map's top row (right end of the key row): "#177 CONFIRMED" green / TENTATIVE amber / COASTING amber-red /
    # NO TRACK red (2026-09-15 "add a track status to that top panel - coasting / live / dropped")
    # the engine's words are already the honest ones ("NO TARGET FEED" amber = interceptor truth only; "NO TRUTH FEED" red = no
    # MAVLink truth at all); the legacy short forms are kept so an older A dict still reads correctly
    _words = {"CONF": "CONFIRMED", "TENT": "TENTATIVE", "COASTING": "COASTING", "NO TRACK": "NO TRACK", "NO TRUTH": "NO TRUTH FEED",
              "NO TRUTH FEED": "NO TRUTH FEED", "NO TARGET": "NO TARGET FEED", "NO TARGET FEED": "NO TARGET FEED"}
    _state = "TRACK CHANGED" if A.get("tgt_flash") else _words.get(str(A.get("track_state")), str(A.get("track_state") or "NO TRACK"))
    _col = {"ok": T.GREEN, "amber": T.AMBER, "fail": T.FAIL, "na": T.INK3}.get("amber" if A.get("tgt_flash") else str(A.get("track_cls") or "na"), T.INK3)
    _tid = f"#{A['tgt_tid']} " if A.get("tgt_tid") is not None else ""
    fig.add_annotation(xref="paper", yref="paper", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=2, showarrow=False,
                       text=f"<b>{_tid}{_state}</b>", font=dict(family=T.MONO, size=font_px + 2, color=_col), bgcolor=TAG_BG, borderpad=2, name="track_status")
    # THE KEY NEVER SITS ON THE IMAGERY: one horizontal row in the top margin, bottom-anchored just above the plot area (paper y = 1),
    # under the hover modebar's own row (MAP_MODEBAR_PX) — the two can never collide and a wider key grows UP into that row, never down
    # over the map.  margin.autoexpand is OFF (map_margin) so the reserved room is EXACT: the legend's size can never move the plot
    # area, i.e. never re-derive the equal-aspect ranges ("snap").  Transparent: it sits on the card paper, not on the imagery.
    fig.update_layout(legend=dict(x=0.0, xanchor="left", y=1.0, yanchor="bottom", bgcolor="rgba(0,0,0,0)"),
                      modebar=dict(remove=list(MAP_MODEBAR_DROP), bgcolor="rgba(0,0,0,0)", color=T.INK3, activecolor=T.INK),
                      dragmode="pan",   # automargin OFF: with scaleanchor + constrain="range" a margin change (wider tick labels far out) re-derives the ranges -> the view "snaps"
                      xaxis=dict(title_text="East of radar (m)", range=[x0, x1], constrain="range", automargin=False),
                      yaxis=dict(title_text="North of radar (m)", range=[y0, y1], scaleanchor="x", scaleratio=1, constrain="range", automargin=False))
    return fig


def aspect_range(box: tuple[float, float, float, float], plot_w_px: float, plot_h_px: float) -> tuple[list[float], list[float]]:
    """The axis ranges that show ``box`` (x0, x1, y0, y1) at 1:1 pixels in a plot area of the given size: the
    box fits entirely, centred, and the extra room goes to the longer side — so xaxis.range and yaxis.range
    are CONSISTENT with equal aspect (setting both to the raw box while scaleanchor/constrain are on makes
    plotly drop the aspect -> squashed satellite).  Python mirror of the panel JS aspectRange()."""
    x0, x1, y0, y1 = (float(v) for v in box)
    w, h = max(1.0, float(plot_w_px)), max(1.0, float(plot_h_px))
    m = max((x1 - x0) / w, (y1 - y0) / h)            # metres per pixel that fits the box both ways
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    return [cx - 0.5 * m * w, cx + 0.5 * m * w], [cy - 0.5 * m * h, cy + 0.5 * m * h]


def map_view(fig: go.Figure) -> dict:
    """{x0, x1, y0, y1} = the axis box the map figure was built for (the panel applies it on the first
    paint / explicit frame changes only; later map reacts carry no ranges so the user's view survives)."""
    xr, yr = fig.layout.xaxis.range, fig.layout.yaxis.range
    return {"x0": float(xr[0]), "x1": float(xr[1]), "y0": float(yr[0]), "y1": float(yr[1])}


def trail_tails(A: dict) -> dict:
    """{tgt|itc: [E, N] | None}: the LAST point of each truth trail as drawn by the map figure — the
    anchor of the panel's live segment (tail -> tweened head)."""
    out = {}
    for key in ("tgt", "itc"):
        tr = A.get(f"{key}_trail")
        out[key] = [round(float(tr[-1, TR["E"]]), 1), round(float(tr[-1, TR["N"]]), 1)] if tr is not None and len(tr) else None
    return out


# ── engagement: ONE separation card (the closing-rate figure was dropped; the rate lives on the tile) ──
def separation_fig(A: dict, P: dict) -> go.Figure:
    """SEPARATION · 3D & HORIZONTAL (m): 3D in primary ink, horizontal in muted ink, ★ CPA + gold
    hairline once the engine's gate validates it (A["cpa"] is None until then), red "now" hairline,
    unified hover.  Window = since flight start (autorange: the user's zoom survives reacts)."""
    S = A["sep"]
    x = D.to_pdt_dt64(S["t"])
    lw = float(P.get("line_w", LINE_W))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=S["sep"], mode="lines", name="separation 3D", line=dict(color=T.INK, width=lw), connectgaps=False,
                             hovertemplate="%{y:.0f} m"))
    fig.add_trace(go.Scatter(x=x, y=S["horiz"], mode="lines", name="horizontal", line=dict(color=T.GREY_TRACK, width=lw), connectgaps=False,
                             hovertemplate="%{y:.0f} m"))
    cpa = A.get("cpa")
    if cpa is not None:
        # the ★ stays (data); its label moved OUT of the plot area into the card header (ih.liveserver "hud", see cpa_hud) — text over the
        # newest samples covered the right side of the plot (t = now is what the operator watches)
        fig.add_trace(go.Scatter(x=[_dt(cpa[1])], y=[cpa[0]], mode="markers", name="CPA", marker=dict(STAR),
                                 hovertemplate=f"CPA %{{y:.0f}} m · {D.pdt_hms(cpa[1])}<extra></extra>"))
        fig.add_vline(x=_dt(cpa[1]), line=dict(color=T.GOLD, width=1))
    fig.add_vline(x=_dt(A["t_now"]), line=dict(color=T.RED, width=1))
    # X_PAD_FRAC right padding: an invisible anchor beyond "now" widens the autorange (the user's zoom still survives reacts: autorange stays autorange)
    t_arr = np.asarray(S["t"], float)
    t0 = float(np.nanmin(t_arr)) if len(t_arr) and np.isfinite(t_arr).any() else float(A["t_now"]) - 60.0
    fig.add_trace(go.Scatter(x=[_dt(float(A["t_now"]) + X_PAD_FRAC * max(60.0, float(A["t_now"]) - t0))], y=[0.0], mode="markers",
                             marker=dict(size=1, opacity=0, color=T.CARD), hoverinfo="skip", showlegend=False, name="_anchor_pad"))
    # EMPTY STATE (truth-to-truth needs BOTH heads): a muted readout in the header strip saying WHY the card is blank —
    # "no target truth" with an interceptor-only MAVLink feed, "no truth feed" with no MAVLink truth at all (2026-09-15 MRU91)
    if not np.isfinite(S["sep"]).any():
        _sep_txt = NO_TGT_SEP_READOUT if no_tgt_truth(A) else (NO_TRUTH_SEP_READOUT if not A.get("has_truth", True) else EMPTY_READOUT)
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=2,
                           text=_sep_txt, showarrow=False, font=dict(family=T.MONO, size=px(READOUT_PX, P), color=T.INK3),
                           bgcolor=TAG_BG, borderpad=1, name="readout_sep")
    font_px = int(P.get("font_px", FONT_PX))
    base_layout(fig, height=int(P.get("sep_height", 240)), uirevision=UIREV["sep"], legend=True, time_x=True, unified=True,
                margin=card_margin(font_px, float(P.get("text_scale", 1.0) or 1.0), t=MARGINS["sep"]["t"], autoexpand=True), font_px=font_px, fixed=True)   # autoexpand stays ON for its key row (no strip budget on this card); the AXES never push (fixed)
    ymax = np.nanmax(S["sep"]) if np.isfinite(S["sep"]).any() else 1000
    top = nice_ceil(max(200.0, float(ymax) * 1.08))                                       # a NICE top, held against jitter (stable_range + dwell): the tick set follows the range only
    _, top = stable_range(P.get("yrng_mem"), "sep", "sep", 0.0, top, data_lo=0.0, data_hi=float(ymax) if np.isfinite(ymax) else 0.0)
    fig.update_yaxes(title_text="sep m", range=[0, top], tickvals=nice_ticks(0.0, top, 3))
    fig.update_xaxes(title_text="", dtick=x_dtick_ms(max(60.0, float(A["t_now"]) - t0), 4))   # 2026-09-15: no axis title — with the fixed bottom margin it sat ON the HH:MM:SS ticks; the clock format is self-evident
    return fig


# ── track-quality (error) panel ──────────────────────────────────────────────
def hole_segments(tg: np.ndarray, yg: np.ndarray, tu: np.ndarray, yu: np.ndarray, gap_s: float) -> tuple[np.ndarray, np.ndarray]:
    """The dotted "ungraded" line: every ungraded sample (tu, yu) plus its graded neighbours
    (tg, yg) in time order, so each hole in the solid line is bridged from graded sample to
    graded sample; NaN breaks between holes and across data gaps > gap_s.  Returns (t, y)
    (empty when there is nothing ungraded)."""
    tg, yg, tu, yu = (np.asarray(v, float) for v in (tg, yg, tu, yu))
    fu = np.isfinite(yu) & np.isfinite(tu)
    if not fu.any():
        return np.zeros(0), np.zeros(0)
    fg = np.isfinite(yg) & np.isfinite(tg)
    t = np.concatenate([tg[fg], tu[fu]])
    y = np.concatenate([yg[fg], yu[fu]])
    g = np.concatenate([np.ones(int(fg.sum()), bool), np.zeros(int(fu.sum()), bool)])
    o = np.argsort(t, kind="stable")
    t, y, g = t[o], y[o], g[o]
    n = len(t)
    same_run = np.diff(t) <= gap_s                              # neighbours only across a small gap
    keep = ~g
    if n > 1:
        keep[:-1] |= (~g[1:]) & same_run                        # graded sample right before an ungraded one
        keep[1:] |= (~g[:-1]) & same_run                        # ... and right after
    idx = np.flatnonzero(keep)
    if not len(idx):
        return np.zeros(0), np.zeros(0)
    brk = np.flatnonzero((np.diff(idx) > 1) | (np.diff(t[idx]) > gap_s)) + 1
    return np.insert(t[idx], brk, np.nan), np.insert(y[idx], brk, np.nan)


def merged_series(t, y, sg, tu, yu, gap_s: float):
    """Graded (t, y, σ) ∪ ungraded (tu, yu) rows in time order -> (ta, ya, sa, ga, ui, brk): σ of an ungraded
    row = linear interpolation between its graded neighbours (clamped at the ends, NaN with no graded row at
    all), ga True on graded rows, ui = index into tu for ungraded rows (−1 otherwise), brk[i] True when the
    step ta[i−1] -> ta[i] exceeds gap_s (a real dropout: the line and band break there and nowhere else)."""
    t, y, sg, tu, yu = (np.asarray(v, float) for v in (t, y, sg, tu, yu))
    fg = np.isfinite(t) & np.isfinite(y)
    fu = np.isfinite(tu) & np.isfinite(yu)
    ta = np.concatenate([t[fg], tu[fu]])
    ya = np.concatenate([y[fg], yu[fu]])
    ga = np.concatenate([np.ones(int(fg.sum()), bool), np.zeros(int(fu.sum()), bool)])
    ui = np.concatenate([np.full(int(fg.sum()), -1), np.flatnonzero(fu)])
    o = np.argsort(ta, kind="stable")
    ta, ya, ga, ui = ta[o], ya[o], ga[o], ui[o]
    sa = np.full(len(ta), np.nan)
    sgg = sg[fg] if len(sg) == len(t) else np.full(int(fg.sum()), np.nan)
    ok = np.isfinite(sgg)
    if ok.any():
        sa[ga] = sgg
        sa[~ga] = np.interp(ta[~ga], t[fg][ok], sgg[ok])       # σ carried across the hole, linearly between the graded neighbours
    brk = np.zeros(len(ta), bool)
    if len(ta) > 1:
        brk[1:] = np.diff(ta) > gap_s
    return ta, ya, sa, ga, ui, brk


def _runs_of(mask: np.ndarray, brk: np.ndarray | None = None) -> list[tuple[int, int]]:
    """Maximal runs [i0, i1] (inclusive) of True in ``mask``, also split where ``brk`` is True."""
    out, start = [], None
    for i, m in enumerate(mask):
        cut = brk is not None and i > 0 and bool(brk[i])
        if m and start is not None and cut:
            out.append((start, i - 1))
            start = i
        elif m and start is None:
            start = i
        elif not m and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def _ev(e) -> tuple:
    """A track-change event as (t, role, old, new, rule) from the engine's tuple or dict shape."""
    if isinstance(e, dict):
        return (float(e.get("t", 0.0)), str(e.get("role", "target")), e.get("old"), e.get("new"), str(e.get("rule") or ""))
    t, role, old, new = (list(e) + [None] * 4)[:4]
    return (float(t), str(role), old, new, "")


def handover_marks(fig: go.Figure, events, t_lo: float, t_now: float, cells: list[tuple[int, int]], label_cells: set[tuple[int, int]], P: dict | None = None,
                   stack_px: float | None = None, roles: tuple = ("target", "interceptor"), labels_out: list | None = None) -> int:
    """Thin vertical tick at the TOP edge of every cell (row, col) for each track-id change inside the window
    (target: secondary-ink tick, interceptor: blue tick) + a "→ #177" / "itc → #203" label on the ``label_cells``
    — id changes read in time context on the error panel and the measurement quad.  Two label styles:
    * ``labels_out`` given (error / velocity cards): the label is VISIBLE — "→ #177 · 07:21:57" is appended to ``labels_out``
      as (t, text, name) and the caller places it with top_labels() (ink text on TAG_BG at the top of the tick, anchored left
      of the tick near the right edge, stacked when labels crowd) — a vertical line always says what it is;
    * otherwise (measurement quad) the label is HOVER-ONLY: a small ▾ mark at the top of the tick whose hovertext carries
      "→ #177 · 07:22:05" (the quad's top edge is crowded with pills and titles).
    ``stack_px`` is accepted for call compatibility and unused.  Returns the number of events marked."""
    n = 0
    tick_px = px(TICK_PX, P)
    for e in events or ():
        t, role, old, new, _ = _ev(e)
        if new is None or not (t_lo <= t <= t_now) or role not in roles:   # the target's error cards show TARGET handovers only (interceptor ticks read as "unexplained blue lines")
            continue
        col = T.INK2 if role == "target" else T.INTERCEPTOR
        txt = f"→ #{new}" if role == "target" else f"itc → #{new}"
        for (r, c) in cells:
            fig.add_shape(type="line", xref="x", yref="y domain", x0=_dt(t), x1=_dt(t), y0=0.86, y1=1.0, line=dict(color=col, width=1),
                          name=f"handover_{role}_{new}", row=r, col=c)
            if (r, c) in label_cells and labels_out is not None:
                labels_out.append((float(t), f"{txt} · {D.pdt_hms(t)}", f"handover_label_{role}_{new}"))
            elif (r, c) in label_cells:
                # hover-only: a small mark at the top edge carrying the id + time on hover — never text stacked over the data or the
                # readout (the TRACK CHANGES card lists every handover in full)
                fig.add_annotation(xref="x", yref="y domain", x=_dt(t), y=1.0, xanchor="center", yanchor="top", yshift=-1, text="▾",
                                   hovertext=f"{txt} · {D.pdt_hms(t)}", showarrow=False, font=dict(family=T.MONO, size=max(9, tick_px - 2), color=T.INK2),   # text never wears a series colour; the tick line carries the role
                                   name=f"handover_tag_{role}_{new}", row=r, col=c)
        n += 1
    return n


def cpa_marks(fig: go.Figure, A: dict, t_lo: float, t_now: float, rows: int, labels_out: list | None = None) -> None:
    """The validated CPA (A["cpa"] = (sep3d, t, ...)) as a gold hairline on every card of a stacked card figure, labelled once
    ("CPA 59 m · 07:22:31") through ``labels_out`` (placed by strip_labels in the first card's header strip)."""
    cpa = A.get("cpa")
    if not cpa or not (t_lo <= float(cpa[1]) <= t_now):
        return
    for r in range(1, rows + 1):
        fig.add_shape(type="line", xref="x" if r == 1 else f"x{r}", yref="y domain" if r == 1 else f"y{r} domain", x0=_dt(cpa[1]), x1=_dt(cpa[1]), y0=0, y1=1,
                      line=dict(color=T.GOLD, width=1), layer="above", name=f"cpa_line_{r}")
    if labels_out is not None:
        labels_out.append((float(cpa[1]), f"CPA {cpa[0]:.0f} m · {D.pdt_hms(cpa[1])}", "cpa_label"))


def top_labels(fig: go.Figure, items: list[tuple[float, str, str]], t_lo: float, t_now: float, P: dict | None = None, row: int = 1, col: int = 1) -> int:
    """Visible ink labels at the top of the vertical hairlines of ONE card (the first): ``items`` = (t, text, name) — the CPA
    ("CPA 59 m · 07:22:31") and the handovers ("→ #177 · 07:21:57").  Style: px(TITLE_PX - LABEL_PX_DELTA) mono in T.INK on
    TAG_BG, yanchor top at y domain 1.0 (inside the plot area, never in the header strip), anchored RIGHT of its line
    (xshift +3) or LEFT of it when the line is past LABEL_RIGHT_FRAC of the window (xshift −3, the label never leaves the
    card).  Labels closer than LABEL_SEP_FRAC of the window stack one text line lower (slot).  Returns the number placed."""
    span = max(1e-9, float(t_now) - float(t_lo))
    f = px(TITLE_PX - LABEL_PX_DELTA, P)
    line_h = ERR_BOX_LH * f + ERR_BOX_PAD + 2.0
    placed: list[tuple[float, int]] = []                                   # (t, slot)
    for t, text, name in sorted(items, key=lambda it: it[0]):
        slot = 0
        while any(sl == slot and abs(t - tp) / span < LABEL_SEP_FRAC for tp, sl in placed):
            slot += 1
        placed.append((t, slot))
        right = (t - t_lo) / span > LABEL_RIGHT_FRAC
        fig.add_annotation(xref="x", yref="y domain", x=_dt(t), y=1.0, xanchor="right" if right else "left", yanchor="top",
                           xshift=-3 if right else 3, yshift=-2 - slot * line_h, text=text, showarrow=False,
                           font=dict(family=T.MONO, size=f, color=T.INK), bgcolor=TAG_BG, borderpad=1, name=name, row=row, col=col)
    return len(placed)


def strip_labels(fig: go.Figure, items: list[tuple[float, str, str]], P: dict | None = None, row: int = 1, col: int = 1) -> int:
    """The labels of ONE card's vertical ticks as a chronological LIST in its header strip — second row, LEFT (the containment text is
    right-aligned there), never inside the plot area: "→ #122 · 07:19:50 · → #129 · 07:20:13".  Style: READOUT_SUB_PX mono in
    primary ink on TAG_BG (annotation name "tick_labels").  Newest STRIP_LABELS_MAX entries.  Returns the number listed."""
    if not items:
        return 0
    items = sorted(items, key=lambda it: it[0])[-STRIP_LABELS_MAX:]
    fig.add_annotation(xref="x domain", yref="y domain", x=0.0, y=1.0, xanchor="left", yanchor="bottom", xshift=2, yshift=1,
                       text="   ".join(t for _, t, _ in items), showarrow=False, font=dict(family=T.MONO, size=px(READOUT_SUB_PX, P), color=T.INK),
                       bgcolor=TAG_BG, borderpad=1, name="tick_labels", row=row, col=col)
    return len(items)


def clamp_range(lo: float, hi: float, lim_lo: float, lim_hi: float) -> tuple[float, float]:
    """[lo, hi] cut to the hard axis limits (never widened).  Applied BEFORE stable_range (its memory only sees clamped
    ranges) and AGAIN after it (a held range can never be wider than the clamp)."""
    lo, hi = max(float(lo), float(lim_lo)), min(float(hi), float(lim_hi))
    return (lo, hi) if hi > lo else (float(lim_lo), float(lim_hi))


def err_half(key: str, mags: list, sigs: list) -> float:
    """Robust half-range of an error card: nice_ceil(max(ERR_FLOOR, 2·median σ, ERR_P_SCALE · P95(|err| + 1σ))) capped at
    ERR_CLAMP[key] — ``mags`` = arrays of |err| + 1σ (|err| where σ is missing) over the window, ``sigs`` = the finite σ arrays
    (the 2·median σ floor keeps the band readable)."""
    need = float(ERR_FLOOR[key])
    if mags:
        m = np.concatenate([np.asarray(v, float).ravel() for v in mags])
        m = m[np.isfinite(m)]
        if len(m):
            need = max(need, ERR_P_SCALE * float(np.percentile(m, ERR_P)))
    if sigs:
        sg = np.concatenate([np.asarray(v, float).ravel() for v in sigs])
        sg = sg[np.isfinite(sg)]
        if len(sg):
            need = max(need, 2.0 * float(np.median(sg)))
    return min(nice_ceil(need), float(ERR_CLAMP[key]))


def err_range(key: str, mags: list, sigs: list, mem: dict | None = None) -> tuple[float, float]:
    """The card's y-range: err_half() -> stable_range (jitter hold) -> clamp_range (the hold never widens past ERR_CLAMP)."""
    half = err_half(key, mags, sigs)
    lim_lo, lim_hi = (0.0, ERR_CLAMP[key]) if key in ONE_SIDED else (-ERR_CLAMP[key], ERR_CLAMP[key])
    lo, hi = clamp_range(*((0.0, half) if key in ONE_SIDED else (-half, half)), lim_lo, lim_hi)
    d_hi = 0.0                                                          # the raw data extent (|err| + 1σ): the dwell rule's "data left the range"
    if mags:
        m = np.concatenate([np.asarray(v, float).ravel() for v in mags])
        m = m[np.isfinite(m)]
        d_hi = float(m.max()) if len(m) else 0.0
    d_hi = min(d_hi, lim_hi)
    lo, hi = stable_range(mem, "err", key, lo, hi, data_lo=(0.0 if key in ONE_SIDED else -d_hi), data_hi=d_hi)
    return clamp_range(lo, hi, lim_lo, lim_hi)


def vel_range(lo_v: list, hi_v: list, mem: dict | None = None, key: str = "vn") -> tuple[float, float]:
    """A velocity card's y-range: the truth + track (+ band) span padded VEL_PAD, never narrower than ±VEL_FLOOR about its
    centre, clamped to ±VEL_CLAMP before AND after stable_range."""
    if lo_v:
        d_lo, d_hi = float(min(lo_v)), float(max(hi_v))
        c, half = 0.5 * (d_lo + d_hi), max(0.5 * (d_hi - d_lo) * (1.0 + VEL_PAD), VEL_FLOOR)
        lo, hi = c - half, c + half
    else:
        d_lo, d_hi = 0.0, 0.0
        lo, hi = -VEL_FLOOR, VEL_FLOOR
    lo, hi = clamp_range(lo, hi, -VEL_CLAMP, VEL_CLAMP)
    lo, hi = clamp_range(*quant_range(lo, hi), -VEL_CLAMP, VEL_CLAMP)   # on the nice grid: equal data -> equal range, jitter mostly lands on the same grid
    lo, hi = stable_range(mem, "vel", key, lo, hi, data_lo=max(d_lo, -VEL_CLAMP), data_hi=min(d_hi, VEL_CLAMP))
    return clamp_range(lo, hi, -VEL_CLAMP, VEL_CLAMP)


def track_pills(A: dict, P: dict | None = None) -> list[dict]:
    """Map track-number pills: "#177" at the newest point of the target's radar track and "#203" at the interceptor's,
    13 px mono in PRIMARY INK on a CARD pill whose 2 px border carries the role colour (target red / interceptor blue;
    AMBER for TRACK_FLASH_S after an id change) — text never wears a series colour, the mark beside it does —
    anchored up-right of the point so it never sits on the vehicle icon.
    Plotly annotation dicts: map_fig adds them and the panel relayouts them every tick with the heads."""
    out = []
    for key, tid_key, col, flash_key, above in (("tgt_track", "tgt_tid", T.TARGET, "tgt_flash", True), ("itc_track", "itc_tid", T.INTERCEPTOR, "itc_flash", False)):
        tk, tid = A.get(key), A.get(tid_key)
        if tk is None or not len(tk) or tid is None:
            continue
        last = tk[-1]
        # the target's pill sits ABOVE-right of its point, the interceptor's BELOW-right: at the CPA both heads are within a few
        # pixels and the two pills used to overlap (the panel's depill only stacks them by 26 px — less than one pill box at the
        # X-Large text scale).  Opposite sides put 28 px + both boxes between them, whatever the text scale.
        out.append(dict(name=f"pill_{key[:3]}", xref="x", yref="y", x=round(float(last[TK["E"]]), 1), y=round(float(last[TK["N"]]), 1), text=f"#{int(tid)}",
                        showarrow=False, font=dict(family=T.MONO, size=px(PILL_PX, P), color=T.INK), bgcolor=T.CARD,
                        bordercolor=T.AMBER if A.get(flash_key) else col, borderwidth=2, borderpad=2,
                        xanchor="left", yanchor="bottom" if above else "top", xshift=14, yshift=14 if above else -14))
    return out


OTH_PILL_NAME = "othpill"          # NOT "pill_*": the panel strips pill_* annotations from the map react (the DOM overlay owns
                                   # those two) and its overlay only knows the tgt / itc slots — these stay IN the figure


def other_pills(A: dict, P: dict | None = None) -> list[dict]:
    """"mav14552_3_0 · unassigned" pills at the head of every UNASSIGNED live MAVLink drone (A["other_truth"]).
    Same pill box as track_pills (mono, primary ink, CARD background) with a MUTED GREY border: the id is named on
    the map, no role and nothing graded.  These are figure annotations — deliberately NOT part of track_pills, whose
    list is what the panel pushes into its two-slot DOM overlay (ih.liveserver ovPills)."""
    out = []
    for i, o in enumerate(A.get("other_truth") or []):
        out.append(dict(name=f"{OTH_PILL_NAME}_{i}", xref="x", yref="y", x=round(float(o["E"]), 1), y=round(float(o["N"]), 1),
                        text=f"{o['id']} · unassigned", showarrow=False,
                        font=dict(family=T.MONO, size=px(PILL_PX, P), color=T.INK), bgcolor=T.CARD,
                        bordercolor=T.GREY_TRACK, borderwidth=2, borderpad=2,
                        xanchor="left", yanchor="middle", xshift=12, yshift=0))
    return out


def engagement_box(A: dict, pass_times=(), *, live: bool = False, pad: float = ENGAGE_PAD, min_half: float = ENGAGE_MIN_HALF_M,
                   q: float = D.FRAME_Q) -> tuple[float, float, float, float]:
    """The default map frame — the ENGAGEMENT BOX: a square about the ACTION, not the whole flight.
    Archive: bounding box of both truths during the passes (each pass time ± ENGAGE_PASS_S, plus the
    validated CPA when any) and of the current vehicle positions; live: both truths over the last
    ENGAGE_LIVE_S s plus the current positions.  Padded ``pad`` each way, half-width >= min_half, centre and
    half-width quantised to ``q`` m (a stable key for the satellite cache).  Falls back to the current
    positions alone, then to the flight footprint / live box."""
    S = A.get("sep") or {}
    pts = []
    tg, ti, ts = S.get("tgt"), S.get("itc"), S.get("t")
    if ts is not None and len(ts):
        ts = np.asarray(ts, float)
        if live:
            m = ts >= float(A["t_now"]) - ENGAGE_LIVE_S
        else:
            m = np.zeros(len(ts), bool)
            times = [float(p) for p in (pass_times or ())]
            cpa = A.get("cpa")
            if cpa is not None:
                times.append(float(cpa[1]))
            for tp in times:
                m |= np.abs(ts - tp) <= ENGAGE_PASS_S
        for arr in (tg, ti):
            if arr is not None and len(arr) == len(ts):
                a = np.asarray(arr, float)[m][:, :2]
                pts.append(a[np.isfinite(a).all(axis=1)])
    for k in ("tgt_now", "itc_now"):
        p = A.get(k)
        if p is not None:
            pts.append(np.array([[float(p[TR["E"]]), float(p[TR["N"]])]]))
    P = np.vstack([p for p in pts if len(p)]) if any(len(p) for p in pts) else np.zeros((0, 2))
    if not len(P):
        return D.LIVE_FRAME if live else D.flight_frame(int(A.get("flight", 1)))
    x0, x1, y0, y1 = float(P[:, 0].min()), float(P[:, 0].max()), float(P[:, 1].min()), float(P[:, 1].max())
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    half = max(0.5 * (x1 - x0), 0.5 * (y1 - y0)) * (1.0 + 2.0 * pad)
    half = max(float(min_half), half)
    cx, cy, half = _quant(cx, q), _quant(cy, q), float(np.ceil(half / q) * q)
    return (cx - half, cx + half, cy - half, cy + half)


def _now_pair(e: dict, key: str, t_now: float, t_lo: float):
    """(current error, current σ) at the newest graded sample in the window, or (None, None)."""
    t = np.asarray(e.get("t", ()), float); y = np.asarray(e.get(key, ()), float); sg = np.asarray(e.get(f"sig_{key}", ()), float)
    if not (len(t) and len(y) == len(t)):
        return None, None
    ok = np.flatnonzero((t >= t_lo) & np.isfinite(y))
    if not len(ok):
        return None, None
    i = int(ok[-1])
    return float(y[i]), (float(sg[i]) if len(sg) == len(t) and np.isfinite(sg[i]) else None)


def readout(e: dict, key: str, t_now: float, t_lo: float) -> str:
    """Primary takeaway for one panel: the CURRENT graded error and its 1σ at the newest graded
    sample in the window — "now +0.8° ± 1.1° (1σ)"; "last 7 s ago …" when that sample is older
    than NOW_MAX_AGE_S; "no sample" when the window holds none."""
    t = np.asarray(e.get("t", ()), float)
    y = np.asarray(e.get(key, ()), float)
    sg = np.asarray(e.get(f"sig_{key}", ()), float)
    if not (len(t) == len(y) and len(t)):
        return "no sample"
    if len(sg) != len(t):
        sg = np.full(len(t), np.nan)
    ok = np.flatnonzero((t >= t_lo) & np.isfinite(y))
    if not len(ok):
        return "no sample"
    i = int(ok[-1])
    has_sig = bool(np.isfinite(sg[i]) and sg[i] > 0)
    if key in ONE_SIDED:   # "now 46 m · σ 31 m" (the error is a magnitude; σ = 1σ radius of the covariance ellipsoid)
        body = f"{ERR_NUM[key].format(y[i])}{ERR_UNIT[key]} · σ " + (f"{ERR_SIG_NUM[key].format(sg[i])}{ERR_UNIT[key]}" if has_sig else "n/a")
    elif has_sig:
        body = f"{ERR_NUM[key].format(y[i])}{ERR_UNIT[key]} ± {ERR_SIG_NUM[key].format(sg[i])}{ERR_UNIT[key]} (1σ)"
    else:   # graded error without a usable covariance (missing p_cov): never a fake "± 0"
        body = f"{ERR_NUM[key].format(y[i])}{ERR_UNIT[key]} · σ n/a"
    age = float(t_now) - float(t[i])
    return f"now {body}" if age <= NOW_MAX_AGE_S else f"last {age:.0f} s ago {body}"


def err_strip_px(readout_px: int, sub_px: int) -> tuple[int, int]:
    """Header strip geometry for the readout / containment fonts -> (hdr, line1): the containment line sits 1 px above the plot area, the
    readout + title line starts above its box (line1), and the strip ends ERR_BOX_PAD above the readout box — the two lines never touch."""
    line1 = int(round(ERR_BOX_LH * sub_px + ERR_BOX_PAD + STRIP_ROW_GAP))       # the containment box spans [1, 1 + h2]: line 1 starts STRIP_ROW_GAP above its top (measured boxes never touch)
    return int(round(line1 + ERR_BOX_LH * readout_px + ERR_BOX_PAD + 2.0)), line1


def cpa_hud(A: dict) -> str:
    """The separation card's header text for a validated CPA ("CPA 59 m · 07:22:31"), '' until the gate passes — the label left the
    plot area (see separation_fig)."""
    cpa = A.get("cpa")
    return f"CPA {cpa[0]:.0f} m · {D.pdt_hms(cpa[1])}" if cpa is not None else ""


def contain_text(c: dict | None) -> str:
    """Second readout line: "1σ 79% · 3σ 100% · n 118" (spa rule |err| ≤ kσ over the window)."""
    if not c or not c.get("n"):
        return "no samples"
    return f"1σ {c['p1']:.0f}% · 3σ {c['p3']:.0f}% · n {c['n']}"


CARD_WARM = "#2a2119"                                          # fail-tone card fill: the card colour pulled towards amber (state, never the vehicle hue)
HDR_BAND = "rgba(255,255,255,0.045)"                           # header-strip band behind title + readout (both card types)
CARD_RULE = "#4a5160"                                          # card frame: brighter than the page rule so the cards read as separate sections
TONE_ENABLED = False                                           # 2026-09-11 user: the state frames "flash" as the error crosses σ each second and do not help -> off (code kept)
TONE_LINE = {"ok": (CARD_RULE, 1.5), "warn": (T.AMBER, 2), "fail": (T.AMBER, 3)}
TONE_FILL = {"ok": T.CARD, "warn": T.CARD, "fail": CARD_WARM}
LOW_RATE_PCT, VERY_LOW_RATE_PCT = 55.0, 30.0                    # 1σ containment below these = the covariance is lying (≈68 % expected)


def card_tone(e_now, sig_now, contain: dict | None) -> tuple[str, str]:
    """(tone, words) for one card from the CURRENT error, its σ and the window's 1σ rate — the user's ask: 'the error plots
    need to pop when there is an issue'.  ok = quiet card; warn = amber 2 px frame + words; fail = amber 3 px frame + warm fill.
    Over-confidence (rate > 80 %) is NOT an issue for a live crew and stays quiet; a missing σ never raises a tone by itself."""
    tone, words = "ok", []
    if not TONE_ENABLED:
        return tone, ""
    if e_now is not None and sig_now is not None and np.isfinite(e_now) and np.isfinite(sig_now) and sig_now > 0:
        k = abs(float(e_now)) / float(sig_now)
        if k > 3.0:
            tone, words = "fail", ["OUTSIDE 3σ"]
        elif k > 1.0:
            tone, words = "warn", ["OUTSIDE 1σ"]
    if contain and contain.get("n", 0) >= 10 and contain.get("p1") is not None:
        if contain["p1"] < VERY_LOW_RATE_PCT:
            tone = "fail"; words.append(f"1σ RATE {contain['p1']:.0f}%")
        elif contain["p1"] < LOW_RATE_PCT:
            tone = "fail" if tone == "fail" else "warn"; words.append(f"1σ RATE {contain['p1']:.0f}%")
    return tone, " · ".join(words)


RANGE_SHRINK_FRAC, RANGE_SHRINK_S = 0.6, 20.0
RANGE_DWELL_S = 10.0                                          # no range change of ANY kind within this of the last one — unless the DATA itself leaves the held range


def stable_range(mem: dict | None, fig: str, key: str, lo: float, hi: float, now: float | None = None,
                 data_lo: float | None = None, data_hi: float | None = None) -> tuple[float, float]:
    """Jitter-free axis range: ``mem`` (session-owned dict) remembers the range per card.  ``lo``/``hi`` = the padded NEED (nice half,
    P95 statistic, ...), ``data_lo``/``data_hi`` = the raw data extent (default: the need).  Rules (2026-09-15 dwell):
      * the data leaves the held range -> widen NOW to the union (the one jump that is necessary);
      * the need grew but the data is still inside -> widen only once RANGE_DWELL_S has passed since the last change;
      * the need is under RANGE_SHRINK_FRAC of the held span for RANGE_SHRINK_S -> shrink (never inside the dwell either).
    A change is only recorded when the held numbers really change.  Without a memory dict the raw range is returned."""
    if mem is None:
        return lo, hi
    import time as _t
    now = _t.time() if now is None else now
    d_lo = lo if data_lo is None else float(data_lo)
    d_hi = hi if data_hi is None else float(data_hi)
    slot = mem.setdefault(fig, {}).get(key)
    if slot is None:
        mem[fig][key] = {"lo": lo, "hi": hi, "small_since": None, "changed_at": now}; return lo, hi
    held_lo, held_hi = slot["lo"], slot["hi"]
    dwell_ok = (now - slot.get("changed_at", -1e18)) >= RANGE_DWELL_S
    grew = lo < held_lo or hi > held_hi
    if grew and (d_lo < held_lo or d_hi > held_hi or dwell_ok):         # need grew: widen (at once when the DATA left the range, else after the dwell)
        new_lo, new_hi = min(lo, held_lo), max(hi, held_hi)
        if (new_lo, new_hi) != (held_lo, held_hi):
            slot.update(lo=new_lo, hi=new_hi, small_since=None, changed_at=now)
        return slot["lo"], slot["hi"]
    if not grew and (hi - lo) < RANGE_SHRINK_FRAC * (held_hi - held_lo):   # need is much smaller: shrink after a quiet period (and the dwell)
        if slot["small_since"] is None:
            slot["small_since"] = now
        elif now - slot["small_since"] >= RANGE_SHRINK_S and dwell_ok:
            slot.update(lo=lo, hi=hi, small_since=None, changed_at=now); return lo, hi
    elif not grew:
        slot["small_since"] = None
    return held_lo, held_hi


def card_shape_style(tone: str) -> dict:
    return {"fillcolor": TONE_FILL[tone], "line": dict(color=TONE_LINE[tone][0], width=TONE_LINE[tone][1])}


def error_fig(A: dict, P: dict, window_s: float = 120.0) -> go.Figure:
    t_now = float(A["t_now"])
    t_lo = t_now - float(window_s)
    tracks = [tr for tr in A.get("err_tracks", []) if len(tr["errors"]["t"]) or len(tr.get("ungraded", {}).get("t", ()))]
    obs = A.get("obs_err") if P.get("show_obs", True) else None
    contain = A.get("contain") or {}
    height = int(P.get("err_height", 420))
    lw = float(P.get("line_w", LINE_W))
    compact = height < COMPACT_ERR_PX                 # short cards (laptop / stacked mode): 12 px readouts
    readout_px = px(READOUT_COMPACT_PX if compact else READOUT_PX, P)
    title_px, sub_px = px(TITLE_PX, P), px(READOUT_SUB_PX, P)
    legend = len(tracks) >= 2
    # each card = a HEADER STRIP (title left · "now ±σ" and the containment line right) ABOVE its plot area, so no text ever sits over the
    # data — least of all over the newest samples at the right edge (t = now).  The strip is part of the card rect (card_rect_top).
    hdr_i, line1 = err_strip_px(readout_px, sub_px)
    hdr = float(hdr_i)
    font_px = int(P.get("font_px", FONT_PX))
    # bottom margin = the x tick labels (bottom row only) at this font; x automargin is OFF so the row geometry (pixels) is exact — plotly's
    # automargin grew the bottom margin for the labels and squeezed the row domains, i.e. the strips and gaps (see liveserver errRows)
    # FIXED left / right / bottom margins (card_margin: from the font only).  The top = strip + pad + the legend row IFF >= 2 tracks: a second
    # track entering / leaving the window (every handover) adds / drops that row and slides the four cards by ~43 px once (measured
    # 2026-09-15: _size.t 55 <-> 103) — reserving it always would cost the stacked laptop cards ~11 px each (liveserver's one_err_px budgets no row)
    margin = dict(card_margin(font_px, float(P.get("text_scale", 1.0) or 1.0)), t=int(round(hdr + CARD_PAD_PX + (legend_row_px(font_px) + LEGEND_EXPAND_PAD if legend else 0))))
    plot_h = max(1.0, height - margin["t"] - margin["b"])
    row_h = PANEL_FIT_SLACK * max(1.0, (plot_h - 3.0 * (PANEL_GAP_PX + hdr)) / 4.0)   # ONE card's plot area (the panel re-fits with the same formula): how many y tick labels fit
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=(PANEL_GAP_PX + hdr) / plot_h)   # gap + the next card's header strip
    mag_all = {k: [] for k in ERR_KEYS}                # |err| + 1σ per graded sample (|err| alone without σ): the robust range statistic
    sig_all = {k: [] for k in ERR_KEYS}
    tones: dict[str, str] = {}
    labels: list[tuple[float, str, str]] = []          # visible top labels of the FIRST card: CPA + target handovers (top_labels)
    yrng: dict[str, tuple[float, float]] = {}
    strips: list[tuple[int, str, str, dict]] = []      # (row, key, name, strip trace kwargs) — added once the card's y-range is known
    for r, key in enumerate(ERR_KEYS, start=1):
        n_before = len(fig.data)
        # raw obs first (underneath): obs - target truth, ✕ in secondary ink, no hover (az / el / alt only)
        if obs is not None and key in OBS_KEYS and len(obs["t"]):
            om = obs["t"] >= t_lo
            if om.any():
                fig.add_trace(go.Scatter(x=D.to_pdt_dt64(obs["t"][om]), y=obs[key][om], mode="markers", name="raw obs vs target truth", legendgroup="obs",
                                         showlegend=False, marker=dict(symbol="x-thin", size=5, line=dict(width=1, color=T.OBS)), opacity=T.OBS_ALPHA,
                                         hoverinfo="skip"), row=r, col=1)
        for i, tr in enumerate(tracks):
            e, col, name = tr["errors"], TRACK_COLORS[min(i, len(TRACK_COLORS) - 1)], f"track #{tr['tid']}"
            m = e["t"] >= t_lo
            t, y, sg = e["t"][m], np.asarray(e[key], float)[m], np.asarray(e[f"sig_{key}"], float)[m]
            ok = np.isfinite(y) & np.isfinite(sg)
            if ok.any():
                mag_all[key].append(np.abs(y[ok]) + sg[ok])
                sig_all[key].append(sg[ok])
            fy = np.isfinite(y) & ~ok
            if fy.any():
                mag_all[key].append(np.abs(y[fy]))
            # the published states spa did NOT grade (tentative / coasting / extrapolated): they BRIDGE the holes in the graded line
            # (same 2.5 px line, lighter) instead of breaking it; only a hole in the published states > BRIDGE_S is a real break
            u = tr.get("ungraded") or {}
            ut = np.asarray(u.get("t", ()), float)
            um = ut >= t_lo
            tu, yu = ut[um], (np.asarray(u[key], float)[um] if len(ut) else np.zeros(0))
            ku = (np.asarray(u.get("kind", ()), object)[um] if len(u.get("kind", ())) == len(ut) else np.full(int(um.sum()), "coast", object))
            dt = np.diff(t) if len(t) > 1 else np.zeros(0)
            med = float(np.median(dt)) if len(dt) else 1.0
            gap = max(BRIDGE_S, GAP_FACTOR * med)
            ta, ya, sa, ga, ui, brk = merged_series(t, y, sg, tu, yu, gap)
            if not len(ta):
                continue
            xa = _x_gaps(np.where(brk, np.nan, ta))               # None (a break) INSTEAD of the first sample after a dropout ...
            xa_full = _x_gaps(ta)
            xs_line, ys_line, ys_solid, cd = [], [], [], []
            for j in range(len(ta)):                               # ... so both the sample and the break survive: [..., prev, None, next, ...]
                if brk[j]:
                    xs_line.append(None); ys_line.append(np.nan); ys_solid.append(np.nan); cd.append(["", ""])
                xs_line.append(xa_full[j]); ys_line.append(ya[j]); ys_solid.append(ya[j] if ga[j] else np.nan)
                sig_txt = (ERR_SIG_FMT[key].replace("%{customdata", "{").replace("}", "}").format(sa[j]) if np.isfinite(sa[j]) else "n/a")
                kind = "" if ga[j] else (" · tentative" if (ui[j] >= 0 and str(ku[ui[j]]) == "tent") else " · coasting")
                cd.append([sig_txt, kind])
            # ±1σ band: one polygon per run between real breaks, σ interpolated across the bridged holes (continuous like the line)
            for k in BANDS_DRAWN:
                for i0, i1 in _runs_of(np.isfinite(sa), brk):
                    sl = slice(i0, i1 + 1)
                    if i1 - i0 + 1 < 2:
                        continue
                    xs = xa_full[sl]
                    if key in ONE_SIDED:   # magnitude: the band is 0 .. +kσ (the 1σ radius), not ±
                        up, lo = k * sa[sl], np.zeros(i1 - i0 + 1)
                    else:
                        up, lo = ya[sl] + k * sa[sl], ya[sl] - k * sa[sl]
                    fig.add_trace(go.Scatter(x=xs + xs[::-1], y=np.concatenate([up, lo[::-1]]), mode="lines", fill="toself",
                                             fillcolor=rgba(col, BAND_ALPHA[k]), line=dict(width=1, color=rgba(col, BAND_EDGE_ALPHA)), hoverinfo="skip",
                                             showlegend=False, legendgroup=name, name=f"{name} ±{k}σ"), row=r, col=1)
            # the CONTINUOUS error line (lighter: the bridged coasting / tentative samples show through) ...
            fig.add_trace(go.Scatter(x=xs_line, y=ys_line, mode="lines", name=name, legendgroup=name, showlegend=(r == 1),
                                     line=dict(color=rgba(col, LIGHT_ALPHA), width=lw), connectgaps=False, customdata=cd,
                                     hovertemplate=ERR_HOVER[key] + " · σ %{customdata[0]}%{customdata[1]}"), row=r, col=1)
            # ... and the GRADED samples on top in the full colour (NaN wherever a sample was not graded -> those segments stay lighter)
            fig.add_trace(go.Scatter(x=xs_line, y=ys_solid, mode="lines", name=f"{name} matched", legendgroup=name, showlegend=False,
                                     line=dict(color=col, width=lw), connectgaps=False, hoverinfo="skip"), row=r, col=1)
            # coast indicator strip: one segment per run of ungraded states (bounded by its graded neighbours when they exist)
            segs = []
            for i0, i1 in _runs_of(~ga, brk):
                t0 = ta[i0 - 1] if (i0 > 0 and not brk[i0]) else ta[i0] - 0.5 * med
                t1 = ta[i1 + 1] if (i1 + 1 < len(ta) and not brk[i1 + 1]) else ta[i1] + 0.5 * med
                kinds = [str(ku[ui[j]]) for j in range(i0, i1 + 1) if ui[j] >= 0]
                word = "tentative" if kinds and kinds.count("tent") > len(kinds) / 2 else "coasting"
                segs.append((float(t0), float(t1), f"{word} {max(t1 - t0, 0.0):.1f} s"))
            if segs:
                strips.append((r, key, name, {"segs": segs}))
        if len(fig.data) == n_before:
            # EMPTY ROW: plotly.js only instantiates the axes of subplot rows that carry a trace — without one the card
            # would render as a blank box (no axes, no annotations).  An invisible anchor point keeps the row alive.
            fig.add_trace(go.Scatter(x=[_dt(t_now)], y=[0.0], mode="markers", marker=dict(size=1, opacity=0, color=T.CARD), hoverinfo="skip",
                                     showlegend=False, name=f"_anchor_{key}"), row=r, col=1)
        # the card's y-range (robust: nice(1.25 · P95(|err| + 1σ)) floored, CLAMPED to ERR_CLAMP, jitter-held) is known now: place this
        # card's coast strips just above its floor
        yrng[key] = err_range(key, mag_all[key], sig_all[key], P.get("yrng_mem"))
        y_strip = yrng[key][0] + STRIP_FRAC * (yrng[key][1] - yrng[key][0])
        for (rr, kk, name, st_) in [x for x in strips if x[0] == r]:
            xs, ys, cds = [], [], []
            for (t0, t1, label) in st_["segs"]:
                xs += [_dt(t0), _dt(t1), None]; ys += [y_strip, y_strip, None]; cds += [label, label, ""]
            fig.add_trace(go.Scatter(x=xs[:-1], y=ys[:-1], mode="lines", name=f"coast strip {name[6:]}", legendgroup=name, showlegend=False,
                                     line=dict(color=T.INK3, width=STRIP_W), opacity=0.9, connectgaps=False, customdata=cds[:-1],
                                     hovertemplate="%{customdata}<extra></extra>"), row=r, col=1)
        # HEADER STRIP (above the plot area, yanchor "bottom" at y domain 1.0): title left (bold mono) on line 1 · takeaway readout
        # right on line 1 (current error ± 1σ, primary ink) · containment rates + n right on line 2 (secondary ink) — all ink
        primary = tracks[0]["errors"] if tracks else {}
        _e_now, _s_now = _now_pair(primary, key, t_now, t_lo)
        tone, words = card_tone(_e_now, _s_now, contain.get(key))
        tones[key] = tone
        fig.add_annotation(xref="x domain", yref="y domain", x=0.0, y=1.0, xanchor="left", yanchor="bottom", xshift=2, yshift=line1,
                           text=f"<b>{ERR_TITLE[key]}</b> ({ERR_UNIT[key].strip()})" + (f"  <b>▲ {words}</b>" if words else ""), showarrow=False,
                           font=dict(family=T.MONO, size=title_px, color=T.INK),   # PRIMARY ink, bold: "still very hard to make out what each one is"; the amber lives on the card frame
                           bgcolor=TAG_BG, borderpad=1, name=f"title_{key}", row=r, col=1)
        txt = readout(primary, key, t_now, t_lo)
        empty = txt == "no sample"
        empty_txt = NO_TGT_READOUT if no_tgt_truth(A) else EMPTY_READOUT      # nothing to grade AGAINST vs nothing graded YET
        # EMPTY STATE: the card still carries its title and a muted "no graded samples yet" readout (never a blank box)
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=line1,
                           text=empty_txt if empty else txt, showarrow=False,
                           font=dict(family=T.MONO, size=readout_px, color=T.INK3 if empty else T.INK),
                           bgcolor=TAG_BG, borderpad=1, name=f"readout_{key}", row=r, col=1)
        c = contain.get(key)
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=1,
                           text=contain_text(c), showarrow=False, hovertext=(f"n {c['n']} samples in the window · spa rule |err| ≤ kσ" if c and c["n"] else None),
                           font=dict(family=T.MONO, size=sub_px, color=T.INK2), bgcolor=TAG_BG, borderpad=1, name=f"contain_{key}", row=r, col=1)
    # VERTICAL ELEMENTS — every one labelled (first card) or self-evident: (1) target track-id changes (handover / steal / loss) inside
    # the window: a secondary-ink tick at the top edge of every card + "→ #177 · 07:21:57" in the first card; (2) the validated CPA: a gold
    # hairline on every card + "CPA 59 m · 07:22:31" in the first card; (3) the coast strip (horizontal, hover) and the faint solid x grid.
    # NO "now" line: the right edge of the time axis is now (X_PAD_FRAC right padding keeps the newest sample off the frame).
    # 2026-09-14 user: "I don't need track number labels and CPA on EVERY plot" / "the numbers must not overlap the plot" -> the target
    # handover tick lives on the FIRST card only and its label(s) sit in that card's header strip (second row, left — never over the data);
    # the CPA (gold ★) is on the map and the separation card only.
    handover_marks(fig, A.get("track_events"), t_lo, t_now, [(1, 1)], {(1, 1)}, P, roles=("target",), labels_out=labels)
    cpa_marks(fig, A, t_lo, t_now, rows=4, labels_out=labels)                                   # 2026-09-15: "show CPA lines on the plots"
    strip_labels(fig, labels, P, row=1, col=1)
    base_layout(fig, height=height, uirevision=UIREV["err"], legend=legend, time_x=True, unified=True, margin=margin, font_px=int(P.get("font_px", FONT_PX)), fixed=True)
    # the four cards: page-surface paper, one card-coloured rect with a 1 px rule border per panel (paper coords: from the top of the
    # header strip to a few px under the plot area), the plot areas themselves transparent so the border reads on all sides
    fig.update_layout(paper_bgcolor=T.SURFACE, plot_bgcolor="rgba(0,0,0,0)")
    pad = CARD_PAD_PX / plot_h
    top_pad = (hdr + CARD_PAD_PX) / plot_h
    for r in range(1, 5):
        d0, d1 = fig.layout["yaxis" if r == 1 else f"yaxis{r}"].domain
        fig.add_shape(type="rect", xref="paper", yref="paper", x0=-CARD_PAD_X, x1=1 + CARD_PAD_X, y0=d0 - pad, y1=d1 + top_pad,
                      layer="below", name=f"card_{ERR_KEYS[r - 1]}", **card_shape_style(tones.get(ERR_KEYS[r - 1], "ok")))
        fig.add_shape(type="rect", xref="paper", yref="paper", x0=-CARD_PAD_X, x1=1 + CARD_PAD_X, y0=d1, y1=d1 + top_pad, layer="below",
                      fillcolor=HDR_BAND, line=dict(width=0), name=f"hdr_{ERR_KEYS[r - 1]}")   # 2026-09-15: shaded header band ("section off the headers more")
    if legend:   # the legend sits above the first header strip (base_layout puts it at paper y = 1, i.e. INSIDE the strip)
        fig.update_layout(legend=dict(y=1.0 + top_pad, yanchor="bottom"))
    # the panel JS re-heights this figure to the browser's column (fitLayout / applyGeometry): the strips and gaps are PIXELS, so it
    # re-derives the row domains, card rects and legend for its own height from this meta (ih.liveserver errRows) — never a squashed strip
    fig.update_layout(meta={"err": {"rows": 4, "hdr": float(hdr), "gap": float(PANEL_GAP_PX), "pad": float(CARD_PAD_PX), "keys": list(ERR_KEYS)}})
    for r, key in enumerate(ERR_KEYS, start=1):   # after base_layout: the card's robust, clamped range (yrng), explicit ticks, muted zero
        lo_, top = yrng.get(key, (-ERR_FLOOR[key], ERR_FLOOR[key]) if key not in ONE_SIDED else (0.0, ERR_FLOOR[key]))
        if key in ONE_SIDED:   # 0 .. top (the zero line IS the axis floor); ticks at top/2 and top
            fig.update_yaxes(title_text=ERR_AXIS[key], range=[0, top], tickvals=thin_ticks([0.0, top / 2.0, top], 0.0, top, row_h, font_px),
                             zerolinecolor=T.ZERO, zerolinewidth=1, row=r, col=1)
        else:
            fig.update_yaxes(title_text=ERR_AXIS[key], range=[-top, top], tickvals=thin_ticks(err_ticks(top), -top, top, row_h, font_px),
                             zerolinecolor=T.ZERO, zerolinewidth=1, row=r, col=1)
    x_hi = _dt(t_now + X_PAD_FRAC * float(window_s))   # 2 % right padding: the newest sample is never glued to the frame edge
    for r in range(1, 5):   # explicit x range on EVERY row: a trace-less card keeps its box, ticks and tags (no autorange collapse)
        fig.update_xaxes(range=[_dt(t_lo), x_hi], automargin=False, dtick=x_dtick_ms(window_s), row=r, col=1)   # no x automargin (the bottom margin is card_margin's); FIXED dtick: the labels slide, never re-space
    # no x-axis title: the tick labels are HH:MM:SS and the separation card right above carries "time (PDT)" — the room goes to the plot areas
    return fig


# ── VELOCITY STATES · the track's FILTERED velocity vs the MAVLink truth, per ENU axis (chaos-spa graded) ──
# The three cards show the STATES (truth line in the role colour, ink track line) — the numbers on them (Δ = track − truth,
# σ, containment) are spa's per-update velocity grading (corr_df e_dot / n_dot / u_dot errors and sigmas, ih.engine.track_errors keys
# ve / vn / vu), never our own subtraction.  ±1σ band around the track line only where the source carried a velocity covariance
# (live block 143; the 8/28 archives were dumped without one -> no band, "σ n/a").  Same card chrome as error_fig: header strip
# above the plot area, coasting / tentative samples lighter + a coast strip, labelled gold CPA hairline + handover ticks, 2 % right pad,
# no "now" line.  TRUTH READS AS TRUTH AT A GLANCE: 3 px in the target's red with a "truth" end label at its newest sample; the track's
# filtered state 2.5 px primary ink with a "track" end label (ink text on TAG_BG, right-anchored at the newest sample; when the two end
# values are close the labels sit on opposite sides of their lines; an off-scale end pins its label at the axis edge).
VEL_KEYS = ("vn", "ve", "vu")                                    # card order: North, East, vertical (up positive)
VEL_TITLE = {"vn": "NORTH VELOCITY", "ve": "EAST VELOCITY", "vu": "VERTICAL VELOCITY"}
VEL_AXIS = {"vn": "vN m/s", "ve": "vE m/s", "vu": "vU m/s"}
VEL_TK = {"vn": 11, "ve": 10, "vu": 12}                          # TK-layout column of the track's filtered velocity component
VEL_TR = {"vn": 5, "ve": 4, "vu": 6}                             # TR-layout column of the truth velocity component
VEL_FLOOR = 2.0                                                  # smallest half-span of a velocity card (m/s)
VEL_CLAMP = 30.0                                                 # hard ± axis limit of every velocity card (N / E / vertical): an unphysical state pins at the edge
VEL_PAD = 0.15                                                   # the truth + track span padded this fraction each way
VEL_TRUTH_W, VEL_TRACK_W = 3.0, 2.5                              # truth 3 px (the thick line IS the truth), track 2.5 px
VEL_UNPHYSICAL_MPS, VEL_WARN_MPS = 20.0, 5.0                    # |Δ| beyond these: fail (a drone cannot) / warn when no σ is available to judge
VEL_TRUTH_COLOR = T.TARGET                                       # truth = the target's role colour (measurement-space convention: thick solid)
VEL_TRACK_COLOR = T.INK                                          # the filtered state = ink line: never the truth's colour, so the two never blur
VEL_TRACK_W_DELTA = 0.0                                          # (legacy) the track is VEL_TRACK_W; colour + width + legend carry the identity
VEL_STACKED_PX = 353                                             # the stacked velocity column's height AT TEXT SCALE 1 (liveserver ONE_VEL_PX): the shortest three-row arrangement the panel draws — the y tick budget assumes it
VEL_SHORT_MAX_PX = 12                                            # the SIDE-BY-SIDE line 2 ("trk … · tru … · Δ …", 30 glyphs: 233 px at 12, 270 at 14) never grows past this: a 3-across velocity card is ~246 px wide at 1920x1080 (two-column, card_margin l 83 / r 40) — 2026-09-15: the user wants it larger; the numbers are bold now, a bigger font needs a wider card (see the report)
VEL_END_LABELS = False                                           # "truth"/"track" words at the line ends: OFF — the clip audit (2026-09-11) found them colliding with the header readout / handover label and leaving the card at X-Large


def vel_stacked_px(P: dict | None = None) -> int:
    """The shortest stacked three-row velocity column the panel draws AT THIS TEXT SCALE — ih.liveserver.one_vel_px
    (its header strips scale with the sidebar "Text size", so the column does too: 353 / 371 / 395 px).  The y tick
    budget below takes this worst case, so it must be the number liveserver really uses; ih.liveserver is imported
    lazily (ih.plots must stay importable on its own) and VEL_STACKED_PX is the fallback."""
    try:
        from . import liveserver as _LS
        sc = float((P or {}).get("text_scale", 1.0) or 1.0)
        return int(_LS.one_vel_px(sc))
    except Exception:
        return VEL_STACKED_PX


def vel_ticks(lo: float, hi: float) -> list[float]:
    """3–4 'nice' ticks inside [lo, hi] (velocities are not centred on zero)."""
    span = max(hi - lo, 1e-9)
    step = nice_ceil(span / 3.0)
    t0 = np.ceil(lo / step) * step
    vals = [float(v) for v in np.arange(t0, hi + step * 0.01, step)]
    return vals if len(vals) >= 2 else [lo, hi]


def vel_readout(tk_now: float | None, tr_now: float | None, d_now: float | None = None) -> str:
    """Line 1 of a velocity card's header — the TAKEAWAY at the error cards' primary readout size, numbers bold:
    'track <b>−11.2</b> · truth <b>−12.0</b> · Δ <b>+0.8</b> m/s' (Δ = spa's track − truth when graded, else the plain difference;
    2026-09-15 user: "make the metrics here larger too")."""
    f = lambda v: f"<b>{v:+.1f}</b>" if v is not None and np.isfinite(v) else "—"   # noqa: E731
    if d_now is None and tk_now is not None and tr_now is not None and np.isfinite(tk_now) and np.isfinite(tr_now):
        d_now = float(tk_now) - float(tr_now)
    return f"track {f(tk_now)} · truth {f(tr_now)} · Δ {f(d_now)} m/s"


VEL_SHORT_TITLE = {"vn": "N", "ve": "E", "vu": "VERT"}   # side-by-side card titles (the column header says "velocity")


def vel_compact(tk_now: float | None, tr_now: float | None, d_now: float | None) -> str:
    """Side-by-side velocity card, ONE line, 21 glyphs: '<b>−39.4</b> · <b style=red>−0.4</b> · Δ <b>−38.6</b>' — track in ink, truth in the
    target red (the same colours as the two lines on the plot), no words so the panel JS can size it up to the card width."""
    f = lambda v: f"{v:+.1f}" if v is not None and np.isfinite(v) else "—"   # noqa: E731
    if (d_now is None or not np.isfinite(d_now)) and tk_now is not None and tr_now is not None and np.isfinite(tk_now) and np.isfinite(tr_now):
        d_now = float(tk_now) - float(tr_now)                          # no spa-graded Δ for this axis yet: track − truth (same fallback as vel_readout)
    return f"{f(tk_now)} · {f(tr_now)} · Δ {f(d_now)} m/s"


def vel_readout_short(tk_now: float | None, tr_now: float | None, d_now: float | None = None) -> str:
    """The SIDE-BY-SIDE (two-column strip, ~250-310 px wide cards) line: 'trk <b>−11.2</b> · tru <b>−12.0</b> · Δ <b>+0.8</b>' — the same
    numbers, bold, at min(READOUT_PX x scale, VEL_SHORT_MAX_PX); the unit is dropped for the width (28 glyphs x 8.75 px = 245 px at 14 px)."""
    f = lambda v: f"<b>{v:+.1f}</b>" if v is not None and np.isfinite(v) else "—"   # noqa: E731
    if d_now is None and tk_now is not None and tr_now is not None and np.isfinite(tk_now) and np.isfinite(tr_now):
        d_now = float(tk_now) - float(tr_now)
    return f"trk {f(tk_now)} · tru {f(tr_now)} · Δ {f(d_now)}"


def vel_delta_text(e: dict, key: str, t_now: float, t_lo: float, c: dict | None) -> str:
    """Line 2: spa's grading — 'Δ +0.8 · σ 0.9 m/s · 1σ 67% · n 118' (σ n/a and no rates when the source had no velocity covariance)."""
    t = np.asarray(e.get("t", ()), float)
    y = np.asarray(e.get(key, ()), float)
    sg = np.asarray(e.get(f"sig_{key}", ()), float)
    if not (len(t) and len(y) == len(t)):
        return "no samples"
    if len(sg) != len(t):
        sg = np.full(len(t), np.nan)
    ok = np.flatnonzero((t >= t_lo) & np.isfinite(y))
    if not len(ok):
        return "no samples"
    i = int(ok[-1])
    d = f"Δ {y[i]:+.1f}"
    sig = f" · σ {sg[i]:.1f} m/s" if np.isfinite(sg[i]) and sg[i] > 0 else " m/s · σ n/a"
    rates = f" · 1σ {c['p1']:.0f}% · n {c['n']}" if c and c.get("n") else ""
    age = float(t_now) - float(t[i])
    lead = "" if age <= NOW_MAX_AGE_S else f"last {age:.0f} s ago · "
    return lead + d + sig + rates


def velocity_fig(A: dict, P: dict, window_s: float = 120.0, truth: np.ndarray | None = None) -> go.Figure:
    """Three stacked cards (N · E · vertical): truth velocity (role colour) vs the target track's filtered velocity (ink),
    ±1σ band around the track where spa had a real velocity σ, coasting / tentative samples lighter, spa's Δ / σ / containment in the
    header strips.  ``truth`` = the target truth window (TR layout) — the Live page passes the snapshot's; None -> track line only.
    The panel JS re-arranges the three rows (stacked in the ultrawide middle column and the stacked mode, side by side under the map
    in the two-column mode) from layout.meta.vel, exactly like errRows for the error panel."""
    t_now = float(A["t_now"])
    t_lo = t_now - float(window_s)
    tk = A.get("tgt_track")
    tid = A.get("tgt_tid")
    err_entry = next((tr for tr in A.get("err_tracks", []) if tr.get("tid") == tid), None) if tid is not None else None
    e = (err_entry or {}).get("errors") or A.get("errors") or {}
    u = (err_entry or {}).get("ungraded") or A.get("errors_ung") or {}
    contain = A.get("contain") or {}
    height = int(P.get("vel_height", 420))
    lw = float(P.get("line_w", LINE_W))
    compact = False                                   # the strips are re-fitted by the panel JS from meta.vel.hdr; the 14 px readout strip is the one spec everywhere
    readout_px = px(READOUT_COMPACT_PX if compact else READOUT_PX, P)
    title_px, sub_px = px(TITLE_PX, P), px(READOUT_SUB_PX, P)
    hdr_i, line1 = err_strip_px(readout_px, sub_px)
    hdr = float(hdr_i)
    font_px = int(P.get("font_px", FONT_PX))
    has_truth = truth is not None and len(truth) > 0
    vel_empty = NO_TGT_READOUT if no_tgt_truth(A) else "no track · no truth"   # interceptor-only truth: the cards say WHY they are empty
    legend = has_truth and tk is not None and len(tk) > 0            # two series (truth + track) -> legend; otherwise a single series
    margin = dict(card_margin(font_px, float(P.get("text_scale", 1.0) or 1.0)), t=int(round(hdr + CARD_PAD_PX + (legend_row_px(font_px) + LEGEND_EXPAND_PAD if legend else 0))))   # FIXED l / r / b (see error_fig)
    # the server picture is built at least tall enough for three legible cards; the panel JS re-fits the rows to the real column height
    build_h = max(height, int(3 * (hdr + 68) + 2 * (PANEL_GAP_PX + hdr) + margin["t"] + margin["b"]))
    plot_h = max(1.0, build_h - margin["t"] - margin["b"])
    # ONE card's plot area for the tick-label budget (thin_ticks).  The SERVER cannot know which of the two arrangements the
    # panel will draw this figure in — stacked (one-column / ultrawide) or side by side (the two-column strip) — nor its real
    # height, and the same figure is re-fitted into both; so the budget takes the WORST case: three stacked rows in a column
    # no taller than vel_stacked_px(P) — liveserver's stacked floor AT THIS TEXT SCALE (2026-09-15: it scales with the
    # strips now, so the budget follows instead of assuming the Normal 353).  (A side-by-side card is 2-3x taller and
    # would carry one more label: see the report — the honest fix is the panel re-deriving the ticks.)
    tick_h = max(float(height), vel_stacked_px(P)) - margin["t"] - margin["b"]
    row_h = PANEL_FIT_SLACK * max(1.0, (tick_h - 2.0 * (PANEL_GAP_PX + hdr)) / 3.0)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=(PANEL_GAP_PX + hdr) / plot_h)
    rows = len(VEL_KEYS)
    vtones: dict[str, str] = {}
    name_tk = f"track #{tid} filtered state" if tid is not None else "track filtered state"
    yrng: dict[str, tuple[float, float]] = {}
    labels: list[tuple[float, str, str]] = []          # visible top labels of the FIRST card (CPA + target handovers)
    # the track's samples in the window, their kind (meas = graded-eligible / coast / tent) and spa's σ aligned by time
    tkw = tk[(tk[:, 0] >= t_lo) & (tk[:, 0] <= t_now)] if tk is not None and len(tk) else np.zeros((0, 13))
    et = np.asarray(e.get("t", ()), float)
    graded_t = set(np.round(et, 3).tolist()) if len(et) else set()
    for r, key in enumerate(VEL_KEYS, start=1):
        n_before = len(fig.data)
        lo_v, hi_v = [], []
        ends: dict[str, tuple[float, float]] = {}      # "truth" / "track" -> (t, y) of the newest finite sample: the end labels
        if has_truth:
            tw = truth[(truth[:, 0] >= t_lo) & (truth[:, 0] <= t_now)]
            if len(tw):
                tt, ty = tw[:, 0], tw[:, VEL_TR[key]]
                fin = np.isfinite(ty)
                if fin.any():
                    lo_v.append(float(np.nanmin(ty))); hi_v.append(float(np.nanmax(ty)))
                    ends["truth"] = (float(tt[fin][-1]), float(ty[fin][-1]))
                    xs = _x_gaps(tt)
                    fig.add_trace(go.Scatter(x=xs, y=np.where(fin, ty, np.nan), mode="lines", name="MAVLink truth", legendgroup="truth", showlegend=(r == 1),
                                             line=dict(color=VEL_TRUTH_COLOR, width=VEL_TRUTH_W), connectgaps=False, hovertemplate="truth %{y:+.1f} m/s"), row=r, col=1)
        if len(tkw):
            t, y = tkw[:, 0], tkw[:, VEL_TK[key]]
            kind = np.array(["tent" if st_ == 1 else ("meas" if (tt - lu) < D.MEAS_LAG_S else "coast") for tt, lu, st_ in zip(t, tkw[:, 7], tkw[:, 9])], object)
            fin = np.isfinite(y)
            if fin.any():
                lo_v.append(float(np.nanmin(y))); hi_v.append(float(np.nanmax(y)))
                ends["track"] = (float(t[fin][-1]), float(y[fin][-1]))
            # spa's σ for this component at the graded sample times (NaN elsewhere / when the source had no velocity covariance)
            sg = np.full(len(t), np.nan)
            sig_e = np.asarray(e.get(f"sig_{key}", ()), float)
            if len(et) and len(sig_e) == len(et):
                idx = {round(float(a), 3): i for i, a in enumerate(et)}
                for j, tt in enumerate(t):
                    i = idx.get(round(float(tt), 3))
                    if i is not None:
                        sg[j] = sig_e[i]
            graded = np.array([round(float(tt), 3) in graded_t for tt in t]) if graded_t else (kind == "meas")
            dt = np.diff(t) if len(t) > 1 else np.zeros(0)
            med = float(np.median(dt)) if len(dt) else 1.0
            gap = max(BRIDGE_S, GAP_FACTOR * med)
            brk = np.concatenate([[False], dt > gap]) if len(t) > 1 else np.zeros(len(t), bool)
            xa = _x_gaps(t)
            xs_line, ys_line, ys_solid, cd = [], [], [], []
            for j in range(len(t)):
                if brk[j]:
                    xs_line.append(None); ys_line.append(np.nan); ys_solid.append(np.nan); cd.append(["", ""])
                xs_line.append(xa[j]); ys_line.append(y[j]); ys_solid.append(y[j] if graded[j] else np.nan)
                cd.append([f"{sg[j]:.1f} m/s" if np.isfinite(sg[j]) else "n/a", "" if graded[j] else (" · tentative" if kind[j] == "tent" else " · coasting")])
            # ±1σ band around the FILTERED STATE where spa had a real velocity σ (one polygon per run without breaks)
            okb = np.isfinite(sg) & np.isfinite(y)
            if okb.any():
                lo_v.append(float(np.nanmin((y - sg)[okb]))); hi_v.append(float(np.nanmax((y + sg)[okb])))
                for k in BANDS_DRAWN:
                    for i0, i1 in _runs_of(okb, brk):
                        if i1 - i0 + 1 < 2:
                            continue
                        sl = slice(i0, i1 + 1)
                        xs = xa[sl]
                        fig.add_trace(go.Scatter(x=xs + xs[::-1], y=np.concatenate([y[sl] + k * sg[sl], (y[sl] - k * sg[sl])[::-1]]), mode="lines", fill="toself",
                                                 fillcolor=rgba(VEL_TRACK_COLOR, BAND_ALPHA[k] * 0.6), line=dict(width=1, color=rgba(VEL_TRACK_COLOR, BAND_EDGE_ALPHA * 0.6)),
                                                 hoverinfo="skip", showlegend=False, legendgroup="track", name=f"{name_tk} ±{k}σ"), row=r, col=1)
            fig.add_trace(go.Scatter(x=xs_line, y=ys_line, mode="lines", name=name_tk, legendgroup="track", showlegend=(r == 1),
                                     line=dict(color=rgba(VEL_TRACK_COLOR, LIGHT_ALPHA), width=VEL_TRACK_W), connectgaps=False, customdata=cd,
                                     hovertemplate="track %{y:+.1f} m/s · σ %{customdata[0]}%{customdata[1]}"), row=r, col=1)
            fig.add_trace(go.Scatter(x=xs_line, y=ys_solid, mode="lines", name=f"{name_tk} matched", legendgroup="track", showlegend=False,
                                     line=dict(color=VEL_TRACK_COLOR, width=VEL_TRACK_W), connectgaps=False, hoverinfo="skip"), row=r, col=1)
            segs = []
            for i0, i1 in _runs_of(~graded, brk):
                t0 = t[i0 - 1] if (i0 > 0 and not brk[i0]) else t[i0] - 0.5 * med
                t1 = t[i1 + 1] if (i1 + 1 < len(t) and not brk[i1 + 1]) else t[i1] + 0.5 * med
                kinds = list(kind[i0:i1 + 1])
                word = "tentative" if kinds.count("tent") > len(kinds) / 2 else "coasting"
                segs.append((float(t0), float(t1), f"{word} {max(t1 - t0, 0.0):.1f} s"))
        else:
            segs = []
        if len(fig.data) == n_before:   # EMPTY ROW: keep the axes alive (plotly drops trace-less subplot rows)
            fig.add_trace(go.Scatter(x=[_dt(t_now)], y=[0.0], mode="markers", marker=dict(size=1, opacity=0, color=T.CARD), hoverinfo="skip",
                                     showlegend=False, name=f"_anchor_{key}"), row=r, col=1)
        # y-range: the truth + track span padded VEL_PAD, never narrower than ±VEL_FLOOR about its centre, clamped to ±VEL_CLAMP (before
        # and after the jitter hold): an unphysical state pins at the edge and reads as wrong
        yrng[key] = vel_range(lo_v, hi_v, P.get("yrng_mem"), key)
        y_strip = yrng[key][0] + STRIP_FRAC * (yrng[key][1] - yrng[key][0])
        # END LABELS "truth" / "track" at the newest sample of each line (ink on TAG_BG, right-anchored so they sit left of the right pad;
        # opposite sides of their lines so they never overlap; an off-scale end pins its label at the axis edge)
        if ends and VEL_END_LABELS:
            lo_r, hi_r = yrng[key]
            above = {"truth": True, "track": False}
            if "truth" in ends and "track" in ends and ends["truth"][1] < ends["track"][1]:
                above = {"truth": False, "track": True}
            for who, (te, ye) in ends.items():
                yl = min(max(ye, lo_r), hi_r)
                up = above[who] if lo_r < yl < hi_r else (yl <= lo_r)    # pinned at the floor -> label above it; at the ceiling -> below
                fig.add_annotation(xref="x", yref="y", x=_dt(te), y=yl, text=who, showarrow=False, xanchor="right", yanchor="bottom" if up else "top",
                                   xshift=-2, yshift=3 if up else -3, font=dict(family=T.MONO, size=px(TITLE_PX - LABEL_PX_DELTA, P), color=T.INK),
                                   bgcolor=TAG_BG, borderpad=1, name=f"endlbl_{who}_{key}", row=r, col=1)
        if segs:
            xs, ys, cds = [], [], []
            for (t0, t1, label) in segs:
                xs += [_dt(t0), _dt(t1), None]; ys += [y_strip, y_strip, None]; cds += [label, label, ""]
            fig.add_trace(go.Scatter(x=xs[:-1], y=ys[:-1], mode="lines", name="coast strip", legendgroup="track", showlegend=False,
                                     line=dict(color=T.INK3, width=STRIP_W), opacity=0.9, connectgaps=False, customdata=cds[:-1],
                                     hovertemplate="%{customdata}<extra></extra>"), row=r, col=1)
        # HEADER STRIP: title left · line 1 right = current state vs truth · line 2 right = spa's Δ / σ / containment
        _d_now, _s_now = _now_pair(e, key, t_now, t_lo)
        tone, words = card_tone(_d_now, _s_now, contain.get(key))
        if TONE_ENABLED and _d_now is not None and abs(_d_now) > VEL_UNPHYSICAL_MPS:
            tone, words = "fail", ("UNPHYSICAL Δ" + (" · " + words if words else ""))
        elif TONE_ENABLED and _d_now is not None and _s_now is None and abs(_d_now) > VEL_WARN_MPS and tone == "ok":
            tone, words = "warn", f"Δ {abs(_d_now):.0f} m/s"
        vtones[key] = tone
        fig.add_annotation(xref="x domain", yref="y domain", x=0.0, y=1.0, xanchor="left", yanchor="bottom", xshift=2, yshift=line1,
                           text=f"<b>{VEL_TITLE[key]}</b>" + (f"  <b>▲ {words}</b>" if words else ""), showarrow=False,
                           font=dict(family=T.MONO, size=title_px, color=T.INK),   # PRIMARY ink, bold (see error_fig)
                           bgcolor=TAG_BG, borderpad=1, name=f"title_{key}", row=r, col=1)
        tk_now = float(tkw[-1, VEL_TK[key]]) if len(tkw) and (t_now - tkw[-1, 0]) <= NOW_MAX_AGE_S else None
        tr_now = None
        if has_truth:
            tw2 = truth[(truth[:, 0] <= t_now) & (truth[:, 0] >= t_now - NOW_MAX_AGE_S)]
            if len(tw2) and np.isfinite(tw2[-1, VEL_TR[key]]):
                tr_now = float(tw2[-1, VEL_TR[key]])
        empty = tk_now is None and tr_now is None
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=line1,
                           text=vel_empty if empty else vel_readout(tk_now, tr_now, _d_now), showarrow=False,
                           font=dict(family=T.MONO, size=readout_px, color=T.INK3 if empty else T.INK), bgcolor=TAG_BG, borderpad=1, name=f"readout_{key}", row=r, col=1)
        c = contain.get(key)
        # SIDE-BY-SIDE cards (two-column strip, ~300 px wide): one short line 2 replaces the long readout + Δ line — the panel JS
        # (cardGrid) shows readout_<key>_s and hides readout_<key> / delta_<key> when the cards sit in columns, and back when stacked
        d_txt = vel_delta_text(e, key, t_now, t_lo, c)
        # ~250-310 px cards: "trk <b>+9.8</b> · tru <b>+13.0</b> · Δ <b>−2.9</b>" (no unit / σ / rates — the stacked layout carries them) at the
        # primary readout size capped at VEL_SHORT_MAX_PX (the line must stay inside the narrow card)
        # SIDE-BY-SIDE strip = ONE row (user 2026-09-15 "why are there still 2 rows?"): short title left, the numbers right, both on the top line
        fig.add_annotation(xref="x domain", yref="y domain", x=0.0, y=1.0, xanchor="left", yanchor="bottom", xshift=2, yshift=line1,
                           text=f"<b>{VEL_TITLE[key]}</b> (m/s)", showarrow=False, visible=False,                 # same look as the error-card titles, unit in the title
                           font=dict(family=T.MONO, size=title_px, color=T.INK), bgcolor=TAG_BG, borderpad=1, name=f"title_{key}_s", row=r, col=1)
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=1,
                           text=vel_empty if empty else vel_compact(tk_now, tr_now, _d_now), showarrow=False, visible=False,
                           font=dict(family=T.MONO, size=readout_px, color=T.INK3 if empty else T.INK), bgcolor=TAG_BG, borderpad=1, name=f"readout_{key}_s", row=r, col=1)   # 2026-09-15: ONE line; the panel JS (cardGrid) fits the size to the card width (cap 18)
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom", xshift=-2, yshift=1,
                           text=d_txt, showarrow=False,
                           hovertext=("spa velocity grading: Δ = track − truth · σ from the track covariance · |Δ| ≤ 1σ rate over the window"),
                           font=dict(family=T.MONO, size=sub_px, color=T.INK2), bgcolor=TAG_BG, borderpad=1, name=f"delta_{key}", row=r, col=1)
    # no vertical marks on the velocity cards (2026-09-14: handover ticks / CPA not on every plot — the azimuth card carries the handovers,
    # the map + separation card the CPA); no "now" line either: the right edge is now
    base_layout(fig, height=build_h, uirevision=UIREV["vel"], legend=legend, time_x=True, unified=True, margin=margin, font_px=font_px, fixed=True)
    fig.update_layout(paper_bgcolor=T.SURFACE, plot_bgcolor="rgba(0,0,0,0)")
    pad = CARD_PAD_PX / plot_h
    top_pad = (hdr + CARD_PAD_PX) / plot_h
    for r in range(1, rows + 1):
        d0, d1 = fig.layout["yaxis" if r == 1 else f"yaxis{r}"].domain
        fig.add_shape(type="rect", xref="paper", yref="paper", x0=-CARD_PAD_X, x1=1 + CARD_PAD_X, y0=d0 - pad, y1=d1 + top_pad,
                      layer="below", name=f"card_{VEL_KEYS[r - 1]}", **card_shape_style(vtones.get(VEL_KEYS[r - 1], "ok")))
        fig.add_shape(type="rect", xref="paper", yref="paper", x0=-CARD_PAD_X, x1=1 + CARD_PAD_X, y0=d1, y1=d1 + top_pad, layer="below",
                      fillcolor=HDR_BAND, line=dict(width=0), name=f"hdr_{VEL_KEYS[r - 1]}")
    if legend:
        fig.update_layout(legend=dict(y=1.0 + top_pad, yanchor="bottom"))
    fig.update_layout(meta={"vel": {"rows": rows, "hdr": float(hdr), "gap": float(PANEL_GAP_PX), "pad": float(CARD_PAD_PX), "keys": list(VEL_KEYS),
                                    "axis": dict(VEL_AXIS)}})   # axis titles: the panel JS drops them side by side and restores them stacked
    for r, key in enumerate(VEL_KEYS, start=1):
        lo, hi = yrng[key]
        fig.update_yaxes(title_text=VEL_AXIS[key], range=[lo, hi], tickvals=thin_ticks(vel_ticks(lo, hi), lo, hi, row_h, font_px),
                         zerolinecolor=T.ZERO, zerolinewidth=1, row=r, col=1)
    x_hi = _dt(t_now + X_PAD_FRAC * float(window_s))
    for r in range(1, rows + 1):
        fig.update_xaxes(range=[_dt(t_lo), x_hi], automargin=False, dtick=x_dtick_ms(window_s), row=r, col=1)   # FIXED dtick (see error_fig)
    cpa_marks(fig, A, t_lo, t_now, rows=rows)                                                  # gold CPA hairline on the velocity cards too
    return fig


# ── MEASUREMENT SPACE · truth, tracks & obs vs time (spa's track_charts quad, reskinned; BISTATIC range / rate) ──
MEAS_KEYS = ("rng", "rr", "az", "el")
MEAS_TITLE = {"rng": "BISTATIC RANGE (KM)", "rr": "BISTATIC RANGE RATE (M/S)", "az": "AZIMUTH (°)", "el": "ELEVATION (°)"}
MEAS_AXIS = {"rng": "bistatic range (km)", "rr": "bistatic range rate (m/s)", "az": "azimuth (°)", "el": "elevation (°)"}
MEAS_HOVER = {"rng": "%{y:.2f} km", "rr": "%{y:+.1f} m/s", "az": "%{y:.2f}°", "el": "%{y:.2f}°"}
MEAS_MIN_HALF = {"rng": 0.6, "rr": 20.0, "az": 2.0, "el": 2.0}       # truth-envelope axis: smallest ± half span per panel (km, m/s, °, °)
MEAS_PAD = 0.15                                                        # ... padded 15 % each way beyond the truth min / max
# COLOUR = ROLE, IDENTITY WITHIN A ROLE = LIGHTNESS STEP + DASH + ID PILL (dataviz validator: no set of >= 2 extra hues passes
# all-pairs beside the red / blue truths).  Ramps by order of appearance within a role: light / mid (= the truth hue) / dark;
# a 4th concurrent track of a role folds to grey.  Ordinal checks pass (ΔL >= .06; red light end >= 3:1 on the card).
RAMP_TGT = ("#f3a6a6", T.TARGET, T.TARGET_DARK)              # target-side tracks: light · mid · dark red
RAMP_ITC = ("#8fb8f0", T.INTERCEPTOR, "#2a6cc7")              # interceptor-side tracks: light · mid · dark blue (dark step 3.27:1 on the card; #1c5cab was 2.55:1)
RAMP_FOLD = "#8a8a8a"                                         # 4th+ concurrent track of a role
STEP_DASH = ("solid", "dash", "dot")                          # the second identity channel: dash by step (coasting = lighter, never a dash)
TRACK_SLOTS = RAMP_TGT                                        # legacy name (tests): the target ramp
TRACK_SLOT_FOLD = RAMP_FOLD
MEAS_H = 520
# ── the quad's PANEL GEOMETRY is never known exactly server-side (the iframe width, plotly's legend autoexpand and the
# panel JS font bump all move it), so every placement decision below is taken against measured BOUNDS instead of a point
# estimate: a bound can only be wrong in the direction that leaves MORE room, an estimate is wrong both ways (2026-09-14
# MEAS_PANEL_PX / MEAS_LEGEND_PUSH_PX: the declutter pushed "#129" 30–53 px OUT of its panel at every size).
# Measured 2026-09-15 (tests/clip_audit.py probe, 1366x768 / 1536x864 / 1920x1080 / 2560x1440 x Normal / Large / X-Large):
# one panel 125–158 px tall, 382–961 px wide; the whole plot area 283–358 px tall.
MEAS_PANEL_MIN_H = 112.0                                      # LOWER bound on one panel's plot height (measured min 125)
MEAS_PANEL_MIN_W = 340.0                                      # LOWER bound on one panel's plot width (measured min 382) — the narrowest panel clusters the most tags
MEAS_PLOT_MIN_H = 260.0                                       # LOWER bound on the whole plot area (measured min 283): the legend's standoff above the title strip
MEAS_TAG_GAP = 2.0                                            # clear px between two stacked tag boxes
MEAS_TAG_STAND = 4.0                                          # ... and between a tag box and its own marker
MEAS_TAG_ROWS = 3                                             # at most this many rows of push in one time cluster; a tag that still does not fit is DROPPED (its ★ / ✕ / ▶ marker and hover keep the id)
MEAS_DEP_MAX = 2                                              # "→ departed" / "→ interceptor (stolen)" tags kept in a panel's HEADER STRIP (newest first)
MEAS_MARGIN = dict(l=56, r=12, t=30, b=30)
MEAS_TOP_PX = 72                                              # top margin: the legend rows above the top-row subplot titles (titles sit ABOVE their plot areas, never over a track pill)
MEAS_UIREV = UIREV["meas"]
MEAS_OBS_PX, MEAS_OBS_COLOR, MEAS_OBS_ALPHA = 6, "#cfd6de", 0.75     # raw obs: filled 6 px light-ink circles at .75 with a 1 px surface ring, drawn FIRST
TRUTH_W = 3.0                                                 # both truths: 3 px (target red, interceptor blue)
ON_W, DEPARTED_W, DEPARTED_ALPHA = 2.0, 1.0, 0.35             # a track ON its side: full 2 px ramp colour; DEPARTED (dragged away): 1 px at .35
COAST_ALPHA = 0.6                                             # tentative / coasting samples of a track: same line, lighter (like the error panel)
MEAS_TITLE_PX, MEAS_LEGEND_PX = 12, 12                        # per-panel titles (bold) and the legend: 12 px
MEAS_GAP_S = 3.0                                              # a track's line breaks across a hole in its states longer than max(this, 3 x median dt)


def track_slot(i: int, role: str = "tgt") -> str:
    """Ramp colour for the i-th concurrent track of a role (0 light, 1 mid, 2 dark, then grey)."""
    ramp = RAMP_ITC if role == "itc" else RAMP_TGT
    return ramp[i] if i < len(ramp) else RAMP_FOLD


def track_style(step: int) -> tuple[str, str, str]:
    """(target-side colour, interceptor-side colour, dash) of a track at appearance step ``step`` within its role."""
    return track_slot(step, "tgt"), track_slot(step, "itc"), STEP_DASH[min(step, len(STEP_DASH) - 1)]


def _meas_val(series: dict, key: str) -> np.ndarray | None:
    v = series.get(key)
    if v is None:
        return None
    v = np.asarray(v, float)
    return v / 1000.0 if key == "rng" else v


def truth_envelope(series: list, key: str, pad: float = MEAS_PAD, min_half: float | None = None) -> tuple[float, float] | None:
    """The panel's y-range from the TRUTH series only: [min, max] over the window padded ``pad`` each way, never
    narrower than ± MEAS_MIN_HALF[key] about the centre.  None without a finite truth value (autorange)."""
    vals = [v for v in (_meas_val(t, key) for t in series if t is not None) if v is not None]
    if not vals:
        return None
    v = np.concatenate(vals)
    v = v[np.isfinite(v)]
    if not len(v):
        return None
    lo, hi = float(v.min()), float(v.max())
    span = hi - lo
    half = MEAS_MIN_HALF[key] if min_half is None else float(min_half)
    if span * (1.0 + 2.0 * pad) < 2.0 * half:
        c = 0.5 * (lo + hi)
        return (c - half, c + half)
    return (lo - pad * span, hi + pad * span)


def _cat_xy(t: np.ndarray, y: np.ndarray, mask: np.ndarray, gap_s: float) -> tuple[list, list]:
    """The (x, y) polyline of one CATEGORY of samples (mask) as NaN-separated runs: each run also carries the
    sample right after it so neighbouring categories join without a gap; a step > gap_s breaks the run."""
    t, y = np.asarray(t, float), np.asarray(y, float)
    ok = mask & np.isfinite(y) & np.isfinite(t)
    brk = np.zeros(len(t), bool)
    if len(t) > 1:
        brk[1:] = np.diff(t) > gap_s
    xs, ys = [], []
    for i0, i1 in _runs_of(ok, brk):
        j1 = i1 + 1 if (i1 + 1 < len(t) and not brk[i1 + 1] and np.isfinite(y[i1 + 1])) else i1
        seg = list(range(i0, j1 + 1))
        if len(seg) < 2:
            seg = [i0, i0]                      # a lone sample still gets a (zero-length) segment so hover finds it
        xs += [str(d) for d in D.to_pdt_dt64(t[seg])] + [None]
        ys += [float(v) for v in y[seg]] + [np.nan]
    return xs[:-1], ys[:-1]


def track_sides(tr: dict) -> tuple[np.ndarray, str]:
    """Per-sample SIDE of a measurement-space track ("tgt" | "itc" | "none") and its HOME side ("tgt" when it was
    ever target-side in the window, else "itc" / "free").  Uses the engine's role-independent on_tgt / on_itc
    arrays when present (a STOLEN track: target-side, then interceptor-side), else the role-dependent ``on``."""
    n = len(tr["t"])
    side = np.full(n, "none", object)
    on_t, on_i = tr.get("on_tgt"), tr.get("on_itc")
    if on_t is None and on_i is None:
        on = np.asarray(tr.get("on", np.ones(n, bool)), bool)
        role = tr.get("role", "target")
        if role == "interceptor":
            on_i = on
        elif role == "free":
            return np.full(n, "none", object), "free"
        else:
            on_t = on
    if on_t is not None:
        side[np.asarray(on_t, bool)] = "tgt"
    if on_i is not None:
        side[np.asarray(on_i, bool) & (side != "tgt")] = "itc"
    if tr.get("role") == "free":
        return np.full(n, "none", object), "free"
    home = "tgt" if (side == "tgt").any() else ("itc" if (side == "itc").any() else ("itc" if tr.get("role") == "interceptor" else "tgt"))
    return side, home


def tag_w(text: str, font_px: float, pad: float = 6.0) -> float:
    """Width in px of a mono tag box: its glyphs + the annotation's border padding."""
    return GLYPH_W * float(font_px) * len(re.sub(r"<[^>]+>", "", str(text))) + pad


def tag_h(font_px: float) -> float:
    """Height in px of a ONE-LINE annotation box (the error cards' box metric: ERR_BOX_LH x font + the border padding)."""
    return ERR_BOX_LH * float(font_px) + ERR_BOX_PAD


def _tag_rec(name: str, text: str, font_px: float, t: float, y: float, t_lo: float, span: float,
             rng: tuple | None, *, xshift: float = 0.0, right: bool = False, prio: int = 0,
             band: str | None = None, full_width: bool = False) -> dict:
    """One quad tag as a PANEL-FRACTION record for stack_annotations: ``xf`` / ``yf`` = its anchor point as a fraction
    of the panel's plot width / height (yf measured DOWN from the panel's top), ``w`` / ``h`` = its box in px.  The
    fractions are exactly what the server knows; the panel's real pixels only ever enter through the conservative
    bounds MEAS_PANEL_MIN_W / MEAS_PANEL_MIN_H.  ``band`` = "top" / "bottom" marks an EDGE-ANCHORED obstacle (the
    handover ▾, the off-scale ▲ / ▼, the geometry note): those sit a fixed number of pixels from that edge at any
    panel height, which is what makes a clearance decision against them exact rather than estimated.
    Without a known y range (an autoranged panel) the tag is pinned mid-panel: the declutter then only separates tags
    that are close in TIME, which is the case that collides."""
    xf = (float(t) - float(t_lo)) / max(1e-9, float(span))
    if rng is not None and float(rng[1]) > float(rng[0]):
        yf = (float(rng[1]) - float(y)) / (float(rng[1]) - float(rng[0]))
    else:
        yf = 0.5
    return {"name": name, "xf": min(max(xf, 0.0), 1.0), "yf": min(max(yf, 0.0), 1.0),
            "w": tag_w(text, font_px), "h": tag_h(font_px), "xshift": float(xshift), "right": bool(right),
            "prio": int(prio), "band": band, "full_width": bool(full_width), "movable": band is None}


def tag_side(xf: float, w: float, panel_w: float = MEAS_PANEL_MIN_W) -> bool:
    """True when a tag of width ``w`` at time fraction ``xf`` must anchor to the RIGHT of its marker to stay inside the
    panel.  Against the NARROWEST panel we support, so the flip happens early rather than one size too late (2026-09-14:
    a flat 0.85 threshold let "→ interceptor (stolen)" run 8–20 px out of the figure at 945 px)."""
    return float(xf) > 1.0 - min(0.6, (float(w) + 12.0) / float(panel_w))


def _x_span(rc: dict, panel_w: float) -> tuple[float, float]:
    """The tag's horizontal extent in px inside a ``panel_w`` wide panel.  Judged in the NARROWEST panel: two tags are
    then never taken for "apart in time" when a real (wider) panel would still print them on top of each other, and the
    extra separation a wider panel brings only ever pulls them further apart."""
    if rc.get("full_width"):
        return (-1e6, 1e6)
    x = rc["xf"] * float(panel_w) + rc["xshift"]
    return (x - rc["w"], x) if rc.get("right") else (x, x + rc["w"])


def _edge_bands(obs: list, panel_h: float) -> list[tuple[float, float]]:
    """Edge-anchored obstacles as (top, bottom) px bands of a ``panel_h`` tall panel."""
    return [(0.0, float(o["h"])) if o.get("band") == "top" else (float(panel_h) - float(o["h"]), float(panel_h)) for o in obs]


def _stack_one_side(cl: list, down: bool, panel_h: float, gap: float, stand: float, max_rows: int, obs: list) -> dict | None:
    """Place one time cluster's tags on ONE side of their markers — ``down`` = every box hangs BELOW its marker, else
    every box sits ABOVE it.  Tops (bottoms) are made strictly monotone, so the boxes cannot overlap by construction
    AND keep their order however the real panel scales: for two boxes pushed the same way the vertical gap between them
    grows with the panel, it never closes.  ``None`` when the stack does not fit in ``panel_h`` / ``max_rows``."""
    bands = _edge_bands(obs, panel_h)
    plan, prev = {}, None
    for rc in (cl if down else list(reversed(cl))):
        p = rc["yf"] * float(panel_h)
        if down:
            top = p + stand if prev is None else max(p + stand, prev + gap)
            for b0, b1 in bands:
                if top < b1 and top + rc["h"] > b0:
                    top = b1 + gap
            if top + rc["h"] > panel_h or (top - p - stand) > max_rows * (rc["h"] + gap):
                return None
            prev = top + rc["h"]
            plan[rc["name"]] = {"drop": False, "yanchor": "top", "yshift": -(top - p)}
        else:
            bot = p - stand if prev is None else min(p - stand, prev - gap)
            for b0, b1 in bands:
                if bot > b0 and bot - rc["h"] < b1:
                    bot = b0 - gap
            if bot - rc["h"] < 0.0 or (p - bot - stand) > max_rows * (rc["h"] + gap):
                return None
            prev = bot - rc["h"]
            plan[rc["name"]] = {"drop": False, "yanchor": "bottom", "yshift": (p - bot)}
    return plan


def stack_annotations(recs: list, panel_h: float = MEAS_PANEL_MIN_H, panel_w: float = MEAS_PANEL_MIN_W,
                      gap: float = MEAS_TAG_GAP, stand: float = MEAS_TAG_STAND, max_rows: int = MEAS_TAG_ROWS) -> dict:
    """DETERMINISTIC slotting of the quad's in-panel tags — the server-side mirror of the panel's depill() for the map
    pills, which the measurement quad never had (clip_audit: "#177 <> #200", "#200 <> #200" printed on top of each
    other; "#129" pushed 30–53 px out of its panel).  ``recs`` = _tag_rec records in draw order (obstacles carry a
    ``band``); the rule is:
      1. tags whose boxes overlap horizontally IN THE NARROWEST PANEL form one time cluster (nothing else can collide);
      2. a cluster is stacked on ONE side of its markers, BELOW them by preference (a start pill is then never pushed
         up into the panel title) and above them only when the stack has no room below;
      3. every box is checked for containment and for the edge-anchored obstacles against the panel's LOWER bound, so a
         box that fits here fits in every real panel;
      4. what still does not fit is dropped, lowest priority first — never drawn outside its panel.
    Returns {name: {"yanchor", "yshift", "drop"}} for every movable tag."""
    obstacles = [r for r in recs if not r.get("movable", True)]
    tags = [r for r in recs if r.get("movable", True)]
    spans = {r["name"]: _x_span(r, panel_w) for r in recs}
    clusters, cur, cur_hi = [], [], 0.0
    for r in sorted(tags, key=lambda r: (spans[r["name"]][0], r["name"])):
        lo, hi = spans[r["name"]]
        if cur and lo < cur_hi:
            cur.append(r)
            cur_hi = max(cur_hi, hi)
        else:
            if cur:
                clusters.append(cur)
            cur, cur_hi = [r], hi
    if cur:
        clusters.append(cur)
    out: dict[str, dict] = {}
    for cl in clusters:
        lo = min(spans[r["name"]][0] for r in cl)
        hi = max(spans[r["name"]][1] for r in cl)
        obs = [o for o in obstacles if spans[o["name"]][0] < hi and spans[o["name"]][1] > lo]
        keep = sorted(cl, key=lambda r: (r["yf"], r["prio"], r["name"]))
        while keep:
            plan = (_stack_one_side(keep, True, panel_h, gap, stand, max_rows, obs)
                    or _stack_one_side(keep, False, panel_h, gap, stand, max_rows, obs))
            if plan is not None:
                out.update(plan)
                break
            keep.remove(max(keep, key=lambda r: (r["prio"], r["yf"], r["name"])))
        for r in cl:
            out.setdefault(r["name"], {"drop": True, "yanchor": "top", "yshift": -stand})
    return out


def strip_tags(items: list, font_px: float, title: str, title_px: float, panel_w: float = MEAS_PANEL_MIN_W,
               max_tags: int = MEAS_DEP_MAX, gap: float = 6.0) -> list:
    """Right-to-left layout of a quad panel's HEADER-STRIP tags (2026-09-15: the "→ departed" / "→ interceptor
    (stolen)" words moved OUT of the plot area — in it they printed over the track lines, over each other and 3–4 px
    past the panel at X-Large).  ``items`` = (sort key, name, text) records — the key ranks a steal above a plain
    departure and a later event above an earlier one; returns [(name, text, xshift)] highest-ranked first for the ones that fit beside the panel ``title`` in the NARROWEST panel.  The rest are dropped: the ▶ marker
    at the departure point keeps the event, with the words in its hover."""
    room = float(panel_w) - tag_w(title, title_px) - gap
    out, off = [], gap / 2.0
    for _, name, text in sorted(items, key=lambda it: it[0], reverse=True)[:int(max_tags)]:
        w = tag_w(text, font_px)
        if off + w > room:
            break
        out.append((name, text, -off))
        off += w + gap
    return out


def meas_ticks(lo: float, hi: float, panel_h: float = MEAS_PANEL_MIN_H, font_px: float = FONT_PX) -> list[float]:
    """Explicit y ticks for a quad panel: 3–4 'nice' values inside the truth envelope, THINNED like the error cards
    (thin_ticks) for the shortest panel we support — plotly never drops colliding y tick labels, and a quad panel is
    only ~112 px tall at a laptop width while the panel JS may raise the tick font to 18 px."""
    return thin_ticks(vel_ticks(float(lo), float(hi)), float(lo), float(hi), float(panel_h), float(font_px))


def meas_fig(M: dict, A: dict, P: dict) -> go.Figure:
    """2×2 quad (BISTATIC range km · BISTATIC range rate m/s · azimuth ° · elevation °): TRUTH = target truth
    3 px target red + interceptor truth 3 px interceptor blue; TRACKS = every target-side track in the
    window, one colour per id by first appearance (dataviz slots, blue reserved), drawn per sample: ON-TARGET
    (M track "on": < 150 m from the target truth and closer to it than to the interceptor) as the full 2 px
    line, DEPARTED (dragged away) as a thin 1 px .35 line in the home hue, STOLEN (interceptor-side) in the blue ramp;
    tentative / coasting samples lighter (.6); ★ at the first ON-TARGET sample, ✕ at the last, both labelled with the id
    on a 12 px mono pill (CARD bg, RULE border), a muted "→ departed" glyph where the track leaves the target
    and carries on; the interceptor-side track dashed blue at .5 ("#203 · interceptor"); OBS = filled 6 px
    light-ink circles at .75 with a surface ring, drawn first (no obs range rate in archive replay: block-103
    amb_dop is not archived — the caption says so).  Y-RANGES = the TRUTH envelope ± 15 % (min spans), never
    the tracks: a runaway track is clipped and acknowledged by a ▲ / ▼ indicator at the panel edge with the
    count in its hover.  Z-order: obs → departed → on-target → truth → markers / pills.  Handover ticks
    "→ #177" at the top edge; CPA ★ hairline as on the other figures; unified hover, legend 12 px in the
    groups TRUTH / TRACKS / OBS."""
    height = int(P.get("meas_height", MEAS_H))
    pill_px, title_px, small_px = px(PILL_PX, P), px(MEAS_TITLE_PX, P), px(11, P)
    fig = make_subplots(rows=2, cols=2, shared_xaxes=True, horizontal_spacing=0.06, vertical_spacing=0.12)
    pos = {"rng": (1, 1), "rr": (1, 2), "az": (2, 1), "el": (2, 2)}
    truth, truth_itc, obs = M.get("truth"), M.get("truth_itc"), M.get("obs")
    cpa = A.get("cpa")
    t_lo, t_now = float(M["t_lo"]), float(M["t_now"])
    span = max(1e-9, t_now - t_lo)
    tracks = list(M.get("tracks", []))
    # an ungraded ("free") track's legend word: with an interceptor-only MAVLink feed the honest reason is the missing
    # TARGET truth, not missing truth (2026-09-15 MRU91)
    free_word = "no target truth" if no_tgt_truth(A) else "no truth"
    step_of, counters = {}, {"tgt": 0, "itc": 0, "free": 0}
    for tr in tracks:                       # identity step by first appearance WITHIN the track's home role (colour = role; step -> lightness + dash; the pill carries the id)
        home = track_sides(tr)[1]
        step_of[tr["tid"]] = counters[home]
        counters[home] += 1
    slot_of = {tid: track_slot(st_, "tgt") for tid, st_ in step_of.items()}     # legacy: the target-ramp colour per track
    first_track = next((tr["tid"] for tr in tracks), None)
    ranges: dict[str, tuple[float, float] | None] = {}
    # in-panel tags per panel as PANEL-FRACTION records for the deterministic declutter (stack_annotations), and the
    # panel's HEADER-STRIP tags (the departures) as (t, name, text) for strip_tags — the quad's pills and "→ departed"
    # words used to be placed against an ESTIMATE of the panel's pixels and landed on top of each other / 30-53 px
    # outside the panel at every size (clip_audit 2026-09-14/15)
    tags: dict[str, list] = {k: [] for k in MEAS_KEYS}
    deps: dict[str, list] = {k: [] for k in MEAS_KEYS}
    for key in MEAS_KEYS:
        r, c = pos[key]
        first = key == "rng"
        n_before = len(fig.data)
        # 1. OBS first (underneath everything); range rate only when the obs carry one (live amb_dop / bistatic rate)
        if obs is not None and len(obs["t"]) and (key != "rr" or M.get("obs_rr")):
            yo = _meas_val(obs, key)
            fig.add_trace(go.Scatter(x=D.to_pdt_dt64(obs["t"]), y=yo, mode="markers", name="raw obs", legendgroup="obs", showlegend=first,
                                     legendgrouptitle_text="OBS" if first else None,
                                     marker=dict(symbol="circle", size=MEAS_OBS_PX, color=MEAS_OBS_COLOR, line=dict(width=1, color=T.SURFACE)),
                                     opacity=MEAS_OBS_ALPHA, hoverinfo="skip"), row=r, col=c)
        # 2. TRACKS: per-sample SIDE — target-side (slot colour, full), interceptor-side (blue dashed: a STOLEN track now
        #    rides the interceptor), neither (thin .35 = departed / dragged away); dotted = tentative / coasting; one legend entry per track
        rng_ = truth_envelope([truth, truth_itc], key)
        ranges[key] = rng_
        for tr in tracks:
            y = _meas_val(tr, key)
            if y is None or not np.isfinite(y).any():
                continue
            t = np.asarray(tr["t"], float)
            side, home = track_sides(tr)
            kind = np.asarray(tr.get("kind", np.full(len(t), "meas", object)), object)
            meas = kind == "meas"
            dt = np.diff(t) if len(t) > 1 else np.zeros(0)
            gap = max(MEAS_GAP_S, GAP_FACTOR * float(np.median(dt))) if len(dt) else MEAS_GAP_S
            tid = tr["tid"]
            c_tgt, c_itc, dash = track_style(step_of.get(tid, 0))
            col_home = T.GREY_TRACK if home == "free" else (c_itc if home == "itc" else c_tgt)
            name = f"#{tid} · " + {"tgt": "target", "itc": "interceptor", "free": free_word}[home]
            grp = f"trk{tid}"
            shown = False
            for cat, coasting in (("none", True), ("none", False), ("itc", True), ("itc", False), ("tgt", True), ("tgt", False)):   # z-order: departed → interceptor-side → target-side
                mask = (side == cat) & (~meas if coasting else meas)
                if not mask.any():
                    continue
                xs, ys = _cat_xy(t, y, mask, gap)
                if not xs:
                    continue
                if cat == "tgt":
                    col, w, alpha = c_tgt, ON_W, 1.0                       # colour = role (red ramp), step -> lightness + dash
                elif cat == "itc":
                    col, w, alpha = c_itc, ON_W, 1.0                       # on the interceptor: the blue ramp at the same step (a STOLEN track turns blue)
                else:
                    col, w, alpha = col_home, DEPARTED_W, DEPARTED_ALPHA   # departed: thin and faint in the home hue
                if coasting:
                    alpha = min(alpha, COAST_ALPHA)                        # tentative / coasting samples: lighter, never a different dash
                note = {"tgt": "", "itc": " · on the interceptor", "none": " · departed"}[cat] + (" · coasting / tentative" if coasting else "")
                kw = dict(legendgroup=grp, showlegend=False)
                if first and cat == home and not coasting and not shown:
                    kw.update(showlegend=True, legendgrouptitle_text="TRACKS" if tid == first_track else None)
                    shown = True
                fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=name, opacity=alpha, line=dict(color=col, width=w, dash=dash),
                                         connectgaps=False, hovertemplate=MEAS_HOVER[key] + note, **kw), row=r, col=c)
            if first and not shown:   # the whole track was departed / coasting in this panel: still one legend entry
                fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name=name, legendgroup=grp, showlegend=True, hoverinfo="skip",
                                         legendgrouptitle_text="TRACKS" if tid == first_track else None,
                                         line=dict(color=col_home, width=ON_W, dash=dash)), row=r, col=c)
            # off-scale indicator: samples outside the truth envelope are clipped by the axis -> ▲ / ▼ at the edge, count in hover
            if rng_ is not None:
                fin = np.isfinite(y)
                for sym, m_off, yy, anch in (("▲", fin & (y > rng_[1]), 1.0, "top"), ("▼", fin & (y < rng_[0]), 0.0, "bottom")):
                    if m_off.any():
                        i = int(np.flatnonzero(m_off)[0])
                        extreme = float(np.nanmax(y[m_off])) if sym == "▲" else float(np.nanmin(y[m_off]))
                        fig.add_annotation(xref="x", yref="y domain", x=_dt(t[i]), y=yy, xanchor="center", yanchor=anch, text=sym, showarrow=False,
                                           font=dict(family=T.MONO, size=small_px, color=T.INK2), bgcolor=TAG_BG, borderpad=1,
                                           hovertext=f"#{tid} · {int(m_off.sum())} samples off scale ({'above' if sym == '▲' else 'below'}, to {extreme:.2f}) · zoom out to see",
                                           name=f"off_{key}_{tid}_{'up' if sym == '▲' else 'dn'}", row=r, col=c)
                        nm = f"off_{key}_{tid}_{'up' if sym == '▲' else 'dn'}"
                        tags[key].append(_tag_rec(nm, sym, small_px, t[i], 0.0, t_lo, span, None,
                                                  band="top" if sym == "▲" else "bottom"))   # an EDGE-anchored obstacle: a pill must clear it
        # 3. TRUTH on top of the tracks: target red, interceptor blue, 3 px
        for series, col, name, ttl in ((truth, T.TARGET, "target truth", "TRUTH"), (truth_itc, T.INTERCEPTOR, "interceptor truth", None)):
            if series is not None and len(series["t"]):
                y = _meas_val(series, key)
                if y is not None and np.isfinite(y).any():
                    fig.add_trace(go.Scatter(x=D.to_pdt_dt64(series["t"]), y=y, mode="lines", name=name, legendgroup="truth", showlegend=first,
                                             legendgrouptitle_text=ttl if first else None,
                                             line=dict(color=col, width=TRUTH_W), connectgaps=False, hovertemplate=MEAS_HOVER[key]), row=r, col=c)
        # 4. MARKERS + PILLS: ★ first ON-TARGET sample, ✕ last (pure interceptor tracks: their on-interceptor span), "→ departed" /
        #    "→ interceptor" glyph where the track carries on after leaving the target
        for tr in tracks:
            y = _meas_val(tr, key)
            if y is None:
                continue
            t = np.asarray(tr["t"], float)
            side, home = track_sides(tr)
            on = (side == ("itc" if home == "itc" else "tgt")) & np.isfinite(y) if home != "free" else np.isfinite(y)
            if not on.any():
                continue
            tid = tr["tid"]
            c_tgt, c_itc, _dash = track_style(step_of.get(tid, 0))
            col = T.GREY_TRACK if home == "free" else (c_itc if home == "itc" else c_tgt)
            grp = f"trk{tid}"
            idx = np.flatnonzero(on)
            i0, i1 = int(idx[0]), int(idx[-1])
            x0_, x1_ = _dt(t[i0]), _dt(t[i1])
            name = f"#{tid} · " + {"tgt": "target", "itc": "interceptor", "free": free_word}[home]
            fig.add_trace(go.Scatter(x=[x0_], y=[y[i0]], mode="markers", name=f"{name} start", legendgroup=grp, showlegend=False,
                                     marker=dict(symbol="star", size=11, color=col, line=dict(width=2, color=T.CARD)),
                                     hovertemplate=f"#{tid} starts here<extra></extra>"), row=r, col=c)   # the id is in the hover too: a pill the declutter had to drop loses nothing
            right = tag_side((t[i0] - t_lo) / span, tag_w(f"#{tid}", pill_px))
            if first:   # 2026-09-14: track-number pills on the first panel only ("not on EVERY plot")
                nm = f"lbl_start_{tid}_{key}"
                # anchor / yshift are PROVISIONAL: stack_annotations sets both below, from the panel's bounds (never an estimate)
                fig.add_annotation(xref="x", yref="y", x=x0_, y=float(y[i0]), text=f"#{tid}", showarrow=False, xanchor="right" if right else "left",
                                   yanchor="top", xshift=-10 if right else 10, yshift=-MEAS_TAG_STAND, font=dict(family=T.MONO, size=pill_px, color=T.INK),
                                   bgcolor=T.CARD, bordercolor=T.RULE, borderwidth=1, borderpad=2, name=nm, row=r, col=c)
                tags[key].append(_tag_rec(nm, f"#{tid}", pill_px, t[i0], float(y[i0]), t_lo, span, rng_,
                                          xshift=-10 if right else 10, right=right, prio=0))
            if i1 > i0:
                fig.add_trace(go.Scatter(x=[x1_], y=[y[i1]], mode="markers", name=f"{name} end", legendgroup=grp, showlegend=False,
                                         marker=dict(symbol="x", size=9, color=col, line=dict(width=2, color=T.CARD)),
                                         hovertemplate=f"#{tid} ends here<extra></extra>"), row=r, col=c)
                right = tag_side((t[i1] - t_lo) / span, tag_w(f"#{tid}", pill_px))
                if first:
                    nm = f"lbl_end_{tid}_{key}"
                    fig.add_annotation(xref="x", yref="y", x=x1_, y=float(y[i1]), text=f"#{tid}", showarrow=False, xanchor="right" if right else "left",
                                       yanchor="top", xshift=-10 if right else 10, yshift=-MEAS_TAG_STAND, font=dict(family=T.MONO, size=pill_px, color=T.INK),
                                       bgcolor=T.CARD, bordercolor=T.RULE, borderwidth=1, borderpad=2, name=nm, row=r, col=c)
                    tags[key].append(_tag_rec(nm, f"#{tid}", pill_px, t[i1], float(y[i1]), t_lo, span, rng_,
                                              xshift=-10 if right else 10, right=right, prio=1))   # the END pill is the first thing dropped when a cluster will not fit
            after = np.flatnonzero(np.isfinite(y[i1 + 1:])) + i1 + 1
            if len(after):   # the track continues after its last on-side sample: the departure point
                j = int(after[0])
                stolen = home == "tgt" and bool((side[j:] == "itc").any())
                word = "→ interceptor (stolen)" if stolen else "→ departed"
                fig.add_trace(go.Scatter(x=[_dt(t[j])], y=[y[j]], mode="markers", name=f"{name} departed", legendgroup=grp, showlegend=False,
                                         marker=dict(symbol="triangle-right", size=8, color=T.INK3, line=dict(width=2, color=T.CARD)),
                                         hovertemplate=f"#{tid} {word[2:]}<extra></extra>"), row=r, col=c)
                # 2026-09-15: the words go in the panel's HEADER STRIP, not in the plot area — "→ interceptor (stolen)" needs
                # ~40 % of a laptop-width panel and, wherever it was put next to its point, it printed over the track lines,
                # over a pill, or past the panel edge (clip_audit "annotation-outside-panel" / "annotations-overlap")
                deps[key].append(((1 if stolen else 0, float(t[j])), f"departed_{tid}_{key}", word))   # a STEAL outranks a plain departure for the strip's room
        if len(fig.data) == n_before:   # empty panel: keep its axes alive (see error_fig)
            fig.add_trace(go.Scatter(x=[_dt(t_now)], y=[0.0], mode="markers", marker=dict(size=1, opacity=0, color=T.CARD), hoverinfo="skip",
                                     showlegend=False, name=f"_anchor_{key}"), row=r, col=c)
        fig.add_vline(x=_dt(t_now), line=dict(color=T.RED, width=1), row=r, col=c)
        # panel title INSIDE the panel, top-left (card-title style, 12 px bold) — the legend above never collides with it
        # subplot title ABOVE the plot area (yanchor bottom at domain 1.0): a track's start pill (★ #129) at the left edge sat under it
        fig.add_annotation(xref="x domain", yref="y domain", x=0.0, y=1.0, xanchor="left", yanchor="bottom", xshift=2, yshift=2,
                           text=f"<b>{MEAS_TITLE[key]}</b>", showarrow=False, font=dict(family=T.MONO, size=title_px, color=T.INK2),
                           bgcolor=TAG_BG, borderpad=1, name=f"title_{key}", row=r, col=c)
    # bistatic geometry note (co-located TX/RX -> 2 x mono) at the foot of the range panel
    geom = M.get("geom") or {}
    if geom.get("note"):
        fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=0.0, xanchor="right", yanchor="bottom", xshift=-4, yshift=2, text=geom["note"],
                           showarrow=False, font=dict(family=T.MONO, size=small_px, color=T.INK3), bgcolor=TAG_BG, borderpad=2, name="geom_note", row=1, col=1)
        # a FIXED full-width obstacle pinned to the panel's BOTTOM edge: the note is wider than one quad panel anyway,
        # and an edge-anchored band is the one thing a clearance decision can use EXACTLY at any panel height
        tags[MEAS_KEYS[0]].insert(0, _tag_rec("geom_note", geom["note"], small_px, t_lo, 0.0, t_lo, span, None,
                                              band="bottom", full_width=True))
    # track-id changes inside the window: a tick at the top edge of all four panels, labelled on the top row.  Drawn
    # BEFORE the declutter so its hover-only ▾ mark at the top edge is an obstacle the pills have to clear.
    handover_marks(fig, A.get("track_events"), t_lo, t_now, [pos[MEAS_KEYS[0]]], {pos[MEAS_KEYS[0]]}, P)   # first panel only (2026-09-14)
    for e in A.get("track_events") or ():
        et, erole, _old, enew, _ = _ev(e)
        if enew is not None and t_lo <= et <= t_now and erole in ("target", "interceptor"):
            tags[MEAS_KEYS[0]].append(_tag_rec(f"handover_tag_{erole}_{enew}", "▾", max(9, px(TICK_PX, P) - 2), et, 0.0, t_lo, span, None, band="top"))
    # HEADER-STRIP tags: the departures, newest first, right-aligned beside the panel title (never in the plot area)
    for key in MEAS_KEYS:
        r, c = pos[key]
        fit = {nm: xsh for nm, _w, xsh in strip_tags(deps[key], small_px, f"<b>{MEAS_TITLE[key]}</b>", title_px)}
        for _t, nm, word in deps[key]:   # every departure is still an annotation (the panel's caption names it); only the ones that FIT are visible
            fig.add_annotation(xref="x domain", yref="y domain", x=1.0, y=1.0, xanchor="right", yanchor="bottom",
                               xshift=fit.get(nm, -4.0), yshift=2, visible=nm in fit,
                               text=word, showarrow=False, font=dict(family=T.MONO, size=small_px, color=T.INK3), bgcolor=TAG_BG, borderpad=1,
                               name=nm, row=r, col=c)
    # DECLUTTER: every in-panel pill gets its slot from the panel's BOUNDS (anchor + pixel yshift); what cannot fit
    # inside the panel is removed rather than drawn over the data or outside it
    for key in MEAS_KEYS:
        if not tags[key]:
            continue
        for nm, sl in stack_annotations(tags[key]).items():
            if sl["drop"]:
                fig.update_annotations(patch={"visible": False}, selector={"name": nm})
            else:
                fig.update_annotations(patch={"yanchor": sl["yanchor"], "yshift": sl["yshift"]}, selector={"name": nm})
    base_layout(fig, height=height, uirevision=MEAS_UIREV, legend=True, time_x=True, unified=True, margin=MEAS_MARGIN, font_px=int(P.get("font_px", FONT_PX)))
    leg_px = max(px(MEAS_LEGEND_PX, P), int(P.get("font_px", FONT_PX)))
    fig.update_layout(legend=dict(y=1.0, yanchor="bottom", x=0.0, font=dict(size=leg_px), grouptitlefont=dict(family=T.MONO, size=leg_px, color=T.INK3)),
                      modebar=dict(remove=list(MAP_MODEBAR_DROP), bgcolor="rgba(0,0,0,0)", color=T.INK3, activecolor=T.INK),   # a shorter modebar: the legend's last row ran under its buttons
                      margin=dict(MEAS_MARGIN, t=MEAS_TOP_PX))
    # the key sits clear ABOVE the top-row title strip.  The standoff is a PAPER fraction, so it is taken against the
    # plot area's LOWER bound (MEAS_PLOT_MIN_H): in a real (taller) plot area the same fraction is more pixels, never fewer
    # — the old `height - margins` divisor was 418 px against a real 283-358 and left the key 1 px off the title box.
    fig.update_layout(legend=dict(y=1.0 + (tag_h(title_px) + 6.0) / MEAS_PLOT_MIN_H, yanchor="bottom"))
    tick_px = max(int(P.get("font_px", FONT_PX)), JS_FONT_MIN)      # the panel JS may RAISE the tick font (meas_html fontFor)
    for key, (r, c) in pos.items():
        fig.update_yaxes(title_text=MEAS_AXIS[key], row=r, col=c)
        if ranges.get(key) is not None:
            lo_, hi_ = float(ranges[key][0]), float(ranges[key][1])
            fig.update_yaxes(range=[lo_, hi_], tickvals=meas_ticks(lo_, hi_, MEAS_PANEL_MIN_H, tick_px), row=r, col=c)
        fig.update_xaxes(range=[_dt(t_lo), _dt(t_now)], row=r, col=c)
    fig.update_xaxes(title_text="time (PDT)", row=2, col=1)
    fig.update_xaxes(title_text="time (PDT)", row=2, col=2)
    return fig
