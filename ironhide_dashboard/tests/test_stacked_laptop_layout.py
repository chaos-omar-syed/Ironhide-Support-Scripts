"""STACKED LAPTOP GEOMETRY (2026-09-14, "clean up all the overlap"): the panel figures built for the ONE-column heights
the panel uses below its 1150 px breakpoint (ih.liveserver ONE_SEP_PX / one_err_px(scale) / one_vel_px(scale)) at the iframe widths a
1366 / 1536 laptop gives (~945 / ~1200 px) and at every sidebar text scale (Normal 1.0 / Large 1.15 / X-Large 1.3).

Static mirror of tests/clip_audit.py (the real-browser audit), so a regression fails in seconds instead of 20 minutes:
  * no two header-strip annotations of a card overlap (title vs "now ±σ" on line 1, line 1 vs line 2)
  * no strip annotation intrudes into a plot area, and none leaves its card
  * the map's key is a CONSTANT-height single row in the top margin, never over the plot area, clear of the hover
    modebar's row; its fixed margins hold the axis titles + the widest tick label (automargin is off there)
  * y tick labels of one card stay at least 0.9 x font apart (plotly never drops colliding y labels)
Text is measured with the mono metrics ih.plots took from Firefox (GLYPH_W 0.625 x font per glyph, LINE_H / ERR_BOX_LH
1.35 x font per line box) — the same numbers the figures reserve their strips with."""
from __future__ import annotations

import logging
import os
import re
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.WARNING)

import ih.data as D  # noqa: E402
import ih.liveserver as LS  # noqa: E402
import ih.plots as PL  # noqa: E402
from test_plots_round3 import _A_err, _A_vel, _truth, T_NOW, WIN  # noqa: E402

WIDTHS = (945, 1200)                       # iframe width at 1366 / 1536 px screens (stacked mode: the err / vel figures span it)
SCALES = (1.0, 1.15, 1.3)                  # sidebar "Text size": Normal / Large / X-Large (ih.theme TEXT_SCALES)
FONTS = {1.0: 14, 1.15: 16, 1.3: 18}       # the figure font per scale on the default preset (LS.FONT_PX["Desktop 1080p"] x scale)
PANELS = {1.0: 485, 1.15: 462, 1.3: 440}   # the panel iframe height a 768 px tall browser leaves (viewportPanel: the top strip grows with the text)
MAP_T0 = LS.map_top_nolegend_px()          # mode "one": the map's top margin without the key row (modebar row + 2 = 28)
BORDERPAD = 1.0                            # annotation borderpad (px, each side)
W_PLOT = [0.0]                             # the current case's plot-area width in px (set by _fig_case)


# ── text metrics ─────────────────────────────────────────────────────────────
def text_w(text: str, font_px: float) -> float:
    """Width of an annotation box: mono glyphs x GLYPH_W x font + the border padding (markup is not drawn)."""
    plain = re.sub(r"<[^>]+>", "", str(text))
    return PL.GLYPH_W * float(font_px) * len(plain) + 2.0 * BORDERPAD + 2.0


def box_h(font_px: float) -> float:
    return PL.ERR_BOX_LH * float(font_px) + PL.ERR_BOX_PAD


def y_margin_l(fig, font_px: float) -> float:
    """The left margin the DRAWN figure has: the base one, or the y automargin's (rotated title + widest tick label)
    when that is wider — measured in Firefox at 18 px: 84 px, this formula 83."""
    L = fig.to_plotly_json()["layout"]
    glyphs = 1
    for k, v in L.items():
        if k.startswith("yaxis"):
            for t in (v.get("tickvals") or ()):
                glyphs = max(glyphs, len(("%g" % float(t)).replace("-", "−")))
    auto = PL.LINE_H * font_px + 6.0 + PL.GLYPH_W * font_px * glyphs + PL.MAP_TICK_GAP
    return max(float(L["margin"]["l"]), auto)


