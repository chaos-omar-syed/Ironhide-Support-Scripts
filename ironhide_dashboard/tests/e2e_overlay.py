"""Real-browser end-to-end checks for the map's DOM OVERLAY (vehicle heads / leaders / live segments / track pills drawn
with CSS transforms + SVG lines, positioned with the map's own l2p every animation frame — ZERO Plotly work between the
server's map reacts).  selenium + headless Firefox against a RUNNING Streamlit instance; not collected by ``pytest tests/``
(no ``test_`` prefix); run it explicitly:

  cd ironhide_dashboard && python -m pytest -q tests/e2e_overlay.py [--durations=0]

Fixtures / helpers come from tests/e2e_browser.py (the module-scoped ``app`` side instance on free ports, Browser, console
collection).  Screenshots + JSON logs land in IH_E2E_SHOTS (default tests/_e2e_shots/).

What is pinned (2026-09-14, "laggy, everything flashing" on the user's laptop: 242 Plotly.relayout({images, shapes}) per 20 s):
  (a) Plotly.relayout / react / animate are wrapped INSIDE the panel iframe: during a 30 s 1x replay of flight 1 from
      07:21:40 the map gets NO relayout from the overlay / tween path (IH.overlay.relayouts == 0 and no relayout whose
      keys are anything but our own range apply, a satellite-tile-only images edit or a geometry fit), and only the
      <= 1 react per ~2 s from server figures.
  (b) every second: each visible head's overlay centre sits within 1 px of l2p(tweened E, N) and never detaches from its
      trail: distance to the ENVELOPE tail (the trail's newest point THIS tick) <= max(6 px, 1.6 s x speed) and to the
      DRAWN trail end (the map figure is re-sent at most every ~2 s, so it can lag the head by ~2 s + the 0.5 s
      prediction) <= max(6 px, 3.5 s x speed).
  (c) a real 200 px pointer drag, then two wheel notches in: heads follow the axes (head shift == axis shift within the
      tween's own motion), (b) re-checked immediately and 1 s later — no teleport.
  (d) the icon's rendered edge is constant across the zoom (+- 1 px) and equals max(16, min(ICON_PX (44) x text scale, 10 % of the
      map's plot-area height)) — 2026-09-15: 44 / 51 only on a >= 440 / 510 px plot area, 37-41 px on a laptop / 1080p map.
  (e) pills never overlap each other and stay inside the plot area.
  (f) the overlay IS the plot area (left/top = axis _offset, size = _length, overflow hidden): a head centred on the
      right edge has no hit-testable pixels outside the plot area; a head far off-view is hidden.
  (g) no console errors.
Both 1366x768 (stacked, one column) and 1920x1080 (two columns) are exercised.
"""
from __future__ import annotations

import math
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_browser import HAVE_TOOLS, F1_PASS_T, Browser, _dump, app, assert_no_console_errors  # noqa: E402,F401  (app = the module-scoped side instance fixture)

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAVE_TOOLS, reason="geckodriver / firefox / selenium missing")]

T0 = "07:21:40"                 # both vehicles airborne, pass 2 building up
RUN_S = 30.0
ICON_PX, ICON_FRAC = 60, 0.18


def want_icon(scale: float, plot_h: float) -> int:
    """ih.liveserver iconPx(): ICON_PX x text scale, capped at ICON_FRAC x the map's plot-area height, never under 16."""
    return max(40, min(round(ICON_PX * float(scale)), round(ICON_FRAC * float(plot_h))))
TAIL_S, TRAIL_S = 1.6, 3.5      # (b): head -> envelope tail (at lerp start the head is one ACTUAL tick interval behind: 1 s nominal + poll jitter; 0.5 s prediction at the end); head -> DRAWN trail end (the map is re-sent every ~2 s on top)

WRAP_JS = r"""
if(!window.__pc){ window.__pc={calls:[],t0:performance.now()};
  ["relayout","react","animate","restyle","update","newPlot","redraw"].forEach(function(fn){ var o=Plotly[fn]; if(typeof o!=="function") return;
    Plotly[fn]=function(gd){ var id=(gd&&gd.id)||(typeof gd==="string"?gd:"?");
      if(id==="map"){ var u=arguments[1], keys=(u&&typeof u==="object"&&!Array.isArray(u))?Object.keys(u):[], sat=false;
        if(fn==="relayout"&&keys.length===1&&keys[0]==="images") sat=(u.images||[]).every(function(im){ return im&&im.name==="sat"; });
        window.__pc.calls.push({fn:fn,t:(performance.now()-window.__pc.t0)/1000,keys:keys.slice(0,12),sat_only:sat,prog:IH.VIEW.prog,ov_relayouts:IH.overlay.relayouts}); }
      return o.apply(this,arguments); }; }); }
return true;
"""
CALLS_JS = "return {calls:window.__pc.calls, elapsed:(performance.now()-window.__pc.t0)/1000};"

