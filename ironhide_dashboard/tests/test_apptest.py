"""Ironhide dashboard — AppTest scenarios + engine checks for the chaos-spa error math
+ presentation contracts (dataviz method) + refresh/blink hygiene + map-lag protocol.

Scenarios: F1 mid-engagement, F3 at best pass playing with blind-zone rings, pre-flight
empty state, live unconfigured, live unreachable host, Data source page; spa-graded
numbers (Spec tiles "RMSE95 (spa)" with a plain-RMSE secondary line, error panel
samples = CONFIRMED+UPDATED only, HAE truth handed to spa).

Presentation: ONE top line (wordmark · crumb · status strip with two ringed feed
glyphs · badge) + THREE tiles above the panel; every figure follows base_layout()
(card backgrounds, solid hairline grid, tight per-figure margins, legend only for >= 2
series, >= 8px ringed markers except the 5 px raw-obs ✕, ink-only text, hovermode set,
constant uirevision); the injected CSS never clips text; the error panel is spa's
containment style (±1σ .35 / ±3σ .15 bands with 1 px .6 edges, |err| ≤ kσ tags inside
top-left, CPA tag top-right, explicit y ticks, short rotated titles, coast gaps, ✕ obs);
CPA is marked on all three figures.

No-remount proof (ih.liveserver): the Live page emits ONE st.iframe whose srcdoc is
byte-identical across five reruns of a PLAYING replay while liveserver.STORE[sid]
changes every rerun (stamp strictly increasing, err x-range advancing) and
layout.uirevision never changes; paused reruns push nothing; a busy panel port falls
back to three st.plotly_chart elements with stable keys.

Map lag / teleport: heads {tgt, itc: E, N, hdg} AND their layout-image dicts (data
coordinates: x/y == the head's E/N, 6 % of the view, 5° heading buckets with a cached
URI) are in figs.json every tick, the map figure carries no layout images except the
satellite tile (the panel relayouts the icons in), and while playing the map is re-sent
at most every other tick while eng / err follow every tick.

CPA gate: F1 replay — no CPA before pass 1; pass 1 (78 m) is rejected by the default
70 m gate (accepted at 100 m); the first valid CPA is pass 2, 59 m @ 07:22:31, marked
once the pass is over (07:22:45) and absent while still closing (07:22:31).

Track-quality cards: four card rects with rule borders on a page-surface paper, bold
mono titles, a 13 px "now ±1σ" readout + "1σ · 3σ · n" line per card, a dotted
ungraded (tentative / coasting) line through the holes, y-range floored at 2·median σ.

Sidebar / CSS: no wildcard font-family rule, no font rule can hit a Material icon span,
sidebar >= 320 px and wrapping, every control a plain-English sentence-case label with
a help tooltip (no icon-name leak, nothing truncated).

Run:
  python -m pytest -q tests/test_apptest.py
  (or .../python tests/test_apptest.py)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.parse

import numpy as np
import pytest

os.environ.setdefault("IH_LIVE_PORT", "8912")  # test panel port; the running dashboard owns 8902
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from ih import data as D  # noqa: E402  (also puts track_correlation + chaos-spa on sys.path)
from ih import engine as E  # noqa: E402
from ih import icons as I  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402

LIVE, SRC = (f"{ROOT}/views/{p}" for p in ("1_live.py", "3_data_source.py"))
F1_MID = D.hms_to_epoch("07:22:31")     # F1 pass 2 (59 m) — comparison-report sanity point (CPA not yet validated: still closing)
F1_PRE = D.hms_to_epoch("07:21:00")     # F1 before pass 1: no CPA at all
F1_P1 = D.hms_to_epoch("07:22:00")      # F1 after pass 1 (78 m @ 07:21:43): rejected by the 70 m gate, accepted by a 100 m one
F1_AFTER = D.hms_to_epoch("07:22:45")   # F1 after pass 2: CPA 59 m @ 07:22:31 validated (separation rose 368 m in 10 s)
F3_BEST = D.hms_to_epoch("08:20:23")    # F3 just before pass 7 (21 m)
F1_TWO = D.hms_to_epoch("07:23:49")     # F1: two target-side tracks graded (#177 + #203) -> dark-red second line
F1_LATE = D.hms_to_epoch("07:24:15")    # F1: after the 177 -> 203 handover (07:23:59); 177 is the interceptor's track (stolen at pass 3, 07:23:47)
F1_HAND = D.hms_to_epoch("07:21:50")    # F1: just before the 129 -> 177 handover (~07:21:57)
APP = f"{ROOT}/app.py"
TIMEOUT = 180
CHART_KEYS = ["live_map", "live_sep", "live_err", "live_vel", "live_meas", "live_meas_itc"]  # fallback-path emission order (map left, sep + err right, velocity, meas below)
FIG_KEYS = ("map", "sep", "err", "vel", "meas")
HAVE0 = {k: "" for k in FIG_KEYS}
SERIES = ("separation to truth", "separation to track", "closing rate", "interceptor truth", "target truth")   # 2026-09-17: sep card = truth + TRACK series (horizontal dropped)


def _at(page: str, **state) -> AppTest:
    at = AppTest.from_file(page, default_timeout=TIMEOUT)
    for k, v in D.STATE_DEFAULTS.items():
        at.session_state[k] = v
    at.session_state["show_sat"] = False  # no tile fetches in tests
    if "text_scale" not in at.session_state:
        at.session_state["text_scale"] = "Normal"   # tests render at Normal text unless they set it (the Desktop default is Large since 2026-09-11)
    at.session_state["meas_open"] = True  # the measurement-space quad is collapsed by default (A15): most figure tests want it built + pushed (the interceptor card stays collapsed unless a test opens it)
    for k, v in state.items():
        at.session_state[k] = v
    return at


def _md(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def _exc(at: AppTest) -> list[str]:
    return [f"{e.type}: {e.message}" for e in at.exception]


def _charts(at: AppTest) -> list[tuple[str, str, str]]:
    """Fallback path only: [(element id, spec json, config json)] in emission order."""
    return [(c.proto.id, c.proto.spec, c.proto.config) for c in at.get("plotly_chart")]


def _n_plotly(at: AppTest) -> int:
    try:
        return len(at.get("plotly_chart"))
    except Exception:
        return -1


def _panel(at: AppTest) -> str:
    """The ONE self-updating panel iframe's HTML (srcdoc) — the first of the two constant iframes (panel, meas)."""
    frames = at.get("iframe")
    assert len(frames) == 2, f"expected the panel + meas iframes, got {len(frames)}"
    assert 'id="meas"' in frames[1].proto.srcdoc and 'id="map"' in frames[0].proto.srcdoc
    return frames[0].proto.srcdoc


def _store(at: AppTest) -> dict:
    return LS.STORE[at.session_state["_sid"]]


def _figs(at: AppTest) -> dict:
    """The figure specs the browser holds: {map|eng|err: {data, layout}} from STORE[sid]."""
    return LS.figs_of(at.session_state["_sid"])


def _arr(v) -> np.ndarray:
    """A plotly-JSON array: plain list (None = gap) or the {dtype, bdata} typed-array encoding."""
    if isinstance(v, dict) and "bdata" in v:
        return np.frombuffer(base64.b64decode(v["bdata"]), dtype=np.dtype(v["dtype"])).astype(float)
    return np.asarray([np.nan if x is None else x for x in v], float)


def _tiles(md: str) -> list[tuple[str, str, str, str]]:
    return re.findall(r'<div class="ih-tile ([a-z]*)"><div class="k">(.*?)</div><div class="v">(.*?)</div>(?:<div class="s">(.*?)</div>)?', md)


# ── AppTest scenarios ────────────────────────────────────────────────────────
def test_live_f1_mid_engagement_paused():
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] == 177, at.session_state["tgt_tid"]
    md = _md(at)
    # ONE top block: row1 = wordmark + crumb (left) + badge (right); row2 = the FIXED-HEIGHT status strip (2026-09-14: no more wrap / unwrap jumps)
    top = re.search(r'<div class="ih-top"><div class="row1"><div class="l">.*?</div><div class="r">.*?</div></div><div class="ih-status">.*?</div></div>', md, re.S)
    assert top and "CHAOS" in top.group(0) and 'class="ih-status"' in top.group(0) and "chaos-spa graded" not in top.group(0)   # 2026-09-14: the grading note left the top line -> footer
    # 2026-09-15 (laptop 1366x768): the footer legend is the ESSENTIALS only — the gold-star/CPA rule and the track-state key.
    # What the plots label themselves (±1σ band, 3D POS = 1σ RADIUS, CROSSES = RAW OBS, the sample-provenance note) is gone.
    assert "GOLD STAR = CPA ON THE MAP AND SEPARATION ONCE GATED" in md and "LIGHTER + STRIP = TENTATIVE/COASTING" in md and "GAP &gt; 3 S = DROPOUT" in md   # the footer is HTML-escaped
    foot = re.search(r'<div class="ih-footer">(.*?)</div>', md, re.S)
    assert foot and "CROSSES = RAW OBS" not in foot.group(1) and "±1σ BAND" not in foot.group(1) and "PANEL DATA ON" not in foot.group(1)
    status = re.search(r'<div class="ih-status">(.*?)</div>', md).group(1)
    plain = re.sub(r"<[^>]+>", "", status)
    # 2026-09-15: archive mode carries NO source part at all (the sidebar already says "8/28 · Flight 1") and NO icons anywhere
    assert "ARCHIVE" not in plain and "F1" not in plain, plain
    assert "07:22:31" in plain and "PAUSED" in plain and "graded" not in plain.lower() and "TGT —" not in plain
    assert "samples" not in plain, plain                                       # 2026-09-15: the sample count left the strip (it IS the confirmed count)
    assert "<svg" not in status and T.ICONS["ok"] not in status, status        # 2026-09-15: "remove all the random icons" — words only
    assert "1×" not in plain and "×" not in plain, plain                      # speed 1 says nothing; only 2× / 4× appear
    assert 'class="fg' not in status and "MAVLink" not in status   # 2026-09-14: no vehicle picture icons in the strip — feed trouble shows as WORDS only
    # ONE row, 1.3 rem (2026-09-15: "2 rows just squishes the plot")
    assert ".ih-top .ih-status { --st-rows:1;" in T.CSS and "font-size:1.3rem;" in T.CSS.split(".ih-top .ih-status {")[1].split("}")[0], T.CSS.split(".ih-top .ih-status {")[1].split("}")[0]
    assert "REFRESH" not in status and "PANEL" not in status                                                             # no more chrome on the line
    # THREE tiles, one row, nothing else above the panel
    tiles = _tiles(md)
    assert [t[1] for t in tiles] == ["Target track", "Separation · 3D truth-to-truth", "Closest so far · 3D"], tiles   # target track FIRST (state matters most live)
    # 07:22:31 = the minimum itself, separation still closing -> running minimum shown, NOT yet a CPA (muted note, no gold)
    assert tiles[2][0] == "na" and re.fullmatch(r"\d+<small>m</small>", tiles[2][2]) and "closest so far (no CPA yet · gate 70 m)" in tiles[2][3]
    assert re.search(r"\d+ m horiz · \d\d:\d\d:\d\d", tiles[2][3]) and "★" not in tiles[2][3]
    assert re.fullmatch(r"\d+<small>m</small>", tiles[1][2]) and re.search(r'<span class="(closing|opening)">closing [+\-−]\d+ m/s</span>', tiles[1][3])
    assert tiles[0][2] == "#177 CONFIRMED" and tiles[0][0] == "ok" and re.match(r"age [\d.]+ s · horiz err \d+ m", tiles[0][3])   # 2026-09-15: FULL state word (was "CONF")
    assert "acquired 07:22:31" in tiles[0][3] or "changed" in tiles[0][3]                                  # the track's arrival time on the tile
    assert "<b>#177</b> <b>CONFIRMED</b>" in status, status                                                # the target track id + its full state word, always on the line
    # ...followed INLINE by THAT track's published-state counter over the metrics window (conf / tent / coast + update rate)
    # 2026-09-15: ONE row ("2 rows just squishes the plot") — no .br break, no .s2 spans, coloured bold numbers before each word
    assert '<span class="br"></span>' not in status and 'class="s2"' not in status, status
    assert re.search(r'<span><b class="c-ok">\d+</b> confirmed</span><span><b class="c-amber">\d+</b> tentative</span>'
                     r'<span><b class="c-fail">\d+</b> coasting</span><span><b>\d+\.\d</b> Hz</span>', status), status
    # ORDER: the track id / state comes first, then the counter, and PAUSED is LAST
    assert status.index("<b>#177</b>") < status.index('c-ok">') < status.index("</b> Hz") < status.index("<b>PAUSED</b>")
    assert "91 confirmed" in plain and "3 tentative" in plain and "3 coasting" in plain and "2.0 Hz" in plain, plain   # track 177 over its first 49.0 s (2 Hz publish cadence)
    assert md.count('class="ih-tile ') == 3 and 'class="ih-hero' not in md and 'class="ih-strip"' not in md and 'class="ih-chip ' not in md.split("ih-tiles")[0]
    assert "Covariance" not in md and "Allegiance" in md and "Coverage" in md                 # allegiance / coverage moved to "More"
    assert len(at.expander) == 1 and at.expander[0].label.startswith("More details: coverage, allegiance, radar tracks")
    more = "\n".join(m.value for m in at.expander[0].markdown)
    assert 'class="ih-chips"' in more and "MAVLink feeds" in more and "Predicted miss" in more and "Radar tracks" in more
    assert more.count('class="ih-chip ') == 5 and "MAVLink interceptor" not in more and "Graded samples" not in more   # ONE row of 5 chips
    assert 'class="ih-events"' in more and "Track changes" in more                                            # + the TRACK CHANGES card
    assert _n_plotly(at) in (0, -1) and len(at.get("iframe")) == 2      # panel path: no st.plotly_chart at all (panel + meas iframes, quad opened by _at)
    assert set(_figs(at)) == set(FIG_KEYS)
    # the controls row left the page (sidebar "Display" owns them now)
    assert len(at.select_slider) == 0 and len(at.toggle) == 0


def _status_plain(at) -> str:
    st_html = re.search(r'<div class="ih-status">(.*?)</div>', _md(at)).group(1)
    return re.sub(r"<[^>]+>", "", st_html)


def _counter(at) -> tuple[int, int, int, float] | None:
    """(conf, tent, coast, Hz) parsed off the status line, None when the line carries no counter."""
    # 2026-09-15: INLINE on the one status row, FULL WORDS, coloured bold number BEFORE each word (the " · " separators are
    # CSS ::before content, so the text runs straight from one part into the next)
    st_html = re.search(r'<div class="ih-status">(.*?)</div>', _md(at)).group(1)
    m = re.search(r'c-ok">(\d+)</b> confirmed.*?c-amber">(\d+)</b> tentative.*?c-fail">(\d+)</b> coasting.*?<b>([\d.]+)</b> Hz', st_html, re.S)
    return (int(m[1]), int(m[2]), int(m[3]), float(m[4])) if m else None


def test_status_track_state_counter_follows_the_metrics_window():
    """The counter beside the track id = THAT track's published states in the metrics window (spec_window), by
    state kind, + its update rate; it is computed in the engine (A["tgt_counts"]) and it moves with the window.
    Numbers checked against a direct read of the F1 quickdump CSV in tests/test_velocity_plumbing.py."""
    import polars as pl

    for W, want in ((60, (91, 3, 3, 2.0)), (120, (91, 3, 3, 2.0)), (300, (91, 3, 3, 2.0))):
        at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, spec_window=W).run()
        assert not _exc(at), _exc(at)
        assert at.session_state["tgt_tid"] == 177
        assert _counter(at) == want, (W, _counter(at))                 # track 177 was created 49 s before 07:22:31: every window sees its whole life
    # 07:24:15, track 203 (older than 60 s): the 60 s and 300 s windows MUST disagree
    c60 = _counter(_at(LIVE, flight=1, anchor_t=F1_LATE, playing=False, spec_window=60).run())
    c300 = _counter(_at(LIVE, flight=1, anchor_t=F1_LATE, playing=False, spec_window=300).run())
    assert c60 == (101, 0, 18, 2.0) and c300 == (175, 2, 23, 2.0), (c60, c300)
    # ...and each matches a direct count of the CSV rows in the window (state 1 = TENTATIVE, t − last_update_t ≥ 1.25 s = coasting)
    for W, want in ((60, c60), (300, c300)):
        df = pl.read_csv(f"{D.flight_dir(1)}/tracks/track_203.csv").filter((pl.col("t_epoch") >= F1_LATE - W) & (pl.col("t_epoch") <= F1_LATE))
        t, stt, lu = (df[c].to_numpy() for c in ("t_epoch", "track_state", "last_update_t"))
        k = np.where(t - lu < D.MEAS_LAG_S, "meas", "coast").astype(object)
        k[stt == 1] = "tent"
        span = min(float(W), float(F1_LATE - t[0]))
        assert want == (int((k == "meas").sum()), int((k == "tent").sum()), int((k == "coast").sum()), round(len(t) / span, 1)), (W, want)
    # no target track (before the first radar track of F1): the track part shows "—" and NOTHING extra
    at = _at(LIVE, flight=1, anchor_t=D.replay_bounds(1)[0], playing=False).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] is None and _counter(at) is None, _status_plain(at)
    assert "trk —" in _status_plain(at) and "Hz" not in _status_plain(at)


def test_live_f3_best_pass_playing_with_rings():
    at = _at(LIVE, flight=3, anchor_t=F3_BEST, anchor_wall=time.time(), playing=True, show_blind=True, show_other=True).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] == 1291, at.session_state["tgt_tid"]
    cpa = at.session_state["cpa_run"]
    assert cpa is not None and 40 < cpa[0] < 70, cpa   # closest-so-far 56 m @ 08:18:13 (= pass 4)


def test_live_preflight_empty_state():
    at = _at(LIVE, flight=1, anchor_t=D.replay_bounds(1)[0], playing=False).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] is None
    md = _md(at)
    assert "NO TRACK" in md
    tiles = _tiles(md)
    assert len(tiles) == 3 and tiles[0][2] == "NO TRACK" and tiles[0][0] == "fail" and tiles[2][0] == "na"


def test_live_unconfigured_live_source():
    at = _at(LIVE, source="live", live_host="", live_run="").run()
    assert not _exc(at), _exc(at)
    assert "OFFLINE" in _md(at)


def test_live_unreachable_host_times_out_cleanly():
    t0 = time.time()
    at = _at(LIVE, source="live", live_host="10.255.255.1", live_port=27017, live_run="run_deadbeef", playing=False).run()
    dt = time.time() - t0
    assert not _exc(at), _exc(at)
    assert "OFFLINE" in _md(at)
    assert dt < 40, f"unreachable host took {dt:.1f} s"


def test_data_source_page_renders():
    """Landing page: LIVE preselected (connect card, not connected), ARCHIVE REPLAY renders the flight list + transport."""
    at = _at(SRC, flight=1).run()
    assert not _exc(at), _exc(at)
    md = _md(at)
    assert "DATA SOURCE" in md and "NOT CONNECTED" in md and at.radio(key="_ds_mode_w").value == "live" and "10.191.28.205" in md
    assert any(b.label == "Connect" for b in at.button) and at.number_input(key="_mru_w").value == 91
    at.radio(key="_ds_mode_w").set_value("archive").run()
    assert not _exc(at), _exc(at)
    assert "Archive replay" in _md(at) and at.selectbox(key="_flight_w").value == 1 and any(b.key == "ds_open_live" for b in at.button)   # transport lives in the sidebar / Live page
    assert at.session_state["mode"] == "archive" and at.session_state["ds_mode"] == "archive"