def strip_boxes(fig, key: str) -> list[tuple[str, float, float, float, float]]:
    """Header-strip annotations of card ``key`` as (name, x0, x1, y0, y1) in px — x from the plot area's left edge,
    y measured UP from that card's plot-area top (0 = the top of the plot area, positive = inside the strip)."""
    L = fig.to_plotly_json()["layout"]
    w = float(W_PLOT[0])
    out = []
    for a in L.get("annotations", []):
        name = str(a.get("name") or "")
        if not name.endswith("_" + key) or a.get("visible") is False:
            continue
        f = float(a["font"]["size"])
        tw = text_w(a["text"], f)
        xs = float(a.get("xshift", 0) or 0)
        x1 = w + xs if a.get("xanchor") == "right" else xs + tw
        y0 = float(a.get("yshift", 0) or 0)
        out.append((name, x1 - tw, x1, y0, y0 + box_h(f)))
    return out


def _trail(dn: float = 0.0) -> np.ndarray:
    t = T_NOW - WIN + np.arange(120, dtype=float)
    tr = np.zeros((120, 7))
    tr[:, 0] = t
    tr[:, D.TR["E"]] = np.linspace(-800.0, 900.0, 120)
    tr[:, D.TR["N"]] = dn + np.linspace(-400.0, 500.0, 120)
    tr[:, D.TR["vE"]], tr[:, D.TR["vN"]] = 14.0, 4.0
    return tr


def _mtrack(dn: float = 0.0) -> np.ndarray:
    t = T_NOW - WIN + np.arange(120, dtype=float)
    tk = np.full((120, D.TK_W), np.nan)
    tk[:, 0] = t
    tk[:, D.TK["E"]] = np.linspace(-780.0, 920.0, 120)
    tk[:, D.TK["N"]] = dn + np.linspace(-390.0, 510.0, 120)
    return tk


def _one_row(width: int, scale: float) -> dict:
    """The mode-one two-up first row at this iframe width / text scale (ih.liveserver.one_row: map | separation)."""
    return LS.one_row(PANELS[scale], width, FONTS[scale], scale)


def _map_fig(width: int, font: int, scale: float, **kw):
    A = {"t_now": T_NOW, "tgt_trail": _trail(), "itc_trail": _trail(150.0), "tgt_track": _mtrack(), "tgt_tid": 177,
         "itc_track": _mtrack(60.0), "itc_tid": 200, "cpa": None}
    A.update(kw.pop("A", {}))
    P = {"font_px": font, "line_w": 2.5, "text_scale": scale, "map_half": 1500.0, "trail_s": 120.0, "show_sat": False,
         "show_blind": False, "map_height": _one_row(width, scale)["map"], "icons_in_fig": False}
    return PL.map_fig(A, {**P, **kw})


def _fig_case(kind: str, width: int, scale: float):
    """(figure, height, font_px) for one stacked-mode figure at this iframe width / text scale; sets W_PLOT."""
    font = FONTS[scale]
    P = {"font_px": font, "line_w": 2.5, "text_scale": scale, "show_obs": True}
    if kind == "err":
        h = LS.one_err_px(scale)                # the stacked heights follow the text scale (the header strips do)
        fig = PL.error_fig(_A_err(), {**P, "err_height": h}, WIN)
    elif kind == "vel":
        h = LS.one_vel_px(scale)
        fig = PL.velocity_fig(_A_vel(), {**P, "vel_height": h}, WIN, truth=_truth())
    elif kind == "sep":
        h = _one_row(width, scale)["sep"]       # mode one: the separation card is the first row's right column (panel − header tall)
        t = np.linspace(T_NOW - WIN, T_NOW, 60)
        A = {"t_now": T_NOW, "sep": {"t": t, "sep": np.linspace(600.0, 60.0, 60), "horiz": np.linspace(590.0, 50.0, 60)}, "cpa": None}
        fig = PL.separation_fig(A, {**P, "sep_height": h})
    else:
        h = _one_row(width, scale)["map"]
        fig = _map_fig(width, font, scale)
    L = fig.to_plotly_json()["layout"]
    fig_w = _one_row(width, scale)["mapW" if kind == "map" else "right"] if kind in ("map", "sep") else width   # the first-row cards do not span the iframe
    left = float(L["margin"]["l"]) if kind == "map" else y_margin_l(fig, font)
    W_PLOT[0] = float(fig_w) - left - float(L["margin"]["r"])
    return fig, h, font


