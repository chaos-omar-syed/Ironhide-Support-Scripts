"""Clipping audit of every widget, in a real browser (Selenium + Firefox — the harness this repo already has).
For a matrix of window sizes x sidebar Text size, on the Live page (archive F1 at the pass): report
  parent page : any element whose text overflows its box (scrollWidth > clientWidth) under an overflow:hidden/clip ancestor,
                any element extending past the viewport, sidebar labels / buttons / tiles / status line included
  quad iframe : the MEASUREMENT SPACE quad (?meas=1) — annotations leaving their own subplot cell or the figure, sitting on
                a panel title strip or on each other, colliding x / y tick labels, the key over a plot area (MEAS_JS)
  panel iframe: plotly annotations whose box leaves its card rect or the figure, tick labels outside their figure or
                colliding with each other / the axis title, a legend over the plot area / under the modebar / outside
                the figure, header (.h) captions/right text overflowing
Writes tests/_e2e_shots/clip_audit_<W>x<H>_<text>.json + a screenshot; prints a compact table.  Usage:
  IH_E2E_URL=http://host:port python tests/clip_audit.py [--sizes=1366x768,1920x1080] [--texts=Normal,Large,X-Large] [--tag=before] [--no-ds]
  (without IH_E2E_URL a side instance is started)"""
import json, os, signal, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_browser as E

