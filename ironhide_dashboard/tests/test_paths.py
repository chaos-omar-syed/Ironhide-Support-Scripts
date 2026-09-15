"""Execution-path matrix for the ARCHIVE REPLAY through the real app entry (st.navigation + sidebar
widgets, AppTest): every flight, play / pause / restart, 1× / 2× / 4× (no clock jump), seek slider and
every "jump to pass" button, the three screen presets, the map frame modes + Reset map view, every Display
toggle (satellite, blind rings, raw obs, freeze), Metrics window 60 / 120 / 300, the More expander, the
Live -> Data source navigation, the ?flight=&t=&screen= deep link, and — for every row — no exception, no
"None" / "nan" leaking into tiles or status, and a status line that reflects the change.  The app's default
landing is the DATA SOURCE page, so the APP rows switch to the Live page first (_at).

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_paths.py
"""
from __future__ import annotations

import datetime
import os
import re
import sys
import time

import numpy as np
import pytest

os.environ.setdefault("IH_LIVE_PORT", "8912")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from streamlit.testing.v1 import AppTest  # noqa: E402

from ih import data as D  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402

APP = f"{ROOT}/app.py"
LIVE, SRC = (f"{ROOT}/views/{p}" for p in ("1_live.py", "3_data_source.py"))
F1_MID = D.hms_to_epoch("07:22:31")
FIG_KEYS = tuple(LS.FIG_KEYS)          # whatever the panel server declares (owner A may add / fold figures)
LEAK = re.compile(r">(nan|None|inf|-inf)<|\b(nan|None) (m|s|°|%)\b|\bnan\b|>None<")
MATRIX: list[tuple[str, str]] = []


def _at(page: str, live_page: bool = True, **state) -> AppTest:
    at = AppTest.from_file(page, default_timeout=240)
    for k, v in D.STATE_DEFAULTS.items():
        at.session_state[k] = v
    if "text_scale" not in at.session_state:
        at.session_state["text_scale"] = "Normal"   # tests render at Normal text unless they set it
    at.session_state["show_sat"] = False
    for k, v in state.items():
        at.session_state[k] = v
    if page == APP and live_page:
        # The app lands on the Data source page.  AppTest.switch_page runs the target page file STANDALONE (the entry script — and
        # with it the sidebar — does not run on a switched page; verified: app.py's _qp_done is never set there).  So these rows run
        # the landing page once (entry script: deep link, state), then switch to the Live page for its content.  Sidebar controls are
        # exercised on a landing-page instance (live_page=False) and the Live page checked in a second instance (_live_of).
        at.run()
        assert not _exc(at), _exc(at)
        at.switch_page("views/1_live.py")
    return at


STATE_KEYS = tuple(D.STATE_DEFAULTS) + ("_speed_eff", "text_scale")


def _state(at: AppTest) -> dict:
    out = {}
    for k in STATE_KEYS:
        try:
            out[k] = at.session_state[k]
        except KeyError:
            pass
    return out


def _live_of(at: AppTest) -> AppTest:
    """A Live-page AppTest carrying a landing-page instance's session state (sidebar controls live in the entry script, which
    AppTest never runs together with a switched page — see _at)."""
    lv = _at(LIVE, **_state(at)).run()
    assert not _exc(lv), _exc(lv)
    return lv


