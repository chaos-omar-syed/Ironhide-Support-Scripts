"""Palette contract for ih.theme (dark, not black-on-black).

WCAG 2.x contrast from relative luminance (sRGB -> linear, 0.2126/0.7152/0.0722):
  * every ink (primary / secondary / muted) >= 4.5:1 on the card surface
  * every entity colour (target, 2nd target track, interceptor, ★ gold, obs ✕, zero line,
    de-emphasis grey) and every status colour (good / warn / fail / n/a) and the CHAOS
    accent >= 3:1 on the card
  * inks also >= 4.5:1 on the page surface (page is darker than the card)
OKLab (Björn Ottosson): target vs interceptor ΔE (x100) >= 15 so the two vehicles never
blur together; the two target-side track colours stay distinguishable too.
The tokens must also be the ones the CSS / config actually ship.

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_palette.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ih import theme as T  # noqa: E402


# ── colour math ──────────────────────────────────────────────────────────────
def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = (to_linear(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def oklab(h: str) -> tuple[float, float, float]:
    """sRGB hex -> OKLab (L in 0..1, a/b ~ ±0.4)."""
    r, g, b = (to_linear(c) for c in hex_to_rgb(h))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = l ** (1 / 3), m ** (1 / 3), s_ ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def delta_e(a: str, b: str) -> float:
    """Euclidean OKLab distance, x100 (CIELAB-like scale)."""
    la, lb = oklab(a), oklab(b)
    return 100.0 * sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5


# ── the palette under test ───────────────────────────────────────────────────
INKS = {"ink": T.INK, "ink2": T.INK2, "ink3": T.INK3}
ENTITIES = {"target": T.TARGET, "target_dark": T.TARGET_DARK, "interceptor": T.INTERCEPTOR, "gold": T.GOLD,
            "obs": T.OBS, "zero": T.ZERO, "grey_track": T.GREY_TRACK}
STATUSES = {"good": T.GREEN, "warn": T.AMBER, "fail": T.FAIL, "na": T.NA, "accent": T.RED}


def report() -> dict[str, float]:
    """Every number the report quotes, in one place."""
    out = {}
    for n, c in INKS.items():
        out[f"{n} on card"] = contrast(c, T.CARD)
        out[f"{n} on page"] = contrast(c, T.SURFACE)
    for n, c in {**ENTITIES, **STATUSES}.items():
        out[f"{n} on card"] = contrast(c, T.CARD)
    out["ΔE target vs interceptor"] = delta_e(T.TARGET, T.INTERCEPTOR)
    out["ΔE target vs target_dark"] = delta_e(T.TARGET, T.TARGET_DARK)
    out["ΔE good vs warn"] = delta_e(T.GREEN, T.AMBER)
    out["ΔE warn vs fail"] = delta_e(T.AMBER, T.FAIL)
    return out


def test_math_sanity():
    assert abs(luminance("#ffffff") - 1.0) < 1e-9 and luminance("#000000") == 0.0
    assert abs(contrast("#ffffff", "#000000") - 21.0) < 1e-9
    L, a, b = oklab("#ffffff")
    assert abs(L - 1.0) < 1e-3 and abs(a) < 1e-3 and abs(b) < 1e-3          # white is neutral at L = 1
    assert delta_e("#e66767", "#e66767") == 0.0


def test_tokens_are_the_agreed_dark_set():
    assert (T.SURFACE, T.CARD, T.CARD2, T.RULE) == ("#101215", "#1a1d22", "#23272e", "#30353d")
    assert T.GRID == "rgba(255,255,255,.16)"
    assert (T.INK, T.INK2, T.INK3) == ("#f5f6f7", "#b3b9c2", "#7d858f")
    assert (T.TARGET, T.INTERCEPTOR, T.GOLD) == ("#e66767", "#3987e5", "#ffd166")
    assert T.OBS == T.INK2 and T.OBS_ALPHA == 0.55 and T.ZERO == T.INK3
    assert (T.GREEN, T.AMBER, T.FAIL) == ("#3ddc84", "#f5b942", "#ff5d5d")
    assert T.RED == "#e5222b" and T.STATUS["fail"][0] == T.FAIL        # fail status is the stepped red; #e5222b is identity only
    assert T.STATUS["na"][0] == T.NA == T.INK3


def test_inks_at_least_4_5_to_1_on_card():
    for n, c in INKS.items():
        assert contrast(c, T.CARD) >= 4.5, (n, c, contrast(c, T.CARD))
        assert contrast(c, T.SURFACE) >= 4.5, (n, c, contrast(c, T.SURFACE))


def test_entities_and_status_at_least_3_to_1_on_card():
    for n, c in {**ENTITIES, **STATUSES}.items():
        assert contrast(c, T.CARD) >= 3.0, (n, c, contrast(c, T.CARD))


def test_target_vs_interceptor_oklab_delta_e():
    assert delta_e(T.TARGET, T.INTERCEPTOR) >= 15.0, delta_e(T.TARGET, T.INTERCEPTOR)
    assert delta_e(T.TARGET, T.TARGET_DARK) >= 8.0                      # the handover track stays tellable from the primary
    assert delta_e(T.GREEN, T.AMBER) >= 15.0 and delta_e(T.AMBER, T.FAIL) >= 15.0


def test_bands_are_the_target_red_one_sigma_only():
    from ih import plots as PL

    assert PL.rgba(T.TARGET, PL.BAND_ALPHA[1]) == "rgba(230,103,103,0.3)"     # ONLY the ±1σ band is drawn (3σ swamped it)
    assert 3 not in PL.BAND_ALPHA and PL.BANDS_DRAWN == (1,)
    assert PL.BAND_EDGE_ALPHA == 0.8 and PL.LINE_W == 2.5


def test_css_and_config_ship_the_tokens():
    css = T.CSS
    for c in (T.SURFACE, T.CARD, T.CARD2, T.RULE, T.INK, T.INK2, T.INK3, T.GREEN, T.AMBER, T.FAIL, T.GOLD, T.RED):
        assert c in css, c
    assert "#000000" not in css and "#070707" not in css and "#f2f2f2" not in css      # the black-on-black set is gone
    cfg = open(os.path.join(ROOT, ".streamlit", "config.toml")).read()
    assert re.search(r'backgroundColor = "#101215"', cfg) and re.search(r'secondaryBackgroundColor = "#23272e"', cfg)
    assert 'textColor = "#f5f6f7"' in cfg and "[theme.sidebar]" in cfg and 'backgroundColor = "#1a1d22"' in cfg


if __name__ == "__main__":
    for k, v in report().items():
        print(f"{k:32s} {v:6.2f}")
