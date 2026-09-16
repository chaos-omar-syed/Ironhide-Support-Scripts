"""Round 3 (2026-09-11) of the operator's plot complaints — ih.plots error_fig / velocity_fig:
titles 16 px bold primary ink WITH their unit over an 18 px takeaway readout (strip 52 / line1 22 — 2026-09-15 "make the metrics
here larger too"), no unlabelled vertical lines (the red "now" line is gone; the gold CPA hairline is back on EVERY error /
velocity card since 2026-09-15 "show CPA lines on the plots", its words in the first card's strip list; the target handover tick sits on the FIRST
error card only, listed in its header strip as "tick_labels"), the velocity truth reads as truth (3 px target
red + "truth" end label; track 2.5 px ink + "track"), robust CLAMPED axis ranges (az / el ±5°, alt ±150 m, 3D 0..300 m,
velocities ±30 m/s — held by stable_range but never widened past the clamp), solid hairline grids, every annotation in ink.
Synthetic A dicts throughout; one bare-mode replay check at 07:23:52 (CPA 53 m at 07:23:47 + handover → #177 at 07:23:12)."""
from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
logging.disable(logging.WARNING)

import ih.data as D  # noqa: E402
import ih.plots as PL  # noqa: E402
import ih.theme as T  # noqa: E402

INKS = {T.INK, T.INK2, T.INK3}
T_NOW = D.hms_to_epoch("07:22:40")
WIN = 120.0
P_ERR = {"show_obs": True, "err_height": 547, "font_px": 14, "line_w": 2.5}
P_VEL = {"vel_height": 400, "font_px": 14, "line_w": 2.5}