PAGE_JS = r"""
var out=[]; var vw=window.innerWidth, vh=window.innerHeight;
function clipAnc(el){ var n=el.parentElement; while(n&&n!==document.body){ var cs=getComputedStyle(n); if(/hidden|clip/.test(cs.overflow)||/hidden|clip/.test(cs.overflowX)) return n; n=n.parentElement; } return null; }
document.querySelectorAll('[data-testid="stSidebar"] *, [data-testid="stMainBlockContainer"] *').forEach(function(el){
  if(el.children.length>0 && !/^(P|SPAN|LABEL|BUTTON|B|SMALL|DIV)$/.test(el.tagName)) return;
  var r=el.getBoundingClientRect(); if(r.width<2||r.height<2) return;
  var txt=(el.textContent||'').trim(); if(!txt) return;
  var cs=getComputedStyle(el);
  if(el.scrollWidth>el.clientWidth+2 && cs.overflowX!=='visible') out.push({kind:'text-overflow', tag:el.tagName, text:txt.slice(0,60), by:el.scrollWidth-el.clientWidth});
  var anc=clipAnc(el); if(anc){ var a=anc.getBoundingClientRect(), ac=getComputedStyle(anc); var hx=/hidden|clip/.test(ac.overflowX), hy=/hidden|clip/.test(ac.overflowY);   // a scrollable axis is not clipping
    if(hx&&(r.right>a.right+1||r.left<a.left-1)) out.push({kind:'clipped-horizontally', tag:el.tagName, text:txt.slice(0,60), by:Math.round(Math.max(r.right-a.right, a.left-r.left))});
    if(hy&&(r.bottom>a.bottom+1||r.top<a.top-1)) out.push({kind:'clipped-vertically', tag:el.tagName, text:txt.slice(0,60), by:Math.round(Math.max(r.bottom-a.bottom, a.top-r.top))}); }
  if(r.right>vw+1) out.push({kind:'past-viewport', tag:el.tagName, text:txt.slice(0,60), by:Math.round(r.right-vw)});
});
// dedupe
var seen={}; return out.filter(function(o){ var k=o.kind+'|'+o.text; if(seen[k]) return false; seen[k]=1; return true; });
"""
PANEL_JS = r"""
var out=[]; var W=window.innerWidth, H=document.documentElement.scrollHeight;
['map','sep','err','vel'].forEach(function(id){ var gd=document.getElementById(id); if(!gd||!gd._fullLayout) return; var g=gd.getBoundingClientRect(), fl=gd._fullLayout, sz=fl._size;
  var cards=(gd.layout.shapes||[]).filter(function(s){ return s.name&&s.name.indexOf('card_')===0&&s.xref==='paper'; }).map(function(s){ return {n:s.name, x0:g.left+sz.l+sz.w*s.x0, x1:g.left+sz.l+sz.w*s.x1, y0:g.top+sz.t+sz.h*(1-s.y1), y1:g.top+sz.t+sz.h*(1-s.y0)}; });
  gd.querySelectorAll('.infolayer g.annotation').forEach(function(a){ var r=a.getBoundingClientRect(); var t=a.querySelector('text'); var txt=t?t.textContent.trim():''; if(!txt||txt.length<2) return;
    if(r.left<g.left-1||r.right>g.right+1||r.top<g.top-1||r.bottom>g.bottom+1) out.push({fig:id, kind:'annotation-outside-figure', text:txt.slice(0,60), by:Math.round(Math.max(g.left-r.left, r.right-g.right, g.top-r.top, r.bottom-g.bottom))});
    if(cards.length){ var inside=cards.some(function(c){ return r.left>=c.x0-2&&r.right<=c.x1+2&&r.top>=c.y0-2&&r.bottom<=c.y1+2; }); if(!inside) out.push({fig:id, kind:'annotation-outside-card', text:txt.slice(0,60)}); }
  });
  gd.querySelectorAll('.xtick text, .ytick text').forEach(function(t){ var r=t.getBoundingClientRect(); if(r.width&&(r.left<g.left-3||r.right>g.right+3||r.top<g.top-3||r.bottom>g.bottom+3)) out.push({fig:id, kind:'tick-outside-figure', text:t.textContent.slice(0,20), by:Math.round(Math.max(g.left-r.left, r.right-g.right, g.top-r.top, r.bottom-g.bottom))}); });
  // two y tick labels of the SAME axis printed on top of each other (plotly never drops colliding y labels: a short card at a big text scale)
  var byax={}; gd.querySelectorAll('.ytick text').forEach(function(t){ var k=(t.parentElement&&t.parentElement.getAttribute('class'))||'y'; (byax[k]=byax[k]||[]).push(t); });
  Object.keys(byax).forEach(function(k){ var ts=byax[k].map(function(t){ return {r:t.getBoundingClientRect(), s:t.textContent, f:parseFloat(getComputedStyle(t).fontSize)||12}; }).filter(function(o){ return o.r.height>0; });
    ts.sort(function(a,b){ return a.r.top-b.r.top; });
    // an SVG text box is ~1.33 x font tall but a digit's INK is only ~0.75 x font: two labels read apart while their boxes
    // already overlap, so the rule is centre-to-centre >= 0.9 x font (ink + a readable gap), verified against the pixels.
    for(var i=1;i<ts.length;i++){ var c0=(ts[i-1].r.top+ts[i-1].r.bottom)/2, c1=(ts[i].r.top+ts[i].r.bottom)/2, need=0.9*Math.max(ts[i-1].f, ts[i].f);
      if(c1-c0 < need) out.push({fig:id, kind:'ticks-collide', text:ts[i-1].s+' <> '+ts[i].s, by:Math.round(need-(c1-c0))}); } });
  // the axis TITLES must clear the tick labels (the map's margins are fixed: no automargin to save them)
  [['.g-ytitle','.ytick text'],['.g-xtitle','.xtick text']].forEach(function(pair){ var ti=gd.querySelector(pair[0]+' text'); if(!ti) return; var a=ti.getBoundingClientRect(); if(!a.width) return;
    gd.querySelectorAll(pair[1]).forEach(function(t){ var b=t.getBoundingClientRect(); var ox=Math.min(a.right,b.right)-Math.max(a.left,b.left), oy=Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top);
      if(ox>1&&oy>1) out.push({fig:id, kind:'axis-title-over-ticks', text:ti.textContent.slice(0,24)+' <> '+t.textContent, by:Math.round(Math.min(ox,oy))}); }); });
  // the LEGEND: never over the plot area (it would sit on the imagery / the data), never under the hover modebar, never outside the figure
  var leg=gd.querySelector('.legend'), mb=(gd.parentElement&&gd.parentElement.querySelector('.modebar'))||gd.querySelector('.modebar');
  if(leg){ var lr=leg.getBoundingClientRect(), pa={l:g.left+sz.l, r:g.left+sz.l+sz.w, t:g.top+sz.t, b:g.top+sz.t+sz.h};
    var ox=Math.min(lr.right,pa.r)-Math.max(lr.left,pa.l), oy=Math.min(lr.bottom,pa.b)-Math.max(lr.top,pa.t);
    if(ox>2&&oy>2) out.push({fig:id, kind:'legend-over-plot-area', text:'legend', by:Math.round(oy)});
    if(lr.left<g.left-1||lr.right>g.right+1||lr.top<g.top-1||lr.bottom>g.bottom+1) out.push({fig:id, kind:'legend-outside-figure', text:'legend', by:Math.round(Math.max(g.left-lr.left, lr.right-g.right, g.top-lr.top, lr.bottom-g.bottom))});
    if(mb){ var m=mb.getBoundingClientRect(), mx=Math.min(lr.right,m.right)-Math.max(lr.left,m.left), my=Math.min(lr.bottom,m.bottom)-Math.max(lr.top,m.top);
      if(mx>2&&my>2) out.push({fig:id, kind:'legend-under-modebar', text:'legend', by:Math.round(Math.min(mx,my))}); } }
  // annotations overlapping each other inside the same card (title vs readout collision)
  var anns=[].slice.call(gd.querySelectorAll('.infolayer g.annotation')).map(function(a){ var r=a.getBoundingClientRect(); var t=a.querySelector('text'); return {r:r, t:t?t.textContent.trim():''}; }).filter(function(a){ return a.t.length>1; });
  for(var i=0;i<anns.length;i++) for(var j=i+1;j<anns.length;j++){ var A=anns[i].r, B=anns[j].r; var ox=Math.min(A.right,B.right)-Math.max(A.left,B.left), oy=Math.min(A.bottom,B.bottom)-Math.max(A.top,B.top); if(ox>3&&oy>3) out.push({fig:id, kind:'annotations-overlap', text:anns[i].t.slice(0,30)+' <> '+anns[j].t.slice(0,30), by:Math.round(ox)}); }
});
document.querySelectorAll('.h').forEach(function(h){ var r=h.querySelector('.r'); if(r&&r.scrollWidth>r.clientWidth+2) out.push({fig:'header', kind:'header-right-overflow', text:(h.textContent||'').trim().slice(0,50), by:r.scrollWidth-r.clientWidth});
  if(h.scrollWidth>h.clientWidth+2) out.push({fig:'header', kind:'header-overflow', text:(h.textContent||'').trim().slice(0,50), by:h.scrollWidth-h.clientWidth}); });
var seen={}; return out.filter(function(o){ var k=o.kind+'|'+o.fig+'|'+o.text; if(seen[k]) return false; seen[k]=1; return true; });
"""

