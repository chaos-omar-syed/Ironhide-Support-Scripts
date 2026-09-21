"""MEASUREMENT SPACE quad — static layout QA of the in-panel tags (owner: ih.plots meas_fig), 2026-09-15.

The quad is the user's secondary display and its worst laptop finding was text on text: the start / end "#id" pills and
the "→ interceptor (stolen)" words were placed against an ESTIMATE of the panel's pixels (MEAS_PANEL_PX 380,
MEAS_LEGEND_PUSH_PX 90) while the real panel measures 382–961 x 125–158 px depending on the window and the sidebar
"Text size".  clip_audit (2026-09-14/15) therefore reported, at EVERY size, "#129" pushed 30–53 px OUTSIDE its panel,
plus "#177 <> #200" / "#200 <> #200" printed on top of each other and "→ departed" 3–4 px past the panel at X-Large.

The placement is now deterministic:
  * every decision is taken against a measured BOUND (MEAS_PANEL_MIN_W / MEAS_PANEL_MIN_H / MEAS_PLOT_MIN_H), so it can
    only ever be wrong in the direction that leaves MORE room;
  * pills that overlap horizontally in the NARROWEST panel form one time cluster and are stacked on ONE side of their
    markers (below by preference), tops strictly monotone -> they cannot overlap, and the order survives any scaling;
  * a tag that still does not fit inside the panel is DROPPED (visible False), never drawn outside it;
  * the departure words live in the panel's HEADER STRIP (x/y domain 1.0, above the plot area), thinned to what fits
    beside the panel title in the narrowest panel;
  * y ticks are explicit and thinned like the error cards (thin_ticks) for the shortest panel we support.

These checks mirror the browser findings with the glyph metrics the other static tests use (PL.GLYPH_W 0.625,
PL.LINE_H / PL.ERR_BOX_LH 1.35) at the laptop iframe widths 945 / 1115 and 1500, and at the text scales 1.0 / 1.15 /
1.3, including SCROLL STABILITY (build at t, t+1 s, t+2 s: every annotation keeps its slot).

Run:
  IH_LIVE_PORT=8926 python -m pytest -q tests/test_meas_quad_layout.py
"""
from __future__ import annotations

import logging
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("IH_LIVE_PORT", "8926")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
logging.disable(logging.WARNING)

import ih.data as D  # noqa: E402
import ih.plots as PL  # noqa: E402
import ih.theme as T  # noqa: E402

T_NOW = D.hms_to_epoch("07:22:40")
WIN = 120.0
WIDTHS = (945, 1115, 1500)          # the laptop iframe at 1366x768 / 1536x864 and the 1920x1080 one
SCALES = (1.0, 1.15, 1.3)           # sidebar "Text size" Normal / Large / X-Large
FONT_OF = {1.0: 14, 1.15: 16, 1.3: 18}   # ih.liveserver.layout: FONT_PX["Desktop 1080p"] 14 x the scale (measured in the iframe)


# ── the panel geometry a width implies (the same arithmetic ih.plots reasons with, for the assertions only) ──
def panel_w(iframe_px: int) -> float:
    """One quad panel's plot width at an iframe ``iframe_px`` wide: (W − l − r) × (1 − horizontal_spacing) / 2."""
    return (float(iframe_px) - PL.MEAS_MARGIN["l"] - PL.MEAS_MARGIN["r"]) * (1.0 - 0.06) / 2.0


def _P(scale: float, iframe_px: int) -> dict:
    return {"font_px": FONT_OF[scale], "text_scale": scale, "line_w": 2.5, "meas_height": PL.MEAS_H, "_w": iframe_px}


# ── synthetic M: two target-side tracks that end at nearly the same time and value (the collided pair), one
#    interceptor-side track, one stolen track that carries on (the departure), truths on both sides ──
def _series(t: np.ndarray, rng_km_0: float, slope: float) -> dict:
    n = len(t)
    return {"t": t, "rng": 1000.0 * (rng_km_0 + slope * (t - t[0]) / 60.0), "rr": -40.0 + 0.1 * np.arange(n, dtype=float),
            "az": 120.0 + 0.02 * np.arange(n, dtype=float), "el": 5.0 + 0.01 * np.arange(n, dtype=float)}