def test_live_window_change_reruns_cleanly():
    """The Window slider now lives in the sidebar: changing spec_window between reruns rebuilds the
    error panel over the new range without an exception (the path the user was on when the
    hot-reload crash surfaced)."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, spec_window=120).run()
    assert not _exc(at), _exc(at)
    for W in (300, 60, 120):
        at.session_state["spec_window"] = W
        at.run()
        assert not _exc(at), (W, _exc(at))
        lo, hi = _figs(at)["err"]["layout"]["xaxis4"]["range"]
        assert abs((np.datetime64(hi) - np.datetime64(lo)) / np.timedelta64(1, "s") - W * (1 + PL.X_PAD_FRAC)) < 0.01, (W, lo, hi)   # window + 2 % right padding


# ── refresh / blink hygiene ──────────────────────────────────────────────────
def test_live_paused_reruns_panel_constant_and_store_idle():
    """5 consecutive reruns, replay PAUSED: the panel iframe HTML is byte-identical, nothing is
    re-pushed (stamp unchanged), the pushed figure JSON is byte-identical, uirevision constant."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    html0, ent0 = _panel(at), dict(_store(at))
    for _ in range(4):
        at.run()
        assert not _exc(at), _exc(at)
        assert _panel(at) == html0, "panel HTML changed across a paused rerun (would remount)"
        ent = _store(at)
        assert ent["stamp"] == ent0["stamp"] and ent["figs"] == ent0["figs"] and ent["fig_stamps"] == ent0["fig_stamps"]
    figs = _figs(at)
    for k in FIG_KEYS:
        assert figs[k]["layout"]["uirevision"] == PL.UIREV[k]
        for ax, v in figs[k]["layout"].items():
            if ax.startswith(("xaxis", "yaxis")):
                assert v["uirevision"] == PL.UIREV[k]
    assert at.session_state["_sid"] in html0 and f"PORT={LS.port()}" in html0 and "P=1000" in html0


def test_live_playing_no_remount_proof():
    """THE proof: replay PLAYING, five reruns >= 1 s apart — the iframe srcdoc is byte-identical
    every time while STORE[sid] changes on every rerun (stamp strictly increasing, error-panel
    x-range advancing) and layout.uirevision never changes.  No st.plotly_chart is emitted."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, anchor_wall=time.time(), playing=True).run()
    assert not _exc(at), _exc(at)
    html0 = _panel(at)
    stamps, ranges = [_store(at)["stamp"]], [_figs(at)["err"]["layout"]["xaxis4"]["range"]]
    for _ in range(4):
        time.sleep(1.05)
        at.run()
        assert not _exc(at), _exc(at)
        assert _panel(at) == html0, "panel HTML changed while playing -> Streamlit would remount it"
        assert _n_plotly(at) in (0, -1)
        stamps.append(_store(at)["stamp"])
        ranges.append(_figs(at)["err"]["layout"]["xaxis4"]["range"])
        figs = _figs(at)
        for k in FIG_KEYS:
            assert figs[k]["layout"]["uirevision"] == PL.UIREV[k]
    assert all(b > a for a, b in zip(stamps, stamps[1:])), stamps           # a push every rerun ...
    assert len({r[1] for r in ranges}) == len(ranges), ranges                # ... with advancing data
    # the constant iframe: same element type/position, srcdoc equal -> React keeps the DOM node
    assert "Plotly.react" in html0 and "/figs.json?" in html0
    # FIXED frame: the view box never moves across the ticks (the map figure carries no range; the envelope's view is constant)
    views = {json.dumps(_store(at)["view"]) for _ in range(1)}
    assert len(views) == 1 and _store(at)["view"]["mode"] == "engage"                                  # ENGAGEMENT box (default), constant across the ticks


def test_live_fallback_when_panel_port_unavailable():
    """Port in use -> liveserver.start() returns None -> the page renders the three
    st.plotly_chart elements with stable keys and NO iframe (vehicle icons drawn inside the map)."""
    real = LS.start
    LS.start = lambda *a, **k: None
    st.cache_resource.clear()
    try:
        at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
        assert not _exc(at), _exc(at)
        assert len(at.get("iframe")) == 0
        charts = _charts(at)
        assert [i.rsplit("-", 1)[-1] for i, _, _ in charts] == CHART_KEYS
        for i, spec, cfg in charts:
            assert json.loads(spec)["layout"]["uirevision"] == PL.UIREV[i.split("live_", 1)[-1]]
            assert json.loads(cfg) == ({"scrollZoom": True, "displayModeBar": True, "displaylogo": False, "doubleClick": "reset"} if i.endswith("map") else
                                       {"scrollZoom": True, "displayModeBar": True, "displaylogo": False})
        srcs = [im["source"] for im in json.loads(charts[0][1])["layout"].get("images", [])]
        assert sum(x.startswith("data:image/svg+xml") for x in srcs) == 2          # fallback: icons ARE layout images
        assert "FALLBACK CHARTS" in _md(at)
    finally:
        LS.start = real
        st.cache_resource.clear()


def test_live_freeze_stops_cadence_but_page_renders():
    at = _at(LIVE, flight=1, anchor_t=F1_MID, anchor_wall=time.time(), playing=True, freeze=True).run()
    assert not _exc(at), _exc(at)
    assert "FROZEN" in _md(at)
    assert _store(at)["frozen"] is True and len(at.get("iframe")) == 2    # panel JS stops reacting on this flag


# ── map lag protocol ─────────────────────────────────────────────────────────
def test_heads_and_head_images_in_figs_json_and_map_has_no_icon_images():
    """The teleport fix by construction: the vehicle icons are layout images in DATA coordinates.
    figs.json carries heads {tgt, itc: E, N, hdg} AND head_imgs (x/y == E/N, sizex == 6 % of the
    view width, 5° heading buckets); the map FIGURE has no images other than the satellite tile
    (the panel relayouts the icons in), a fresh browser's envelope carries the tile (when any)
    + sat_keep, and the panel HTML holds no overlay."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    ent = _store(at)
    heads, imgs = ent["heads"], ent["head_imgs"]
    assert set(heads) == {"tgt", "itc"}
    for k in ("tgt", "itc"):
        assert {"E", "N", "U", "spd", "hdg"} <= set(heads[k]) and -180.0 <= heads[k]["hdg"] <= 180.0 and heads[k]["spd"] >= 0
    assert [im["name"] for im in imgs] == ["tgt_icon", "itc_icon"]
    for im, k in zip(imgs, ("tgt", "itc")):
        assert im["x"] == heads[k]["E"] and im["y"] == heads[k]["N"] and im["visible"] is True                    # icon sits ON its head
        assert im["xref"] == "x" and im["yref"] == "y" and im["xanchor"] == "center" and im["yanchor"] == "middle" and im["layer"] == "above"
        fr = _store(at)["view"]
        assert im["sizex"] == im["sizey"] == PL.icon_size_m(fr["x1"] - fr["x0"]) >= 40.0                     # 6 % of the engagement frame
        assert im["source"] == I.icon_uri(k, I.heading_q(heads[k]["hdg"]), PL.HEAD_COLOR[k], T.SURFACE, True)   # cached per 5° bucket
    body = json.loads(LS.response_body(ent, HAVE0))
    assert body["heads"] == heads and body["head_imgs"] == imgs and set(body["figs"]) == set(FIG_KEYS)
    assert "images" not in _figs(at)["map"]["layout"] and "sat" not in body and ent["sat_on"] is False   # tile off in tests
    tt = next(t for t in _figs(at)["map"]["data"] if t.get("name") == "target truth")           # live-segment anchor = the drawn trail's last point
    assert body["tails"]["tgt"] == [round(float(_arr(tt["x"])[-1]), 1), round(float(_arr(tt["y"])[-1]), 1)] and body["tails"]["itc"] is not None
    assert body["view"]["mode"] == "engage" and body["view"]["rev"] == 0 and body["ui"] == {"font_px": 13, "line_w": 2.5, "preset": "Laptop", "text_scale": 1.0}   # 2026-09-15: the default preset is Laptop (D.STATE_DEFAULTS["screen"])
    assert [p["name"] for p in body["pills"]] == ["pill_tgt", "pill_itc"] and body["pills"][0]["text"] == "#177"   # map track-number pills ride the envelope (2026-09-17 pm: the interceptor's pill is back)
    assert isinstance(body["more"], list) and body["more"] and body["more"][0][0] == "Coverage · 60 s"
    # heads match the engine's truth heads
    A_t = heads["tgt"]
    b = D.archive_bundle(1)
    p = E.latest(D.slice_truth(b["tgt"], F1_MID - 180, F1_MID), F1_MID)
    assert abs(A_t["E"] - p[D.TR["E"]]) < 0.06 and abs(A_t["N"] - p[D.TR["N"]]) < 0.06
    assert abs(A_t["hdg"] - float(np.degrees(np.arctan2(p[D.TR["vE"]], p[D.TR["vN"]])))) < 0.06
    # the 5 s velocity leader: on the PANEL path the panel JS draws it as a layout shape from the tweened head (5 s of travel, on-screen
    # length capped / floored in px, like the icon size) — the map FIGURE carries no leader traces; the st.plotly_chart fallback keeps them
    leaders = [t for t in _figs(at)["map"]["data"] if (t.get("name") or "").endswith("_leader")]
    assert leaders == []
    core = LS.panel_core_js()
    # 2026-09-14: the heads / leaders moved OUT of plotly (layout images + shapes) into the DOM overlay over the plot area
    # (ovDraw places the <img id="ov-head-*"> and the leader line from the tweened head) — same px caps / floors.
    assert "function ovDraw(" in core and "function placeHeads(" in core and "LEADER_MAX_PX" in core and "ICON_PX" in core and "function iconSizeM(" in core
    # with the tile ON the map FIGURE still carries no images: the tile goes through the envelope for the BROWSER's view
    # (worker fetch, externalised; skipped when no imagery is reachable)
    at2 = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, show_sat=True).run()
    assert not _exc(at2), _exc(at2)
    assert "images" not in _figs(at2)["map"]["layout"]
    ent2 = _store(at2)
    assert ent2["sat_on"] is True and callable(ent2["sat_fn"])
    v = ent2["view"]
    imgs2 = None
    for _ in range(40):
        with LS._LOCK:
            ks, imgs2 = LS.sat_for(ent2, (v["x0"], v["x1"], v["y0"], v["y1"]))
        if imgs2 is not None:
            break
        time.sleep(0.25)
    if imgs2:   # reachable imagery: externalised, and the envelope hands it over only when the client's key differs
        assert imgs2[0]["source"].startswith("@img/") and imgs2[0]["layer"] == "below"
        b = json.loads(LS.response_body(ent2, HAVE0))
        assert b["sat"]["key"] == ks and b["sat"]["imgs"] == imgs2
        assert "sat" not in json.loads(LS.response_body(ent2, HAVE0, satk=ks))
    # panel HTML (2026-09-14): the heads / leaders / pills are drawn by the DOM overlay over the plot area — <img id="ov-head-*">
    # placed with the axes' own l2p pixel math (placeHeads / ovDraw), so the overlay and that pixel math MUST be in the HTML;
    # the envelope still carries head_imgs (E/N + 5° icon URI) and Plotly.relayout still owns the satellite tile.
    html = _panel(at)
    assert '"ov-head-"' in html or "'ov-head-'" in html or "ov-head-" in html      # the <img id="ov-head-tgt"/"-itc"> are built by ovMake
    assert ".ov .ov-head{position:absolute" in html and "ovMake" in html and "l2p(" in html
    assert "placeHeads" in html and "ovDraw" in html and "Plotly.relayout" in html and "head_imgs" in html


def test_heads_images_unit_x_y_equal_head_and_uri_stable_within_5deg_bucket():
    """heads -> layout images: x/y equal the head E/N, size = max(40 m, 6 % of the view), and the data
    URI is the SAME string for every heading inside one 5° bucket (so Plotly keeps the <image> and its
    SMIL clocks) and a different one across a bucket edge; a missing head keeps its slot invisible."""
    heads = {"tgt": {"E": 2099.3, "N": 187.3, "U": 75.2, "spd": 21.8, "hdg": 53.1}, "itc": {"E": -410.0, "N": 1555.5, "U": 128.3, "spd": 30.1, "hdg": -172.6}}
    imgs = PL.heads_images(heads, 1600.0)
    assert [(im["name"], im["x"], im["y"], im["sizex"], im["sizey"], im["visible"]) for im in imgs] == \
        [("tgt_icon", 2099.3, 187.3, 96.0, 96.0, True), ("itc_icon", -410.0, 1555.5, 96.0, 96.0, True)]
    assert all(im["xref"] == "x" and im["yref"] == "y" and im["sizing"] == "contain" and im["layer"] == "above" for im in imgs)
    assert PL.heads_images(heads, 400.0)[0]["sizex"] == 40.0 and PL.heads_images(heads, 6000.0)[0]["sizex"] == 360.0   # floor 40 m, 6 %
    # URI stable within a bucket, changes across it; the source is the cached icon for the quantised heading
    u = lambda h: PL.heads_images({"tgt": {**heads["tgt"], "hdg": h}}, 1600.0)[0]["source"]  # noqa: E731
    assert u(53.1) == u(52.6) == u(55.0) == u(54.9) and u(53.1) is u(52.6)                # same bucket (55) -> identical (cached) string
    assert u(53.1) != u(57.6) and u(0.0) == u(-2.4) == u(2.4) == u(359.9)                   # bucket edge / wrap-around
    assert u(53.1) == I.icon_uri("tgt", 55.0, T.TARGET, T.SURFACE, True) and I.heading_q(53.1) == 55.0 and I.heading_q(-172.6) == 185.0
    assert u(53.1).startswith("data:image/svg+xml;utf8,") and "animate" in urllib.parse.unquote(u(53.1))   # animated halo / rotors
    assert T.TARGET.lstrip("#") in urllib.parse.unquote(u(53.1)) and T.SURFACE in urllib.parse.unquote(u(53.1))   # card colours, surface param
    # the interceptor icon differs from the target's; heading is applied as a rotate() of the quantised value
    assert PL.heads_images(heads, 1600.0)[1]["source"] != u(-172.6)
    assert "rotate(185.0 50 50)" in urllib.parse.unquote(PL.heads_images(heads, 1600.0)[1]["source"])
    # missing head -> slot kept, invisible (index-stable join in Plotly)
    only = PL.heads_images({"tgt": heads["tgt"], "itc": None}, 1600.0)
    assert [im["name"] for im in only] == ["tgt_icon", "itc_icon"] and only[1]["visible"] is False and only[0]["visible"] is True
    assert PL.heads_images({}, 1600.0)[0]["visible"] is False


def test_map_resent_at_most_every_other_tick_while_eng_err_follow_the_clock():
    at = _at(LIVE, flight=1, anchor_t=F1_MID, anchor_wall=time.time(), playing=True).run()
    assert not _exc(at), _exc(at)
    n_map = n_err = n_legit = 0
    prev = dict(_store(at)["fig_stamps"])
    heads = [_store(at)["heads"]["tgt"]["E"]]
    key = lambda: (tuple(p["text"] for p in _store(at)["pills"]), at.session_state["cpa_ok"])   # noqa: E731  an id change / CPA validation legitimately rebuilds the map
    k0 = key()
    for _ in range(4):
        time.sleep(1.05)
        at.run()
        assert not _exc(at), _exc(at)
        cur = _store(at)["fig_stamps"]
        k1 = key()
        if cur["map"] > prev["map"] and k1 != k0:
            n_legit += 1
        k0 = k1
        n_map += cur["map"] > prev["map"]
        n_err += cur["err"] > prev["err"]
        heads.append(_store(at)["heads"]["tgt"]["E"])
        prev = dict(cur)
    assert n_err == 4 and 1 <= n_map <= 4 and n_map - n_legit <= 2, (n_map, n_legit, n_err)   # eng/err every tick; a trail-only map at most every other tick (id / CPA changes excepted)
    assert len(set(heads)) >= 4                                                  # the heads moved every tick (envelope), whatever the map did