def rows_geometry(fig, height: int, rows: int) -> tuple[float, float, float]:
    """(plot_h, row_h, hdr) at ``height`` — the panel's own formula (liveserver errRows / cardGrid)."""
    L = fig.to_plotly_json()["layout"]
    hdr = float(L["meta"]["err" if rows == 4 else "vel"]["hdr"])
    plot_h = float(height) - L["margin"]["t"] - L["margin"]["b"]
    return plot_h, (plot_h - (rows - 1) * (PL.PANEL_GAP_PX + hdr)) / rows, hdr


# ── 1. header strips: nothing overlaps, nothing reaches a plot area ──────────
@pytest.mark.parametrize("kind,keys,rows", [("err", PL.ERR_KEYS, 4), ("vel", PL.VEL_KEYS, 3)])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_strip_annotations_never_overlap_or_touch_a_plot_area(kind, keys, rows, width, scale):
    fig, height, font = _fig_case(kind, width, scale)
    plot_h, row_h, hdr = rows_geometry(fig, height, rows)
    assert row_h > 0 and W_PLOT[0] > 200, (kind, width, scale, row_h, W_PLOT[0])
    for key in keys:
        boxes = strip_boxes(fig, key)
        assert len(boxes) >= 2, (kind, key, boxes)
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                n0, x00, x01, y00, y01 = boxes[i]
                n1, x10, x11, y10, y11 = boxes[j]
                ox, oy = min(x01, x11) - max(x00, x10), min(y01, y11) - max(y00, y10)
                assert not (ox > 0 and oy > 0), (kind, key, scale, width, n0, n1, round(ox, 1), round(oy, 1))
        for (n, x0, x1, y0, y1) in boxes:
            assert y0 >= 0.0, (kind, key, scale, width, n, y0)                              # never down into its own plot area
            assert y1 <= hdr + PL.CARD_PAD_PX, (kind, key, scale, width, n, y1, hdr)        # nor up out of the card, into the gap above
            pad = PL.CARD_PAD_X * W_PLOT[0] + 1.0
            assert x0 >= -pad and x1 <= W_PLOT[0] + pad, (kind, key, scale, width, n, round(x0, 1), round(x1, 1), round(W_PLOT[0], 1))


# ── 2. the reservation the panel re-derives its rows from (layout.margin) adds up ──
@pytest.mark.parametrize("kind,rows", [("err", 4), ("vel", 3)])
@pytest.mark.parametrize("scale", SCALES)
def test_top_margin_is_strip_plus_legend_row(kind, rows, scale):
    fig, height, font = _fig_case(kind, WIDTHS[0], scale)
    L = fig.to_plotly_json()["layout"]
    hdr = float(L["meta"]["err" if rows == 4 else "vel"]["hdr"])
    legend = PL.legend_row_px(font) + PL.LEGEND_EXPAND_PAD if L["showlegend"] else 0
    assert L["margin"]["t"] == int(round(hdr + PL.CARD_PAD_PX + legend)), (kind, scale, L["margin"], hdr, legend)
    assert L["margin"]["b"] >= PL.LINE_H * font + PL.MAP_TICK_GAP - 1, (kind, scale, L["margin"]["b"], font)