def _M(t_now: float = T_NOW, n_close: int = 2, note: bool = True) -> dict:
    t_lo = t_now - WIN
    t = t_lo + 1.0 + np.arange(WIN - 1, dtype=float)
    truth = _series(t, 3.2, 0.30)
    truth_itc = _series(t, 3.6, -0.25)
    tracks = []
    # #129 / #200: both END within a second of each other at nearly the same range -> one time cluster (the "#177 <> #200" case)
    for k, tid in enumerate((129, 200)[:n_close]):
        tt = t[: int(len(t) * 0.55) + k]
        tr = _series(tt, 3.2 + 0.002 * k, 0.30)
        tr.update(tid=tid, role="target", on=np.ones(len(tt), bool), on_tgt=np.ones(len(tt), bool),
                  on_itc=np.zeros(len(tt), bool), kind=np.full(len(tt), "meas", object))
        tracks.append(tr)
    # #177: target-side, then STOLEN by the interceptor and carrying on -> "→ interceptor (stolen)"
    tr = _series(t, 3.21, 0.30)
    on_t = np.zeros(len(t), bool)
    on_t[: int(len(t) * 0.7)] = True
    tr.update(tid=177, role="target", on=on_t, on_tgt=on_t, on_itc=~on_t, kind=np.full(len(t), "meas", object))
    tracks.append(tr)
    # #203: the interceptor's own track (a BOTTOM-side identity)
    tt = t[int(len(t) * 0.3):]
    tr = _series(tt, 3.6, -0.25)
    tr.update(tid=203, role="interceptor", on=np.ones(len(tt), bool), on_tgt=np.zeros(len(tt), bool),
              on_itc=np.ones(len(tt), bool), kind=np.full(len(tt), "meas", object))
    tracks.append(tr)
    return {"t_lo": t_lo, "t_now": t_now, "truth": truth, "truth_itc": truth_itc, "tracks": tracks, "obs": None, "obs_rr": False,
            "geom": {"colocated": True, "note": "TX 91 / RX 91 co-located → bistatic = 2×mono"} if note else {}}


def _A(events=()) -> dict:
    return {"t_now": T_NOW, "cpa": None, "track_events": list(events)}


def _fig(scale: float, iframe_px: int, t_now: float = T_NOW, **kw):
    return PL.meas_fig(_M(t_now=t_now, **kw), _A(), _P(scale, iframe_px))


def _ann(fig) -> list[dict]:
    return fig.to_plotly_json()["layout"].get("annotations", [])


def _named(fig, *prefixes) -> list[dict]:
    return [a for a in _ann(fig) if str(a.get("name") or "").startswith(prefixes)]


def _shown(fig, *prefixes) -> list[dict]:
    return [a for a in _named(fig, *prefixes) if a.get("visible", True)]


def _axes(fig) -> dict:
    L = fig.to_plotly_json()["layout"]
    return {k: L[k] for k in ("yaxis", "yaxis2", "yaxis3", "yaxis4")}


def _xf(x, t_lo: float, t_now: float) -> float:
    """A plotly x value (the figure's own local-time strings) as a fraction of the window — read off the window's own
    ends, so the test never has to know the display time zone."""
    a, b, v = (np.datetime64(str(PL._dt(t_lo)), "ms"), np.datetime64(str(PL._dt(t_now)), "ms"), np.datetime64(str(x), "ms"))
    return float((v - a).astype("int64")) / max(1.0, float((b - a).astype("int64")))


