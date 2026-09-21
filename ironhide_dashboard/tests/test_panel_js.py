"""The panel JS state machine, executed for real in headless Firefox with the real plotly.js (no node
on this box): geometry(W) breakpoints, the VIEW LOCK (a user zoom survives 10 server map reacts),
the reset pill (re-applies the frame), FOLLOW hysteresis (re-centre only when a head is within 15 %
of the edge), the tween (interpolated heads drawn by the DOM overlay: l2p pixels, CSS transforms, SVG lines, stacked
pills — ZERO Plotly calls) and the measured overlay cost.
The page reports JSON to the harness server; skipped when Firefox is not installed.

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_panel_js.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

os.environ.setdefault("IH_LIVE_PORT", "8912")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffharness as FF  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import theme as T  # noqa: E402

pytestmark = pytest.mark.skipif(FF.FIREFOX is None, reason="firefox not installed")
L = LS.layout("Desktop 1080p")
STEPS = 10


def _fig(k: str, n: int = 0, with_range: bool = False) -> dict:
    """A minimal server-like figure: one scattergl line, our uirevision, map axes with equal aspect.
    Server map figures carry NO ranges (push strips them) unless the test asks for one."""
    x = [100 + 40 * i + n for i in range(12)]
    lay = {"uirevision": f"live-{k}", "height": L[k], "margin": {"l": 48, "r": 12, "t": 8, "b": 34}, "paper_bgcolor": T.CARD, "plot_bgcolor": T.CARD,
           "font": {"family": T.MONO, "size": L["font_px"], "color": T.INK2}, "showlegend": False, "images": [], "shapes": [],
           "xaxis": {"uirevision": f"live-{k}", "constrain": "range", "tickfont": {"size": L["font_px"]}, "title": {"text": "East of radar (m)", "font": {"size": L["font_px"]}}},
           "yaxis": {"uirevision": f"live-{k}", "constrain": "range", "tickfont": {"size": L["font_px"]}, "title": {"text": "North of radar (m)", "font": {"size": L["font_px"]}}}}
    if k == "map":
        lay["yaxis"].update(scaleanchor="x", scaleratio=1)
        lay["dragmode"] = "pan"
    if with_range:
        lay["xaxis"]["range"], lay["yaxis"]["range"] = [0, 1000], [0, 1000]
    return {"data": [{"type": "scattergl" if k == "map" else "scatter", "mode": "lines", "x": x, "y": [200 + (i % 3) * 30 + n for i in range(12)],
                      "line": {"color": T.TARGET, "width": 2.5}, "name": "target truth"}], "layout": lay}


def _env(stamp: int, *, view_rev: int = 0, mode: str = "fixed", heads=None, figs=None, x0=0, x1=1000, y0=0, y1=1000) -> dict:
    heads = heads if heads is not None else {"tgt": {"E": 500 + stamp, "N": 500, "U": 100, "spd": 20.0, "hdg": 90.0}, "itc": None}
    imgs = [{"source": "data:image/svg+xml;utf8,%3Csvg%3E", "xref": "x", "yref": "y", "x": heads["tgt"]["E"], "y": heads["tgt"]["N"], "sizex": 60, "sizey": 60,
             "xanchor": "center", "yanchor": "middle", "sizing": "contain", "layer": "above", "visible": True, "name": "tgt_icon"},
            {"source": "data:image/svg+xml;utf8,", "xref": "x", "yref": "y", "x": 0, "y": 0, "sizex": 60, "sizey": 60, "xanchor": "center", "yanchor": "middle",
             "sizing": "contain", "layer": "above", "visible": False, "name": "itc_icon"}]
    return {"stamp": stamp, "clock": "07:22:31", "t_now": 0.0, "frozen": False, "map_stamp": stamp, "sep_stamp": stamp, "err_stamp": stamp, "meas_stamp": 0,
            "heads": heads, "head_imgs": imgs, "tails": {"tgt": [heads["tgt"]["E"] - 20, heads["tgt"]["N"]], "itc": None},
            "view": {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "rev": view_rev, "mode": mode}, "ui": {"font_px": L["font_px"], "line_w": L["line_w"], "preset": L["preset"]},
            "more": [["Coverage · 60 s", "94 %", "fresh ≤1.5 s / 150 m", "ok"]], "figs": figs if figs is not None else {}}


HARNESS_JS = r"""
function rng(){ var fl=document.getElementById("map")._fullLayout; return [fl.xaxis.range.slice(), fl.yaxis.range.slice()]; }
function sleep(ms){ return new Promise(function(r){ setTimeout(r,ms); }); }
function eq(a,b,tol){ return Math.abs(a-b)<=(tol||0.5); }
var R={geometry:{}, steps:[], errors:[]};
window.onerror=function(m){ R.errors.push(String(m)); };
(async function(){
  try{
    [1000,1366,1600,1920,2400,3796].forEach(function(W){ var g=geometry(W); R.geometry[W]={mode:g.mode,map:g.map,mapW:g.mapW,right:g.right,sep:g.sep,err:g.err,vel:g.vel,velCols:g.velCols,col:g.col,font:g.font,lw:g.lw}; });
    R.one_laptop=(function(){ var P0=PANEL; PANEL=485; var g=geometry(945); PANEL=P0; return {map:g.map,mapW:g.mapW,right:g.right,sep:g.sep,col:g.col,MAP_T0:MAP_T0,m:mapMargin()}; })();   // the 1366x768 iframe (945 x 485)
    R.icon=(function(){ var out={}, sc0=UI.text_scale; [1,1.15].forEach(function(sc){ UI.text_scale=sc; out[sc]={px:iconPx(),plot_h:plotPx().h}; }); UI.text_scale=sc0; return out; })();   // pre-paint: plotPx() = the map div minus MAP_M
    // PRE-PAINT plotPx: nothing is drawn yet, so the plot area = the map div minus the map figure's REAL margins (MAP_M)
    var md=document.getElementById("map");
    R.prepaint={p:plotPx(), m:mapMargin(), cw:md.clientWidth, ch:md.clientHeight, gmap:geo.map, MAP_M:MAP_M};
    // the card-height budget follows the text scale (the header strips do): geometry() re-read at each scale
    R.scaled={}; var sc0=UI.text_scale;
    [1,1.15,1.3].forEach(function(sc){ UI.text_scale=sc; var one=geometry(1000), two=geometry(1920);
      R.scaled[sc]={hdr:hdrPx(),one_err:one.err,one_vel:one.vel,two_err:two.err,two_sep:two.sep,vel_strip:two.vel,err_min:errMinPx(),vel_stripPx:velStripPx()}; });
    UI.text_scale=sc0;
    // 1. first paint: the frame range from the envelope is applied
    await applyEnvelope(ENV0);
    var r0=rng(); R.first={x:r0[0],y:r0[1],userView:VIEW.userView,mode:document.getElementById("grid").className};
    // 2. the USER zooms: a real wheel event on the drag layer (scrollZoom) -> plotly zooms -> plotly_relayout -> lock
    var gd=document.getElementById("map"), drag=gd.querySelector(".nsewdrag"), bb=drag.getBoundingClientRect();
    R.hasRangeKeyBefore=("range" in ENVS[0].figs.map.layout.xaxis);   // the server payload carries no range key
    for(var w=0;w<3;w++){ drag.dispatchEvent(new WheelEvent("wheel",{deltaY:-120,deltaMode:0,clientX:bb.left+bb.width*0.6,clientY:bb.top+bb.height*0.5,bubbles:true,cancelable:true})); await sleep(60); }
    await sleep(150);
    var rz=rng(); R.afterZoom={x:rz[0],y:rz[1],userView:VIEW.userView,pill:!document.getElementById("pill").hidden};
    // 3. ten server ticks with NEW map figures (no ranges) + heads: the view must not move
    for(var i=1;i<=STEPS;i++){ await applyEnvelope(ENVS[i-1]); var r=rng(); R.steps.push({stamp:stamp,x:r[0],y:r[1]}); }
    R.locked={userView:VIEW.userView, reacted:have.map};
    // 4. reset pill: releases the lock and animates back to the frame — at 1:1 pixels (aspect-correct ranges)
    await resetView(); await sleep(900);
    R.reset={x:rng()[0],y:rng()[1],userView:VIEW.userView,pill:!document.getElementById("pill").hidden};
    var fl=gd._fullLayout, pw=fl.width-fl.margin.l-fl.margin.r, ph=fl.height-fl.margin.t-fl.margin.b, rr=rng();
    R.aspect={mpp_x:(rr[0][1]-rr[0][0])/pw, mpp_y:(rr[1][1]-rr[1][0])/ph, pw:pw, ph:ph};
    // 4b. double-click = OUR reset (config.doubleClick=false, so plotly never autoscales to the satellite extent): zoom, dblclick, back to the frame
    for(var w=0;w<3;w++){ drag.dispatchEvent(new WheelEvent("wheel",{deltaY:-120,deltaMode:0,clientX:bb.left+bb.width*0.6,clientY:bb.top+bb.height*0.5,bubbles:true,cancelable:true})); await sleep(60); }
    await sleep(150); var rz2=rng(); R.beforeDbl={x:rz2[0],y:rz2[1],userView:VIEW.userView};
    drag.dispatchEvent(new MouseEvent("dblclick",{bubbles:true,cancelable:true,clientX:bb.left+bb.width*0.5,clientY:bb.top+bb.height*0.5})); await sleep(900);
    R.dbl={x:rng()[0],y:rng()[1],userView:VIEW.userView,pill:!document.getElementById("pill").hidden};
    // 5. FOLLOW hysteresis: a head at the centre does nothing; a head within 15 % of the edge re-centres (animated).
    //    2026-09-15 adds a 10 s cooldown (VIEW.lastFrameAt): a SECOND re-centre inside that window is suppressed while the
    //    heads stay inside the view, and allowed again as soon as a head LEAVES it.  Each step below clears / keeps
    //    VIEW.lastFrameAt deliberately so the two behaviours are measured separately.
    VIEW.frame=Object.assign({},VIEW.frame,{mode:"follow"});
    VIEW.lastFrameAt=0;
    var c1=followCheck({tgt:{E:500,N:500,hdg:0,spd:0},itc:null}); await sleep(50); var rc=rng();
    var rx=rng()[0], eh=rx[1]-0.05*(rx[1]-rx[0]);                                                   // a head 5 % inside the (aspect-widened) right edge
    VIEW.lastFrameAt=0;                                                                             // the centre check above armed the cooldown: this is the FIRST re-frame
    var c2=followCheck({tgt:{E:eh,N:500,hdg:0,spd:0},itc:null}); await sleep(900); var re=rng();
    var rx2=rng()[0], w2=rx2[1]-rx2[0], eh2=rx2[1]-0.05*w2;                                         // still in the edge band and still INSIDE the view: suppressed for 10 s
    var c3=followCheck({tgt:{E:eh2,N:500,hdg:0,spd:0},itc:null}); await sleep(900); var r3=rng();
    var ehout=rx2[1]+0.5*w2;                                                                        // a head that has LEFT the view: re-frames despite the cooldown
    var c4=followCheck({tgt:{E:ehout,N:500,hdg:0,spd:0},itc:null}); await sleep(900); var r4=rng();
    R.follow={centre_recentred:c1, centre_range:rc[0], edge_recentred:c2, edge_range:re[0], edge_centre:(re[0][0]+re[0][1])/2, edge_head:eh,
              cooldown_recentred:c3, cooldown_range:r3[0], cooldown_head:eh2, cooldown_dt:performance.now()-VIEW.lastFrameAt,
              out_recentred:c4, out_range:r4[0], out_centre:(r4[0][0]+r4[0][1])/2, out_head:ehout};
    // 6. tween: two heads envelopes -> an interpolated position; the DOM OVERLAY places the head at l2p(E, N) with a CSS transform,
    //    draws the live segment + leader as SVG lines and stacks the pills — with ZERO Plotly calls; cost over 20 overlay updates
    await resetView(); await sleep(900);                  // back to the frame [0,1000]² so the tween head (E ~150, N 100) is inside the plot area
    TW.P=1000; takeHeads(Object.assign({},ENV0,{heads:{tgt:{E:100,N:100,U:0,spd:10,hdg:0},itc:null},tails:{tgt:[80,100],itc:null}}));
    await sleep(20);
    takeHeads(Object.assign({},ENV0,{heads:{tgt:{E:200,N:100,U:0,spd:10,hdg:90},itc:null},tails:{tgt:[80,100],itc:null}}));
    TW.cur.t=performance.now()-500;                       // half a period after the newest head: expect ~150, heading ~45
    var st=tweenState(performance.now());
    R.tween={x:st.heads.tgt.E,y:st.heads.tgt.N,hdg:st.heads.tgt.hdg,spd:st.heads.tgt.spd,itc:st.heads.itc};
    var calls={relayout:0,react:0,animate:0,restyle:0}; ["relayout","react","animate","restyle"].forEach(function(fn){ var o=Plotly[fn]; Plotly[fn]=function(){ calls[fn]++; return o.apply(this,arguments); }; });
    var costs=[]; var gd=document.getElementById("map");
    for(var j=0;j<20;j++){ var t0=performance.now(); ovDraw(st); costs.push(performance.now()-t0); }
    costs.sort(function(a,b){return a-b;}); R.overlay_ms={median:costs[10],max:costs[19]};
    var fl=gd._fullLayout, ov=document.getElementById("ov"), im=document.getElementById("ov-head-tgt"), ob=ov.getBoundingClientRect(), gb=gd.getBoundingClientRect(), ib=im.getBoundingClientRect();
    R.ovbox={left:ob.left-gb.left,top:ob.top-gb.top,w:ob.width,h:ob.height,xoff:fl.xaxis._offset,yoff:fl.yaxis._offset,xlen:fl.xaxis._length,ylen:fl.yaxis._length,
             overflow:getComputedStyle(ov).overflow,pe:getComputedStyle(ov).pointerEvents,inMap:ov.parentElement===gd,n_img:ov.querySelectorAll("img").length,n_line:ov.querySelectorAll("line").length,n_pill:ov.querySelectorAll(".ov-pill").length};
    R.head={cx:ib.left+ib.width/2-ob.left,cy:ib.top+ib.height/2-ob.top,px:fl.xaxis.l2p(st.heads.tgt.E),py:fl.yaxis.l2p(st.heads.tgt.N),w:im.offsetWidth,h:im.offsetHeight,hidden:im.hidden,
            transform:im.style.transform,src_svg:im.src.indexOf("svg")>=0&&im.src.indexOf("animate")>=0,want:IH.iconPx(),reported:IH.overlay.heads.tgt,itc_hidden:document.getElementById("ov-head-itc").hidden};
    var live=ov.querySelector('line[data-name="tgt_live"]'), lead=ov.querySelector('line[data-name="tgt_leader"]'), ilive=ov.querySelector('line[data-name="itc_live"]');
    R.lines={live:{x1:+live.getAttribute("x1"),y1:+live.getAttribute("y1"),x2:+live.getAttribute("x2"),y2:+live.getAttribute("y2"),vis:live.getAttribute("visibility"),tail_px:fl.xaxis.l2p(80),w:live.getAttribute("stroke-width")},
             leader:{vis:lead.getAttribute("visibility"),len:Math.hypot(+lead.getAttribute("x2")-+lead.getAttribute("x1"),+lead.getAttribute("y2")-+lead.getAttribute("y1")),dx:+lead.getAttribute("x2")-+lead.getAttribute("x1"),dy:+lead.getAttribute("y2")-+lead.getAttribute("y1")},
             itc_live_vis:ilive.getAttribute("visibility")};
    // pills: two annotation dicts (metres, plotly anchors / shifts) almost on top of each other -> stacked in pixel space (no overlap), inside the plot area, role-colour borders
    PILLS=[{name:"pill_tgt",xref:"x",yref:"y",x:st.heads.tgt.E,y:100,text:"#177",xanchor:"left",yanchor:"bottom",xshift:14,yshift:14,bordercolor:TGT_COLOR,bgcolor:CARD_COLOR,font:{size:13,color:INK_COLOR}},
           {name:"pill_itc",xref:"x",yref:"y",x:st.heads.tgt.E+2,y:101,text:"#203",xanchor:"left",yanchor:"bottom",xshift:14,yshift:14,bordercolor:ITC_COLOR,bgcolor:CARD_COLOR,font:{size:13,color:INK_COLOR}}];
    ovDraw(st);
    var pe=["tgt","itc"].map(function(k){ var e=document.getElementById("ov-pill-"+k), r=e.getBoundingClientRect(); return {l:r.left-ob.left,t:r.top-ob.top,w:r.width,h:r.height,text:e.textContent,border:getComputedStyle(e).borderTopColor,hidden:e.hidden}; });
    R.pills={state:IH.overlay.pills,els:pe,anchor:{px:fl.xaxis.l2p(st.heads.tgt.E),py:fl.yaxis.l2p(100)}};
    PILLS=[{name:"pill_tgt",xref:"x",yref:"y",x:-5000,y:100,text:"#177",xshift:14,yshift:14}]; ovDraw(st);   // anchor outside the axis range -> hidden (like a data-anchored plotly annotation)
    R.pill_offview={tgt_hidden:document.getElementById("ov-pill-tgt").hidden,itc_hidden:document.getElementById("ov-pill-itc").hidden,n:IH.overlay.pills.length};
    // the USER pans (wheel-zoom already locked the view above): the axes move, the overlay follows on plotly_relayout — head px minus trail-end px stays constant (no teleport)
    var before={hx:IH.overlay.heads.tgt.px,tx:fl.xaxis.l2p(80)};
    await Plotly.relayout(gd,{"xaxis.range":[fl.xaxis.range[0]-100,fl.xaxis.range[1]-100]}); await sleep(50); ovDraw(st);   // (plotly_relayout already re-placed it; redraw the SAME state so the tween's advance does not blur the comparison)
    var im2=im.getBoundingClientRect(), ob2=ov.getBoundingClientRect();
    R.pan={dhead:IH.overlay.heads.tgt.px-before.hx,dtail:gd._fullLayout.xaxis.l2p(80)-before.tx,dom_cx:im2.left+im2.width/2-ob2.left,px:IH.overlay.heads.tgt.px,frames:IH.overlay.frames};
    R.calls=calls; R.ov_relayouts=IH.overlay.relayouts; R.tile_relayouts=IH.overlay.tile_relayouts;
    R.after_tween_range=rng()[0];
    R.lerp={a45:lerpAng(350,10,0.5), shortest:lerpAng(0,270,0.5), q:quant5(53.1)};
    // the separation header's right group degrades instead of overflowing: squeeze its box and read what survives
    var rbox=document.querySelector(".cm .h .r"), sel=document.getElementById("st"), hel=document.getElementById("hud"), sep_h=document.querySelector(".cm .h");
    var h0=sep_h.getBoundingClientRect().height;
    function statSweep(cpa, widths){ var out=[];
      rbox.style.flex="0 0 auto";                                           // the sweep OWNS the box width (max-width alone is only a cap on what flex hands out)
      widths.forEach(function(w){ rbox.style.maxWidth=w+"px"; setHud(cpa); setStatus("ok","live","07:22:31");
        out.push({w:w, st:sel.textContent.trim(), hud:hel.textContent.trim(), over:rbox.scrollWidth-rbox.clientWidth, title:sel.title, hh:sep_h.getBoundingClientRect().height}); });
      rbox.style.flex=""; return out; }
    R.status=statSweep("CPA 59 m · 07:22:31",[900,220,150,96,40]);          // with a validated CPA in the same group
    var ws=[]; for(var w=200;w>=20;w-=6) ws.push(w);
    R.status_nocpa=statSweep("",ws);                                        // without one: every rung of the ladder is reachable
    rbox.style.maxWidth=""; rbox.style.flex=""; setHud(""); setStatus("ok","live","07:22:31"); R.status_h0=h0;
    R.status_col={r:rbox.getBoundingClientRect().width, st:sel.textContent.trim(), over:rbox.scrollWidth-rbox.clientWidth};   // and in the REAL two-column header: fits, degraded as needed

  }catch(e){ R.errors.push(String(e&&e.stack||e)); }
  fetch("/report?j="+encodeURIComponent(JSON.stringify(R)),{mode:"no-cors"}).catch(function(){});
})();
"""


def _page(env0: dict, envs: list[dict]) -> str:
    return (LS.panel_css(L) + LS.panel_body() + '<img src="/hold" width="1" height="1" alt="">'
            + '<script src="/plotly.min.js"></script><script>\n' + LS.panel_core_js(L)
            + f"\nvar ENV0={json.dumps(env0)};\nvar ENVS={json.dumps(envs)};\nvar STEPS={STEPS};\nBASE=''; GL=false; applyGeometry();\n" + HARNESS_JS + "</script>")


@pytest.fixture(scope="module")
def report():
    d = tempfile.mkdtemp(prefix="ihjs_")
    env0 = _env(1, figs={"map": _fig("map", 0), "sep": _fig("sep", 0), "err": _fig("err", 0)})
    envs = [_env(10 + i, figs={"map": _fig("map", i + 1)}) for i in range(STEPS)]
    with open(os.path.join(d, "h.html"), "w", encoding="utf-8") as f:
        f.write(_page(env0, envs))
    h = FF.Harness(d, hold_s=45.0)
    try:
        png = FF.shot(f"{h.base}/h.html", 1600, 900, os.path.join(d, "h.png"), timeout=120)
        assert h.got.wait(5.0), "the harness page never reported (Firefox did not run the panel JS)"
        r = h.reports[-1]
    finally:
        h.close()
    r["_png"] = png
    return r


def test_js_ran_without_errors(report):
    assert report.get("errors") == [], report.get("errors")


def test_geometry_breakpoints_match_the_server_rules(report):
    g = {int(k): v for k, v in report["geometry"].items()}
    one = LS.one_row(L["panel"], 1000, L["font_px"], 1.0)                                          # mode "one" = the two-up first row: map | separation, both panel − header; err / vel below
    assert g[1000]["mode"] == "one" and g[1000]["err"] == LS.ONE_ERR_PX and g[1000]["vel"] == LS.ONE_VEL_PX and g[1000]["velCols"] == 1
    assert (g[1000]["map"], g[1000]["mapW"], g[1000]["right"], g[1000]["sep"]) == (one["map"], one["mapW"], one["right"], one["sep"]) == (L["panel"] - L["header"], one["mapW"], one["right"], L["panel"] - L["header"])
    assert g[1000]["col"] == L["panel"] and g[1000]["mapW"] + g[1000]["right"] + LS.COL_GAP_PX == 1000
    lap, one_lap = report["one_laptop"], LS.one_row(485, 945, L["font_px"], 1.0)                     # the real laptop iframe: 945 x 485 -> a 369 px square plot area, 464 px map column, 469 px separation column
    assert (lap["map"], lap["mapW"], lap["right"], lap["sep"], lap["col"]) == (one_lap["map"], one_lap["mapW"], one_lap["right"], one_lap["sep"], 485) == (453, 464, 469, 453, 485)
    assert lap["MAP_T0"] == LS.map_top_nolegend_px() == 28 and lap["mapW"] - lap["m"]["l"] - lap["m"]["r"] == lap["map"] - lap["MAP_T0"] - lap["m"]["b"] == 369   # square
    vs = LS.vel_strip_px(L["panel"])
    for W in (1366, 1600, 1920):
        assert g[W]["mode"] == "two" and g[W]["velCols"] == 3 and g[W]["vel"] == vs, (W, g[W])   # MAP over the 3-across VELOCITY STRIP | sep over err
        left = (W - 12) * LS.SPLIT[0] // 100
        assert g[W]["map"] == min(L["panel"] - L["header"] - vs, left) and LS.SPLIT[0] == 62   # the MAP dominates (62 % of the width); no block header in this mode
        assert g[W]["col"] == g[W]["map"] + L["header"] + vs == 2 * L["header"] + g[W]["sep"] + g[W]["err"]   # right column fills the left one
        assert (g[W]["err"] - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - LS.ERR_HDR_PX >= LS.MIN_CARD_PX     # plot areas never below MIN_CARD_PX (53, under the 46 px header strips)
    assert g[1600]["map"] == L["panel"] - L["header"] - vs == 860 - LS.HEADER_PX - LS.vel_strip_px(860) and (g[1600]["sep"], g[1600]["err"]) == (L["sep"], L["err"])   # the preset's column split at 1600 (622 with 32 px headers)
    for W in (2400, 3796):
        assert g[W]["mode"] == "three" and g[W]["velCols"] == 1 and g[W]["err"] == g[W]["col"] - L["header"] and g[W]["lw"] == 3.0 and g[W]["font"] >= 15
        assert abs(g[W]["sep"] - round(LS.SEP_FRAC3 * (g[W]["col"] - 2 * L["header"]))) <= 1                # separation ~26 % of the middle column ...
        assert g[W]["sep"] + g[W]["vel"] + 2 * L["header"] == g[W]["col"]                                   # ... the velocity cards the rest
    assert g[3796]["font"] == 15 and g[1366]["font"] == 14 and g[1600]["font"] == L["font_px"] == 14 and g[1920]["font"] == 14   # base 14 x text scale 1


def test_first_paint_applies_the_frame_and_stays_two_column(report):
    """The frame [0,1000]² is applied on the first paint; the map div is wider than tall, so constrain="range" +
    scaleanchor widen x symmetrically about the frame centre (y stays exactly the frame)."""
    f = report["first"]
    assert abs(f["y"][0] - 0) < 0.5 and abs(f["y"][1] - 1000) < 0.5, f
    assert f["x"][0] <= 0 and f["x"][1] >= 1000 and abs((f["x"][0] + f["x"][1]) / 2 - 500) < 0.5, f
    assert f["userView"] is False and f["mode"] == "grid two"


def test_user_zoom_locks_the_view_and_ten_server_reacts_leave_it_untouched(report):
    z, f = report["afterZoom"], report["first"]
    assert z["userView"] is True and z["pill"] is True
    assert (z["x"][1] - z["x"][0]) < 0.9 * (f["x"][1] - f["x"][0]), (z, f)                   # the wheel really zoomed in
    assert len(report["steps"]) == STEPS and report["locked"]["userView"] is True
    assert report["hasRangeKeyBefore"] is False                                            # server map payloads carry no range key
    assert report["locked"]["reacted"] == str(10 + STEPS - 1)                              # the figures WERE applied (have.map advanced) ...
    for s in report["steps"]:
        assert abs(s["x"][0] - z["x"][0]) < 0.5 and abs(s["x"][1] - z["x"][1]) < 0.5, (s, z)   # ... and the view never moved


def test_reset_pill_reapplies_the_frame(report):
    """Reset -> the frame at 1:1 pixels: y exactly the frame (the plot is wider than tall), x centred on it and at least as wide."""
    r = report["reset"]
    assert r["userView"] is False and r["pill"] is False
    assert abs(r["y"][0] - 0) < 2 and abs(r["y"][1] - 1000) < 2 and abs((r["x"][0] + r["x"][1]) / 2 - 500) < 2 and r["x"][1] - r["x"][0] >= 1000 - 2, r


def test_reset_is_aspect_correct_and_double_click_resets(report):
    """After a reset the x and y metres-per-pixel agree within 1 % (the frame is applied as ranges consistent with 1:1
    pixels, so plotly never squashes the satellite tile); a double-click after a zoom is OUR reset: the view comes
    back to the default frame (y exactly the frame, x centred on it), not to plotly's autoscale."""
    a = report["aspect"]
    assert abs(a["mpp_x"] - a["mpp_y"]) / a["mpp_y"] < 0.01, a
    b, d = report["beforeDbl"], report["dbl"]
    f0 = report["first"]
    assert b["userView"] is True and (b["x"][1] - b["x"][0]) < 0.9 * (f0["x"][1] - f0["x"][0])  # zoomed in again (relative: the map is landscape now) ...
    assert d["userView"] is False and d["pill"] is False                                        # ... double-click released the lock ...
    assert abs(d["y"][0] - 0) < 2 and abs(d["y"][1] - 1000) < 2 and abs((d["x"][0] + d["x"][1]) / 2 - 500) < 2, d   # ... and re-applied the frame


