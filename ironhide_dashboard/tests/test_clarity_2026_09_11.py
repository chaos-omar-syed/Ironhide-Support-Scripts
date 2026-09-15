"""2026-09-11 user asks: no flashing state frames, no unexplained blue ticks on the error cards, clearer labels / readouts,
cards sectioned, NO JITTER (axis ranges hold), satellite tile wide enough to zoom out."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ih import plots as PL, liveserver as LS, theme as T  # noqa: E402


def test_state_frames_are_off_and_cards_are_quiet_but_sectioned():
    assert PL.TONE_ENABLED is False and PL.card_tone(5.0, 1.0, {"n": 50, "p1": 10.0}) == ("ok", "")   # a 5σ error raises nothing while the frames are off
    st = PL.card_shape_style("ok")
    assert st["line"]["color"] == PL.CARD_RULE == "#4a5160" and st["line"]["width"] == 1.5 and st["fillcolor"] == T.CARD   # a brighter, thicker frame does the sectioning
    assert PL.PANEL_GAP_PX == 10.0 and (PL.READOUT_PX, PL.READOUT_SUB_PX, PL.TITLE_PX) == (18, 12, 16)   # 2026-09-15: "make the metrics here larger too" (14 / 11 / 14 -> 18 / 12 / 16)
    assert LS.ERR_HDR_PX == PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0] == 52 and LS.ERR_GAP == 10 and LS.MIN_CARD_PX == 53   # strip 46 -> 52
    # the SECTIONING is the shaded header band (2026-09-15 "section off the headers more"): one hdr_<key> rect per card, from the
    # plot-area top to the strip top, filled HDR_BAND and layered BELOW the data — on top of the card frame, not instead of it
    assert PL.HDR_BAND == "rgba(255,255,255,0.045)"
    fig = PL.error_fig({"t_now": 0.0, "err_tracks": [], "contain": {}, "track_events": []}, {"err_height": 547, "font_px": 14, "line_w": 2.5}, 120.0)
    shapes = {str(s.get("name") or ""): s for s in fig.to_plotly_json()["layout"]["shapes"]}
    for key in PL.ERR_KEYS:
        card, band = shapes[f"card_{key}"], shapes[f"hdr_{key}"]
        assert band["type"] == "rect" and band["fillcolor"] == PL.HDR_BAND and band["layer"] == "below" and band["line"]["width"] == 0
        assert band["xref"] == band["yref"] == "paper" and band["y1"] == card["y1"] and band["y0"] < band["y1"]   # the strip's own band, flush with the card top
        assert card["fillcolor"] == T.CARD and card["line"]["color"] == PL.CARD_RULE                              # ... the frame still does the outer sectioning


def test_stable_range_widens_at_once_and_shrinks_only_after_a_quiet_period():
    mem = {}
    assert PL.stable_range(mem, "err", "az", -5, 5, now=0) == (-5, 5)
    assert PL.stable_range(mem, "err", "az", -8, 8, now=1) == (-8, 8)              # wider need: widen now
    assert PL.stable_range(mem, "err", "az", -2, 2, now=2) == (-8, 8)              # smaller need: hold ...
    assert PL.stable_range(mem, "err", "az", -2, 2, now=2 + PL.RANGE_SHRINK_S - 1) == (-8, 8)
    assert PL.stable_range(mem, "err", "az", -2, 2, now=2 + PL.RANGE_SHRINK_S) == (-2, 2)   # ... until it stayed small for the quiet period
    assert PL.stable_range(mem, "err", "az", -7, 7, now=30) == (-8, 8) or PL.stable_range(mem, "err", "az", -7, 7, now=30) == (-7, 7)
    assert PL.stable_range(None, "err", "az", -1, 1) == (-1, 1)                      # no memory: raw


def test_handover_ticks_on_the_error_cards_are_target_only():
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    ev = [{"t": 50.0, "role": "interceptor", "old": 1, "new": 2, "rule": "x"}, {"t": 60.0, "role": "target", "old": 3, "new": 4, "rule": "x"}]
    fig = make_subplots(rows=1, cols=1); fig.add_trace(go.Scatter(x=[PL._dt(0), PL._dt(100)], y=[0, 1]), row=1, col=1)
    assert PL.handover_marks(fig, ev, 0.0, 100.0, [(1, 1)], {(1, 1)}, None, roles=("target",)) == 1
    names = [sh.name for sh in fig.layout.shapes]
    assert names == ["handover_target_4"] and all(sh.line.color != T.INTERCEPTOR for sh in fig.layout.shapes)   # no blue interceptor tick on the target's cards
    assert PL.handover_marks(make_subplots(rows=1, cols=1), ev, 0.0, 100.0, [(1, 1)], set(), None) == 2         # the measurement quad still marks both roles


def test_satellite_tile_is_twice_the_view():
    assert LS.SAT_MARGIN == 0.5
    k = LS.sat_key((0.0, 1000.0, 0.0, 1000.0))                      # a 1 km view -> a 2 km fetch box (quantised): zooming out to half scale keeps imagery
    x0, x1, y0, y1 = (float(v) for v in k[:4])
    assert abs((x1 - x0) - 2000.0) <= 100 and abs((y1 - y0) - 2000.0) <= 100 and x0 <= -400 and x1 >= 1400
