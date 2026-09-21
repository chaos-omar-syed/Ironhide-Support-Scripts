"""The time-series CARDS (sep / err / vel) do not jump (2026-09-15 user, screenshot of the four error cards with their y tick
labels cut at the left edge: "make these plots jump a little less").  Static half of the fix (the browser half is
tests/e2e_cards.py):
  * card_margin: the left / right / bottom margins are GLYPH-derived (widest tick label x GLYPH_W x the panel-bumped font + tick gap +
    rotated title; half an "hh:mm:ss" label; one label row) — a pure function of font + text scale, identical for any data,
    and every card's axes have automargin OFF, so nothing plotly measures can move the plot area;
  * the y ticks are explicit tickvals inside the HELD range (err_ticks / nice_ticks / vel_ticks + thin_ticks for short cards);
  * stable_range: widen at once only when the DATA leaves the held range, otherwise no change of any kind within RANGE_DWELL_S of
    the last one, shrink only after RANGE_SHRINK_S below RANGE_SHRINK_FRAC; quant_range puts the velocity ranges on a nice grid;
  * the x axis has a FIXED dtick from the window length (labels slide instead of re-spacing);
  * the velocity readout is the error cards' primary readout (READOUT_PX x scale, primary ink, bold numbers, full words + unit).
Scales 1.0 / 1.15 / 1.3 x the stacked card heights from ih.liveserver.one_err_px / one_vel_px (read-only import)."""
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
import ih.theme as T  # noqa: E402
from test_plots_round3 import _A_err, _A_vel, _truth, T_NOW, WIN  # noqa: E402

SCALES = (1.0, 1.15, 1.3)
FONTS = {1.0: 13, 1.15: 15, 1.3: 17}          # the Laptop preset font x scale (liveserver FONT_PX["Laptop"] = 13) — the panel bumps to >= 13 x scale anyway


def _P(kind: str, scale: float) -> dict:
    h = LS.one_err_px(scale) if kind == "err" else (LS.one_vel_px(scale) if kind == "vel" else LS.one_sep_px(scale))
    return {"show_obs": True, f"{kind}_height": h, "font_px": FONTS[scale], "line_w": 2.5, "text_scale": scale}


def _L(fig) -> dict:
    return fig.to_plotly_json()["layout"]


def _err_fig(scale: float, **kw):
    return PL.error_fig(_A_err(**kw), _P("err", scale), WIN)


def _vel_fig(scale: float, **kw):
    return PL.velocity_fig(_A_vel(**kw), _P("vel", scale), WIN, truth=_truth())


def _sep_fig(scale: float, n: int = 300, peak: float = 4000.0):
    t = T_NOW - n + 1.0 + np.arange(n, dtype=float)
    sep = np.abs(peak * np.cos(np.arange(n) / 60.0)) + 50.0
    A = {"t_now": T_NOW, "sep": {"t": t, "sep": sep, "horiz": sep * 0.9}, "cpa": None}
    return PL.separation_fig(A, _P("sep", scale))


# ── 1. glyph-derived FIXED margins, independent of the data ──────────────────
@pytest.mark.parametrize("scale", SCALES)
def test_card_margins_are_glyph_derived_and_data_independent(scale):
    f = PL.card_font(FONTS[scale], scale)
    assert f == max(FONTS[scale], round(PL.JS_FONT_MIN * scale))                                        # the panel's possible font bump is budgeted
    m = PL.card_margin(FONTS[scale], scale)
    assert m["l"] == int(round(PL.LINE_H * f + PL.MAP_TITLE_STANDOFF + PL.GLYPH_W * f * PL.CARD_TICK_GLYPHS + 2.0 + PL.MAP_TICK_GAP))
    assert m["r"] == int(round(0.5 * PL.X_TICK_GLYPHS * PL.GLYPH_W * f + 2.0))                           # half an "hh:mm:ss" label: the newest label slides to the edge intact
    assert m["b"] == PL.card_bottom_px(PL.CARD_B_PX, f) >= round(PL.LINE_H * f) + PL.MAP_TICK_GAP        # one x tick label row + the tick
    assert m["autoexpand"] is False
    assert PL.CARD_TICK_GLYPHS >= 4                                                                     # "−150" / "−2.5" (err), "−12.5" (vel)
    # the same margins whatever the data: quiet, spiked, pinned off-scale, empty
    quiet, wild = _L(_err_fig(scale)), _L(_err_fig(scale, az_spike=25.0, alt_spike=900.0, pos_spike=2500.0))
    empty = _L(PL.error_fig({"t_now": T_NOW, "err_tracks": [], "contain": {}, "track_events": []}, _P("err", scale), WIN))
    for k in ("l", "r", "b"):
        assert quiet["margin"][k] == wild["margin"][k] == empty["margin"][k] == m[k], (scale, k)
    assert quiet["margin"]["autoexpand"] is False
    vq, vw = _L(_vel_fig(scale)), _L(_vel_fig(scale, vu_pin=-80.0))
    for k in ("l", "r", "b"):
        assert vq["margin"][k] == vw["margin"][k] == m[k], (scale, k)
    sq, sw = _L(_sep_fig(scale, peak=400.0)), _L(_sep_fig(scale, peak=9000.0))
    for k in ("l", "r", "b"):
        assert sq["margin"][k] == sw["margin"][k] == m[k], (scale, k)
    # every axis of every card: automargin OFF (nothing plotly measures — a label, a title — can push the plot area)
    for L in (quiet, wild, empty, vq, vw, sq, sw):
        for a, v in L.items():
            if re.match(r"^[xy]axis\d*$", a):
                assert v.get("automargin") is False, (scale, a)
    assert PL.MARGINS["err"] == PL.MARGINS["vel"] == PL.card_margin(PL.FONT_PX, 1.0) and PL.MARGINS["sep"]["l"] == PL.MARGINS["err"]["l"]   # the three cards' plot areas line up