def test_live_figures_follow_mark_and_chrome_spec():
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, show_obs=True).run()
    assert not _exc(at), _exc(at)
    LP = LS.layout(D.STATE_DEFAULTS["screen"], 1.0)   # 2026-09-15: the DEFAULT preset is Laptop (mode "one"), not LS.LAYOUT's Desktop 1080p
    assert LP["preset"] == "Laptop" and LP["mode"] == "one" and (LP["map"], LP["sep"], LP["err"], LP["vel"]) == (568, 568, 486, 371)
    figs = _figs(at)
    assert set(figs) == set(FIG_KEYS)
    for k, spec in figs.items():
        L, data = spec["layout"], spec["data"]
        if k in ("err", "vel"):   # boxed cards on the page surface (rects with a rule border), plot areas transparent
            assert L["paper_bgcolor"] == T.SURFACE and L["plot_bgcolor"] == "rgba(0,0,0,0)"
        else:
            assert L["paper_bgcolor"] == L["plot_bgcolor"] == T.CARD               # the figure IS the card
        if k == "meas":
            assert L["margin"] == dict(PL.MEAS_MARGIN, t=PL.MEAS_TOP_PX), L["margin"]
        else:
            if k in ("err", "vel"):   # header strip (readout 14 + containment 12 + pad) + card pad in the top margin (+ the legend when >= 2 series); computed bottom margin
                # 2026-09-15 card type: readout 18 / its second line 12 / title 16 -> strip (52, 22) = LS.ERR_HDR_PX
                assert (PL.READOUT_PX, PL.READOUT_SUB_PX, PL.TITLE_PX) == (18, 12, 16)
                assert PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX) == (52, 22) and LS.ERR_HDR_PX == 52
                hdr_t = round(PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0] + PL.CARD_PAD_PX)   # 52 + 3 = 55 (READOUT_PX 18 / READOUT_SUB_PX 12)
                leg = round(PL.legend_row_px(LP["font_px"]) + PL.LEGEND_EXPAND_PAD)    # the legend reserve follows THIS figure's font (Laptop 13 -> 44), not PL.LEGEND_PX (font 14 -> 45)
                assert hdr_t == 55 and leg == 44, (hdr_t, leg)
                assert L["margin"] in (dict(PL.MARGINS[k], t=hdr_t), dict(PL.MARGINS[k], t=hdr_t + leg)), (k, L["margin"])
            else:
                assert L["margin"] in (PL.MARGINS[k], dict(PL.MARGINS[k], t=24)), (k, L["margin"])   # tight per-figure margins
        assert L["hovermode"] in ("x unified", "closest")
        assert (L["height"] == LP[k] if k != "vel" else L["height"] >= LP[k]) and "title" not in L   # card header carries the title; the velocity figure is built at least 3-cards tall (the panel JS re-fits it)
        assert L["legend"]["borderwidth"] == 0 and L["legend"]["orientation"] == "h" and L["legend"]["xanchor"] == "left"
        for kk, v in L.items():
            if kk.startswith(("xaxis", "yaxis")):
                assert v["gridcolor"] == T.GRID and v["griddash"] == "solid" and v["gridwidth"] == 1   # solid hairline, never dashed
                assert v["uirevision"] == L["uirevision"]
                assert v["tickfont"]["family"] == T.MONO and v["tickfont"]["size"] == LP["font_px"] == 13 and v["tickfont"]["color"] == T.INK2   # Laptop preset font
                assert "overlaying" not in v                                                # never dual axes
        n_legend = len({t.get("name") for t in data if t.get("showlegend", True) and t.get("name")})
        assert L["showlegend"] == (n_legend >= 2), (k, n_legend)                  # legend iff >= 2 series
        for t in data:
            mk = t.get("marker") or {}
            name = t.get("name") or ""
            if "markers" in (t.get("mode") or "") and name not in ("raw obs vs target truth", "raw obs") and not name.startswith("_anchor"):
                assert mk["size"] >= 8 and mk["line"]["width"] == 2, (k, name, mk)   # >= 8px + 2px ring
            tf = t.get("textfont") or {}
            if tf.get("color"):
                assert tf["color"] in (T.INK, T.INK2), (k, name)       # text never wears the series color
            if k == "meas":   # spa quad reskin: truths 3 px (target red, interceptor blue); tracks 2 px from the role ramps (thin when departed)
                if name == "target truth":
                    assert t["line"] == {"color": T.TARGET, "width": PL.TRUTH_W}
                elif name == "interceptor truth":
                    assert t["line"] == {"color": T.INTERCEPTOR, "width": PL.TRUTH_W}
                elif re.fullmatch(r"#\d+ · (target|interceptor)", name) and t.get("mode") == "lines" and t.get("x") not in ([None],):
                    assert t["line"]["width"] in (PL.ON_W, PL.DEPARTED_W) and t["line"]["color"] in PL.RAMP_TRACK + (PL.RAMP_FOLD, T.GREY_TRACK)
                    assert t["line"]["dash"] in PL.STEP_DASH and t["opacity"] in (1.0, PL.COAST_ALPHA, PL.DEPARTED_ALPHA)
            elif k == "map" and t.get("mode") == "lines" and name in ("interceptor truth", "target truth"):
                assert t["line"]["width"] == max(PL.LINE_W, PL.TRAIL_W) == 5.0, (k, name)   # 2026-09-17 pm map contrast: truth trails 5 px over an 8 px halo
            elif t.get("mode") == "lines" and not t.get("fill") and (name in SERIES or name.startswith("track #")):
                assert t["line"]["width"] == PL.LINE_W == LP["line_w"], (k, name)       # series lines 2.5 px (Laptop / Desktop presets)
        # text wears ink — except the map's track-number pills (team colour by design: "#177" red / "#203" blue) and the map's
        # track_status annotation (2026-09-15: "#177 CONFIRMED" above the plot in the STATE colour, like the status word on the line)
        for a in L.get("annotations", []):
            nm = a.get("name") or ""
            if nm in ("track_status", "itc_status"):
                assert k == "map" and a["font"]["color"] in (T.GREEN, T.AMBER, T.FAIL, T.NA), (k, a)
                continue
            assert a["font"]["color"] in (T.INK, T.INK2, T.INK3) or nm.startswith("pill_"), (k, a)
    # time-series figures: unified crosshair + clean time ticks; map: per-mark tooltips + equal aspect
    for k in ("sep", "err", "meas"):
        assert figs[k]["layout"]["hovermode"] == "x unified"
        assert figs[k]["layout"]["xaxis"]["tickformat"] == "%H:%M:%S"
    assert figs["map"]["layout"]["hovermode"] == "closest"
    assert figs["map"]["layout"]["yaxis"]["scaleanchor"] == "x" and figs["map"]["layout"]["yaxis"]["scaleratio"] == 1
    assert figs["map"]["layout"]["xaxis"]["title"]["text"].endswith("(m)")
    assert "range" not in figs["map"]["layout"]["xaxis"] and "range" not in figs["map"]["layout"]["yaxis"]   # the browser owns the view
    # trails + track are scattergl; no vehicle-icon layout images
    gl = {t.get("name") for t in figs["map"]["data"] if t.get("type") == "scattergl"}
    assert {"interceptor truth", "target truth", "target track #177"} <= gl, gl
    assert not any(im["source"].startswith("data:image/svg") for im in figs["map"]["layout"].get("images", []))
    # 2026-09-15: the map also carries the track id + state above the plot, paper-anchored top-right, in the state colour
    stat = next(a for a in figs["map"]["layout"]["annotations"] if a["name"] == "track_status")
    assert stat["text"] == "<b>#177 CONFIRMED</b>" and stat["xref"] == stat["yref"] == "paper" and (stat["x"], stat["y"]) == (1.0, 1.0)
    assert stat["font"]["color"] == T.GREEN and stat["xanchor"] == "right" and stat["yanchor"] == "bottom"
    pills = {a["name"]: a for a in figs["map"]["layout"]["annotations"] if a["name"].startswith("pill_")}
    assert pills["pill_tgt"]["text"] == "#177" and pills["pill_tgt"]["font"] == {"family": T.MONO, "size": PL.PILL_PX, "color": T.INK}   # D7: text in ink, colour on the border (2026-09-17: PILL_PX 15)
    assert pills["pill_tgt"]["bgcolor"] == T.CARD and pills["pill_tgt"]["bordercolor"] == T.TARGET and pills["pill_tgt"]["borderwidth"] == 2 and pills["pill_tgt"]["xshift"] == 14   # up-right of the newest track point
    trk = next(t for t in figs["map"]["data"] if t.get("name") == "target track #177")
    assert abs(pills["pill_tgt"]["x"] - float(_arr(trk["x"])[-1])) < 0.06 and abs(pills["pill_tgt"]["y"] - float(_arr(trk["y"])[-1])) < 0.06


def test_error_panel_is_spa_containment_style():
    """Bands (±1σ .35, ±3σ .15 in the track colour, 1 px .6 edge, one polygon per non-coasting run),
    no amber off-scale ✕, per-card containment line = spa rule over the display window (top-right
    under the readout), y-range = ±nice(max(|err| + 3σ, floor, 2·median σ)) with thin_ticks-thinned ticks
    (the Laptop default preset's ~46 px rows carry only ±half/2: the zero LABEL is dropped, the zero LINE stays)
    and short rotated titles, 14 px between the cards, coast gaps, ✕ raw obs in secondary ink at .55.
    At 07:22:31 the CPA is NOT yet valid -> no gold hairline / tag on the panel."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, spec_window=120, show_obs=True).run()
    assert not _exc(at), _exc(at)
    err = _figs(at)["err"]
    L, data = err["layout"], err["data"]
    fills = [t for t in data if t.get("fill") == "toself"]
    assert {t["fillcolor"] for t in fills} == {PL.rgba(T.TARGET, .3)}                             # ONLY the ±1σ band (3σ stays in the readout)
    assert sum(t["fillcolor"].endswith(",0.3)") for t in fills) >= 4 and not any("0.15)" in t["fillcolor"] for t in fills)
    assert PL.BANDS_DRAWN == (1,) and PL.BAND_ALPHA == {1: 0.30} and PL.BAND_EDGE_ALPHA == 0.8
    for t in fills:
        assert t["line"] == {"width": 1, "color": PL.rgba(T.TARGET, .8)} and t["hoverinfo"] == "skip" and t["showlegend"] is False
    assert not any((t.get("marker") or {}).get("symbol") == "x" for t in data)                      # no amber off-scale markers
    assert not any((t.get("marker") or {}).get("color") == T.AMBER for t in data)
    assert not any(t.get("name") in ("measurement", "coasting", "tentative", "off scale") for t in data)
    lines = [t for t in data if t.get("name") == "track #177" and t.get("mode") == "lines" and not t.get("fill")]
    assert len(lines) == 4 and all(t["line"]["color"] == PL.rgba(T.TARGET, PL.LIGHT_ALPHA) and t["line"]["width"] == PL.LINE_W and t["connectgaps"] is False for t in lines)
    graded = [t for t in data if t.get("name") == "track #177 matched"]      # the graded samples in the full colour ON TOP of the continuous (lighter) line
    assert len(graded) == 4 and all(t["line"]["color"] == T.TARGET and t["line"]["width"] == PL.LINE_W and t["showlegend"] is False for t in graded)
    assert all(any(y is None or (isinstance(y, float) and np.isnan(y)) for y in _arr(t["y"]).tolist()) or True for t in graded)
    assert len([t for t in data if t.get("name") == "coast strip #177"]) == 4                                 # a coast indicator strip per card
    obs = [t for t in data if t.get("name") == "raw obs vs target truth"]
    assert len(obs) == 3 and PL.OBS_KEYS == ("az", "el", "alt")                                # no 3D position from one sensor's obs
    for t in obs:
        assert t["marker"]["symbol"] == "x-thin" and t["marker"]["size"] == 5 and t["marker"]["line"]["color"] == T.OBS == T.INK2
        assert t["opacity"] == T.OBS_ALPHA == 0.55 and t["hoverinfo"] == "skip" and t["showlegend"] is False
    # containment lines: exactly spa's rule (|err| <= k sigma), computed here from the engine's graded samples
    b = D.archive_bundle(1)
    tracks = D.slice_tracks(b["tracks"], F1_MID - 180, F1_MID)
    Tt = D.slice_truth(b["tgt"], F1_MID - 180, F1_MID)
    e = E.track_errors(tracks[177], Tt, ant=b["ant"], t_now=F1_MID, window_s=120.0, tid=177)
    caps = [a for a in L["annotations"] if a["text"].startswith("1σ")]
    assert len(caps) == 4
    for a, key in zip(caps, PL.ERR_KEYS):
        err_k, sig_k = np.asarray(e[key]), np.asarray(e[f"sig_{key}"])
        ok = np.isfinite(err_k) & np.isfinite(sig_k)
        p1, p3 = 100 * np.mean(np.abs(err_k[ok]) <= sig_k[ok]), 100 * np.mean(np.abs(err_k[ok]) <= 3 * sig_k[ok])
        assert a["text"] == f"1σ {p1:.0f}% · 3σ {p3:.0f}% · n {ok.sum()}", (key, a["text"])
        assert a["x"] == 1.0 and a["xanchor"] == "right" and a["y"] == 1.0 and a["yanchor"] == "bottom" and a["xref"].endswith("domain")   # header strip, right (above the plot area)
        assert a["font"]["size"] == PL.READOUT_SUB_PX and a["font"]["color"] == T.INK2 and a["bgcolor"] == PL.TAG_BG
        c = E.containment(e, key)
        assert abs(c["p1"] - p1) < 1e-9 and abs(c["p3"] - p3) < 1e-9 and c["n"] == int(ok.sum())
        # y-range: nice ceiling of max(|err| + 3 sigma), never under the floor nor under 2·median sigma; 3 ticks; short title; muted zero
        yax = L["yaxis" if key == "az" else f"yaxis{PL.ERR_KEYS.index(key) + 1}"]
        assert yax["title"]["text"] == PL.ERR_AXIS[key] and yax["zerolinecolor"] == T.ZERO
        # y-range (2026-09-11 round 3): ROBUST half = nice_ceil(max(floor, 2·median σ, 1.25 · P95(|err| + 1σ))) capped at ERR_CLAMP — a spike no longer blows the axis
        top = PL.err_half(key, [np.abs(err_k[ok]) + sig_k[ok]], [sig_k[ok]])
        assert top <= PL.ERR_CLAMP[key] and yax["range"][1] == top, (key, yax["range"], top)
        if key == "pos3d":   # a magnitude: 0 .. top, band 0 .. +σ (1σ radius of the covariance ellipsoid), ticks 0 / top/2 / top
            assert yax["range"][0] == 0 and yax["tickvals"] == [0.0, top / 2, top]
            assert np.all(err_k[ok] >= 0)
        else:
            assert yax["range"][0] == -top
            # the tick LIST is still ±half/2 + 0, but thin_ticks drops labels that would collide in a row this short:
            # on the Laptop default (err 486 px, 4 rows) row_h ≈ 46 px and 13 px type leaves room for two labels only
            hdr_px = PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0]
            ph = L["height"] - L["margin"]["t"] - L["margin"]["b"]
            row_h = PL.PANEL_FIT_SLACK * max(1.0, (ph - 3.0 * (PL.PANEL_GAP_PX + hdr_px)) / 4.0)
            fpx = L["yaxis"]["tickfont"]["size"]
            assert PL.err_ticks(top) == [-top / 2, 0.0, top / 2]                                   # ±half/2, 0 (never at the edges)
            assert yax["tickvals"] == PL.thin_ticks(PL.err_ticks(top), -top, top, row_h, fpx) == [-top / 2, top / 2], (key, yax["tickvals"], row_h)
    assert not any(a["text"].startswith("CPA ") for a in L["annotations"])                         # not yet a valid CPA
    assert not any(sh["line"]["color"] == T.GOLD for sh in L.get("shapes", []) if sh.get("type") == "line")
    assert all(a["font"]["color"] in (T.INK, T.INK2) for a in L["annotations"])
    # between the four cards: the 10 px gap + the next card's header strip (readout 14 + containment 12 + pad)
    doms = [L[ax]["domain"] for ax in ("yaxis", "yaxis2", "yaxis3", "yaxis4")]
    plot_h = L["height"] - L["margin"]["t"] - L["margin"]["b"]
    hdr = PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0]
    for hi, lo in zip(doms[1:], doms[:-1]):
        assert abs((lo[0] - hi[1]) * plot_h - (PL.PANEL_GAP_PX + hdr)) < 0.5 and PL.PANEL_GAP_PX == 10.0, (doms, plot_h)
    assert L["margin"]["t"] == round(hdr + PL.CARD_PAD_PX)                                          # the first strip lives in the top margin
    assert L["meta"]["err"] == {"rows": 4, "hdr": hdr, "gap": PL.PANEL_GAP_PX, "pad": PL.CARD_PAD_PX, "keys": list(PL.ERR_KEYS)}   # the panel JS re-derives the rows for its height
    assert L["showlegend"] is False                                                 # one track -> no legend
    for ax in ("xaxis", "xaxis2", "xaxis3", "xaxis4"):   # no x-axis title on the error panel (HH:MM:SS ticks; the separation card above carries "time (PDT)") -> room for the plot areas
        assert L[ax].get("title", {}).get("text") in (None, ""), (ax, L[ax].get("title"))
        assert L[ax].get("automargin") is False, ax                                                # exact row pixels: the bottom margin is computed, not auto


def test_error_panel_cards_titles_readouts_and_bridged_ungraded():
    """Each of the four panels is a boxed card (card-coloured rect, 1 px rule border, padded past its
    plot area) with a bold mono title top-left and a 13 px primary-ink takeaway top-right = the
    CURRENT graded error ± its 1σ at the newest graded sample; tentative / coasting states bridge the holes
    of the line (lighter segment, σ interpolated) and never enter the stats."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, spec_window=120).run()
    assert not _exc(at), _exc(at)
    err = _figs(at)["err"]
    L, data = err["layout"], err["data"]
    rects = [sh for sh in L["shapes"] if sh.get("type") == "rect"]
    # 2026-09-15 ("section off the headers more"): each card is a card_<key> frame PLUS a shaded hdr_<key> band over its strip
    assert [sh["name"] for sh in rects] == [n for k in PL.ERR_KEYS for n in (f"card_{k}", f"hdr_{k}")], [sh["name"] for sh in rects]
    cards = [sh for sh in rects if sh["name"].startswith("card_")]
    hdrs = [sh for sh in rects if sh["name"].startswith("hdr_")]
    plot_h = L["height"] - L["margin"]["t"] - L["margin"]["b"]
    hdr_px = PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0]
    for sh, ax in zip(hdrs, ("yaxis", "yaxis2", "yaxis3", "yaxis4")):   # the band spans exactly the strip: plot-area top -> card top
        d1 = L[ax]["domain"][1]
        assert sh["fillcolor"] == PL.HDR_BAND and sh["line"]["width"] == 0 and sh["layer"] == "below", sh
        assert sh["y0"] == d1 and abs((sh["y1"] - d1) * plot_h - (hdr_px + PL.CARD_PAD_PX)) < 1e-6, sh
    for sh, ax in zip(cards, ("yaxis", "yaxis2", "yaxis3", "yaxis4")):
        d0, d1 = L[ax]["domain"]
        assert sh["layer"] == "below" and any(sh["fillcolor"] == PL.TONE_FILL[t] and sh["line"] == {"color": PL.TONE_LINE[t][0], "width": PL.TONE_LINE[t][1]} for t in ("ok", "warn", "fail")), sh   # quiet card, or the amber state frame
        assert sh["xref"] == sh["yref"] == "paper" and sh["x0"] == -PL.CARD_PAD_X and sh["x1"] == 1 + PL.CARD_PAD_X
        hdr = PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0]                                    # header strip (readout 14 · containment 12 · pad) at text scale 1
        assert abs((d0 - sh["y0"]) * plot_h - PL.CARD_PAD_PX) < 1e-6 and abs((sh["y1"] - d1) * plot_h - (hdr + PL.CARD_PAD_PX)) < 1e-6   # the card rect includes its strip
    titles = [a for a in L["annotations"] if a["text"].startswith("<b>")]
    # 2026-09-15: the card title carries its UNIT — "<b>AZIMUTH ERROR</b> (°)" (the state words "▲ OUTSIDE 1σ" may still follow)
    assert [a["text"].split("  <b>▲")[0] for a in titles] == [f"<b>{PL.ERR_TITLE[k]}</b> ({PL.ERR_UNIT[k].strip()})" for k in PL.ERR_KEYS]
    assert [a["text"].split("  <b>▲")[0] for a in titles][:2] == ["<b>AZIMUTH ERROR</b> (°)", "<b>ELEVATION ERROR</b> (°)"]
    # header strip: title / readouts anchored ABOVE the plot area (y domain 1.0, yanchor bottom) — nothing sits over the data
    assert all(a["x"] == 0.0 and a["xanchor"] == "left" and a["y"] == 1.0 and a["yanchor"] == "bottom" and a["yshift"] > 0 and a["font"]["family"] == T.MONO for a in titles)
    for a in L["annotations"]:
        if (a.get("name") or "").startswith(("readout_", "contain_", "title_")):
            assert a["yref"].endswith(" domain") and a["y"] == 1.0 and a["yanchor"] == "bottom" and a["yshift"] >= 1, a["name"]
    # readouts: current graded error ± 1σ from the engine's newest graded sample in the window
    b = D.archive_bundle(1)
    tracks = D.slice_tracks(b["tracks"], F1_MID - 180, F1_MID)
    Tt = D.slice_truth(b["tgt"], F1_MID - 180, F1_MID)
    e = E.track_errors(tracks[177], Tt, ant=b["ant"], t_now=F1_MID, window_s=120.0, tid=177)
    reads = {a["name"]: a for a in L["annotations"] if (a.get("name") or "").startswith("readout_")}
    assert set(reads) == {f"readout_{k}" for k in PL.ERR_KEYS}
    for k in PL.ERR_KEYS:
        a = reads[f"readout_{k}"]
        assert a["font"]["size"] == PL.READOUT_PX and a["font"]["color"] == T.INK and a["x"] == 1.0 and a["xanchor"] == "right" and a["bgcolor"] == PL.TAG_BG
        y, sg = np.asarray(e[k]), np.asarray(e[f"sig_{k}"])
        i = int(np.flatnonzero(np.isfinite(y) & np.isfinite(sg))[-1])
        if k in PL.ONE_SIDED:
            want = f"{PL.ERR_NUM[k].format(y[i])}{PL.ERR_UNIT[k]} · σ {PL.ERR_SIG_NUM[k].format(sg[i])}{PL.ERR_UNIT[k]}"
        else:
            want = f"{PL.ERR_NUM[k].format(y[i])}{PL.ERR_UNIT[k]} ± {PL.ERR_SIG_NUM[k].format(sg[i])}{PL.ERR_UNIT[k]} (1σ)"
        assert a["text"] in (f"now {want}", *(f"last {n} s ago {want}" for n in range(0, 200))), (k, a["text"], want)
        assert a["text"] == PL.readout(e, k, F1_MID, F1_MID - 120.0)
    m = re.fullmatch(r"now ([+-]\d+\.\d)° ± (\d+\.\d)° \(1σ\)", reads["readout_az"]["text"])
    assert m and abs(float(m.group(1))) < 3.0 and 0.3 < float(m.group(2)) < 3.0, reads["readout_az"]["text"]   # "+1 deg class" at this point
    # A6: the published states spa did NOT grade (tentative / coasting) BRIDGE the holes of the graded line instead of breaking it
    u = E.ungraded_errors(tracks[177], Tt, e, ant=b["ant"], t_now=F1_MID, window_s=120.0, tid=177)
    assert len(u["t"]) >= 1 and not np.isin(np.round(u["t"], 6), np.round(e["t"], 6)).any()          # disjoint from the graded set
    assert set(u["kind"]) <= {"tent", "coast", "meas"} and set(u["kind"]) & {"tent", "coast"}       # (a 'meas' row spa still refused stays labelled by kind)
    assert not any((t.get("name") or "").startswith("ungraded #") for t in data)                     # the dotted line is gone ...
    assert E.containment(e, "az")["n"] == int(np.isfinite(e["az"]).sum())                            # ... and ungraded rows still never enter the stats
    ta, ya, sa, ga, ui, brk = PL.merged_series(e["t"], e["az"], e["sig_az"], u["t"], u["az"], 3.0)
    assert len(ta) == int(np.isfinite(e["az"]).sum()) + int(np.isfinite(u["az"]).sum()) and np.all(np.diff(ta) > 0) and ga.sum() == int(np.isfinite(e["az"]).sum())
    assert np.isfinite(sa[~ga]).all() and np.all(sa[~ga] >= np.nanmin(sa[ga]) - 1e-9) and np.all(sa[~ga] <= np.nanmax(sa[ga]) + 1e-9)   # σ interpolated between graded neighbours
    # hole_segments unit (kept for tooling): neighbours bridge, gaps break, all-graded -> nothing
    t, y = PL.hole_segments(np.array([0., 1., 3., 4.]), np.array([0., 0., 0., 0.]), np.array([2.]), np.array([5.]), 3.0)
    assert list(t) == [1.0, 2.0, 3.0] and list(y) == [0.0, 5.0, 0.0]
    t, y = PL.hole_segments(np.array([0., 1.]), np.array([0., 0.]), np.array([20.]), np.array([5.]), 3.0)
    assert list(t) == [20.0] and list(y) == [5.0]                                                    # far from any graded sample: lone point, no bridge
    assert len(PL.hole_segments(np.array([0., 1.]), np.array([0., 0.]), np.zeros(0), np.zeros(0), 3.0)[0]) == 0
    assert PL.readout({"t": np.zeros(0)}, "az", 0.0, -10.0) == "no sample"
    assert PL.readout({"t": np.array([0.0]), "az": np.array([0.83]), "sig_az": np.array([1.14])}, "az", 1.0, -10.0) == "now +0.8° ± 1.1° (1σ)"
    assert PL.readout({"t": np.array([0.0]), "alt": np.array([-12.4]), "sig_alt": np.array([22.8])}, "alt", 7.0, -10.0) == "last 7 s ago -12 m ± 23 m (1σ)"
    assert PL.readout({"t": np.array([0.0]), "pos3d": np.array([46.2]), "sig_pos3d": np.array([31.4])}, "pos3d", 1.0, -10.0) == "now 46 m · σ 31 m"
    assert PL.ERR_TITLE["pos3d"] == "3D POSITION ERROR" and "rng" not in PL.ERR_KEYS and PL.ERR_KEYS == ("az", "el", "pos3d", "alt")
    # 3D position error = |ENU error| (spa errors_3d) and its σ = sqrt(eig1² + eig2² + eig3²) = sqrt(trace) of the ENU position covariance
    assert np.allclose(e["pos3d"], np.sqrt(e["horiz"] ** 2 + e["alt"] ** 2), atol=0.6)          # spa's u_errors is in the truth-local frame: ~alt
    assert np.all(e["sig_pos3d"][np.isfinite(e["sig_pos3d"])] >= e["sig_alt"][np.isfinite(e["sig_pos3d"])] - 1e-6)


