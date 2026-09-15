"""ih.liveserver unit tests: the panel data server serves figs.json / plotly.min.js /
externalised images with CORS, honours the envelope `since` AND the per-figure stamps
(map / sep / err / meas), carries the vehicle heads + their layout-image dicts (data
coordinates) + tails + view + ui + more, hands satellite tiles over per BROWSER VIEW (view=
query, worker-fetched, keyed, only when different from the client's satk), strips the map's
axis ranges (the browser owns the view), gzips, 404s unknown sids; the screen presets ->
layout() numbers; the panel HTML is a pure function of (sid, host, port, cadence, preset),
carries the responsive / view-lock / tween JS and draws the heads / pills in a DOM overlay (l2p pixels), never with Plotly.

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_liveserver.py
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import sys
import time
import urllib.request

os.environ.setdefault("IH_LIVE_PORT", "8912")  # never the production 8902 (the running dashboard owns it)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402

PORT = int(os.environ["IH_LIVE_PORT"])
HEADS = {"tgt": {"E": 2099.3, "N": 187.3, "U": 75.2, "spd": 21.8, "hdg": 53.1}, "itc": {"E": 2102.9, "N": 155.9, "U": 128.3, "spd": 30.1, "hdg": 5.4}}
HEAD_IMGS = [{"source": "data:image/svg+xml;utf8,%3Csvg%3E", "xref": "x", "yref": "y", "x": 2099.3, "y": 187.3, "sizex": 96.0, "sizey": 96.0,
              "xanchor": "center", "yanchor": "middle", "sizing": "contain", "layer": "above", "visible": True, "name": "tgt_icon"},
             {"source": "data:image/svg+xml;utf8,%3Csvg%3E", "xref": "x", "yref": "y", "x": 2102.9, "y": 155.9, "sizex": 96.0, "sizey": 96.0,
              "xanchor": "center", "yanchor": "middle", "sizing": "contain", "layer": "above", "visible": True, "name": "itc_icon"}]


def _get(path: str, gzip_ok: bool = False):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"Accept-Encoding": "gzip" if gzip_ok else "identity"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        body, code, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        body, code, hdrs = e.read(), e.code, dict(e.headers)
    if hdrs.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return code, body, hdrs


BIG = base64.b64encode(np.random.default_rng(0).bytes(40_000)).decode()
VIEW = {"x0": 900.0, "x1": 4100.0, "y0": -1100.0, "y1": 2100.0, "rev": 0, "mode": "fixed"}
UI = {"font_px": 11, "line_w": 2.5, "preset": "Desktop 1080p"}
TAILS = {"tgt": [2099.3, 187.3], "itc": None}
MORE = [["Coverage · 60 s", "94 %", "fresh ≤1.5 s / 150 m", "ok"]]
HAVE0 = {"map": "", "sep": "", "err": "", "meas": ""}


def _fake_figs(sat: bool = False) -> dict:
    fmap = go.Figure(go.Scattergl(x=[0, 1], y=[0, 1]))
    if sat:   # the st.plotly_chart fallback still puts the tile INSIDE the figure
        fmap.add_layout_image(dict(source="data:image/jpeg;base64," + BIG, xref="x", yref="y", x=0, y=1, sizex=1, sizey=1))
        fmap.add_layout_image(dict(source="data:image/svg+xml;utf8,%3Csvg%3E", xref="x", yref="y", x=0, y=0, sizex=.1, sizey=.1))
    fmap.update_layout(uirevision="live-map", xaxis=dict(range=[900, 4100], constrain="range"), yaxis=dict(range=[-1100, 2100], scaleanchor="x", constrain="range"))
    fsep = go.Figure(go.Scatter(x=[0, 1], y=[1, 0])).update_layout(uirevision="live-sep")
    ferr = go.Figure(go.Scatter(x=[0, None, 2], y=[1, None, 3])).update_layout(uirevision="live-err")
    fmeas = go.Figure(go.Scatter(x=[0, 1], y=[0, 1])).update_layout(uirevision="live-meas")
    return {"map": fmap, "sep": fsep, "err": ferr, "meas": fmeas}


def _sat_fn(x0, x1, y0, y1):
    """A fake tile fetcher: a 40 kB 'jpeg' for any box (externalised by the server)."""
    return {"img": "data:image/jpeg;base64," + BIG, "x0": x0, "x1": x1, "y0": y0, "y1": y1}


def test_start_is_singleton_and_binds_test_port():
    srv = LS.start(PORT)
    assert srv is not None and LS.is_up() and LS.port() == PORT
    assert LS.start(PORT) is srv
    code, body, _ = _get("/health")
    assert code == 200 and json.loads(body)["ok"] is True and json.loads(body)["port"] == PORT


def test_figs_json_cors_no_store_since_stamps_heads_view_and_stripped_map_range():
    LS.start(PORT)
    stamp = LS.push("test", _fake_figs(), t_now=1787926951.0, clock="07:22:31", frozen=False, heads=HEADS, head_imgs=HEAD_IMGS, tails=TAILS, view=VIEW, ui=UI, more=MORE)
    code, body, h = _get("/figs.json?sid=test")
    assert code == 200
    assert h["Access-Control-Allow-Origin"] == "*" and h["Cache-Control"] == "no-store" and h["Content-Type"].startswith("application/json")
    j = json.loads(body)
    assert j["stamp"] == stamp and j["clock"] == "07:22:31" and j["frozen"] is False and set(j["figs"]) == {"map", "sep", "err", "meas"}
    assert j["map_stamp"] == j["sep_stamp"] == j["err_stamp"] == j["meas_stamp"] == stamp      # first push: every figure stamped
    assert j["heads"] == HEADS and j["head_imgs"] == HEAD_IMGS and j["tails"] == TAILS and j["view"] == VIEW and j["ui"] == UI and j["more"] == MORE
    assert j["pills"] == [] and j["events"] == []                                        # track-number pills / track-change rows ride the envelope too
    assert "sat" not in j and "sat_keep" not in j                                              # no tile fetcher registered
    for k, uir in (("map", "live-map"), ("sep", "live-sep"), ("err", "live-err"), ("meas", "live-meas")):
        assert j["figs"][k]["layout"]["uirevision"] == uir
    assert j["figs"]["err"]["data"][0]["x"] == [0, None, 2]                                    # gaps survive as null
    # THE view fix: the map figure carries NO axis ranges (the browser owns the view; the frame travels as "view")
    mx, my = j["figs"]["map"]["layout"]["xaxis"], j["figs"]["map"]["layout"]["yaxis"]
    assert "range" not in mx and "range" not in my and "autorange" not in mx and mx["constrain"] == "range" and my["scaleanchor"] == "x"
    # since == stamp -> tiny "unchanged" answer, no figs
    code, body, _ = _get(f"/figs.json?sid=test&since={stamp}")
    j2 = json.loads(body)
    assert code == 200 and j2["unchanged"] is True and "figs" not in j2 and j2["stamp"] == stamp
    # a client that already holds every figure at this stamp gets an EMPTY figs dict (heads only)
    j3 = json.loads(_get(f"/figs.json?sid=test&since=0&map={stamp}&sep={stamp}&err={stamp}&meas={stamp}")[1])
    assert j3["figs"] == {} and j3["heads"] == HEADS and j3["head_imgs"] == HEAD_IMGS
    # keys=meas -> a LITE envelope: only that figure, no heads / tile / more
    j3b = json.loads(_get("/figs.json?sid=test&keys=meas")[1])
    assert set(j3b["figs"]) == {"meas"} and "heads" not in j3b and "more" not in j3b and j3b["ui"] == UI
    # frozen flag flips without a re-push; stamp unchanged
    LS.set_frozen("test", True)
    j4 = json.loads(_get("/figs.json?sid=test&since=0")[1])
    assert j4["frozen"] is True and j4["stamp"] == stamp and "figs" in j4
    LS.set_frozen("test", False)
    # a PARTIAL push (sep + heads only): envelope stamp and sep_stamp advance, map/err keep their stamp + JSON,
    # a client holding the old stamps receives ONLY sep; tails / view / ui / more are kept
    heads2 = {**HEADS, "tgt": {**HEADS["tgt"], "E": 2110.0}}
    imgs2 = [dict(HEAD_IMGS[0], x=2110.0), HEAD_IMGS[1]]
    s2 = LS.push("test", {"sep": _fake_figs()["sep"]}, t_now=1787926952.0, clock="07:22:32", frozen=False, heads=heads2, head_imgs=imgs2)
    assert s2 > stamp
    j5 = json.loads(_get(f"/figs.json?sid=test&since={stamp}&map={stamp}&sep={stamp}&err={stamp}&meas={stamp}")[1])
    assert j5["stamp"] == s2 and j5["sep_stamp"] == s2 and j5["map_stamp"] == stamp and j5["err_stamp"] == stamp
    assert list(j5["figs"]) == ["sep"] and j5["heads"] == heads2 and j5["head_imgs"] == imgs2 and j5["view"] == VIEW and j5["tails"] == TAILS and j5["ui"] == UI
    # a push without heads / head_imgs keeps the previous ones; a new view rev replaces the view
    LS.push("test", {"sep": _fake_figs()["sep"]}, t_now=1787926952.5, clock="07:22:32", frozen=False, view={**VIEW, "rev": 3})
    j5b = json.loads(_get("/figs.json?sid=test")[1])
    assert j5b["heads"] == heads2 and j5b["head_imgs"] == imgs2 and j5b["view"]["rev"] == 3
    assert "range" not in LS.figs_of("test")["map"]["layout"]["xaxis"]                        # the store holds the stripped map


def test_satellite_tile_follows_the_browser_view():
    """The tile is fetched for the CLIENT's view box (worker thread, quantised key, cached), handed over as
    "sat": {key, imgs} only when the key differs from the client's satk, externalised to /img/."""
    LS.start(PORT)
    SV = {"x0": 20900.0, "x1": 24100.0, "y0": 18900.0, "y1": 22100.0, "rev": 0, "mode": "fixed"}   # a box no other test's real tile fetch uses
    key = LS.sat_key((SV["x0"], SV["x1"], SV["y0"], SV["y1"]))
    with LS._LOCK:
        LS.SAT.pop(key, None); LS.SAT.pop(LS.sat_key((1530, 2530, 30, 1030)), None)   # SAT is process-wide: never reuse a cached tile here
    LS.push("sat", _fake_figs(), t_now=0.0, clock="00:00:00", frozen=False, view=SV, sat_fn=_sat_fn, sat_on=True)
    # first poll: no client view yet -> the frame view's tile is requested (worker); the envelope may not have it yet
    for _ in range(50):
        j = json.loads(_get("/figs.json?sid=sat")[1])
        if "sat" in j:
            break
        time.sleep(0.05)
    assert "sat" in j, "tile never arrived"
    assert j["sat"]["key"] == LS.sat_key_str(key) and len(j["sat"]["imgs"]) == 1
    im = j["sat"]["imgs"][0]
    assert im["source"].startswith("@img/") and im["source"].endswith(".jpg") and im["layer"] == "below" and im["sizing"] == "stretch"
    code, raw, h = _get("/img/" + im["source"][5:])
    assert code == 200 and h["Content-Type"] == "image/jpeg" and len(raw) == 40_000 and "immutable" in h["Cache-Control"]
    # the client reports the same key -> no tile re-sent
    j2 = json.loads(_get(f"/figs.json?sid=sat&satk={j['sat']['key']}")[1])
    assert "sat" not in j2
    # the client zoomed (its own view): a DIFFERENT box -> a new tile for it; the key is the view widened 15 % (SAT_MARGIN) and
    # quantised to 100 m (a few-metre pan away from a rounding boundary maps to the same key -> cached tile)
    k_user = LS.sat_key((1530, 2530, 30, 1030))
    m = 0.5 + LS.SAT_MARGIN   # the fetch box = the view widened SAT_MARGIN each way (0.5 -> 2x the view), 100 m quantised
    assert k_user == (round(2035 - m * 1000, -2), round(2035 + m * 1000, -2), round(535 - m * 1000, -2), round(535 + m * 1000, -2)) and LS.sat_key((1535, 2535, 35, 1035)) == k_user and k_user != key
    # keep-rule: a tile still covering the view at <= 2x its width is kept (no fetch, no re-send); a pan outside it or a 2x zoom-in is not
    assert LS.sat_covers(LS.sat_key_str(k_user), (1530, 2530, 30, 1030)) and LS.sat_covers(LS.sat_key_str(k_user), (1600, 2400, 100, 900))
    assert not LS.sat_covers(LS.sat_key_str(k_user), (800, 1800, 30, 1030)) and not LS.sat_covers(LS.sat_key_str(k_user), (1900, 2200, 400, 700))
    assert not LS.sat_covers("", (0, 1, 0, 1)) and not LS.sat_covers("bad", (0, 1, 0, 1))
    assert all(v % 100 == 0 for v in k_user) and k_user[0] <= 1530 and k_user[1] >= 2530 and k_user[2] <= 30 and k_user[3] >= 1030
    for _ in range(50):
        j3 = json.loads(_get(f"/figs.json?sid=sat&view=1530,2530,30,1030&satk={j['sat']['key']}")[1])
        if "sat" in j3:
            break
        time.sleep(0.05)
    assert j3["sat"]["key"] == LS.sat_key_str(k_user) and j3["sat"]["imgs"][0]["sizex"] == k_user[1] - k_user[0]
    assert LS.parse_view("1500,2500,0,1000") == (1500.0, 2500.0, 0.0, 1000.0) and LS.parse_view("") is None and LS.parse_view("1,0,0,1") is None
    # PAUSED / stale (the stamp does not move): the "unchanged" reply still carries a tile for a view the browser moved to (user: "edges don't load")
    st_ = json.loads(_get("/figs.json?sid=sat")[1])["stamp"]
    for _ in range(50):
        j4 = json.loads(_get(f"/figs.json?sid=sat&since={st_}&view=5530,6530,30,1030&satk={j3['sat']['key']}")[1])
        if "sat" in j4:
            break
        time.sleep(0.05)
    assert j4.get("unchanged") is True and j4["sat"]["key"] == LS.sat_key_str(LS.sat_key((5530, 6530, 30, 1030)))
    j5 = json.loads(_get(f"/figs.json?sid=sat&since={st_}&view=5530,6530,30,1030&satk={j4['sat']['key']}")[1])
    assert j5.get("unchanged") is True and "sat" not in j5                                             # the client has it: nothing re-sent
    # the fallback path keeps the tile INSIDE the figure and the server externalises it there too
    LS.push("sat2", {"map": _fake_figs(sat=True)["map"]}, t_now=0.0, clock="00:00:00", frozen=False)
    imgs = LS.figs_of("sat2")["map"]["layout"]["images"]
    assert imgs[0]["source"].startswith("@img/") and imgs[1]["source"].startswith("data:image/svg+xml")