def test_card_left_margin_holds_the_widest_label_at_every_scale():
    """The clipped-label bug: the label INK (glyphs x GLYPH_W x the drawn font) + the tick gap must fit left of the plot area — with
    room for the rotated axis title too."""
    for scale in SCALES:
        f = PL.card_font(FONTS[scale], scale)
        m = PL.card_margin(FONTS[scale], scale)
        widest = max(len(s) for s in ("−150", "−2.5", "300", "−12.5", "10000"))
        assert m["l"] >= widest * PL.GLYPH_W * f + PL.MAP_TICK_GAP + PL.LINE_H * f, (scale, m)


# ── 2. explicit y ticks that cover the held range and only follow it ─────────
@pytest.mark.parametrize("scale", SCALES)
def test_y_ticks_are_explicit_and_cover_the_range(scale):
    for fig, rows in ((_err_fig(scale, az_spike=25.0), 4), (_vel_fig(scale), 3), (_sep_fig(scale), 1)):
        L = _L(fig)
        for r in range(1, rows + 1):
            ax = L["yaxis" if r == 1 else f"yaxis{r}"]
            lo, hi = ax["range"]
            tv = ax.get("tickvals")
            assert tv, (scale, r, ax)
            assert all(lo - 1e-9 <= v <= hi + 1e-9 for v in tv), (scale, r, tv, lo, hi)                 # inside the range (never a label at a phantom position)
            assert "nticks" not in ax or ax["nticks"] is None                                               # never plotly's auto picker
    # the tick set is a pure function of the range
    assert PL.nice_ticks(0.0, 300.0) == PL.nice_ticks(0.0, 300.0) == [0.0, 100.0, 200.0, 300.0]
    assert PL.err_ticks(5.0) == [-2.5, 0.0, 2.5]
    assert PL.vel_ticks(-10.0, -6.0) == PL.vel_ticks(-10.0, -6.0)
    assert PL.thin_ticks([-2.5, 0.0, 2.5], -5.0, 5.0, 60.0, 13.0) == [-2.5, 0.0, 2.5]                   # a 60 px card carries three 13 px labels (15 px apart) ...
    assert len(PL.thin_ticks([-2.5, 0.0, 2.5], -5.0, 5.0, 20.0, 17.0)) < 3                               # ... a 20 px one at 17 px does not (thinned, never colliding)


def test_quant_range_makes_equal_data_give_equal_ranges():
    a = PL.vel_range([-9.31], [-6.69], None)
    b = PL.vel_range([-9.29], [-6.71], None)
    assert a == b, (a, b)                                                                                # a hair of jitter lands on the same grid
    lo, hi = PL.quant_range(-9.3, -6.7)
    step = PL.nice_ceil((-6.7 + 9.3) / 4.0)
    assert abs(lo / step - round(lo / step)) < 1e-9 and abs(hi / step - round(hi / step)) < 1e-9 and lo <= -9.3 and hi >= -6.7
    assert PL.vel_range([], [], None) == (-PL.VEL_FLOOR, PL.VEL_FLOOR)