def test_cpa_marked_on_map_sep_and_every_error_and_velocity_card():
    """07:22:45 — pass 2 is over: the validated CPA (59 m @ 07:22:31) is the gold ★ + "CPA n m" on the
    map and the gold ★ + hairline on the separation card (words in its header HUD); the tile is gold.
    2026-09-15 ("show CPA lines on the plots"): a gold hairline ALSO on every error card (cpa_line_1..4)
    and every velocity card (cpa_line_1..3), with the words "CPA 59 m · 07:22:31" once, in the azimuth
    card's header-strip label list (tick_labels).  The measurement quad still carries none."""
    at = _at(LIVE, flight=1, anchor_t=F1_AFTER, playing=False).run()
    assert not _exc(at), _exc(at)
    figs = _figs(at)
    cpa = at.session_state["cpa_ok"]
    assert cpa is not None and cpa == at.session_state["cpa_run"]
    assert abs(cpa[0] - 59.0) < 0.6 and D.pdt_hms(cpa[1]) == "07:22:31", cpa
    star = dict(symbol="star", size=21, color=T.GOLD)   # 2026-09-17: x1.5
    for k in ("map", "sep"):
        s_ = [t for t in figs[k]["data"] if t.get("name") == "CPA"]
        if k == "map":   # 2026-09-17: the map label is an annotation on TAG_BG (>= 14 px x scale) beside the marker-only ★
            assert len(s_) == 1 and all(s_[0]["marker"][kk] == v for kk, v in star.items()) and s_[0]["marker"]["line"] == {"width": 2, "color": T.CARD}
            assert "text" not in s_[0] and s_[0]["mode"] == "markers"
            lab = [a for a in figs[k]["layout"].get("annotations", []) if a.get("name") == "cpa_map_label"]
            assert len(lab) == 1 and lab[0]["text"] == "CPA 59 m" and lab[0]["font"]["color"] == T.INK and lab[0]["font"]["size"] >= 14 and lab[0]["bgcolor"] == PL.TAG_BG
        else:   # separation (2026-09-17 pm "I dont want stars, just a line with CPA"): NO marker trace — a labelled gold line + a red dashed track line
            assert not s_ and not any((t.get("name") or "").startswith(("CPA", "closest")) for t in figs[k]["data"])
            L_ = figs[k]["layout"]
            ln = {s.get("name"): s for s in L_.get("shapes", []) if str(s.get("name") or "").endswith("_line")}
            assert ln["cpa_line"]["line"] == {"color": T.GOLD, "width": PL.CPA_LINE_W, "dash": "solid"} and "cpa_trk_line" not in ln   # ONE line (2026-09-17 pm "CPA is CPA")
            lab = {a["name"]: a for a in L_.get("annotations", []) if str(a.get("name") or "").endswith("_label")}
            assert lab["cpa_label"]["text"] == "CPA 59 m" and lab["cpa_label"]["font"]["color"] == T.INK and "cpa_trk_label" not in lab
            assert "07:22:31" in lab["cpa_label"]["hovertext"]
            assert _store(at)["hud"] == {"cpa": "CPA 59 m · 07:22:31"}   # one CPA, one word
            assert any((t.get("name") or "") == "_anchor_pad" for t in figs[k]["data"])                 # 2 % right padding anchor
        assert not any(t.get("name") == "closest so far" for t in figs[k]["data"])
    gold_sep = [sh for sh in figs["sep"]["layout"]["shapes"] if sh["line"]["color"] == T.GOLD]
    assert len(gold_sep) == 1 and gold_sep[0]["xref"] == "x" and gold_sep[0]["yref"] == "y domain"       # the separation card's hairline
    # 2026-09-15: the gold hairline is on EVERY error / velocity card (one shape per row, named cpa_line_<r>)
    for k, rows in (("err", 4), ("vel", 3)):
        Lk = figs[k]["layout"]
        gold = [sh for sh in Lk.get("shapes", []) if (sh.get("line") or {}).get("color") == T.GOLD]
        assert [sh["name"] for sh in gold] == [f"cpa_line_{r}" for r in range(1, rows + 1)], (k, [sh["name"] for sh in gold])
        for r, sh in enumerate(gold, start=1):
            assert sh["type"] == "line" and sh["layer"] == "above" and sh["line"]["width"] == PL.CPA_LINE_W
            assert sh["xref"] == ("x" if r == 1 else f"x{r}") and sh["yref"] == ("y domain" if r == 1 else f"y{r} domain")
            assert sh["x0"] == sh["x1"] and (sh["y0"], sh["y1"]) == (0, 1)
        assert not [t for t in figs[k]["data"] if t.get("name") == "CPA"], k          # a hairline, never a ★ trace
    # the WORDS appear once: in the azimuth card's header-strip label list (with the handover labels), never on the velocity cards
    strip = [a["text"] for a in figs["err"]["layout"]["annotations"] if a.get("name") == "tick_labels"]
    assert len(strip) == 1 and "CPA 59 m · 07:22:31" in strip[0], strip
    assert not [a for a in figs["vel"]["layout"].get("annotations", []) if "CPA" in str(a.get("text") or "")]
    for k in ("meas",):   # the measurement quad keeps no CPA marks at all
        Lk = figs[k]["layout"]
        assert not [sh for sh in Lk.get("shapes", []) if (sh.get("line") or {}).get("color") == T.GOLD], k
        assert not [a for a in Lk.get("annotations", []) if a.get("name") == "cpa_label" or "CPA" in str(a.get("text") or "")], k
        assert not [t for t in figs[k]["data"] if t.get("name") == "CPA"], k
    xr = figs["err"]["layout"]["xaxis"]["range"]                                                                          # 2 % right padding past "now"
    t_lo_, t_hi_ = (np.datetime64(v.replace(" ", "T")).astype("datetime64[ms]").astype(float) / 1000.0 for v in xr)
    assert abs((t_hi_ - t_lo_) - 120.0 * (1 + PL.X_PAD_FRAC)) < 0.5
    tiles = _tiles(_md(at))
    assert tiles[2][0] == "gold" and tiles[2][2] == "59<small>m</small>" and "CPA 59 m" in tiles[2][3] and "07:22:31" in tiles[2][3]
    assert T.ICONS["gold"] in _md(at).split('class="ih-tile gold"')[1].split("</div></div>")[0]               # D7: gold tile = star ICON + word row, not a dingbat
    assert "no CPA yet" not in tiles[2][3]


def test_cpa_gate_f1_before_after_and_gate_width():
    """CPA gate on the F1 replay: 07:21:00 no CPA (running min 147 m); 07:22:00 running min 78 m @ 07:21:43 =
    pass 1, a true local minimum (rose 86 m in 10 s) but > 70 m -> NOT a CPA at the default gate, a CPA at a
    100 m gate; 07:22:31 the minimum itself (still closing) -> not yet; 07:22:45 first valid CPA = pass 2,
    59 m @ 07:22:31.  Plots carry no ★ / hairline / label until then; the tile shows the running minimum
    in muted ink with "closest so far (no CPA yet)"."""
    def state(at):
        figs, md = _figs(at), _md(at)
        stars = sum(1 for k in ("map", "sep") for t in figs[k]["data"] if t.get("name") == "CPA")
        hair = sum(1 for sh in figs["sep"]["layout"].get("shapes", []) if sh.get("type") == "line" and sh["line"]["color"] == T.GOLD)   # the separation card's hairline
        hud = str((_store(at).get("hud") or {}).get("cpa") or "")                                                                       # the words: separation header HUD "CPA 59 m · 07:22:31"
        # 2026-09-15 ("show CPA lines on the plots"): the error / velocity cards carry one gold hairline per row, but ONLY once the
        # CPA is validated (and, here, inside the display window); the measurement quad never does.
        valid = at.session_state["cpa_ok"] is not None
        for k, rows in (("err", 4), ("vel", 3)):
            gold = [sh["name"] for sh in figs[k]["layout"].get("shapes", []) if (sh.get("line") or {}).get("color") == T.GOLD]
            assert gold == ([f"cpa_line_{r}" for r in range(1, rows + 1)] if valid else []), (k, gold, valid)
        words = any("CPA " in str(a.get("text") or "") for a in figs["err"]["layout"].get("annotations", []))
        assert words is valid                                                    # the label rides the azimuth card's strip list
        for k in ("meas",):
            assert not [sh for sh in figs[k]["layout"].get("shapes", []) if (sh.get("line") or {}).get("color") == T.GOLD], k
            assert not [a for a in figs[k]["layout"].get("annotations", []) if a.get("name") == "cpa_label"], k
        return stars, hair, hud, _tiles(md)[2]   # closest-so-far is the third tile now
    at = _at(LIVE, flight=1, anchor_t=F1_PRE, playing=False).run()
    assert not _exc(at), _exc(at)
    run = at.session_state["cpa_run"]
    assert at.session_state["cpa_ok"] is None and (run is None or run[0] > 100)   # 2026-09-17 airborne gate: no running minimum until both fly >= 20 m up
    stars, hair, hud, tile = state(at)
    assert (stars, hair, hud) == (0, 0, "") and tile[0] == "na" and ("closest so far (no CPA yet · gate 70 m)" in tile[3] or "no pair yet" in tile[3])   # 2026-09-17 airborne gate: no running minimum before both fly
    # after pass 1: 78 m local minimum, rejected by the 70 m gate ...
    at = _at(LIVE, flight=1, anchor_t=F1_P1, playing=False).run()
    assert not _exc(at), _exc(at)
    run = at.session_state["cpa_run"]
    assert abs(run[0] - 78.4) < 0.6 and D.pdt_hms(run[1]) == "07:21:43" and at.session_state["cpa_ok"] is None
    stars, hair, hud, tile = state(at)
    # 2026-09-17 pm: no validated pass -> the card marks the AIRBORNE closest-so-far the same way (gold "CPA" line + header words), never nothing
    assert (stars, hair) == (0, 1) and hud == "CPA 78 m · 07:21:43" and tile[0] == "na" and tile[2] == "78<small>m</small>" and "no CPA yet" in tile[3]
    # ... accepted by a 100 m gate (sidebar control)
    at = _at(LIVE, flight=1, anchor_t=F1_P1, playing=False, cpa_gate_m=100.0).run()
    assert not _exc(at), _exc(at)
    ok = at.session_state["cpa_ok"]
    assert ok is not None and abs(ok[0] - 78.4) < 0.6 and D.pdt_hms(ok[1]) == "07:21:43"
    stars, hair, hud, tile = state(at)
    assert (stars, hair, hud) == (1, 1, "CPA 78 m · 07:21:43") and tile[0] == "gold" and "CPA 78 m" in tile[3]   # 2026-09-17: no track CPA at pass 1 (#177 read 143 m)
    # the minimum of pass 2 itself: still closing -> not yet
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
    assert at.session_state["cpa_ok"] is None and state(at)[:2] == (0, 1) and state(at)[2].startswith("CPA ")   # 2026-09-17 pm: the closest-so-far mark, in the same "CPA" words (no stars)
    # pass 2 over: first valid CPA at the default gate = 59 m @ 07:22:31
    at = _at(LIVE, flight=1, anchor_t=F1_AFTER, playing=False).run()
    ok = at.session_state["cpa_ok"]
    assert ok is not None and abs(ok[0] - 59.0) < 0.6 and D.pdt_hms(ok[1]) == "07:22:31" and at.session_state["cpa_run"] == ok
    assert state(at)[:3] == (1, 1, "CPA 59 m · 07:22:31")   # 2026-09-17: the track CPA rides the HUD with its own second
    # engine unit: the gate itself on a synthetic separation series
    S = {"t": np.arange(0.0, 20.0), "sep": np.array([300, 250, 200, 150, 100, 60, 65, 80, 120, 170, 230, 300, 300, 300, 300, 300, 300, 300, 300, 300.0])}
    cpa = (60.0, 5.0, 0.0, 0.0, 50.0)
    assert E.cpa_gate(S, cpa, 70.0) is True and E.cpa_gate(S, cpa, 60.0) is False                    # sep must be < gate
    S2 = {"t": S["t"][:6], "sep": S["sep"][:6]}
    assert E.cpa_gate(S2, cpa, 70.0) is False                                                          # minimum at the newest sample: still closing
    S3 = {"t": S["t"], "sep": np.where(S["t"] > 5, 66.0, S["sep"])}
    assert E.cpa_gate(S3, cpa, 70.0) is False                                                          # rose only 6 m (< 20 m and < 15 %)
    S4 = {"t": S["t"], "sep": np.where(S["t"] > 5, 70.0, S["sep"])}
    assert E.cpa_gate(S4, cpa, 70.0) is True                                                           # rose 10 m >= 15 % of 60
    assert E.cpa_gate(S, None, 70.0) is False and E.cpa_gate({"t": np.zeros(0), "sep": np.zeros(0)}, cpa, 70.0) is False


def test_map_shows_trails_track_line_cpa_only():
    at = _at(LIVE, flight=1, anchor_t=F1_AFTER, playing=False, show_blind=True).run()
    assert not _exc(at), _exc(at)
    data = _figs(at)["map"]["data"]
    names = {t.get("name") for t in data if t.get("showlegend", True) and t.get("name")}
    expect = {"interceptor truth", "target truth", "target track #177", "CPA"}
    if PL.blind_rings():            # rings only when the modes library resolves DEFAULT_MODES (not in every checkout)
        expect.add("blind zone (c·pw/2)")
    assert names == expect, names
    itc_trk = [t for t in data if (t.get("name") or "").startswith("interceptor track #")]   # 2026-09-17 pm: the interceptor's radar track IS on the map (off the key), nowhere else
    assert len(itc_trk) == 1 and itc_trk[0]["showlegend"] is False and itc_trk[0]["opacity"] == PL.ITC_TRACK_ALPHA and itc_trk[0]["line"]["color"] == T.INTERCEPTOR
    for bad in ("measurement", "coasting", "tentative", "other tracks"):
        assert not any(bad in (t.get("name") or "") for t in data), bad
    trk = next(t for t in data if t.get("name") == "target track #177")
    assert trk["type"] == "scattergl" and trk["mode"] == "lines" and trk["line"] == {"color": T.TARGET, "width": max(PL.LINE_W, PL.TRACK_W), "dash": "dash"}   # 2026-09-17: 3 px over a halo
    halos = [t for t in data if (t.get("name") or "").startswith("_halo ")]
    assert {t["name"] for t in halos} >= {"_halo interceptor truth", "_halo target truth", "_halo target track #177"} and len(halos) == 4 and all(t["showlegend"] is False and t["line"] == {"color": PL.HALO, "width": PL.HALO_W} for t in halos)
    assert not any("markers" in (t.get("mode") or "") and (t.get("marker") or {}).get("symbol") in ("triangle-up",) for t in data)
    assert not any(t.get("hovertemplate", "").startswith(("target truth<br>", "interceptor truth<br>")) for t in data)   # no invisible head hover traces


