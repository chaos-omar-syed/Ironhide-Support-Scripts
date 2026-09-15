"""Real-browser end-to-end scenarios for the Live panel: selenium + headless Firefox against a RUNNING Streamlit
instance.  Not collected by ``pytest tests/`` (no ``test_`` prefix: it takes minutes and starts a browser per scenario);
run it explicitly:

  cd ironhide_dashboard && python -m pytest -q tests/e2e_browser.py [-k wrapper] [--durations=0]

Target instance: ``IH_E2E_URL`` (e.g. http://127.0.0.1:8905) when set, else the module fixture starts a SIDE instance of
app.py on free ports (never the user's :8901 unless you point IH_E2E_URL at it).  Skipped when geckodriver / firefox are
missing.  Screenshots + geckodriver logs (browser console via devtools.console.stdout.content) land in ``IH_E2E_SHOTS``
(default tests/_e2e_shots/).  Every scenario ends with ``assert_no_console_errors``.

User complaints these scenarios pin (2026-09-10):
  2. "MEASUREMENT SPACE strip covering the view"  -> test_panel_wrapper_never_overlapped
  1. "zoom out too far: snapping, edges don't load" -> test_map_view_lock_and_satellite_coverage
  3. "track error covering the plots (right side = now)" -> test_readouts_outside_plot_area
  4. "completely smooth" edge-case matrix         -> test_replay_smooth_no_flicker, test_controls_matrix, test_live_mode_fake_and_unreachable,
                                                    test_handover_and_steal_render, test_icon_size_constant_on_screen
"""
from __future__ import annotations

import json
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

GECKO = os.environ.get("IH_GECKODRIVER") or shutil.which("geckodriver") or "geckodriver"
FIREFOX = os.environ.get("IH_FIREFOX") or shutil.which("firefox") or "/usr/bin/firefox"
STREAMLIT = os.path.join(sys.prefix, "bin", "streamlit")
PYPATH = os.environ.get("PYTHONPATH", "")          # the app is self-contained (ih/vendor); chaos-spa is optional (README)
SHOTS = os.environ.get("IH_E2E_SHOTS") or os.path.join(ROOT, "tests", "_e2e_shots")
HAVE_TOOLS = os.path.isfile(GECKO) and os.path.isfile(FIREFOX)
try:
    from selenium import webdriver  # noqa: E402
    from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
    from selenium.webdriver.common.by import By  # noqa: E402
    from selenium.webdriver.common.keys import Keys  # noqa: E402
    from selenium.webdriver.firefox.options import Options  # noqa: E402
    from selenium.webdriver.firefox.service import Service  # noqa: E402
except Exception:  # pragma: no cover
    HAVE_TOOLS = False

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAVE_TOOLS, reason="geckodriver / firefox / selenium missing")]