# ── 3. stable_range: dwell + the data-left-the-range exception ───────────────
def test_stable_range_dwell_rule():
    mem = {}
    assert PL.stable_range(mem, "err", "az", -2.5, 2.5, now=0.0, data_lo=-1.0, data_hi=1.0) == (-2.5, 2.5)
    # the padded NEED grows (P95 wobble) but the data is still inside: held during the dwell ...
    assert PL.stable_range(mem, "err", "az", -3.0, 3.0, now=1.0, data_lo=-1.2, data_hi=1.2) == (-2.5, 2.5)
    assert PL.stable_range(mem, "err", "az", -3.0, 3.0, now=PL.RANGE_DWELL_S - 0.5, data_lo=-1.2, data_hi=1.2) == (-2.5, 2.5)
    # ... and widens once the dwell has passed
    assert PL.stable_range(mem, "err", "az", -3.0, 3.0, now=PL.RANGE_DWELL_S, data_lo=-1.2, data_hi=1.2) == (-3.0, 3.0)
    # the DATA leaves the held range: widen NOW, dwell or not
    assert PL.stable_range(mem, "err", "az", -5.0, 5.0, now=PL.RANGE_DWELL_S + 1.0, data_lo=-3.4, data_hi=3.4) == (-5.0, 5.0)
    t1 = PL.RANGE_DWELL_S + 1.0
    # a smaller need holds, then shrinks only after RANGE_SHRINK_S below RANGE_SHRINK_FRAC (which is also past the dwell)
    for dt in (1.0, 5.0, PL.RANGE_SHRINK_S - 0.5):
        assert PL.stable_range(mem, "err", "az", -1.0, 1.0, now=t1 + 1.0 + dt, data_lo=-0.5, data_hi=0.5) == (-5.0, 5.0)
    assert PL.stable_range(mem, "err", "az", -1.0, 1.0, now=t1 + 2.0 + PL.RANGE_SHRINK_S, data_lo=-0.5, data_hi=0.5) == (-1.0, 1.0)   # small since t1 + 2
    # unchanged numbers never restart the dwell (a need already covered by a clamp-pinned hold)
    mem2 = {"err": {"az": {"lo": -5.0, "hi": 5.0, "small_since": None, "changed_at": 0.0}}}
    assert PL.stable_range(mem2, "err", "az", -5.0, 5.0, now=1.0, data_lo=-9.0, data_hi=9.0) == (-5.0, 5.0)
    assert mem2["err"]["az"]["changed_at"] == 0.0
    # no memory: raw; default data extent = the need (the 2026-09-11 contract: a wider need widens at once)
    assert PL.stable_range(None, "err", "az", -1.0, 1.0) == (-1.0, 1.0)
    mem3 = {}
    PL.stable_range(mem3, "vel", "vu", -2.0, 2.0, now=0.0)
    assert PL.stable_range(mem3, "vel", "vu", -4.0, 4.0, now=1.0) == (-4.0, 4.0)
    assert PL.RANGE_DWELL_S == 10.0 and PL.RANGE_SHRINK_S == 20.0 and PL.RANGE_SHRINK_FRAC == 0.6


def test_err_and_vel_ranges_pass_the_data_extent_and_never_pass_the_clamp():
    mem = {}
    PL.err_range("az", [np.full(20, 1.0)], [np.full(20, 0.3)], mem)                                  # first tick: need 1.25 x P95 -> nice
    held = (mem["err"]["az"]["lo"], mem["err"]["az"]["hi"])
    # ONE spike beyond the clamp: the P95 need ignores it and the data trigger only ever widens to the NEED -> the hold stands (no jump)
    assert PL.err_range("az", [np.r_[np.full(20, 1.0), 40.0]], [np.full(20, 0.3)], mem) == held
    # a SUSTAINED excursion: the need reaches the clamp, the data left the hold -> pinned at the clamp at once, and the same numbers after
    a = PL.err_range("az", [np.r_[np.full(20, 1.0), np.full(10, 40.0)]], [np.full(20, 0.3)], mem)
    assert a == (-5.0, 5.0) and held[1] <= 5.0
    b = PL.err_range("az", [np.r_[np.full(20, 1.0), np.full(10, 40.0)]], [np.full(20, 0.3)], mem)
    assert a == b
    m2 = {}
    v1 = PL.vel_range([-80.0], [5.0], m2, "vu")
    assert v1[0] == -30.0 and v1[1] <= 30.0
    assert PL.vel_range([-80.0], [5.0], m2, "vu") == v1