def test_follow_hysteresis_recentres_only_near_the_edge(report):
    """FOLLOW re-frames only when a head is within 15 % of the view edge, and — since 2026-09-15 — at most once every
    10 s (VIEW.lastFrameAt) unless a head actually LEAVES the view: three animates in 20 s read as jitter."""
    f = report["follow"]
    assert f["centre_recentred"] is False and abs((f["centre_range"][0] + f["centre_range"][1]) / 2 - 500) < 2   # centre head: nothing happens
    assert f["edge_recentred"] is True and abs(f["edge_centre"] - f["edge_head"]) < 5, f      # head inside the 15 % edge band: view animates to it
    assert f["cooldown_dt"] < 10000, f                                                        # (the second / third checks are inside the 10 s window)
    assert f["cooldown_recentred"] is False and f["cooldown_range"] == f["edge_range"], f      # a head still in the edge band but INSIDE the view: suppressed, the view does not move
    assert f["out_recentred"] is True and abs(f["out_centre"] - f["out_head"]) < 5, f          # a head OUTSIDE the view: re-frames despite the cooldown


def _rgb(hex_: str) -> str:
    h = hex_.lstrip("#")
    return "rgb(%d, %d, %d)" % tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def test_tween_interpolates_heads_and_the_overlay_draws_them_without_plotly(report):
    """The tween state is data (E/N/hdg); the DOM overlay converts it to pixels with the map's own l2p and places the head <img>
    (one animated base SVG per role, rotated by CSS) exactly on that pixel, the live segment from the trail tail and the 5 s
    leader as SVG lines, the pills stacked in pixel space — with ZERO Plotly calls (the one relayout counted below is the
    harness's own pan), and cheaply (median < 5 ms for a full overlay update)."""
    t = report["tween"]
    assert 140 <= t["x"] <= 160 and abs(t["y"] - 100) < 0.5 and 40 <= t["hdg"] <= 50 and t["itc"] is None, t   # halfway between the two received heads, heading through the short arc
    assert report["calls"] == {"relayout": 1, "react": 0, "animate": 0, "restyle": 0}, report["calls"]      # 20 overlay updates + 2 pill passes + a pan: only the harness's own relayout
    assert report["ov_relayouts"] == 0 and report["tile_relayouts"] == 0
    assert report["overlay_ms"]["median"] < 5, report["overlay_ms"]                                        # DOM transforms: an order of magnitude under the old relayout
    ob = report["ovbox"]                                                                                   # the overlay IS the plot area (inside the map div, clipped, mouse-transparent)
    assert ob["inMap"] and ob["overflow"] == "hidden" and ob["pe"] == "none" and ob["n_img"] == 2 and ob["n_line"] == 4 and ob["n_pill"] == 2, ob
    assert abs(ob["left"] - ob["xoff"]) <= 1 and abs(ob["top"] - ob["yoff"]) <= 1 and abs(ob["w"] - ob["xlen"]) <= 1 and abs(ob["h"] - ob["ylen"]) <= 1, ob
    h = report["head"]                                                                                     # the head's DOM centre = l2p of the tweened E/N; edge = ICON_PX x text scale; rotated; animated SVG
    assert not h["hidden"] and h["itc_hidden"] and h["src_svg"] and abs(h["cx"] - h["px"]) <= 1 and abs(h["cy"] - h["py"]) <= 1, h
    assert h["w"] == h["h"] == h["want"] == 44 and "rotate(" in h["transform"] and "translate(" in h["transform"], h
    assert abs(h["reported"]["px"] - h["px"]) < 1e-6 and abs(h["reported"]["py"] - h["py"]) < 1e-6 and h["reported"]["size"] == 44 and h["reported"]["visible"] is True
    ln = report["lines"]                                                                                   # live segment: trail tail (E 80) -> head; leader: heading ~45°, 18..90 px
    assert ln["live"]["vis"] == "visible" and abs(ln["live"]["x1"] - ln["live"]["tail_px"]) <= 0.1 and abs(ln["live"]["x2"] - h["px"]) <= 0.1 and abs(ln["live"]["y2"] - h["py"]) <= 0.1 and ln["live"]["w"] == "2.5", ln
    assert ln["leader"]["vis"] == "visible" and 18 <= ln["leader"]["len"] <= 90 and ln["leader"]["dx"] > 0 and ln["leader"]["dy"] < 0 and ln["itc_live_vis"] == "hidden", ln   # NE on screen (y down)
    p = report["pills"]
    assert [x["text"] for x in p["state"]] == ["#177", "#203"] and [e["text"] for e in p["els"]] == ["#177", "#203"] and not any(e["hidden"] for e in p["els"]), p
    assert p["els"][0]["border"] == _rgb(T.TARGET) and p["els"][1]["border"] == _rgb(T.INTERCEPTOR), p["els"]
    a, b = p["els"]
    ix, iy = min(a["l"] + a["w"], b["l"] + b["w"]) - max(a["l"], b["l"]), min(a["t"] + a["h"], b["t"] + b["h"]) - max(a["t"], b["t"])
    assert not (ix > 0 and iy > 0), ("pills overlap", a, b)                                                # stacked, never on top of each other
    for e in (a, b):
        assert e["l"] >= -0.5 and e["t"] >= -0.5 and e["l"] + e["w"] <= ob["w"] + 0.5 and e["t"] + e["h"] <= ob["h"] + 0.5, ("pill outside the plot area", e, ob)
    assert abs(a["l"] - (p["anchor"]["px"] + 14)) <= 1 and abs(a["t"] + a["h"] - (p["anchor"]["py"] - 14)) <= 1, (a, p["anchor"])   # xshift 14 / yshift 14 like the annotation
    assert report["pill_offview"] == {"tgt_hidden": True, "itc_hidden": True, "n": 0}
    pan = report["pan"]                                                                                    # the axes moved 100 m: head and trail end moved by the SAME pixels, the DOM followed
    assert abs(pan["dhead"] - pan["dtail"]) <= 0.1 and abs(pan["dhead"]) > 20 and abs(pan["dom_cx"] - pan["px"]) <= 1, pan
    assert abs(report["after_tween_range"][0] - (report["reset"]["x"][0] - 100)) < 2                    # the overlay never touches the axes: only the harness's pan moved them
    lp = report["lerp"]
    assert abs(lp["a45"] - 360) < 1e-9 or abs(lp["a45"]) < 1e-9 or abs(lp["a45"] - 0) < 1e-9   # 350 -> 10 through north (shortest arc)
    assert abs(lp["shortest"] - (-45)) < 1e-9 or abs(lp["shortest"] - 315) < 1e-9
    assert lp["q"] == 55