# ── synthetic inputs ──────────────────────────────────────────────────────────
def _errors(n: int = 120, az_spike: float | None = None, alt_spike: float | None = None, pos_spike: float | None = None) -> dict:
    t = T_NOW - WIN + 1.0 + np.arange(n, dtype=float)
    rng = np.random.default_rng(3)
    e = {"t": t, "az": rng.normal(0.2, 0.4, n), "el": rng.normal(-0.1, 0.3, n), "pos3d": np.abs(rng.normal(40.0, 12.0, n)), "alt": rng.normal(5.0, 15.0, n),
         "sig_az": np.full(n, 0.9), "sig_el": np.full(n, 0.8), "sig_pos3d": np.full(n, 35.0), "sig_alt": np.full(n, 30.0)}
    if az_spike is not None:
        e["az"][n // 2] = az_spike
    if alt_spike is not None:
        e["alt"][n // 2] = alt_spike
    if pos_spike is not None:
        e["pos3d"][n // 2] = pos_spike
    return e


def _A_err(cpa_t: float | None = None, events=(), **spikes) -> dict:
    A = {"t_now": T_NOW, "err_tracks": [{"tid": 177, "errors": _errors(**spikes), "ungraded": {"t": np.zeros(0)}}],
         "contain": {"az": {"n": 120, "p1": 79.0, "p3": 100.0}}, "track_events": list(events), "cpa": None}
    if cpa_t is not None:
        A["cpa"] = (59.0, float(cpa_t), 3600.0, 880.0, 25.0)
    return A


def _truth(n: int = 120) -> np.ndarray:
    t = T_NOW - WIN + 1.0 + np.arange(n, dtype=float)
    tr = np.zeros((n, 7))
    tr[:, 0] = t
    tr[:, 4] = 12.0 + 2.0 * np.sin(np.arange(n) / 20.0)       # vE
    tr[:, 5] = -8.0 + np.cos(np.arange(n) / 15.0)              # vN
    tr[:, 6] = 0.5 * np.sin(np.arange(n) / 9.0)                # vU
    return tr


def _track(n: int = 120, vu_pin: float | None = None) -> np.ndarray:
    t = T_NOW - WIN + 1.0 + np.arange(n, dtype=float)
    tk = np.full((n, D.TK_W), np.nan)
    tk[:, 0] = t
    tk[:, 1:4] = 0.0
    tk[:, 7] = t                                               # last update = the state time -> a measurement update (not coasting)
    tk[:, 8] = 1.0
    tk[:, 9] = 2.0                                             # confirmed (1 would be tentative)
    tk[:, 10] = 11.0 + 2.0 * np.sin(np.arange(n) / 20.0)
    tk[:, 11] = -9.0 + np.cos(np.arange(n) / 15.0)
    tk[:, 12] = 0.3 * np.sin(np.arange(n) / 9.0)
    if vu_pin is not None:
        tk[n // 2:, 12] = vu_pin
    return tk


def _A_vel(cpa_t: float | None = None, events=(), vu_pin: float | None = None) -> dict:
    A = {"t_now": T_NOW, "tgt_track": _track(vu_pin=vu_pin), "tgt_tid": 177, "err_tracks": [], "contain": {}, "track_events": list(events), "cpa": None}
    if cpa_t is not None:
        A["cpa"] = (59.0, float(cpa_t), 3600.0, 880.0, 25.0)
    return A


def _L(fig) -> dict:
    return fig.to_plotly_json()["layout"]


def _ann(fig, name: str) -> list[dict]:
    return [a for a in _L(fig).get("annotations", []) if str(a.get("name") or "") == name or re.fullmatch(name, str(a.get("name") or ""))]


def _ts(x) -> np.datetime64:
    """A plotly x value (ISO string / np.datetime64 / datetime) as a ms-resolution datetime64 for comparisons."""
    return np.datetime64(x, "ms")


def _vlines(fig) -> list[dict]:
    """Layout shapes that are vertical lines (x0 == x1, type line)."""
    return [s for s in _L(fig).get("shapes", []) if s.get("type") == "line" and s.get("x0") == s.get("x1")]


# ── 1. constants / strip geometry ─────────────────────────────────────────────
def test_constants_and_strip_geometry():
    assert (PL.TITLE_PX, PL.READOUT_PX, PL.READOUT_SUB_PX) == (16, 18, 12)                          # 2026-09-14: "text is massive even at Normal" (were 20 / 20 / 15)
    assert (PL.ERR_BOX_LH, PL.ERR_BOX_PAD, PL.STRIP_ROW_GAP) == (1.35, 4.0, 2.0)
    assert PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX) == (52, 22)                            # was (57, 24)
    # two label sizes: the in-plot top_labels use TITLE_PX - LABEL_PX_DELTA (13), the header-strip tick LIST uses READOUT_SUB_PX (12)
    assert PL.LABEL_PX_DELTA == 3 and PL.px(PL.TITLE_PX - PL.LABEL_PX_DELTA, None) == 13 and PL.READOUT_SUB_PX == 12
    assert PL.STRIP_LABELS_MAX == 3
    assert PL.ERR_TITLE["az"] == "AZIMUTH ERROR" and PL.VEL_TITLE["vu"] == "VERTICAL VELOCITY"      # title texts unchanged
    assert PL.ERR_UNIT == {"az": "°", "el": "°", "pos3d": " m", "alt": " m"}                       # 2026-09-15: the card title carries the unit
    assert PL.ERR_CLAMP == {"az": 5.0, "el": 5.0, "pos3d": 300.0, "alt": 150.0} and PL.VEL_CLAMP == 30.0 and PL.VEL_FLOOR == 2.0
    assert PL.ERR_FLOOR == {"az": 0.5, "el": 0.5, "pos3d": 10.0, "alt": 10.0}


def test_titles_bold_primary_ink_at_16px():
    """2026-09-15 ("make the metrics here larger too"): the card TITLE is 16 px bold primary ink and carries its UNIT
    ("<b>AZIMUTH ERROR</b> (°)"); the takeaway readout is the larger 18 px (strip 52 px, ih.liveserver.ERR_HDR_PX)."""
    title_px, readout_px = PL.px(PL.TITLE_PX, None), PL.px(PL.READOUT_PX, None)                    # 16 / 18 at scale 1
    assert (title_px, readout_px) == (16, 18)
    for fig, keys in ((PL.error_fig(_A_err(), P_ERR, WIN), PL.ERR_KEYS), (PL.velocity_fig(_A_vel(), P_VEL, WIN, truth=_truth()), PL.VEL_KEYS)):
        err = keys is PL.ERR_KEYS
        for k in keys:
            (a,) = _ann(fig, f"title_{k}")
            assert a["text"].startswith("<b>") and a["font"]["color"] == T.INK and a["font"]["size"] == title_px and a["bgcolor"] == PL.TAG_BG, a
            if err:   # the unit rides in the title, not in the readout ("<b>3D POSITION ERROR</b> (m)")
                assert a["text"] == f"<b>{PL.ERR_TITLE[k]}</b> ({PL.ERR_UNIT[k].strip()})", a["text"]
            (r,) = _ann(fig, f"readout_{k}")
            assert r["font"]["size"] == readout_px and r["font"]["color"] in INKS
        # the SIDE-BY-SIDE (panel-only, visible=False here) pair: same 16 px title with the unit, the compact numbers at the readout size
        if not err:
            for k in keys:
                (ts,) = _ann(fig, f"title_{k}_s")
                (rs,) = _ann(fig, f"readout_{k}_s")
                assert ts["text"] == f"<b>{PL.VEL_TITLE[k]}</b> (m/s)" and ts["font"]["size"] == title_px and ts["visible"] is False
                assert rs["font"]["size"] == readout_px and rs["visible"] is False and "<b>" not in rs["text"] and "<span" not in rs["text"]
                assert not _ann(fig, f"delta_{k}_s")                                                # there is no second side-by-side line any more
        meta = _L(fig)["meta"]
        assert meta["err" if err else "vel"]["hdr"] == float(PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0]) == 52.0


# ── 2. vertical elements ──────────────────────────────────────────────────────
def test_no_now_line_in_error_or_velocity_fig():
    ev = [{"t": T_NOW - 60, "role": "target", "old": 129, "new": 177}]
    for fig in (PL.error_fig(_A_err(cpa_t=T_NOW - 30, events=ev), P_ERR, WIN),
                PL.velocity_fig(_A_vel(cpa_t=T_NOW - 30, events=ev), P_VEL, WIN, truth=_truth())):
        L = _L(fig)
        now_x = str(PL._dt(T_NOW))
        for s in L.get("shapes", []):
            assert "now" not in str(s.get("name") or "").lower(), s
            assert (s.get("line") or {}).get("color") != T.RED, s                                     # the red "now" hairline is gone
            if s.get("type") == "line":
                assert str(s.get("x0")) != now_x, s                                                   # nothing vertical AT now
        for a in L.get("annotations", []):
            assert "now" not in str(a.get("name") or "").lower(), a
        # every remaining vertical line is either the gold CPA hairline or a (named) handover tick
        for s in _vlines(fig):
            assert (s.get("line") or {}).get("color") == T.GOLD or str(s.get("name") or "").startswith("handover_"), s
        # the 2 % right padding stays: the x range ends after now
        assert _ts(L["xaxis"]["range"][1]) == _ts(PL._dt(T_NOW + PL.X_PAD_FRAC * WIN))


def test_cpa_marked_on_every_error_and_velocity_card():
    """2026-09-15 ("show CPA lines on the plots") — INVERTS the 2026-09-14 pin: cpa_marks() puts a GOLD hairline
    ("cpa_line_<r>", xref x/x<r>, yref y<r> domain 0..1) on EVERY error and velocity card whenever A["cpa"] is valid and
    inside the window, and the WORDS go once into the azimuth card's header-strip label list ("CPA 59 m · hh:mm:ss" in
    "tick_labels"), never over the data.  PL.cpa_hud still feeds the separation card's header HUD."""
    t_cpa = T_NOW - 50.0
    A_e, A_v = _A_err(cpa_t=t_cpa), _A_vel(cpa_t=t_cpa)
    assert PL.cpa_hud(A_e) == PL.cpa_hud(A_v) == f"CPA 59 m · {D.pdt_hms(t_cpa)}"
    for fig, rows in ((PL.error_fig(A_e, P_ERR, WIN), 4), (PL.velocity_fig(A_v, P_VEL, WIN, truth=_truth()), 3)):
        L = _L(fig)
        gold = [s for s in L["shapes"] if str(s.get("name") or "").startswith("cpa_line_")]
        assert sorted(s["name"] for s in gold) == [f"cpa_line_{r}" for r in range(1, rows + 1)], gold   # one per card
        for r, s in enumerate(sorted(gold, key=lambda sh: sh["name"]), start=1):
            assert s["line"]["color"] == T.GOLD and s["line"]["width"] == 1 and s["layer"] == "above"
            assert s["xref"] == ("x" if r == 1 else f"x{r}") and s["yref"] == ("y domain" if r == 1 else f"y{r} domain")
            assert (s["y0"], s["y1"]) == (0, 1) and _ts(s["x0"]) == _ts(s["x1"]) == _ts(PL._dt(t_cpa))   # full card height, at the CPA time
        assert all(str(s.get("name") or "").startswith(("handover_", "cpa_line_")) for s in _vlines(fig))   # still nothing unlabelled / at "now"
        assert not _ann(fig, "cpa_label") and not _ann(fig, "cpa_tag")                                  # the words are not a separate in-plot label
    # the WORDS: once, in the azimuth card's header-strip list — and the velocity figure carries no label list at all
    (lst,) = _ann(PL.error_fig(A_e, P_ERR, WIN), "tick_labels")
    assert lst["text"] == f"CPA 59 m · {D.pdt_hms(t_cpa)}" and lst["font"]["size"] == PL.px(PL.READOUT_SUB_PX, P_ERR)
    assert not _ann(PL.velocity_fig(A_v, P_VEL, WIN, truth=_truth()), "tick_labels")
    # a CPA at the right edge is still inside the window -> still marked ...
    fig = PL.error_fig(_A_err(cpa_t=T_NOW - 5.0), P_ERR, WIN)
    assert len([s for s in _L(fig)["shapes"] if str(s.get("name") or "").startswith("cpa_line_")]) == 4
    # ... one OUTSIDE the window is not marked at all (cpa_marks' t_lo <= t <= t_now gate)
    out = PL.error_fig(_A_err(cpa_t=T_NOW - WIN - 30.0), P_ERR, WIN)
    assert not [s for s in _L(out)["shapes"] if str(s.get("name") or "").startswith("cpa_line_")] and not _ann(out, "tick_labels")


def test_handover_ticks_first_card_only_with_strip_list():
    """2026-09-14 ("don't need track number labels ... on EVERY plot", "numbers must not overlap the plots"): the target handover
    tick is drawn on the FIRST error card only and its labels are ONE "tick_labels" list in that card's header strip (left,
    READOUT_SUB_PX = 12 px mono ink on TAG_BG) — never text over the plot area; the velocity cards carry no HANDOVER marks
    (the gold CPA hairline of 2026-09-15 is the only vertical they get, and there is no CPA in these fixtures)."""
    ev = [{"t": T_NOW - 43.0, "role": "target", "old": 129, "new": 177, "rule": "closest"},
          {"t": T_NOW - 43.0, "role": "interceptor", "old": None, "new": 203},                         # interceptor changes: NOT on the target's cards
          {"t": T_NOW - 8.0, "role": "target", "old": 177, "new": 203}]
    fig = PL.error_fig(_A_err(events=ev), P_ERR, WIN)
    L = _L(fig)
    ticks = [s for s in L["shapes"] if str(s.get("name") or "").startswith("handover_")]
    assert sorted(s["name"] for s in ticks) == ["handover_target_177", "handover_target_203"]          # ONE tick per event: the first card only
    assert all(s["line"]["color"] == T.INK2 and s["xref"] == "x" and s["yref"] == "y domain" and s["y0"] == 0.86 and s["y1"] == 1.0 for s in ticks)
    (a,) = _ann(fig, "tick_labels")
    assert a["text"] == f"→ #177 · {D.pdt_hms(T_NOW - 43.0)}   → #203 · {D.pdt_hms(T_NOW - 8.0)}"   # chronological, three spaces apart
    assert a["xref"] == "x domain" and a["x"] == 0.0 and a["yref"] == "y domain" and a["y"] == 1.0
    assert a["xanchor"] == "left" and a["yanchor"] == "bottom" and a["xshift"] == 2 and a["yshift"] == 1   # in the header strip, never over the data
    assert a["font"]["family"] == T.MONO and a["font"]["size"] == PL.px(PL.READOUT_SUB_PX, P_ERR) == 12 and a["font"]["color"] == T.INK
    assert a["bgcolor"] == PL.TAG_BG
    assert not _ann(fig, r"handover_label_.*") and not _ann(fig, r"handover_tag_.*") and not _ann(fig, "cpa_label")
    # the velocity cards: no handover shapes, no tick list, no vertical lines at all
    fv = PL.velocity_fig(_A_vel(events=ev), P_VEL, WIN, truth=_truth())
    assert not [s for s in _L(fv)["shapes"] if str(s.get("name") or "").startswith("handover_")] and not _vlines(fv)
    assert not _ann(fv, "tick_labels") and not _ann(fv, r"handover_.*")
    # the strip list keeps the newest STRIP_LABELS_MAX handovers (a tick still marks every one)
    many = [{"t": T_NOW - 100.0 + 20.0 * i, "role": "target", "old": 100 + i, "new": 101 + i} for i in range(5)]
    fe = PL.error_fig(_A_err(events=many), P_ERR, WIN)
    (a,) = _ann(fe, "tick_labels")
    assert a["text"].count("→ #") == PL.STRIP_LABELS_MAX and a["text"].startswith("→ #103 ·") and a["text"].endswith(D.pdt_hms(T_NOW - 20.0))
    assert len([s for s in _L(fe)["shapes"] if str(s.get("name") or "").startswith("handover_")]) == 5


def test_top_labels_stack_when_crowded():
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=1, cols=1)
    fig.add_trace(go.Scatter(x=[PL._dt(0.0), PL._dt(100.0)], y=[0, 1]), row=1, col=1)
    n = PL.top_labels(fig, [(50.0, "→ #129 · a", "l1"), (56.0, "→ #177 · b", "l2"), (90.0, "CPA 59 m · c", "cpa_label")], 0.0, 100.0, None)
    assert n == 3
    A = {a["name"]: a for a in _L(fig)["annotations"]}
    assert A["l1"]["yshift"] == -2 and A["l2"]["yshift"] < A["l1"]["yshift"]                          # 6 s apart in 100 s: the later one stacks
    assert A["cpa_label"]["yshift"] == -2 and A["cpa_label"]["xanchor"] == "right"                     # far away: slot 0, left of its line (90 %)
    assert all(a["font"]["color"] == T.INK and a["bgcolor"] == PL.TAG_BG for a in A.values())


def test_vertical_grid_solid_hairline():
    for fig in (PL.error_fig(_A_err(), P_ERR, WIN), PL.velocity_fig(_A_vel(), P_VEL, WIN, truth=_truth())):
        L = _L(fig)
        axes = {a: v for a, v in L.items() if re.match(r"^[xy]axis\d*$", a)}
        assert len(axes) >= 6
        for a, v in axes.items():
            assert v.get("showgrid") is True and v.get("griddash") == "solid" and v.get("gridwidth") == 1 and v.get("gridcolor") == T.GRID, (a, v)


# ── 3. velocity truth obvious ─────────────────────────────────────────────────
def test_velocity_truth_and_track_styling_with_end_labels():
    fig = PL.velocity_fig(_A_vel(), {**P_VEL, "line_w": 3.0}, WIN, truth=_truth())          # even on the Large preset (line_w 3) the widths are the spec's
    truth = [t for t in fig.data if t.name == "MAVLink truth"]
    track = [t for t in fig.data if t.name == "track #177 filtered state matched"]
    light = [t for t in fig.data if t.name == "track #177 filtered state"]
    assert len(truth) == 3 and all(t.line.width == 3 and t.line.color == T.TARGET for t in truth)
    assert len(track) == 3 and all(t.line.width == 2.5 and t.line.color == T.INK for t in track)
    assert len(light) == 3 and all(t.line.width == 2.5 for t in light) and light[0].showlegend is True and truth[0].showlegend is True
    for k in PL.VEL_KEYS:
        assert PL.VEL_END_LABELS is False and not _ann(fig, f"endlbl_truth_{k}") and not _ann(fig, f"endlbl_track_{k}")   # end words off: they collided with the header strip (clip audit)
    for t in fig.data:                                                                                   # series lines never carry text
        assert "text" not in (t.mode or "")
    assert _L(fig)["showlegend"] is True


def test_velocity_end_label_pins_at_axis_edge_when_off_scale():
    """End labels are OFF (VEL_END_LABELS False): no endlbl_* annotation is emitted even when the track pins off-scale."""
    fig = PL.velocity_fig(_A_vel(vu_pin=-80.0), P_VEL, WIN, truth=_truth())
    assert not _ann(fig, "endlbl_track_vu") and not _ann(fig, "endlbl_truth_vu")


# ── 4. axis scaling ───────────────────────────────────────────────────────────
def test_error_ranges_are_robust_and_clamped():
    L = _L(PL.error_fig(_A_err(az_spike=25.0, alt_spike=900.0, pos_spike=2500.0), P_ERR, WIN))
    for ax, cap in (("yaxis", 5.0), ("yaxis2", 5.0), ("yaxis4", 150.0)):
        lo, hi = L[ax]["range"]
        assert -cap <= lo < 0 < hi <= cap, (ax, L[ax]["range"])                                  # a single spike never blows the axis past the clamp
    assert L["yaxis3"]["range"][0] == 0 and L["yaxis3"]["range"][1] <= 300.0
    # a window holding ONLY off-scale samples pins every card at its cap (the readout still shows the number)
    wild_all = _A_err(n=3)
    for k, v in (("az", 25.0), ("el", -40.0), ("pos3d", 2500.0), ("alt", 900.0)):
        wild_all["err_tracks"][0]["errors"][k] = np.full(3, v)
    wild = _L(PL.error_fig(wild_all, P_ERR, WIN))
    assert wild["yaxis"]["range"] == [-5.0, 5.0] and wild["yaxis2"]["range"] == [-5.0, 5.0] and wild["yaxis3"]["range"] == [0.0, 300.0] and wild["yaxis4"]["range"] == [-150.0, 150.0]
    # the quiet series alone is not blown up either: P95(|err| + σ) x 1.25 -> a small nice half, but >= the 2·median σ floor (bands read)
    quiet = _L(PL.error_fig(_A_err(), P_ERR, WIN))
    assert quiet["yaxis"]["range"][1] <= 5.0 and quiet["yaxis4"]["range"][1] <= 150.0


def test_err_half_is_p95_based_and_clamped():
    mags = [np.r_[np.full(99, 1.0), 25.0]]                                                             # one 25° spike in 100 samples
    assert PL.err_half("az", mags, [np.full(100, 0.5)]) == PL.nice_ceil(1.25 * float(np.percentile(mags[0], 95)))
    assert PL.err_half("az", [np.full(10, 40.0)], []) == 5.0                                           # everything off scale -> the clamp
    assert PL.err_half("pos3d", [np.full(10, 1e4)], []) == 300.0 and PL.err_half("alt", [np.full(10, 1e4)], []) == 150.0
    assert PL.err_half("az", [], []) == PL.nice_ceil(PL.ERR_FLOOR["az"])                               # nothing graded: the floor


def test_stable_range_hold_never_widens_past_the_clamp():
    mem = {"err": {"az": {"lo": -20.0, "hi": 20.0, "small_since": None}}, "vel": {"vu": {"lo": -80.0, "hi": 80.0, "small_since": None}}}   # a stale wide hold
    assert PL.err_range("az", [np.full(5, 1.0)], [], mem) == (-5.0, 5.0)
    assert PL.vel_range([-80.0], [10.0], mem, "vu") == (-30.0, 30.0)
    assert PL.clamp_range(-40.0, 40.0, -30.0, 30.0) == (-30.0, 30.0) and PL.clamp_range(-1.0, 1.0, -30.0, 30.0) == (-1.0, 1.0)
    # velocity: span padded 15 %, floored ±2, clamped ±30, then WIDENED OUTWARDS to the nice tick step of its span (quant_range, 2026-09-15:
    # equal data -> equal range; the raw centre ± half was never twice the same number and the axis moved every second)
    assert PL.vel_range([10.0], [10.0], None) == PL.quant_range(8.0, 12.0) == (8.0, 12.0)
    lo, hi = PL.vel_range([0.0], [10.0], None)
    assert (lo, hi) == PL.quant_range(5.0 - 5.75, 5.0 + 5.75) and lo <= 5.0 - 5.75 and hi >= 5.0 + 5.75
    assert PL.vel_range([-80.0], [5.0], None)[0] == -30.0 and PL.vel_range([-80.0], [5.0], None)[1] < 30.0   # only the runaway edge is pinned
    assert PL.vel_range([-80.0], [80.0], None) == (-30.0, 30.0)                                        # both edges pinned by the clamp
    assert PL.vel_range([], [], None) == (-2.0, 2.0)


def test_velocity_range_pins_unphysical_state_at_the_edge():
    fig = PL.velocity_fig(_A_vel(vu_pin=-80.0), P_VEL, WIN, truth=_truth())
    L = _L(fig)
    for ax in ("yaxis", "yaxis2", "yaxis3"):
        lo, hi = L[ax]["range"]
        assert -30.0 <= lo < hi <= 30.0, (ax, lo, hi)
    assert L["yaxis3"]["range"][0] == -30.0
    (r,) = _ann(fig, "readout_vu")
    assert "-80.0" in r["text"]                                                                        # the readout still tells the truth


# ── 6. every annotation in ink ────────────────────────────────────────────────
def test_every_annotation_font_is_an_ink_token():
    ev = [{"t": T_NOW - 43.0, "role": "target", "old": 129, "new": 177}]
    for fig in (PL.error_fig(_A_err(cpa_t=T_NOW - 20, events=ev, az_spike=25.0), P_ERR, WIN),
                PL.velocity_fig(_A_vel(cpa_t=T_NOW - 20, events=ev, vu_pin=-80.0), P_VEL, WIN, truth=_truth())):
        for a in _L(fig)["annotations"]:
            assert a.get("font", {}).get("color") in INKS, a
            assert a.get("text") != "▾", a                                                             # no hover-only marks left on these cards
        for t in fig.data:
            assert "text" not in (t.mode or ""), t.name


# ── bare-mode replay: the real 8/28 F1 archive at 07:23:52 (CPA 53 m @ 07:23:47, → #177 @ 07:23:12) ──
@lru_cache(maxsize=None)
def _bare(hms: str):
    import ih.engine as E
    import ih.feed as F
    D.init_state(); D.set_flight(1)
    t = D.hms_to_epoch(hms)
    P = {k: D.STATE_DEFAULTS.get(k) for k in ("map_half", "trail_s", "show_sat", "show_blind", "show_obs", "spec_window", "cpa_gate_m")} | {"frame_mode": "engagement", "role_ids": {}}
    A = snap = None
    for tt in np.arange(t - 40.0, t + 1e-6, 2.0):
        D.seek(float(tt), keep_playing=False)
        snap = F.archive_snapshot(float(tt), 120.0)
        A = E.analyze(snap, P)
    return A, P, snap


def test_bare_mode_07_23_52_labels_widths_ranges():
    try:
        A, P, snap = _bare("07:23:52")
    except Exception as exc:  # pragma: no cover - archive not on this machine
        pytest.skip(f"archive replay unavailable: {exc}")
    if A.get("cpa") is None:
        pytest.skip("engine did not validate a CPA at this moment (archive differs)")
    fe = PL.error_fig(A, {**P, "err_height": 547, "font_px": 14, "line_w": 2.5}, 120.0)
    fv = PL.velocity_fig(A, {**P, "vel_height": 400, "font_px": 14, "line_w": 2.5}, 120.0, truth=snap["tgt"])
    assert PL.cpa_hud(A) == f"CPA {A['cpa'][0]:.0f} m · {D.pdt_hms(A['cpa'][1])}"                    # the same words also feed the separation header HUD
    in_win = (A["t_now"] - 120.0) <= float(A["cpa"][1]) <= A["t_now"]
    for fig, rows in ((fe, 4), (fv, 3)):
        L = _L(fig)
        assert not any((s.get("line") or {}).get("color") == T.RED for s in L["shapes"])              # the red "now" line is still gone
        gold = [s for s in L["shapes"] if str(s.get("name") or "").startswith("cpa_line_")]
        assert len(gold) == (rows if in_win else 0)                                                   # 2026-09-15: the gold CPA hairline is on EVERY card
        assert all((s.get("line") or {}).get("color") == T.GOLD for s in gold)
        assert not _ann(fig, "cpa_label") and not _ann(fig, r"handover_label_.*") and not _ann(fig, r"handover_tag_.*")
        for a in L["annotations"]:
            assert a["font"]["color"] in INKS
    ticks = [s for s in _L(fe)["shapes"] if str(s.get("name") or "").startswith("handover_target_")]
    assert ticks and all(s["yref"] == "y domain" for s in ticks)                                      # → #177 at 07:23:12: first card only
    cpa_lines = [s for s in _L(fe)["shapes"] if str(s.get("name") or "").startswith("cpa_line_")]
    assert sorted(_vlines(fe), key=lambda s: s["name"]) == sorted(ticks + cpa_lines, key=lambda s: s["name"])   # handover ticks + CPA hairlines, nothing else
    (lst,) = _ann(fe, "tick_labels")                                                                   # one chronological list: handovers and/or the CPA
    entry = r"(?:→ #\d+|CPA \d+ m) · \d\d:\d\d:\d\d"
    assert re.fullmatch(rf"{entry}(   {entry}){{0,2}}", lst["text"]) and lst["xref"] == "x domain" and lst["yanchor"] == "bottom"
    assert f"CPA {A['cpa'][0]:.0f} m · {D.pdt_hms(A['cpa'][1])}" in lst["text"] or not in_win           # the CPA words ride in that list
    assert [s["name"] for s in sorted(_vlines(fv), key=lambda s: s["name"])] == ([f"cpa_line_{r}" for r in (1, 2, 3)] if in_win else [])
    assert not _ann(fv, "tick_labels")                                                                  # velocity: the CPA hairline, no label list
    Le = _L(fe)
    assert -5.0 <= Le["yaxis"]["range"][0] and Le["yaxis"]["range"][1] <= 5.0 and Le["yaxis2"]["range"][1] <= 5.0
    assert Le["yaxis3"]["range"][1] <= 300.0 and Le["yaxis4"]["range"][1] <= 150.0
    Lv = _L(fv)
    assert all(-30.0 <= Lv[ax]["range"][0] < Lv[ax]["range"][1] <= 30.0 for ax in ("yaxis", "yaxis2", "yaxis3"))
    truth = [t for t in fv.data if t.name == "MAVLink truth"]
    assert truth and all(t.line.width == 3 and t.line.color == T.TARGET for t in truth)
    assert not _ann(fv, "endlbl_truth_vn") and not _ann(fv, "endlbl_track_vn")   # end words off


# ── 2026-09-15 user: "why do all plots go blank during a coast? the track should be propagated" ──
def _coast_split(n_c: int = 10, coast_az: float = 4.5):
    """Graded rows up to T_NOW − n_c, then n_c ungraded COASTING rows (propagated states, growing σ) to T_NOW.
    coast_az = a large propagated error: drawn, but it must never widen the held range."""
    e = _errors(n=int(WIN) - n_c)                                        # t = T_NOW−119 … T_NOW−n_c
    e["kind"] = np.full(len(e["t"]), "meas", object)
    tc = T_NOW - n_c + 1.0 + np.arange(n_c, dtype=float)
    u = {"t": tc, "az": np.full(n_c, coast_az), "el": np.full(n_c, 0.2), "pos3d": np.full(n_c, 60.0), "alt": np.full(n_c, 8.0),
         "sig_az": 1.1 + 0.05 * np.arange(n_c), "sig_el": np.full(n_c, np.nan), "sig_pos3d": np.full(n_c, 40.0), "sig_alt": np.full(n_c, 30.0),
         "kind": np.full(n_c, "coast", object), "vn": np.full(n_c, 0.8), "sig_vn": np.full(n_c, np.nan)}
    return e, u


def test_coast_keeps_the_cards_populated():
    e, u = _coast_split()
    A = {"t_now": T_NOW, "err_tracks": [{"tid": 177, "errors": e, "ungraded": u}], "contain": {"az": {"n": 110, "p1": 70.0, "p3": 100.0}},
         "track_events": [], "cpa": None}
    fig = PL.error_fig(A, P_ERR, WIN)
    (ro,) = _ann(fig, "readout_az")
    assert ro["text"].startswith("coasting · now +4.5° ± 1.6° (1σ)") and ro["font"]["color"] == T.INK    # the CURRENT propagated error, σ from the coast covariance, ink
    (ro_el,) = _ann(fig, "readout_el")
    assert ro_el["text"] == "coasting · now +0.2° · σ n/a"                                                 # no coast covariance -> "σ n/a", never "no samples"
    light = [t for t in fig.data if t.name == "track #177" and t.yaxis == "y"]
    solid = [t for t in fig.data if t.name == "track #177 matched" and t.yaxis == "y"]
    assert len(light) == 1 and len(solid) == 1
    newest = lambda tr: max(_ts(x) for x, y in zip(tr.x, tr.y) if x is not None and y is not None and np.isfinite(y))   # noqa: E731
    assert newest(light[0]) == _ts(D.to_pdt_dt64(np.array([T_NOW]))[0])                                   # the lighter line runs through the coast to now
    assert newest(solid[0]) == _ts(D.to_pdt_dt64(np.array([T_NOW - 10.0]))[0])                            # the matched line stops at the last graded row
    assert any(str(t.name).startswith("coast strip") for t in fig.data)
    # the range is HELD: the +4.5° coast error is drawn but the az range is the one the graded rows alone give
    A0 = {"t_now": T_NOW, "err_tracks": [{"tid": 177, "errors": e, "ungraded": {"t": np.zeros(0)}}], "contain": {}, "track_events": [], "cpa": None}
    assert _L(fig)["yaxis"]["range"] == _L(PL.error_fig(A0, P_ERR, WIN))["yaxis"]["range"]
    (ro0,) = _ann(PL.error_fig(A0, P_ERR, WIN), "readout_az")
    assert ro0["text"].startswith("last 10 s ago ")                                                        # without the propagated rows the graded value is honestly stale
    # velocity line 2: the same rule
    assert PL.vel_delta_text(e | {"vn": np.zeros(len(e["t"])), "sig_vn": np.full(len(e["t"]), 0.9)}, "vn", T_NOW, T_NOW - WIN, None, u) == "coasting · Δ +0.8 m/s · σ n/a"
    # a track with NO row for > 3 s is dropped: the readout goes dark
    late = {k: (v[:-8] if hasattr(v, "__len__") else v) for k, v in u.items()}
    assert PL.readout(e, "az", T_NOW, T_NOW - WIN, late).startswith("coasting · last 8 s ago ")
    assert PL.readout({"t": np.zeros(0)}, "az", T_NOW, T_NOW - WIN, {"t": np.zeros(0)}) == "no sample"


def test_engine_coast_hold_keeps_the_previous_track_until_it_drops_or_measures_elsewhere():
    import ih.engine as E
    t = T_NOW - 30.0 + np.arange(31, dtype=float)
    Tt = np.zeros((len(t), 7)); Tt[:, 0] = t; Tt[:, 1] = 1000.0; Tt[:, 2] = 500.0; Tt[:, 3] = 100.0        # target truth: hovering
    Ti = Tt.copy(); Ti[:, 1] = 9000.0                                                                       # interceptor truth far away
    tk = np.full((len(t), D.TK_W), np.nan); tk[:, 0] = t; tk[:, 3] = 100.0; tk[:, 4:7] = 30.0; tk[:, 8] = 1; tk[:, 9] = 2; tk[:, 10:13] = 0.0
    tk[:, 1] = 1020.0; tk[:, 2] = 500.0; tk[:, 7] = t                                                       # measured 20 m off the truth ...
    coast = t >= T_NOW - 12.0
    tk[coast, 7] = T_NOW - 13.0                                                                             # ... then 12 s of COASTING states ...
    tk[coast, 1] = 1020.0 + 40.0 * (t[coast] - (T_NOW - 13.0))                                              # ... drifting 40 m/s: 500 m off at now
    tracks = {177: tk}
    sel = E.select_tracks(tracks, Tt, Ti, T_NOW, prev_tgt=177)
    assert sel["tgt_tid"] == 177 and sel["tgt_rule"] == E.COAST_HOLD_RULE and sel["scores"][177] > D.GATE_M    # held through the coast
    assert E.select_tracks(tracks, Tt, Ti, T_NOW, prev_tgt=None)["tgt_tid"] is None                          # never a fresh pick beyond the gate
    tk2 = tk.copy(); tk2[-1, 7] = tk2[-1, 0] - 0.5                                                          # the newest state is a MEASUREMENT 500 m off: a steal
    assert E.select_tracks({177: tk2}, Tt, Ti, T_NOW, prev_tgt=177)["tgt_tid"] is None
    assert E.select_tracks(tracks, Tt, Ti, T_NOW + 4.0, prev_tgt=177)["tgt_tid"] is None                    # no state for > 3 s: dropped -> dark
    near = tk.copy(); near[:, 1] = 1010.0; near[:, 7] = t                                                   # an in-gate track appears: it wins (allowed track switch)
    assert E.select_tracks({177: tk, 178: near}, Tt, Ti, T_NOW, prev_tgt=177)["tgt_tid"] == 178