# ── the MEASUREMENT SPACE quad (its own iframe: id "meas", four subplots in ONE plotly div, no card rects, no modebar) ──
# Same rules as the panel figures, expressed per SUBPLOT: an in-panel annotation must stay inside its own subplot cell
# (its plot area plus the title strip above it), nothing may overlap anything else in the same cell, the ticks must read.
MEAS_JS = r"""
var out=[]; var gd=document.getElementById('meas');
if(!gd||!gd._fullLayout) return [{fig:'meas', kind:'quad-not-drawn', text:'no _fullLayout'}];
var g=gd.getBoundingClientRect(), fl=gd._fullLayout, sz=fl._size;
function ax(ref,kind){ var n=String(ref||(kind==='x'?'x':'y')); var i=n.replace(/^[xy]/,'').replace(/ .*$/,''); return fl[(kind==='x'?'xaxis':'yaxis')+i]||null; }
function cell(xa,ya){ return {x0:g.left+sz.l+sz.w*xa.domain[0], x1:g.left+sz.l+sz.w*xa.domain[1],
                              y0:g.top+sz.t+sz.h*(1-ya.domain[1]), y1:g.top+sz.t+sz.h*(1-ya.domain[0])}; }
var specs=fl.annotations||[];
var recs=[].slice.call(gd.querySelectorAll('.infolayer g.annotation')).map(function(n){
    var i=parseInt(n.getAttribute('data-index'),10); var t=n.querySelector('text');
    return {i:i, r:n.getBoundingClientRect(), txt:t?t.textContent.trim():'', sp:(isNaN(i)?null:specs[i])||{}}; })
  .filter(function(a){ return a.r.width>1 && a.r.height>1 && a.txt.length>1; });
recs.forEach(function(a){
  var xa=/^x/.test(String(a.sp.xref||''))?ax(a.sp.xref,'x'):null, ya=/^y/.test(String(a.sp.yref||''))?ax(a.sp.yref,'y'):null;
  a.key=(xa&&ya)?String(xa._id||'x')+'/'+String(ya._id||'y'):'paper';
  a.cell=(xa&&ya)?cell(xa,ya):null;
  a.title=/^title_/.test(String(a.sp.name||''));
  a.nm=String(a.sp.name||'');
});
var strip={}; recs.forEach(function(a){ if(a.title&&a.cell) strip[a.key]=a.r; });   // the title strip of a cell = its own title box (titles sit ABOVE the plot area)
recs.forEach(function(a){
  if(a.r.left<g.left-1||a.r.right>g.right+1||a.r.top<g.top-1||a.r.bottom>g.bottom+1)
    out.push({fig:'meas', kind:'annotation-outside-figure', name:a.nm, text:a.txt.slice(0,60), by:Math.round(Math.max(g.left-a.r.left, a.r.right-g.right, g.top-a.r.top, a.r.bottom-g.bottom))});
  if(a.cell){ var st=strip[a.key], top=st?Math.min(a.cell.y0, st.top):a.cell.y0;
    var by=Math.max(a.cell.x0-a.r.left, a.r.right-a.cell.x1, top-a.r.top, a.r.bottom-a.cell.y1);
    if(by>2) out.push({fig:'meas', kind:'annotation-outside-panel', name:a.nm, text:a.txt.slice(0,60), by:Math.round(by)}); }
});
for(var i=0;i<recs.length;i++) for(var j=i+1;j<recs.length;j++){
  var A=recs[i], B=recs[j];
  if(A.key!==B.key && A.key!=='paper' && B.key!=='paper') continue;                 // different subplots never read as one label
  var ox=Math.min(A.r.right,B.r.right)-Math.max(A.r.left,B.r.left), oy=Math.min(A.r.bottom,B.r.bottom)-Math.max(A.r.top,B.r.top);
  if(ox>3&&oy>3) out.push({fig:'meas', kind:(A.title||B.title)?'annotation-over-title':'annotations-overlap', text:A.txt.slice(0,30)+' <> '+B.txt.slice(0,30), by:Math.round(Math.min(ox,oy))});
}
gd.querySelectorAll('.xtick text, .ytick text').forEach(function(t){ var r=t.getBoundingClientRect();
  if(r.width&&(r.left<g.left-3||r.right>g.right+3||r.top<g.top-3||r.bottom>g.bottom+3))
    out.push({fig:'meas', kind:'tick-outside-figure', text:t.textContent.slice(0,20), by:Math.round(Math.max(g.left-r.left, r.right-g.right, g.top-r.top, r.bottom-g.bottom))}); });
var byax={}; gd.querySelectorAll('.ytick text').forEach(function(t){ var k=(t.parentElement&&t.parentElement.getAttribute('class'))||'y'; (byax[k]=byax[k]||[]).push(t); });
Object.keys(byax).forEach(function(k){ var ts=byax[k].map(function(t){ return {r:t.getBoundingClientRect(), s:t.textContent, f:parseFloat(getComputedStyle(t).fontSize)||12}; }).filter(function(o){ return o.r.height>0; });
  ts.sort(function(a,b){ return a.r.top-b.r.top; });
  for(var i=1;i<ts.length;i++){ var c0=(ts[i-1].r.top+ts[i-1].r.bottom)/2, c1=(ts[i].r.top+ts[i].r.bottom)/2, need=0.9*Math.max(ts[i-1].f, ts[i].f);
    if(c1-c0<need) out.push({fig:'meas', kind:'ticks-collide', text:ts[i-1].s+' <> '+ts[i].s, by:Math.round(need-(c1-c0))}); } });
var byx={}; gd.querySelectorAll('.xtick text').forEach(function(t){ var k=(t.parentElement&&t.parentElement.getAttribute('class'))||'x'; (byx[k]=byx[k]||[]).push(t); });
Object.keys(byx).forEach(function(k){ var ts=byx[k].map(function(t){ return {r:t.getBoundingClientRect(), s:t.textContent}; }).filter(function(o){ return o.r.width>0; });
  ts.sort(function(a,b){ return a.r.left-b.r.left; });
  for(var i=1;i<ts.length;i++){ if(ts[i].r.left<ts[i-1].r.right+2) out.push({fig:'meas', kind:'xticks-collide', text:ts[i-1].s+' <> '+ts[i].s, by:Math.round(ts[i-1].r.right+2-ts[i].r.left)}); } });
[['.g-ytitle','.ytick text'],['.g-xtitle','.xtick text']].forEach(function(pair){
  gd.querySelectorAll(pair[0]+' text').forEach(function(ti){ var a=ti.getBoundingClientRect(); if(!a.width) return;
    gd.querySelectorAll(pair[1]).forEach(function(t){ var b=t.getBoundingClientRect(); var ox=Math.min(a.right,b.right)-Math.max(a.left,b.left), oy=Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top);
      if(ox>1&&oy>1) out.push({fig:'meas', kind:'axis-title-over-ticks', text:ti.textContent.slice(0,24)+' <> '+t.textContent, by:Math.round(Math.min(ox,oy))}); }); }); });
var leg=gd.querySelector('.legend');
if(leg){ var lr=leg.getBoundingClientRect();
  if(lr.left<g.left-1||lr.right>g.right+1||lr.top<g.top-1||lr.bottom>g.bottom+1)
    out.push({fig:'meas', kind:'legend-outside-figure', text:'legend', by:Math.round(Math.max(g.left-lr.left, lr.right-g.right, g.top-lr.top, lr.bottom-g.bottom))});
  var overs=0; recs.forEach(function(a){ if(!a.cell) return; var ox=Math.min(lr.right,a.cell.x1)-Math.max(lr.left,a.cell.x0), oy=Math.min(lr.bottom,a.cell.y1)-Math.max(lr.top,a.cell.y0); if(ox>2&&oy>2) overs=Math.max(overs,Math.round(oy)); });
  if(overs) out.push({fig:'meas', kind:'legend-over-plot-area', text:'legend', by:overs});
  var mb=(gd.parentElement&&gd.parentElement.querySelector('.modebar'))||gd.querySelector('.modebar');
  if(mb){ var m=mb.getBoundingClientRect(), mx=Math.min(lr.right,m.right)-Math.max(lr.left,m.left), my=Math.min(lr.bottom,m.bottom)-Math.max(lr.top,m.top);
    if(mx>2&&my>2) out.push({fig:'meas', kind:'legend-under-modebar', text:'legend', by:Math.round(Math.min(mx,my))}); } }
document.querySelectorAll('.h').forEach(function(h){ var r=h.querySelector('.r');
  if(r&&r.scrollWidth>r.clientWidth+2) out.push({fig:'meas-header', kind:'header-right-overflow', text:(h.textContent||'').trim().slice(0,50), by:r.scrollWidth-r.clientWidth});
  if(h.scrollWidth>h.clientWidth+2) out.push({fig:'meas-header', kind:'header-overflow', text:(h.textContent||'').trim().slice(0,50), by:h.scrollWidth-h.clientWidth}); });
var seen={}; return out.filter(function(o){ var k=o.kind+'|'+o.text; if(seen[k]) return false; seen[k]=1; return true; });
"""