def _md(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def _exc(at) -> list[str]:
    return [f"{e.type}: {e.message}" for e in at.exception]


def _status(md: str) -> str:
    m = re.search(r'<div class="ih-status">(.*?)</div>', md)
    return re.sub(r"<[^>]+>", " ", m.group(1)) if m else ""


def _tiles(md: str):
    return re.findall(r'<div class="ih-tile ([a-z]*)"><div class="k">(.*?)</div><div class="v">(.*?)</div>(?:<div class="s">(.*?)</div>)?', md)


def _clean(at, label: str) -> str:
    """No exception, no None/nan leak, three tiles, four figures, status present. Returns the markdown."""
    assert not _exc(at), (label, _exc(at))
    md = _md(at)
    assert not LEAK.search(md), (label, LEAK.search(md).group(0))
    assert len(_tiles(md)) == 3, label
    figs = set(LS.figs_of(at.session_state["_sid"]))
    assert {"map", "sep", "err"} <= figs <= set(FIG_KEYS), (label, figs)              # the measurement quad is collapsed by default (may be absent)
    assert _status(md), label
    return md


def _radio(at, label: str):
    return next(r for r in at.sidebar.radio if r.label == label)


def _btn(at, label: str):
    return next(b for b in at.sidebar.button if b.label == label)


def _sel(at, label: str):
    return next(w for w in at.sidebar.select_slider if w.label == label)


def _tog(at, label: str):
    return next(w for w in at.sidebar.toggle if w.label == label)


# ── every flight, at its first verified pass ─────────────────────────────────
@pytest.mark.parametrize("flight", [1, 2, 3, 4])
def test_each_flight_renders_at_a_pass(flight):
    ps = [p for p in D.passes(flight) if p["verified"]]
    t = ps[0]["t"] + 6.0 if ps else D.FLIGHT_WINDOWS[flight][0] + 60.0
    at = _at(APP, flight=flight, anchor_t=t, playing=False).run()
    md = _clean(at, f"F{flight}")
    st = _status(md)
    # (2026-09-15: the flight is in the sidebar only — the status row must fit 938 px at X-Large)
    assert D.pdt_hms(t) in st and "PAUSED" in st
    tiles = _tiles(md)
    assert re.fullmatch(r"\d+<small>m</small>", tiles[2][2]), tiles[2]   # closest-so-far is the third tile now                          # closest so far is a number
    assert at.session_state["flight"] == flight
    MATRIX.append((f"replay · flight {flight} @ {D.pdt_hms(t)}", f"tgt track {at.session_state['tgt_tid']}, closest {re.sub('<.*?>', ' ', tiles[2][2])}"))


# ── jump-to-pass (sidebar, archive mode): verified passes only, seeks PASS_LEAD_S before the pass and plays ──
def test_jump_to_pass_buttons_seek_lead_before_pass_and_play():
    at = _at(APP, live_page=False, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    labels = sorted(b.label for b in at.sidebar.button if b.label.startswith("Pass "))            # AppTest lists column 1 then column 2
    assert labels == ["Pass 1 · 07:21:43 · 78 m", "Pass 2 · 07:22:31 · 59 m", "Pass 3 · 07:23:47 · 53 m"], labels
    for b in at.sidebar.button:
        if b.label.startswith("Pass "):
            assert b.proto.help and "before this pass" in b.proto.help
    at.session_state["track_events"] = [{"t": 1.0, "role": "target", "old": 1, "new": 2, "rule": "x"}]
    _btn(at, "Pass 2 · 07:22:31 · 59 m").click().run()
    assert not _exc(at), _exc(at)
    p2 = next(p for p in D.passes(1) if p["n"] == 2)
    assert at.session_state["playing"] is True and abs(at.session_state["anchor_t"] - (p2["t"] - D.PASS_LEAD_S)) < 0.01
    assert at.session_state["track_events"] == [] and at.session_state["cpa_run"] is None      # a seek is a new context
    assert D.pdt_hms(p2["t"] - D.PASS_LEAD_S) in _status(_clean(_live_of(at), "pass2")) or "07:22:0" in _status(_clean(_live_of(at), "pass2b"))
    # flight 3: passes 1-2 are NOT verified (interceptor still grounded) -> no buttons for them
    at3 = _at(APP, live_page=False, flight=3, anchor_t=D.replay_bounds(3)[0], playing=False).run()
    assert not _exc(at3), _exc(at3)
    n3 = [b.label for b in at3.sidebar.button if b.label.startswith("Pass ")]
    assert len(n3) == sum(1 for p in D.passes(3) if p["verified"]) == 5 and not any(l.startswith(("Pass 1 ", "Pass 2 ")) for l in n3), n3
    MATRIX.append(("sidebar · jump to pass", f"F1 {len(labels)} buttons, F3 {len(n3)} verified of {len(D.passes(3))}"))


# ── transport: play / pause / restart / speed (no clock jump) ────────────────
def test_play_pause_restart_and_speed_change_never_jumps_the_clock():
    at = _at(APP, live_page=False, flight=1, anchor_t=F1_MID, playing=False).run()          # landing page: the sidebar replay transport
    assert not _exc(at), _exc(at)
    assert "PAUSED" in _status(_clean(_live_of(at), "paused"))
    _btn(at, "Play replay").click().run()
    assert at.session_state["playing"] is True and abs(at.session_state["anchor_t"] - F1_MID) < 2.0 and at.session_state["_speed_eff"] == 1.0
    md = _clean(_live_of(at), "playing")
    assert "PAUSED" not in _status(md) and "1×" not in _status(md)   # 1× hidden 2026-09-15
    # speed 1x -> 4x after 100 s of (simulated) play: the clock stays where it was (+100 s), not +400 s
    at.session_state["anchor_wall"] = time.time() - 100.0
    _radio(at, "Replay speed (× real time)").set_value(4.0).run()
    assert at.session_state["speed"] == 4.0 and at.session_state["_speed_eff"] == 4.0
    assert abs(at.session_state["anchor_t"] - (F1_MID + 100.0)) < 3.0, at.session_state["anchor_t"] - F1_MID  # re-anchored, no ×4 rescale
    assert abs(time.time() - at.session_state["anchor_wall"]) < 3.0
    assert "4×" in _status(_clean(_live_of(at), "4x"))
    _btn(at, "Pause replay").click().run()
    assert at.session_state["playing"] is False
    assert "PAUSED" in _status(_clean(_live_of(at), "pause"))
    _btn(at, "Restart replay").click().run()
    assert at.session_state["playing"] is True and abs(at.session_state["anchor_t"] - D.replay_bounds(1)[0]) < 1.0
    lv = _live_of(at)
    md = _clean(lv, "restart")
    assert lv.session_state["cpa_run"] is None or lv.session_state["cpa_run"][1] <= D.replay_bounds(1)[0] + 40    # 4x: seconds of runs = tens of replay s
    assert D.pdt_hms(D.replay_bounds(1)[0]) in _status(md) or "07:19" in _status(md)
    for sp in (2.0, 1.0):
        _radio(at, "Replay speed (× real time)").set_value(sp).run()
        assert at.session_state["speed"] == sp and at.session_state["_speed_eff"] == sp
        assert (f"{sp:g}×" in _status(_clean(_live_of(at), f"{sp}x"))) == (sp != 1)   # 1× is not shown (2026-09-15)
    MATRIX.append(("replay · play / 4× (no jump) / pause / restart / 2× / 1× (sidebar on the landing page, Live page instance checked)", "ok"))


# ── seek slider + jump-to-pass buttons (Data source page) ───────────────────
def test_flight_select_and_load_flight_button_then_live_renders():
    """Data source (ARCHIVE REPLAY): the flight selectbox + ONE primary "Load flight and open Live view" button — the replay transport
    (play / restart / speed) lives in the sidebar and the seek / jump controls left this page (2026-09-10: controls with no data in view).
    Selecting a flight seeks to 20 s before its window, paused, derived state reset; the button re-seeks and switches to the Live page
    (swallowed on a standalone page); the Live page then renders where the clock was left."""
    at = _at(SRC, flight=1, anchor_t=D.replay_bounds(1)[0], playing=False, ds_mode="archive").run()
    assert not _exc(at), _exc(at)
    keys = {w.key for w in list(at.slider) + list(at.radio) + list(at.button)}
    assert "_seek_w" not in keys and "ds_play" not in keys and "ds_restart" not in keys and not any(str(k).startswith("jump_") for k in keys)
    for fl in (2, 3, 4, 1):                                    # a CHANGE each time (re-selecting the current flight fires no callback)
        at.selectbox(key="_flight_w").select(fl).run()
        assert not _exc(at), (fl, _exc(at))
        assert at.session_state["flight"] == fl and abs(at.session_state["anchor_t"] - D.replay_bounds(fl)[0]) < 1.0 and at.session_state["playing"] is False
        assert at.session_state["cpa_run"] is None and at.session_state["tgt_tid"] is None                       # derived state reset on seek
        assert D.flight_label(fl) in "\n".join(m.value for m in at.markdown)                                     # archive contents follow the selection
        MATRIX.append((f"data source · flight {fl} select", "ok"))
    at.session_state["anchor_t"] = F1_MID
    at.button(key="ds_open_live").click().run()
    assert not _exc(at), _exc(at)
    assert abs(at.session_state["anchor_t"] - D.replay_bounds(1)[0]) < 1.0 and at.session_state["playing"] is False   # the button re-seeks to the flight start
    live = _at(LIVE, **{k: at.session_state[k] for k in ("flight", "anchor_t", "anchor_wall", "playing", "speed")}).run()
    _clean(live, "after load flight")
    MATRIX.append(("data source · Load flight and open Live view", "ok"))


# ── Display: screen presets, map frame, reset view, toggles, window ──────────
def test_display_controls_matrix():
    at = _at(APP, live_page=False, flight=1, anchor_t=F1_MID, playing=False).run()          # landing page: the sidebar Display section
    assert not _exc(at), _exc(at)
    _clean(_live_of(at), "base")
    for preset in ("Laptop", "Large 1440p", "Desktop 1080p"):
        _radio(at, "Screen size").set_value(preset).run()
        lv = _live_of(at)
        _clean(lv, preset)
        sid, L = lv.session_state["_sid"], LS.layout(preset)
        assert LS.STORE[sid]["ui"]["preset"] == preset and LS.figs_of(sid)["err"]["layout"]["height"] == L["err"]
        assert f"PANEL={L['panel']}" in lv.get("iframe")[0].proto.srcdoc
        MATRIX.append((f"display · screen preset {preset}", f"panel {L['panel']} px, font {L['font_px']}"))
    rev0 = at.session_state["view_rev"]
    assert at.session_state["frame_mode"] == "engagement" and _radio(at, "Map frame").value == "engagement"      # default frame = engagement box
    assert _sel(at, "Map zoom (m)").disabled is True
    _radio(at, "Map frame").set_value("follow").run()
    assert at.session_state["frame_mode"] == "follow" and at.session_state["view_rev"] == rev0 + 1 and _sel(at, "Map zoom (m)").disabled is False
    lv = _live_of(at)
    _clean(lv, "Follow")
    v = LS.STORE[lv.session_state["_sid"]]["view"]
    assert v["mode"] == "follow" and v["rev"] == rev0 + 1 and v["x1"] - v["x0"] == 2 * at.session_state["map_half"]
    _sel(at, "Map zoom (m)").set_value(400).run()
    assert at.session_state["map_half"] == 400 and at.session_state["view_rev"] == rev0 + 2
    lv = _live_of(at)
    _clean(lv, "zoom 400")
    v = LS.STORE[lv.session_state["_sid"]]["view"]
    assert v["x1"] - v["x0"] == 800 and v["rev"] == rev0 + 2
    _btn(at, "Reset map view").click().run()
    assert at.session_state["view_rev"] == rev0 + 3
    lv = _live_of(at)
    _clean(lv, "reset view")
    assert LS.STORE[lv.session_state["_sid"]]["view"]["rev"] == rev0 + 3
    _radio(at, "Map frame").set_value("fit_flight").run()
    assert at.session_state["frame_mode"] == "fit_flight" and _sel(at, "Map zoom (m)").disabled is True
    lv = _live_of(at)
    _clean(lv, "Fit whole flight")
    v = LS.STORE[lv.session_state["_sid"]]["view"]
    assert v["mode"] == "fixed" and (v["x0"], v["x1"], v["y0"], v["y1"]) == D.flight_frame(1) and v["rev"] == rev0 + 4
    MATRIX.append(("display · engagement -> follow -> zoom 400 -> Reset -> fit_flight", f"view revs {rev0}..{rev0 + 4}, ranges as expected"))
    # toggles
    _tog(at, "Satellite imagery").set_value(True).run()
    lv = _live_of(at)
    _clean(lv, "sat on")
    assert LS.STORE[lv.session_state["_sid"]]["sat_on"] is True and callable(LS.STORE[lv.session_state["_sid"]]["sat_fn"])
    _tog(at, "Satellite imagery").set_value(False).run()
    lv = _live_of(at); _clean(lv, "sat off"); assert LS.STORE[lv.session_state["_sid"]]["sat_on"] is False
    _tog(at, "Radar blind-range rings (pulse width)").set_value(True).run()
    lv = _live_of(at)
    _clean(lv, "rings")
    names = {t.get("name") for t in LS.figs_of(lv.session_state["_sid"])["map"]["data"]}
    assert ("blind zone (c·pw/2)" in names) == bool(PL.blind_rings())
    _tog(at, "Show raw radar detections on error plots").set_value(False).run()
    lv = _live_of(at)
    _clean(lv, "obs off")
    assert not any(t.get("name") == "raw obs vs target truth" for t in LS.figs_of(lv.session_state["_sid"])["err"]["data"])
    _tog(at, "Show raw radar detections on error plots").set_value(True).run()
    lv = _live_of(at)
    _clean(lv, "obs on")
    assert any(t.get("name") == "raw obs vs target truth" for t in LS.figs_of(lv.session_state["_sid"])["err"]["data"])
    _tog(at, "Freeze display (pause updates)").set_value(True).run()
    lv = _live_of(at)
    md = _clean(lv, "freeze")
    assert "FROZEN" in _status(md) and LS.STORE[lv.session_state["_sid"]]["frozen"] is True
    _tog(at, "Freeze display (pause updates)").set_value(False).run()
    lv = _live_of(at)
    md = _clean(lv, "unfreeze")
    assert "FROZEN" not in _status(md) and LS.STORE[lv.session_state["_sid"]]["frozen"] is False
    MATRIX.append(("display · satellite / blind rings / raw obs / freeze toggles", "ok"))
    for W in (60, 300, 120):
        _sel(at, "Metrics time window (s)").set_value(W).run()
        lv = _live_of(at)
        _clean(lv, f"W={W}")
        lo, hi = LS.figs_of(lv.session_state["_sid"])["err"]["layout"]["xaxis4"]["range"]
        assert abs((np.datetime64(hi) - np.datetime64(lo)) / np.timedelta64(1, "s") - W * (1 + PL.X_PAD_FRAC)) < 0.01   # window + 2 % right padding
    for tr in (10, 30, 20):
        _sel(at, "Path history on map (s)").set_value(tr).run()
        _clean(_live_of(at), f"trail={tr}")
    MATRIX.append(("display · metrics window 60/300/120 · path history 10/30/20", "ok"))
    # closest-approach gate: 100 m makes pass 1 (78 m) a CPA at 07:22:00; 70 m does not
    at2 = _at(APP, live_page=False, flight=1, anchor_t=D.hms_to_epoch("07:22:00"), playing=False).run()
    assert _live_of(at2).session_state["cpa_ok"] is None
    next(w for w in at2.sidebar.slider if w.label == "Closest-approach gate (m)").set_value(100.0).run()
    assert at2.session_state["cpa_gate_m"] == 100.0
    lv = _live_of(at2)
    md = _clean(lv, "gate 100")
    assert lv.session_state["cpa_ok"] is not None and "CPA 78 m" in md and 'class="ih-tile gold"' in md
    MATRIX.append(("display · closest-approach gate 70 -> 100 m", "pass 1 becomes a CPA"))


# ── More expander, navigation Live -> Data source, sidebar per mode, deep link ──────────────
def test_more_expander_opens_with_content_and_navigation_to_data_source():
    lv = _at(LIVE, flight=1, anchor_t=F1_MID, playing=False).run()
    _clean(lv, "live")
    ex = [e for e in lv.expander if e.label.startswith("More details")]
    assert len(ex) == 1
    more = "\n".join(m.value for m in ex[0].markdown)
    for need in ("Coverage · 60 s", "Allegiance", "Predicted miss", "Radar tracks", "MAVLink feeds"):      # A10: ONE chip row of 5
        assert need in more, need
    assert not LEAK.search(more), LEAK.search(more).group(0)
    MATRIX.append(("live · More expander", "5 chips, no leaks"))
    # sidebar (archive mode, landing page): transport present, no live block, Text size + Map frame radios with the contract values
    at = _at(APP, live_page=False, flight=1, anchor_t=F1_MID, playing=False).run()
    assert not _exc(at), _exc(at)
    sb = at.sidebar
    assert {b.label for b in sb.button if not b.label.startswith("Pass ")} == {"Play replay", "Restart replay", "Reset map view"} and not any(b.label == "Disconnect" for b in sb.button)
    assert sum(b.label.startswith("Pass ") for b in sb.button) == 3                                       # F1: three verified passes -> jump buttons
    radios = {r.label: (list(r.options), r.value) for r in sb.radio}
    assert radios["Map frame"][1] == "engagement" and radios["Text size"] == (["Normal", "Large", "X-Large"], "Normal")
    assert "Refresh interval (s)" not in {w.label for w in sb.select_slider}                                          # live only
    assert "Replay speed (× real time)" in radios
    side = " ".join(re.sub(r"<[^>]+>", " ", m.value) for m in sb.markdown)
    assert "ARCHIVE" in side and "8/28 · Flight 1" in side                                                            # T.source_line_html
    # the landing page itself: LIVE preselected, then ARCHIVE REPLAY
    md = _md(at)
    assert "DATA SOURCE" in md and "NOT CONNECTED" in md and at.radio(key="_ds_mode_w").value == "live"
    at.radio(key="_ds_mode_w").set_value("archive").run()
    assert not _exc(at), _exc(at)
    assert "Archive replay" in _md(at) and at.selectbox(key="_flight_w").value == 1 and at.session_state["mode"] == "archive"
    MATRIX.append(("navigation · Live -> Data source (LIVE preselected) -> ARCHIVE REPLAY", "ok"))


def test_sidebar_live_block_when_following_a_run():
    """Live mode sidebar: unit / run / last-data / truth-roles block + Disconnect + Refresh interval; no replay transport."""
    at = _at(APP, live_page=False, source="live", live_host="10.255.255.1", live_port=27017, live_run="run_deadbeef", live_run_name="Dead_Beef", mru_number=91).run()
    assert not _exc(at), _exc(at)
    sb = at.sidebar
    assert any(b.label == "Disconnect" for b in sb.button) and not any(b.label in ("Play replay", "Pause replay", "Restart replay") for b in sb.button)
    assert "Refresh interval (s)" in {w.label for w in sb.select_slider} and "Replay speed (× real time)" not in {r.label for r in sb.radio}
    side = " ".join(re.sub(r"<[^>]+>", " ", m.value) for m in sb.markdown)
    assert "LIVE" in side and "MRU 91" in side and "Dead_Beef" in side and "10.255.255.1" in side and "TGT" in side
    assert at.session_state["mode"] == "live"
    next(b for b in sb.button if b.label == "Disconnect").click().run()
    assert not _exc(at), _exc(at)
    assert at.session_state["source"] == "archive" and at.session_state["mode"] == "archive" and at.session_state["live_run"] == ""
    assert any(b.label == "Play replay" for b in at.sidebar.button)
    MATRIX.append(("sidebar · live block + Disconnect -> archive", "ok"))


def test_deep_link_query_params_seek_flight_and_preset():
    at = _at(APP, live_page=False)                                    # the deep link is applied by the ENTRY script on the first run (any page)
    at.query_params["flight"] = "3"
    at.query_params["t"] = "08:20:23"
    at.query_params["screen"] = "Laptop"
    at.run()
    assert not _exc(at), _exc(at)
    assert at.session_state["flight"] == 3 and abs(at.session_state["anchor_t"] - D.hms_to_epoch("08:20:23")) < 1.0
    assert at.session_state["playing"] is False and at.session_state["screen"] == "Laptop" and at.session_state["_qp_done"] is True
    at.switch_page("views/1_live.py").run()
    _clean(at, "deep link")
    assert "08:20:23" in _status(_md(at))
    at.run()                                                                                   # applied once: a later rerun does not re-seek
    assert abs(at.session_state["anchor_t"] - D.hms_to_epoch("08:20:23")) < 1.0
    bad = _at(APP, live_page=False); bad.query_params["t"] = "garbage"; bad.query_params["flight"] = "9"; bad.run()
    assert not _exc(bad), _exc(bad)
    bad.switch_page("views/1_live.py").run()
    _clean(bad, "bad deep link")
    assert bad.session_state["flight"] == 1
    # ?ds=archive opens the Data-source page in ARCHIVE REPLAY mode (engine on the replay); ?ds=live keeps LIVE preselected
    ar = _at(APP, live_page=False); ar.query_params["ds"] = "archive"; ar.run()
    assert not _exc(ar), _exc(ar)
    assert ar.session_state["ds_mode"] == "archive" and ar.session_state["mode"] == "archive" and ar.radio(key="_ds_mode_w").value == "archive" and "Archive replay" in _md(ar)
    lv2 = _at(APP, live_page=False); lv2.query_params["ds"] = "live"; lv2.run()
    assert not _exc(lv2) and lv2.session_state["ds_mode"] == "live" and lv2.radio(key="_ds_mode_w").value == "live" and "NOT CONNECTED" in _md(lv2)
    MATRIX.append(("deep link ?flight=3&t=08:20:23&screen=Laptop (+ garbage) · ?ds=archive|live", "ok"))


def test_zz_write_matrix():
    out = os.environ.get("IH_MATRIX_OUT")
    if out:
        with open(out, "a") as f:
            for k, v in MATRIX:
                f.write(f"{k}\t{v}\n")