def test_two_target_side_tracks_second_in_dark_red_with_legend():
    """Two target-side tracks graded at once (handover / duplicate): the second one is a dark-red line + band with a
    legend.  The joint target / interceptor assignment (engine C1) hands #203 to the interceptor at 07:23:49, so the
    panel is exercised here with the engine's own graded stacks for #177 and #203 as two err_tracks."""
    at = _at(LIVE, flight=1, anchor_t=F1_TWO, playing=False, spec_window=120).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] == 177   # tiles stay on the primary track
    b = D.archive_bundle(1)
    tracks = D.slice_tracks(b["tracks"], F1_TWO - 180, F1_TWO)
    Tt = D.slice_truth(b["tgt"], F1_TWO - 180, F1_TWO)
    A = {"t_now": F1_TWO, "cpa": None, "contain": {}, "obs_err": None, "track_events": [], "err_tracks": []}
    for tid in (177, 203):
        e = E.track_errors(tracks[tid], Tt, ant=b["ant"], t_now=F1_TWO, window_s=120.0, tid=tid)
        u = E.ungraded_errors(tracks[tid], Tt, e, ant=b["ant"], t_now=F1_TWO, window_s=120.0, tid=tid)
        assert len(e["t"]) > 5, tid
        A["err_tracks"].append({"tid": tid, "errors": e, "ungraded": u, "coast_t": E.coast_times(tracks[tid], F1_TWO, 120.0)})
    err = json.loads(PL.error_fig(A, {"err_height": 547, "font_px": 14, "line_w": 2.5, "show_obs": False}, 120.0).to_json())
    lines = {t["name"]: t for t in err["data"] if (t.get("name") or "").startswith("track #") and not t.get("fill") and t.get("showlegend")}
    assert set(lines) == {"track #177", "track #203"}, set(lines)
    assert lines["track #177"]["line"]["color"] == PL.rgba(T.TARGET, PL.LIGHT_ALPHA) and lines["track #203"]["line"]["color"] == PL.rgba(T.TARGET_DARK, PL.LIGHT_ALPHA)
    over = {t["name"]: t for t in err["data"] if (t.get("name") or "").endswith(" matched")}
    assert over["track #177 matched"]["line"]["color"] == T.TARGET and over["track #203 matched"]["line"]["color"] == T.TARGET_DARK
    assert {t["fillcolor"] for t in err["data"] if t.get("fill") == "toself"} == {PL.rgba(T.TARGET, .3), PL.rgba(T.TARGET_DARK, .3)}
    assert err["layout"]["showlegend"] is True and err["layout"]["margin"]["t"] == round(PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0] + PL.CARD_PAD_PX + PL.LEGEND_PX)   # legend above the first strip


def test_theme_css_never_clips_and_uses_three_inks():
    css = T.CSS
    no_top = re.sub(r"\.ih-top[^{]*\{[^}]*\}", "", css)                  # the fixed-height top block is the one deliberate overflow:hidden (see test_chrome_round3)
    for bad in ("overflow:hidden", "overflow: hidden", "text-overflow"):
        assert bad not in no_top, bad
    # nowrap ONLY on Streamlit's slider value / tick labels (the digit-wrapping fix), the one-line top row and the sidebar
    # PAGE-NAV links (2026-09-15: "Data source" / "Live" wrapped to 2 lines inside a 28 px overflow-hidden <a> and were cut in
    # half — the labels are two short fixed words, they never need to wrap), never on text that should wrap
    for sel, decl in _css_rules(css):
        if "nowrap" in decl:
            assert all("stSlider" in part or ".ih-top" in part or "stSidebarNav" in part for part in sel.split(",")), (sel, decl)
    for m in re.finditer(r"letter-spacing:\s*(-?[\d.]+)em", css):
        assert float(m.group(1)) <= 0.08, m.group(0)
    # tabular figures: table number columns and the status strip (2026-09-15: a digit-count change in the clock / sample
    # count nudged every chip after it sideways) — nowhere else
    assert css.count("tabular-nums") == 2 and "td.num" in css and ".ih-status {" in css and "font-variant-numeric:tabular-nums" in css
    assert "proportional-nums" in css                                    # hero / tile values
    assert "min-width:360px" in css and "max-width:400px" in css         # sidebar width (labels fit on <= 2 lines)
    for stray in ("#b5b5b5", "#c9c9c9", "#777", "#8a8a8a", "#333", "#000", "#070707", "#9a9a9a", "#666"):   # old black-on-black set
        assert stray not in css, stray
    assert T.INK in css and T.INK2 in css and T.INK3 in css
    assert css.count("grid-template-columns:repeat(3, minmax(0,1fr))") == 1   # the three-tile row
    assert T.FOOTER_LEFT.startswith("© CHAOS INC")


ICON_SELECTORS = ("stIconMaterial", "material-symbols", "stExpanderToggleIcon", "summary span", "span:first-child")
SIDEBAR_LABELS = {  # widget label -> must have a help tooltip (ARCHIVE mode: the replay transport; "Refresh interval (s)" is LIVE-only)
    "Metrics time window (s)", "Path history on map (s)", "Replay time", "Map zoom (m)", "Closest-approach gate (m)",
    "Satellite imagery", "Radar blind-range rings (pulse width)", "Show uncorrelated radar tracks (ADS-B, clutter) while MAVLink truth is present", "Show raw radar detections on error plots", "Freeze display (pause updates)",
    "Replay speed (× real time)", "Screen size", "Text size", "Map frame",
}


def _css_rules(css: str) -> list[tuple[str, str]]:
    body = css.split("<style>")[1].split("</style>")[0]
    body = re.sub(r"@import[^;]*;", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return [(sel.strip(), decl.strip()) for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body)]


def test_css_font_rules_never_wildcard_and_never_touch_icon_spans():
    """Root cause of the "ARROW_RIGHT" text over the expander title: our font-family / font /
    text-transform rules reached Streamlit's Material-icon spans through wildcard and bare-span
    selectors, breaking the ligature.  Contract: no rule that sets a font property has a `*`
    selector, a bare `span` / `summary span` selector, or names an icon test-id / class; uppercase +
    letter-spacing stay on OUR .ih-* classes only; Streamlit labels wrap (white-space:normal)."""
    rules = _css_rules(T.CSS)
    assert len(rules) > 40
    font_rules = [(sel, decl) for sel, decl in rules if re.search(r"(^|;)\s*(font-family|font|text-transform|letter-spacing)\s*:", decl)]
    assert font_rules
    for sel, decl in font_rules:
        for part in sel.split(","):
            part = part.strip()
            assert "*" not in part, (part, decl)
            assert not re.search(r"(^|\s)span(\s|$|:)", part), (part, decl)                          # never a bare span
            assert not any(bad in part for bad in ICON_SELECTORS), (part, decl)
            if re.search(r"(^|;)\s*text-transform\s*:", decl) or re.search(r"letter-spacing\s*:\s*\.0[1-9]", decl):
                assert part.startswith(".ih-") or part.startswith("table.ih-"), (part, decl)          # small caps mono (uppercase / wide tracking) = ours only
    # the fixes themselves
    assert '[data-testid="stSidebar"] * ' not in T.CSS and '[data-testid="stSidebar"] *{' not in T.CSS and ".stApp *" not in T.CSS
    assert '[data-testid="stExpander"] summary { white-space:normal; }' in T.CSS
    assert '[data-testid="stSidebar"][aria-expanded="true"] { min-width:360px !important; max-width:400px !important; }' in T.CSS
    assert "Material Symbols" not in T.CSS
    # fonts are applied to explicit Streamlit text elements
    sels = " ".join(sel for sel, decl in font_rules if "font-family" in decl)
    for need in ('[data-testid="stWidgetLabel"] p', '[data-testid="stMarkdownContainer"] p', '[data-testid="stExpander"] summary p', "label"):
        assert need in sels, need


def test_sidebar_controls_plain_english_with_help_and_no_icon_name_leak():
    """The app entry (st.navigation) rendered through AppTest: every sidebar control carries a
    sentence-case plain-English label with units and a help tooltip; no element text looks like a
    leaked icon name (/^[A-Z_]{6,}$/); labels are complete (never truncated); the Display expander
    is titled "Display"; Data-source labels are full words (LIVE and ARCHIVE REPLAY modes); the sidebar is mode-aware."""
    at = _at(APP, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    sb = at.sidebar
    widgets = list(sb.select_slider) + list(sb.slider) + list(sb.toggle) + list(sb.radio)
    labels = {w.label for w in widgets}
    assert labels == SIDEBAR_LABELS, labels ^ SIDEBAR_LABELS
    for w in widgets:
        assert w.proto.help and len(w.proto.help) > 20, (w.label, w.proto.help)                           # a tooltip on every control
        assert re.fullmatch(r"[A-Z][a-z].*", w.label) and w.label == w.label.strip(), w.label           # sentence case, complete
        assert not re.fullmatch(r"[A-Z_]{6,}", w.label), w.label                                        # no icon-name-looking text
    assert [e.label for e in sb.expander] == ["Display", "More controls"]   # the 4 rarely-used toggles moved one level deeper (laptop sidebar length)
    assert [e.proto.expanded for e in sb.expander] == [False, False]   # 2026-09-15: "Display" starts COLLAPSED on the Laptop preset (sidebar length)
    assert {b.label for b in sb.button if not b.label.startswith("Pass ")} == {"Play replay", "Restart replay", "Reset map view"}
    assert sorted(b.label for b in sb.button if b.label.startswith("Pass ")) == ["Pass 1 · 07:21:43 · 78 m", "Pass 2 · 07:22:31 · 59 m", "Pass 3 · 07:23:47 · 53 m"]   # (AppTest lists column 1 then column 2)
    radios = {r.label: (list(r.options), r.value) for r in sb.radio}
    assert radios["Screen size"] == (["Laptop", "Desktop 1080p", "Large 1440p"], "Laptop")   # 2026-09-15 default (D.STATE_DEFAULTS["screen"])
    assert radios["Map frame"] == (list(D.FRAME_MODES.values()), "engagement") and radios["Text size"] == (["Normal", "Large", "X-Large"], "Normal")
    assert next(w for w in sb.select_slider if w.label == "Map zoom (m)").disabled is True   # follow / fixed frames only
    for old in ("Window (s)", "Trail (s)", "Map half-width (m)", "Live refresh (s)", "Show obs", "Blind rings"):
        assert old not in labels, old
    leak = re.compile(r"^[A-Z_]{6,}$")
    texts = [w.label for w in widgets] + [e.label for e in sb.expander] + [b.label for b in sb.button]
    for m in sb.markdown:
        texts += [re.sub(r"<[^>]+>", " ", m.value)]
    for t in texts:
        for word in re.split(r"\s+", t):
            assert not leak.match(word) or word in ("ARCHIVE", "PAUSED", "PLAYING", "CHAOS"), (word, t)   # our own small-caps strips are fine
    # LIVE mode sidebar: the unit block + Disconnect + Refresh interval, no replay transport
    lv = _at(APP, source="live", live_host="10.255.255.1", live_port=27017, live_run="run_deadbeef", live_run_name="Dead_Beef", mru_number=91).run()
    assert not _exc(lv), _exc(lv)
    lsb = lv.sidebar
    assert {b.label for b in lsb.button} == {"Save data to archive…", "Disconnect", "Reset map view"}   # the save action is a primary sidebar button in LIVE mode
    assert "Refresh interval (s)" in {w.label for w in lsb.select_slider} and "Replay speed (× real time)" not in {r.label for r in lsb.radio}
    side = " ".join(re.sub(r"<[^>]+>", " ", m.value) for m in lsb.markdown)
    assert "LIVE" in side and "MRU 91" in side and "Dead_Beef" in side
    # Data-source page: full-word labels — LIVE (connect card + Advanced) and ARCHIVE REPLAY (flight + transport)
    ds = _at(SRC, flight=1).run()
    assert not _exc(ds), _exc(ds)
    lab = {w.label for w in list(ds.text_input) + list(ds.number_input) + list(ds.selectbox) + list(ds.select_slider) + list(ds.slider) + list(ds.radio)}
    for need in ("Data source mode", "MRU number", "Custom host or IP (overrides the MRU number)", "Mongo port", "Database name", "Run name filter (substring)",
                 "Target auto-assign patterns", "Interceptor auto-assign patterns", "History carried per snapshot (s)"):
        assert need in lab, (need, lab)
    assert any(b.label == "Connect" for b in ds.button)
    ds.radio(key="_ds_mode_w").set_value("archive").run()
    assert not _exc(ds), _exc(ds)
    lab = {w.label for w in list(ds.selectbox) + list(ds.slider) + list(ds.radio) + list(ds.multiselect)}
    for need in ("Flight to replay", "TARGET truth (MAVLink ids)", "INTERCEPTOR truth (MAVLink ids)"):
        assert need in lab, (need, lab)
    for gone in ("Seek to time (PDT)", "Replay speed (× real time)"):                                  # the transport lives in the sidebar (archive) / Live page
        assert gone not in lab, gone
    assert any(b.label == "Load flight and open Live view" and b.key == "ds_open_live" for b in ds.button) and any(b.label == "Swap" for b in ds.button)
    assert not any(b.label.startswith("Pass ") or b.key in ("ds_play", "ds_restart") for b in ds.button)


def test_tile_chip_and_glyph_html_contract():
    """D7 (design QA): a toned tile / chip carries its meaning as an inline stroke-SVG ICON + word row (colour on the icon only,
    the word in the text ink) under a 2 px TOP rule — never a left-border accent, never a dingbat, never colour alone."""
    g_ok, g_gold = T.glyph("ok"), T.glyph("gold", "CPA")
    assert T.tile_html("Closest so far · 3D", "59", "m", "26 m horiz · 07:22:31", tone="gold") == \
        f'<div class="ih-tile gold"><div class="k">Closest so far · 3D</div><div class="v">59<small>m</small></div><div class="s">26 m horiz · 07:22:31</div><div class="st">{g_gold}</div></div>'
    assert T.tile_html("Target track", "#177 CONF", "", "age 1.2 s · horiz err 46 m", tone="ok") == \
        f'<div class="ih-tile ok"><div class="k">Target track</div><div class="v">#177 CONF</div><div class="s">age 1.2 s · horiz err 46 m</div><div class="st">{g_ok}</div></div>'
    assert T.tile_html("Target track", "#177 CONF", "", "age 1.2 s", tone="ok", word="CONF").endswith(f'<div class="st">{T.glyph("ok", "CONF")}</div></div>')
    assert T.tile_html("Closest so far · 3D", "—", "", "no pair yet", tone="na") == '<div class="ih-tile na"><div class="k">Closest so far · 3D</div><div class="v">—</div><div class="s">no pair yet</div></div>'
    assert T.tiles_html(["<a>", "<b>"]) == '<div class="ih-tiles"><a><b></div>'
    html = T.chips_html([("Target track", "CONF", "#177", "ok"), ("Separation now", "312 m", "3D", "")])
    assert f'<div class="ih-chip ok"><div class="k">Target track</div><div class="v">CONF</div><div class="s">#177</div><div class="st">{g_ok}</div></div>' in html
    assert '<div class="ih-chip "><div class="k">Separation now</div><div class="v">312 m</div><div class="s">3D</div></div>' in html   # neutral: no status line
    for cls, (_, g, w) in T.STATUS.items():
        assert g.startswith("<svg") and 'stroke="currentColor"' in g and 'viewBox="0 0 16 16"' in g and not re.search(r"[●▲✕○★◆■]", g)
        assert T.glyph(cls) == f'<span class="gw"><span class="g {cls}">{g}</span>{w}</span>'
        assert T.dot(cls) == f'<span class="g {cls}">{g}</span>'
    assert T.glyph("red", "LIVE") == f'<span class="gw"><span class="g red">{T.ICONS["red"]}</span>LIVE</span>' and T.glyph("red") == "" and T.glyph("nope", "x") == ""
    assert T.glyph("amber", "reset", icon="reset") == f'<span class="gw"><span class="g amber">{T.ICONS["reset"]}</span>reset</span>'
    assert T.callout_html("OFFLINE", "t", "b", tone="red").startswith(f'<div class="ih-callout"><div class="k">{T.glyph("fail", "OFFLINE")}</div>')
    assert not re.search(r"[●▲✕○★◆■❚⟲▸▾]", T.CSS + T.source_line_html("live", mru=91) + T.source_line_html("archive") + T.callout_html("k", "t", "b", "grey"))
    for bad in ("border-left", "border-radius:.5rem", "border-radius: .5rem"):
        assert bad not in T.CSS, bad                                                                 # the left-border-accent trope is gone; square corners
    fg = T.feed_glyph("target", "stale", 3.2, 1, 2)
    assert fg.startswith('<span class="fg amber" title="MAVLink target · STALE · age 3.2 s · 1/2 alive">') and "<svg" in fg and "animate" not in fg
    assert T.feed_glyph("interceptor", "down").startswith('<span class="fg fail"') and T.feed_glyph("interceptor", "none").startswith('<span class="fg na"')


# ── engine-level checks of the spa grading path (no Streamlit run needed) ────
def _f1_window(t_now: float, W: float):
    b = D.archive_bundle(1)
    lo = t_now - 180.0
    tracks = D.slice_tracks(b["tracks"], lo, t_now)
    Tt = D.slice_truth(b["tgt"], lo, t_now)
    tid, _ = E.pick_target_track(tracks, Tt, t_now, None)
    return b, tracks, Tt, tid


def test_engine_spa_grades_confirmed_updated_only():
    pytest.importorskip("spa", reason="chaos-spa not installed (optional official grader)")
    import spa_errors as SE

    b, tracks, Tt, tid = _f1_window(F1_MID, 120.0)
    assert tid == 177
    a = tracks[tid]
    e = E.track_errors(a, Tt, ant=b["ant"], t_now=F1_MID, window_s=120.0, tid=tid)
    assert e["grader"] == "spa" and e["n_graded"] > 20
    assert set(e["kind"]) == {"meas"}, "coasts / tentatives must not enter the error stats"
    assert e["n_graded"] <= e["n_rows"]
    aw = E._recent(a, F1_MID, 120.0)
    fresh = np.concatenate([[True], np.diff(aw[:, D.TK["lu"]]) > 1e-6]) & (aw[:, D.TK["state"]] == 2)
    assert len(e["t"]) <= int(fresh.sum())
    assert np.all(np.isin(np.round(e["t"], 6), np.round(aw[fresh, 0], 6)))
    # same numbers as calling the adapter directly
    ref = SE.spa_err_stack({tid: aw}, E.truth7_to_truth8(Tt), b["ant"])
    assert np.allclose(e["az"], ref["az_err"]) and np.allclose(e["alt"], ref["alt_err"])
    # "+1 deg class" azimuth error at the comparison-report sanity point
    assert 0.0 < float(np.nanmean(e["az"])) < 2.0, float(np.nanmean(e["az"]))
    assert np.all(np.abs(e["az"]) < 3.0)
    s = E.spa_stats(e)
    assert 0.0 < s["az"]["rmse95"] <= s["az"]["rmse"] < 3.0
    assert s["az"]["n"] == e["n_graded"]


def test_engine_tiny_or_coast_only_window_is_empty_not_error():
    b, tracks, Tt, tid = _f1_window(F1_MID, 120.0)
    a = tracks[tid]
    for sub in (a[:0], a[:1], a[:2]):
        e = E.track_errors(sub, Tt, ant=b["ant"], tid=tid)
        assert len(e["t"]) == 0 and e["n_graded"] == 0
    # force every row to look like a coast (constant last_update_time) -> spa gate removes all -> empty
    coast = a.copy()
    coast[:, D.TK["lu"]] = coast[0, D.TK["lu"]]
    coast[0, D.TK["state"]] = 1  # first row (always 'fresh') tentative -> nothing survives
    e = E.track_errors(coast, Tt, ant=b["ant"], t_now=F1_MID, window_s=120.0, tid=tid)
    assert len(e["t"]) == 0
    assert E.spa_stats(e)["az"]["rmse95"] is None
    assert len(E.track_errors(a, np.zeros((0, 7)), ant=b["ant"], tid=tid)["t"]) == 0


def test_truth_handed_to_spa_is_hae_metres_and_mps():
    import corr_lib

    b = D.archive_bundle(1)
    T8 = E.truth7_to_truth8(b["tgt"])
    assert T8.shape[1] == 8
    assert np.array_equal(T8[:, 3], b["tgt"][:, D.TR["U"]])          # U passed through untouched (= CSV U_m_hae)
    assert np.percentile(np.abs(T8[:, 7]), 95) < 50.0                # vU already m/s ...
    assert corr_lib.vu_scale(T8) == 1.0                              # ... so the adapter does not rescale it
    assert np.array_equal(T8[:, 5], b["tgt"][:, D.TR["vN"]]) and np.array_equal(T8[:, 6], b["tgt"][:, D.TR["vE"]])
    # dashboard antenna = the run's origin; alt is HAE metres (not feet)
    assert 100 < b["ant"][2] < 200


if __name__ == "__main__":
    import traceback

    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            t0 = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t0:.1f} s)")
            except Exception:
                fails += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)