def _box(a: dict, rng: tuple, ph: float, pw: float, t_lo: float, t_now: float) -> tuple[float, float, float, float]:
    """An in-panel annotation's box in PANEL PIXELS (x right, y down from the panel's top) for a panel ``pw`` x ``ph``."""
    f = float(a["font"]["size"])
    w, h = PL.tag_w(a["text"], f), PL.tag_h(f)
    x = _xf(a["x"], t_lo, t_now) * pw + float(a.get("xshift", 0) or 0)
    x0 = x - w if a.get("xanchor") == "right" else x
    p = (float(rng[1]) - float(a["y"])) / (float(rng[1]) - float(rng[0])) * ph
    ys = float(a.get("yshift", 0) or 0)
    y0 = (p - ys - h) if a.get("yanchor") == "bottom" else (p - ys)
    return (x0, x0 + w, y0, y0 + h)


# ── 1. the bounds are BOUNDS, and the estimates are gone ─────────────────────
def test_geometry_bounds_replace_the_panel_pixel_estimates():
    assert not hasattr(PL, "MEAS_PANEL_PX") and not hasattr(PL, "MEAS_LEGEND_PUSH_PX")     # the 2026-09-14 point estimates
    assert (PL.GLYPH_W, PL.LINE_H, PL.ERR_BOX_LH) == (0.625, 1.35, 1.35)                    # the glyph metrics every static test shares
    # every bound is BELOW the smallest panel the browser actually draws (clip_audit probe 2026-09-15: 382 x 125, plot area 283)
    assert PL.MEAS_PANEL_MIN_W <= 382.0 and PL.MEAS_PANEL_MIN_H <= 125.0 and PL.MEAS_PLOT_MIN_H <= 283.0
    # ... and below the narrowest panel the supported widths imply
    assert PL.MEAS_PANEL_MIN_W <= min(panel_w(w) for w in WIDTHS)
    assert (PL.MEAS_TAG_GAP, PL.MEAS_TAG_STAND, PL.MEAS_TAG_ROWS, PL.MEAS_DEP_MAX) == (2.0, 4.0, 3, 2)
    assert PL.tag_h(12) == pytest.approx(PL.ERR_BOX_LH * 12 + PL.ERR_BOX_PAD)
    assert PL.tag_w("#177", 13) == pytest.approx(PL.GLYPH_W * 13 * 4 + 6.0)
    assert PL.tag_w("<b>X</b>", 13) == PL.tag_w("X", 13)                                     # markup is not glyphs


# ── 2. the declutter itself: non-overlap, containment, monotone slots ────────
def _recs(ys: list[float], font_px: float = 13.0, t0: float = 10.0, dt: float = 0.2) -> list[dict]:
    return [PL._tag_rec(f"p{i}", f"#{100 + i}", font_px, t0 + i * dt, y, 0.0, 100.0, (0.0, 10.0), xshift=10, prio=i)
            for i, y in enumerate(ys)]


@pytest.mark.parametrize("scale", SCALES)
def test_stack_annotations_never_overlaps_and_never_leaves_the_panel(scale):
    pill = PL.px(PL.PILL_PX, {"text_scale": scale})
    for ys in ([5.0, 5.0], [5.0, 4.9, 4.8], [9.8, 9.7], [0.3, 0.2], [5.0, 5.0, 5.0, 5.0]):
        recs = _recs(ys, pill)
        slots = PL.stack_annotations(recs)
        assert set(slots) == {r["name"] for r in recs}
        boxes = []
        for r in recs:
            sl = slots[r["name"]]
            assert (sl["yshift"] > 0) == (sl["yanchor"] == "bottom")          # the sign rule the pills are asserted on
            if sl["drop"]:
                continue
            p = r["yf"] * PL.MEAS_PANEL_MIN_H
            top = (p - sl["yshift"] - r["h"]) if sl["yanchor"] == "bottom" else (p - sl["yshift"])
            assert top >= -1e-9 and top + r["h"] <= PL.MEAS_PANEL_MIN_H + 1e-9, (ys, r["name"], top)
            x0, x1 = PL._x_span(r, PL.MEAS_PANEL_MIN_W)
            boxes.append((x0, x1, top, top + r["h"]))
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                assert not (min(a[1], b[1]) - max(a[0], b[0]) > 0 and min(a[3], b[3]) - max(a[2], b[2]) > 0), (ys, a, b)
        assert any(not slots[r["name"]]["drop"] for r in recs)                 # something always survives