# ── A15 viewport fit vs Streamlit re-applying the preset height ───────────────
OUTER_JS = r"""
function sleep(ms){ return new Promise(function(r){ setTimeout(r,ms); }); }
var R={errors:[]};
window.onerror=function(m){ R.errors.push(String(m)); };
(async function(){
  try{
    var f=document.getElementById("f");
    await sleep(1800);                                             // the inner panel JS ran applyGeometry() -> viewportPanel() -> setFrameHeight()
    R.innerHeight=window.innerHeight; R.top=Math.round(f.getBoundingClientRect().top);
    R.fitted={style:f.style.height, attr:f.getAttribute("height"), client:f.clientHeight};
    // Streamlit re-applies the preset height on a rerun WITHOUT remounting the (byte-identical) iframe: simulate it twice
    f.style.height="864px"; f.setAttribute("height","864"); await sleep(350);
    R.after1={style:f.style.height, attr:f.getAttribute("height"), client:f.clientHeight};
    f.setAttribute("height","864"); f.style.height="864px"; await sleep(350);
    R.after2={style:f.style.height, attr:f.getAttribute("height"), client:f.clientHeight};
    var ec=document.getElementById("ec"), nx=document.getElementById("next");
    R.wrapper={style:ec.style.height, client:ec.clientHeight, nextTop:Math.round(nx.getBoundingClientRect().top), frameBottom:Math.round(f.getBoundingClientRect().bottom)};
    ec.style.height=""; ec.className="stElementContainer element-container ec"; await sleep(350);   // Streamlit re-renders the wrapper (class height back to the preset)
    R.wrapper2={style:ec.style.height, client:ec.clientHeight, nextTop:Math.round(nx.getBoundingClientRect().top), frameBottom:Math.round(f.getBoundingClientRect().bottom)};
    R.chain=f.contentWindow.IH.frameChain(f).map(function(n){ return n.id||n.tagName; }); R.want=f.contentWindow.IH.want;
    f.contentWindow.IH.setFrameHeight(f, 600); await sleep(120);                                    // the SHRINK direction (a smaller viewport): the flex-basis must follow too
    R.shrunk={frame:f.clientHeight, wrapper:ec.clientHeight, nextTop:Math.round(nx.getBoundingClientRect().top), frameBottom:Math.round(f.getBoundingClientRect().bottom)};
    f.contentWindow.IH.setFrameHeight(f, R.want||f.contentWindow.IH.want); await sleep(120);
    R.innerPanel=f.contentWindow.IH.PANEL; R.want=R.want||f.contentWindow.IH.want;
  }catch(e){ R.errors.push(String(e&&e.stack||e)); }
  fetch("/report?j="+encodeURIComponent(JSON.stringify(R)),{mode:"no-cors"}).catch(function(){});
})();
"""


