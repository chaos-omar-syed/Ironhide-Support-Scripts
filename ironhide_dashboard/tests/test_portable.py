"""Portability checks for the public checkout: the package imports with NO PYTHONPATH / chaos-spa / data, the archive
index degrades to an empty state (no traceback), the panel server always has a plotly.min.js to serve, and the engine's
in-house bistatic geometry agrees with the formulas.  Each subprocess gets its own environment (ih.data reads the
env at import time)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PY = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(code: str, **env) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    e.update({"IH_LIVE_PORT": str(_free_port()), "PYTHONPATH": ""}, **env)
    return subprocess.run([PY, "-c", textwrap.dedent(code)], cwd=ROOT, env=e, capture_output=True, text=True, timeout=240)


def test_imports_without_pythonpath_or_grader():
    r = _run("""
        import ih, ih.data, ih.engine, ih.plots, ih.liveserver, ih.feed, ih.archive, ih.theme
        import corr_lib, live_correlator
        assert not ih.spa_available()
        try:
            import spa_errors
        except ImportError:
            pass
        else:
            raise SystemExit("spa_errors imported although spa is blocked")
        print("OK")
    """, IH_NO_SPA="1")
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


def test_empty_state_without_data(tmp_path):
    r = _run("""
        import os
        from streamlit.testing.v1 import AppTest
        from ih import data as D
        assert not D.has_builtin() and D.flight_numbers() == [] and D.FLIGHT_WINDOWS == {} or D.refresh_registry() == {}
        for page in ("app.py", "views/3_data_source.py", "views/1_live.py"):
            at = AppTest.from_file(page, default_timeout=120)
            at.session_state["show_sat"] = False
            at.run()
            ex = [f"{e.type}: {e.message}" for e in at.exception]
            assert not ex, (page, ex)
            md = "\\n".join(m.value for m in at.markdown)
            if page != "views/1_live.py":
                assert "NO ARCHIVE DATA" in md or "No archive flights" in md or "DATA SOURCE" in md, page
            assert at.session_state["source"] == "live", (page, at.session_state["source"])
        D.set_flight  # the archive API still exists
        print("OK")
    """, IH_QUICKDUMP_DIR=str(tmp_path / "nonexistent"), IH_ARCHIVE_ROOT=str(tmp_path / "archives"), IH_NO_SPA="1")
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_plotly_js_always_available():
    from ih import liveserver as LS

    js = LS.load_plotly_js()
    assert js and js[:200].lstrip().startswith(b"/**") and b"plotly.js v" in js[:200]


def test_bistatic_fallback_matches_formulas():
    """The in-house fallback in ih.engine.bistatic_series must agree with |Tx-T| + |Rx-T| - |Tx-Rx| and the opening rate."""
    r = _run("""
        import numpy as np
        from ih import engine as E
        E_, N_, U_ = np.array([1000.0, -500.0]), np.array([2000.0, 300.0]), np.array([100.0, 50.0])
        vE, vN, vU = np.array([10.0, -3.0]), np.array([-20.0, 4.0]), np.array([1.0, 0.0])
        tx = np.array([300.0, -200.0, 5.0])
        rng, rr = E.bistatic_series(E_, N_, U_, vE, vN, vU, tx_enu=tx)
        P = np.column_stack([E_, N_, U_]); V = np.column_stack([vE, vN, vU])
        exp = np.linalg.norm(P - tx, axis=1) + np.linalg.norm(P, axis=1) - np.linalg.norm(tx)
        assert np.allclose(rng, exp), (rng, exp)
        a, b = P - tx, P
        exp_rr = (a * V).sum(1) / np.linalg.norm(a, axis=1) + (b * V).sum(1) / np.linalg.norm(b, axis=1)
        assert np.allclose(rr, exp_rr), (rr, exp_rr)
        mono, _ = E.bistatic_series(E_, N_, U_)
        assert np.allclose(mono, 2 * np.linalg.norm(P, axis=1))
        print("OK")
    """, IH_NO_SPA="1")
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]