# ── 3. y tick labels of one card never collide ──────────────────────────────
@pytest.mark.parametrize("kind,rows", [("err", 4), ("vel", 3)])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_y_tick_labels_stay_a_digit_apart(kind, rows, width, scale):
    fig, height, font = _fig_case(kind, width, scale)
    L = fig.to_plotly_json()["layout"]
    _, row_h, _ = rows_geometry(fig, height, rows)
    if kind == "vel":                       # the velocity block is re-fitted into a column no taller than VEL_STACKED_PX
        _, row_h, _ = rows_geometry(fig, max(height, PL.vel_stacked_px({"text_scale": scale})), rows)
    for k, ax in L.items():
        if not k.startswith("yaxis"):
            continue
        vals = [float(v) for v in (ax.get("tickvals") or ())]
        if len(vals) < 2:
            continue
        lo, hi = (float(v) for v in ax["range"])
        gap = min(abs(b - a) for a, b in zip(vals, vals[1:])) / (hi - lo) * row_h
        assert gap >= 0.9 * font, (kind, k, scale, width, vals, round(gap, 1), font)   # 0.9 x font = a mono digit's ink + a readable gap


# ── 4. the map key: one row in the top margin, under the modebar's row, never on the imagery ──
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_map_key_row_in_the_top_margin_and_fixed_margins(width, scale):
    fig, height, font = _fig_case("map", width, scale)
    L = fig.to_plotly_json()["layout"]
    m = L["margin"]
    assert m["autoexpand"] is False                                      # nothing may grow the margins: the plot area drives the equal-aspect ranges
    assert m == PL.map_margin(font, scale), (m, font, scale)             # derived from the FONT alone — never from the data or the key's content
    row = PL.legend_row_px(max(font, round(PL.JS_FONT_MIN * scale)))
    assert m["t"] == PL.MAP_MODEBAR_PX + row + 2, (m, row)               # modebar row ABOVE the key row, key row directly above the plot area
    assert L["legend"]["y"] == 1.0 and L["legend"]["yanchor"] == "bottom" and L["legend"]["x"] == 0.0
    assert L["legend"]["orientation"] == "h" and L["legend"]["bgcolor"] == "rgba(0,0,0,0)"   # no translucent card: it is not over the imagery any more
    assert L["showlegend"] is True
    need_l = PL.LINE_H * font + 6.0 + PL.GLYPH_W * font * PL.MAP_TICK_GLYPHS + PL.MAP_TICK_GAP
    need_b = PL.MAP_TICK_GAP + 2.0 * PL.LINE_H * font + 6.0
    assert m["l"] >= need_l and m["b"] >= need_b, (m, need_l, need_b)
    assert L["yaxis"]["scaleanchor"] == "x" and L["xaxis"]["constrain"] == "range" and L["yaxis"]["automargin"] is False
    assert _one_row(width, scale)["mapW"] - m["l"] - m["r"] > 0.5 * _one_row(width, scale)["mapW"]   # the margins never eat half the map column
    assert set(L["modebar"]["remove"]) == set(PL.MAP_MODEBAR_DROP)        # a shorter modebar: more room beside the key row


def test_map_margins_do_not_move_with_the_key_content():
    """The point of the fixed top margin: however many series (and however wide their names) the key carries, the plot
    area — and with it the equal-aspect view — is unchanged."""
    m1 = _map_fig(945, 14, 1.0, A={"itc_trail": None, "tgt_track": None, "tgt_tid": None, "itc_track": None, "itc_tid": None}).to_plotly_json()["layout"]["margin"]
    m4 = _map_fig(945, 14, 1.0, show_blind=True, A={"cpa": (59.0, T_NOW - 8.0, 900.0, 300.0, 25.0)}).to_plotly_json()["layout"]["margin"]
    assert m1 == m4 == PL.MARGINS["map"] == dict(l=83, r=12, t=62, b=56, autoexpand=False)