@pytest.fixture(scope="module")
def frame_report():
    """An OUTER page (the Streamlit page stand-in, 1600 x 1000) holding the panel iframe at the preset height 864; the inner
    panel JS fits the iframe to the parent viewport and must keep it there when the parent re-applies 864."""
    d = tempfile.mkdtemp(prefix="ihjsf_")
    inner = LS.panel_css(L) + LS.panel_body() + "<script>\n" + LS.panel_core_js(L) + "\nBASE=''; GL=false; applyGeometry();\n</script>"
    outer = ('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#101215}</style></head><body>'
             f'<style>.ec{{flex:0 0 {L["panel"] + 4}px;height:{L["panel"] + 4}px}}</style><div style="display:flex;flex-direction:column;gap:16px">'   # the real DOM: stVerticalBlock (flex column) > element container (flex item, basis = the preset)
             f'<div id="ec" data-testid="stElementContainer" class="stElementContainer element-container ec" height="{L["panel"] + 4}px">'
             f'<iframe id="f" src="inner.html" style="width:100%;height:{L["panel"] + 4}px;border:0;display:block" height="{L["panel"] + 4}"></iframe></div>'
             '<button id="next" style="display:block;height:44px;margin:0;flex:0 0 44px">MEASUREMENT SPACE</button></div>'
             '<img src="/hold" width="1" height="1" alt="" style="position:absolute;left:-9px"><script>' + OUTER_JS + "</script></body></html>")
    with open(os.path.join(d, "inner.html"), "w", encoding="utf-8") as f:
        f.write(inner)
    with open(os.path.join(d, "outer.html"), "w", encoding="utf-8") as f:
        f.write(outer)
    h = FF.Harness(d, hold_s=30.0)
    try:
        FF.shot(f"{h.base}/outer.html", 1600, 1000, os.path.join(d, "outer.png"), timeout=90)   # 1000 px: avail 980 is > 24 px from the 860 preset (viewportPanel hysteresis), so the fit engages
        assert h.got.wait(5.0), "the outer harness page never reported"
        r = h.reports[-1]
    finally:
        h.close()
    return r


