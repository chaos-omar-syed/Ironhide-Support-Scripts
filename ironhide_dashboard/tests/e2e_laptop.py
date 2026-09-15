"""Real-browser checks for the LAPTOP panel (1366x768, sidebar open, mode "one") — selenium + headless Firefox against a running
Streamlit instance; not collected by ``pytest tests/`` (no ``test_`` prefix).  Run it explicitly:

  cd ironhide_dashboard && [IH_E2E_URL=http://127.0.0.1:<port>] python -m pytest -q tests/e2e_laptop.py

Fixtures / helpers come from tests/e2e_browser.py (the module-scoped ``app`` side instance, Browser, console collection, the
sidebar radio); the panel clipping rules from tests/clip_audit.py (PANEL_JS).  Screenshots + JSON logs land in IH_E2E_SHOTS.

What is pinned (2026-09-15 laptop audit, ih/liveserver.py fixes 1-4), for flight 1 replayed from 07:21:40 at 1x, Text size Normal AND Large:
  (1) WIDTH PIN: after a 1366 -> 1536 -> 1366 window resize and after collapsing Streamlit's sidebar, every figure's _fullLayout.width
      equals its div width +- 2 px (Plots.resize used to leave an explicit layout.width behind — 371 px of dead card after a
      collapse), and the map's view keeps 1:1 pixels (m/px equal on x and y).
  (2) TWO-UP FIRST ROW in mode "one": the map's plot area is SQUARE (+- 2 px) and the separation card sits to its right, both the
      iframe height, its plot area >= 200 px tall and wholly visible without scrolling; the error / velocity cards follow below.
      At 07:22:31 the engagement box (envelope view) fills >= 85 % of the plot area and both vehicle heads are visible.
  (3) ICON edge = max(16, min(round(44 x text scale), round(0.10 x plot-area height))) +- 1 px.
  (4) the map's key row and the separation key are OFF (showlegend false; map margin.t = the modebar row + 2).
  Plus: 0 panel clipping findings (clip_audit.PANEL_JS), figure div sizes constant during 20 s of play (100 ms sampler), no console errors.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_browser import HAVE_TOOLS, Browser, _dump, _hms_s, _page_clock, _sidebar_radio, app, assert_no_console_errors  # noqa: E402,F401
from clip_audit import PANEL_JS  # noqa: E402
import ih.liveserver as LS  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAVE_TOOLS, reason="geckodriver / firefox / selenium missing")]

T0 = "07:21:40"                 # both vehicles airborne, pass 2 building up
T_CPA = "07:22:31"              # F1 pass 2 (the standard screenshot moment)
ICON_PX, ICON_FRAC = 44, 0.10
SAMPLE_S, SAMPLE_MS = 20.0, 100

GEOM_JS = r"""
var out={W:window.innerWidth,cw:document.documentElement.clientWidth,H:window.innerHeight,scroll:document.documentElement.scrollTop||document.body.scrollTop||0,
         mode:IH.geo&&IH.geo.mode,geo:IH.geo,panel:IH.PANEL,scale:(IH.UI&&IH.UI.text_scale)||1,figs:{}};
['map','sep','err','vel'].forEach(function(id){ var gd=document.getElementById(id), r=gd.getBoundingClientRect(), fl=gd._fullLayout;
  out.figs[id]={div_w:gd.clientWidth,div_h:gd.clientHeight,top:r.top,bottom:r.bottom,left:r.left,right:r.right,
    fl_w:fl?fl.width:null,fl_h:fl?fl.height:null,lay_w:gd.layout?gd.layout.width:null,showlegend:fl?fl.showlegend:null,
    margin:fl?{l:fl.margin.l,r:fl.margin.r,t:fl.margin.t,b:fl.margin.b}:null,
    xlen:(fl&&fl.xaxis)?fl.xaxis._length:null,ylen:(fl&&fl.yaxis)?fl.yaxis._length:null}; });