def test_interceptor_track_off_the_key_and_the_two_pills_on_opposite_sides():
    """Four long names would wrap the one-row key (and a second row would land in the modebar's row): the interceptor's
    radar track stays out of it — its pill names it.  The pills sit above / below their point, so two tracks a few
    pixels apart never stack their pills on top of each other (the panel's depill step is a flat 26 px)."""
    fig = _map_fig(945, 14, 1.0, A={"itc_track": _mtrack(0.0)})
    d = fig.to_plotly_json()["data"]
    named = {t.get("name") for t in d if t.get("showlegend", True) and t.get("name")}
    assert "target track #177" in named and "interceptor track #200" not in named, named
    itc = next(t for t in d if t.get("name") == "interceptor track #200")
    assert itc["showlegend"] is False and itc["opacity"] == PL.ITC_TRACK_ALPHA
    pills = {a["name"]: a for a in fig.to_plotly_json()["layout"]["annotations"] if str(a.get("name", "")).startswith("pill_")}
    assert pills["pill_tgt"]["yanchor"] == "bottom" and pills["pill_tgt"]["yshift"] == 14
    assert pills["pill_itc"]["yanchor"] == "top" and pills["pill_itc"]["yshift"] == -14
    assert pills["pill_tgt"]["xshift"] == pills["pill_itc"]["xshift"] == 14


# ── 5. the separation card at the stacked height ────────────────────────────
@pytest.mark.parametrize("scale", SCALES)
def test_separation_card_key_and_titles(scale):
    fig, height, font = _fig_case("sep", WIDTHS[0], scale)
    L = fig.to_plotly_json()["layout"]
    assert L["showlegend"] is True and L["legend"]["y"] == 1.0 and L["legend"]["yanchor"] == "bottom"   # above the plot area, never on the data
    # 2026-09-15: NO x-axis title on the separation card — with the fixed bottom margin it sat ON the HH:MM:SS ticks and the
    # clock format is self-evident (ih.plots.separation_fig).  The measurement quad, which has room, keeps its "time (PDT)".
    assert L["xaxis"]["title"]["text"] == "" and L["yaxis"]["title"]["text"] == "sep m"
    assert 'title_text="time (PDT)"' in open(os.path.join(ROOT, "ih", "plots.py"), encoding="utf-8").read()   # ... only in the quad
    assert L["margin"].get("autoexpand") is not False    # no px-exact strips on this card: plotly may grow its margins for the key / titles
    assert height - L["margin"]["t"] - L["margin"]["b"] >= 200   # the panel switches the key OFF in mode one (legendPatch): the plot area is the card minus these two margins, >= 200 px in the first row


# ── 6. the card plot areas no longer shrink as the text grows (2026-09-15) ──
@pytest.mark.parametrize("kind,rows", [("err", 4), ("vel", 3)])
def test_card_plot_areas_do_not_shrink_with_the_text_scale(kind, rows):
    """The stacked heights (ih.liveserver one_err_px / one_vel_px) scale with the header strips, so ONE card's plot
    area is the same at Normal / Large / X-Large.  Before that fix the strip grew (46 -> 55 px) inside a FIXED
    column: the error rows fell 43 -> 32 px and the velocity rows 40 -> 28 px, under MIN_CARD_PX."""
    rh = {}
    for scale in SCALES:
        fig, height, font = _fig_case(kind, WIDTHS[0], scale)
        _, row_h, hdr = rows_geometry(fig, height, rows)
        rh[scale] = row_h
        assert hdr <= LS.err_hdr_px(scale), (kind, scale, hdr)          # the budget's strip is never shorter than the drawn one
    for scale in SCALES[1:]:
        assert rh[scale] >= rh[1.0] - 3.0, (kind, rh)                   # flat within a rounding pixel or two, never squeezed
    assert min(rh.values()) > 30.0, (kind, rh)


