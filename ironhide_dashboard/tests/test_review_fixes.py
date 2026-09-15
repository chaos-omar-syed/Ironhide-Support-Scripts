"""Regression tests for the 2026-09-10 fresh-eyes review fixes (each one a defect seen in a screenshot or the code):
sidebar slider values fractured per digit (our sidebar <p> wrap rule reached the slider's markdown <p>), Streamlit's
Deploy button over the build badge, header captions cut mid-word (now hidden whole + title), 10 px chrome on ultrawide
(header / chip fonts follow the measured geometry), identical x tick labels on the pre-flight separation card (1 s
span -> >= 60 s), invisible meas raw-obs circles (open symbols stroke with marker.color), a graded error whose σ is
missing / zero shown as "± 0" (now "σ n/a"; σ <= 0 -> NaN everywhere), a polling browser losing its STORE slot after
the hourly prune, the replay-speed clock jump, role_of('mavlink_14551'), and the Spec page's NaN guard.

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_review_fixes.py
"""
from __future__ import annotations

import os
import re
import sys
import time

import numpy as np

os.environ.setdefault("IH_LIVE_PORT", "8912")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import plotly.graph_objects as go  # noqa: E402

from ih import data as D  # noqa: E402
from ih import engine as E  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402


def _rules(css: str) -> list[tuple[str, str]]:
    body = css.split("<style>")[1].split("</style>")[0]
    body = re.sub(r"@import[^;]*;", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return [(sel.strip(), decl.strip()) for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body)]


def test_slider_value_paragraphs_are_pinned_after_the_sidebar_wrap_rule():
    """Streamlit 1.63 renders the thumb value as markdown (<p>) INSIDE the thumb; the sidebar "labels wrap" rule
    ([data-testid="stSidebar"] p { white-space:normal }) must never win over the slider pin (source order + !important)."""
    rules = _rules(T.CSS)
    sidebar_p = [i for i, (sel, _) in enumerate(rules) if '[data-testid="stSidebar"] p' in sel]
    pin = [i for i, (sel, decl) in enumerate(rules) if '[data-testid="stSliderThumbValue"] p' in sel and "white-space:nowrap !important" in decl]
    assert sidebar_p and pin, "both rules must exist"
    assert max(pin) > max(sidebar_p), "the slider pin must come AFTER the sidebar wrap rule"
    sel, decl = rules[pin[0]]
    assert "word-break:normal !important" in decl and "overflow-wrap:normal !important" in decl and "width:max-content" in decl
    assert all("stSlider" in part for part in sel.split(",")), sel            # the pin never reaches other text
    for need in ('[data-testid="stSliderTickBar"] p', '.stSlider [data-testid="stMarkdownContainer"] p'):
        assert need in sel


def test_streamlit_deploy_button_and_status_widget_hidden():
    assert any('[data-testid="stAppDeployButton"]' in sel and "display:none !important" in decl for sel, decl in _rules(T.CSS))
    assert "text-overflow" not in T.CSS                                                   # still no ellipsis cut mid-word
    # overflow:hidden is allowed ONLY on the fixed-height top bar rows (2026-09-14: the status strip wrapping/unwrapping moved the
    # whole panel 32-36 px every few seconds; the box is sized to hold its content and a browser test asserts it fits)
    for ln in T.CSS.splitlines():
        if "overflow:hidden" in ln:
            assert ".ih-top" in ln, ln


def test_panel_headers_hide_captions_whole_and_scale_fonts_with_geometry():
    body, css, js = LS.panel_body(), LS.panel_css(), LS.responsive_js()
    assert body.count('<span class="cap">') == 3 and body.count('<div class="h" title="') == 4   # map + velocity + err captions; every header titled (map · sep · velocity · error)
    assert "text-overflow" not in css                                                     # never an ellipsis cut mid-word
    assert ".h .r .cap[hidden]{display:none;}" in css
    assert "function fitCaptions()" in js and "c.hidden=true" in js and "fitCaptions();" in js
    assert "var(--hf,1rem)" in css and "var(--cf,.85rem)" in css                              # rem: the "Text size" scale applies
    assert 'setProperty("--hf",Math.max(14,g.font)+"px")' in js and 'setProperty("--cf",Math.max(13,g.font-1)+"px")' in js   # header / chip text follows the measured font (>= 14 px; 2026-09-14: no +4)
    for k in ("map", "err"):
        assert len(LS.HEADERS[k][1]) <= (200 if k == "meas" else 130)   # hidden WHOLE when the column is too narrow; the meas header spans the page