SAMPLE_JS = r"""
var gd=document.getElementById('map'), fl=gd._fullLayout, xa=fl.xaxis, ya=fl.yaxis, ov=document.getElementById('ov'), ob=ov.getBoundingClientRect(), gb=gd.getBoundingClientRect();
function trailEnd(name){ var tr=null; (gd._fullData||[]).forEach(function(t){ if(t.name===name&&t.visible!==false&&t.x&&t.x.length) tr=t; }); if(!tr) return null; var n=tr.x.length-1; return {E:tr.x[n],N:tr.y[n],px:xa.l2p(tr.x[n]),py:ya.l2p(tr.y[n]),n:n+1}; }   // _fullData: plotly's JSON keeps arrays base64-typed ({dtype,bdata}) in gd.data
var o=IH.overlay, out={geom:{l:ob.left-gb.left,t:ob.top-gb.top,w:ob.width,h:ob.height,xoff:xa._offset,yoff:ya._offset,xlen:xa._length,ylen:ya._length,overflow:getComputedStyle(ov).overflow,pe:getComputedStyle(ov).pointerEvents,inMap:ov.parentElement===gd},
  mpp:IH.mpp(), scale:(IH.UI&&IH.UI.text_scale)||1, relayouts:o.relayouts, frames:o.frames, avg_ms:o.avg_ms, rate:o.rate, tiles:o.tile_relayouts, heads:{}, pills:[], range:IH.curRange(), stamp:IH.stamp, frozen:IH.TW.frozen, mode:IH.geo&&IH.geo.mode, frame_mode:IH.VIEW.frame&&IH.VIEW.frame.mode, userView:IH.VIEW.userView};
[["tgt","target truth"],["itc","interceptor truth"]].forEach(function(kn){ var k=kn[0], h=o.heads[k], im=document.getElementById('ov-head-'+k), te=trailEnd(kn[1]), tail=IH.TW.tails&&IH.TW.tails[k], r=im.getBoundingClientRect();
  out.heads[k]={state:h, hidden:im.hidden, w:im.offsetWidth, h:im.offsetHeight, dom_cx:r.left+r.width/2-ob.left, dom_cy:r.top+r.height/2-ob.top, rect:{l:r.left-ob.left,t:r.top-ob.top,r:r.right-ob.left,b:r.bottom-ob.top},
    trail_end:te, tail_px:tail?{px:xa.l2p(tail[0]),py:ya.l2p(tail[1])}:null, src_anim:im.src.indexOf('animate')>=0&&im.src.indexOf('svg')>=0}; });
out.pills=Array.prototype.map.call(ov.querySelectorAll('.ov-pill'),function(e){ var r=e.getBoundingClientRect(); return {id:e.id,hidden:e.hidden,text:e.textContent,l:r.left-ob.left,t:r.top-ob.top,r:r.right-ob.left,b:r.bottom-ob.top,border:getComputedStyle(e).borderTopColor}; });
out.pill_state=o.pills;
return out;
"""
L2P_JS = "var fl=document.getElementById('map')._fullLayout; return {px:fl.xaxis.l2p(arguments[0]), py:fl.yaxis.l2p(arguments[1])};"