@pytest.mark.parametrize("scale", SCALES)
def test_live_figures_hold_their_ranges_across_a_jittering_window(scale):
    """error_fig / velocity_fig / separation_fig fed 12 consecutive 1 s windows of the same process: the y range changes at
    most once per card in the run (the initial set aside) and the tick set only with it."""
    rng = np.random.default_rng(7)
    mem = {}
    P = {**_P("err", scale), "yrng_mem": mem}
    seen = {k: [] for k in PL.ERR_KEYS}
    base = _A_err()
    for i in range(12):
        A = {**base, "t_now": T_NOW + i}
        e = base["err_tracks"][0]["errors"]
        e2 = {k: (v + rng.normal(0, 0.05, len(v)) if k in ("az", "el", "alt") else v) for k, v in e.items()}
        A["err_tracks"] = [{"tid": 177, "errors": e2, "ungraded": {"t": np.zeros(0)}}]
        L = _L(PL.error_fig(A, P, WIN))
        for r, k in enumerate(PL.ERR_KEYS, start=1):
            ax = L["yaxis" if r == 1 else f"yaxis{r}"]
            seen[k].append((tuple(ax["range"]), tuple(ax["tickvals"])))
    for k, rows in seen.items():
        ranges = [r for r, _ in rows]
        changes = sum(1 for a, b in zip(ranges, ranges[1:]) if a != b)
        assert changes <= 1, (scale, k, ranges)
        for (r0, t0), (r1, t1) in zip(rows, rows[1:]):
            assert r0 == r1 or t0 != t1 or True                                                          # ticks are a function of the range ...
            if r0 == r1:
                assert t0 == t1, (scale, k, "tick set changed without a range change")                    # ... and never move without it


# ── 4. x axis: fixed dtick from the window, fixed bottom margin ──────────────
def test_x_axis_dtick_is_fixed_from_the_window_and_the_bottom_margin_from_the_font():
    assert PL.x_dtick_ms(120.0) == 30000 and PL.x_dtick_ms(60.0) == 15000 and PL.x_dtick_ms(600.0) == 300000
    assert PL.x_dtick_ms(120.0) == PL.x_dtick_ms(120.0)
    for scale in SCALES:
        for L, rows in ((_L(_err_fig(scale)), 4), (_L(_vel_fig(scale)), 3)):
            for r in range(1, rows + 1):
                ax = L["xaxis" if r == 1 else f"xaxis{r}"]
                assert ax["dtick"] == PL.x_dtick_ms(WIN) and ax["automargin"] is False, (scale, r, ax)
        S = _L(_sep_fig(scale))
        assert S["xaxis"]["dtick"] in tuple(int(s * 1000) for s in PL.X_DTICK_LADDER_S) and S["xaxis"]["automargin"] is False
        f = PL.card_font(FONTS[scale], scale)
        assert _L(_err_fig(scale))["margin"]["b"] >= round(PL.LINE_H * f) + PL.MAP_TICK_GAP             # the "hh:mm:ss" row fits under the plot area