def test_separation_series_spans_at_least_60s_even_at_the_first_tick():
    S = E.separation_series(np.zeros((0, 7)), np.zeros((0, 7)), 1000.0, 1000.4)
    assert S["t"][-1] - S["t"][0] == 60.0 and len(S["t"]) == 61 and np.isnan(S["sep"]).all()
    S = E.separation_series(np.zeros((0, 7)), np.zeros((0, 7)), 100.0, 1000.4)
    assert S["t"][0] == 100.0                                                             # a longer history is untouched
    A = {"sep": S, "t_now": 1000.4}
    fig = PL.separation_fig(A, {"sep_height": 200})
    assert fig.layout.xaxis.tickformat == "%H:%M:%S" and fig.layout.yaxis.range[0] == 0


def test_readout_never_shows_a_fake_sigma_and_guard_makes_sigma_le_0_nan():
    assert PL.readout({"t": np.array([0.0, 1.0]), "az": np.array([0.83, 0.5]), "sig_az": np.array([1.14, np.nan])}, "az", 1.0, -10.0) == "now +0.5° · σ n/a"
    assert PL.readout({"t": np.array([0.0]), "az": np.array([0.83]), "sig_az": np.array([0.0])}, "az", 1.0, -10.0) == "now +0.8° · σ n/a"
    assert PL.readout({"t": np.array([0.0]), "pos3d": np.array([46.2]), "sig_pos3d": np.array([0.0])}, "pos3d", 1.0, -10.0) == "now 46 m · σ n/a"
    assert PL.readout({"t": np.array([0.0]), "az": np.array([0.83])}, "az", 1.0, -10.0) == "now +0.8° · σ n/a"      # no sigma array at all
    assert PL.readout({"t": np.array([0.0]), "az": np.array([0.83]), "sig_az": np.array([1.14])}, "az", 1.0, -10.0) == "now +0.8° ± 1.1° (1σ)"
    g = E.guard_sigmas({"sig_az": np.array([0.0, 1.0, -2.0, np.nan]), "sig_pos3d": np.array([0.0]), "az": np.array([1, 2, 3, 4.0])})
    assert np.isnan(g["sig_az"][0]) and g["sig_az"][1] == 1.0 and np.isnan(g["sig_az"][2]) and np.isnan(g["sig_pos3d"][0])
    c = E.containment({"az": np.array([0.1, 0.2, 0.3]), "sig_az": np.array([np.nan, 1.0, np.nan])}, "az")
    assert c["n"] == 1 and c["p1"] == 100.0                                              # NaN-σ rows leave the statistic, never "perfect"
    e = E._legacy_track_errors(np.array([[0.0, 1000, 0, 100, 0, 0, 0, 0, 1, 2, 0, 0, 0], [1.0, 1000, 0, 100, 0, 0, 0, 1, 2, 2, 0, 0, 0]]),
                               np.array([[0.0, 1000, 0, 100, 0, 0, 0], [1.0, 1000, 0, 100, 0, 0, 0], [2.0, 1000, 0, 100, 0, 0, 0]]))
    assert np.isnan(e["sig_pos3d"]).all()                                                # zero covariance -> unknown