def test_layout_presets_geometry():
    """LAYOUT(preset): header HEADER_PX (32 since 2026-09-14); two-column presets: map = panel − 2 headers − velocity strip,
    right column = sep ~32 % / err the rest with the error panel floored so every card keeps a >= MIN_CARD_PX plot area
    under its ERR_HDR_PX strip; the Laptop panel is too short for that -> mode "one" (2026-09-15: the two-up first row —
    map = sep = panel − header, one_row() squares the map's plot area / err ERR_MIN_PX + vel below, the iframe scrolls).  Panels fill ~85–90 % of 768 / 1080 / 1440 (600 / 860 / 1180);
    fonts 13 / 14 / 16 x the text scale, lines 2.5 / 2.5 / 3."""
    Ld, Ll, Lg = LS.layout("Desktop 1080p", 1.0), LS.layout("Laptop", 1.0), LS.layout("Large 1440p", 1.0)
    # two-column presets: MAP over the VELOCITY STRIP (24 % of the panel, 160..230 px) on the left, separation over the error cards on the right
    assert (Ld["panel"], Ld["mode"], Ld["vel"], Ld["font_px"], Ld["line_w"]) == (860, "two", 206, 14, 2.5) and (Ld["sep"], Ld["err"]) == LS._fit_two(860)
    assert (Lg["panel"], Lg["mode"], Lg["vel"], Lg["font_px"], Lg["line_w"]) == (1180, "two", 230, 16, 3.0) and (Lg["sep"], Lg["err"]) == LS._fit_two(1180)
    assert (Ll["panel"], Ll["mode"], Ll["map"], Ll["sep"], Ll["err"], Ll["vel"], Ll["font_px"], Ll["line_w"]) == (600, "one", 600 - LS.HEADER_PX, 600 - LS.HEADER_PX, LS.ERR_MIN_PX, LS.ONE_VEL_PX, 13, 2.5)
    assert LS.vel_strip_px(688) == 165 and LS.vel_strip_px(860) == 206 and LS.vel_strip_px(1180) == 230
    assert LS.ONE_VEL_PX == 3 * (LS.MIN_CARD_PX + LS.ERR_HDR_PX) + LS.ERR_T + LS.ERR_B + 2 * LS.ERR_GAP   # stacked mode: three velocity cards at the plot-area floor
    for L in (Ld, Lg):
        assert L["header"] == LS.HEADER_PX == 32 and L["map"] == L["panel"] - 2 * L["header"] - L["vel"]
        assert 2 * L["header"] + L["map"] + L["vel"] == L["panel"] == 2 * L["header"] + L["sep"] + L["err"]
        assert (L["err"] - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - LS.ERR_HDR_PX >= LS.MIN_CARD_PX   # plot area under the header strip
        assert abs(L["sep"] / (L["sep"] + L["err"]) - LS.SEP_FRAC) < 0.01 and LS.SEP_FRAC == 0.32 and LS.SEP_FRAC3 == 0.26   # separation ~32 % (2-col) / 26 % of the middle column (3-col)
    assert LS._fit_two(688) == (688 - 2 * LS.HEADER_PX - LS.ERR_MIN_PX, LS.ERR_MIN_PX) and LS._fit_two(688)[0] >= LS.SEP_MIN_PX   # a 1080p browser (fitted panel 688) keeps two columns
    assert Ll["map"] == Ll["sep"] == Ll["panel"] - Ll["header"] and Ll["meas"] == Ld["meas"] == LS.MEAS_PX == 520
    assert LS.layout(None, 1.0) == LS.layout("nope", 1.0) == LS.LAYOUT == Ld and LS.DEFAULT_PRESET == "Desktop 1080p"
    assert LS.layout("Large 1440p", 1.15)["font_px"] == 18 and LS.layout("Desktop 1080p", 1.3)["font_px"] == 18   # "Text size" scales the figure fonts
    assert list(LS.PRESETS) == ["Laptop", "Desktop 1080p", "Large 1440p"] and LS.SPLIT == (62, 38) and LS.SPLIT3 == (46, 27, 27)   # the MAP dominates; 3-col = map | sep + velocity | error
    assert LS._fit_two(860) == (Ld["sep"], Ld["err"]) and LS._fit_two(720) == (720 - 2 * LS.HEADER_PX - LS.ERR_MIN_PX, LS.ERR_MIN_PX) and LS._fit_two(600) is None   # the 53 px plot-area floor (+ 46 px strips), then stacked
    assert LS.ERR_HDR_PX == PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0] == PL.err_strip_px(18, 12)[0]   # the strip follows the plots readout sizes (46)
    assert LS.ERR_MIN_PX == 4 * (LS.MIN_CARD_PX + LS.ERR_HDR_PX) + LS.ERR_T + LS.ERR_B + 3 * LS.ERR_GAP and LS.ONE_ERR_PX == LS.ERR_MIN_PX and LS.FIG_KEYS == ("map", "sep", "err", "vel", "meas") and LS.PANEL_KEYS == ("map", "sep", "err", "vel")


