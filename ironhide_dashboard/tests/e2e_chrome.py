"""Real-browser end-to-end scenarios in headless CHROME (the browser the user actually runs) — the Chrome twin of
tests/e2e_browser.py / tests/e2e_overlay.py, which only ever ran headless Firefox.  selenium + Chrome for Testing
against a RUNNING Streamlit instance; not collected by ``pytest tests/`` (no ``test_`` prefix: it starts a browser per
scenario and takes minutes).  Run it explicitly:

  cd ironhide_dashboard && python -m pytest -q tests/e2e_chrome.py [-k never_moves] [--durations=0]

Chrome / chromedriver: NOTHING is installed on this host.  Selenium Manager (Selenium >= 4.11) downloads BOTH
"Chrome for Testing" and the matching chromedriver into ~/.cache/selenium the first time ``webdriver.Chrome()`` finds
no browser, and reuses them afterwards:
  ~/.cache/selenium/chrome/linux64/<ver>/chrome
  ~/.cache/selenium/chromedriver/linux64/<ver>/chromedriver
Override either with IH_CHROME_BIN / IH_CHROMEDRIVER.  Flags: --headless=new --window-size=W,H --no-sandbox
--disable-gpu --disable-dev-shm-usage (a sandboxed headless Chrome cannot start in this container).

Target instance: ``IH_E2E_URL`` when set, else the module fixture starts a SIDE instance on FREE ports (never the
user's 8901/8902 and never the other reserved ports) from IH_CHROME_APP_DIR — by default the frozen snapshot
../ironhide_dashboard_served ($IH_SERVED_SNAPSHOT), so a half-saved edit in the live repo cannot break the run.

What is pinned (2026-09-15; all three of tonight's fixes, in Chrome):
  (a) test_page_never_moves — fixed-height top status bar: 40 s of 100 ms samples at 1x replay of flight 1 from
      07:21:40 (through the 07:21:57 handover + a Pause/Play round trip): ONE distinct value for the panel iframe's
      top / height, .ih-top height, .ih-status height and window.scrollY; 0 iframe loads / re-creations; Chrome's
      PerformanceObserver('layout-shift') cumulative score < 0.001 (Firefox has no such API — this is the one check
      Chrome can make and Firefox cannot); .ih-top computed opacity == 1 throughout.
  (b) test_map_no_relayout — the DOM overlay: 30 s with Plotly.relayout/react/animate/restyle wrapped INSIDE the panel
      iframe: 0 relayout and 0 animate on the map (only the server's <= 1 react per ~2 s), IH.overlay.relayouts == 0,
      and every second #ov-head-tgt sits inside the plot area with its centre within 20 px of the newest finite point
      of the target trail (gd._fullLayout.xaxis/yaxis _offset + l2p, as tests/e2e_overlay.py does in Firefox).
  (c) test_visual — screenshots of the Live page and the Data source page at every size x Text size into
      tests/_e2e_shots/chrome_*.png, plus a DOM overflow / overlap scan (and tests/clip_audit.py's PAGE_JS / PANEL_JS
      when importable) and the Chrome-specific metrics: classic scrollbar width eating panel width, status wrapping,
      the fixed status box's fit.
  (d) test_top_bar_never_dims — the Streamlit "stale element" dimming override: .ih-top computed opacity sampled every
      50 ms for 6 s of play must stay exactly 1.

Sizes: 1366x768 (the user's laptop — the important one), 1536x864, 1920x1080; Text size Normal and Large.  The window
is resized so the VIEWPORT is exactly W x H (headless Chrome's set_window_size sets the OUTER size and leaves ~143 px
of frame, unlike headless Firefox where inner == outer).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT = os.environ.get("IH_SERVED_SNAPSHOT") or os.path.join(os.path.dirname(ROOT), "ironhide_dashboard_served")
APP_DIR = os.environ.get("IH_CHROME_APP_DIR") or (SNAPSHOT if os.path.isfile(os.path.join(SNAPSHOT, "app.py")) else ROOT)
STREAMLIT = os.path.join(sys.prefix, "bin", "streamlit")
PYPATH = os.environ.get("PYTHONPATH", "")          # the app is self-contained (ih/vendor); chaos-spa is optional (README)
SHOTS = os.environ.get("IH_E2E_SHOTS") or os.path.join(ROOT, "tests", "_e2e_shots")
RESERVED = {8901, 8902, 8912, 8924, 8925, 8931, 8932}        # the served instance + ports other agents hold
CHROME_BIN = os.environ.get("IH_CHROME_BIN") or ""           # "" -> let Selenium Manager download Chrome for Testing
CHROMEDRIVER = os.environ.get("IH_CHROMEDRIVER") or ""

HAVE_TOOLS = True
try:
    from selenium import webdriver  # noqa: E402
    from selenium.webdriver.chrome.options import Options  # noqa: E402
    from selenium.webdriver.chrome.service import Service  # noqa: E402
    from selenium.webdriver.common.by import By  # noqa: E402
except Exception:  # pragma: no cover
    HAVE_TOOLS = False

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAVE_TOOLS, reason="selenium missing")]

SIZES = [(1366, 768), (1536, 864), (1920, 1080)]             # 1366x768 = the user's laptop
TEXTS = ["Normal", "Large"]
if os.environ.get("IH_CHROME_SIZES"):                        # "1366x768,1920x1080" — trim the matrix for a fast pass
    SIZES = [tuple(int(v) for v in part.lower().split("x")) for part in os.environ["IH_CHROME_SIZES"].split(",")]
if os.environ.get("IH_CHROME_TEXTS"):
    TEXTS = os.environ["IH_CHROME_TEXTS"].split(",")
MOVE_T = "07:21:40"                                          # 17 s before the F1 target-track handover 129 -> 177
MOVE_WINDOW_S = float(os.environ.get("IH_CHROME_MOVE_S", 40.0))
RUN_S = float(os.environ.get("IH_CHROME_RUN_S", 30.0))       # (b)
MIN_SAMPLES = int(MOVE_WINDOW_S * 10 * 0.62)                 # 100 ms sampler, minus the seconds spent in the two clicks
ICON_PX = 44
HEAD_TOL_PX = 20.0                                           # (b): head centre -> newest target trail point

# Chrome logs things Firefox does not: Streamlit's page-scoped /_stcore probes 404 on a multipage URL, the react-dom
# iframe allow-list warnings, font / source-map noise.  None of it is ours.
BENIGN = re.compile(r"_stcore/(health|host-config)|Unrecognized feature|downloadable font|source ?map|favicon|"
                    r"Content-Security-Policy|webgl|WEBGL|Deprecation|Third-party cookie|preload|"
                    r"Error: WebSocket|unreachable web|net::ERR_ABORTED|sandbox|Ignoring unsupported entryTypes", re.I)


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = int(s.getsockname()[1])
        if p not in RESERVED:
            return p


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def chrome_paths() -> dict:
    """Where Selenium Manager put Chrome for Testing / chromedriver (after the first webdriver.Chrome())."""
    out = {"chrome": CHROME_BIN, "chromedriver": CHROMEDRIVER}
    cache = os.path.expanduser("~/.cache/selenium")
    for kind in ("chrome", "chromedriver"):
        if out[kind]:
            continue
        base = os.path.join(cache, kind, "linux64")
        if os.path.isdir(base):
            vers = sorted(os.listdir(base))
            if vers:
                out[kind] = os.path.join(base, vers[-1], kind)
    return out


# ── the app under test ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def app():
    """Base URL: IH_E2E_URL, else a SIDE instance of APP_DIR/app.py started here on free ports (its own panel port, so
    it never touches the served :8901/:8902)."""
    url = os.environ.get("IH_E2E_URL")
    if url:
        assert _http_ok(url.rstrip("/") + "/healthz") or _http_ok(url.rstrip("/")), f"{url} not reachable"
        yield url.rstrip("/")
        return
    os.makedirs(SHOTS, exist_ok=True)
    port, lport = free_port(), free_port()
    env = {**os.environ, "IH_LIVE_PORT": str(lport), "PYTHONPATH": PYPATH}
    log = open(os.path.join(SHOTS, "chrome_side_instance.log"), "w")
    proc = subprocess.Popen([STREAMLIT, "run", "app.py", "--server.port", str(port), "--server.headless", "true",
                             "--server.fileWatcherType", "none"], cwd=APP_DIR, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    t0 = time.time()
    while time.time() - t0 < 90 and not _http_ok(base + "/healthz"):
        time.sleep(0.5)
    assert _http_ok(base + "/healthz"), f"side instance ({APP_DIR}) did not come up on {port}"
    try:
        yield base
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        log.close()


# ── browser wrapper ──────────────────────────────────────────────────────────
PANEL_SEL = "iframe"


class ChromeBrowser:
    """One headless Chrome at a VIEWPORT size; the same surface as e2e_browser.Browser (get / open_live / wait_panel /
    styled / wait_made / js / pjs / shot / console_errors / quit) so the helpers below read the same either way."""

    def __init__(self, w: int, h: int, name: str):
        os.makedirs(SHOTS, exist_ok=True)
        self.name = name
        self.want = (int(w), int(h))
        opts = Options()
        for a in ("--headless=new", f"--window-size={int(w)},{int(h)}", "--no-sandbox", "--disable-gpu",
                  "--disable-dev-shm-usage"):
            opts.add_argument(a)
        opts.add_argument("--force-device-scale-factor=1")          # deterministic screenshot pixels / DPR 1
        opts.add_argument("--hide-scrollbars=false")                # keep the CLASSIC scrollbar: it is what eats panel width on the user's Chrome
        opts.add_argument("--disable-features=CalculateNativeWinOcclusion")
        opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})  # the page console, read with get_log('browser')
        if CHROME_BIN:
            opts.binary_location = CHROME_BIN
        self.drv = (webdriver.Chrome(service=Service(executable_path=CHROMEDRIVER), options=opts)
                    if CHROMEDRIVER else webdriver.Chrome(options=opts))
        self.version = self.drv.capabilities.get("browserVersion", "?")
        self.driver_version = (self.drv.capabilities.get("chrome") or {}).get("chromedriverVersion", "?").split(" ")[0]
        self.fit_viewport(w, h)
        self._frame = None
        self._frame_collected = False

    def fit_viewport(self, w: int, h: int) -> dict:
        """headless Chrome's window size is the OUTER size (~143 px of frame at 1366x768): grow the window until the
        viewport is exactly w x h, so 1366x768 means the same thing here as in headless Firefox."""
        self.drv.set_window_size(int(w), int(h))
        for _ in range(4):
            iw, ih = self.drv.execute_script("return [window.innerWidth, window.innerHeight];")
            if (iw, ih) == (int(w), int(h)):
                break
            r = self.drv.get_window_rect()
            self.drv.set_window_rect(width=r["width"] + (int(w) - iw), height=r["height"] + (int(h) - ih))
        iw, ih = self.drv.execute_script("return [window.innerWidth, window.innerHeight];")
        self.viewport = (iw, ih)
        return {"want": [int(w), int(h)], "viewport": [iw, ih]}

    # ── navigation ──
    COLLECT_JS = """if(!window.__ihErrs){ window.__ihErrs=[]; var push=function(m){ try{ window.__ihErrs.push(String(m).slice(0,300)); }catch(e){} };
        window.addEventListener('error',function(e){ push('error: '+(e.message||e)+' @'+(e.filename||'')+':'+(e.lineno||0)); });
        window.addEventListener('unhandledrejection',function(e){ push('unhandledrejection: '+(e.reason&&(e.reason.stack||e.reason.message)||e.reason)); });
        var ce=console.error.bind(console), cw=console.warn.bind(console);
        console.error=function(){ push('console.error: '+Array.prototype.slice.call(arguments).join(' ')); ce.apply(null,arguments); };
        console.warn=function(){ var m=Array.prototype.slice.call(arguments).join(' '); if(/failed|unreachable|cannot load/i.test(m)) push('console.warn: '+m); cw.apply(null,arguments); }; }
        return true;"""

    def get(self, url: str) -> None:
        self.drv.get(url)
        self._frame = None
        self._frame_collected = False
        try:
            self.js(self.COLLECT_JS)
        except Exception:
            pass

    def collect_frame(self) -> None:
        if self._frame_collected:
            return
        try:
            self.pjs(self.COLLECT_JS)
            self._frame_collected = True
        except Exception:
            pass

    def open_live(self, base: str, **qp) -> None:
        qs = "&".join(f"{k}={v}" for k, v in qp.items())
        url = f"{base}/live" + (f"?{qs}" if qs else "")
        for _ in range(2):
            self.get(url)
            self.wait_panel()
            if self.styled():
                self.wait_made()
                return
            time.sleep(2)
        raise AssertionError("the Live page rendered unstyled twice (sidebar / block width)")

    def styled(self) -> bool:
        r = self.js("""var m=document.querySelector('[data-testid="stMainBlockContainer"]');
            var our=Array.from(document.querySelectorAll('style')).some(function(x){ return (x.textContent||'').indexOf('.ih-top')>=0; });
            return {our: our, mw: m?getComputedStyle(m).maxWidth:''};""")
        return bool(r["our"]) and r["mw"] == "100%"

    def wait_panel(self, timeout: float = 90.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            fr = self.panel()
            if fr is not None:
                self.collect_frame()
                return fr
            time.sleep(0.5)
        self.shot("no_panel")
        raise AssertionError("panel iframe not found")

    def panel(self):
        for f in self.drv.find_elements(By.CSS_SELECTOR, PANEL_SEL):
            try:
                if 'id="map"' in (f.get_attribute("srcdoc") or ""):
                    self._frame = f
                    return f
            except Exception:
                continue
        return None

    def wait_made(self, timeout: float = 60.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.pjs("return !!(window.IH&&IH.made.map&&IH.made.sep&&IH.made.err);"):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self.shot("not_made")
        raise AssertionError("panel figures not drawn in time")

    # ── JS ──
    def js(self, script: str, *args):
        return self.drv.execute_script(script, *args)

    def pjs(self, script: str, *args):
        """Run JS inside the panel iframe (switches in and back out)."""
        fr = self._frame or self.panel()
        self.drv.switch_to.frame(fr)
        try:
            return self.drv.execute_script(script, *args)
        finally:
            self.drv.switch_to.default_content()

    def in_frame(self, fr=None):
        self.drv.switch_to.frame(fr or self._frame or self.panel())

    def out(self):
        self.drv.switch_to.default_content()

    # ── evidence ──
    def shot(self, name: str) -> str:
        p = os.path.join(SHOTS, f"chrome_{self.name}_{name}.png")
        try:
            self.drv.save_screenshot(p)
        except Exception:
            pass
        return p

    def console_errors(self) -> list[str]:
        """Our page's errors: the in-page collectors (top document + panel frame) plus chromedriver's browser log
        (SEVERE entries), minus Chrome's own noise (BENIGN)."""
        out = []
        for scope, fn in (("page", self.js), ("panel", self.pjs)):
            try:
                for m in fn("return window.__ihErrs||[];") or []:
                    if not BENIGN.search(m):
                        out.append(f"{scope}: {m}")
            except Exception:
                pass
        try:
            for e in self.drv.get_log("browser"):
                m = str(e.get("message", ""))
                if e.get("level") == "SEVERE" and not BENIGN.search(m):
                    out.append("log: " + m[:300])
        except Exception:
            pass
        return out

    def quit(self) -> None:
        try:
            self.drv.quit()
        except Exception:
            pass