def test_stack_annotations_clears_the_edge_anchored_obstacles():
    """The geometry note (bottom band, full width), the off-scale ▲ (top band) and the handover ▾ are pinned a fixed
    number of pixels from an edge at ANY panel height — a pill has to clear them, which is the one clearance decision
    the server can take exactly."""
    note = PL._tag_rec("geom_note", "TX 91 / RX 91 co-located → bistatic = 2×mono", 11, 0.0, 0.0, 0.0, 100.0, None,
                       band="bottom", full_width=True)
    mark = PL._tag_rec("handover_tag_target_177", "▾", 11, 50.0, 0.0, 0.0, 100.0, None, band="top")
    for y, obstacle in ((0.05, mark), (9.95, note)):                           # a pill right on top of / right under an obstacle
        rec = PL._tag_rec("lbl_start_1_rng", "#129", 13, 50.0, y, 0.0, 100.0, (0.0, 10.0), xshift=10)
        sl = PL.stack_annotations([obstacle, rec])["lbl_start_1_rng"]
        if sl["drop"]:
            continue
        p = rec["yf"] * PL.MEAS_PANEL_MIN_H
        top = (p - sl["yshift"] - rec["h"]) if sl["yanchor"] == "bottom" else (p - sl["yshift"])
        b0, b1 = PL._edge_bands([obstacle], PL.MEAS_PANEL_MIN_H)[0]
        assert not (top < b1 and top + rec["h"] > b0), (y, top, (b0, b1))


# ── 3. the figure: nothing in the plot area overlaps, nothing leaves its panel ──
@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_quad_in_panel_tags_are_inside_their_panel_and_clear_of_each_other(w, scale):
    fig = _fig(scale, w)
    M = _M()
    rng = tuple(_axes(fig)["yaxis"]["range"])
    pw, ph = panel_w(w), PL.MEAS_PANEL_MIN_H
    pills = _shown(fig, "lbl_start_", "lbl_end_")
    assert pills, (w, scale)
    boxes = [(a["name"], _box(a, rng, ph, pw, M["t_lo"], M["t_now"])) for a in pills]
    for nm, b in boxes:
        assert b[2] >= -1.0 and b[3] <= ph + 1.0, (w, scale, nm, b)            # inside the SHORTEST panel -> inside every real one
        assert b[0] >= -1.0 and b[1] <= pw + 1.0, (w, scale, nm, b)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (na, a), (nb, b) = boxes[i], boxes[j]
            ox, oy = min(a[1], b[1]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[2], b[2])
            assert not (ox > 0 and oy > 0), (w, scale, na, nb, round(ox, 1), round(oy, 1))


@pytest.mark.parametrize("scale", SCALES)
def test_pills_keep_the_contract_the_other_static_tests_assert(scale):
    """Shape of a pill annotation (tests/test_apptest.py): data-space refs, 13 px x scale mono primary ink on a CARD
    pill with a RULE border, ±10 px x shift, and the yshift sign tied to the yanchor."""
    fig = _fig(scale, 945)
    pills = _named(fig, "lbl_start_", "lbl_end_")
    assert pills and all(a["name"].endswith("_rng") for a in pills)             # the pills live on the FIRST panel only
    for a in pills:
        assert a["xref"] == "x" and a["yref"] == "y"
        assert a["font"] == {"family": T.MONO, "size": PL.px(PL.PILL_PX, {"text_scale": scale}), "color": T.INK}
        assert a["bgcolor"] == T.CARD and a["bordercolor"] == T.RULE and a["borderpad"] == 2
        assert a["xanchor"] in ("left", "right") and abs(a["xshift"]) == 10 and a["yanchor"] in ("bottom", "top")
        assert (a["yshift"] > 0) == (a["yanchor"] == "bottom")