def test_unknown_sid_404_and_gzip():
    LS.start(PORT)
    code, body, h = _get("/figs.json?sid=nope")
    assert code == 404 and json.loads(body)["error"] and h["Access-Control-Allow-Origin"] == "*"
    LS.push("gz", _fake_figs(), t_now=0.0, clock="00:00:00", frozen=False)
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/figs.json?sid=gz", headers={"Accept-Encoding": "gzip"})
    r = urllib.request.urlopen(req, timeout=5)
    assert r.headers.get("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(r.read()))["figs"]["sep"]["layout"]["uirevision"] == "live-sep"


def test_plotly_js_served_with_cors():
    LS.start(PORT)
    code, body, h = _get("/plotly.min.js")
    assert code == 200 and h["Content-Type"].startswith("application/javascript") and h["Access-Control-Allow-Origin"] == "*"
    assert len(body) == len(LS.load_plotly_js() or b"") and body[:20].lstrip().startswith((b"/**", b"!function", b"(function", b"/*"))
    code, _, _ = _get("/nothing")
    assert code == 404


def test_panel_html_is_pure_and_bakes_sid_host_port_cadence_and_preset():
    a = LS.panel_html("abc123", "172.18.1.28", 8902, 1000)
    assert a == LS.panel_html("abc123", "172.18.1.28", 8902, 1000) == LS.panel_html("abc123", "172.18.1.28", 8902, 1000, LS.LAYOUT)
    assert 'SID="abc123"' in a and 'HOST="172.18.1.28"' in a and "PORT=8902" in a and "P=1000" in a
    assert "Plotly.react" in a and "/plotly.min.js" in a and "/figs.json?" in a and "uirevision" not in a  # uirevision travels in the JSON
    assert 'id="map"' in a and 'id="sep"' in a and 'id="err"' in a and 'id=st' in a and 'id="pill"' in a and 'id="more"' in a and 'id="grid"' in a
    assert 'id="eng"' not in a and 'id="clo"' not in a
    b = LS.panel_html("abc123", "100.124.170.89", 8902, 2000)
    assert a != b and 'HOST="100.124.170.89"' in b and "P=2000" in b
    # the preset is baked in: a different preset -> different HTML (Streamlit remounts the iframe once, intended)
    lap = LS.panel_html("abc123", "172.18.1.28", 8902, 1000, LS.layout("Laptop"))
    assert lap != a and "PANEL=600" in lap and "PANEL=860" in a and "PANEL=1180" in LS.panel_html("x", "h", 1, 1, LS.layout("Large 1440p"))
    L = LS.LAYOUT
    assert f"#map{{height:{L['map']}px;}}" in a and f"#sep{{height:{L['sep']}px;}}" in a and f"#err{{height:{L['err']}px;}}" in a
    assert f"UI={{font_px:{L['font_px']},line_w:{L['line_w']}" in a
    assert LS.host_from_header("172.18.1.28:8901") == "172.18.1.28"
    assert LS.host_from_header("[fd7a:115c::5d01]:8901") == "[fd7a:115c::5d01]"
    assert LS.host_from_header("mru91-mx") == "mru91-mx" and LS.host_from_header(None) == ""
    # the measurement-space iframe: its own constant page polling keys=meas
    m = LS.meas_html("abc123", "172.18.1.28", 8902, 1000)
    assert 'id="meas"' in m and "keys=meas" in m and f"#meas{{width:100%;height:{L['meas']}px" in m and "Plotly.react" in m and "Plotly.Plots.resize" in m


def test_panel_js_has_the_responsive_breakpoints_view_lock_and_tween():
    """String contract for the in-iframe JS (the real behaviour is exercised in headless Firefox by
    tests/test_panel_js.py): breakpoints 1150 / 2400 / 1400, the 95 px card floor, stacked-mode scroll,
    relayout + Plots.resize on fit, font bump, the view lock (no server ranges, user relayout -> lock, reset
    pill, follow hysteresis with a 600 ms animate), the rAF tween with shortest-arc heading drawn by the DOM
    overlay (30 Hz, l2p pixels, one base SVG per role rotated by CSS, SVG live segments / leaders, pixel-stacked pills,
    cost measurement -> 15 Hz), the satellite tile-only relayout, and the ultrawide chips."""
    js = LS.panel_script("abc123", "172.18.1.28", 8902, 1000)
    for needle in ("BP=1150", "UW=2400", "FONT_BP=1400", f"MIN_CARD={LS.MIN_CARD_PX}", f"ERR_MIN={LS.ERR_MIN_PX}", f"ERR_HDR={LS.ERR_HDR_PX}", "ICON_PX=44", "function errRows(", "meta.err", "window.innerWidth", 'addEventListener("resize"',
                   'mode:"three"', 'mode:"two"', 'mode:"one"', "var mp=PANEL-HEADER, mm=mapMargin(), ph=mp-MAP_T0-mm.b, mapW=ph+mm.l+mm.r", "Math.min(PANEL-HEADER", "Plotly.Plots.resize", "Plotly.relayout",
                   # 2026-09-15 laptop panel: width pinned to the div, the key rows off in mode "one", icon <= 10 % of the plot height, the grid's own ResizeObserver, the frame re-applied after a re-shaped map
                   "u.width=Math.round(gd.clientWidth", "function legendPatch(k,mode)", 'u["margin.t"]=one?MAP_T0', f"MAP_T0={LS.map_top_nolegend_px()}", "ICON_FRAC=0.10", "Math.round(ICON_FRAC*ph)",
                   "new ResizeObserver(", "function refitView(p0)", 'grid.style.gridTemplateColumns=g.mode==="one"?(g.mapW+"px minmax(0,1fr)"):""',
                   '"font.size":f', "tickfont.size", "Math.max(b,Math.round(15*sc))", "Math.max(b,Math.round(13*sc))", "Math.max((UI&&UI.line_w)||2.5,3)",
                   "SEP_FRAC3", "viewportPanel", "window.parent.innerHeight", "frameElement", "bindParentKeys", "--ih-scale",   # A15 / A17: viewport fit, "m" key, text scale
                   'documentElement.className=g.mode==="one"?"one":""', "fitLayout(k,f.layout)", "fitLines(f.data", "renderMore(",
                   # view lock
                   "delete layout[ax].range", "delete layout[ax].autorange", "onMapRelayout", "plotly_relayout", 'addEventListener("dblclick"', "doubleClick:false",
                   "aspectRange", "plotPx", "lockView()", "unlockView()", "PILLS", "eventsCard", "satSoon",
                   "resetView", 'getElementById("pill")', "takeView(j.view)", "v.rev!==VIEW.rev", "&view=\"+viewParam()", "&satk=", "j.sat.imgs", "SATK=j.sat.key",
                   "followCheck", "EDGE=0.15", "transition:{duration:600,easing:\"cubic-in-out\"}", "VIEW.prog",
                   # tween -> DOM overlay
                   "requestAnimationFrame(tweenFrame)", "lerpAng", "((b-a+540)%360)-180", "quant5", "Math.min(tau-TW.P,0.5*TW.P)", "tweenState", "performance.now()",
                   "rate:30", "OV.avg>8&&TW.rate>15", "TW.rate=15", "function ovMake(", "function ovDraw(", "function ovPills(", "function ovGeom(", "function axes()",
                   "xa.l2p(", "ya.l2p(", "xa._offset", "ya._offset", "xa._length", "ya._length", 'gd.on("plotly_relayouting",ovNow)', "rotate(\"+h.hdg.toFixed(1)+\"deg)",
                   "im.src=HEAD_SRC[k]", "HEAD_SRC={tgt:", 'data-name",k+"_"+w', "LEADER_MIN_PX),LEADER_MAX_PX)", "function satFlush(", "SAT_DIRTY", "get overlay()",
                   "f.layout.images=(satImgs||[]).slice(); f.layout.shapes=[]; f.layout.annotations=(f.layout.annotations||[]).filter(function(a){ return !isPill(a); });"):
        assert needle in js, needle
    css = LS.panel_css()
    assert "html.one,html.one body{overflow-y:auto" in css and ".grid.three{grid-template-columns:46fr 27fr 27fr" in css and 'id="velblock"' in LS.panel_body() and "function cardGrid" in LS.panel_html("s", "h", 1, 1000, LS.LAYOUT) and ".grid.two{grid-template-columns:62fr 38fr" in css
    assert "font-size:calc(16px * var(--ih-scale,1))" in css and ".ih-events{" in css and "var(--hf,1rem)" in css and "var(--cf,.85rem)" in css   # rem type, scaled
    assert ".grid.three #more{display:none;}" in css and ".ih-chip{" in css
    # the view-lock pill is an inline chip in the map header's right group (2026-09-14: the absolute overlay covered the plot)
    assert "#pill{position:absolute" not in css and "#pill{cursor:pointer;" in css and "font:500 calc(var(--hf,1rem) * .85)/1" in css
    body = LS.panel_body()
    assert 'class="col cl"' in body and 'class="col cm"' in body and 'class="col cr"' in body and "view locked" in body
    pill = body.index('<span id="pill" hidden')
    assert body.rindex('<div class="h"', 0, pill) < body.rindex('<span class="r">', 0, pill) and body.index('<div id="map"', pill) < body.index('<div class="h"', pill)   # in the MAP header's .r group
    assert T.glyph("amber", "reset", icon="reset") in body and T.glyph("na", "connecting…") in body        # D7: stroke-SVG icons + word, no dingbats
    assert not re.search(r"[●▲✕○⟲❚]", body) and "ICONS=" in js and "STATUS_WORDS=" in js and "function gw(c,w)" in js and not re.search(r"[●▲✕○⟲❚]", js)
    assert body.count('class="h"') == 4 and "Separation · 3D &amp; horizontal (m)" in body and "Track quality" in body
    # the core JS is shareable (static preview / test harness): no fetch loop, no baked sid
    core = LS.panel_core_js()
    assert "applyEnvelope" in core and "fetch(" not in core and "SID=" not in core and "setInterval(tick" not in core   # (the 1 s wrapper-height guard in setFrameHeight is not a poller)
    assert "frameChain" in core and 'setProperty("flex-basis"' in core and "scrollerOf" in core                     # item 2: wrapper chain sized (flex item), scroll-safe viewport fit


def test_panel_html_draws_heads_in_a_dom_overlay_and_never_relayouts_them():
    """2026-09-14: the vehicle heads / leaders / live segments / track pills are a DOM OVERLAY the panel JS builds INSIDE the map
    div after the first paint (ovMake): the body markup stays free of it (the only inline SVGs are the theme status icons), the
    JS converts metres -> pixels with the map's own l2p / _offset (the browser owns the view, so _fullLayout is the truth), the
    tween loop and the overlay update contain NO Plotly call, the map react carries the satellite tiles only (shapes [], pill_*
    annotations stripped), and one animated base SVG per role (heading 0) is baked in and rotated with CSS."""
    from ih import icons

    a = LS.panel_html("abc123", "172.18.1.28", 8902, 1000)
    body = LS.panel_body()
    for gone in ("ic-tgt", "ic-itc", 'class="ov"', "animateTransform", "<animate", "mapwrap", "<image", "ov-head", "ov-pill"):
        assert gone not in body, gone
    assert body.count("<svg") == sum(body.count(v) for v in T.ICONS.values())                            # the only inline SVGs are the theme's status icons
    js = LS.panel_script("abc123", "172.18.1.28", 8902, 1000)
    core = LS.panel_core_js()
    for gone in ("liveSegments", "mapAnnotations", "depill(", "leaderShape(", "sizeIcons(", "mapImages(", "TW.uris", "quant5(hd)", "shapes:st.shapes",
                 "Plotly.relayout(gd,{images:(satImgs||[]).concat", "annotations:mapAnnotations", "transition .9s linear", "Plotly.restyle"):
        assert gone not in js, gone
    for needle in ("l2p(", "_offset", "_length", "plotly_relayouting", "function ovMake(", "function ovDraw(", "ovMake(gd)", "gd.appendChild(el)", 'el.className="ov"',
                   "im.src=HEAD_SRC[k]", "im.hidden=true", 'im.style.transform="translate(', "rotate(", "j.head_imgs", "HEADIMGS", "j.pills", "PILLS", "isPill(a)",
                   "reacting", "fetching", 'displayModeBar:"hover"', "hasWebGL", 'type="scatter"', "placeHeads", "ovNow()", "get overlay()", "relayouts:OV.relayouts", "tile_relayouts:SATN"):
        assert needle in js, needle
    # the tween loop / overlay update never call Plotly; the ONLY relayouts left are the tile-only satFlush / satHeal, our own range applyRange and the geometry fit
    for fn_start, fn_end in (("function tweenFrame(", "function eventsCard("), ("function ovDraw(", "function ovNow("), ("function ovPills(", "function ovDraw("), ("function ovMake(", "function ovGeom(")):
        blk = core[core.index(fn_start):core.index(fn_end)]
        assert "Plotly." not in blk, (fn_start, blk[:200])
    assert core.count("Plotly.relayout(gd") == 4 and "Plotly.relayout(gd,{images:(satImgs||[]).slice()})" in core     # the 4 call sites: satFlush (tile), applyRange (range), applyGeometry (height / fonts), satHeal (tile, user drag)
    # 2026-09-15 satHeal ("drag to pan and only the track moves"): a USER-drag tile heal, never part of the tween / overlay path —
    # its body does nothing but re-apply the satellite images, and it is bound ONLY to plotly_relayouting and to plotly_relayout
    # behind the user-range-key guard (an unguarded plotly_relayout binding would feed its own images relayout back at 12 Hz)
    heal = core[core.index("function satHeal("):core.index("function bindMap(")]
    assert heal.count("Plotly.") == 1 and "Plotly.relayout(gd,{images:(satImgs||[]).slice()})" in heal and "range" not in heal, heal
    bind = core[core.index("function bindMap("):core.index("function noGL(")]
    assert bind.count("satHeal") == 2 and 'gd.on("plotly_relayouting",satHeal)' in bind
    assert r'gd.on("plotly_relayout",function(ev){ var ks=Object.keys(ev||{}); if(ks.some(function(k){ return /^[xy]axis\.(range|autorange)/.test(k); })) setTimeout(satHeal,0); })' in bind
    assert core.count("Plotly.animate(") == 1 and core.count("Plotly.react(") == 1 and core.count("Plotly.newPlot(") == 1
    # the map react: satellite tiles only, no shapes, the figure's own annotations minus the pills
    assert "f.layout.images=(satImgs||[]).slice(); f.layout.shapes=[]; f.layout.annotations=(f.layout.annotations||[]).filter(function(a){ return !isPill(a); });" in core
    # one base SVG per role (heading 0, animated) baked in — the overlay rotates it; no per-5° URIs
    for role, col in (("tgt", T.TARGET), ("itc", T.INTERCEPTOR)):
        assert json.dumps(icons.icon_uri(role, 0.0, col, T.SURFACE, True)) in core, role
    assert "<animate" in icons.icon_uri("tgt", 0.0, T.TARGET, T.SURFACE, True).replace("%3C", "<") and "rotate(0.0 50 50)" in icons.icon_uri("itc", 0.0, T.INTERCEPTOR, T.SURFACE, True).replace("%20", " ").replace("%28", "(").replace("%29", ")")
    css = LS.panel_css()
    assert ".ov{position:absolute;left:0;top:0;width:0;height:0;overflow:hidden;pointer-events:none;z-index:2;}" in css
    assert ".ov .ov-head{position:absolute;left:0;top:0;width:44px;height:44px;will-change:transform;transform-origin:50% 50%;" in css
    assert f".ov .ov-pill{{position:absolute;left:0;top:0;white-space:nowrap;font:500 13px/1.15 {T.MONO};letter-spacing:0;color:{T.INK};background:{T.CARD};border:2px solid {T.RULE};padding:2px 3px;" in css
    assert "transition:transform" not in css and ".ic{" not in css
    assert "add_layout_image" not in a and "HELPERS_JS" not in a
    assert "★ CPA" in LS.HEADERS["map"][1] and "dashed = radar tracks" in LS.HEADERS["map"][1]
    assert "±1σ band" in LS.HEADERS["err"][1] and "3D pos = 1σ radius" in LS.HEADERS["err"][1] and "dotted" not in LS.HEADERS["err"][1]
    assert "lighter segments + bottom strip = coasting/tentative (not in stats)" in LS.HEADERS["err"][1] and "gaps > 3 s = dropout" in LS.HEADERS["err"][1]
    assert len(LS.HEADERS["map"][1]) <= 80 and len(LS.HEADERS["err"][1]) <= 130          # captions are hidden whole when a column is too narrow
    assert "solid thick = truth (red target · blue interceptor)" in LS.HEADERS["meas"][1] and "circles = raw obs" in LS.HEADERS["meas"][1] and "2×mono" in LS.HEADERS["meas"][1]
    assert 'class="cap"' in LS.panel_body() and "fitCaptions" in LS.responsive_js() and 'title="' in LS.panel_body()   # hidden whole, never cut mid-word
    assert "amb_dop" in LS.HEADERS["meas"][1]


def test_module_reload_orphans_store_and_adopt_rebinds_port_and_store():
    """Regression (2026-09-10 live crash: int(None) in panel_script): Streamlit hot-reloads ih.liveserver as a NEW module object
    (fresh _SERVER=None / STORE={}), while the Live page's @st.cache_resource keeps the OLD server whose handler thread still reads
    the OLD module's STORE.  start() attaches srv.store / srv.images; adopt(srv) makes the new module's _SERVER / STORE / IMAGES the
    server's, so port() is back and a push from the new module is served by the old handler."""
    import importlib
    import sys

    srv = LS.start(PORT)
    assert srv is not None and srv.store is LS.STORE and srv.images is LS.IMAGES and LS.port() == PORT
    old_mod = sys.modules["ih.liveserver"]
    try:
        del sys.modules["ih.liveserver"]
        LS2 = importlib.import_module("ih.liveserver")                        # what the reloaded page imports: a different module object
        assert LS2 is not old_mod and LS2.port() is None and LS2.STORE is not srv.store and not LS2.is_up()   # the bug: PANEL would be True, port None
        LS2.adopt(srv)
        assert LS2.port() == PORT and LS2.is_up() and LS2.STORE is srv.store is LS.STORE and LS2.IMAGES is srv.images is LS.IMAGES
        assert LS2.start(PORT) is srv                                          # idempotent after the adoption
        LS2.push("reloaded", _fake_figs(), t_now=1.0, clock="00:00:01", frozen=False, view=VIEW, ui=UI)
        code, body, _ = _get("/figs.json?sid=reloaded")                        # served by the OLD handler thread (old module globals)
        assert code == 200 and json.loads(body)["clock"] == "00:00:01" and LS.has("reloaded")
        assert "PORT=" + str(PORT) in LS2.panel_script("sid", "h", PORT, 1000)  # what the page bakes in — an int, never None
        LS2.adopt(None)                                                        # a None server (bind failed) is a no-op
        assert LS2.port() == PORT
    finally:
        sys.modules["ih.liveserver"] = old_mod                                 # the rest of the suite keeps its module object
    # both call sites keep the pattern: a cache_resource'd server object + LS.adopt (the entry script AND the Live page)
    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    live_src = open(os.path.join(ROOT, "views", "1_live.py"), encoding="utf-8").read()
    assert "@st.cache_resource" in app_src and "def _panel_server_boot(" in app_src and "LS.adopt(_panel_server_boot(LS.DEFAULT_PORT)" in app_src
    assert "LS.adopt(SERVER)" in live_src and "PORT = int(SERVER.server_address[1]) if SERVER is not None else None" in live_src and "LS.port()" not in live_src


# ── 2026-09-15: the card header strips scale with the sidebar "Text size", so every height budget built on them must too ──
SCALES = (1.0, 1.15, 1.3)                       # ih.theme.TEXT_SCALES: Normal / Large / X-Large


def test_header_strip_and_card_floors_follow_the_text_scale():
    """ih.plots builds each card's header strip from the readout fonts px(14) / px(11), so the DRAWN strip is
    46 / 52 / 55 px at Normal / Large / X-Large.  err_hdr_px() (46 / 53 / 60) is the budget's strip: never SHORTER
    than the drawn one (a shorter reserve squeezed the stacked cards to ~44 / ~28 px, under MIN_CARD_PX) and never
    more than 5 px longer (a fat reserve wastes the column).  Every floor built on it scales with it; the scale-1
    numbers are exactly the ERR_MIN_PX / ONE_ERR_PX / ONE_VEL_PX constants (unchanged)."""
    assert (LS.one_sep_px(1.0), LS.one_sep_px(1.15), LS.one_sep_px(1.3)) == (LS.ONE_SEP_PX, 230, 260) == (200, 230, 260)   # the separation card's autoexpand rows scale too
    for sc in SCALES:
        drawn = PL.err_strip_px(PL.px(PL.READOUT_PX, {"text_scale": sc}), PL.px(PL.READOUT_SUB_PX, {"text_scale": sc}))[0]
        assert drawn <= LS.err_hdr_px(sc) <= drawn + 5, (sc, drawn, LS.err_hdr_px(sc))
        assert LS.err_hdr_px(sc) == int(round(LS.ERR_HDR_PX * sc))
        assert LS.err_min_px(sc) == LS.one_err_px(sc) == 4 * (LS.MIN_CARD_PX + LS.err_hdr_px(sc)) + LS.ERR_T + LS.ERR_B + 3 * LS.ERR_GAP
        assert LS.one_vel_px(sc) == 3 * (LS.MIN_CARD_PX + LS.err_hdr_px(sc)) + LS.ERR_T + LS.ERR_B + 2 * LS.ERR_GAP
        assert PL.vel_stacked_px({"text_scale": sc}) == LS.one_vel_px(sc)    # ih.plots' y tick budget takes the same column height
    assert (LS.err_hdr_px(1.0), LS.err_min_px(1.0), LS.one_vel_px(1.0)) == (LS.ERR_HDR_PX, LS.ERR_MIN_PX, LS.ONE_VEL_PX) == (52, 486, 371)
    assert (LS.err_hdr_px(1.15), LS.err_min_px(1.15), LS.one_vel_px(1.15)) == (60, 518, 395)
    assert (LS.err_hdr_px(1.3), LS.err_min_px(1.3), LS.one_vel_px(1.3)) == (68, 550, 419)
    # the default arguments are the scale-1 behaviour: nothing moves for a Normal-text caller
    assert LS._fit_two(688) == LS._fit_two(688, 1.0) and LS.vel_strip_px(860) == LS.vel_strip_px(860, 1.0) == 206
    # the two-column velocity strip: 24 % of the panel between clamps that BOTH grow by the extra strip px
    for sc in SCALES:
        e = LS.err_hdr_px(sc) - LS.ERR_HDR_PX
        for panel in (600, 688, 860, 1180):
            assert LS.vel_strip_px(panel, sc) == min(LS.VEL_STRIP_PX + e, max(LS.VEL_STRIP_MIN_PX + e, round(LS.VEL_STRIP_FRAC * panel))), (panel, sc)
        assert LS.vel_strip_px(600, sc) == LS.VEL_STRIP_MIN_PX + e and LS.vel_strip_px(1180, sc) == LS.VEL_STRIP_PX + e   # both clamps follow the strip


def test_layout_card_heights_scale_with_the_text_size():
    """layout(preset, scale): every card that sits under a header strip keeps its MIN_CARD_PX plot area at Large /
    X-Large instead of squeezing — the stacked (Laptop) err / vel heights and the two-column error floor grow with
    the strip, the map and the separation card do not move."""
    for pre in LS.PRESETS:
        prev = None
        for sc in SCALES:
            L = LS.layout(pre, sc)
            assert L["text_scale"] == sc and L["font_px"] == int(round(LS.FONT_PX[pre] * sc))
            assert (L["err"] - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - LS.err_hdr_px(sc) >= LS.MIN_CARD_PX, (pre, sc, L)
            if L["mode"] == "one":
                assert (L["err"], L["vel"], L["sep"]) == (LS.one_err_px(sc), LS.one_vel_px(sc), L["panel"] - LS.HEADER_PX)
                assert L["map"] == L["sep"] == L["panel"] - LS.HEADER_PX              # the two-up first row = the iframe height; err / vel scroll below
            else:
                assert (L["sep"], L["err"]) == LS._fit_two(L["panel"], sc) and L["vel"] == LS.vel_strip_px(L["panel"], sc)
                assert 2 * LS.HEADER_PX + L["map"] + L["vel"] == L["panel"] == 2 * LS.HEADER_PX + L["sep"] + L["err"]
            if prev is not None:
                assert L["err"] >= prev["err"] and L["vel"] >= prev["vel"], (pre, sc)  # monotone in the text scale, never shorter
            prev = L


def test_panel_js_scales_the_card_budget_and_knows_the_real_map_margins():
    """The panel JS mirrors err_hdr_px / err_min_px / one_err_px / one_vel_px / vel_strip_px from UI.text_scale
    (hdrPx / errMinPx / oneErrPx / oneVelPx / velStripPx), and plotPx()'s PRE-PAINT fallback uses the map figure's
    real margins — the drawn ones, the ones the figure arrived with, else MAP_M = ih.plots.map_margin(font, scale)
    baked in here (the old fallback assumed {l 64, r 12, t 8, b 44} and mis-estimated the first frame's aspect)."""
    for sc in SCALES:
        L = LS.layout("Desktop 1080p", sc)
        js = LS.panel_script("sid", "h", 8902, 1000, L)
        assert f"var MAP_M={json.dumps(LS.map_margin_px(L['font_px'], sc))};" in js, sc
        assert f"text_scale:{float(sc)}" in js and f"ERR_HDR={LS.ERR_HDR_PX}" in js        # the base strip + the scale: the JS derives the rest
        for needle in ("function hdrPx(){ return Math.round(ERR_HDR*((UI&&UI.text_scale)||1)); }",
                       "function errMinPx(){ return 4*(MIN_CARD+hdrPx())+ERR_T+ERR_B+3*ERR_GAP; }",
                       "function oneVelPx(){ return 3*(MIN_CARD+hdrPx())+ERR_T+ERR_B+2*ERR_GAP; }",
                       "function velStripPx(){ var d=hdrPx()-ERR_HDR;", "sep:mp,err:oneErrPx(),vel:oneVelPx()",
                       "function oneSepPx(){ return Math.round(ONE_SEP*((UI&&UI.text_scale)||1)); }",
                       "/4-hdrPx()<MIN_CARD){ err=errMinPx();", "var vs=velStripPx();",
                       "function mapMargin(){", "gd.layout&&gd.layout.margin", "W-m.l-m.r", "H-m.t-m.b"):
            assert needle in js, (sc, needle)
        assert "W-64-12" not in js and "H-8-44" not in js                                  # the stale pre-paint guess is gone
    assert LS.map_margin_px(PL.FONT_PX, 1.0) == {k: v for k, v in PL.MARGINS["map"].items() if k != "autoexpand"} == {"l": 83, "r": 12, "t": 62, "b": 56}
    assert LS.map_margin_px(18, 1.3) == {"l": 106, "r": 12, "t": 69, "b": 70}              # X-Large: the key row under the 26 px modebar row
    assert LS.MAP_MARGIN_FALLBACK == LS.map_margin_px(PL.FONT_PX, 1.0)                     # the no-ih.plots fallback is the default-font truth


def test_separation_header_status_degrades_instead_of_overflowing():
    """The separation card's header right group ("CPA 59 m · 07:22:31" + "live · 07:22:31") overflowed its box by
    6 px at Large and 75 px at X-Large in two-column mode (clip_audit "header-right-overflow"): fitCaptions only
    hides CAPTIONS and that header has none.  fitStatus() now drops detail in a FIXED order — the clock's seconds,
    the clock, the CPA's time, then the words (the status icon's colour still carries the state) — measuring the
    box after each step, and never touches the header's height."""
    js = LS.panel_script("sid", "h", 8902, 1000)
    core = LS.panel_core_js()
    for needle in ("function fitStatus(", "function statHtml(", "function setStatus(", "function setHud(",
                   "STAT_STEPS=[[0,0],[1,0],[2,0],[2,1],[3,1],[3,2]]", "c.slice(0,5)", 'gw(STAT.cls,"")',
                   "r.scrollWidth<=r.clientWidth+1", "fitStatus();", 'el.title=STAT.word'):
        assert needle in js, needle
    assert 'setStatus(j.frozen?"pause":"ok", j.frozen?"frozen":"live", j.clock)' in core and "setHud(j.hud.cpa" in core
    assert 'el.innerHTML=j.frozen?gw("pause"' not in core and "hd.textContent=j.hud.cpa" not in core   # the envelope no longer writes the header text directly
    fitcap = core[core.index("function fitCaptions("):core.index("var STAT=")]
    assert "fitStatus();" in fitcap                                                        # every caption fit re-fits the status (resize / geometry change)
    assert "function status(cls,txt){ setStatus(cls||\"na\",txt,\"\"); }" in js            # the poller's own messages go through the same renderer


def test_measurement_quad_iframe_shows_no_modebar():
    """The meas card's key sits in its top margin; plotly's hover modebar row sat over it (14 px at X-Large), so the
    quad joins the other card figures with displayModeBar false — only the MAP keeps a modebar."""
    m = LS.meas_html("sid", "h", 8902, 1000)
    assert "displayModeBar:false" in m and 'displayModeBar:"hover"' not in m
    assert 'displayModeBar:"hover"' in LS.panel_core_js()                                   # ... and the map still has one


def test_measurement_quad_header_caption_is_hidden_when_it_does_not_fit():
    """The quad's own iframe header carried the long explanatory caption with text-overflow: ellipsis — i.e. it
    always overflowed its box (clip_audit 2026-09-15: 756-1993 px, every size).  It now follows the panel's rule:
    the caption is hidden WHOLE when the row is too narrow, and the header's title keeps the full text."""
    m = LS.meas_html("sid", "h", 8902, 1000)
    assert "function fitCap(){" in m and "c.hidden=true" in m and "fitCap();" in m and 'addEventListener("resize",function(){ fitCap();' in m
    assert '<span class="r"><span class="cap">' in m and 'class="h" title="' in m and "text-overflow:ellipsis" not in m
    assert ".h .r .cap[hidden]{display:none;}" in LS.meas_css() and "white-space:nowrap" in LS.meas_css()


def test_one_row_squares_the_map_and_keeps_a_separation_column():
    """one_row(panel, W): mode "one" first row.  The map's plot area is SQUARE (mapW − l − r == map − MAP_T0 − b), the map and the
    separation card are both panel − header tall, the separation column is what is left (>= ONE_RIGHT_MIN_PX: on a very narrow
    iframe the map narrows first).  At the 1366x768 laptop numbers (iframe 945 x 485 Normal / 462 Large) the plot area is 369 / 340 px
    (the old full-width stacked map: 829 x 267)."""
    t0 = LS.map_top_nolegend_px()
    assert t0 == PL.MAP_MODEBAR_PX + 2 == 28
    for panel, W, sc in ((485, 945, 1.0), (462, 945, 1.15), (485, 1299, 1.0), (600, 1000, 1.0), (485, 520, 1.0)):
        L = LS.layout("Desktop 1080p", sc)
        r = LS.one_row(panel, W, L["font_px"], sc)
        m = LS.map_margin_px(L["font_px"], sc)
        assert r["map"] == r["sep"] == panel - LS.HEADER_PX and r["margin"] == m and r["t0"] == t0
        assert r["plot"] == r["map"] - t0 - m["b"] and r["plot_w"] == r["mapW"] - m["l"] - m["r"]
        assert r["mapW"] + r["right"] + LS.COL_GAP_PX == W
        if W >= 900:
            assert r["plot_w"] == r["plot"] and r["right"] >= LS.ONE_RIGHT_MIN_PX, (panel, W, sc, r)          # square, and a real separation column
        else:
            assert r["right"] == min(LS.ONE_RIGHT_MIN_PX, round(W * 0.4)) and r["plot_w"] < r["plot"], (W, r)   # narrow: the separation keeps its floor, the map narrows
    assert LS.one_row(485, 945, 14, 1.0)["plot"] == 485 - 32 - 28 - 56 == 369 and LS.one_row(485, 945, 14, 1.0)["mapW"] == 369 + 83 + 12 == 464
    assert LS.one_row(462, 945, 16, 1.15)["plot"] == 340 and LS.one_row(462, 945, 16, 1.15)["right"] >= 480