def assert_no_console_errors(b: ChromeBrowser) -> None:
    errs = b.console_errors()
    assert not errs, "browser console errors:\n" + "\n".join(errs[:20])


def _dump(b: ChromeBrowser, name: str, obj) -> None:
    with open(os.path.join(SHOTS, f"chrome_{b.name}_{name}.json"), "w") as f:
        json.dump(obj, f, indent=1, default=str)


# ── page helpers (same selectors as e2e_browser; duck-typed on ChromeBrowser) ──
def _sidebar_radio(b: ChromeBrowser, widget: str, option: str) -> None:
    ok = b.js("""var W=arguments[0], O=arguments[1];
        var rs=document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stRadio"]');
        for(var i=0;i<rs.length;i++){ var lab=rs[i].querySelector('[data-testid="stWidgetLabel"]'); if(!lab||lab.textContent.indexOf(W)<0) continue;
          var opts=rs[i].querySelectorAll('[role="radiogroup"] label'); for(var j=0;j<opts.length;j++){ if(opts[j].textContent.trim()===O){ opts[j].click(); return true; } } }
        return false;""", widget, option)
    assert ok, f"sidebar radio {widget!r} option {option!r} not found"
    time.sleep(2.5)


def _click_button(b: ChromeBrowser, text: str, where: str = "") -> None:
    ok = b.js("""var T=arguments[0], root=arguments[1]?document.querySelector(arguments[1]):document; var bs=root.querySelectorAll('button');
        for(var i=0;i<bs.length;i++){ var r=bs[i].getBoundingClientRect(); if(r.height>0&&(bs[i].textContent||'').indexOf(T)>=0){ bs[i].click(); return true; } } return false;""",
              text, where)
    assert ok, f"button {text!r} not found"
    time.sleep(2.5)