def quad(b):
    """MEAS_JS inside the measurement-space iframe (the Live page must have been opened with meas=1, which expands it)."""
    fr = b.wait_meas()
    b.drv.switch_to.frame(fr)
    try:
        return b.drv.execute_script(MEAS_JS)
    finally:
        b.drv.switch_to.default_content()


def run(base, sizes=((1920, 1080), (3796, 1879), (1366, 768)), texts=("Normal", "Large", "X-Large"), t="07:22:31", tag="", datasource=True):
    """``sizes`` = (w, h) browser windows, ``texts`` = sidebar Text size options, ``tag`` = suffix on the
    report / shot names (before/after runs), ``datasource`` = also audit the Data source page (page-chrome
    work; off for a plots-only pass, it halves the wall time)."""
    report = {}
    for (w, h) in sizes:
        for tx in texts:
            name = f"clip_audit_{w}x{h}_{tx.replace('-', '')}" + (f"_{tag}" if tag else "")
            b = E.Browser(w, h, name)
            try:
                b.open_live(base, flight=1, t=t, meas=1)   # meas=1: the MEASUREMENT SPACE quad starts EXPANDED (its own iframe below the panel)
                E._sidebar_radio(b, "Text size", tx); b.wait_panel(); time.sleep(4)   # ALWAYS set it: the app's default scale is per screen preset (Desktop 1080p defaults to Large), so "Normal" must be selected too
                page = b.js(PAGE_JS); panel = b.pjs(PANEL_JS)
                try:
                    quadr = quad(b)
                except Exception as e:                      # the quad never drew: that is itself the finding
                    quadr = [{"fig": "meas", "kind": "quad-not-drawn", "text": str(e)[:80]}]
                b.shot("live")
                ds = []
                if datasource:
                    b.get(base + "/"); time.sleep(5); ds = b.js(PAGE_JS); b.shot("datasource")
                report[name] = {"page": page, "panel": panel, "quad": quadr, "datasource": ds}
                json.dump(report[name], open(os.path.join(E.SHOTS, name + ".json"), "w"), indent=1)
                print(f"{w}x{h} {tx:8s} live page {len(page):2d} | panel {len(panel):2d} | quad {len(quadr):2d} | data source {len(ds):2d}")
                for o in (page + panel + quadr + ds)[:14]:
                    print("    ", o)
            finally:
                b.quit()
    return report

