"""Ironhide test dashboard — shared package.

Importing ``ih`` makes the dashboard self-contained:

* ``ih/vendor`` (corr_lib, spa_errors, live_correlator — the track_correlation helpers the dashboard
  uses, vendored) is put on ``sys.path`` RELATIVE to this file, so ``import corr_lib`` works from any
  checkout without PYTHONPATH.
* ``IH_TRACK_CORRELATION`` / ``IH_SPA_SRC`` (optional) add a local track_correlation checkout and/or
  ``chaos-spa/src`` to ``sys.path`` — chaos-spa is the OPTIONAL official grader (private repo); without
  it the engine uses its legacy grader and in-house bistatic geometry (see README, "Optional chaos-spa").
* ``IH_NO_SPA=1`` blocks ``import spa`` even when chaos-spa is installed (used to test the fallback path).
"""
from __future__ import annotations

import importlib.abc
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
VENDOR = os.path.join(HERE, "vendor")

for _p in (os.environ.get("IH_TRACK_CORRELATION"), os.environ.get("IH_SPA_SRC"), VENDOR):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


class _BlockSpa(importlib.abc.MetaPathFinder):
    """``IH_NO_SPA=1``: make ``import spa`` / ``spa.*`` raise ImportError (fallback-path testing)."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "spa" or fullname.startswith("spa."):
            raise ImportError(f"{fullname} blocked by IH_NO_SPA=1")
        return None


def spa_blocked() -> bool:
    return os.environ.get("IH_NO_SPA", "").strip().lower() in ("1", "true", "yes")


if spa_blocked() and not any(isinstance(f, _BlockSpa) for f in sys.meta_path):
    sys.meta_path.insert(0, _BlockSpa())
    for _m in [m for m in sys.modules if m == "spa" or m.startswith("spa.")]:
        del sys.modules[_m]


def spa_available() -> bool:
    """True when chaos-spa can be imported (and is not blocked)."""
    if spa_blocked():
        return False
    try:
        import spa  # noqa: F401
    except Exception:
        return False
    return True