var fl=document.getElementById('map')._fullLayout;
out.range={x:fl.xaxis.range.slice(),y:fl.yaxis.range.slice()}; out.frame=IH.VIEW.frame; out.userView=IH.VIEW.userView; out.prog=IH.VIEW.prog;
out.icon={want:IH.iconPx()}; out.heads=IH.overlay.heads; out.mpp=IH.mpp();
['tgt','itc'].forEach(function(k){ var im=document.getElementById('ov-head-'+k); out.icon[k]=im?{w:im.offsetWidth,h:im.offsetHeight,hidden:im.hidden}:null; });
var grid=document.getElementById('grid'); out.grid={cols:grid.style.gridTemplateColumns,w:grid.clientWidth,cls:grid.className};
return out;
"""
SIZE_SAMPLER_START_JS = r"""
var MS=arguments[0]; window.__ihSz={rows:[]};
window.__ihSz.tick=function(){ var row={t:Math.round(performance.now())}; ['map','sep','err','vel'].forEach(function(id){ var el=document.getElementById(id);
  if(el) row[id]=[Math.round(el.getBoundingClientRect().width*10)/10, Math.round(el.getBoundingClientRect().height*10)/10]; }); window.__ihSz.rows.push(row); };
window.__ihSz.iv=setInterval(window.__ihSz.tick, MS); window.__ihSz.tick(); return true;
"""
SIZE_SAMPLER_STOP_JS = "try{ clearInterval(window.__ihSz.iv); }catch(e){} return (window.__ihSz||{}).rows||[];"
COLLAPSE_JS = r"""
var b=document.querySelector('[data-testid="stSidebarCollapseButton"] button')||document.querySelector('[data-testid="stSidebarCollapseButton"]');
if(!b) return null; b.click(); return true;
"""
SIDEBAR_W_JS = "var s=document.querySelector('[data-testid=\"stSidebar\"]'); return s?s.getBoundingClientRect().width:0;"


def _want_icon(scale: float, plot_h: float) -> int:
    return max(16, min(round(ICON_PX * scale), round(ICON_FRAC * plot_h)))


def _fill(g: dict) -> float:
    """The envelope frame box's share of the drawn view AREA, 0 when the view does not contain the box (a stale / narrower view
    shows only part of the engagement): 1.0 = the box IS the view."""
    f, r = g["frame"], g["range"]
    if not f or f.get("x0") is None:
        return float("nan")
    rw, rh = r["x"][1] - r["x"][0], r["y"][1] - r["y"][0]
    ix = max(0.0, min(f["x1"], r["x"][1]) - max(f["x0"], r["x"][0]))      # the ENGAGE re-frame has hysteresis (a head within 15 % of the edge): the view may
    iy = max(0.0, min(f["y1"], r["y"][1]) - max(f["y0"], r["y"][0]))      # lag the drifting box by a quantum, so score the INTERSECTION over the view area
    return (ix * iy) / max(1e-9, rw * rh)


def _aspect_err(g: dict) -> float:
    """|m/px on x − m/px on y| / m/px: 0 = exactly 1:1 pixels."""
    r, m = g["range"], g["figs"]["map"]
    mx = (r["x"][1] - r["x"][0]) / max(1.0, m["xlen"])
    my = (r["y"][1] - r["y"][0]) / max(1.0, m["ylen"])
    return abs(mx - my) / max(mx, 1e-9)


def _check_widths(g: dict, tag: str) -> dict:
    """(1) every figure's drawn width == its div width (+- 2 px), the map's view 1:1."""
    out = {}
    for k, f in g["figs"].items():
        assert f["fl_w"] is not None and abs(f["fl_w"] - f["div_w"]) <= 2, (tag, k, "figure width != div width (dead card)", f["fl_w"], f["div_w"])
        assert abs(f["fl_h"] - f["div_h"]) <= 2, (tag, k, "figure height != div height", f["fl_h"], f["div_h"])
        out[k] = (f["div_w"], f["fl_w"])
    assert _aspect_err(g) < 0.01, (tag, "map view not 1:1", g["range"], g["figs"]["map"])
    return out


def _check_two_up(g: dict, tag: str) -> dict:
    """(2) + (4): the two-up first row and the keys off."""
    m, s = g["figs"]["map"], g["figs"]["sep"]
    assert g["mode"] == "one", (tag, g["mode"], g["geo"])
    assert abs(m["xlen"] - m["ylen"]) <= 2, (tag, "map plot area not square", m["xlen"], m["ylen"])
    assert m["xlen"] >= 300, (tag, "map plot area too small", m["xlen"])                       # 369 / 340 px at the audit's 485 / 462 px panel, 413 at 529
    assert abs(m["div_w"] - g["geo"]["mapW"]) <= 1 and g["grid"]["cols"].startswith(str(g["geo"]["mapW"]) + "px"), (tag, m["div_w"], g["geo"], g["grid"])
    assert s["left"] >= m["right"] + LS.COL_GAP_PX - 1 and abs(s["top"] - m["top"]) <= 1, (tag, "separation not beside the map", m, s)   # same row, to the right
    assert s["ylen"] >= 200, (tag, "separation plot area < 200 px", s["ylen"])
    assert g["scroll"] == 0 and s["top"] >= 0 and s["bottom"] <= g["H"] + 1 and m["bottom"] <= g["H"] + 1, (tag, "first row not wholly visible", s, m, g["H"])
    assert abs(m["div_h"] - (g["panel"] - LS.HEADER_PX)) <= 1 and abs(s["div_h"] - (g["panel"] - LS.HEADER_PX)) <= 1, (tag, m["div_h"], s["div_h"], g["panel"])
    for k in ("err", "vel"):
        assert g["figs"][k]["top"] >= m["bottom"] - 1 and abs(g["figs"][k]["div_w"] - g["grid"]["w"]) <= 1, (tag, k, "not full-width below the first row", g["figs"][k])
    assert m["showlegend"] is False and s["showlegend"] is False, (tag, "a key row is still on in mode one", m["showlegend"], s["showlegend"])
    assert m["margin"]["t"] == LS.map_top_nolegend_px() == 28, (tag, m["margin"])
    return {"map_plot": (m["xlen"], m["ylen"]), "mapW": m["div_w"], "sep_plot": (s["xlen"], s["ylen"]), "sep_div_w": s["div_w"], "panel": g["panel"], "grid_w": g["grid"]["w"], "mpp": g["mpp"]}


def _check_icons(g: dict, tag: str) -> dict:
    """(3): the rendered head edge follows the plot-area rule."""
    want = _want_icon(float(g["scale"]), float(g["figs"]["map"]["ylen"]))
    assert g["icon"]["want"] == want, (tag, "iconPx() rule", g["icon"]["want"], want, g["scale"], g["figs"]["map"]["ylen"])
    seen = {}
    for k in ("tgt", "itc"):
        st, im = g["heads"].get(k), g["icon"].get(k)
        if st and st["visible"] and im and not im["hidden"]:
            assert abs(im["w"] - want) <= 1 and abs(im["h"] - want) <= 1, (tag, k, "icon edge", im, want)
            seen[k] = im["w"]
    return {"want": want, "seen": seen}


def _wait_clock(b: Browser, hms: str, timeout: float = 75.0) -> str | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = _page_clock(b)
        if c is not None and _hms_s(c) >= _hms_s(hms):
            return c
        time.sleep(1.0)
    return _page_clock(b)


@pytest.mark.parametrize("text", ["Normal", "Large"])
def test_laptop_two_up_row_width_pin_icons_and_no_clipping(app, text):
    b = Browser(1366, 768, f"laptop_{text}")
    log: dict = {"text": text}
    try:
        b.open_live(app, flight=1, t=T0, play=1)
        _sidebar_radio(b, "Text size", text)        # ALWAYS set it: the default scale follows the screen preset (Desktop 1080p -> Large)
        b.wait_panel(); b.wait_made(); time.sleep(3.0)
        g0 = b.pjs(GEOM_JS)
        log["scale"] = g0["scale"]
        assert abs(float(g0["scale"]) - {"Normal": 1.0, "Large": 1.15}[text]) < 1e-6, (text, g0["scale"])
        log["two_up"] = _check_two_up(g0, "t0")
        log["widths_t0"] = _check_widths(g0, "t0")
        log["icons_t0"] = _check_icons(g0, "t0")
        b.shot("t0")
        # figure div sizes constant during 20 s of play (100 ms sampler), icons re-checked every 5 s
        b.pjs(SIZE_SAMPLER_START_JS, SAMPLE_MS)
        t_s = time.time()
        while time.time() - t_s < SAMPLE_S:
            time.sleep(5.0)
            gi = b.pjs(GEOM_JS)
            _check_icons(gi, f"play+{time.time() - t_s:.0f}s")
            _check_widths(gi, f"play+{time.time() - t_s:.0f}s")
        rows = b.pjs(SIZE_SAMPLER_STOP_JS)
        sizes = {k: sorted({tuple(r[k]) for r in rows if k in r}) for k in ("map", "sep", "err", "vel")}
        log["sampler"] = {"n": len(rows), "sizes": sizes}
        assert len(rows) >= SAMPLE_S * 1000 / SAMPLE_MS * 0.7, ("sampler starved", len(rows))
        for k, v in sizes.items():
            assert len(v) == 1, (k, "figure div size changed during play", v)
        # at the CPA moment: the engagement box fills the square plot area, both heads visible
        clock = _wait_clock(b, T_CPA)
        gc = b.pjs(GEOM_JS)
        log["cpa"] = {"clock": clock, "fill": _fill(gc), "frame": gc["frame"], "range": gc["range"], "mpp": gc["mpp"], "heads": {k: (h and h["visible"]) for k, h in gc["heads"].items()}, "userView": gc["userView"]}
        b.shot("cpa")
        assert clock is not None and _hms_s(clock) >= _hms_s(T_CPA), ("replay did not reach the CPA moment", clock)
        assert gc["userView"] is False and gc["frame"] and gc["frame"]["mode"] == "engage", gc["frame"]
        assert _fill(gc) >= 0.85, ("engagement box does not fill the map", log["cpa"])
        assert all(h and h["visible"] for h in gc["heads"].values()), ("a vehicle head is off the map at the CPA", gc["heads"])
        _check_icons(gc, "cpa"); _check_two_up(gc, "cpa")
        # 0 panel clipping findings (the clip_audit rules, in place)
        findings = b.pjs(PANEL_JS)
        log["clip"] = findings
        assert findings == [], ("panel clipping findings", findings[:8])
        # (1) window resizes 1366 -> 1536 -> 1366: widths follow, view 1:1
        log["resize"] = {}
        for (w, h) in ((1536, 864), (1366, 768)):
            b.drv.set_window_size(w, h); time.sleep(2.0)
            gr = b.pjs(GEOM_JS)
            log["resize"][f"{w}x{h}"] = {"widths": _check_widths(gr, f"{w}x{h}"), "grid_w": gr["grid"]["w"], "mode": gr["mode"], "fill": _fill(gr), "aspect_err": _aspect_err(gr)}
            _check_two_up(gr, f"{w}x{h}")
        assert log["resize"]["1536x864"]["grid_w"] > log["resize"]["1366x768"]["grid_w"] + 100, log["resize"]   # the wider window really widened the grid
        # (1) sidebar collapse: the iframe grows ~360 px; every figure follows within the debounce + Streamlit's ~300 ms animation
        sb0 = b.js(SIDEBAR_W_JS)
        hit = b.js(COLLAPSE_JS)
        log["collapse"] = {"control_found": bool(hit), "sidebar_w_before": sb0}
        if hit:
            time.sleep(2.5)
            sb1 = b.js(SIDEBAR_W_JS)
            gcol = b.pjs(GEOM_JS)
            log["collapse"].update({"sidebar_w_after": sb1, "grid_w": gcol["grid"]["w"], "mode": gcol["mode"], "widths": _check_widths(gcol, "collapsed"), "fill": _fill(gcol), "aspect_err": _aspect_err(gcol),
                                    "dead_px": max(f["div_w"] - f["fl_w"] for f in gcol["figs"].values())})
            assert sb1 < sb0 - 200 and gcol["grid"]["w"] > log["resize"]["1366x768"]["grid_w"] + 200, ("the sidebar did not collapse / the grid did not widen", log["collapse"])
            if gcol["mode"] == "one":
                _check_two_up(gcol, "collapsed")
            b.shot("collapsed")
        assert_no_console_errors(b)
    finally:
        _dump(b, "laptop_log", log)
        b.quit()