def _nav(b: ChromeBrowser, title: str) -> None:
    ok = b.js("""var T=arguments[0]; var as=document.querySelectorAll('[data-testid="stSidebarNav"] a, [data-testid="stSidebarNavLink"], [data-testid="stSidebar"] a');
        for(var i=0;i<as.length;i++){ if((as[i].textContent||'').trim()===T){ as[i].click(); return true; } } return false;""", title)
    assert ok, f"sidebar nav link {title!r} not found"
    time.sleep(3)


def _status_text(b: ChromeBrowser) -> str:
    try:
        return b.js("var s=document.querySelector('.ih-top .ih-status'); return s?s.textContent:'';") or ""
    except Exception:
        return ""


def _wait_status(b: ChromeBrowser, needle: str, timeout: float = 45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if needle in _status_text(b):
            return True
        time.sleep(0.25)
    return False


def _open_live_at(b: ChromeBrowser, app: str, text: str, t: str = MOVE_T, play: int = 1) -> None:
    """Live page at flight 1 / ``t`` with the sidebar Text size explicitly set (the app's default scale is per screen
    preset — "Normal" must be selected too), the panel re-found after the rerun and the figures redrawn."""
    kw = {"flight": 1, "t": t}
    if play:
        kw["play"] = 1
    for attempt in range(2):                     # a busy Streamlit occasionally serves a blank first render (no panel at all)
        try:
            b.open_live(app, **kw)
            _sidebar_radio(b, "Text size", text)
            b.wait_panel()
            b.wait_made()
            break
        except AssertionError:
            if attempt:
                raise
            time.sleep(3.0)
    time.sleep(1.5)


# ── (a) + (d) samplers ───────────────────────────────────────────────────────
# Same measurements as e2e_browser.MOVE_SAMPLER_JS, with Chrome's layout-shift observer added: NOT buffered (buffered
# entries replay the initial page render, which legitimately shifts ~0.3 before our chrome exists) and with each
# entry's sources resolved to a readable selector.
MOVE_SAMPLER_JS = r"""
var MS = arguments[0];
window.__ihMove = {rows: [], ls: [], loads: 0, gen: 0, lsErr: '', po: 0,
                   support: (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes) || []};
function sel(n){ if(!n) return 'null';
  if(!n.tagName) return 'text("'+String(n.textContent||n.nodeValue||'').trim().slice(0,40)+'")';
  var s=n.tagName.toLowerCase();
  if(n.id) s+='#'+n.id; var c=(n.getAttribute&&n.getAttribute('class'))||''; if(c) s+='.'+c.trim().split(/\s+/).slice(0,3).join('.');
  var dt=n.getAttribute&&n.getAttribute('data-testid'); if(dt) s+='['+dt+']';
  var p=n.parentElement, chain=[]; while(p&&chain.length<3){ var pc=(p.getAttribute&&p.getAttribute('class'))||''; chain.push(p.tagName.toLowerCase()+(pc?'.'+pc.trim().split(/\s+/)[0]:'')); p=p.parentElement; }
  return s+' "'+String(n.textContent||'').trim().slice(0,40)+'" in '+chain.join('<'); }
try {
  var po = new PerformanceObserver(function(l){ l.getEntries().forEach(function(e){
      var src = [];
      try { (e.sources||[]).forEach(function(s){ src.push({node: sel(s.node),
              prev: s.previousRect?[Math.round(s.previousRect.x),Math.round(s.previousRect.y),Math.round(s.previousRect.width),Math.round(s.previousRect.height)]:null,
              cur: s.currentRect?[Math.round(s.currentRect.x),Math.round(s.currentRect.y),Math.round(s.currentRect.width),Math.round(s.currentRect.height)]:null}); }); } catch(x){}
      window.__ihMove.ls.push({v: e.value, t: Math.round(e.startTime), inp: !!e.hadRecentInput, sources: src}); }); });
  po.observe({type: 'layout-shift'});                      /* NOT buffered: only shifts from now on */
  window.__ihMove.po = 1;
} catch (e) { window.__ihMove.lsErr = String(e); }
function panelFrame(){ var fs = document.querySelectorAll('iframe');
  for (var i = 0; i < fs.length; i++){ if ((fs[i].getAttribute('srcdoc') || '').indexOf('id="map"') >= 0) return fs[i]; }
  return null; }
function r2(v){ return Math.round(v * 100) / 100; }
window.__ihMove.tick = function(){
  var f = panelFrame(), top = document.querySelector('.ih-top'), st = document.querySelector('.ih-top .ih-status');
  var row = {t: Math.round(performance.now()), sy: Math.round(window.scrollY),
             sbw: window.innerWidth - document.documentElement.clientWidth};
  if (f) {
    if (!f.__ihHooked){ f.__ihHooked = 1; window.__ihMove.gen++; f.addEventListener('load', function(){ window.__ihMove.loads++; }); }
    var r = f.getBoundingClientRect();
    row.itop = r2(r.top); row.ih = r2(r.height); row.iw = r2(r.width); row.iabs = r2(r.top + window.scrollY);
  } else { row.missing = 1; }
  if (top) { row.toph = r2(top.getBoundingClientRect().height); row.op = getComputedStyle(top).opacity; }
  if (st) {
    var sr = st.getBoundingClientRect();
    row.sth = r2(sr.height); row.ssh = st.scrollHeight; row.sch = st.clientHeight;
    row.txt = (st.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200);
    var tops = {};
    row.out = [].slice.call(st.children).map(function(c){ var k = c.getBoundingClientRect(); tops[Math.round(k.top)] = 1;
        return {by: Math.round(Math.max(0, k.bottom - sr.bottom, sr.top - k.top, k.right - sr.right, sr.left - k.left)),
                tx: (c.textContent || '').trim().slice(0, 30)}; }).filter(function(o){ return o.by > 1; });
    row.rows = Object.keys(tops).length;                   /* how many visual lines the status wrapped onto */
  }
  row.gen = window.__ihMove.gen; row.loads = window.__ihMove.loads;
  window.__ihMove.rows.push(row);
};
window.__ihMove.iv = setInterval(window.__ihMove.tick, MS);
window.__ihMove.tick();
return {support: window.__ihMove.support, po: window.__ihMove.po, lsErr: window.__ihMove.lsErr,
        ls_supported: (window.__ihMove.support || []).indexOf('layout-shift') >= 0};
"""

PANEL_FIG_SAMPLER_JS = r"""
var MS = arguments[0];
window.__ihFig = {rows: []};
window.__ihFig.tick = function(){
  var row = {t: Math.round(performance.now())};
  ['map', 'sep', 'err', 'vel', 'velblock'].forEach(function(id){ var el = document.getElementById(id);
    if (el) row[id] = Math.round(el.getBoundingClientRect().height * 100) / 100; });
  row.body = Math.round(document.body.getBoundingClientRect().height);
  window.__ihFig.rows.push(row);
};
window.__ihFig.iv = setInterval(window.__ihFig.tick, MS);
window.__ihFig.tick();
return true;
"""

OPACITY_BURST_JS = r"""
window.__ihOp = {vals: [], n: 0};
window.__ihOp.iv = setInterval(function(){
  var top = document.querySelector('.ih-top'), main = document.querySelector('[data-testid="stMainBlockContainer"]');
  window.__ihOp.n++;
  if (top) window.__ihOp.vals.push(getComputedStyle(top).opacity + '/' + (main ? getComputedStyle(main).opacity : '-'));
}, arguments[0]);
return true;
"""


def _movement_start(b: ChromeBrowser, ms: int = 100) -> dict:
    return b.js(MOVE_SAMPLER_JS, ms)


def _movement_stop(b: ChromeBrowser) -> dict:
    return b.js("""try { clearInterval(window.__ihMove.iv); } catch (e) {}
        var m = window.__ihMove || {}; return {rows: m.rows || [], ls: m.ls || [], support: m.support || [], loads: m.loads || 0, gen: m.gen || 0};""")


def _panel_fig_sampler_start(b: ChromeBrowser, ms: int = 100) -> None:
    b.pjs(PANEL_FIG_SAMPLER_JS, ms)


def _panel_fig_sampler_stop(b: ChromeBrowser) -> list:
    return b.pjs("try { clearInterval(window.__ihFig.iv); } catch (e) {} return (window.__ihFig || {}).rows || [];")


def _opacity_burst(b: ChromeBrowser, seconds: float = 6.0, ms: int = 50) -> list:
    b.js(OPACITY_BURST_JS, ms)
    time.sleep(seconds)
    return b.js("try { clearInterval(window.__ihOp.iv); } catch (e) {} return (window.__ihOp || {}).vals || [];")


def _distinct(rows: list, key: str) -> list:
    return sorted({r[key] for r in rows if r.get(key) is not None})


def _status_fit_failures(rows: list) -> list:
    bad = []
    for r in rows:
        if r.get("sch") is None:
            continue
        if r["ssh"] > r["sch"] + 1 or r.get("out"):
            bad.append({"t": r["t"], "ssh": r["ssh"], "sch": r["sch"], "out": r.get("out"), "txt": r.get("txt")})
    return bad


def _cls(ls: list) -> dict:
    """Chrome's layout-shift report: cumulative score (all entries, input-triggered ones counted separately)."""
    tot = sum(float(e.get("v") or 0) for e in ls if not e.get("inp"))
    return {"cumulative": tot, "n": len(ls), "worst": max([float(e.get("v") or 0) for e in ls] or [0.0]),
            "entries": [{"v": e["v"], "t": e["t"], "sources": e.get("sources")} for e in ls[:12]]}


def _assert_never_moved(rows: list, tag: str) -> dict:
    """GEOMETRY only (the layout-shift budget is asserted separately, last, so a CLS regression cannot hide the
    geometric evidence): every sampled value is IDENTICAL and the iframe is the same element, never reloaded."""
    got = {k: _distinct(rows, k) for k in ("itop", "iabs", "ih", "iw", "toph", "sth", "sy")}
    for k in ("itop", "iabs", "ih", "iw", "toph", "sth"):
        assert len(got[k]) == 1, (tag, f"{k} moved", got[k][:8], got)
    assert len(got["sy"]) == 1, (tag, "the page scrolled during the sample", got["sy"][:8])
    assert _distinct(rows, "gen") == [1], (tag, "the panel iframe element was re-created", _distinct(rows, "gen"))
    assert _distinct(rows, "loads") == [0], (tag, "the panel iframe reloaded", _distinct(rows, "loads"))
    assert not [r for r in rows if r.get("missing")], (tag, "the panel iframe disappeared from the DOM")
    return got


@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("text", TEXTS)
def test_page_never_moves(app, w, h, text):
    """(a) The fixed-height top status bar: nothing geometric moves for 40 s of 1x replay across the 07:21:57 handover
    (amber TRACK CHANGED) and a Pause / Play round trip — the two status changes that used to re-wrap the strip and
    shove the panel iframe 32-36 px.  Chrome-only evidence: PerformanceObserver('layout-shift') cumulative < 0.001."""
    b = ChromeBrowser(w, h, f"nomove_{w}x{h}_{text}")
    rows, figs, ls, info, ops, got = [], [], [], {}, [], {}
    try:
        _open_live_at(b, app, text)
        info = _movement_start(b, 100)
        assert info["ls_supported"] and info["po"] == 1, ("Chrome did not install the layout-shift observer", info)
        _panel_fig_sampler_start(b, 100)
        t_start = time.time()
        ops = _opacity_burst(b, 6.0, 50)                                        # (d), during play
        saw_change = _wait_status(b, "TRACK CHANGED", 45.0)                     # the handover flash
        _click_button(b, "Pause replay", '[data-testid="stSidebar"]')           # ... and PAUSED on top of it
        paused = _wait_status(b, "PAUSED", 10.0)
        time.sleep(3.0)
        _click_button(b, "Play replay", '[data-testid="stSidebar"]')
        while time.time() - t_start < MOVE_WINDOW_S:
            time.sleep(0.5)
        out = _movement_stop(b)
        rows, ls = out["rows"], out["ls"]
        figs = _panel_fig_sampler_stop(b)
        b.shot("end")

        assert len(rows) >= MIN_SAMPLES, (f"too few samples for a {MOVE_WINDOW_S:g} s window", len(rows), MIN_SAMPLES)
        assert saw_change, "the 07:21:57 TRACK CHANGED flash never appeared — the sample missed the handover"
        assert paused and any("PAUSED" in (r.get("txt") or "") for r in rows), "the status never showed PAUSED"
        assert len({r.get("txt") for r in rows}) >= 5, "the status text never changed — nothing was under test"
        got = _assert_never_moved(rows, f"{w}x{h} {text}")
        assert set(r.get("op") for r in rows) == {"1"}, ("the top bar was dimmed", sorted(set(r.get("op") for r in rows)))
        assert ops and set(ops) == {"1/1"}, ("fragment dimming during play (.ih-top / main block opacity)", sorted(set(ops))[:6])
        assert not _status_fit_failures(rows), ("the status box overflowed / clipped a part", _status_fit_failures(rows)[:4])
        assert len(figs) >= MIN_SAMPLES, ("too few panel samples", len(figs), MIN_SAMPLES)
        for fid in ("map", "sep", "err"):
            vals = _distinct(figs, fid)
            assert len(vals) == 1, (f"{fid} figure height changed", vals[:8])
        assert len(_distinct(figs, "vel")) <= 1, ("velocity figure height changed", _distinct(figs, "vel")[:8])
        assert_no_console_errors(b)
        # LAST: Chrome's layout-shift budget.  Nothing above moved a pixel VERTICALLY, but Chrome also scores the
        # HORIZONTAL reflow inside the flex .ih-status row (a part changing width pushes the parts after it sideways) —
        # Firefox has no layout-shift API and cannot see it at all.
        c = _cls(ls)
        assert c["cumulative"] < 0.001, (f"{w}x{h} {text}", "layout-shift cumulative score", c["cumulative"], c["entries"][:6])
    finally:
        _dump(b, "samples", {"chrome": b.version, "viewport": b.viewport, "info": info, "distinct": got,
                             "layout_shift": _cls(ls), "opacity_burst": sorted(set(ops)),
                             "status_rows": _distinct(rows, "rows"), "scrollbar_px": _distinct(rows, "sbw"),
                             "rows": rows, "figs": figs})
        b.quit()


@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("text", TEXTS)
def test_top_bar_never_dims(app, w, h, text):
    """(d) The Streamlit "stale element" dimming override (theme.py: [data-testid="stMain"] .stale-element /
    [data-stale="true"] { opacity:1 !important }): Streamlit fades a running fragment's elements after 0.5 s, which
    reads as the whole page flashing once a second.  6 s of 50 ms samples of .ih-top (and the main block) during play
    must be exactly 1."""
    b = ChromeBrowser(w, h, f"opacity_{w}x{h}_{text}")
    try:
        _open_live_at(b, app, text)
        vals = _opacity_burst(b, 6.0, 50)
        stale = b.js("""var n=document.querySelectorAll('[data-stale="true"]').length;
            var top=document.querySelector('.ih-top'); return {stale:n, top_op:top?getComputedStyle(top).opacity:null, n_samples:(window.__ihOp||{}).n};""")
        _dump(b, "opacity", {"chrome": b.version, "viewport": b.viewport, "n": len(vals), "distinct": sorted(set(vals)), "stale": stale})
        assert len(vals) >= 100, ("too few opacity samples", len(vals))
        assert set(vals) == {"1/1"}, ("the top bar / main block was dimmed during play", sorted(set(vals))[:6])
        assert_no_console_errors(b)
    finally:
        b.quit()


# ── (b) the map overlay: zero Plotly work ────────────────────────────────────
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

# The head-vs-trail sample: the overlay's own state, the DOM head rect, and the newest FINITE point of the target
# trail in pixels via the map's own axes (_offset / l2p) — exactly what tests/e2e_overlay.py does in Firefox.
HEAD_JS = r"""
var gd=document.getElementById('map'), fl=gd._fullLayout, xa=fl.xaxis, ya=fl.yaxis,
    ov=document.getElementById('ov'), ob=ov.getBoundingClientRect(), gb=gd.getBoundingClientRect();
function lastFinite(pred){ var best=null;
  (gd._fullData||[]).forEach(function(t){ var n=t.name||''; if(!pred(n)||t.visible===false||!t.x||!t.x.length) return;
    for(var i=t.x.length-1;i>=0;i--){ if(isFinite(t.x[i])&&isFinite(t.y[i])){ best={name:n,E:t.x[i],N:t.y[i],px:xa.l2p(t.x[i]),py:ya.l2p(t.y[i]),n:t.x.length}; break; } } });
  return best; }
var im=document.getElementById('ov-head-tgt'), r=im.getBoundingClientRect(), st=IH.overlay.heads.tgt;
return {geom:{l:ob.left-gb.left, t:ob.top-gb.top, w:ob.width, h:ob.height, xoff:xa._offset, yoff:ya._offset,
              xlen:xa._length, ylen:ya._length, overflow:getComputedStyle(ov).overflow, pe:getComputedStyle(ov).pointerEvents, inMap:ov.parentElement===gd},
        relayouts:IH.overlay.relayouts, tiles:IH.overlay.tile_relayouts, frames:IH.overlay.frames, avg_ms:IH.overlay.avg_ms,
        rate:IH.overlay.rate, mpp:IH.mpp(), scale:(IH.UI&&IH.UI.text_scale)||1, stamp:IH.stamp, mode:IH.geo&&IH.geo.mode,
        head:{hidden:im.hidden, state:st, iw:im.offsetWidth, ih:im.offsetHeight,
              cx:r.left+r.width/2-ob.left, cy:r.top+r.height/2-ob.top,
              rect:{l:r.left-ob.left, t:r.top-ob.top, r:r.right-ob.left, b:r.bottom-ob.top}},
        truth:lastFinite(function(n){ return n==='target truth'; }),
        track:lastFinite(function(n){ return n.indexOf('target track')===0; })};
"""


def _classify(calls: list) -> dict:
    out = {"react": [], "range": [], "tile": [], "geometry": [], "animate": [], "restyle": [], "forbidden": []}
    for c in calls:
        fn, keys = c["fn"], c["keys"]
        if fn == "react":
            out["react"].append(c)
        elif fn == "animate":
            out["animate"].append(c)
        elif fn == "restyle":
            out["restyle"].append(c)
        elif fn == "relayout" and keys and all(k in ("xaxis.range", "yaxis.range") for k in keys):
            out["range"].append(c)                        # our own applyRange (first paint / frame rev / follow / reset)
        elif fn == "relayout" and c.get("sat_only"):
            out["tile"].append(c)                         # a new satellite tile with no react to carry it
        elif fn == "relayout" and "height" in keys:
            out["geometry"].append(c)                     # applyGeometry (resize)
        else:
            out["forbidden"].append(c)
    return out


def _head_check(s: dict, tag: str, log: list) -> None:
    g, hd = s["geom"], s["head"]
    assert g["inMap"] and g["overflow"] == "hidden" and g["pe"] == "none", (tag, "overlay is not the clipped child of the map", g)
    assert abs(g["l"] - g["xoff"]) <= 1 and abs(g["t"] - g["yoff"]) <= 1 and abs(g["w"] - g["xlen"]) <= 1 and abs(g["h"] - g["ylen"]) <= 1, \
        (tag, "the overlay is not the plot area", g)
    assert s["relayouts"] == 0, (tag, "the overlay / tween path relayouted", s["relayouts"])
    st = hd["state"]
    if not st or not st.get("visible") or hd["hidden"]:
        log.append({"tag": tag, "head": "off"})
        return
    want = round(ICON_PX * float(s["scale"]))
    assert abs(hd["iw"] - want) <= 1 and abs(hd["ih"] - want) <= 1, (tag, "icon on-screen size", hd["iw"], hd["ih"], want)
    assert abs(hd["cx"] - st["px"]) <= 1 and abs(hd["cy"] - st["py"]) <= 1, (tag, "DOM centre != l2p", hd["cx"], hd["cy"], st)
    assert -1 <= hd["cx"] <= g["w"] + 1 and -1 <= hd["cy"] <= g["h"] + 1, (tag, "#ov-head-tgt centre outside the plot area", hd, g)
    d_truth = math.hypot(hd["cx"] - s["truth"]["px"], hd["cy"] - s["truth"]["py"]) if s.get("truth") else None
    d_track = math.hypot(hd["cx"] - s["track"]["px"], hd["cy"] - s["track"]["py"]) if s.get("track") else None
    cand = [d for d in (d_truth, d_track) if d is not None]
    assert cand, (tag, "no target trail trace on the map to compare against")
    log.append({"tag": tag, "cx": round(hd["cx"], 1), "cy": round(hd["cy"], 1),
                "d_truth": None if d_truth is None else round(d_truth, 1),
                "d_track": None if d_track is None else round(d_track, 1),
                "spd": st.get("spd"), "icon": hd["iw"], "mpp": round(s["mpp"], 2), "stamp": s["stamp"]})
    assert min(cand) <= HEAD_TOL_PX, (tag, "head detached from the newest target trail point", log[-1])


@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("text", TEXTS)
def test_map_no_relayout(app, w, h, text):
    """(b) The DOM overlay (vehicle heads / leaders / live segments / track pills) does ZERO Plotly work between the
    server's map reacts: 30 s of 1x replay with Plotly.relayout/react/animate/restyle wrapped inside the panel iframe
    -> 0 relayout and 0 animate on the map, IH.overlay.relayouts == 0, reacts <= 1 per ~2 s; and every second
    #ov-head-tgt is inside the plot area within 20 px of the newest finite target trail point."""
    b = ChromeBrowser(w, h, f"norelayout_{w}x{h}_{text}")
    log: dict = {"chrome": b.version, "samples": [], "calls": None}
    try:
        _open_live_at(b, app, text)
        log["mode"] = b.pjs("return IH.geo&&IH.geo.mode;")
        assert b.pjs(WRAP_JS) is True
        t0 = time.time()
        i = 0
        while time.time() - t0 < RUN_S:
            _head_check(b.pjs(HEAD_JS), f"s{i:02d}", log["samples"])
            if i in (0, 15):
                b.shot(f"play_{i:02d}")
            i += 1
            time.sleep(max(0.0, 1.0 - 0.15))
        c = b.pjs(CALLS_JS)
        cl = _classify(c["calls"])
        ov = b.pjs("return IH.overlay;")
        log["calls"] = {k: len(v) for k, v in cl.items()}
        log["calls"]["elapsed_s"] = round(c["elapsed"], 1)
        log["calls"]["forbidden_detail"] = cl["forbidden"][:10]
        log["calls"]["relayout_total"] = len(cl["range"]) + len(cl["tile"]) + len(cl["geometry"]) + \
            len([x for x in cl["forbidden"] if x["fn"] == "relayout"])
        log["overlay"] = ov
        assert not cl["forbidden"], ("Plotly calls on the map outside reacts / our range apply / tile / geometry", cl["forbidden"][:5])
        assert log["calls"]["relayout_total"] == 0, ("the map was relayouted", log["calls"])
        assert not cl["animate"], ("the map was animated", cl["animate"][:5])
        assert ov["relayouts"] == 0 and all(x["ov_relayouts"] == 0 for x in c["calls"]), ("IH.overlay.relayouts", ov["relayouts"])
        n_react, el = len(cl["react"]), c["elapsed"]
        assert max(3, el / 4) <= n_react <= el / 1.5 + 2, ("react cadence is not <= 1 per ~2 s", n_react, round(el, 1))
        assert ov["avg_ms"] is not None and ov["avg_ms"] < 8 and ov["rate"] == 30, ov
        assert_no_console_errors(b)
    finally:
        _dump(b, "overlay", log)
        b.quit()


# ── (c) visual + overflow scan ───────────────────────────────────────────────
try:
    import clip_audit as CA                                # PAGE_JS / PANEL_JS only (its run() builds a Firefox Browser)
    HAVE_CLIP = True
except Exception:                                          # pragma: no cover
    HAVE_CLIP = False

# Chrome-specific page geometry: the classic scrollbar (Chrome on Linux reserves ~15 px, headless Firefox 0 with
# overlay scrollbars), how the status strip wrapped, and every overlap in the chrome the user looks at.
OVERFLOW_JS = r"""
var vw = window.innerWidth, out = [], boxes = {};
function rect(e){ var r = e.getBoundingClientRect(); return {l:r.left, t:r.top, r:r.right, b:r.bottom, w:r.width, h:r.height}; }
document.querySelectorAll('[data-testid="stSidebar"] *, [data-testid="stMainBlockContainer"] *, .ih-top, .ih-top *').forEach(function(el){
  var r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return;
  var cs = getComputedStyle(el), txt = (el.textContent||'').trim();
  if (el.scrollWidth > el.clientWidth + 2 && cs.overflowX !== 'visible' && txt)
    out.push({kind:'scrollWidth>clientWidth', tag:el.tagName, cls:(el.className||'').toString().slice(0,40), text:txt.slice(0,60), by:el.scrollWidth-el.clientWidth});
  if (r.right > vw + 1) out.push({kind:'past-viewport-right', tag:el.tagName, text:txt.slice(0,60), by:Math.round(r.right-vw)});
});
function overlaps(rootSel, label){
  var root = document.querySelector(rootSel); if (!root) return;
  var kids = [].slice.call(root.children).filter(function(c){ var r=c.getBoundingClientRect(); return r.width>1&&r.height>1; });
  for (var i=0;i<kids.length;i++) for (var j=i+1;j<kids.length;j++){
    var A = kids[i].getBoundingClientRect(), B = kids[j].getBoundingClientRect();
    var ox = Math.min(A.right,B.right)-Math.max(A.left,B.left), oy = Math.min(A.bottom,B.bottom)-Math.max(A.top,B.top);
    if (ox>2 && oy>2) out.push({kind:'siblings-overlap', where:label, by:Math.round(Math.min(ox,oy)),
        text:((kids[i].textContent||'').trim().slice(0,30)+' <> '+(kids[j].textContent||'').trim().slice(0,30))});
  }
}
overlaps('.ih-top', 'top bar'); overlaps('.ih-top .row1', 'top bar row1'); overlaps('.ih-top .row1 .l', 'top bar left');
overlaps('.ih-top .row1 .r', 'top bar right'); overlaps('.ih-top .ih-status', 'status row');
overlaps('[data-testid="stSidebarUserContent"]', 'sidebar');
var st = document.querySelector('.ih-top .ih-status'), fr = null, fs = document.querySelectorAll('iframe');
for (var i=0;i<fs.length;i++){ if ((fs[i].getAttribute('srcdoc')||'').indexOf('id="map"')>=0) fr = fs[i]; }
var sb = document.querySelector('[data-testid="stSidebar"]'), main = document.querySelector('[data-testid="stMainBlockContainer"]');
var spans = st ? [].slice.call(st.children).map(function(c){ var r=c.getBoundingClientRect(), sr=st.getBoundingClientRect();
      return {text:(c.textContent||'').trim().slice(0,40), t:Math.round(r.top-sr.top), h:Math.round(r.height),
              out:Math.round(Math.max(0, r.bottom-sr.bottom, sr.top-r.top, r.right-sr.right, sr.left-r.left))}; }) : [];
var tops = {}; spans.forEach(function(s){ tops[s.t] = 1; });
return {findings: out, viewport:{w:vw, h:window.innerHeight, clientW:document.documentElement.clientWidth,
          scrollbar_px: vw - document.documentElement.clientWidth, dpr: window.devicePixelRatio,
          doc_scrollW: document.documentElement.scrollWidth, doc_clientW: document.documentElement.clientWidth,
          body_overflows_x: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1},
        status: st ? {h:Math.round(st.getBoundingClientRect().height), scrollH:st.scrollHeight, clientH:st.clientHeight,
                      fits: st.scrollHeight <= st.clientHeight + 1, wrapped_rows: Object.keys(tops).length,
                      font: getComputedStyle(st).font, text:(st.textContent||'').trim().replace(/\s+/g,' ').slice(0,160), spans: spans} : null,
        top: (function(){ var t=document.querySelector('.ih-top'); return t?{h:Math.round(t.getBoundingClientRect().height), op:getComputedStyle(t).opacity}:null; })(),
        panel: fr ? {w:Math.round(fr.getBoundingClientRect().width), h:Math.round(fr.getBoundingClientRect().height), top:Math.round(fr.getBoundingClientRect().top)} : null,
        sidebar_w: sb ? Math.round(sb.getBoundingClientRect().width) : null,
        main_w: main ? Math.round(main.getBoundingClientRect().width) : null};
"""


@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("text", TEXTS)
def test_visual(app, w, h, text):
    """(c) Screenshots of the Live page and the Data source page at every size x Text size, plus a DOM overflow /
    overlap scan and the Chrome page-geometry metrics (scrollbar width, status wrapping / fit, panel width).  The
    clip_audit PAGE_JS / PANEL_JS scans run too when that module imports."""
    b = ChromeBrowser(w, h, f"visual_{w}x{h}_{text}")
    rep: dict = {"chrome": b.version, "chromedriver": b.driver_version, "viewport": b.viewport, "want": [w, h]}
    try:
        _open_live_at(b, app, text, t="07:22:31", play=0)
        time.sleep(2.0)
        rep["live"] = b.js(OVERFLOW_JS)
        if HAVE_CLIP:
            rep["clip_page"] = b.js(CA.PAGE_JS)
            rep["clip_panel"] = b.pjs(CA.PANEL_JS)
        rep["shots"] = [os.path.basename(b.shot("live"))]
        _nav(b, "Data source")
        time.sleep(3.0)
        rep["datasource"] = b.js(OVERFLOW_JS)
        if HAVE_CLIP:
            rep["clip_datasource"] = b.js(CA.PAGE_JS)
        rep["shots"].append(os.path.basename(b.shot("datasource")))
        st = rep["live"]["status"]
        assert st is not None, "no .ih-status on the Live page"
        assert st["fits"], ("the fixed status box does not fit its content (scrollHeight > clientHeight)", st)
        assert not [s for s in st["spans"] if s["out"] > 1], ("a status span sticks out of the row", st["spans"])
        assert rep["live"]["top"]["op"] == "1", ("the top bar is dimmed", rep["live"]["top"])
        assert not rep["live"]["viewport"]["body_overflows_x"], ("the Live page scrolls horizontally", rep["live"]["viewport"])
        assert not rep["datasource"]["viewport"]["body_overflows_x"], ("the Data source page scrolls horizontally", rep["datasource"]["viewport"])
        bad = [f for f in rep["live"]["findings"] + rep["datasource"]["findings"] if f["kind"] != "scrollWidth>clientWidth" or f["by"] > 2]
        assert not bad, ("DOM overflow / overlap findings", bad[:8])
        if HAVE_CLIP:
            # clip_audit.PAGE_JS's "clipped-vertically" rule is NOT portable to Chrome: clipAnc() stops at the first
            # ancestor whose computed overflow contains "hidden" (Chrome's two-value form on Streamlit's scroll
            # containers), so every element below the fold is reported — 110 findings on a clean page.  Its other
            # kinds, and PANEL_JS, are portable.  See the report: fix clipAnc by skipping ancestors that actually
            # scroll on that axis (scrollHeight > clientHeight).
            for key in ("clip_page", "clip_datasource"):
                rep[key + "_vertical_only"] = len([f for f in rep[key] if f["kind"] == "clipped-vertically"])
                real = [f for f in rep[key] if f["kind"] != "clipped-vertically"]
                assert not real, (f"clip_audit findings ({key})", real[:6])
            assert not rep["clip_panel"], ("clip_audit findings in the panel", rep["clip_panel"][:6])
        assert_no_console_errors(b)
    finally:
        _dump(b, "visual", rep)
        b.quit()


# ── the pixel-diff series (task 3): run as a script, not a test ──────────────
def pixel_diff_series(base: str, w: int = 1366, h: int = 768, text: str = "Normal", n: int = 12, dt: float = 0.5) -> dict:
    """PIL pixel diff INSIDE the panel iframe: a screenshot every ``dt`` s during 1x play; the fraction of panel pixels
    that changed from the previous frame (the Firefox baseline is 0.1-0.5 %; before tonight's fixes a jump frame moved
    15 % of them)."""
    from PIL import Image, ImageChops

    b = ChromeBrowser(w, h, f"pxdiff_{w}x{h}_{text}")
    try:
        _open_live_at(b, app=base, text=text)
        fr = b._frame or b.panel()
        box = b.js("""var r=arguments[0].getBoundingClientRect(), d=window.devicePixelRatio||1;
            return [Math.round(r.left*d), Math.round(r.top*d), Math.round(r.right*d), Math.round(r.bottom*d)];""", fr)
        shots, prev, fracs = [], None, []
        for i in range(n):
            p = b.shot(f"px_{i:02d}")
            im = Image.open(p).convert("L").crop(tuple(box))
            if prev is not None:
                diff = ImageChops.difference(im, prev)
                px = list(diff.getdata())
                fracs.append(sum(1 for v in px if v > 8) / float(len(px)))
            prev = im
            shots.append(os.path.basename(p))
            time.sleep(dt)
        out = {"chrome": b.version, "viewport": b.viewport, "panel_box": box, "shots": shots,
               "frac_changed": [round(f, 5) for f in fracs],
               "pct_changed": [round(100 * f, 3) for f in fracs],
               "max_pct": round(100 * max(fracs), 3) if fracs else None,
               "mean_pct": round(100 * sum(fracs) / len(fracs), 3) if fracs else None}
        _dump(b, "pixdiff", out)
        return out
    finally:
        b.quit()


if __name__ == "__main__":                                  # python tests/e2e_chrome.py [base_url]
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IH_E2E_URL", "http://127.0.0.1:8940")
    print("chrome paths:", json.dumps(chrome_paths(), indent=1))
    print(json.dumps(pixel_diff_series(base.rstrip("/")), indent=1))