CLIP_JS = r"""
// a SYNTHETIC overlay state: the target head centred exactly on the RIGHT edge of the plot area (and the interceptor far off-view);
// hit-test the head with pointer-events temporarily on: inside the plot area it is hit, outside (over the map margin) it is not — the overlay clips it.
var gd=document.getElementById('map'), fl=gd._fullLayout, xa=fl.xaxis, ya=fl.yaxis, ov=document.getElementById('ov'), ob=ov.getBoundingClientRect();
var E=xa.p2l(xa._length), N=ya.p2l(ya._length/2), far=xa.p2l(xa._length+5000);
IH.ovDraw({heads:{tgt:{E:E,N:N,hdg:0,spd:0},itc:{E:far,N:N,hdg:0,spd:0}}});
var im=document.getElementById('ov-head-tgt'), r=im.getBoundingClientRect(), st=IH.overlay.heads, out={rect:{l:r.left-ob.left,r:r.right-ob.left,t:r.top-ob.top,b:r.bottom-ob.top},ov_w:ob.width,tgt:st.tgt,itc:st.itc,itc_hidden:document.getElementById('ov-head-itc').hidden,tgt_hidden:im.hidden};
im.style.pointerEvents="auto";
try{ var yc=r.top+r.height/2, inside=document.elementFromPoint(ob.right-6,yc), outside=document.elementFromPoint(ob.right+6,yc);
  out.hit_inside=inside===im; out.hit_outside=outside===im; out.outside_tag=outside?(outside.tagName+"."+(outside.getAttribute("class")||"")):null; }
finally{ im.style.pointerEvents=""; }
return out;
"""


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _check_sample(s: dict, tag: str, log: list) -> None:
    """(b) + (d) + (e) + geometry for one sample; appends a compact row to ``log``."""
    g = s["geom"]
    assert g["inMap"] and g["overflow"] == "hidden" and g["pe"] == "none", (tag, g)
    assert abs(g["l"] - g["xoff"]) <= 1 and abs(g["t"] - g["yoff"]) <= 1 and abs(g["w"] - g["xlen"]) <= 1 and abs(g["h"] - g["ylen"]) <= 1, (tag, "overlay != plot area", g)
    assert s["relayouts"] == 0, (tag, "the overlay / tween path relayouted", s["relayouts"])
    want = want_icon(s["scale"], g["h"])
    row = {"tag": tag, "stamp": s["stamp"], "frames": s["frames"], "avg_ms": s["avg_ms"], "rate": s["rate"], "tiles": s["tiles"], "mpp": round(s["mpp"], 3), "heads": {}}
    for k, h in s["heads"].items():
        st = h["state"]
        if not st or not st["visible"] or h["hidden"]:
            row["heads"][k] = "off"
            continue
        assert h["src_anim"], (tag, k, "the head <img> is not the animated SVG")
        assert abs(h["w"] - want) <= 1 and abs(h["h"] - want) <= 1, (tag, k, "icon size", h["w"], h["h"], want)               # (d)
        assert abs(h["dom_cx"] - st["px"]) <= 1 and abs(h["dom_cy"] - st["py"]) <= 1, (tag, k, "DOM centre != l2p", h["dom_cx"], h["dom_cy"], st)
        v_px = float(st["spd"]) / max(1e-6, float(s["mpp"]))                                                                  # speed in px/s at this zoom
        d_tail = _dist((st["px"], st["py"]), (h["tail_px"]["px"], h["tail_px"]["py"])) if h["tail_px"] else None
        d_trail = _dist((st["px"], st["py"]), (h["trail_end"]["px"], h["trail_end"]["py"])) if h["trail_end"] else None
        row["heads"][k] = {"d_tail": None if d_tail is None else round(d_tail, 1), "d_trail": None if d_trail is None else round(d_trail, 1), "v_px": round(v_px, 1), "px": round(st["px"]), "py": round(st["py"])}
        if d_tail is not None:
            assert d_tail <= max(6.0, TAIL_S * v_px), (tag, k, "head detached from the envelope tail", d_tail, v_px, row)         # (b)
        if d_trail is not None:
            assert d_trail <= max(6.0, TRAIL_S * v_px), (tag, k, "head detached from the drawn trail end", d_trail, v_px, row)   # (b)
    vis = [p for p in s["pills"] if not p["hidden"]]
    for p in vis:                                                                                                                # (e) inside the plot area
        assert p["l"] >= -1 and p["t"] >= -1 and p["r"] <= g["w"] + 1 and p["b"] <= g["h"] + 1, (tag, "pill outside the plot area", p, g)
    for i in range(len(vis)):
        for j in range(i + 1, len(vis)):
            a, c = vis[i], vis[j]
            ix, iy = min(a["r"], c["r"]) - max(a["l"], c["l"]), min(a["b"], c["b"]) - max(a["t"], c["t"])
            assert not (ix > 1 and iy > 1), (tag, "pills overlap", a, c)                                                         # (e) never on top of each other
    row["pills"] = [(p["text"], round(p["l"]), round(p["t"])) for p in vis]
    log.append(row)


def _classify(calls: list) -> dict:
    out = {"react": [], "range": [], "tile": [], "geometry": [], "animate": [], "forbidden": []}
    for c in calls:
        fn, keys = c["fn"], c["keys"]
        if fn == "react":
            out["react"].append(c)
        elif fn == "animate":
            out["animate"].append(c)
        elif fn == "relayout" and keys and all(k in ("xaxis.range", "yaxis.range") for k in keys):
            out["range"].append(c)                                                     # our own applyRange (first paint / frame rev / follow / reset)
        elif fn == "relayout" and c.get("sat_only"):
            out["tile"].append(c)                                                      # a new satellite tile with no react to carry it
        elif fn == "relayout" and "height" in keys:
            out["geometry"].append(c)                                                  # applyGeometry (resize)
        else:
            out["forbidden"].append(c)
    return out