# ── 4. the departure words: in the HEADER STRIP, never in the plot area ─────
@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_departed_tags_sit_in_the_header_strip_above_the_plot_area(w, scale):
    fig = _fig(scale, w)
    dep = _shown(fig, "departed_")
    assert dep, (w, scale)
    for a in dep:
        assert a["xref"].endswith("x domain") or a["xref"].startswith("x"), a
        assert a["xref"].endswith(" domain") and a["yref"].endswith(" domain"), a
        assert (a["x"], a["xanchor"], a["y"], a["yanchor"]) == (1.0, "right", 1.0, "bottom"), a     # the strip: above the plot area
        assert a["yshift"] > 0 and a["xshift"] <= 0 and a["text"].startswith("→ ")
        assert a["font"]["color"] == T.INK3 and a["font"]["size"] == PL.px(11, {"text_scale": scale})
    small = PL.px(11, {"text_scale": scale})
    title = PL.px(PL.MEAS_TITLE_PX, {"text_scale": scale})
    for key in PL.MEAS_KEYS:                                                    # ... and every one of them clears the panel title
        tags = [a for a in dep if a["name"].endswith("_" + key)]
        assert len(_named(fig, "departed_")) >= len(tags)                       # a departure that does not fit stays an (invisible) annotation
        assert len(tags) <= PL.MEAS_DEP_MAX
        for a in tags:
            left = PL.MEAS_PANEL_MIN_W + a["xshift"] - PL.tag_w(a["text"], small)
            assert left >= PL.tag_w(PL.MEAS_TITLE[key], title), (key, a["name"], left)
    # two departures in one panel are laid out right-to-left without touching
    laid = PL.strip_tags([(1.0, "d1", "→ departed"), (2.0, "d2", "→ departed")], 11, "TIME", 12)
    assert len(laid) == 2 and laid[0][2] > laid[1][2]
    assert laid[1][2] + PL.tag_w(laid[1][1], 11) <= laid[0][2]                  # the newer tag is to the right of the older


def test_departed_words_are_thinned_not_stacked_out_of_the_strip():
    """More departures than fit beside the title are DROPPED (their ▶ marker + hover keep the event) — the strip never
    grows a second row and never runs under the panel title."""
    items = [(float(i), f"d{i}", "→ interceptor (stolen)") for i in range(5)]
    out = PL.strip_tags(items, 14, "<b>BISTATIC RANGE (KM)</b>", 16)
    assert len(out) <= PL.MEAS_DEP_MAX
    assert [n for n, _, _ in out] == [f"d{i}" for i in range(4, 4 - len(out), -1)]   # newest first
    assert PL.strip_tags([], 11, "X", 12) == []


# ── 5. tick labels: explicit and thinned like the error cards ───────────────
@pytest.mark.parametrize("scale", SCALES)
def test_y_ticks_are_explicit_and_thinned_for_the_shortest_panel(scale):
    fig = _fig(scale, 945)
    font = max(FONT_OF[scale], PL.JS_FONT_MIN)
    for ax, key in (("yaxis", "rng"), ("yaxis2", "rr"), ("yaxis3", "az"), ("yaxis4", "el")):
        v = _axes(fig)[ax]
        lo, hi = float(v["range"][0]), float(v["range"][1])
        vals = list(v["tickvals"])
        assert vals and vals == PL.meas_ticks(lo, hi, PL.MEAS_PANEL_MIN_H, font), (scale, key)
        assert all(lo - 1e-9 <= x <= hi + 1e-9 for x in vals), (scale, key, vals)
        for a, b in zip(vals, vals[1:]):                                        # neighbouring labels read apart in the SHORTEST panel
            assert abs(b - a) / (hi - lo) * PL.MEAS_PANEL_MIN_H >= PL.TICK_SEP_FRAC * font - 1e-6, (scale, key, vals)
    # thin_ticks is the error cards' rule, used unchanged
    assert PL.meas_ticks(0.0, 10.0, 40.0, 18.0) == PL.thin_ticks(PL.vel_ticks(0.0, 10.0), 0.0, 10.0, 40.0, 18.0)