if __name__ == "__main__":
    url = os.environ.get("IH_E2E_URL"); proc = None
    if not url:
        port, lport = E.free_port(), E.free_port()
        env = {**os.environ, "IH_LIVE_PORT": str(lport), "PYTHONPATH": E.PYPATH}
        log = open(os.path.join(E.SHOTS, "side_clip_audit.log"), "w")
        proc = subprocess.Popen([E.STREAMLIT, "run", "app.py", "--server.port", str(port), "--server.headless", "true"], cwd=E.ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        url = f"http://127.0.0.1:{port}"; t0 = time.time()
        while time.time() - t0 < 60 and not E._http_ok(url + "/healthz"): time.sleep(0.5)
    def _sizes(v):
        return tuple(tuple(int(x) for x in part.lower().split("x")) for part in v.split(","))
    kw = {}
    for arg in sys.argv[1:]:                      # --sizes=1366x768,1536x864  --texts=Normal,Large  --tag=before  --no-ds
        if arg.startswith("--sizes="): kw["sizes"] = _sizes(arg.split("=", 1)[1])
        elif arg.startswith("--texts="): kw["texts"] = tuple(arg.split("=", 1)[1].split(","))
        elif arg.startswith("--tag="): kw["tag"] = arg.split("=", 1)[1]
        elif arg == "--no-ds": kw["datasource"] = False
    try:
        run(url.rstrip("/"), **kw)
    finally:
        if proc is not None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except Exception: pass
