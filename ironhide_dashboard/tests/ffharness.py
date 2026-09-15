"""Headless-Firefox harness for the panel JS (no node on this box, but Firefox is installed).

serve(dir) starts a tiny HTTP server (thread) that
  * serves files from ``dir`` (the harness / preview pages + plotly.min.js),
  * GET /report?j=<json>   records a JSON report from the page (fetch(..., {mode:"no-cors"}) from the page),
  * GET /hold              blocks until a report arrived (or ``hold_s``) — an <img src="/hold"> in the page
                           keeps the document's load event (and so Firefox's --screenshot) waiting until the
                           page's async work is done.
shot(url, w, h, out) runs ``firefox --headless --screenshot`` with a throw-away profile and returns the PNG path.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# plotly.min.js for the headless page: the same resolution as the panel server (file / plotly package / CDN)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from ih import liveserver as _LS  # noqa: E402

PLOTLY_JS = _LS.PLOTLY_JS
PLOTLY_BYTES = _LS.load_plotly_js()
FIREFOX = shutil.which("firefox")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Harness:
    def __init__(self, root: str, hold_s: float = 40.0):
        self.root, self.hold_s = root, hold_s
        self.reports: list[dict] = []
        self.got = threading.Event()
        self.port = free_port()
        h = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):   # Firefox already closed (screenshot taken)
                    pass

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/report":
                    try:
                        h.reports.append(json.loads(unquote((parse_qs(u.query).get("j") or ["{}"])[0])))
                    except Exception as ex:  # noqa: BLE001
                        h.reports.append({"error": str(ex)})
                    h.got.set()
                    return self._send(200, b"ok", "text/plain")
                if u.path == "/hold":
                    h.got.wait(h.hold_s)
                    # a 1x1 transparent GIF: the <img> load completes -> the document's load event fires
                    return self._send(200, bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"), "image/gif")
                if u.path == "/plotly.min.js":
                    return self._send(200, PLOTLY_BYTES or b"", "application/javascript")
                p = os.path.normpath(os.path.join(h.root, u.path.lstrip("/")))
                if not p.startswith(os.path.abspath(h.root)) or not os.path.isfile(p):
                    return self._send(404, b"nope", "text/plain")
                ct = {"html": "text/html; charset=utf-8", "js": "application/javascript", "png": "image/png", "css": "text/css"}.get(p.rsplit(".", 1)[-1], "application/octet-stream")
                with open(p, "rb") as f:
                    return self._send(200, f.read(), ct)

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def shot(url: str, w: int, h: int, out: str, timeout: float = 90.0) -> str | None:
    """Headless Firefox screenshot of ``url`` at a w×h window; None when Firefox is unavailable / fails."""
    if not FIREFOX:
        return None
    prof = tempfile.mkdtemp(prefix="ihff_")
    try:
        # user.js: no first-run pages / update checks, and allow the page's fetch from an http origin
        with open(os.path.join(prof, "user.js"), "w") as f:
            f.write('user_pref("browser.shell.checkDefaultBrowser", false);\nuser_pref("app.update.enabled", false);\n'
                    'user_pref("datareporting.policy.firstRunURL", "");\nuser_pref("browser.startup.homepage_override.mstone", "ignore");\n'
                    'user_pref("webgl.disabled", true);\n')
        cmd = [FIREFOX, "--headless", "--no-remote", "--profile", prof, f"--window-size={int(w)},{int(h)}", "--screenshot", out, url]
        t0 = time.time()
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        return out if os.path.exists(out) and time.time() - t0 < timeout else None
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(prof, ignore_errors=True)