# ── 6. the key never sits on the title strip ────────────────────────────────
@pytest.mark.parametrize("scale", SCALES)
def test_legend_stands_off_above_the_title_strip_against_the_plot_area_lower_bound(scale):
    L = _fig(scale, 945).to_plotly_json()["layout"]
    title = PL.px(PL.MEAS_TITLE_PX, {"text_scale": scale})
    assert L["legend"]["yanchor"] == "bottom" and L["legend"]["orientation"] == "h" and L["legend"]["x"] == 0.0
    want = 1.0 + (PL.tag_h(title) + 6.0) / PL.MEAS_PLOT_MIN_H
    assert L["legend"]["y"] == pytest.approx(want)
    # the standoff in the SHORTEST real plot area still clears a title box (283 px measured; 358 px is the tallest)
    for plot_h in (283.0, 358.0):
        assert (L["legend"]["y"] - 1.0) * plot_h >= PL.tag_h(title) + 6.0
    assert L["margin"] == dict(PL.MEAS_MARGIN, t=PL.MEAS_TOP_PX)                # the figure's margin contract is unchanged


# ── 7. scroll stability: the window moves a second, nothing flips slot ─────
@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("scale", SCALES)
def test_annotation_slots_do_not_flip_flop_as_the_window_scrolls(w, scale):
    """Build the quad at t, t + 1 s and t + 2 s: every annotation keeps its side (xanchor / yanchor), keeps being drawn
    and keeps its row — the yshift may follow the data by less than one row, it may never jump a row (a pill that
    flip-flops between rows as the clock ticks is the "jitter" the dashboard rules forbid)."""
    row = PL.tag_h(PL.px(PL.PILL_PX, {"text_scale": scale})) + PL.MEAS_TAG_GAP
    frames = [{str(a.get("name")): a for a in _ann(_fig(scale, w, t_now=T_NOW + dt))} for dt in (0.0, 1.0, 2.0)]
    base = frames[0]
    common = set(base) & set(frames[1]) & set(frames[2])
    assert {n for n in common if n.startswith(("lbl_start_", "lbl_end_", "departed_", "title_"))}
    for nm in sorted(common):
        a0 = base[nm]
        for f in frames[1:]:
            a = f[nm]
            assert a.get("xanchor") == a0.get("xanchor"), nm
            assert a.get("yanchor") == a0.get("yanchor"), nm
            assert bool(a.get("visible", True)) == bool(a0.get("visible", True)), nm
            assert abs(float(a.get("yshift", 0) or 0) - float(a0.get("yshift", 0) or 0)) < row, (nm, a.get("yshift"), a0.get("yshift"))
            assert float(a.get("xshift", 0) or 0) == float(a0.get("xshift", 0) or 0), nm


def test_stack_annotations_is_a_pure_function_of_the_fractions():
    """The same records always give the same slots (no data-driven hysteresis, nothing derived from wall time) — and a
    record's fractions are clamped to the panel, so a sample exactly at the window edge cannot place a tag outside it."""
    recs = _recs([5.0, 4.9, 0.1, 9.9])
    assert PL.stack_annotations(recs) == PL.stack_annotations(list(reversed(recs)))
    edge = PL._tag_rec("e", "#1", 13, 400.0, 99.0, 0.0, 100.0, (0.0, 10.0))
    assert edge["xf"] == 1.0 and edge["yf"] == 0.0
    mid = PL._tag_rec("m", "#1", 13, 50.0, 5.0, 0.0, 100.0, None)
    assert mid["yf"] == 0.5 and mid["movable"] is True                          # an autoranged panel: pinned mid-panel


# ── 8. an empty / single-track window still builds ──────────────────────────
def test_quad_builds_with_one_track_and_without_a_geometry_note():
    fig = _fig(1.3, 945, n_close=1, note=False)
    assert not [a for a in _ann(fig) if a.get("name") == "geom_note"]
    assert _shown(fig, "lbl_start_")
    assert PL.stack_annotations([]) == {}