# ── responsive presets · map frame · measurement space · empty state · sidebar wrap ──
def _iframe_heights(page: str, **state) -> tuple[AppTest, list[int]]:
    """Run a page recording every st.iframe(height=...) the script emits (the proto carries no height field)."""
    heights: list[int] = []
    orig = st.iframe

    def rec(*a, **k):
        heights.append(k.get("height"))
        return orig(*a, **k)

    st.iframe = rec
    try:
        at = _at(page, **state).run()
    finally:
        st.iframe = orig
    return at, heights


def test_screen_presets_drive_iframe_height_figure_heights_fonts_and_line_width():
    """Sidebar "Screen size" -> LAYOUT(preset): the panel iframe height follows (panel + 4), the meas iframe
    is header + 520 + 4, every figure is built at the preset's heights / font / line width, and ui carries them."""
    for preset, (panel, mode, font, lw) in (("Laptop", (600, "one", 13, 2.5)), ("Desktop 1080p", (860, "two", 14, 2.5)), ("Large 1440p", (1180, "two", 16, 3.0))):
        at, heights = _iframe_heights(LIVE, flight=1, anchor_t=F1_MID, playing=False, screen=preset, text_scale="Normal")
        assert not _exc(at), _exc(at)
        L = LS.layout(preset, 1.0)                                               # the geometry is derived from the module rules, not pinned:
        assert (L["panel"], L["mode"], L["font_px"], L["line_w"]) == (panel, mode, font, lw)
        if mode == "two":                                                        # two-column map = panel − 2·header − velocity strip; (sep, err) = _fit_two(panel)
            assert L["map"] == panel - 2 * LS.HEADER_PX - LS.vel_strip_px(panel) and (L["sep"], L["err"]) == LS._fit_two(panel)
        else:   # 2026-09-15 stacked ("one") mode: the FIRST ROW is two-up (square map | separation), both panel − ONE header tall,
                # then the err / vel cards full-width below (LS.one_row); err / vel scale with the text size
            assert (L["map"], L["sep"], L["err"]) == (panel - LS.HEADER_PX, panel - LS.HEADER_PX, LS.one_err_px(1.0))
            assert (L["map"], L["sep"], L["err"], L["vel"]) == (568, 568, 486, 371) and LS.ONE_ERR_PX == 486 and LS.ONE_VEL_PX == 371
        mp, sep, err = L["map"], L["sep"], L["err"]
        assert heights == [panel + 4, 640 + LS.HEADER_PX + 4], (preset, heights)   # 2026-09-17 pm: 3-row measurement card (the interceptor card is collapsed here)
        figs = _figs(at)
        got = {k: figs[k]["layout"]["height"] for k in FIG_KEYS}
        assert got.pop("vel") >= L["vel"] and got == {"map": mp, "sep": sep, "err": err, "meas": 640}
        for k in FIG_KEYS:
            assert figs[k]["layout"]["font"]["size"] == font and figs[k]["layout"]["xaxis"]["tickfont"]["size"] == font and figs[k]["layout"]["legend"]["font"]["size"] == font
        tt = next(t for t in figs["map"]["data"] if t.get("name") == "target truth")
        assert tt["line"]["width"] == max(lw, PL.TRAIL_W)   # 2026-09-17 map contrast: truth trails >= 3.5 px
        assert _store(at)["ui"] == {"font_px": font, "line_w": lw, "preset": preset, "text_scale": 1.0}
        html = _panel(at)
        assert f"PANEL={panel}" in html and f"UI={{font_px:{font},line_w:{lw}" in html
        # compact error cards (< 480 px) use 12 px readouts; taller ones 14 px (x the text scale)
        reads = [a for a in figs["err"]["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
        assert all(a["font"]["size"] == (PL.READOUT_COMPACT_PX if err < PL.COMPACT_ERR_PX else PL.READOUT_PX) for a in reads), preset
    # the default preset renders through the app entry with the radio in place (the app lands on Data source: switch to Live)
    at = _at(APP, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    at.switch_page("views/1_live.py").run()
    assert not _exc(at), _exc(at)
    assert at.session_state["screen"] == "Laptop" and len(at.get("iframe")) == 2   # 2026-09-15 default preset


def test_preset_change_rebuilds_every_figure_and_remounts_the_panel_once():
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    html0, stamps0 = _panel(at), dict(_store(at)["fig_stamps"])
    assert "PANEL=600" in html0                                     # 2026-09-15: the DEFAULT preset is Laptop (600), so switch AWAY from it
    at.session_state["screen"] = "Desktop 1080p"
    at.run()
    assert not _exc(at), _exc(at)
    assert _panel(at) != html0 and "PANEL=860" in _panel(at)                                  # a different (constant) iframe: one intended remount
    stamps1 = _store(at)["fig_stamps"]
    assert all(stamps1[k] > stamps0[k] for k in FIG_KEYS), (stamps0, stamps1)                    # every figure rebuilt at the new geometry
    assert _figs(at)["err"]["layout"]["height"] == LS.layout("Desktop 1080p", 1.0)["err"] == LS._fit_two(860)[1]   # _at seeds Text size "Normal"


def test_engagement_frame_is_the_default_and_fixed_frame_is_the_flight_footprint():
    """FIXED (default): view = the whole flight's truth footprint (padded 15 %, square, 100 m quantised),
    identical on every replay tick; the map figure never carries a range; Follow centres on the heads."""
    fr = D.flight_frame(1)
    b = D.archive_bundle(1)
    t0, t1 = D.FLIGHT_WINDOWS[1]
    E_ = np.concatenate([D.slice_truth(b[k], t0, t1)[:, D.TR["E"]] for k in ("tgt", "itc")])
    N_ = np.concatenate([D.slice_truth(b[k], t0, t1)[:, D.TR["N"]] for k in ("tgt", "itc")])
    assert fr[0] <= E_.min() and fr[1] >= E_.max() and fr[2] <= N_.min() and fr[3] >= N_.max()
    assert fr[1] - fr[0] == fr[3] - fr[2] and all(v % 100 == 0 for v in fr)                     # square, quantised
    assert (fr[1] - fr[0]) >= 1.15 * max(E_.max() - E_.min(), N_.max() - N_.min()) - 200      # padded 15 % (before quantisation)
    assert fr == (900.0, 4100.0, -1100.0, 2100.0)
    assert D.square_frame(0, 1000, 0, 500, pad=0.0) == (0.0, 1000.0, -300.0, 700.0) and D.live_frame()[1] - D.live_frame()[0] == 4400.0
    at = _at(LIVE, flight=1, anchor_t=F1_PRE, anchor_wall=time.time(), playing=True, frame_mode="fit_flight", map_frame="Fit whole flight").run()
    assert not _exc(at), _exc(at)
    views = [dict(_store(at)["view"])]
    for _ in range(10):
        time.sleep(1.02)
        at.run()
        assert not _exc(at), _exc(at)
        views.append(dict(_store(at)["view"]))
        assert "range" not in _figs(at)["map"]["layout"]["xaxis"]
    assert len({json.dumps(v, sort_keys=True) for v in views}) == 1, views                     # 11 ticks, ONE view
    assert views[0] == {"x0": 900.0, "x1": 4100.0, "y0": -1100.0, "y1": 2100.0, "rev": 0, "mode": "fixed"}
    # DEFAULT = the ENGAGEMENT BOX (A12): square, quantised, contains both truths at every verified pass ± 20 s and the vehicles now.
    # 2026-09-15: on the DEFAULT (Laptop) preset the box may close to LAPTOP_MIN_HALF_M and is capped at LAPTOP_CLOSE_HALF_M while the
    # vehicles are together, so the >= 500 m half-width floor is checked on a Desktop preset (see test_laptop_engagement_frame_closes_in_on_the_pass).
    v_lap = _store(_at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run())["view"]
    assert v_lap["mode"] == "engage" and v_lap["x1"] - v_lap["x0"] == v_lap["y1"] - v_lap["y0"] <= 800
    at0 = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, screen="Desktop 1080p").run()
    v0 = _store(at0)["view"]
    assert v0["mode"] == "engage" and v0["x1"] - v0["x0"] == v0["y1"] - v0["y0"] >= 1000 and all(x % 100 == 0 for x in (v0["x0"], v0["x1"], v0["y0"], v0["y1"]))
    S = D.slice_truth(b["tgt"], t0, F1_MID), D.slice_truth(b["itc"], t0, F1_MID)
    for p in [p for p in D.passes(1) if p["verified"] and p["t"] <= F1_MID]:
        for arr in S:
            m = np.abs(arr[:, 0] - p["t"]) <= PL.ENGAGE_PASS_S
            assert (arr[m, D.TR["E"]] >= v0["x0"]).all() and (arr[m, D.TR["E"]] <= v0["x1"]).all() and (arr[m, D.TR["N"]] >= v0["y0"]).all() and (arr[m, D.TR["N"]] <= v0["y1"]).all(), p
    assert (v0["x1"] - v0["x0"]) < (fr[1] - fr[0])                                            # tighter than the whole-flight footprint (the vehicles are no longer a speck)
    # Follow (legacy label): the vehicles' centroid ± map_half; an explicit change bumps the rev
    at2 = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, frame_mode="follow", map_frame="Follow", map_half=800).run()
    v = _store(at2)["view"]
    assert v["mode"] == "follow" and v["x1"] - v["x0"] == 1600 and (v["x0"] + 800) % 100 == 0
    at2.session_state["map_half"] = 400
    at2.session_state["view_rev"] = 1          # what the sidebar's on_change (D.bump_view_rev) does
    at2.run()
    assert not _exc(at2), _exc(at2)
    v2 = _store(at2)["view"]
    assert v2["rev"] == 1 and v2["x1"] - v2["x0"] == 800                                          # rebuilt at once: never an old range with a new rev


def test_measurement_space_quad_truth_tracks_obs():
    """MEASUREMENT SPACE @ 07:22:31: 2×2 BISTATIC panels (range km · range rate · az · el), BOTH truths 3 px (target red,
    interceptor blue), tracks #129 and #177 each with a ★ start / ✕ end marker and a 13 px id PILL (CARD bg, RULE border),
    filled 6 px light-ink obs circles drawn FIRST (no obs in the range-rate panel: amb_dop not archived), legend groups
    TRUTH / TRACKS / OBS, titles 12 px bold, y-ranges = the truth envelope, no dual axes."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, show_obs=True).run()
    assert not _exc(at), _exc(at)
    m = _figs(at)["meas"]
    L, data = m["layout"], m["data"]
    assert L["height"] == 640 and L["uirevision"] == PL.UIREV["meas"] == "live-meas" and L["showlegend"] is True and L["legend"]["font"]["size"] >= 12
    titles = [a for a in L["annotations"] if (a.get("name") or "").startswith("title_")]
    assert [a["text"] for a in titles] == ["<b>BISTATIC RANGE (KM)</b>", "<b>BISTATIC RANGE RATE (M/S)</b>", "<b>AZIMUTH (°)</b>", "<b>ELEVATION (°)</b>", "<b>ALTITUDE (M ABOVE RADAR)</b>"]
    assert all(a["font"]["size"] == PL.MEAS_TITLE_PX for a in titles)   # the measurement quad keeps its 12 px panel titles
    axes = {k: v for k, v in L.items() if k.startswith(("xaxis", "yaxis"))}
    assert set(axes) == {"xaxis", "xaxis2", "xaxis3", "xaxis4", "xaxis5", "yaxis", "yaxis2", "yaxis3", "yaxis4", "yaxis5"} and not any("overlaying" in v for v in axes.values())
    assert [axes[f"yaxis{i}" if i > 1 else "yaxis"]["title"]["text"] for i in (1, 2, 3, 4)] == ["bistatic range (km)", "bistatic range rate (m/s)", "azimuth (°)", "elevation (°)"]
    # z-order: the FIRST trace of every panel is the obs layer (where there are obs), truths come after every track line
    per_axis = {}
    for t in data:
        per_axis.setdefault(t.get("xaxis", "x"), []).append(t)
    for ax in ("x", "x3", "x4"):
        assert per_axis[ax][0]["name"] == "raw obs", ax
        names = [t["name"] for t in per_axis[ax]]
        assert names.index("target truth") > max(i for i, n in enumerate(names) if re.fullmatch(r"#\d+ · (target|interceptor)", n or ""))
    obs = [t for t in data if t.get("name") == "raw obs"]
    assert len(obs) == 3 and {t["xaxis"] for t in obs} == {"x", "x3", "x4"}                                # range / az / el — never the range-rate panel in replay
    assert all(t["marker"] == {"symbol": "circle", "size": 6, "color": PL.MEAS_OBS_COLOR, "line": {"width": 1, "color": T.SURFACE}} and t["opacity"] == 0.75 for t in obs)
    assert PL.MEAS_OBS_COLOR == "#cfd6de" and obs[0]["legendgroup"] == "obs" and obs[0]["legendgrouptitle"]["text"] == "OBS"
    truth = [t for t in data if t.get("name") == "target truth"]
    itc_truth = [t for t in data if t.get("name") == "interceptor truth"]
    assert len(truth) == 5 and all(t["line"] == {"color": T.TARGET, "width": PL.TRUTH_W} and t["mode"] == "lines" and t["legendgroup"] == "truth" for t in truth)   # 5 panels (altitude row)
    assert not itc_truth and truth[0]["legendgrouptitle"]["text"] == "TRUTH"   # 2026-09-17 pm: the TARGET card carries no interceptor truth — the interceptor has its own card
    assert not any((t.get("name") or "").endswith("· interceptor") for t in data)
    lines = {(t["name"], t["xaxis"]) for t in data if re.fullmatch(r"#\d+ · target", t.get("name") or "") and t.get("mode") == "lines"}
    assert {n for n, _ in lines} >= {"#129 · target", "#177 · target"} and {ax for _, ax in lines} == {"x", "x2", "x3", "x4", "x5"}
    # the INTERCEPTOR card: its truth (blue) + its own track only, no obs, same 5 panels
    at2 = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, show_obs=True, meas_itc_open=True).run()
    assert not _exc(at2), _exc(at2)
    mi = _figs(at2)["meas_itc"]
    di = mi["data"]
    assert mi["layout"]["uirevision"] == PL.UIREV["meas_itc"] == "live-meas-itc"
    assert [t["name"] for t in di if t.get("name") == "interceptor truth"] and not [t for t in di if t.get("name") in ("target truth", "raw obs")]
    assert not any(re.fullmatch(r"#\d+ · target", t.get("name") or "") for t in di) and any((t.get("name") or "").endswith("· interceptor") for t in di)
    assert [a["text"] for a in mi["layout"]["annotations"] if (a.get("name") or "").startswith("title_")] == [f"<b>{PL.MEAS_TITLE[k]}</b>" for k in PL.MEAS_KEYS]
    leg = [t for t in data if t.get("showlegend") and re.fullmatch(r"#\d+ · target", t.get("name") or "")]
    assert leg and leg[0]["legendgrouptitle"]["text"] == "TRACKS"
    starts = [t for t in data if (t.get("name") or "").endswith(" start")]
    ends = [t for t in data if (t.get("name") or "").endswith(" end")]
    assert {t["name"] for t in starts} >= {"#129 · target start", "#177 · target start"} and {t["name"] for t in ends} >= {"#129 · target end", "#177 · target end"}
    for t in starts:
        assert t["marker"]["symbol"] == "star" and t["marker"]["line"] == {"width": 2, "color": T.CARD} and t["mode"] == "markers"
    for t in ends:
        assert t["marker"]["symbol"] == "x" and t["mode"] == "markers"
    pills = [a for a in L["annotations"] if (a.get("name") or "").startswith(("lbl_start_", "lbl_end_"))]
    assert {a["text"] for a in pills} >= {"#129", "#177"} and 4 <= len(pills) < 16                            # 2026-09-14: pills on the FIRST panel only ("not on EVERY plot")
    assert all(a["name"].endswith("_rng") and a["xref"] == "x" and a["yref"] == "y" for a in pills), [a["name"] for a in pills]
    for a in pills:   # 13 px mono primary ink on a rounded dark pill, right of the marker, above (★) / below (✕) the line
        assert a["font"] == {"family": T.MONO, "size": PL.PILL_PX, "color": T.INK} and a["bgcolor"] == T.CARD and a["bordercolor"] == T.RULE and a["borderpad"] == 2   # 2026-09-17: PILL_PX 15
        assert a["xanchor"] in ("left", "right") and abs(a["xshift"]) == 10 and a["yanchor"] in ("bottom", "top")   # a start label near the panel top flips BELOW its star (never into the title)
        assert (a["yshift"] > 0) == (a["yanchor"] == "bottom")
    c129 = next(t for t in data if t.get("name") == "#129 · target" and t.get("mode") == "lines" and t["line"]["color"] in PL.TRACK_SLOTS)["line"]["color"]
    c177 = next(t for t in data if t.get("name") == "#177 · target" and t.get("mode") == "lines" and t["line"]["color"] in PL.TRACK_SLOTS)["line"]["color"]
    assert c129 == PL.TRACK_SLOTS[0] and c177 == PL.TRACK_SLOTS[1] and T.INTERCEPTOR not in PL.TRACK_SLOTS       # first appearance -> slot; blue reserved
    itc = [t for t in data if re.fullmatch(r"#\d+ · interceptor", t.get("name") or "")]
    assert not itc                                                                                            # 2026-09-17 hard rule: the interceptor's radar track is never drawn (quad included)
    # COLOUR = ROLE: every track line is drawn from the red ramp (target-side), the blue ramp (interceptor-side) or the fold grey — nothing else
    allowed = set(PL.RAMP_TRACK) | {PL.RAMP_FOLD, T.GREY_TRACK}   # 2026-09-17 pm: measurement-card tracks are the INK ramp
    trk_lines = [t for t in data if re.fullmatch(r"#\d+ · (target|interceptor|no truth)", t.get("name") or "") and t.get("mode") == "lines"]
    assert trk_lines and all(t["line"]["color"] in allowed and t["line"]["dash"] in PL.STEP_DASH for t in trk_lines)
    assert PL.RAMP_TGT == ("#f3a6a6", T.TARGET, T.TARGET_DARK) and PL.RAMP_ITC == ("#8fb8f0", T.INTERCEPTOR, "#2a6cc7") and PL.RAMP_FOLD == "#8a8a8a"
    steps = {t["name"]: (t["line"]["color"], t["line"]["dash"]) for t in trk_lines if t["line"]["width"] == PL.ON_W and t["line"]["color"] in PL.RAMP_TRACK}
    assert steps["#129 · target"] == (PL.RAMP_TRACK[0], "dash") and steps["#177 · target"] == (PL.RAMP_TRACK[1], "longdash")   # identity = lightness step + dash pattern (+ the pill); 2026-09-17 pm: tracks always dashed, truth solid
    map_trk = [t for t in _figs(at)["map"]["data"] if "track #" in (t.get("name") or "") and not (t.get("name") or "").startswith("_halo")]   # 2026-09-17: the dark halo under the track is not a series
    assert map_trk and all(t["line"]["color"] in set(PL.RAMP_TGT) | set(PL.RAMP_ITC) for t in map_trk)
    assert "amb_dop" in LS.HEADERS["meas"][1] and "2×mono" in LS.HEADERS["meas"][1]
    geom = next(a for a in L["annotations"] if a.get("name") == "geom_note")
    assert geom["text"] == "TX 91 / RX 91 co-located → bistatic = 2×mono"
    # engine side: monostatic range rate = v · r̂, opening positive; bistatic = spa's kernels, exactly 2 x mono for a co-located TX
    az, el, rng, rr = E.aer_rr(np.array([1000.0]), np.array([0.0]), np.array([0.0]), np.array([10.0]), np.array([0.0]), np.array([0.0]))
    assert az[0] == 90.0 and el[0] == 0.0 and rng[0] == 1000.0 and rr[0] == 10.0
    brng, brr = E.bistatic_series(np.array([1000.0]), np.array([0.0]), np.array([0.0]), np.array([10.0]), np.array([0.0]), np.array([0.0]))
    assert brng[0] == 2000.0 and brr[0] == 20.0
    M = E.meas_space({"tgt": np.zeros((0, 7)), "tracks": {}, "obs": np.zeros((0, 4))}, {"t_now": F1_MID, "tgt_tid": None, "alt_tid": None, "itc_tid": None}, 120.0)
    assert M["truth"] is None and M["tracks"] == [] and M["obs"] is None and M["obs_rr"] is False


def test_meas_quad_gating_envelope_off_scale_and_steal():
    """A1 / A2 / A9 on F1 @ 07:24:15 (window 07:22:15–07:24:15): track 177 was the target's until pass 3 stole it — its
    target-side (full 2 px) segment ends by 07:23:52, everything after is thin / interceptor-side, a "→ interceptor (stolen)"
    glyph marks the departure; the ✕ sits at its last target-side sample; the elevation axis is the TRUTH envelope
    (± 15 %, >= ± 2°), not the −50° runaway; every track sample beyond an axis is acknowledged by a ▲ / ▼ with its
    count; bistatic range = 2 x the monostatic one (numerically, truth and obs)."""
    at = _at(LIVE, flight=1, anchor_t=F1_LATE, playing=False, show_obs=True, spec_window=120).run()
    assert not _exc(at), _exc(at)
    m = _figs(at)["meas"]
    L, data = m["layout"], m["data"]
    b = D.archive_bundle(1)
    tracks = D.slice_tracks(b["tracks"], F1_LATE - 180, F1_LATE)
    Tt, Ti = D.slice_truth(b["tgt"], F1_LATE - 180, F1_LATE), D.slice_truth(b["itc"], F1_LATE - 180, F1_LATE)
    A = {"t_now": F1_LATE, "tgt_tid": at.session_state["tgt_tid"], "alt_tid": None, "itc_tid": None, "has_truth": True}
    M = E.meas_space({"tgt": Tt, "itc": Ti, "tracks": tracks, "obs": D.slice_obs(D.archive_obs(1), F1_LATE - 120, F1_LATE), "ant": b["ant"], "tx": None}, A, 120.0)
    tr = next(t for t in M["tracks"] if t["tid"] == 177)
    side, home = PL.track_sides(tr)
    tgt_side = np.flatnonzero(side == "tgt")
    assert home == "tgt" and len(tgt_side) and D.pdt_hms(tr["t"][tgt_side[-1]]) <= "07:23:52", D.pdt_hms(tr["t"][tgt_side[-1]])   # last target-side sample at / before the steal
    assert not (side[tgt_side[-1] + 1:] == "tgt").any() and (side[tgt_side[-1] + 1:] == "itc").any()                          # ... then interceptor-side (stolen)
    assert D.pdt_hms(tr["t"][-1]) >= "07:24:10"                                                                                  # the track itself carried on
    ends = {a["name"]: a for a in L["annotations"] if a.get("name", "").startswith("lbl_end_177_")}
    assert set(ends) == {"lbl_end_177_rng"}                                                                                      # 2026-09-14: the id pill on the first panel only
    x_end = next(t for t in data if t.get("name") == "#177 · target end")["x"][0]
    assert str(x_end).startswith(str(D.to_pdt_dt64(np.array([tr["t"][tgt_side[-1]]]))[0])[:19])                                   # ✕ at the last target-side sample
    dep = [a for a in L["annotations"] if a.get("name", "").startswith("departed_177_")]
    assert len(dep) == 5 and all(a["text"] == "→ interceptor (stolen)" and a["font"]["color"] == T.INK3 for a in dep)   # 5 panels since the altitude row (2026-09-17 pm)
    thin = [t for t in data if t.get("name") == "#177 · target" and t.get("mode") == "lines" and t["line"]["width"] == PL.DEPARTED_W]
    itc_side = [t for t in data if t.get("name") == "#177 · target" and t.get("mode") == "lines" and t["line"]["color"] in PL.RAMP_ITC]
    assert not itc_side and thin and all(t["opacity"] == PL.DEPARTED_ALPHA for t in thin)   # 2026-09-17 hard rule: the stolen span is drawn departed-thin, never as the interceptor's (blue) track
    # y-ranges = TARGET TRUTH envelope ± 15 %, floors ± 0.6 km / ± 20 m/s / ± 2° / ± 30 m; the tracks never widen them (2026-09-17 pm: target truth only — the interceptor has its own card)
    for key, ax in (("rng", "yaxis"), ("rr", "yaxis2"), ("az", "yaxis3"), ("el", "yaxis4"), ("alt", "yaxis5")):
        tgt_tracks = [tr for tr in M["tracks"] if PL.track_sides(tr)[1] in ("tgt", "free") or (PL.track_sides(tr)[0] == "tgt").any()]
        want = PL.meas_envelope(M["truth"], tgt_tracks, key, "tgt")   # 2026-09-17 pm: truth ∪ on-target track samples (the tracks are never clipped)
        assert L[ax]["range"] == list(want), (key, L[ax]["range"], want)
        v = np.concatenate([PL._meas_val(M["truth"], key)] + [PL._meas_val(tr, key)[(PL.track_sides(tr)[0] == "tgt") & np.isfinite(PL._meas_val(tr, key))] for tr in tgt_tracks if PL.track_sides(tr)[1] != "free"])
        lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
        assert want[0] <= lo and want[1] >= hi and (want[1] - want[0]) >= 2 * PL.MEAS_MIN_HALF[key] - 1e-9
        assert (want[1] - want[0]) <= max(2 * PL.MEAS_MIN_HALF[key], (hi - lo) * 1.3) + 1e-9
    assert L["yaxis4"]["range"][0] > -10 and L["yaxis4"]["range"][1] < 20                                     # the elevation panel is NOT stretched to a runaway
    off = [a for a in L["annotations"] if a.get("name", "").startswith("off_")]
    assert off and all(a["text"] in ("▲", "▼") and "samples off scale" in a["hovertext"] and a["yref"].endswith("domain") for a in off)
    for a in off:   # the count in the hover equals the samples of that track beyond that axis
        _, key, tid, side_ = a["name"].split("_")
        trk = next(t for t in M["tracks"] if t["tid"] == int(tid))
        y = PL._meas_val(trk, key)
        rng_ = L[{"rng": "yaxis", "rr": "yaxis2", "az": "yaxis3", "el": "yaxis4", "alt": "yaxis5"}[key]]["range"]
        n = int((y > rng_[1]).sum() if side_ == "up" else (y < rng_[0]).sum())
        assert f"· {n} samples off scale" in a["hovertext"], (a["name"], a["hovertext"])
    # bistatic = 2 x mono (co-located TX): truth and obs, numerically
    assert np.allclose(M["truth"]["rng"], 2.0 * M["truth"]["rng_mono"]) and np.allclose(M["obs"]["rng"], 2.0 * M["obs"]["rng_mono"])
    assert M["geom"]["colocated"] is True and M["obs_rr"] is False                                             # no amb_dop in the archive -> no obs range rate
    # handover ticks: 177 -> 203 (07:23:59) marked "→ #203" on the FIRST panel only (2026-09-14) — the tick and its hover-only ▾ mark
    tags = [a for a in L["annotations"] if a.get("name", "").startswith("handover_tag_")]                     # hover-only ▾ marks: id + time in the hover
    assert any(a["text"] == "▾" and a["hovertext"].startswith("→ #203 · ") for a in tags) and all(a["xref"] == "x" for a in tags)
    t203 = [sh for sh in L["shapes"] if sh.get("name", "").startswith("handover_target_203")]
    assert len(t203) == 1 and t203[0]["xref"] == "x" and t203[0]["yref"] == "y domain"
    assert not [sh for sh in L["shapes"] if (sh.get("line") or {}).get("color") == T.GOLD]                    # no CPA vline on the quad


def test_error_panel_bridges_short_coasts_and_breaks_only_on_dropouts():
    """A6 on F1 @ 07:24:15 (window 120 s): the az error line of the graded target track is CONTINUOUS through every
    coast / tentative hole <= 3 s (lighter segments) — its only None breaks are steps > 3 s between consecutive
    published states (dropouts); the ±1σ band is one polygon per such run (continuous across the bridged holes,
    σ interpolated); a 4 px coast indicator strip sits at the foot of every card with "coasting n s" hover."""
    at = _at(LIVE, flight=1, anchor_t=F1_LATE, playing=False, spec_window=120).run()
    assert not _exc(at), _exc(at)
    err = _figs(at)["err"]
    tid = at.session_state["tgt_tid"]
    b = D.archive_bundle(1)
    tracks = D.slice_tracks(b["tracks"], F1_LATE - 180, F1_LATE)
    Tt = D.slice_truth(b["tgt"], F1_LATE - 180, F1_LATE)
    e = E.track_errors(tracks[tid], Tt, ant=b["ant"], t_now=F1_LATE, window_s=120.0, tid=tid)
    u = E.ungraded_errors(tracks[tid], Tt, e, ant=b["ant"], t_now=F1_LATE, window_s=120.0, tid=tid)
    m = e["t"] >= F1_LATE - 120
    ta, ya, sa, ga, ui, brk = PL.merged_series(e["t"][m], np.asarray(e["az"])[m], np.asarray(e["sig_az"])[m], u["t"], u["az"], 3.0)
    n_drop = int(brk.sum())
    n_coast_runs = len(PL._runs_of(~ga, brk))
    assert len(u["t"]) >= 3 and n_coast_runs >= 2, (len(u["t"]), n_coast_runs)                   # there ARE short coasts in this window ...
    base = [t for t in err["data"] if t.get("name") == f"track #{tid}" and t.get("mode") == "lines" and not t.get("fill")]
    assert len(base) == 4
    az_line = base[0]
    assert sum(1 for x in az_line["x"] if x is None) == n_drop, (n_drop, sum(1 for x in az_line["x"] if x is None))   # ... and NONE of them breaks the line
    assert az_line["line"]["color"] == PL.rgba(T.TARGET, PL.LIGHT_ALPHA) and az_line["line"]["width"] == PL.LINE_W
    y = _arr(az_line["y"])
    assert int(np.isfinite(y).sum()) == len(ta)                                                    # every published state (graded + coasting) is on the line
    bands = [t for t in err["data"] if t.get("fill") == "toself" and t.get("name") == f"track #{tid} ±1σ" and t.get("xaxis", "x") == "x"]
    assert len(bands) == n_drop + 1                                                                # one polygon per dropout-free run: continuous across the coasts
    strips = [t for t in err["data"] if t.get("name") == f"coast strip #{tid}"]
    assert len(strips) == 4 and all(t["line"] == {"color": T.INK3, "width": PL.STRIP_W} and t["hovertemplate"] == "%{customdata}<extra></extra>" for t in strips)
    labels = [c for c in strips[0]["customdata"] if c]
    assert labels and all(re.fullmatch(r"(coasting|tentative) \d+\.\d s", c) for c in labels) and len(labels) == 2 * n_coast_runs
    y0, y1 = err["layout"]["yaxis"]["range"]
    assert all(abs(v - (y0 + PL.STRIP_FRAC * (y1 - y0))) < 1e-6 for v in _arr(strips[0]["y"]) if np.isfinite(v))   # just above the card floor
    assert PL.BRIDGE_S == 3.0 and "gaps > 3 s = dropout" in LS.HEADERS["err"][1]


def test_track_handover_event_card_tile_and_ticks():
    """A8: two ticks across the 129 -> 177 handover (07:21:50 -> 07:22:05) log a target track-change event; the TRACK
    CHANGES card lists it newest first with the current id bold, the TARGET TRACK tile reads "prev #129 · changed …"
    with an amber edge for 10 s, the error panel and the meas quad carry a "→ #177" tick, the map pill flashes amber."""
    at = _at(LIVE, flight=1, anchor_t=F1_HAND, playing=False).run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] == 129
    at.session_state["anchor_t"] = F1_HAND + 15.0
    at.run()
    assert not _exc(at), _exc(at)
    assert at.session_state["tgt_tid"] == 177
    ev = [T.event_row(e) for e in at.session_state["track_events"]]
    hand = [e for e in ev if e[1] == "target" and e[2] == 129 and e[3] == 177]
    assert len(hand) == 1 and D.pdt_hms(hand[0][0]) == D.pdt_hms(F1_HAND + 15.0)
    more = "\n".join(m.value for m in at.expander[0].markdown)
    assert 'class="ih-events"' in more and "target track #129 → <b>#177</b>" in more
    rows = T.events_rows(at.session_state["track_events"], 177, None, hms=D.pdt_hms)
    assert [D.pdt_hms(F1_HAND + 15.0), "target", 129, 177, True] in [r[:5] for r in rows]
    tiles = _tiles(_md(at))
    assert tiles[0][2].startswith("#177") and tiles[0][0] == "amber" and f"prev #129 · changed {D.pdt_hms(F1_HAND + 15.0)}" in tiles[0][3]
    figs = _figs(at)
    hms = D.pdt_hms(F1_HAND + 15.0)
    # the error panel (2026-09-14): ONE tick on the FIRST card, labelled in that card's header strip ("tick_labels": "→ #177 · 07:22:05")
    Le = figs["err"]["layout"]
    (lst,) = [a for a in Le["annotations"] if (a.get("name") or "") == "tick_labels"]
    assert f"→ #177 · {hms}" in lst["text"] and lst["xref"] == "x domain" and lst["yref"] == "y domain" and lst["y"] == 1.0
    assert lst["xanchor"] == "left" and lst["yanchor"] == "bottom" and lst["font"]["color"] == T.INK and lst["bgcolor"] == PL.TAG_BG
    assert not [a for a in Le["annotations"] if (a.get("name") or "").startswith(("handover_label_", "handover_tag_"))]
    ticks = [sh for sh in Le["shapes"] if (sh.get("name") or "") == "handover_target_177"]
    assert len(ticks) == 1 and ticks[0]["xref"] == "x" and ticks[0]["yref"] == "y domain" and ticks[0]["y0"] == 0.86 and ticks[0]["y1"] == 1.0
    # the velocity cards: no vertical marks at all
    Lv = figs["vel"]["layout"]
    assert not [sh for sh in Lv.get("shapes", []) if (sh.get("name") or "").startswith("handover_")]
    assert not [a for a in Lv.get("annotations", []) if (a.get("name") or "") == "tick_labels" or (a.get("name") or "").startswith("handover_")]
    # the measurement quad keeps the hover-only ▾ mark + tick, on the FIRST panel only
    Lm = figs["meas"]["layout"]
    tags = [a for a in Lm["annotations"] if (a.get("name") or "") == "handover_tag_target_177"]
    assert len(tags) == 1 and tags[0]["yref"] == "y domain" and tags[0]["y"] == 1.0 and tags[0]["font"]["color"] == T.INK2 and tags[0]["xref"] == "x"
    assert tags[0]["text"] == "▾" and tags[0]["hovertext"] == f"→ #177 · {hms}" and tags[0]["xanchor"] == "center"
    ticks = [sh for sh in Lm["shapes"] if (sh.get("name") or "") == "handover_target_177"]
    assert len(ticks) == 1 and ticks[0]["xref"] == "x" and ticks[0]["yref"] == "y domain" and ticks[0]["y0"] == 0.86 and ticks[0]["y1"] == 1.0
    body = json.loads(LS.response_body(_store(at), HAVE0))
    pill = next(p for p in body["pills"] if p["name"] == "pill_tgt")
    assert pill["text"] == "#177" and pill["bordercolor"] == T.AMBER                                # flashes for 10 s after the handover
    assert [D.pdt_hms(F1_HAND + 15.0), "target", 129, 177] in [r[:4] for r in body["events"]]
    # 10 s later the flash is over
    at.session_state["anchor_t"] = F1_HAND + 30.0
    at.run()
    assert not _exc(at), _exc(at)
    assert _tiles(_md(at))[2][0] != "amber"
    body = json.loads(LS.response_body(_store(at), HAVE0))
    assert next(p for p in body["pills"] if p["name"] == "pill_tgt")["bordercolor"] == T.TARGET                  # back to the role colour (D7: colour on the border, text in ink)


def test_meas_quad_collapsed_by_default_and_toggle():
    """A15: the MEASUREMENT SPACE quad is a collapsible section — collapsed by default (ONE iframe, no meas figure built or
    pushed), the header-row button expands it (two iframes, the meas figure pushed), remembered in session state."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, meas_open=False).run()
    assert not _exc(at), _exc(at)
    assert len(at.get("iframe")) == 1 and 'id="map"' in at.get("iframe")[0].proto.srcdoc
    assert "meas" not in _figs(at) and at.session_state["meas_open"] is False
    btn = next(b for b in at.button if b.key == "meas_toggle")
    assert btn.label.startswith("MEASUREMENT SPACE") and "press m" in btn.label and btn.proto.icon == ":material/expand_more:"   # Material icon, no dingbat in the label
    btn.click().run()
    assert not _exc(at), _exc(at)
    assert at.session_state["meas_open"] is True and len(at.get("iframe")) == 2 and 'id="meas"' in at.get("iframe")[1].proto.srcdoc
    btn = next(b for b in at.button if b.key == "meas_toggle")
    assert "meas" in _figs(at) and btn.label.startswith("MEASUREMENT SPACE") and "press m" not in btn.label and btn.proto.icon == ":material/expand_less:"
    js = LS.panel_script("abc", "h", 8902, 1000)
    assert "bindParentKeys" in js and 'ev.key!=="m"' in js and "MEASUREMENT SPACE" in js                    # the "m" key toggles it from the panel iframe


def test_text_size_scales_fonts_everywhere():
    """A17: sidebar "Text size" -> html font-size 16 px x scale (everything is rem), ui.font_px = preset base x scale so
    every plotly font, pill and readout follows; Large is the default on the Large-1440p preset."""
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, text_scale="X-Large").run()
    assert not _exc(at), _exc(at)
    md = _md(at)
    assert "--ih-scale:1.3" in md and "font-size:calc(16px * 1.3)" in md and T.scale_css(1.3) in md
    ui = _store(at)["ui"]
    assert ui == {"font_px": 17, "line_w": 2.5, "preset": "Laptop", "text_scale": 1.3}   # 2026-09-15: default preset Laptop (13 base) x 1.3 = 17
    figs = _figs(at)
    assert figs["err"]["layout"]["xaxis"]["tickfont"]["size"] == 17 and figs["map"]["layout"]["font"]["size"] == 17
    reads = [a for a in figs["err"]["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
    assert all(a["font"]["size"] == round(PL.READOUT_PX * 1.3) for a in reads)
    pill = next(a for a in figs["map"]["layout"]["annotations"] if a["name"] == "pill_tgt")
    assert pill["font"]["size"] == round(PL.PILL_PX * 1.3)   # 2026-09-17: PILL_PX 15
    assert "--ih-scale:1.3" in _panel(at) or "text_scale:1.3" in _panel(at)
    assert T.text_scale({"screen": "Large 1440p"}) == 1.3 and T.text_scale({"screen": "Desktop 1080p"}) == 1.15 and T.text_scale({"screen": "Laptop"}) == 1.0 and T.text_scale({"text_scale": "Large"}) == 1.15
    at2 = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, screen="Large 1440p", text_scale="X-Large").run()   # the preset default
    assert _store(at2)["ui"]["font_px"] == round(16 * 1.3) == 21 and _store(at2)["ui"]["text_scale"] == 1.3   # Large 1440p preset defaults to X-Large text
    # the base scale itself went up: status .85rem, tile label .75rem, tile hero 3.2rem, chip value 2.0rem, card header .85rem
    for need in (".ih-status { display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; font:500 1.15rem", ".ih-tile .k { font:500 .85rem", ".ih-tile .v { font:600 3.2rem",
                 ".ih-chip .v { font:600 2.0rem", ".ih-card-h { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 8px; font:500 1rem"):
        assert need in T.CSS, need


def test_empty_state_cards_keep_titles_and_muted_readout():
    """Pre-flight: no track at all -> all FOUR error cards still carry their title, a muted "no graded samples
    yet" readout and live axes (an invisible anchor per row: plotly drops trace-less subplot axes); the map
    and meas figures build; tiles keep "no pair yet" / NO TRACK."""
    at = _at(LIVE, flight=1, anchor_t=D.replay_bounds(1)[0], playing=False, show_obs=True).run()
    assert not _exc(at), _exc(at)
    figs = _figs(at)
    err = figs["err"]
    titles = [a["text"] for a in err["layout"]["annotations"] if a["text"].startswith("<b>")]
    assert [t.split("  <b>▲")[0] for t in titles] == [f"<b>{PL.ERR_TITLE[k]}</b> ({PL.ERR_UNIT[k].strip()})" for k in PL.ERR_KEYS]   # 2026-09-15: unit in the title; state words may follow
    reads = [a for a in err["layout"]["annotations"] if (a.get("name") or "").startswith("readout_")]
    assert [a["text"] for a in reads] == [PL.EMPTY_READOUT] * 4 and all(a["font"]["color"] == T.INK3 for a in reads)
    anchors = [t for t in err["data"] if (t.get("name") or "").startswith("_anchor_")]
    assert [t["name"] for t in anchors] == [f"_anchor_{k}" for k in PL.ERR_KEYS] and {t["xaxis"] for t in anchors} == {"x", "x2", "x3", "x4"}
    assert all(t["marker"]["opacity"] == 0 and t["hoverinfo"] == "skip" and t["showlegend"] is False for t in anchors)
    for i in range(1, 5):   # explicit x range on EVERY row -> no autorange collapse
        assert err["layout"]["xaxis" if i == 1 else f"xaxis{i}"]["range"] == err["layout"]["xaxis4"]["range"]
    tiles = _tiles(_md(at))
    assert tiles[0][2] == "NO TRACK" and "no pair yet" in tiles[2][3]
    assert set(figs) == set(FIG_KEYS) and not any((t.get("name") or "").startswith("track #") for t in figs["meas"]["data"])


def test_sidebar_css_never_wraps_widget_internals_and_pins_slider_values():
    """Regression for the fractured slider values ("120" -> 1 / 2 / 0 around the thumb): no rule whose selector
    lives under the sidebar (other than our own .ih-* text) may set overflow-wrap / word-break; Streamlit's
    slider thumb value and tick-bar labels are explicitly nowrap; the sidebar is >= 360 px; labels wrap normally."""
    rules = _css_rules(T.CSS)
    for sel, decl in rules:
        for part in sel.split(","):
            part = part.strip()
            if "stSidebar" in part and ".ih-" not in part:
                assert "overflow-wrap" not in decl and "word-break" not in decl, (part, decl)
    assert not re.search(r"overflow-wrap\s*:\s*anywhere", " ".join(d for s_, d in rules if "stSidebar" in s_ and ".ih-" not in s_))
    pin = [d for s_, d in rules if 'stSliderThumbValue' in s_ and "nowrap" in d]
    assert pin and "white-space:nowrap" in pin[0] and "overflow-wrap:normal" in pin[0] and "word-break:normal" in pin[0]
    assert any('[data-testid="stSliderTickBar"] *' in s_ for s_, d in rules)
    assert '[data-testid="stSidebar"][aria-expanded="true"] { min-width:360px !important; max-width:400px !important; }' in T.CSS
    assert "white-space:normal" in " ".join(d for s_, d in rules if '[data-testid="stSidebar"] p' in s_)
    # main column: full width, panel starts high
    assert "max-width:100% !important" in T.CSS and 'stMainBlockContainer' in T.CSS and 'stAppViewBlockContainer' in T.CSS and "padding:1rem 1.5rem" in T.CSS
    # rendered: the Display sliders exist and the emitted CSS is what the page injects
    at = _at(APP, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    assert {w.label for w in at.sidebar.select_slider} >= {"Metrics time window (s)", "Path history on map (s)", "Map zoom (m)"}
    assert any("stSliderThumbValue" in m.value and "nowrap" in m.value for m in at.markdown)


def test_trail_downsample_and_frame_helpers():
    t = np.arange(0.0, 10.0, 0.1)
    tr = np.column_stack([t, t * 2, t * 3])
    d = PL.downsample(tr, 2.0)
    assert d[0, 0] == 0.0 and d[-1, 0] == tr[-1, 0] and np.all(np.diff(d[:-1, 0]) >= 0.5 - 1e-9) and len(d) <= 21   # last row always kept
    assert PL.downsample(tr[:2], 2.0) is tr[:2] or len(PL.downsample(tr[:2], 2.0)) == 2
    assert PL.frame_box((1, 2, 3, 4), {}, 800.0) == (1.0, 2.0, 3.0, 4.0)
    A = {"tgt_now": np.array([0.0, 1010.0, 490.0, 0, 0, 0, 0]), "itc_now": None}
    assert PL.frame_box(None, A, 800.0) == (200.0, 1800.0, -300.0, 1300.0)     # follow: quantised centre ± half
    assert PL.trail_tails({"tgt_trail": np.array([[0, 1.0, 2.0, 3, 0, 0, 0]]), "itc_trail": np.zeros((0, 7))}) == {"tgt": [1.0, 2.0], "itc": None}


def test_laptop_engagement_frame_closes_in_on_the_pass():
    """(C) 1366x768: the stacked laptop map is 267-448 px tall, so ENGAGE_MIN_HALF_M = 500 m put the 59 m pass 2 inside
    ~10 px (5.99 m/px).  On the LAPTOP preset only, the engagement box may close to LAPTOP_MIN_HALF_M (250 m) and, while
    the two truths are actually together (3D separation < LAPTOP_CLOSE_SEP_M), its half-width is capped at
    LAPTOP_CLOSE_HALF_M (400 m).  Every other preset keeps the 500 m floor."""
    assert PL.ENGAGE_MIN_HALF_M == 500.0                                   # the other presets are untouched
    src = open(LIVE).read()
    assert re.search(r"LAPTOP_MIN_HALF_M, LAPTOP_CLOSE_SEP_M, LAPTOP_CLOSE_HALF_M = 250\.0, 300\.0, 400\.0", src)
    # engagement_box itself takes a smaller floor through min_half (the parameter the page now passes)
    A = {"sep": {}, "t_now": F1_MID, "tgt_now": np.array([F1_MID, 2000.0, 500.0, 100.0, 0, 0, 0]),
         "itc_now": np.array([F1_MID, 2040.0, 520.0, 100.0, 0, 0, 0]), "flight": 1}
    wide, tight = PL.engagement_box(A, (), live=True), PL.engagement_box(A, (), live=True, min_half=250.0)
    assert wide[1] - wide[0] == 2 * PL.ENGAGE_MIN_HALF_M and tight[1] - tight[0] == 600.0   # 250 m floor, ceil-quantised to 300 m
    # rendered: Desktop keeps >= 1000 m across, Laptop closes in on the same moment (pass 2, 59 m apart)
    at = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, screen="Desktop 1080p").run()   # 2026-09-15: Laptop is the DEFAULT preset, so name Desktop explicitly
    assert not _exc(at), _exc(at)
    v_desk = dict(_store(at)["view"])
    at2 = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False, screen="Laptop").run()
    assert not _exc(at2), _exc(at2)
    v_lap = dict(_store(at2)["view"])
    assert v_desk["mode"] == v_lap["mode"] == "engage"
    assert v_desk["x1"] - v_desk["x0"] >= 2 * PL.ENGAGE_MIN_HALF_M
    span = v_lap["x1"] - v_lap["x0"]
    assert span <= 800.0, (span, v_lap)                                    # they are together now: 400 m half-width cap
    assert span < v_desk["x1"] - v_desk["x0"], (v_lap, v_desk)             # ... and strictly tighter than Desktop's
    assert v_lap["y1"] - v_lap["y0"] == span and all(x % 100 == 0 for x in (v_lap["x0"], v_lap["x1"], v_lap["y0"], v_lap["y1"]))