def test_viewport_fit_survives_streamlit_reapplying_the_preset_height(frame_report):
    """Regression (user: 'the measurement-space strip sits in the middle of the screen'): the srcdoc is byte-identical so the
    iframe never remounts, but Streamlit re-applies the preset height style on every rerun; setFrameHeight() asserts style +
    attribute and re-asserts through a MutationObserver, so the panel stays viewport-fitted (parent innerHeight - top - 20 + 4)."""
    r = frame_report
    assert not r["errors"], r["errors"]
    want = int(r["innerHeight"]) - int(r["top"]) - 20 + 4                         # viewportPanel(): avail = floor(ph - top - 20); iframe = PANEL + 4
    assert abs(int(r["want"]) - want) <= 2 and int(r["innerPanel"]) == int(r["want"]) - 4, r
    assert r["fitted"]["style"] == f'{r["want"]}px' and r["fitted"]["attr"] == str(r["want"]) and abs(int(r["fitted"]["client"]) - int(r["want"])) <= 2, r["fitted"]
    for key in ("after1", "after2"):                                              # the re-applied 864 px is undone within a frame, twice
        a = r[key]
        assert a["style"] == f'{r["want"]}px' and a["attr"] == str(r["want"]) and abs(int(a["client"]) - int(r["want"])) <= 2, (key, a)
    assert int(r["want"]) != L["panel"] + 4 and int(r["innerPanel"]) == 980         # i.e. the fit really changed the height (1000 px window vs the 860 preset)
    # item 2 (user: "MEASUREMENT SPACE strip covering the view"): Streamlit's element container keeps the PRESET height (emotion class + height
    # attribute) while the iframe grows -> the iframe overflowed it and the next elements overlaid the panel.  setFrameHeight sizes the whole
    # wrapper chain (iframe -> element container) and re-asserts it when the wrapper is re-rendered.
    assert r["chain"] == ["f", "ec"], r["chain"]
    for key in ("wrapper", "wrapper2"):
        w = r[key]
        assert w["style"] == f'{r["want"]}px' and abs(int(w["client"]) - int(r["want"])) <= 2, (key, w)
        assert w["nextTop"] >= w["frameBottom"], (key, w)                            # the next element starts at / below the iframe's bottom
    sh = r["shrunk"]                                                                  # and the wrapper SHRINKS with the iframe (flex-basis pinned), no dead band
    assert abs(int(sh["frame"]) - 600) <= 2 and abs(int(sh["wrapper"]) - 600) <= 2 and sh["nextTop"] >= sh["frameBottom"] and sh["nextTop"] - sh["frameBottom"] <= 20, sh