F1_PASS_T = "07:22:31"          # F1 pass 2, still closing (the standard screenshot moment)
HANDOVER_T = "07:21:57"         # F1 target track 129 -> 177
STEAL_T = "07:23:52"            # the steal
BENIGN = re.compile(r"webgl|WEBGL|downloadable font|Content-Security-Policy|Layout was forced|favicon|"
                    r"unreachable web|NS_BINDING_ABORTED|source map|onmozfullscreen|InstallTrigger|"
                    r"Ignoring unsupported entryTypes|Cookie|sandbox|Error: WebSocket", re.I)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ── the app under test ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def app():
    """Base URL of the instance under test: IH_E2E_URL, else a SIDE instance started here (free ports, own log)."""
    url = os.environ.get("IH_E2E_URL")
    if url:
        assert _http_ok(url.rstrip("/") + "/healthz") or _http_ok(url.rstrip("/")), f"{url} not reachable"
        yield url.rstrip("/")
        return
    os.makedirs(SHOTS, exist_ok=True)
    port, lport = free_port(), free_port()
    env = {**os.environ, "IH_LIVE_PORT": str(lport), "PYTHONPATH": PYPATH}
    log = open(os.path.join(SHOTS, "side_instance.log"), "w")
    proc = subprocess.Popen([STREAMLIT, "run", "app.py", "--server.port", str(port), "--server.headless", "true"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    t0 = time.time()
    while time.time() - t0 < 60 and not _http_ok(base + "/healthz"):
        time.sleep(0.5)
    assert _http_ok(base + "/healthz"), "side instance did not come up"
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


class Browser:
    """One headless Firefox at a window size; helpers to reach the Live page, the panel iframe's JS state (window.IH) and
    plotly's DOM; screenshots + the browser console (via the geckodriver log)."""

    def __init__(self, w: int, h: int, name: str):
        os.makedirs(SHOTS, exist_ok=True)
        self.name = name
        opts = Options()
        opts.add_argument("-headless")
        opts.binary_location = FIREFOX
        opts.set_preference("devtools.console.stdout.content", True)   # page console -> firefox stdout -> geckodriver log
        opts.set_preference("webgl.disabled", True)                    # the panel falls back to SVG scatter (deterministic pixels)
        opts.set_preference("browser.shell.checkDefaultBrowser", False)
        self.log = os.path.join(SHOTS, f"gecko_{name}.log")
        open(self.log, "w").close()                                     # geckodriver APPENDS: a previous run's errors must not leak into this session's verdict
        self.drv = webdriver.Firefox(service=Service(executable_path=GECKO, log_output=self.log), options=opts)
        self.drv.set_window_size(int(w), int(h))
        self._frame = None

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
        try:
            self.js(self.COLLECT_JS)
        except Exception:
            pass
        self._frame_collected = False

    def collect_frame(self) -> None:
        if getattr(self, "_frame_collected", False):
            return
        try:
            self.pjs(self.COLLECT_JS)
            self._frame_collected = True
        except Exception:
            pass

    def open_live(self, base: str, **qp) -> None:
        """Load /live?… and wait until the panel iframe exists, the page is STYLED (our CSS + wide layout) and the three figures are
        drawn.  A rare first render comes back unstyled / un-seeked (Streamlit race on a busy server): reload once."""
        qs = "&".join(f"{k}={v}" for k, v in qp.items())
        url = f"{base}/live" + (f"?{qs}" if qs else "")
        for attempt in range(2):
            self.get(url)
            self.wait_panel()
            if self.styled():
                self.wait_made()
                return
            time.sleep(2)
        raise AssertionError("the Live page rendered unstyled twice (sidebar / block width)")

    def styled(self) -> bool:
        r = self.js("""var sb=document.querySelector('[data-testid="stSidebar"]'); var m=document.querySelector('[data-testid="stMainBlockContainer"]');
            var our=Array.from(document.querySelectorAll('style')).some(function(x){ return (x.textContent||'').indexOf('.ih-top')>=0; });
            return {sb: sb?sb.getBoundingClientRect().width:0, m: m?m.getBoundingClientRect().width:0, our: our, mw: m?getComputedStyle(m).maxWidth:''};""")
        return bool(r["our"]) and r["mw"] == "100%"

    def wait_panel(self, timeout: float = 60.0):
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

    def wait_made(self, timeout: float = 45.0) -> None:
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

    def wait_meas(self, timeout: float = 30.0):
        """The MEASUREMENT SPACE iframe (when expanded) with its figure drawn -> the iframe element."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            for f in self.drv.find_elements(By.CSS_SELECTOR, "iframe"):
                if 'id="meas"' in (f.get_attribute("srcdoc") or ""):
                    self.drv.switch_to.frame(f)
                    try:
                        ok = self.drv.execute_script("var g=document.getElementById('meas'); return !!(g&&g._fullLayout);")
                    finally:
                        self.drv.switch_to.default_content()
                    if ok:
                        return f
            time.sleep(0.5)
        raise AssertionError("measurement-space quad not drawn")

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

    # ── panel state ──
    def map_range(self) -> dict:
        return self.pjs("return IH.curRange();")

    def view(self) -> dict:
        return self.pjs("var v=IH.VIEW; return {userView:v.userView, rev:v.rev, prog:v.prog, frame:v.frame, applied:v.applied};")

    def plot_px(self) -> dict:
        return self.pjs("var fl=document.getElementById('map')._fullLayout; return {w:fl.width-fl.margin.l-fl.margin.r,h:fl.height-fl.margin.t-fl.margin.b,l:fl.margin.l,t:fl.margin.t,W:fl.width,H:fl.height};")

    def wheel(self, n: int, dy: int = 120, fx: float = 0.5, fy: float = 0.5, pause: float = 0.08) -> None:
        """n wheel notches on the map's drag layer (dy > 0 = zoom OUT); scrollZoom is on."""
        for _ in range(n):
            self.pjs("""var gd=document.getElementById('map'), d=gd.querySelector('.nsewdrag'), bb=d.getBoundingClientRect();
                d.dispatchEvent(new WheelEvent('wheel',{deltaY:arguments[0],deltaMode:0,clientX:bb.left+bb.width*arguments[1],clientY:bb.top+bb.height*arguments[2],bubbles:true,cancelable:true}));""", dy, fx, fy)
            time.sleep(pause)
        time.sleep(0.3)

    def pan(self, dx: int, dy: int) -> None:
        """Drag the map by (dx, dy) px with REAL pointer events (ActionChains inside the frame)."""
        self.in_frame()
        try:
            d = self.drv.find_element(By.CSS_SELECTOR, "#map .nsewdrag")
            ac = ActionChains(self.drv)
            ac.move_to_element(d).click_and_hold().pause(0.05)
            steps = 6
            for _ in range(steps):
                ac.move_by_offset(dx / steps, dy / steps).pause(0.03)
            ac.release().perform()
        finally:
            self.out()
        time.sleep(0.4)

    def dblclick_map(self) -> None:
        self.pjs("""var gd=document.getElementById('map'), d=gd.querySelector('.nsewdrag'), bb=d.getBoundingClientRect();
            d.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true,clientX:bb.left+bb.width*0.5,clientY:bb.top+bb.height*0.5}));""")
        time.sleep(1.0)

    def sat_box(self) -> dict | None:
        """The satellite layout image(s) as drawn: union box in data units + the <image> element identity counter."""
        return self.pjs("""var gd=document.getElementById('map'), ims=(gd.layout.images||[]).filter(function(i){ return i.name==='sat'||(i.layer==='below'&&i.sizing==='stretch'); });
            if(!ims.length) return null; var x0=Infinity,x1=-Infinity,y0=Infinity,y1=-Infinity;
            ims.forEach(function(i){ x0=Math.min(x0,i.x); x1=Math.max(x1,i.x+i.sizex); y1=Math.max(y1,i.y); y0=Math.min(y0,i.y-i.sizey); });
            var els=gd.querySelectorAll('.layer-below image'); var ids=[]; els.forEach(function(e){ if(!e.__ihid){ window.__ihn=(window.__ihn||0)+1; e.__ihid=window.__ihn; } ids.push(e.__ihid); });
            return {x0:x0,x1:x1,y0:y0,y1:y1,n:ims.length,ids:ids,srcs:ims.map(function(i){ return String(i.source).slice(-24); })};""")

    def wrapper_metrics(self) -> dict:
        """Item 2: iframe vs its Streamlit element container vs the next element (the quad toggle row)."""
        fr = self._frame or self.panel()
        return self.js("""var fr=arguments[0], ec=fr.closest('[data-testid="stElementContainer"]')||fr.parentElement;
            var ir=fr.getBoundingClientRect(), er=ec.getBoundingClientRect(), nx=ec.nextElementSibling, nr=nx?nx.getBoundingClientRect():null;
            var btn=null; document.querySelectorAll('button').forEach(function(b){ var r=b.getBoundingClientRect(); if(/MEASUREMENT SPACE/i.test(b.textContent||'')&&r.height>0) btn=b; });
            var br=btn?btn.getBoundingClientRect():null;
            return {iframe:{top:ir.top,bottom:ir.bottom,h:ir.height,w:ir.width}, container:{top:er.top,bottom:er.bottom,h:er.height,flex:getComputedStyle(ec).flexBasis},
                    next:nr?{top:nr.top,h:nr.height}:null, toggle:br?{top:br.top,bottom:br.bottom}:null, innerH:window.innerHeight, innerW:window.innerWidth};""", fr)

    # ── evidence ──
    def shot(self, name: str) -> str:
        p = os.path.join(SHOTS, f"{self.name}_{name}.png")
        try:
            self.drv.save_screenshot(p)
        except Exception:
            pass
        return p

    PANEL_PHRASES = re.compile(r"relayout images failed|fit failed|range failed|react failed|tween failed|unreachable|cannot load .*plotly|plotly|Plotly", re.I)

    def console_errors(self) -> list[str]:
        """Errors OUR page logged: the in-page collectors (parent document + panel frame: window error / unhandledrejection / console.error /
        failure console.warn) plus geckodriver-log lines (devtools.console.stdout.content) that name the app's host or the panel JS's own
        failure phrases — Firefox's chrome noise (Nimbus experiments, line-0 internal exceptions) is not ours."""
        out = []
        for scope, fn in (("page", self.js), ("panel", self.pjs)):
            try:
                for m in fn("return window.__ihErrs||[];") or []:
                    if not BENIGN.search(m):
                        out.append(f"{scope}: {m}")
            except Exception:
                pass
        try:
            with open(self.log, errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        host = "127.0.0.1"
        for ln in lines:
            if not re.search(r"console\.error|JavaScript error|Uncaught|TypeError|ReferenceError|RangeError|SyntaxError", ln):
                continue
            if BENIGN.search(ln):
                continue
            if host in ln or self.PANEL_PHRASES.search(ln):
                out.append("log: " + ln.strip()[:300])
        return out

    def quit(self) -> None:
        try:
            self.drv.quit()
        except Exception:
            pass


def assert_no_console_errors(b: Browser) -> None:
    errs = b.console_errors()
    assert not errs, "browser console errors:\n" + "\n".join(errs[:20])


def _dump(b: Browser, name: str, obj) -> None:
    with open(os.path.join(SHOTS, f"{b.name}_{name}.json"), "w") as f:
        json.dump(obj, f, indent=1, default=str)


# ── 2. MEASUREMENT SPACE strip / wrapper height ─────────────────────────────
@pytest.mark.parametrize("w,h", [(1920, 1600), (3796, 1600), (1920, 1080), (1366, 900)])
def test_panel_wrapper_never_overlapped(app, w, h):
    """The panel iframe fits itself to the viewport (setFrameHeight); Streamlit's element container (a flex item with the PRESET
    flex-basis) must follow in BOTH directions so the quad toggle row starts at / below the iframe bottom — also after a
    window resize mid-session (grow, then shrink)."""
    b = Browser(w, h, f"wrapper_{w}x{h}")
    try:
        b.open_live(app, flight=1, t=F1_PASS_T)
        time.sleep(3)
        seq = []
        for tag, (ww, hh) in (("initial", (w, h)), ("taller", (w, h + 300)), ("narrower_shorter", (max(1000, w - 500), h - 200))):
            if tag != "initial":
                b.drv.set_window_size(ww, hh)
                time.sleep(2.5)
            m = b.wrapper_metrics()
            m["tag"] = tag
            seq.append(m)
            b.shot(tag)
            assert m["toggle"] is not None, m
            assert m["toggle"]["top"] >= m["iframe"]["bottom"] - 0.5, (tag, m)                      # never over the panel
            assert abs(m["container"]["h"] - m["iframe"]["h"]) <= 2, (tag, m)                       # the wrapper IS the iframe's height
            assert m["toggle"]["top"] - m["iframe"]["bottom"] <= 40, (tag, m)                       # and no dead band either (flex gap only)
            avail = m["innerH"] - m["iframe"]["top"] - 20                                            # viewportPanel(): fits the panel to the room below the (now fixed-height) top bar ...
            assert m["iframe"]["bottom"] <= m["innerH"] + 2 or avail < 420, (tag, avail, m)        # ... unless under 420 px remain: then the preset height stands and the page scrolls
        _dump(b, "wrapper", seq)
        assert_no_console_errors(b)
    finally:
        b.quit()


# ── 1. map: view lock (no snapping) + satellite coverage follows the view ───────
def _rng_eq(a: dict, b: dict, tol: float = 0.5) -> bool:
    return all(abs(a[k][i] - b[k][i]) <= tol for k in ("x", "y") for i in (0, 1))


def _covers(sat: dict | None, r: dict, frac: float = 0.01) -> bool:
    """The satellite image box covers the view (each edge within ``frac`` of the span)."""
    if not sat:
        return False
    w, h = r["x"][1] - r["x"][0], r["y"][1] - r["y"][0]
    return sat["x0"] <= r["x"][0] + frac * w and sat["x1"] >= r["x"][1] - frac * w and sat["y0"] <= r["y"][0] + frac * h and sat["y1"] >= r["y"][1] - frac * h


def _wait_cover(b: Browser, timeout: float = 40.0) -> tuple[bool, dict, dict | None, float]:
    """Wait until the satellite tile covers the CURRENT view; returns (covered, view, sat, seconds)."""
    t0 = time.time()
    while True:
        r, sat = b.map_range(), b.sat_box()
        if _covers(sat, r):
            return True, r, sat, time.time() - t0
        if time.time() - t0 > timeout:
            return False, r, sat, time.time() - t0
        time.sleep(1.0)


def _edge_texture(b: Browser, name: str) -> dict:
    """Pixel evidence for black edges: std-dev of the four 3 %-wide edge strips of the map plot area in a fresh screenshot
    (imagery has texture; a bare card / black strip is flat)."""
    from PIL import Image

    p = b.shot(name)
    fr = b._frame or b.panel()
    geo = b.js("var r=arguments[0].getBoundingClientRect(); return {x:r.left,y:r.top,dpr:window.devicePixelRatio||1};", fr)
    pp = b.pjs("var gd=document.getElementById('map'), fl=gd._fullLayout, r=gd.getBoundingClientRect(); return {x:r.left+fl.margin.l, y:r.top+fl.margin.t, w:fl.width-fl.margin.l-fl.margin.r, h:fl.height-fl.margin.t-fl.margin.b};")
    im = Image.open(p).convert("L")
    d = geo["dpr"]
    X, Y, W, H = (geo["x"] + pp["x"]) * d, (geo["y"] + pp["y"]) * d, pp["w"] * d, pp["h"] * d
    import statistics

    def std(box):
        px = list(im.crop(tuple(int(v) for v in box)).getdata())
        return statistics.pstdev(px) if len(px) > 1 else 0.0, (sum(px) / len(px) if px else 0.0)
    e = int(0.03 * min(W, H))
    return {"left": std((X, Y, X + e, Y + H)), "right": std((X + W - e, Y, X + W, Y + H)), "top": std((X, Y, X + W, Y + e)), "bottom": std((X, Y + H - e, X + W, Y + H)),
            "centre": std((X + W / 2 - e, Y + H / 2 - e, X + W / 2 + e, Y + H / 2 + e))}


def test_map_view_lock_and_satellite_coverage(app):
    """User: "when you zoom out too far things start snapping and the edges don't load".  While the view is locked nothing but the
    user may move it (ranges identical 3 s after the last user event, through server map re-sends every ~2 s and the follow logic);
    the satellite tile must follow the view (cover it) far out (~10 km), after a pan beyond the data, and far in (~100 m);
    Reset re-applies the frame and unlocks."""
    b = Browser(1920, 1080, "map_view")
    log: dict = {}
    try:
        b.open_live(app, flight=1, t=F1_PASS_T, play=1)              # playing: 1 s ticks (map re-sent every ~2 s, heads every tick)
        time.sleep(4)
        log["start"] = {"range": b.map_range(), "view": b.view(), "sat": b.sat_box()}
        # far OUT: 16 notches (plotly: ~9.5 % per notch) -> whole range ~10 km
        b.wheel(16, dy=120)
        r1, v1 = b.map_range(), b.view()
        assert v1["userView"] is True, v1                                            # the wheel locked the view
        time.sleep(3.5)
        r2 = b.map_range()
        log["far_out"] = {"after_wheel": r1, "after_3s": r2, "view": b.view()}
        assert _rng_eq(r1, r2), ("SNAP after zoom-out", r1, r2)                     # nothing moved the view (server ticks ran meanwhile)
        ok, r, sat, dt = _wait_cover(b)
        log["far_out"]["cover"] = {"ok": ok, "sat": sat, "range": r, "s": dt}
        assert _rng_eq(r2, r), ("SNAP while waiting for the tile", r2, r)
        assert ok, ("satellite does not cover the far-out view", r, sat)
        log["far_out"]["texture"] = _edge_texture(b, "far_out")
        # PAN beyond the data (real pointer drag), then hold
        b.pan(650, 320)
        r3 = b.map_range(); time.sleep(3.5); r4 = b.map_range()
        log["pan"] = {"after_drag": r3, "after_3s": r4}
        assert abs(r3["x"][0] - r2["x"][0]) > 0.2 * (r2["x"][1] - r2["x"][0]), ("the drag did not pan", r2, r3)
        assert _rng_eq(r3, r4), ("SNAP after pan", r3, r4)
        ok, r, sat, dt = _wait_cover(b)
        log["pan"]["cover"] = {"ok": ok, "sat": sat, "range": r, "s": dt}
        assert _rng_eq(r4, r), ("SNAP while waiting for the tile (pan)", r4, r)
        assert ok, ("satellite does not cover the panned view", r, sat)
        log["pan"]["texture"] = _edge_texture(b, "pan")
        # far IN: back over the vehicles (reset first so the target is in view), then 30 notches in about the centre -> ~120 m
        b.pjs("document.getElementById('pill').click();"); time.sleep(1.5)
        b.wheel(30, dy=-120)
        r5 = b.map_range(); time.sleep(3.5); r6 = b.map_range()
        log["far_in"] = {"after_wheel": r5, "after_3s": r6, "view": b.view()}
        assert (r5["x"][1] - r5["x"][0]) < 400, ("not far in", r5)
        assert _rng_eq(r5, r6), ("SNAP after zoom-in", r5, r6)
        ok, r, sat, dt = _wait_cover(b)
        log["far_in"]["cover"] = {"ok": ok, "sat": sat, "range": r, "s": dt}
        assert ok, ("satellite does not cover the far-in view", r, sat)
        log["far_in"]["texture"] = _edge_texture(b, "far_in")
        # RESET (pill): unlocked, frame re-applied at 1:1 px (y = frame exactly when the plot is wider than tall)
        b.pjs("document.getElementById('pill').click();"); time.sleep(1.5)
        v, r = b.view(), b.map_range()
        log["reset"] = {"view": v, "range": r}
        assert v["userView"] is False, v
        f = v["frame"]
        tol = 0.02 * (f["x1"] - f["x0"])                                                                  # plotly's automargin re-constrains by < 2 %
        assert abs(r["y"][0] - f["y0"]) < tol and abs(r["y"][1] - f["y1"]) < tol or abs(r["x"][0] - f["x0"]) < tol and abs(r["x"][1] - f["x1"]) < tol, (r, f)
        b.shot("reset")
        assert_no_console_errors(b)
    finally:
        _dump(b, "log", log)
        b.quit()


# ── 3. readouts outside the plot area (the right side = now) ─────────────────
ANNOT_JS = r"""
var gd=document.getElementById(arguments[0]), fl=gd._fullLayout, g=gd.getBoundingClientRect(), sz=fl._size;
var subs=[]; Object.keys(fl).forEach(function(k){ var m=/^yaxis(\d*)$/.exec(k); if(!m) return; var ya=fl[k], xa=fl['xaxis'+m[1]]||fl.xaxis; if(!ya.domain||!xa.domain) return;
  subs.push({key:k, x0:sz.l+sz.w*xa.domain[0], x1:sz.l+sz.w*xa.domain[1], y0:sz.t+sz.h*(1-ya.domain[1]), y1:sz.t+sz.h*(1-ya.domain[0]), xr:xa.range?xa.range.slice():null}); });
var ann=[]; gd.querySelectorAll('.infolayer g.annotation').forEach(function(a){ var r=a.getBoundingClientRect(); var t=a.querySelector('text'); ann.push({x0:r.left-g.left,x1:r.right-g.left,y0:r.top-g.top,y1:r.bottom-g.top,text:t?t.textContent.slice(0,60):'',idx:a.getAttribute('data-index')}); });
var lastx=null; (gd.data||[]).forEach(function(tr){ if(!tr.x||tr.hoverinfo==='skip'&&tr.name&&/anchor|pad/.test(tr.name)) return; for(var i=tr.x.length-1;i>=0;i--){ var v=tr.x[i]; if(v==null) continue; var t=new Date(v).getTime(); if(!isNaN(t)&&(lastx===null||t>lastx)) lastx=t; break; } });
var xr=fl.xaxis.range?fl.xaxis.range.map(function(v){ return new Date(v).getTime(); }):null;
return {subs:subs, ann:ann, lastx:lastx, xr:xr, W:fl.width, H:fl.height};
"""
MARK_GLYPHS = {"▾", "▲", "▼", "★", "✕"}
LINE_LABEL_RE = re.compile(r"^(CPA \d+ m · \d\d:\d\d:\d\d|(→ #\d+ · \d\d:\d\d:\d\d)(\s+→ #\d+ · \d\d:\d\d:\d\d)*|truth|track)$")   # 2026-09-14: the handover labels are ONE list in the first card's header strip ("tick_labels")   # 2026-09-11: lines are LABELLED on the cards (user: "what are these lines") — intended text at the top edge / line end


def _is_line_label(text: str) -> bool:
    return bool(LINE_LABEL_RE.match(text.strip()))


def _right_band_hits(rep: dict, frac: float = 0.15) -> list:
    hits = []
    for s in rep["subs"]:
        bx0 = s["x0"] + (1 - frac) * (s["x1"] - s["x0"])
        for a in rep["ann"]:
            if a["text"].strip() in MARK_GLYPHS or not a["text"].strip() or _is_line_label(a["text"]):
                continue                                                                  # marks and line labels are data annotations, not readouts
            if a["x1"] > bx0 and a["x0"] < s["x1"] and a["y1"] > s["y0"] and a["y0"] < s["y1"]:
                hits.append({"sub": s["key"], "ann": a, "band_x0": bx0})
    return hits


def _inside_plot_hits(rep: dict) -> list:
    """Annotations whose box intersects ANY subplot's plot area (readouts / titles must live in the header strips)."""
    hits = []
    for s in rep["subs"]:
        for a in rep["ann"]:
            if a["text"].strip() in MARK_GLYPHS or not a["text"].strip() or _is_line_label(a["text"]):
                continue
            ix = min(a["x1"], s["x1"]) - max(a["x0"], s["x0"]); iy = min(a["y1"], s["y1"]) - max(a["y0"], s["y0"])
            if ix > 2 and iy > 2:
                hits.append({"sub": s["key"], "ann": a, "overlap_px": [round(ix), round(iy)]})
    return hits


@pytest.mark.parametrize("t", [F1_PASS_T, "07:22:45", STEAL_T])
def test_readouts_outside_plot_area(app, t):
    """User: "track error is covering the plots — the right side (t = now) is the most important".  No text annotation (readouts, titles,
    CPA tag) may intersect any plot area of the track-quality cards or the separation card — in particular the last 15 % of the time
    axis; the time axis keeps ~2 % right padding so the newest sample is not glued to the frame."""
    b = Browser(1920, 1080, f"readouts_{t.replace(':', '')}")
    try:
        b.open_live(app, flight=1, t=t)
        time.sleep(3)
        b.shot("panel")
        out = {k: b.pjs(ANNOT_JS, k) for k in ("err", "sep")}
        out["mode"] = b.pjs("return IH.geo&&IH.geo.mode;")
        _dump(b, "annotations", out)                                                                  # evidence first, verdict second
        for k in ("err", "sep"):
            rep = out[k]
            hits = _right_band_hits(rep)
            assert not hits, (k, "annotations over the last 15 % of the time axis", hits[:4])
            inside = _inside_plot_hits(rep)
            assert not inside, (k, "annotations inside the plot area", inside[:4])
            if rep["xr"] and rep["lastx"]:
                span = rep["xr"][1] - rep["xr"][0]
                pad = (rep["xr"][1] - rep["lastx"]) / span
                assert pad >= 0.015, (k, "newest sample glued to the right edge", pad)
        assert_no_console_errors(b)
    finally:
        b.quit()


# ── icons: constant ON-SCREEN size ──────────────────────────────────────────
# 2026-09-14: the vehicle heads left Plotly's layout.images — they are DOM <img id="ov-head-tgt" / "ov-head-itc"> elements overlaid
# on the map div, their state in IH.overlay.heads[k] (px / py / hdg / size / visible), so the on-screen size is the <img> box.
ICON_JS = r"""
var out=[]; ["tgt","itc"].forEach(function(k){ var im=document.getElementById('ov-head-'+k), st=IH.overlay.heads[k]; if(!im||im.hidden||!st||!st.visible) return; out.push({w:im.offsetWidth,h:im.offsetHeight,svg:im.src.indexOf('svg')>=0}); }); return {els:out, imgs:[], ui:IH.UI};
"""


def test_icon_size_constant_on_screen(app):
    """User: "why are the drone and interceptor icons suddenly much bigger" — the heads are DOM <img> overlays positioned from the map's
    pixel transform; their on-screen size must stay ~ICON_PX (44 × text scale) ± 6 at the default frame, 3 notches in and 3 notches out."""
    b = Browser(1920, 1080, "icons")
    try:
        b.open_live(app, flight=1, t=F1_PASS_T)
        time.sleep(3)
        want = 44 * float(b.pjs("return (IH.UI&&IH.UI.text_scale)||1;"))
        rows = {}
        for tag, n, dy in (("default", 0, 0), ("in3", 3, -120), ("out3", 6, 120)):
            if n:
                b.wheel(n, dy=dy)
                time.sleep(1.2)
            r = b.pjs(ICON_JS)
            rows[tag] = r
            b.shot(f"icons_{tag}")
            vis = [e for e in r["els"] if e["svg"]]
            assert vis, (tag, r)
            for e in vis:
                assert abs(max(e["w"], e["h"]) - want) <= 6, (tag, "icon on-screen size", e, want)
        _dump(b, "icons", rows)
        assert_no_console_errors(b)
    finally:
        b.quit()


# ── 4. edge-case matrix ───────────────────────────────────────────────────────
def _map_pixels(b: Browser, name: str) -> dict:
    """Mean / std of the map plot area in a fresh screenshot (a black or white flash frame shows as an extreme mean)."""
    from PIL import Image
    import statistics

    p = b.shot(name)
    fr = b._frame or b.panel()
    geo = b.js("var r=arguments[0].getBoundingClientRect(); return {x:r.left,y:r.top,dpr:window.devicePixelRatio||1};", fr)
    pp = b.pjs("var gd=document.getElementById('map'), fl=gd._fullLayout, r=gd.getBoundingClientRect(); return {x:r.left+fl.margin.l, y:r.top+fl.margin.t, w:fl.width-fl.margin.l-fl.margin.r, h:fl.height-fl.margin.t-fl.margin.b};")
    im = Image.open(p).convert("L")
    d = geo["dpr"]
    box = tuple(int(v) for v in ((geo["x"] + pp["x"]) * d, (geo["y"] + pp["y"]) * d, (geo["x"] + pp["x"] + pp["w"]) * d, (geo["y"] + pp["y"] + pp["h"]) * d))
    px = list(im.crop(box).getdata())
    return {"mean": sum(px) / max(1, len(px)), "std": statistics.pstdev(px) if len(px) > 1 else 0.0, "shot": os.path.basename(p)}


def _sidebar_radio(b: Browser, widget: str, option: str) -> None:
    """Click ``option`` in the sidebar radio whose widget label contains ``widget`` (Streamlit: [data-testid=stRadio] > label + radiogroup)."""
    ok = b.js("""var W=arguments[0], O=arguments[1];
        var rs=document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stRadio"]');
        for(var i=0;i<rs.length;i++){ var lab=rs[i].querySelector('[data-testid="stWidgetLabel"]'); if(!lab||lab.textContent.indexOf(W)<0) continue;
          var opts=rs[i].querySelectorAll('[role="radiogroup"] label'); for(var j=0;j<opts.length;j++){ if(opts[j].textContent.trim()===O){ opts[j].click(); return true; } } }
        return false;""", widget, option)
    assert ok, f"sidebar radio {widget!r} option {option!r} not found"
    time.sleep(2.5)


def _sidebar_toggle(b: Browser, label: str) -> bool:
    """Flip the sidebar toggle whose label contains ``label`` (click its input / role=checkbox), return the new checked state."""
    r = b.js("""var L=arguments[0]; var cs=document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stCheckbox"]');
        for(var i=0;i<cs.length;i++){ if((cs[i].textContent||'').indexOf(L)<0) continue;
          var inp=cs[i].querySelector('input[type="checkbox"], [role="checkbox"], [role="switch"]')||cs[i].querySelector('label');
          var before=inp.getAttribute('aria-checked')||String(!!inp.checked); inp.click();
          return {before:before}; }
        return null;""", label)
    assert r is not None, f"sidebar toggle {label!r} not found"
    time.sleep(3.0)
    after = b.js("""var L=arguments[0]; var cs=document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stCheckbox"]');
        for(var i=0;i<cs.length;i++){ if((cs[i].textContent||'').indexOf(L)<0) continue; var inp=cs[i].querySelector('input[type="checkbox"], [role="checkbox"], [role="switch"]');
          return inp?(inp.getAttribute('aria-checked')||String(!!inp.checked)):null; } return null;""", label)
    return str(after).lower() == "true"


def _wait_pjs(b: Browser, script: str, want, timeout: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if b.pjs(script) == want:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _click_button(b: Browser, text: str, where: str = "") -> None:
    ok = b.js("""var T=arguments[0], root=arguments[1]?document.querySelector(arguments[1]):document; var bs=root.querySelectorAll('button');
        for(var i=0;i<bs.length;i++){ var r=bs[i].getBoundingClientRect(); if(r.height>0&&(bs[i].textContent||'').indexOf(T)>=0){ bs[i].click(); return true; } } return false;""", text, where)
    assert ok, f"button {text!r} not found"
    time.sleep(2.5)


def _press_key_on_page(b: Browser, key: str) -> None:
    b.js("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:arguments[0],bubbles:true}));", key)
    time.sleep(2.5)


def _meas_iframes(b: Browser) -> int:
    return len([f for f in b.drv.find_elements(By.CSS_SELECTOR, "iframe") if 'id="meas"' in (f.get_attribute("srcdoc") or "")])


@pytest.mark.parametrize("speed", ["1×", "2×", "4×"])
def test_replay_smooth_no_flicker(app, speed):
    """User: "make sure this is COMPLETELY SMOOTH".  Replay at 1x / 2x / 4x for 60 s, a screenshot every 2 s: no black / white flash
    frame in the map plot area, the satellite <image> element identity is stable while the tile key is unchanged (no re-created
    imagery), figures stay drawn, the envelope stamp advances; a window resize mid-play (1x) keeps the panel / wrapper consistent."""
    b = Browser(1920, 1080, f"replay_{speed[0]}x")
    rows = []
    try:
        b.open_live(app, flight=1, t="07:21:20", play=1)
        if speed != "1×":
            _sidebar_radio(b, "Replay speed", speed)
        time.sleep(2)
        prev_ids, prev_key, t0 = None, None, time.time()
        i = 0
        while time.time() - t0 < 60:
            if speed == "1×" and i == 10:
                b.drv.set_window_size(1500, 900)                                            # resize mid-play
            if speed == "1×" and i == 20:
                b.drv.set_window_size(1920, 1080)
            px = _map_pixels(b, f"s{i:02d}")
            sat = b.sat_box()
            key = b.pjs("return IH.SATK;")
            st = b.pjs("return {stamp:IH.stamp, made:IH.made, frozen:IH.TW.frozen, reacting:IH.reacting};")
            m = b.wrapper_metrics()
            row = {"i": i, "t": round(time.time() - t0, 1), "mean": round(px["mean"], 1), "std": round(px["std"], 1), "ids": sat and sat["ids"], "key": key, "stamp": st["stamp"],
                   "toggle_gap": round(m["toggle"]["top"] - m["iframe"]["bottom"], 1) if m["toggle"] else None}
            rows.append(row)
            assert 8 < px["mean"] < 200, ("flash frame", row)                               # never a black / white map
            assert st["made"]["map"] and st["made"]["sep"] and st["made"]["err"], row
            if prev_ids is not None and key == prev_key and sat:
                assert sat["ids"] == prev_ids, ("satellite <image> re-created without a tile change", row, prev_ids)
            if m["toggle"]:
                assert m["toggle"]["top"] >= m["iframe"]["bottom"] - 0.5, ("overlay during play", row)
            prev_ids, prev_key = (sat and sat["ids"]), key
            i += 1
            time.sleep(2.0)
        stamps = [r["stamp"] for r in rows if r["stamp"]]
        assert len(set(stamps)) >= 8, ("the envelope stamp did not advance", stamps)
        _dump(b, "samples", rows)
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", rows)
        b.quit()


def test_controls_matrix(app):
    """Presets Laptop / Desktop / Large; Text size Normal / Large / X-Large; frames Engagement / Follow / Fit / Fixed (+ Reset); Freeze on/off;
    quad expand / collapse by button and by the "m" key — every step: figures drawn, no console error, the quad toggle never over the panel."""
    b = Browser(1920, 1080, "controls")
    log = []
    try:
        b.open_live(app, flight=1, t=F1_PASS_T)

        def check(tag):
            b.wait_panel(); b.wait_made()
            time.sleep(1.0)
            m = b.wrapper_metrics()
            g = b.pjs("return {mode:IH.geo&&IH.geo.mode, panel:IH.PANEL, ui:IH.UI, font:IH.geo&&IH.geo.font};")
            log.append({"tag": tag, "mode": g["mode"], "panel": g["panel"], "font": g["font"], "ui": g["ui"], "gap": m["toggle"] and round(m["toggle"]["top"] - m["iframe"]["bottom"], 1)})
            b.shot(tag)
            assert m["toggle"] and m["toggle"]["top"] >= m["iframe"]["bottom"] - 0.5, (tag, m)
            errs = b.console_errors()
            assert not errs, (tag, errs[:5])
            return g

        for preset in ("Laptop", "Large 1440p", "Desktop 1080p"):
            _sidebar_radio(b, "Screen size", preset)
            g = check(f"preset_{preset.split()[0]}")
            assert g["ui"]["preset"] == preset, g
        for ts, want in (("Large", 1.15), ("X-Large", 1.3), ("Normal", 1.0)):
            _sidebar_radio(b, "Text size", ts)
            g = check(f"text_{ts}")
            assert abs(float(g["ui"]["text_scale"]) - want) < 1e-6, g
        for frame in ("Follow vehicles", "Fit whole flight", "Fixed (zoom slider)", "Engagement box"):
            _sidebar_radio(b, "Map frame", frame)
            check(f"frame_{frame.split()[0]}")
            v = b.view()
            assert v["userView"] is False, (frame, v)                                        # an explicit frame change re-applies the frame, unlocked
        b.wheel(3, dy=120)
        assert b.view()["userView"] is True
        _click_button(b, "Reset map view", '[data-testid="stSidebar"]')
        check("reset_button")
        assert b.view()["userView"] is False
        on = _sidebar_toggle(b, "Freeze display")
        assert on is True, "the Freeze toggle did not switch on"
        check("freeze_on")
        assert _wait_pjs(b, "return IH.TW.frozen;", True), ("panel not frozen", b.js("return document.querySelector('.ih-status').textContent;"))
        off = _sidebar_toggle(b, "Freeze display")
        assert off is False, "the Freeze toggle did not switch off"
        check("freeze_off")
        assert _wait_pjs(b, "return IH.TW.frozen;", False)
        assert _meas_iframes(b) == 0
        _click_button(b, "MEASUREMENT SPACE")
        b.wait_meas()
        check("quad_open_button")
        _press_key_on_page(b, "m")
        t0 = time.time()
        while _meas_iframes(b) and time.time() - t0 < 8:
            time.sleep(0.5)
        assert _meas_iframes(b) == 0, "m did not collapse the quad"
        check("quad_closed_key")
        _press_key_on_page(b, "m")
        b.wait_meas()
        check("quad_open_key")
        _dump(b, "log", log)
    finally:
        _dump(b, "log", log)
        b.quit()


PAIR_JS = r"""
var gd=document.getElementById(arguments[0]), g=gd.getBoundingClientRect(), out=[];
gd.querySelectorAll('.infolayer g.annotation').forEach(function(a){ var r=a.getBoundingClientRect(); var t=a.querySelector('text'); var nm=(gd.layout.annotations[+a.getAttribute('data-index')]||{}).name||'';
  out.push({x0:r.left-g.left,x1:r.right-g.left,y0:r.top-g.top,y1:r.bottom-g.top,text:t?t.textContent.slice(0,40):'',name:nm}); });
return out;
"""


def _overlaps(anns: list, skip_pairs=lambda a, b: False) -> list:
    out = []
    for i in range(len(anns)):
        for j in range(i + 1, len(anns)):
            a, c = anns[i], anns[j]
            if not a["text"].strip() or not c["text"].strip() or a["text"].strip() in MARK_GLYPHS or c["text"].strip() in MARK_GLYPHS or skip_pairs(a, c):
                continue
            ix = min(a["x1"], c["x1"]) - max(a["x0"], c["x0"]); iy = min(a["y1"], c["y1"]) - max(a["y0"], c["y0"])
            if ix > 3 and iy > 3:
                out.append((a["name"] or a["text"], c["name"] or c["text"], round(ix), round(iy)))
    return out


@pytest.mark.parametrize("t,label", [("07:22:02", "handover_129_177"), ("07:23:56", "steal")])
def test_handover_and_steal_render(app, t, label):
    """A track handover (F1 07:21:57, 129 -> 177) and the steal (07:23:52) inside the window: the error panel and the measurement quad
    render their ticks / marks / pills without text-on-text overlap; no console errors."""
    b = Browser(1920, 1080, label)
    try:
        b.open_live(app, flight=1, t=t)
        _click_button(b, "MEASUREMENT SPACE")
        mf = b.wait_meas()
        time.sleep(2)
        b.shot("panel")
        err = b.pjs(PAIR_JS, "err")
        ov = _overlaps(err)
        assert not ov, ("error panel text overlaps", ov)
        marks = [a for a in b.pjs(ANNOT_JS, "err")["ann"] if a["text"].strip() == "▾" or _is_line_label(a["text"])]   # 2026-09-11: visible line labels ("→ #177 · …", "CPA 59 m · …")
        assert marks, "no handover / CPA label rendered in the window"
        b.drv.switch_to.frame(mf)
        try:
            meas = b.drv.execute_script(PAIR_JS, "meas")
        finally:
            b.drv.switch_to.default_content()
        pills = [a for a in meas if a["name"].startswith(("lbl_start_", "lbl_end_"))]
        titles = [a for a in meas if a["name"].startswith("title_") or a["name"] == "geom_note"]
        ov2 = _overlaps(pills + titles, skip_pairs=lambda a, c: a["name"].startswith("lbl_") and c["name"].startswith("lbl_"))   # pills may touch each other at a shared end time
        assert not ov2, ("measurement quad pill / title overlaps", ov2)
        _dump(b, "annotations", {"err": err, "meas": meas})
        assert_no_console_errors(b)
    finally:
        b.quit()


# ── live mode against the fake mongo (tests/fake_mongo.py) + an unreachable host; Data source -> Load flight -> Live ──
FAKE_ENTRY = '''"""TEMPORARY e2e entry (generated by tests/e2e_browser.py, deleted after the run): app.py with tests/fake_mongo installed
process-wide, the 8/28 archive streaming as if pass 2 of F1 were about to happen."""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tests")); sys.path.insert(0, ROOT)
import fake_mongo as FM
from ih import data as D
if getattr(FM, "_E2E", None) is None:
    T0, T1 = D.FLIGHT_WINDOWS[1][0] - 60, D.FLIGHT_WINDOWS[1][1] + 30
    FM._E2E = FM.FakeMongo(T0, T1, shift_to_now=D.hms_to_epoch("07:21:50"))
    FM._E2E.install()
exec(compile(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read(), os.path.join(ROOT, "app.py"), "exec"))
'''


@pytest.fixture(scope="module")
def app_fake():
    """A SIDE instance of app.py whose pymongo.MongoClient is tests/fake_mongo (any host except the fake's UNREACHABLE set)."""
    entry = os.path.join(ROOT, "_e2e_fake_entry.py")
    with open(entry, "w", encoding="utf-8") as f:
        f.write(FAKE_ENTRY)
    os.makedirs(SHOTS, exist_ok=True)
    port, lport = free_port(), free_port()
    env = {**os.environ, "IH_LIVE_PORT": str(lport), "PYTHONPATH": PYPATH}
    log = open(os.path.join(SHOTS, "fake_instance.log"), "w")
    proc = subprocess.Popen([STREAMLIT, "run", "_e2e_fake_entry.py", "--server.port", str(port), "--server.headless", "true"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    t0 = time.time()
    while time.time() - t0 < 90 and not _http_ok(base + "/healthz"):
        time.sleep(0.5)
    try:
        assert _http_ok(base + "/healthz"), "fake-mongo side instance did not come up"
        yield base
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        log.close()
        try:
            os.remove(entry)
        except OSError:
            pass


def _wait_text(b: Browser, needle: str, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if needle in b.drv.find_element(By.TAG_NAME, "body").text:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _no_exception(b: Browser) -> None:
    ex = b.js("var e=document.querySelector('[data-testid=\"stException\"]'); return e?e.textContent.slice(0,400):null;")
    assert ex is None, ("Streamlit exception on the page", ex)


def _nav(b: Browser, title: str) -> None:
    ok = b.js("""var T=arguments[0]; var as=document.querySelectorAll('[data-testid="stSidebarNav"] a, [data-testid="stSidebarNavLink"], [data-testid="stSidebar"] a');
        for(var i=0;i<as.length;i++){ if((as[i].textContent||'').trim()===T){ as[i].click(); return true; } } return false;""", title)
    assert ok, f"sidebar nav link {title!r} not found"
    time.sleep(3)


def test_live_mode_fake_and_unreachable(app_fake):
    """Data source (LIVE mode) -> Connect to the fake unit (MRU 91 -> the fake's newest run) -> Live page in LIVE mode ticks (stamps advance,
    figures drawn, no console errors, no overlay); Disconnect -> a custom host the fake treats as unreachable -> an error callout, never a
    Streamlit exception."""
    b = Browser(1920, 1080, "live_fake")
    try:
        b.get(f"{app_fake}/")
        assert _wait_text(b, "Connect", 60), "Data source page did not render"
        time.sleep(1)
        _click_button(b, "Connect")
        assert _wait_text(b, "e65bd4f9", 60) or _wait_text(b, "Following", 5), "the fake unit's run was not followed"
        _no_exception(b)
        b.shot("connected")
        _nav(b, "Live")
        b.wait_panel(); b.wait_made()
        time.sleep(3)
        st0 = b.pjs("return IH.stamp;")
        assert _wait_text(b, "LIVE", 10)
        time.sleep(8)
        st1 = b.pjs("return IH.stamp;")
        assert st1 and st1 != st0, ("live envelope did not advance", st0, st1)
        m = b.wrapper_metrics()
        assert m["toggle"] and m["toggle"]["top"] >= m["iframe"]["bottom"] - 0.5, m
        _no_exception(b)
        b.shot("live_panel")
        assert_no_console_errors(b)
        # unreachable host
        _nav(b, "Data source")
        _click_button(b, "Disconnect", '[data-testid="stSidebar"]')
        assert _wait_text(b, "Connect", 30)
        t0 = time.time()                                                                   # let the post-Disconnect rerun finish re-mounting the page
        while time.time() - t0 < 20 and b.js("""return !!document.querySelector('[data-testid="stStatusWidget"]');"""):
            time.sleep(0.5)
        time.sleep(3)
        for attempt in range(3):                                                           # type the host, confirm the resolved line took it (a re-mount mid-typing loses keys)
            ok = b.js("""var ss=document.querySelectorAll('[data-testid="stExpander"] summary'); for(var i=0;i<ss.length;i++){ if(/Advanced/.test(ss[i].textContent)){ var d=ss[i].parentElement; if(!(d&&d.open)) ss[i].click(); return true; } } return false;""")
            assert ok, "Advanced expander not found"
            time.sleep(1.5)
            inp = b.drv.find_element(By.CSS_SELECTOR, 'input[aria-label="Custom host or IP (overrides the MRU number)"]')
            inp.click(); time.sleep(0.3)                                                    # FOCUS first: after a Live-page visit the keystrokes otherwise land in an unfocused input and never commit
            inp.clear(); inp.send_keys("10.255.255.1"); time.sleep(0.5); inp.send_keys(Keys.ENTER)      # a real Enter KEY: geckodriver types "\n" as a literal into an <input> and Streamlit never commits
            time.sleep(3)
            if "10.255.255.1" in b.drv.find_element(By.TAG_NAME, "body").text:
                break
        else:
            raise AssertionError("custom host never took (resolved line still the MRU convention)")
        _click_button(b, "Connect")
        assert _wait_text(b, "unreachable", 40) or _wait_text(b, "timed out", 5) or _wait_text(b, "OFFLINE", 5) or _wait_text(b, "cannot", 5), "no error message for the unreachable host"
        _no_exception(b)
        b.shot("unreachable")
        assert_no_console_errors(b)
    finally:
        b.quit()


def test_load_flight_then_live(app):
    """Data source in ARCHIVE mode -> "Load flight and open Live view" -> the Live page (paused at the flight start) -> Play -> the clock runs."""
    b = Browser(1920, 1080, "load_flight")
    try:
        b.get(f"{app}/?ds=archive")
        assert _wait_text(b, "Load flight and open Live view", 60)
        time.sleep(1)
        _click_button(b, "Load flight and open Live view")
        b.wait_panel(); b.wait_made()
        assert _wait_text(b, "ARCHIVE", 10) and _wait_text(b, "PAUSED", 5)
        c0 = b.js("return document.querySelector('.ih-top .ih-status').textContent;")
        _click_button(b, "Play replay", '[data-testid="stSidebar"]')
        time.sleep(6)
        c1 = b.js("return document.querySelector('.ih-top .ih-status').textContent;")
        assert "PAUSED" not in c1 and c1 != c0, (c0, c1)
        _no_exception(b)
        m = b.wrapper_metrics()
        assert m["toggle"] and m["toggle"]["top"] >= m["iframe"]["bottom"] - 0.5, m
        b.shot("playing")
        assert_no_console_errors(b)
    finally:
        b.quit()


# ── FULL REPLAY of every flight (user: "test the full replay for bugs and visual issues before showing me") ──────────
FLIGHT_REPLAY = {1: ("07:19:29", "07:27:57"), 2: ("07:55:16", "08:08:00"), 3: ("08:15:17", "08:22:15"), 4: ("08:34:11", "08:43:04")}   # D.replay_bounds
CLOCK_RE = re.compile(r"ARCHIVE\s+F\d\s+(\d\d:\d\d:\d\d)")


def _hms_s(t: str) -> int:
    h, m, s_ = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s_


def _page_clock(b: Browser) -> str | None:
    txt = b.js("return document.body.innerText.slice(0, 4000);")
    m = CLOCK_RE.search(txt or "")
    return m.group(1) if m else None


def _replay_flight(b: Browser, app: str, flight: int, speed: str, sample_s: float, shot_every_replay_s: float, expect_mode: str) -> list[dict]:
    """Play one flight end to end at ``speed`` sampling every ``sample_s`` wall seconds; returns the sample rows (asserts inline)."""
    t0_hms, t1_hms = FLIGHT_REPLAY[flight]
    b.open_live(app, flight=flight, t=t0_hms, play=1)
    if speed != "1×":
        _sidebar_radio(b, "Replay speed", speed)
    time.sleep(2)
    mult = float(speed.rstrip("×"))
    budget = (_hms_s(t1_hms) - _hms_s(t0_hms)) / mult + 90.0
    rows, stamps, t_wall0, last_shot, i = [], [], time.time(), -1e9, 0
    while time.time() - t_wall0 < budget:
        clock = _page_clock(b)
        st = b.pjs("return {stamp:IH.stamp, made:IH.made, reacting:IH.reacting, mode:IH.geo&&IH.geo.mode, velCols:IH.geo&&IH.geo.velCols};")
        m = b.wrapper_metrics()
        row = {"i": i, "wall": round(time.time() - t_wall0, 1), "clock": clock, "stamp": st["stamp"], "mode": st["mode"], "made": st["made"],
               "toggle_gap": round(m["toggle"]["top"] - m["iframe"]["bottom"], 1) if m["toggle"] else None}
        # every figure drawn, the right arrangement, wrapper never overlapped
        assert st["mode"] == expect_mode, row
        for k in ("map", "sep", "err", "vel"):
            assert st["made"].get(k), ("figure not drawn", k, row)
        if m["toggle"]:
            assert m["toggle"]["top"] >= m["iframe"]["bottom"] - 0.5, ("overlay during play", row)
        # no text over any plot area / the last 15 % of the time axis, on every time-series card
        for k in ("err", "vel", "sep"):
            rep = b.pjs(ANNOT_JS, k)
            inside = _inside_plot_hits(rep)
            assert not inside, (flight, clock, k, "annotation inside the plot area", inside[:3])
            band = _right_band_hits(rep)
            assert not band, (flight, clock, k, "annotation over the last 15 % (t = now)", band[:3])
            if k == "vel":
                cols = sorted(s_["x0"] for s_ in rep["subs"])
                if expect_mode == "two":
                    assert st["velCols"] == 3 and len(cols) == 3 and cols[1] - cols[0] > 50, ("velocity cards not side by side", cols, row)
                else:
                    assert len(cols) == 3 and max(cols) - min(cols) < 2, ("velocity cards not stacked", cols, row)
        # a screenshot + flash check every ~shot_every_replay_s of replay time
        if clock is not None and _hms_s(clock) - last_shot >= shot_every_replay_s:
            px = _map_pixels(b, f"f{flight}_{clock.replace(':', '')}")
            assert 8 < px["mean"] < 200, ("map flash frame", row, px)
            row["map_mean"] = round(px["mean"], 1)
            last_shot = _hms_s(clock)
        rows.append(row)
        if st["stamp"]:
            stamps.append(st["stamp"])
        if clock is not None and _hms_s(clock) >= _hms_s(t1_hms) - 2:
            break
        i += 1
        time.sleep(sample_s)
    assert rows and rows[-1]["clock"] is not None and _hms_s(rows[-1]["clock"]) >= _hms_s(t1_hms) - 2, ("replay never reached the end of the flight", rows[-1] if rows else None)
    assert len(set(stamps)) >= max(4, len(rows) // 3), ("the envelope stamp did not keep advancing", len(set(stamps)), len(rows))
    return rows


@pytest.mark.parametrize("flight", [1, 2, 3, 4])
def test_full_replay_flight_two_column(app, flight):
    """Every flight replayed END TO END at 4x in a 2560x1440 browser (two-column mode: map over the velocity strip | separation over the
    error cards; a 1920x1080 browser fits two columns again since 2026-09-14, see test_full_replay_flight1_two_column_1080p; a
    1366x768 laptop runs the stacked mode, see test_full_replay_flight1_stacked_laptop).  Every 5 s wall (~20 s replay): all four figures drawn, envelope stamp advancing, no
    annotation inside any plot area or over the last 15 % of the time axis on the separation / error / velocity cards, the three velocity
    cards side by side, the wrapper never overlapped; a screenshot + map flash check every 60 replay-s."""
    b = Browser(2560, 1440, f"full_f{flight}")
    rows = []
    try:
        rows = _replay_flight(b, app, flight, "4×", 5.0, 60.0, "two")
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", rows)
        b.quit()


def test_full_replay_flight1_ultrawide(app):
    """Flight 1 end to end at 4x on the user's 3796x1879 display (three-column mode: map | separation + stacked velocity cards | error cards)."""
    b = Browser(3796, 1879, "full_f1_ultrawide")
    rows = []
    try:
        rows = _replay_flight(b, app, 1, "4×", 5.0, 60.0, "three")
        host = b.pjs("var v=document.getElementById('velblock'); return v&&v.parentElement&&v.parentElement.className;")
        assert host == "col cm", ("velocity block not in the middle column", host)
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", rows)
        b.quit()


def test_full_replay_flight1_two_column_1080p(app):
    """Flight 1 end to end at 4x in a 1920x1080 browser: with the 2026-09-14 type sizes (32 px headers, 46 px strips, ERR_MIN 462) the
    ~640 px panel fits TWO columns again (map over the velocity strip | separation over the error cards) for the whole flight."""
    b = Browser(1920, 1080, "full_f1_1080p")
    rows = []
    try:
        rows = _replay_flight(b, app, 1, "4×", 5.0, 60.0, "two")
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", rows)
        b.quit()


def test_full_replay_flight1_stacked_laptop(app):
    """Flight 1 end to end at 4x in a 1366x768 browser (laptop): too short for two columns -> the STACKED mode (map / separation / error
    cards / velocity cards, the iframe scrolls) must run cleanly for the whole flight."""
    b = Browser(1366, 768, "full_f1_stacked")
    rows = []
    try:
        rows = _replay_flight(b, app, 1, "4×", 5.0, 60.0, "one")
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", rows)
        b.quit()


# ── 5. "everything (plots, map, images) is jumping on every update" — the page must NEVER move (2026-09-14) ───────────
# Measured cause: the top status strip's text changes every tick (PAUSED, TRACK CHANGED, the track state word, the roles
# label) and the strip WRAPPED to an extra line and back, moving the panel iframe 32-36 px every few seconds at 1920x1080
# and 1366x768.  Fix (ih/theme.py): .ih-top is a column — row1 (wordmark + crumb + badge) at a fixed 1.7rem and the
# .ih-status strip at a FIXED height of --st-rows wrapped lines (2 under 2400 px, 1 above), overflow hidden — plus an
# override that keeps Streamlit's "stale element" fragment dimming at opacity 1.  These tests are the regression net:
# a 100 ms sampler in the TOP document (geometry, the iframe's identity + load count, the status fit, the top bar's
# computed opacity) and a 100 ms sampler INSIDE the panel iframe (per-figure heights), over a 40 s window that crosses
# the ~07:21:57 target-track handover (amber TRACK CHANGED for 10 s) and a Pause / Play round trip.
#
# Firefox does not implement the Layout Instability API (PerformanceObserver.supportedEntryTypes has no 'layout-shift'),
# so the layout-shift stream is a BONUS signal only — the geometric sampler is what actually catches movement here; the
# test records which of the two was live (samples["ls_supported"]).
MOVE_SIZES = [(1366, 768), (1536, 864), (1920, 1080), (2560, 1440)]
MOVE_T = "07:21:40"          # 17 s before the F1 target-track handover 129 -> 177
MOVE_WINDOW_S = 40.0

MOVE_SAMPLER_JS = r"""
var MS = arguments[0];
window.__ihMove = {rows: [], ls: [], loads: 0, gen: 0, lsErr: '', po: 0,
                   support: (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes) || []};
try {
  var po = new PerformanceObserver(function(l){ l.getEntries().forEach(function(e){
      window.__ihMove.ls.push({v: e.value, t: Math.round(e.startTime), inp: !!e.hadRecentInput}); }); });
  po.observe({type: 'layout-shift', buffered: true});
  window.__ihMove.po = 1;
} catch (e) { window.__ihMove.lsErr = String(e); }
function panelFrame(){ var fs = document.querySelectorAll('iframe');
  for (var i = 0; i < fs.length; i++){ if ((fs[i].getAttribute('srcdoc') || '').indexOf('id="map"') >= 0) return fs[i]; }
  return null; }
function r2(v){ return Math.round(v * 100) / 100; }
window.__ihMove.tick = function(){
  var f = panelFrame(), top = document.querySelector('.ih-top'), st = document.querySelector('.ih-top .ih-status');
  var row = {t: Math.round(performance.now()), sy: Math.round(window.scrollY)};
  if (f) {
    if (!f.__ihHooked){ f.__ihHooked = 1; window.__ihMove.gen++; f.addEventListener('load', function(){ window.__ihMove.loads++; }); }
    var r = f.getBoundingClientRect();
    row.itop = r2(r.top); row.ih = r2(r.height); row.iabs = r2(r.top + window.scrollY);
  } else { row.missing = 1; }
  if (top) { row.toph = r2(top.getBoundingClientRect().height); row.op = getComputedStyle(top).opacity; }
  if (st) {
    var sr = st.getBoundingClientRect();
    row.sth = r2(sr.height); row.ssh = st.scrollHeight; row.sch = st.clientHeight;
    row.txt = (st.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200);
    row.out = [].slice.call(st.children).map(function(c){ var k = c.getBoundingClientRect();
        return {by: Math.round(Math.max(0, k.bottom - sr.bottom, sr.top - k.top, k.right - sr.right, sr.left - k.left)),
                tx: (c.textContent || '').trim().slice(0, 30)}; }).filter(function(o){ return o.by > 1; });
  }
  row.gen = window.__ihMove.gen; row.loads = window.__ihMove.loads;
  window.__ihMove.rows.push(row);
};
window.__ihMove.iv = setInterval(window.__ihMove.tick, MS);
window.__ihMove.tick();
return {support: window.__ihMove.support, po: window.__ihMove.po, lsErr: window.__ihMove.lsErr};
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


def _movement_start(b: Browser, ms: int = 100) -> dict:
    """Install the top-document sampler (geometry / iframe identity / status fit / opacity) + the layout-shift observer."""
    return b.js(MOVE_SAMPLER_JS, ms)


def _movement_stop(b: Browser) -> dict:
    return b.js("""try { clearInterval(window.__ihMove.iv); } catch (e) {}
        var m = window.__ihMove || {}; return {rows: m.rows || [], ls: m.ls || [], support: m.support || [], loads: m.loads || 0, gen: m.gen || 0};""")


def _panel_fig_sampler_start(b: Browser, ms: int = 100) -> None:
    b.pjs(PANEL_FIG_SAMPLER_JS, ms)


def _panel_fig_sampler_stop(b: Browser) -> list:
    return b.pjs("try { clearInterval(window.__ihFig.iv); } catch (e) {} return (window.__ihFig || {}).rows || [];")


def _opacity_burst(b: Browser, seconds: float = 6.0, ms: int = 50) -> list:
    """Item 3: the top bar's computed opacity every ``ms`` for ``seconds`` of PLAY (Streamlit dims a running fragment's
    elements after 0.5 s — that dimming reads as the page flashing once a second and must never reach our chrome)."""
    b.js(OPACITY_BURST_JS, ms)
    time.sleep(seconds)
    return b.js("try { clearInterval(window.__ihOp.iv); } catch (e) {} return (window.__ihOp || {}).vals || [];")


def _distinct(rows: list, key: str) -> list:
    return sorted({r[key] for r in rows if r.get(key) is not None})


def _status_text(b: Browser) -> str:
    try:
        return b.js("var s = document.querySelector('.ih-top .ih-status'); return s ? s.textContent : '';") or ""
    except Exception:
        return ""


def _wait_status(b: Browser, needle: str, timeout: float = 45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if needle in _status_text(b):
            return True
        time.sleep(0.25)
    return False


def _status_fit_failures(rows: list) -> list:
    """Rows where the fixed-height status box did not fit its content (scrollHeight > clientHeight + 1, or a child span
    sticking out of the row's rect) — the box must never grow AND never clip a part."""
    bad = []
    for r in rows:
        if r.get("sch") is None:
            continue
        if r["ssh"] > r["sch"] + 1 or r.get("out"):
            bad.append({"t": r["t"], "ssh": r["ssh"], "sch": r["sch"], "out": r.get("out"), "txt": r.get("txt")})
    return bad


def _assert_never_moved(rows: list, ls: list, tag: str) -> dict:
    """The whole point: every sampled geometry value is IDENTICAL (0 px variation), the iframe is the same element and
    never reloaded, and no layout shift was reported."""
    got = {k: _distinct(rows, k) for k in ("itop", "iabs", "ih", "toph", "sth", "sy")}
    for k in ("itop", "iabs", "ih", "toph", "sth"):
        assert len(got[k]) == 1, (tag, f"{k} moved", got[k][:8], got)
    assert len(got["sy"]) == 1, (tag, "the page scrolled during the sample", got["sy"][:8])
    assert _distinct(rows, "gen") == [1], (tag, "the panel iframe element was re-created", _distinct(rows, "gen"))
    assert _distinct(rows, "loads") == [0], (tag, "the panel iframe reloaded", _distinct(rows, "loads"))
    assert not [r for r in rows if r.get("missing")], (tag, "the panel iframe disappeared from the DOM")
    shifts = [e for e in ls if float(e.get("v") or 0) > 0.0005]
    assert not shifts, (tag, "layout-shift entries", shifts[:6])
    return got


@pytest.mark.parametrize("w,h", MOVE_SIZES)
def test_page_never_moves(app, w, h):
    """User: "everything (plots, map, images) is jumping on every update / flashing".  Flight 1 from 07:21:40 at 1x, 40 s
    of 100 ms samples across the ~07:21:57 target-track handover (amber TRACK CHANGED for 10 s) and a Pause / Play round
    trip inside that window — the two status changes that used to re-wrap the strip.  Asserts: the panel iframe's top and
    height, the .ih-top height and the .ih-status height never change by even a fraction of a pixel; the iframe is never
    re-created and never reloads; no layout-shift entry > 0.0005; every figure div inside the panel keeps its height; the
    top bar's computed opacity is 1 throughout (no fragment "stale" dimming); and the fixed-height status box neither
    overflows nor clips a part."""
    b = Browser(w, h, f"nomove_{w}x{h}")
    rows, figs, ls, info, ops = [], [], [], {}, []
    try:
        b.open_live(app, flight=1, t=MOVE_T, play=1)
        time.sleep(1.5)
        info = _movement_start(b, 100)
        _panel_fig_sampler_start(b, 100)
        t_start = time.time()
        ops = _opacity_burst(b, 6.0, 50)                                        # item 3, during play
        saw_change = _wait_status(b, "TRACK CHANGED", 45.0)                     # the handover flash
        _click_button(b, "Pause replay", '[data-testid="stSidebar"]')           # ... and PAUSED on top of it
        paused = _wait_status(b, "PAUSED", 10.0)
        time.sleep(3.0)
        _click_button(b, "Play replay", '[data-testid="stSidebar"]')
        while time.time() - t_start < MOVE_WINDOW_S:
            time.sleep(0.5)
        got = _movement_stop(b)
        rows, ls = got["rows"], got["ls"]
        figs = _panel_fig_sampler_stop(b)

        assert len(rows) >= 250, ("too few samples for a 40 s window", len(rows))
        assert saw_change, "the 07:21:57 TRACK CHANGED flash never appeared — the sample missed the handover"
        assert paused and any("PAUSED" in (r.get("txt") or "") for r in rows), "the status never showed PAUSED"
        assert len({r.get("txt") for r in rows}) >= 5, "the status text never changed — nothing was under test"
        _assert_never_moved(rows, ls, f"{w}x{h}")
        assert set(r.get("op") for r in rows) == {"1"}, ("the top bar was dimmed", sorted(set(r.get("op") for r in rows)))
        assert ops and set(ops) == {"1/1"}, ("fragment dimming during play (.ih-top / main block opacity)", sorted(set(ops))[:6])
        assert not _status_fit_failures(rows), ("the status box overflowed / clipped a part", _status_fit_failures(rows)[:4])
        assert len(figs) >= 250, ("too few panel samples", len(figs))
        for fid in ("map", "sep", "err"):
            vals = _distinct(figs, fid)
            assert len(vals) == 1, (f"{fid} figure height changed", vals[:8])
        assert len(_distinct(figs, "vel")) <= 1, ("velocity figure height changed", _distinct(figs, "vel")[:8])
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", {"info": info, "ls_supported": "layout-shift" in (info.get("support") or []), "ls": ls,
                             "opacity_burst": sorted(set(ops)), "rows": rows, "figs": figs})
        b.quit()


@pytest.mark.parametrize("w,h", MOVE_SIZES)
def test_status_row_fits_fixed_box(app, w, h):
    """Item 2: at every window size x "Text size" the FIXED-height .ih-status box must FIT its content for the whole 40 s
    window (scrollHeight <= clientHeight + 1 and every child span fully inside the row's rect) — the box may never grow
    (that is what moved the page) and may never clip a part.  Same worst case as test_page_never_moves: the handover
    flash with PAUSED on top of it.  Geometry is re-checked here too (it must not move at Large / X-Large either)."""
    b = Browser(w, h, f"statusfit_{w}x{h}")
    per = {}
    try:
        for tx in ("Normal", "Large", "X-Large"):
            b.open_live(app, flight=1, t=MOVE_T, play=1)
            _sidebar_radio(b, "Text size", tx)
            b.wait_panel(); b.wait_made()
            time.sleep(1.5)
            info = _movement_start(b, 100)
            t_start = time.time()
            saw_change = _wait_status(b, "TRACK CHANGED", 45.0)
            _click_button(b, "Pause replay", '[data-testid="stSidebar"]')
            _wait_status(b, "PAUSED", 10.0)
            time.sleep(3.0)
            _click_button(b, "Play replay", '[data-testid="stSidebar"]')
            while time.time() - t_start < MOVE_WINDOW_S:
                time.sleep(0.5)
            got = _movement_stop(b)
            rows, ls = got["rows"], got["ls"]
            bad = _status_fit_failures(rows)
            per[tx] = {"n": len(rows), "fit_failures": bad[:6], "n_fit_failures": len(bad), "saw_change": saw_change,
                       "sth": _distinct(rows, "sth"), "itop": _distinct(rows, "itop"), "rows": rows}
            b.shot(f"statusfit_{tx}")
            assert len(rows) >= 250, (tx, "too few samples", len(rows))
            assert saw_change, (tx, "the sample missed the 07:21:57 handover")
            assert not bad, (tx, f"{w}x{h}", "the status box overflowed / clipped a part", bad[:4])
            _assert_never_moved(rows, ls, f"{w}x{h} {tx}")
        assert_no_console_errors(b)
    finally:
        _dump(b, "samples", per)
        b.quit()


# ── 2026-09-15: the user's 1366x768 laptop (viewport ~1366x682, sidebar open) ────────────────────────────────────────
LAPTOP_JS = r"""
var out = {innerH: window.innerHeight, innerW: window.innerWidth};
var fr = null;
document.querySelectorAll('iframe').forEach(function(f){ if(!fr && ((f.getAttribute('srcdoc')||'').indexOf('id="map"')>=0)) fr = f; });
out.iframe_top = fr ? Math.round(fr.getBoundingClientRect().top * 10) / 10 : null;
var hd = document.querySelector('[data-testid="stHeader"]');
out.header_h = hd ? Math.round(hd.getBoundingClientRect().height * 10) / 10 : null;
var blk = document.querySelector('[data-testid="stMainBlockContainer"]');
var first = blk ? blk.querySelector('[data-testid="stElementContainer"]:not([style*="display: none"])') : null;
out.first_el_top = first ? Math.round(first.getBoundingClientRect().top * 10) / 10 : null;
// style-only element containers (the <style> markdown injections) still in flow?
var styleEls = 0, styleShown = 0;
document.querySelectorAll('[data-testid="stElementContainer"]').forEach(function(ec){
  var st = ec.querySelector('[data-testid="stMarkdownContainer"] > style');
  if(st){ styleEls++; if(getComputedStyle(ec).display !== 'none') styleShown++; } });
out.style_containers = styleEls; out.style_containers_in_flow = styleShown;
// MEASUREMENT SPACE toggle + the More expander
var tog = null, more = null;
document.querySelectorAll('button').forEach(function(bt){ var r = bt.getBoundingClientRect();
  if(r.height > 0 && /MEASUREMENT SPACE/i.test(bt.textContent||'')) tog = r; });
document.querySelectorAll('[data-testid="stExpander"] summary').forEach(function(sm){
  if(/More details/i.test(sm.textContent||'')) more = sm.getBoundingClientRect(); });
out.meas_toggle_top = tog ? Math.round(tog.top * 10) / 10 : null;
out.more_top = more ? Math.round(more.top * 10) / 10 : null;
// footer: height + how many LINE BOXES its text really occupies (range rects per text node)
var ft = document.querySelector('.ih-footer');
out.footer_h = ft ? Math.round(ft.getBoundingClientRect().height * 10) / 10 : null;
out.footer_lines = null;
if(ft){ var tops = {};      // ONE set of line-box tops for the WHOLE footer: two spans sharing a line is ONE line
  ft.querySelectorAll('span').forEach(function(sp){ var rg = document.createRange(); rg.selectNodeContents(sp);
    Array.prototype.forEach.call(rg.getClientRects(), function(r){ if(r.width > 0) tops[Math.round(r.top / 4) * 4] = 1; }); });
  out.footer_lines = Object.keys(tops).length;
  out.footer_clipped = ft.scrollHeight > ft.clientHeight + 1;
  out.footer_font = getComputedStyle(ft).fontSize;
  out.footer_text_len = (ft.textContent||'').length; }
// sidebar: page-nav label clipping + total scroll length
var nav = [];
document.querySelectorAll('[data-testid="stSidebarNav"] a').forEach(function(a){ var p = a.querySelector('p');
  nav.push({txt: (a.textContent||'').trim(), a_c: a.clientHeight, a_s: a.scrollHeight,
            p_c: p ? p.clientHeight : null, p_s: p ? p.scrollHeight : null}); });
out.nav = nav;
var sb = document.querySelector('[data-testid="stSidebar"]');
out.sidebar_scroll = 0; out.sidebar_client = 0;
if(sb){ var best = sb;
  sb.querySelectorAll('*').forEach(function(e){ if(e.scrollHeight > best.scrollHeight) best = e; });
  out.sidebar_scroll = best.scrollHeight; out.sidebar_client = best.clientHeight; }
var exp = [];
document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stExpander"] summary').forEach(function(sm){
  exp.push({label: (sm.textContent||'').trim().slice(0, 40), open: !!(sm.parentElement && sm.parentElement.open)}); });
out.sidebar_expanders = exp;
return out;
"""

# the two rules under test, undone in the live page -> the BEFORE geometry in the same session
LAPTOP_UNDO_CSS = ('[data-testid="stHeader"]{height:auto !important;min-height:3.75rem !important;}'
                   '[data-testid="stMainBlockContainer"] [data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"] > style:only-child),'
                   '[data-testid="stSidebar"] [data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"] > style:only-child){display:block !important;}'
                   '[data-testid="stSidebarNav"] a p{white-space:normal !important;}'
                   '.ih-footer{display:flex !important;font-size:.85rem !important;line-height:1.4 !important;letter-spacing:.08em !important;}'
                   '.ih-footer span + span::before{content:"" !important;}')


def _undo_laptop_css(b: Browser, on: bool) -> None:
    """Inject / remove the override.  It goes at the END OF THE BODY: our theme CSS is a st.markdown <style> inside the body,
    so a <style> in <head> loses the cascade tie-break to it."""
    b.js("""var id='ih-undo-laptop', el=document.getElementById(id);
        if(arguments[1]){ if(!el){ el=document.createElement('style'); el.id=id; document.body.appendChild(el); } el.textContent=arguments[0]; }
        else if(el){ el.remove(); }""", LAPTOP_UNDO_CSS, bool(on))
    time.sleep(0.6)


@pytest.mark.parametrize("tx", ["Normal", "Large"])
def test_laptop_1366x768_fits_the_one_screen_panel(app, tx):
    """The user's laptop (1366x768 -> viewport ~1366x682, sidebar open), Text size Normal and Large.  Measures, in ONE
    session, the page WITH the 2026-09-15 fixes and with them undone by an injected override (the "before"):
      A  the panel iframe's top (the 60/69 px empty header band + three zero-height style-only element containers, each
         eating a 16 px vertical-block gap),
      B  the MEASUREMENT SPACE toggle's top (it has to be ON screen) and the footer's real line count,
      D  the sidebar page-nav labels ("Data source" / "Live") clipped inside a 28 px overflow-hidden box, and the
         sidebar's total scroll length.
    Nothing may be clipped: the footer's scrollHeight must still fit its box."""
    b = Browser(1366, 768, f"laptop1366_{tx}")
    got = {}
    try:
        b.open_live(app, flight=1, t=F1_PASS_T)
        _sidebar_radio(b, "Text size", tx)          # the shipped default preset is Desktop 1080p -> Large text, so set BOTH explicitly
        b.wait_panel(); b.wait_made()
        time.sleep(1.5)
        after = b.js(LAPTOP_JS)
        _undo_laptop_css(b, True)
        before = b.js(LAPTOP_JS)
        _undo_laptop_css(b, False)
        again = b.js(LAPTOP_JS)
        got = {"text_scale": tx, "after": after, "before_overridden": before, "after_again": again}
        b.shot("laptop")
        assert after["innerW"] == 1366 and 660 <= after["innerH"] <= 700, after
        # (A) the header band and the style-only containers are out of the flow
        assert after["style_containers"] >= 2 and after["style_containers_in_flow"] == 0, after
        assert after["header_h"] <= 42, after["header_h"]        # 2.25rem: 36 px at Normal, 41.4 px at Large (rem follows the text scale)
        # the fix is worth a measured ~48 px; the absolute top also carries the status box, which gained a THIRD line of
        # allowance on 2026-09-15 (row 1 may wrap + the row-2 update counter), so assert the DELTA and a loose ceiling
        assert after["iframe_top"] <= (170 if tx == "Normal" else 190), (after["iframe_top"], before["iframe_top"])
        assert after["iframe_top"] < before["iframe_top"] - 40, (after["iframe_top"], before["iframe_top"])
        # (B) the quad toggle is on screen and the footer is 2 lines and NOT clipped
        assert after["meas_toggle_top"] is not None and after["meas_toggle_top"] < after["innerH"] + 4, after["meas_toggle_top"]
        assert after["footer_lines"] is not None and after["footer_lines"] <= 2, (after["footer_lines"], after["footer_h"])
        assert after["footer_lines"] < before["footer_lines"], (after["footer_lines"], before["footer_lines"])
        assert not after["footer_clipped"], after
        # (D) the page-nav labels are no longer cut in half (the <a> is the overflow-hidden box)
        assert after["nav"], "no sidebar page nav"
        for n in after["nav"]:
            assert n["a_s"] <= n["a_c"] + 1, (n, before["nav"])
        assert after["sidebar_scroll"] > 0
        assert [e["label"] for e in after["sidebar_expanders"]][:1] and after["sidebar_expanders"][0]["label"].endswith("Display"), after["sidebar_expanders"]   # Streamlit prefixes the icon ligature text
        # the override is a pure overlay: removing it restores the fixed geometry exactly
        assert again["iframe_top"] == after["iframe_top"], (after["iframe_top"], again["iframe_top"])
        # (D) the LAPTOP preset starts with "Display" collapsed -> a far shorter sidebar
        _sidebar_radio(b, "Screen size", "Laptop")
        b.wait_panel(); b.wait_made()
        time.sleep(1.0)
        lap = b.js(LAPTOP_JS)
        got["laptop_preset"] = lap
        assert [e["open"] for e in lap["sidebar_expanders"]][:1] == [False], lap["sidebar_expanders"]
        assert lap["sidebar_scroll"] < after["sidebar_scroll"], (lap["sidebar_scroll"], after["sidebar_scroll"])
        assert_no_console_errors(b)
    finally:
        _dump(b, "laptop_geometry", got)
        b.quit()


SIDEBAR_LABEL_JS = r"""
var sb = document.querySelector('[data-testid="stSidebar"]');
var sr = sb.getBoundingClientRect(), out = [];
sb.querySelectorAll('[data-testid="stWidgetLabel"]').forEach(function(wl){
  var txt = (wl.textContent||'').trim(); if(!txt) return;
  var inner = wl.querySelector('[data-testid="stMarkdownContainer"]') || wl;
  var r = inner.getBoundingClientRect();
  // the help icon (tooltip target), when the widget has one
  var ic = wl.querySelector('[data-testid="stTooltipHoverTarget"], [data-testid="stTooltipIcon"], svg');
  var ir = ic ? ic.getBoundingClientRect() : null;
  out.push({txt: txt.slice(0, 48), cw: Math.round(inner.clientWidth), sw: Math.round(inner.scrollWidth),
            ch: Math.round(inner.clientHeight), sh: Math.round(inner.scrollHeight),
            right: Math.round(r.right * 10) / 10, icon_right: ir ? Math.round(ir.right * 10) / 10 : null,
            sb_right: Math.round(sr.right * 10) / 10, ws: getComputedStyle(inner).whiteSpace, ov: getComputedStyle(inner).overflow});
});
sb.querySelectorAll('[data-testid="stExpander"] summary').forEach(function(sm){
  var r = sm.getBoundingClientRect();
  out.push({txt: 'summary: ' + (sm.textContent||'').trim().slice(0, 40), cw: Math.round(sm.clientWidth), sw: Math.round(sm.scrollWidth),
            ch: Math.round(sm.clientHeight), sh: Math.round(sm.scrollHeight), right: Math.round(r.right * 10) / 10,
            icon_right: null, sb_right: Math.round(sr.right * 10) / 10, ws: getComputedStyle(sm).whiteSpace, ov: getComputedStyle(sm).overflow});
});
return out;
"""


@pytest.mark.parametrize("tx", ["Normal", "Large", "X-Large"])
def test_sidebar_labels_never_clip(app, tx):
    """User screenshot (2026-09-15, 1366x768 at Large): the slider label "Closest-approach gate (m)" lost its "(m)" AND its
    help icon off the sidebar's right edge.  Streamlit 1.63 lays the label out as a flex row (text + help icon) whose text
    box is nowrap / overflow:hidden.  For EVERY sidebar widget label (and every sidebar expander summary) at Normal / Large /
    X-Large: the text box must not overflow horizontally (scrollWidth <= clientWidth + 1) and both its right edge and its
    help icon's right edge must be inside the sidebar rect."""
    b = Browser(1366, 768, f"sidelabels_{tx}")
    rows = []
    try:
        b.open_live(app, flight=1, t=F1_PASS_T)
        _sidebar_radio(b, "Text size", tx)
        b.wait_panel(); b.wait_made()
        time.sleep(1.0)
        rows = b.js(SIDEBAR_LABEL_JS)
        b.shot("sidelabels")
        assert len(rows) >= 8, ("too few sidebar labels found", rows)
        clipped = [r for r in rows if r["sw"] > r["cw"] + 1]
        assert not clipped, (tx, "label text clipped horizontally", clipped)
        outside = [r for r in rows if r["right"] > r["sb_right"] + 0.5 or (r["icon_right"] or 0) > r["sb_right"] + 0.5]
        assert not outside, (tx, "label / help icon outside the sidebar", outside)
        tall = [r for r in rows if r["sh"] > r["ch"] + 1]
        assert not tall, (tx, "label text clipped vertically", tall)
    finally:
        _dump(b, "sidebar_labels", {"text_scale": tx, "rows": rows})
        b.quit()


# ── 2026-09-15: navigation / first-paint TIMING against the REAL unit ────────
# User report: "after Connect the Live tab is blank for 30-60 s" and "swapping tabs takes 1 min".
# These two tests time the operator's actual gestures in a real browser; they need the live MRU
# (skipped when its mongo port is not reachable from this host).
REAL_MRU = int(os.environ.get("IH_E2E_MRU", "91"))
REAL_HOST = os.environ.get("IH_E2E_MRU_HOST", f"10.1{REAL_MRU:02d}.28.205")


def _mru_up(host: str = REAL_HOST, port: int = 27017, timeout: float = 4.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _nav_click(b: Browser, title: str) -> None:
    """Click a sidebar nav link and return AT ONCE (no settling sleep) so the caller can time the paint."""
    ok = b.js("""var T=arguments[0]; var as=document.querySelectorAll('[data-testid="stSidebarNav"] a, [data-testid="stSidebarNavLink"], [data-testid="stSidebar"] a');
        for(var i=0;i<as.length;i++){ if((as[i].textContent||'').trim()===T){ as[i].click(); return true; } } return false;""", title)
    assert ok, f"sidebar nav link {title!r} not found"


def _wait_for(b: Browser, js: str, timeout: float, poll: float = 0.15) -> float | None:
    """Seconds until ``js`` returns truthy in the top document, None on timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if b.js(js):
                return time.time() - t0
        except Exception:
            pass
        time.sleep(poll)
    return None


CHROME_JS = ("var st=document.querySelector('.ih-status,.ih-top,[data-testid=\"stMainBlockContainer\"] .ih-topbar');"
             "var fr=document.querySelectorAll('iframe');var pan=false;"
             "for(var i=0;i<fr.length;i++){var sd=fr[i].getAttribute('srcdoc')||'';if(sd.indexOf('id=\"map\"')>=0){pan=true;}}"
             "return !!st && pan;")
DS_JS = "return (document.body.textContent||'').indexOf('DATA SOURCE')>=0;"
# FIRST DATA on screen: the status row carries "N trk/s" only on an OK live snapshot (and "#<id>" once a TARGET track
# exists — a run with no MAVLink truth never gets one, so either counts).
TRACK_JS = ("var e=document.querySelector('.ih-status');var t=e?(e.textContent||''):'';"
            "return /trk[/]s/.test(t)||/#[0-9]{2,}/.test(t);")
TRUTH_JS = "return /TGT[^<]*mav/i.test(document.body.textContent||'');"


def test_nav_timing_real_unit(app):
    """Data source -> Connect (real MRU) -> Live: the CHROME (top bar + panel iframe) must paint < 5 s after the nav click;
    then report the time to the first radar track / MAVLink truth; then Live <-> Data source three times, each switch < 3 s."""
    if not _mru_up():
        pytest.skip(f"{REAL_HOST}:27017 unreachable")
    b = Browser(1920, 1080, "nav_timing")
    rec: dict = {}
    try:
        b.get(f"{app}/")
        assert _wait_text(b, "Connect", 60), "Data source page did not render"
        time.sleep(1)
        assert _wait_for(b, "return !!Array.from(document.querySelectorAll('button')).find(function(x){return (x.textContent||'').trim()==='Connect';});", 60), "no Connect button"
        time.sleep(1.5)
        t0 = time.time()
        _click_button(b, "Connect")
        got = _wait_for(b, "return /run collections|Run to follow|NO REPLY|no run collections/.test(document.body.textContent||'');", 120)
        rec["connect_s"] = round(time.time() - t0, 2)
        if got is None:
            b.shot("no_runs")
            print("BODY AFTER CONNECT:", (b.js("return (document.body.innerText||'').slice(0,2500);") or "").replace("\n", " | "))
        assert got is not None, f"Connect produced no outcome in 120 s (rec={rec})"
        assert "NO REPLY" not in (b.js("return document.body.textContent||'';") or ""), "the unit did not answer the probe"

        t0 = time.time()
        _nav_click(b, "Live")
        rec["live_chrome_s"] = _wait_for(b, CHROME_JS, 90.0)
        assert rec["live_chrome_s"] is not None, "the Live page never painted its chrome"
        rec["first_data_s"] = _wait_for(b, TRACK_JS, 120.0)
        rec["first_truth_s"] = _wait_for(b, TRUTH_JS, 5.0)
        rec["status_text"] = (b.js("var e=document.querySelector('.ih-status');return e?(e.textContent||''):'NO .ih-status';") or "")[:400]
        rec["callouts"] = b.js("""return Array.prototype.map.call(document.querySelectorAll('.ih-callout'),function(x){return (x.textContent||'').slice(0,120);});""")
        rec["cadence_caption"] = b.js("""var c=document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stCaptionContainer"]');
            for(var i=0;i<c.length;i++){ if(/ticks every/.test(c[i].textContent||'')) return c[i].textContent; } return '';""")
        rec["last_data_side"] = b.js("""var d=document.querySelectorAll('[data-testid="stSidebar"] .ih-kv dd');
            return d.length? Array.prototype.map.call(d,function(x){return (x.textContent||'').trim();}).join(' ~ ') : '';""")
        b.shot("nav_timing_live")

        switches = []
        for i in range(3):
            t0 = time.time()
            _nav_click(b, "Data source")
            ds = _wait_for(b, DS_JS, 90.0)
            t0b = time.time()
            _nav_click(b, "Live")
            lv = _wait_for(b, CHROME_JS, 90.0)
            switches.append((ds, lv))
        rec["switches"] = switches
        _no_exception(b)
    finally:
        try:
            with open(os.path.join(SHOTS, "nav_timing.json"), "w") as f:
                json.dump(rec, f, indent=1, default=str)
        except Exception:
            pass
        print("NAV TIMING:", rec)
        b.quit()
    assert rec["live_chrome_s"] < 5.0, f"Live chrome took {rec['live_chrome_s']:.1f} s (must be < 5 s): {rec}"
    for i, (ds, lv) in enumerate(rec["switches"]):
        assert ds is not None and ds < 3.0, f"switch {i} -> Data source took {ds} s: {rec}"
        assert lv is not None and lv < 3.0, f"switch {i} -> Live took {lv} s: {rec}"