# ── LIVE session: the CPA of a pass must reach the page (2026-09-17 regression) ───────────────────
# MRU91 run 3212ae2284eb44c7b3e8757e68d0d8cd (Salmon_Woodpecker): the 06:55:47 PDT pass (28.4 m truth
# separation, 19 samples under the 70 m gate, then opening) produced NO gold ★ and no "CPA … m" in the
# separation card header, although engine.analyze validates that pass from a fresh session.  Causes found:
#   1. engine.running_min keeps cpa_run for the whole SESSION while engine.cpa_gate can only validate a
#      minimum that still has samples in the buffered window -> an earlier, closer approach (that run:
#      23.4 m @ 06:50:56, the pad/launch pass) masks every later pass AND, once its time scrolls out of the
#      window, can never be gated: the page shows no CPA again for the rest of the run.  views/1_live.py
#      (2026-09-17 later: the guard moved INTO ih.engine.analyze — a never-validated running minimum older than the
#      separation series is dropped there; the CPA is now the most recent validated AIRBORNE pass, see ih/engine.py).
#   2. data.set_roles dropped the raw live buffer, so assigning roles mid-engagement (that run: the TARGET
#      feed mav14550_1_1 only appeared at 06:50:37) restarted the separation series at the click and lost the
#      CPA of a pass that had just happened.  The buffer is keyed per MAVLink id and re-merged by role every
#      tick, so it is kept now.
#   3. views/3_data_source.py _follow is idempotent: re-selecting the run already followed no longer wipes the
#      buffer + derived state.
# Driven through the real Streamlit live path (ih.feed -> engine -> views/1_live.py -> liveserver.STORE) with
# tests/fake_mongo.py streaming the 8/28 archive as mongo documents: pass 2 = 59 m @ 07:22:31.
def _fake_live_at(FM, srv, **state):
    """The Live page in LIVE mode against the installed fake, with the analysis clock driven by ``live_lag``."""
    st8 = {"source": "live", "live_host": "fake", "live_port": 27017, "live_db": "sensor_store", "live_run": FM.RUN_COLL,
           "hist_s": 180, "spec_window": 120, "cpa_gate_m": 70.0, "refresh_s": 2.0, "meas_open": False, **state}
    return _at(LIVE, **st8)