def test_prepaint_plot_area_uses_the_real_map_margins(report):
    """plotPx() before the first paint: the map div minus the map figure's FIXED margins (ih.plots.map_margin,
    baked in as MAP_M) — the old fallback assumed {l 64, r 12, t 8, b 44} and mis-estimated the first frame's
    aspect (the map's real margins are 83 / 12 / 62 / 56 at the default font, 106 / 12 / 69 / 70 at X-Large)."""
    pp = report["prepaint"]
    assert pp["MAP_M"] == LS.map_margin_px(L["font_px"], L.get("text_scale", 1.0)) == pp["m"]
    assert pp["p"]["w"] == pp["cw"] - pp["m"]["l"] - pp["m"]["r"] and pp["p"]["h"] == pp["gmap"] - pp["m"]["t"] - pp["m"]["b"]
    assert pp["p"]["w"] > 200 and pp["p"]["h"] > 200 and pp["m"]["l"] >= 83   # a real plot area, not the stale 64 px guess


def test_card_budget_follows_the_text_scale(report):
    """The in-iframe geometry mirrors the server's err_hdr_px / one_err_px / one_vel_px / vel_strip_px from
    UI.text_scale, so the stacked cards keep their MIN_CARD_PX plot areas at Large / X-Large."""
    sc = {float(k): v for k, v in report["scaled"].items()}
    for s_, g in sc.items():
        assert g["hdr"] == LS.err_hdr_px(s_) and g["err_min"] == LS.err_min_px(s_), (s_, g)
        assert g["one_err"] == LS.one_err_px(s_) and g["one_vel"] == LS.one_vel_px(s_), (s_, g)
        assert g["vel_strip"] == g["vel_stripPx"] == LS.vel_strip_px(L["panel"], s_), (s_, g)
        assert (g["two_sep"], g["two_err"]) == LS._fit_two(g["two_sep"] + g["two_err"] + 2 * L["header"], s_), (s_, g)
        assert (g["two_err"] - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - g["hdr"] >= LS.MIN_CARD_PX, (s_, g)
    assert sc[1.3]["one_err"] > sc[1.15]["one_err"] > sc[1.0]["one_err"] and sc[1.3]["one_vel"] > sc[1.0]["one_vel"]


def test_separation_header_status_degrades_and_never_overflows(report):
    """fitStatus(): the right group drops the clock's seconds, then the clock, then the CPA's time, then the words
    (the status icon's colour still carries the state) — measuring its box after each step.  At every width the
    group FITS, the header height never changes, and the full text stays on the element's title."""
    order = ["live · 07:22:31", "live · 07:22", "live", ""]
    for key in ("status", "status_nocpa"):
        steps = report[key]
        assert len(steps) >= 5, (key, steps)
        for st in steps:
            assert st["over"] <= 1, (key, st)                              # never overflows, at any width
            assert abs(st["hh"] - report["status_h0"]) < 0.6, (key, st)    # ... and never changes the header's height
            assert st["title"] == "live · 07:22:31", (key, st)             # whatever is dropped stays on hover
            assert st["st"] in order, (key, st)
        idx = [order.index(st["st"]) for st in steps]
        assert idx == sorted(idx), (key, idx)                              # deterministic: monotone as the box narrows
        assert idx[0] == 0 and idx[-1] == len(order) - 1, (key, idx)       # widest: everything; narrowest: the icon alone
    assert sorted(set(st["st"] for st in report["status_nocpa"])) == sorted(order)   # every rung is reachable
    cpa = report["status"]
    assert cpa[0]["hud"] == "CPA 59 m · 07:22:31" and cpa[-1]["hud"] == ""           # the CPA's time goes before the words, the CPA last
    hud_order = ["CPA 59 m · 07:22:31", "CPA 59 m", ""]
    assert [hud_order.index(st["hud"]) for st in cpa] == sorted(hud_order.index(st["hud"]) for st in cpa)


def test_separation_header_fits_in_the_real_two_column_column(report):
    """The same header in the harness's real 1600 px two-column layout (the 1920x1080 case the clip audit flagged):
    the group fits, so the audit's "header-right-overflow" cannot come back."""
    c = report["status_col"]
    assert c["over"] <= 1, c
    assert c["st"] in ("live · 07:22:31", "live · 07:22", "live", ""), c


def test_icon_edge_follows_the_plot_area_height(report):
    """iconPx() = max(32, min(round(44 x scale), round(0.14 x plot-area height))): 44 / 51 px on a >= 315 / 365 px plot area, 14 % of a
    shorter one, never under 32 (2026-09-17: the 10 % cap made 27 px vehicles on a 1366x768 laptop map, unreadable on satellite)."""
    ic = {float(k): v for k, v in report["icon"].items()}
    for sc, r in ic.items():
        assert r["px"] == max(40, min(round(60 * sc), round(0.18 * r["plot_h"]))), (sc, r)
    assert ic[1.0]["plot_h"] == ic[1.15]["plot_h"] > 0