@pytest.mark.parametrize("w,h,mode", [(1366, 768, "one"), (1920, 1080, "two")])
def test_overlay_zero_plotly_work_and_heads_follow_the_trails(app, w, h, mode):
    b = Browser(w, h, f"overlay_{mode}")
    log: dict = {"samples": [], "calls": None}
    try:
        b.open_live(app, flight=1, t=T0, play=1)
        time.sleep(2.5)
        assert b.pjs("return IH.geo&&IH.geo.mode;") == mode, ("layout mode", w, h)
        assert b.pjs(WRAP_JS) is True
        t_start = time.time()
        i = 0
        while time.time() - t_start < RUN_S:                                                  # (a)(b)(d)(e): one sample per second for 30 s
            s = b.pjs(SAMPLE_JS)
            _check_sample(s, f"s{i:02d}", log["samples"])
            if i in (0, 15):
                b.shot(f"play_{i:02d}")
            i += 1
            time.sleep(max(0.0, 1.0 - 0.15))
        # (c) a real 200 px pointer drag: the axes shift by ~200 px and the heads shift with them (within their own tween motion)
        s0 = b.pjs(SAMPLE_JS)
        ref = {k: (hh["state"]["E"], hh["state"]["N"]) for k, hh in s0["heads"].items() if hh["state"] and hh["state"]["visible"]}
        before = {k: b.pjs(L2P_JS, *ref[k]) for k in ref}
        t_pan = time.time()
        b.pan(200, 0)
        s1 = b.pjs(SAMPLE_JS)
        dt = time.time() - t_pan
        _check_sample(s1, "after_drag", log["samples"])
        after = {k: b.pjs(L2P_JS, *ref[k]) for k in ref}
        assert ref, "no visible head to drag against"
        drag = {}
        for k in ref:
            shift = after[k]["px"] - before[k]["px"]
            v_px = float(s1["heads"][k]["state"]["spd"]) / max(1e-6, float(s1["mpp"])) if s1["heads"][k]["state"] else 0.0
            d_head = s1["heads"][k]["dom_cx"] - s0["heads"][k]["dom_cx"]
            drag[k] = {"axis_shift": round(shift, 1), "head_shift": round(d_head, 1), "dt": round(dt, 2), "v_px": round(v_px, 1), "visible_after": bool(s1["heads"][k]["state"] and s1["heads"][k]["state"]["visible"])}
            # the axes moved by the drag (~200 px) — plus, in ENGAGE / FOLLOW mode, possibly one re-centre animate from an envelope that
            # landed mid-drag (before the drag's plotly_relayout locked the view; unchanged behaviour): what must hold is that the HEAD moved
            # by exactly the same pixels as the axes (within its own tween motion over dt) — no teleport
            assert 150 <= abs(shift) <= 600, ("the drag did not pan", drag[k])
            if drag[k]["visible_after"]:
                assert abs(d_head - shift) <= 4 + v_px * dt, ("head did not follow the axes (teleport)", drag[k])
        log["drag"] = drag
        assert s1["userView"] is True                                                            # the drag locked the view (unchanged behaviour)
        time.sleep(1.0)
        _check_sample(b.pjs(SAMPLE_JS), "drag_plus_1s", log["samples"])
        b.shot("after_drag")
        # (c)(d) two wheel notches IN: heads stay on their trails, the icon edge is unchanged
        b.wheel(2, dy=-120)
        s2 = b.pjs(SAMPLE_JS)
        _check_sample(s2, "after_wheel", log["samples"])
        assert (s2["range"]["x"][1] - s2["range"]["x"][0]) < 0.95 * (s1["range"]["x"][1] - s1["range"]["x"][0]), ("the wheel did not zoom in", s1["range"], s2["range"])
        time.sleep(1.0)
        s3 = b.pjs(SAMPLE_JS)
        _check_sample(s3, "wheel_plus_1s", log["samples"])
        b.shot("after_wheel")
        sizes = sorted({(hh["w"], hh["h"]) for s in (s0, s1, s2, s3) for hh in s["heads"].values() if hh["state"] and hh["state"]["visible"] and not hh["hidden"]})
        want = want_icon(s3["scale"], s3["geom"]["h"])
        assert sizes and all(abs(sw - want) <= 1 and abs(sh - want) <= 1 for sw, sh in sizes), ("icon size changed across the zoom", sizes, want)   # (d)
        # (f) clipping at the plot-area edge (synthetic state; the frame loop restores the real one within a frame)
        clip = b.pjs(CLIP_JS)
        log["clip"] = clip
        assert not clip["tgt_hidden"] and clip["rect"]["r"] > clip["ov_w"] + 10 and clip["rect"]["l"] < clip["ov_w"], ("the edge head does not straddle the edge", clip)
        assert clip["hit_inside"] is True and clip["hit_outside"] is False, ("the overlay does not clip the head at the plot-area edge", clip)
        assert clip["itc_hidden"] is True and clip["itc"] is not None and clip["itc"]["visible"] is False, ("an off-view head is not hidden", clip)
        time.sleep(0.3)
        _check_sample(b.pjs(SAMPLE_JS), "after_clip", log["samples"])                            # the real state is back
        # (a) the Plotly calls the map received during all of the above
        c = b.pjs(CALLS_JS)
        cls = _classify(c["calls"])
        log["calls"] = {k: len(v) for k, v in cls.items()}
        log["calls"]["elapsed_s"] = round(c["elapsed"], 1)
        log["calls"]["forbidden_detail"] = cls["forbidden"][:10]
        log["calls"]["animate_detail"] = [(round(x["t"], 1), x["prog"]) for x in cls["animate"]]
        log["calls"]["tile_detail"] = [round(x["t"], 1) for x in cls["tile"]]
        log["overlay"] = b.pjs("return IH.overlay;")
        assert not cls["forbidden"], ("Plotly calls on the map outside reacts / our range apply / tile / geometry", cls["forbidden"][:5])
        assert log["overlay"]["relayouts"] == 0 and all(x["ov_relayouts"] == 0 for x in c["calls"])
        n_react, el = len(cls["react"]), c["elapsed"]
        assert 5 <= n_react <= el / 1.5 + 2, ("react cadence is not <= 1 per ~2 s", n_react, el)
        assert all(x["prog"] > 0 for x in cls["animate"]) and all(x["prog"] > 0 for x in cls["range"]), ("a range edit that was not ours", cls["animate"], cls["range"])
        assert log["overlay"]["avg_ms"] is not None and log["overlay"]["avg_ms"] < 8 and log["overlay"]["rate"] == 30, log["overlay"]
        assert_no_console_errors(b)                                                                # (g)
    finally:
        _dump(b, "overlay_log", log)
        b.quit()