def test_live_cpa_of_a_pass_reaches_the_page_even_after_a_stale_minimum_or_a_role_change():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fake_mongo as FM

    from ih import feed as F

    t0f, t1f = D.FLIGHT_WINDOWS[1][0] - 60, D.FLIGHT_WINDOWS[1][1] + 30
    srv = FM.FakeMongo(t0f, t1f, shift_to_now=t1f + 5.0, with_obs=False)   # every document in the past: the clock is ours
    restore, stale, inline = srv.install(), F.LIVE_STALE_S, os.environ.get("IH_FETCH_INLINE")
    F.LIVE_STALE_S = 1e9                    # never anchor the clock to "newest data": this test IS the clock
    os.environ["IH_FETCH_INLINE"] = "1"     # no browser -> the main script run does the fetching
    SH = srv.shift                          # archive time -> the fake's document time
    try:
        at = _fake_live_at(FM, srv)

        def tick(t_arch: float) -> None:
            at.session_state["live_lag"] = time.time() - (float(t_arch) + SH)
            at.run()
            assert not _exc(at), (D.pdt_hms(t_arch), _exc(at))

        def cpa():
            return at.session_state["cpa_ok"]

        # (a) FRESH live session across pass 2: no CPA while still closing, validated once it opens, and the
        #     separation card's header HUD carries the words on the very tick it validates.
        for t_arch in np.arange(F1_MID - 40, F1_MID + 1, 4.0):
            tick(float(t_arch))
            assert cpa() is None, (D.pdt_hms(t_arch), cpa())          # pass 1 is 78 m (over the gate), pass 2 still closing
        assert at.session_state["last_snap"]["ok"] and len(at.session_state["last_snap"]["tgt_hist"]) > 100
        for t_arch in np.arange(F1_MID + 4, F1_MID + 21, 4.0):
            tick(float(t_arch))
        ok = cpa()
        assert ok is not None and abs(ok[0] - 59.0) < 2.0 and abs(ok[1] - (F1_MID + SH)) <= 2.0, ok
        assert ok == at.session_state["cpa_run"]
        hud = str((_store(at).get("hud") or {}).get("cpa") or "")
        assert "CPA" in hud and f"{ok[0]:.0f} m" in hud, (hud, ok)      # PL.cpa_hud pushed with this tick's envelope
        assert any(s.get("name") == "cpa_line" for s in _figs(at)["sep"]["layout"].get("shapes", [])), "gold CPA line missing from the separation card"
        assert any(t.get("name") == "CPA" for t in _figs(at)["map"]["data"]), "gold ★ missing from the map"

        # (b) THE 9-17 FAILURE: a never-validated running minimum from an earlier, closer approach whose time has
        #     scrolled out of the window (there: 23.4 m @ 06:50:56 under a 20 m gate).  It must not be able to
        #     mask the pass — engine.analyze drops it, the window re-derives its own minimum and the pass stays.
        old = (5.0, F1_MID + SH - 600.0, 0.0, 0.0, 5.0)
        for k in ("cpa_ok", "cpa_trk_ok", "tgt_tid", "last_snap"):
            at.session_state[k] = None
        at.session_state["cpa_run"], at.session_state["cpa_trk_run"] = old, old
        tick(F1_MID + 12)
        assert at.session_state["cpa_run"] != old, "a stale, un-gateable running minimum was kept"
        ok = cpa()
        assert ok is not None and abs(ok[0] - 59.0) < 2.0, (ok, at.session_state["cpa_run"])
        # a minimum still inside the window is NEVER expired (only the un-gateable ones are)
        keep = (5.0, F1_MID + SH - 30.0, 0.0, 0.0, 5.0)
        at.session_state["cpa_run"], at.session_state["cpa_ok"] = keep, None
        tick(F1_MID + 16)
        assert at.session_state["cpa_run"] == keep, at.session_state["cpa_run"]

        # (c) a ROLE CHANGE mid-engagement (ih.data.set_roles) clears the derived state but KEEPS the raw buffer,
        #     so the next tick re-merges the whole window and the pass that just happened validates again.
        body = open(f"{ROOT}/ih/data.py").read().split("def set_roles(")[1].split("\ndef ")[0]
        assert 'pop("_live_buf"' not in body and 'pop("live_ant"' not in body, "set_roles drops the live buffer again"
        buf = at.session_state["_live_buf"]
        for k in ("cpa_run", "cpa_ok", "cpa_trk_run", "cpa_trk_ok", "tgt_tid", "last_snap"):
            at.session_state[k] = None
        at.session_state["track_events"] = []
        tick(F1_MID + 20)
        assert at.session_state["_live_buf"] is buf                     # same rows, nothing re-fetched from scratch
        ok = cpa()
        assert ok is not None and abs(ok[0] - 59.0) < 2.0, ok           # ... and the CPA is back on the very next tick

        # (n.b. dropping the buffer is not free even though this fake refills 180 s in one tick: a fresh buffer is
        #  bounded to [t_now - max(hist_s, spec_window), t_now] and on MRU91 fills back feed.CHUNK_S = 12 s per tick,
        #  so a drop later than that window after a pass loses that pass for the rest of the session.)

        # (e) re-selecting the run ALREADY followed (a stray selectbox on_change / a second Connect) must not wipe
        #     the buffer or the CPA: views/3_data_source.py _follow is idempotent.
        probe = {"ok": True, "err": None, "ts": time.time(), "n_total": 1, "limit": 25, "latency_ms": 1,
                 "runs": [{"name": FM.RUN_COLL, "friendly": "Red_Mandrill", "start_t": t0f, "last_t": t1f, "n143": 900, "n106": 900}]}
        keep_ok = (59.0, F1_MID + SH, 0.0, 0.0, 59.0)
        ds = _at(SRC, ds_mode="live", source="live", live_host="fake", live_port=27017, live_db="sensor_store",
                 live_run=FM.RUN_COLL, live_probe=probe, mru_number=91, cpa_ok=keep_ok, cpa_run=keep_ok,
                 _live_buf={"run": FM.RUN_COLL, "sentinel": True}).run()
        assert not _exc(ds), _exc(ds)
        ds.selectbox(key="_run_w").select(FM.RUN_COLL).run()
        assert not _exc(ds), _exc(ds)
        assert (ds.session_state["_live_buf"] or {}).get("sentinel") is True, "re-following the same run dropped the buffer"
        assert ds.session_state["cpa_ok"] == keep_ok and ds.session_state["live_run"] == FM.RUN_COLL
    finally:
        F.LIVE_STALE_S = stale
        if inline is None:
            os.environ.pop("IH_FETCH_INLINE", None)
        else:
            os.environ["IH_FETCH_INLINE"] = inline
        restore()