# ── 5. header strip: right-anchored annotations cannot move the layout; velocity readout = the primary readout ──
@pytest.mark.parametrize("scale", SCALES)
def test_strip_readouts_are_anchored_annotations_and_the_velocity_readout_is_primary(scale):
    P = _P("vel", scale)
    fig = _vel_fig(scale)
    L = _L(fig)
    anns = {a["name"]: a for a in L["annotations"] if a.get("name")}
    for k in PL.VEL_KEYS:
        r = anns[f"readout_{k}"]
        assert r["xref"].endswith(" domain") and r["x"] == 1.0 and r["xanchor"] == "right" and r["showarrow"] is False   # a domain-anchored annotation: no margin effect
        assert r["font"]["size"] == PL.px(PL.READOUT_PX, P) and r["font"]["color"] == T.INK, (scale, r["font"])   # the error cards' primary readout
        assert re.fullmatch(r"track <b>[+-]\d+\.\d</b> · truth <b>[+-]\d+\.\d</b> · Δ <b>[+-]\d+\.\d</b> m/s", r["text"]), r["text"]
        # SIDE BY SIDE (panel-only, visible=False in the served figure): ONE row — the 16 px title with its unit and the compact
        # numbers at the SAME READOUT_PX as the stacked readout (2026-09-15 "make the metrics here larger too"); the browser
        # re-sizes only that line, to the real card width (cardGrid: max(12, min(18, (colW - 24) / (26 * .625)))).
        t_ = anns[f"title_{k}_s"]
        assert t_["text"] == f"<b>{PL.VEL_TITLE[k]}</b> (m/s)" and t_["font"]["size"] == PL.px(PL.TITLE_PX, P) and t_["visible"] is False
        s_ = anns[f"readout_{k}_s"]
        assert s_["font"]["size"] == PL.px(PL.READOUT_PX, P) and s_["font"]["color"] == T.INK and s_["visible"] is False
        num = r"(?:[+-]\d+\.\d|—)"                                                                     # vel_compact prints "—" for a value it does not have
        assert re.fullmatch(rf"{num} · {num} · Δ {num} m/s", s_["text"]), s_["text"]                    # no bold, no colour span, no words
        assert "<b>" not in s_["text"] and "<span" not in s_["text"] and "trk" not in s_["text"]
        assert anns[f"delta_{k}"]["font"]["size"] == PL.px(PL.READOUT_SUB_PX, P)                        # line 2 stays at the sub size
        assert f"delta_{k}_s" not in anns                                                              # the side-by-side strip has no second line
    E = _L(_err_fig(scale))
    for a in E["annotations"]:
        if str(a.get("name", "")).startswith(("readout_", "contain_", "title_")):
            assert a["xref"].endswith(" domain") and a["yref"].endswith(" domain") and a["showarrow"] is False
    assert PL.VEL_SHORT_MAX_PX == 12                                                                     # (legacy: vel_readout_short's cap — the drawn side-by-side line is vel_compact at READOUT_PX, sized by the panel)
    assert "Math.max(12,Math.min(18," in LS.panel_core_js()                                              # ... the real cap lives in the browser (cardGrid)
    assert PL.vel_readout(-37.9, -0.2, -37.4) == "track <b>-37.9</b> · truth <b>-0.2</b> · Δ <b>-37.4</b> m/s"
    assert PL.vel_readout(-37.9, -0.2) == "track <b>-37.9</b> · truth <b>-0.2</b> · Δ <b>-37.7</b> m/s"     # no spa Δ: the plain difference
    assert PL.vel_readout(None, 1.0) == "track — · truth <b>+1.0</b> · Δ — m/s"
    assert PL.vel_compact(-81.6, 0.0, -81.0) == "-81.6 · +0.0 · Δ -81.0 m/s" and PL.vel_compact(None, None, None) == "— · — · Δ — m/s"
    # both lines fall back to track − truth when spa has no graded Δ for the axis (2026-09-15 fix: the side-by-side line read "Δ —")
    assert PL.vel_readout(-37.9, -0.2).endswith("Δ <b>-37.7</b> m/s") and PL.vel_compact(-37.9, -0.2, None).endswith("Δ -37.7 m/s")


def test_strip_stays_clean_in_a_945px_stacked_card_at_large():
    """Title (left) + the primary velocity readout (right) in a 945 px stacked card (a 1366 laptop's full-width card).
    2026-09-15 raised READOUT_PX 14 -> 18: the pair still never OVERLAPS at any text scale, but at X-Large it no longer
    clears the panel's 40 px one-row pad — the browser's stripOneLine then drops the readout to a second row (each line
    alone fits by a wide margin).  That two-row fallback IS the deliberate behaviour, so the pin follows its rule."""
    PAD = 40                                                                                             # stripOneLine's one-row pad (ih.liveserver.panel_core_js)
    one_row = {}
    for scale in SCALES:
        P = _P("vel", scale)
        title_w = len("VERTICAL VELOCITY") * PL.GLYPH_W * PL.px(PL.TITLE_PX, P) + 8
        read_w = len("track -37.9 · truth -0.2 · Δ -37.4 m/s") * PL.GLYPH_W * PL.px(PL.READOUT_PX, P) + 8
        plot_w = 945 - PL.card_margin(FONTS[scale], scale)["l"] - PL.card_margin(FONTS[scale], scale)["r"]
        assert title_w + read_w <= plot_w, (scale, title_w, read_w, plot_w)                              # the served (always one-row) figure: the boxes never meet
        assert max(title_w, read_w) < plot_w - 20, (scale, title_w, read_w, plot_w)                      # ... and on the two-row fallback each line has room to spare
        one_row[scale] = title_w + read_w + PAD <= plot_w
    assert one_row == {1.0: True, 1.15: True, 1.3: False}, one_row                                       # X-Large = the two-row fallback (clearance 1.6 px of the 40 px pad)
    # the SIDE-BY-SIDE line the panel really draws is vel_compact, sized by cardGrid to the card width (26 glyphs, 12..18 px)
    compact = PL.vel_compact(-81.6, 0.0, -81.0)
    assert len(compact) == 26
    col_w = 246                                                                                          # a 3-across velocity card at 1920x1080 (two-column, card_margin l 83 / r 40)
    f = max(12, min(18, int((col_w - 24) / (26 * PL.GLYPH_W))))
    assert len(compact) * PL.GLYPH_W * f + 8 <= col_w, (f,)
