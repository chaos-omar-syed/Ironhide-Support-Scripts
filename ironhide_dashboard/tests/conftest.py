"""Shared pytest configuration.

* the ``slow`` marker (tests/e2e_browser.py, headless-browser scenarios);
* SKIP (never fail) the suites that need what a clean public checkout may not have:
    - the built-in 8/28 quickdump day (``$IH_QUICKDUMP_DIR``, default ``data/2026-08-28`` — range data, not shipped):
      every module except the data-free ones in ``DATA_FREE`` is skipped without it;
    - chaos-spa (the optional official grader): ``test_velocity_plumbing.py`` and every test with ``spa`` in its
      name assert spa-graded numbers and are skipped when ``import spa`` fails (or ``IH_NO_SPA=1``).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA_FREE = {"test_palette.py", "test_panel_js.py", "test_liveserver.py", "test_portable.py"}
SPA_ONLY_MODULES = {"test_velocity_plumbing.py"}
# tests that assert chaos-spa's GATING semantics (graded vs ungraded split, coast-only windows empty) — the legacy grader
# grades every published row, so these are spa-only by design
SPA_ONLY_TESTS = {"test_error_panel_cards_titles_readouts_and_bridged_ungraded", "test_engine_tiny_or_coast_only_window_is_empty_not_error",
                  "test_error_panel_bridges_short_coasts_and_breaks_only_on_dropouts"}


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: real-browser end-to-end scenario (minutes, starts Firefox)")
    config.addinivalue_line("markers", "needs_data: needs the built-in 8/28 quickdump day (IH_QUICKDUMP_DIR)")
    config.addinivalue_line("markers", "needs_spa: needs chaos-spa (optional official grader)")


def pytest_collection_modifyitems(config, items):
    import ih
    from ih import data as D

    have_data, have_spa = D.has_builtin(), ih.spa_available()
    skip_data = pytest.mark.skip(reason=f"built-in 8/28 quickdump day not present at {D.BASE} (IH_QUICKDUMP_DIR); data is not shipped")
    skip_spa = pytest.mark.skip(reason="chaos-spa not importable (optional official grader; IH_NO_SPA or not installed)")
    for it in items:
        mod = os.path.basename(str(it.fspath))
        if not have_data and mod not in DATA_FREE:
            it.add_marker(skip_data)
        if not have_spa and (mod in SPA_ONLY_MODULES or it.name.split("[")[0] in SPA_ONLY_TESTS or "spa" in it.name.lower()):
            it.add_marker(skip_spa)