def test_meas_raw_obs_circles_are_visible_and_range_rate_panel_follows_obs_rr():
    M = {"t_lo": 0.0, "t_now": 100.0, "truth": None, "tracks": [],
         "obs": {"t": np.array([10.0, 20.0]), "az": np.array([90.0, 91.0]), "el": np.array([1.0, 2.0]), "rng": np.array([1500.0, 1600.0]), "rr": np.array([np.nan, np.nan])},
         "obs_rr": False}
    fig = PL.meas_fig(M, {"cpa": None}, {"meas_height": 520})
    obs = [t for t in fig.data if t.name == "raw obs"]
    assert len(obs) == 3 and all(t.marker.symbol == "circle" and t.marker.color == PL.MEAS_OBS_COLOR == "#cfd6de" and t.marker.size == 6 and t.marker.line.width == 1
                                 and t.marker.line.color == T.SURFACE and t.opacity == 0.75 for t in obs)          # filled light-ink circles with a surface ring
    assert not any(t.xaxis == "x2" for t in obs)                                          # no range rate without amb_dop
    M["obs"]["rr"] = np.array([-5.0, 3.0]); M["obs_rr"] = True
    fig = PL.meas_fig(M, {"cpa": None}, {"meas_height": 520})
    assert any(t.name == "raw obs" and t.xaxis == "x2" for t in fig.data)                # live: range-rate obs drawn
    # engine: a 5-column obs array (live) yields obs_rr; the archive's NaN column does not
    tt = np.arange(0.0, 101.0)                                                              # 1 Hz truth (interp_truth refuses gaps > 2.5 s)
    snap = {"tgt": np.column_stack([tt, 1000 + 10 * tt, 0 * tt, 100 + 0 * tt, 10 + 0 * tt, 0 * tt, 0 * tt]), "tracks": {},
            "obs": np.array([[25.0, np.pi / 2, 0.05, 1260.0, 9.9], [26.0, np.pi / 2, 0.05, 1270.0, np.nan]])}
    A = {"t_now": 100.0, "tgt_tid": None, "alt_tid": None, "itc_tid": None, "has_truth": True}
    Mx = E.meas_space(snap, A, 100.0)
    assert Mx["obs"] is not None and Mx["obs_rr"] is True and np.isfinite(Mx["obs"]["rr"]).sum() == 1
    snap["obs"] = snap["obs"][:, :4]
    assert E.meas_space(snap, A, 100.0)["obs_rr"] is False
    assert D.archive_obs(1).shape[1] == D.OBS_COLS == 8 and D.slice_obs(np.zeros((0, 4)), 0, 1).shape == (0, 8)


def test_polling_browser_keeps_its_store_slot():
    LS.start(int(os.environ["IH_LIVE_PORT"]))
    import urllib.request

    sid = "keepalive"
    LS.push(sid, {"sep": go.Figure(go.Scatter(x=[0, 1], y=[0, 1])).update_layout(uirevision="live-sep")}, t_now=0.0, clock="00:00:00", frozen=False)
    with LS._LOCK:
        LS.STORE[sid]["wall"] = time.time() - LS.STALE_SID_S - 5           # an hour old
    urllib.request.urlopen(f"http://127.0.0.1:{LS.port()}/figs.json?sid={sid}", timeout=5).read()
    with LS._LOCK:
        assert time.time() - LS.STORE[sid]["wall"] < 5                        # the poll refreshed it ...
    LS.push("other", {"sep": go.Figure(go.Scatter(x=[0, 1], y=[0, 1]))}, t_now=0.0, clock="00:00:00", frozen=False)
    assert LS.has(sid)                                                         # ... so another session's push did not prune it


def test_role_of_speed_reanchor_helpers_and_age_format():
    assert D.role_of("mavlink_14551") == "interceptor" and D.role_of("mavlink_14550") == "target" and D.role_of("MAV14551_2_34") == "interceptor"
    assert D.role_of("mav14550_1_1") == "target" and D.role_of("mavlink_1") == "target" and D.role_of("mavlink_2") == "interceptor"
    assert D.fmt_age(45) == "45 s" and D.fmt_age(7200) == "2.0 h" and D.fmt_age(9 * 86400) == "9 d" and D.fmt_age(None) == "—" and D.fmt_age(float("nan")) == "—"
    assert "_speed_eff" in D.play.__code__.co_consts and "_speed_eff" in D.seek.__code__.co_consts and callable(D.speed_changed)
    assert callable(D.apply_query_params)