# ── 7. a bigger text scale never costs a card its y tick labels ──
@pytest.mark.parametrize("kind,rows", [("err", 4), ("vel", 3)])
@pytest.mark.parametrize("width", WIDTHS)
def test_text_scale_never_drops_a_y_tick_label(kind, rows, width):
    """thin_ticks drops labels that would collide in the card's plot area, so a card squeezed by a scaled strip
    inside a FIXED column lost labels as the text grew (down to ONE: the axis then reads as a single number).  The
    stacked column scales with the strip now, so every axis keeps at least as many labels as at Normal.
    (A card whose data range only offers two close 'nice' ticks can still print one — that is ih.plots.vel_ticks,
    not the geometry: it is identical at every scale.)"""
    base = None
    for scale in SCALES:
        fig, height, font = _fig_case(kind, width, scale)
        L = fig.to_plotly_json()["layout"]
        n = {k: len(ax.get("tickvals") or ()) for k, ax in L.items() if k.startswith("yaxis")}
        assert len(n) == rows and max(n.values()) >= 2, (kind, width, scale, n)
        if base is None:
            base = n
        else:
            assert all(n[k] >= base[k] for k in base), (kind, width, scale, n, base)


# ── 8. mode "one" two-up first row (2026-09-15): a SQUARE map plot area | the separation card, both the iframe height ──
@pytest.mark.parametrize("width", WIDTHS + (1299,))          # 1366 sidebar open / 1536 sidebar open / 1366 sidebar collapsed
@pytest.mark.parametrize("scale", SCALES)
def test_first_row_map_plot_area_is_square_and_fills_the_engagement_box(width, scale):
    """one_row(): the map div width is the one that squares the plot area under the mode-one margins (key row off: MAP_T0 on top,
    the figure's own l / r / b), so aspect_range() of the square engagement box IS the box — it fills the map (before: a full-width
    829 x 267 plot area showed 3.1x the box's area).  The separation column is what is left, never under ONE_RIGHT_MIN_PX."""
    r = _one_row(width, scale)
    fig, height, font = _fig_case("map", width, scale)
    m = fig.to_plotly_json()["layout"]["margin"]
    assert r["margin"] == {k: m[k] for k in ("l", "r", "t", "b")} and r["t0"] == MAP_T0 == PL.MAP_MODEBAR_PX + 2 < m["t"]   # the patched top margin drops exactly the key row
    assert m["t"] - MAP_T0 == PL.legend_row_px(max(font, round(PL.JS_FONT_MIN * scale)))
    assert r["map"] == r["sep"] == PANELS[scale] - LS.HEADER_PX == height
    plot_w, plot_h = r["mapW"] - m["l"] - m["r"], r["map"] - MAP_T0 - m["b"]
    assert plot_w == plot_h == r["plot"] >= 300, (width, scale, plot_w, plot_h)                                 # square (369 / 340 / 310 px at Normal / Large / X-Large)
    assert r["right"] == width - r["mapW"] - LS.COL_GAP_PX >= LS.ONE_RIGHT_MIN_PX, (width, scale, r)
    box = (-800.0, 800.0, -300.0, 1300.0)                                                                       # a square 1600 m engagement box
    xr, yr = PL.aspect_range(box, plot_w, plot_h)
    assert all(abs(a - b) < 1e-6 for a, b in zip(xr + yr, box[:2] + box[2:]))                                   # fills the plot area exactly (100 %)
    assert abs((xr[1] - xr[0]) / plot_w - (yr[1] - yr[0]) / plot_h) < 1e-9                                      # 1:1 pixels
    if scale == 1.0 and width == 945:
        assert (plot_w, r["mapW"], r["right"]) == (369, 464, 469) and round(1600.0 / plot_w, 2) == 4.34            # vs 5.99 m/px on the old 829 x 267 area


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_first_row_separation_card_has_a_readable_plot_area_without_its_key(width, scale):
    """The separation card in the first row: panel − header tall; with the key off (legendPatch) its plot area is the card minus the
    built top / bottom margins — >= 200 px — and its column is at least ONE_RIGHT_MIN_PX wide for the two-line hover / the x tick row."""
    fig, height, font = _fig_case("sep", width, scale)
    L = fig.to_plotly_json()["layout"]
    r = _one_row(width, scale)
    assert height == r["sep"] and height - L["margin"]["t"] - L["margin"]["b"] >= 200 and W_PLOT[0] >= 150, (width, scale, height, L["margin"], W_PLOT[0])