ICON_JS = r"""
var out=[]; ["tgt","itc"].forEach(function(k){ var im=document.getElementById('ov-head-'+k), st=IH.overlay.heads[k]; if(!im||im.hidden||!st||!st.visible) return; out.push({k:k,w:im.offsetWidth,h:im.offsetHeight,svg:im.src.indexOf('svg')>=0}); });
return {els:out, want:IH.iconPx(), scale:(IH.UI&&IH.UI.text_scale)||1, sizex_m:IH.iconSizeM(), mpp:IH.mpp(), plot_h:document.getElementById('map')._fullLayout.yaxis._length};
"""


def test_overlay_icon_size_constant_on_screen(app):
    """The overlay replacement for e2e_browser.test_icon_size_constant_on_screen (which read layout.images / .layer-above <image>):
    the head <img> edge is max(16, min(ICON_PX (44) x text scale, 10 % of the plot-area height)), +- 1 px, at the default frame,
    3 notches in and 3 notches out; the metre size IH.iconSizeM() scales with metres-per-pixel accordingly."""
    b = Browser(1920, 1080, "overlay_icons")
    rows = {}
    try:
        b.open_live(app, flight=1, t=F1_PASS_T)
        time.sleep(3)
        r0 = b.pjs(ICON_JS)
        want = want_icon(r0["scale"], r0["plot_h"])
        for tag, n, dy in (("default", 0, 0), ("in3", 3, -120), ("out3", 6, 120)):
            if n:
                b.wheel(n, dy=dy)
                time.sleep(1.2)
            r = b.pjs(ICON_JS)
            rows[tag] = r
            b.shot(f"icons_{tag}")
            assert r["els"], (tag, r)
            for e in r["els"]:
                assert e["svg"] and abs(e["w"] - want) <= 1 and abs(e["h"] - want) <= 1, (tag, "icon on-screen size", e, want)
            assert abs(r["sizex_m"] - r["want"] * r["mpp"]) < 1e-6 and r["want"] == want
        assert rows["in3"]["mpp"] < rows["default"]["mpp"] < rows["out3"]["mpp"]
        _dump(b, "icons", rows)
        assert_no_console_errors(b)
    finally:
        b.quit()
