"""D7 — formal design QA (owner D, 2026-09-10): the dataviz / design rules from docs/CHANGES_2026-09-10.md as MACHINE checks,
so a regression shows up here and not in a screenshot.

(a) tone on a tile / chip / callout = 2 px TOP rule + icon+word row, square corners — never the left-border-accent trope
(b) status glyphs are small inline STROKE SVGs (16 px grid, currentColor, one style), never dingbats — HTML side; plotly marker
    symbols (★ CPA, ✕ obs) stay symbols inside the figures
(c) hit targets >= 44 px on buttons / radio options / toggles / selects / inputs (--hit = 2.75rem at the 16 px base)
(d) gap-based layouts inside every component (flex / grid gap), no margin-spaced siblings
(e) ONE type scale (.75 / .85 / 1 / 1.15 / 1.3 / 1.6 / 2 / 3.2 rem), body >= 16 px at the Normal scale, two faces (sans + mono)
(f) palette: entity pair, the two role ramps (ordinal: monotone L, adjacent ΔL >= .06, one hue), status trio — every mark >= 3:1
    on the card; track colours in the figures come ONLY from the ramps (+ the fold grey); no series colour is a status colour
(g) figures: legend iff >= 2 series, solid hairline grids, no dual (overlaying) axes, text never in a series colour (ink tokens
    only — the map pills carry the role colour on their BORDER; the map's "track_status" STATE tag is the one documented
    exception, in a status colour by the user's 2026-09-15 request, like the html tiles / chips), tooltips on every data
    series (decorations may skip); card chrome (card_ frame, hdr_ band, cpa_line_ hairline) is shapes, never text

Run (from ironhide_dashboard/):
  python -m pytest -q tests/test_design_qa.py
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from functools import lru_cache

os.environ.setdefault("IH_LIVE_PORT", "8912")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamlit.testing.v1 import AppTest  # noqa: E402

from ih import data as D  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import plots as PL  # noqa: E402
from ih import theme as T  # noqa: E402
from test_palette import contrast, delta_e, oklab  # noqa: E402

DINGBAT = re.compile(r"[●▲✕○★◆■⟲❚▸▾]")
INKS = {T.INK, T.INK2, T.INK3}
SCALE = {0.75, 0.85, 1.0, 1.15, 1.3, 1.6, 2.0, 3.2}          # rem type steps
FONT_RE = re.compile(r"font(?:-size)?:\s*(?:\d{3}\s+)?(\.?\d+(?:\.\d+)?)rem")
LIVE = f"{ROOT}/views/1_live.py"
F1_LATE = D.hms_to_epoch("07:24:15")   # two target-side tracks graded, 177 stolen by the interceptor, CPA validated


def _rules(css: str) -> list[tuple[str, str]]:
    body = css.split("<style>")[1].split("</style>")[0]
    body = re.sub(r"@import[^;]*;", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return [(sel.strip(), decl.strip()) for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body)]


def _ds_css() -> str:
    """The Data-source page's page-scoped CSS block (the page is a Streamlit script: read, do not import)."""
    src = open(os.path.join(ROOT, "views", "3_data_source.py"), encoding="utf-8").read()
    return src.split('PAGE_CSS = """', 1)[1].split('"""', 1)[0]


@lru_cache(maxsize=None)
def _live(t: float):
    """One Live-page run (F1 replay paused at ``t``, obs on, quad expanded): (markdown, figure specs, button protos)."""
    at = AppTest.from_file(LIVE, default_timeout=240)
    for k, v in D.STATE_DEFAULTS.items():
        at.session_state[k] = v
    for k, v in dict(show_sat=False, meas_open=True, flight=1, anchor_t=t, playing=False, show_obs=True, spec_window=120).items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [f"{e.type}: {e.message}" for e in at.exception]
    md = "\n".join(m.value for m in at.markdown)
    return md, LS.figs_of(at.session_state["_sid"]), [(b.label, b.proto.icon) for b in at.button]


# ── (a) top rule + icon/word row, square corners ─────────────────────────────
def test_a_tone_is_a_top_rule_plus_icon_word_row_never_a_left_border_accent():
    for name, css in (("theme", T.CSS), ("panel", LS.panel_css()), ("data-source", _ds_css()), ("meas", LS.meas_css())):
        assert "border-left" not in css, name
        assert not re.search(r"border-radius:\s*(\.[3-9]|[1-9]\d*(\.\d+)?)(rem|px|em)", css.replace("border-radius:2px", "").replace("border-radius: 2px", "")), name   # 2 px / 50 % rings only
    rules = dict(_rules(T.CSS))
    for comp in (".ih-tile", ".ih-chip", ".ih-events", ".ih-callout"):
        assert "border-top:2px solid" in rules[comp], comp
    for cls, tok in (("ok", "--green"), ("amber", "--amber"), ("fail", "--fail"), ("gold", "--gold")):
        assert f".ih-tile.{cls} {{ border-top-color:var({tok}); }}" in T.CSS, cls
    for cls, tok in (("ok", "--green"), ("amber", "--amber"), ("fail", "--fail")):
        assert f".ih-chip.{cls} {{ border-top-color:var({tok}); }}" in T.CSS, cls
    ds = _ds_css()
    assert ".ds-tile { display: flex; flex-direction: column; gap:" in ds and "border-top: 2px solid var(--rule)" in ds and ".ds-tile.ok { border-top-color: var(--green); }" in ds
    # every toned tile / chip carries the icon + word row; neutral ones carry none
    for tone in ("ok", "amber", "fail", "gold"):
        html = T.tile_html("Target track", "#177 CONF", "", "age 1 s", tone=tone)
        assert f'<div class="st">{T.glyph(tone, T.TILE_WORD.get(tone))}</div>' in html, tone
    assert '<div class="st">' not in T.tile_html("Closest so far", "—", "", "no pair yet", tone="na") + T.tile_html("Separation", "312", "m", "closing", tone="")
    chips = T.chips_html([("Coverage · 60 s", "94 %", "fresh", "ok"), ("Predicted miss", "—", "needs both heads", "")])
    assert chips.count('<div class="st">') == 1 and T.glyph("ok") in chips
    md, _, _ = _live(F1_LATE)
    tiles = re.findall(r'<div class="ih-tile ([a-z]*)">(.*?)</div></div>', md)
    assert [t[0] for t in tiles] == ["ok", "", "gold"]                                       # CONFIRMED track · neutral separation · CPA validated (target track first)
    # 2026-09-15: the engine's state words are spelled out (CONFIRMED / TENTATIVE / COASTING — ih.engine, ih.plots track_status), not "CONF" / "TENT"
    assert T.ICONS["gold"] in tiles[2][1] and "CPA" in tiles[2][1] and '<div class="st">' not in tiles[1][1] and T.glyph("ok", "CONFIRMED") in tiles[0][1]   # target track first, CPA tile last


# ── (b) inline stroke SVG icons, no dingbats ─────────────────────────────────
def test_b_status_icons_are_one_style_stroke_svgs_and_no_dingbat_reaches_the_html():
    assert set(T.ICONS) >= {"ok", "amber", "fail", "na", "red", "gold", "pause", "reset"}
    for k, svg in T.ICONS.items():
        assert svg.startswith('<svg class="ih-i" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75"'), k
        assert 'stroke-linecap="round"' in svg and 'aria-hidden="true"' in svg and svg.endswith("</svg>") and not DINGBAT.search(svg), k
    for cls, (_, svg, word) in T.STATUS.items():
        assert svg == T.ICONS[cls] and T.glyph(cls) == f'<span class="gw"><span class="g {cls}">{svg}</span>{word}</span>'
    html = "".join((T.glyph("ok"), T.glyph("red", "LIVE"), T.glyph("gold", "CPA"), T.glyph("amber", "reset", icon="reset"), T.dot("na"),
                    T.callout_html("OFFLINE", "t", "b", "red"), T.callout_html("WAITING", "t", "b", "amber"), T.callout_html("SAVED", "t", "b", "green"),
                    T.callout_html("CONNECTED", "t", "b", "grey"), T.source_line_html("live", mru=91, run="x"), T.source_line_html("archive", flight=1),
                    T.tile_html("t", "1", "m", "s", "gold"), T.chips_html([("a", "b", "c", "amber")]), T.events_card_html([]), T.feed_glyph("target", "stale", 1.0),
                    LS.panel_body(), LS.panel_core_js(), LS.panel_script("sid", "host", 8902, 1000), LS.meas_html("sid", "host", 8902, 1000), T.CSS, _ds_css()))
    for cap in LS.HEADERS.values():                                # the card-header captions NAME plotly marker symbols ("★ CPA (gated)", "★ start ✕ end") — legend text, not icons
        html = html.replace(cap[1], "").replace(T.esc(cap[1]), "")
    assert not DINGBAT.search(html), DINGBAT.search(html).group(0)
    # the panel JS builds its status / chip rows from the same icons + words
    js = LS.panel_core_js()
    assert "var ICONS=" + json.dumps(T.ICONS) in js                                                  # the JS carries the very same icon strings (JSON-encoded)
    assert '"ok": "OK"' in js and '"amber": "WARN"' in js and "function gw(c,w)" in js and 'setStatus(j.frozen?"pause":"ok", j.frozen?"frozen":"live", j.clock)' in js and "gw(STAT.cls,w" in js
    # the rendered Live page (HTML markdown + button labels) carries none either; the quad toggle uses a Material icon
    md, _, buttons = _live(F1_LATE)
    assert not DINGBAT.search(md), DINGBAT.search(md).group(0)
    assert all(not DINGBAT.search(lab) for lab, _ in buttons) and any(ic == ":material/expand_less:" for _, ic in buttons)


# ── (c) hit targets ──────────────────────────────────────────────────────────
def test_c_hit_targets_at_least_44px_on_real_controls():
    css = T.CSS
    assert "--hit:2.75rem" in css and 2.75 * T.BASE_FONT_PX >= 44
    for need in ('.stButton button, [data-testid="stSidebar"] .stButton button { min-height:var(--hit); }',
                 '.stRadio [role="radiogroup"] label, [data-testid="stCheckbox"] label { min-height:var(--hit); align-items:center; }',
                 '[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"] { min-height:var(--hit); }',
                 '[data-testid="stExpander"] summary { min-height:var(--hit); }'):
        assert need in css, need
    ds = _ds_css()
    for need in (".stButton button { min-height: 3rem;", 'button[kind="primary"] { min-height: 3.6rem;', '[data-baseweb="select"] > div { min-height: 3.4rem;',
                 ".stRadio [role=\"radiogroup\"] label { font-size: 1.15rem; min-height: var(--hit); align-items: center; }", "min-height: var(--hit); }"):
        assert need in ds, need
    assert ".st-key-ds_mode_box [role=\"radiogroup\"] label { padding: 1.1rem 2rem;" in ds                 # the big mode selector: 17.6 px pad + 32 px text > 44 px


# ── (d) gap-based layouts ────────────────────────────────────────────────────
def test_d_components_are_gap_based_flex_columns_without_margin_spaced_children():
    rules = _rules(T.CSS)
    by_sel = dict(rules)
    for comp in (".ih-tile", ".ih-chip", ".ih-events", ".ih-callout"):
        decl = by_sel[comp]
        assert "display:flex" in decl and "flex-direction:column" in decl and re.search(r"gap:\d+px", decl), comp
        for sel, d in rules:
            if sel.startswith(comp + " ") or sel.startswith(comp + "."):
                for part in sel.split(","):
                    assert not re.search(r"margin(-top|-bottom|-left|-right)?\s*:", d), (part, d)
    assert ".gw { display:inline-flex; align-items:center; gap:.3em;" in T.CSS and "margin-right:4px" not in T.CSS
    assert ".ih-tile .v { font:600 3.2rem/1 var(--sans); display:flex; flex-wrap:wrap; align-items:baseline; gap:6px;" in T.CSS   # value + unit gap-spaced
    assert ".ih-chip .k { font:500 .85rem/1.3 var(--mono); display:flex; align-items:center; gap:6px;" in T.CSS               # icon + label gap-spaced
    assert ".ih-wordmark { display:inline-flex; align-items:center; gap:.32em;" in T.CSS
    for need in (".ih-tiles { display:grid;", ".ih-chips { display:grid;", ".ih-kv { display:grid;", ".ih-top { display:flex;", ".ih-status { display:flex;"):
        assert need in T.CSS and "gap:" in by_sel[need.split(" {")[0]], need
    pcss = LS.panel_css()
    for need in (".ih-chip{display:flex;flex-direction:column;gap:4px;", ".ih-events{display:flex;flex-direction:column;gap:2px;", ".gw{display:inline-flex;align-items:center;gap:.3em;",
                 ".grid{display:grid;gap:0 12px;"):
        assert need in pcss, need
    assert not re.search(r"\.ih-(chip|events) \.[a-z]+\{[^}]*margin-(top|bottom)", pcss)
    ds = _ds_css()
    assert ".ds-tile { display: flex; flex-direction: column; gap: .35rem;" in ds and ".ds-big { font: 600 2rem/1.1 var(--sans); display: flex; flex-wrap: wrap; align-items: baseline; gap: .4rem;" in ds
    assert not re.search(r"\.ds-(kicker|big|sub|st) \{[^}]*margin-(top|bottom|left)", ds)


# ── (e) one type scale, body >= 16 px, two faces ─────────────────────────────
def test_e_one_type_scale_body_16px_two_faces():
    for name, css in (("theme", T.CSS), ("data-source", _ds_css())):
        sizes = {float(v) for v in FONT_RE.findall(css)}
        assert sizes and sizes <= SCALE, (name, sorted(sizes - SCALE))
        assert min(sizes) >= 0.75                                                          # nothing under 12 px at the Normal scale
    assert T.BASE_FONT_PX == 16 and T.TEXT_SCALES == {"Normal": 1.0, "Large": 1.15, "X-Large": 1.3}
    for body in ("p, li, label, .stMarkdown { font-size:1rem; }", '[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label { font-size:1rem !important;',
                 ".ih-callout .b { font:400 1rem/1.5 var(--sans);", ".ih-kv dd { margin:0; font:400 1rem/1.5 var(--sans);", "table.ih-table { width:100%; border-collapse:collapse; font:400 1rem/1.4 var(--sans); }",
                 ".ih-sub { font:400 1rem/1.5 var(--sans);", '[data-testid="stSidebar"] .stButton button, .ih-controls .stButton button { background:var(--card2); border:1px solid var(--rule); color:var(--ink); font-weight:600; font-size:1rem; }'):
        assert body in T.CSS, body                                                         # body text = 1rem = 16 px at Normal
    fams = set(re.findall(r"font(?:-family)?:[^;{}]*?var\((--(?:mono|sans))\)", T.CSS))
    assert fams == {"--mono", "--sans"}                                                    # exactly two faces
    assert not re.search(r"font-family:\s*(?!var\(--(mono|sans)\))[A-Za-z\"']", T.CSS)      # no third face anywhere
    assert "family=Inter" in T.CSS and "family=JetBrains+Mono" in T.CSS and T.CSS.count("@import") == 1
    assert T.MONO.endswith("monospace") and T.SANS.endswith("sans-serif")                 # close-metric fallbacks
    # the panel iframe / plotly fonts follow the same factor
    L = LS.layout("Desktop 1080p", 1.3)
    assert L["font_px"] == round(14 * 1.3) and "font-size:calc(16px * var(--ih-scale,1))" in LS.panel_css()


# ── (f) palette ───────────────────────────────────────────────────────────────
def _hue(h: str) -> float:
    _, a, b = oklab(h)
    return math.degrees(math.atan2(b, a)) % 360.0


def test_f_palette_entities_ramps_status_and_track_colours_come_from_the_ramps():
    # entity pair (categorical, all pairs): normal-vision floor >= 15, >= 3:1 on the card
    assert delta_e(T.TARGET, T.INTERCEPTOR) >= 15 and contrast(T.TARGET, T.CARD) >= 3 and contrast(T.INTERCEPTOR, T.CARD) >= 3
    # role ramps (ordinal): one hue (spread <= 5°), monotone L light -> dark, adjacent ΔL >= .06, every step >= 3:1 on the card
    for ramp in (PL.RAMP_TGT, PL.RAMP_ITC):
        Ls = [oklab(c)[0] for c in ramp]
        assert all(Ls[i] - Ls[i + 1] >= 0.06 for i in range(len(Ls) - 1)), (ramp, Ls)
        hues = [_hue(c) for c in ramp]
        assert max(hues) - min(hues) <= 5.0, (ramp, hues)
        for c in ramp:
            assert contrast(c, T.CARD) >= 3.0, (c, contrast(c, T.CARD))
    assert PL.RAMP_ITC[2] == "#2a6cc7" and contrast("#1c5cab", T.CARD) < 3.0 <= contrast(PL.RAMP_ITC[2], T.CARD)   # the old dark step was 2.55:1
    # cross-role pairs (same step) and every mixed pair clear the normal-vision floor
    for t in PL.RAMP_TGT:
        for i in PL.RAMP_ITC:
            assert delta_e(t, i) >= 15, (t, i, delta_e(t, i))
    assert contrast(PL.RAMP_FOLD, T.CARD) >= 3.0 and abs(oklab(PL.RAMP_FOLD)[1]) < 0.01 and abs(oklab(PL.RAMP_FOLD)[2]) < 0.01   # the fold is a true neutral
    # status trio: >= 3:1, distinct from each other for full-colour readers; yellow <-> green CVD is mitigated by the mandatory icon + word
    for c in (T.GREEN, T.AMBER, T.FAIL):
        assert contrast(c, T.CARD) >= 3.0
    assert delta_e(T.GREEN, T.AMBER) >= 15 and delta_e(T.AMBER, T.FAIL) >= 15 and delta_e(T.GREEN, T.FAIL) >= 15
    assert all(len(v[2]) > 0 and v[1].startswith("<svg") for v in T.STATUS.values())
    # a series colour is never a status colour, and status never a series colour
    series = set(PL.RAMP_TGT) | set(PL.RAMP_ITC) | {PL.RAMP_FOLD, T.GREY_TRACK, T.GOLD}
    assert not series & {T.GREEN, T.AMBER, T.FAIL}
    # the figures: track colours come ONLY from the ramps (+ fold grey; the map's ungraded "radar" tracks are the de-emphasis grey)
    _, figs, _ = _live(F1_LATE)
    meas_tracks = [t for t in figs["meas"]["data"] if (t.get("name") or "").startswith("#") and t.get("line")]
    assert meas_tracks and {t["line"]["color"] for t in meas_tracks} <= set(PL.RAMP_TGT) | set(PL.RAMP_ITC) | {PL.RAMP_FOLD}
    assert {t["line"]["dash"] for t in meas_tracks} <= set(PL.STEP_DASH)
    map_tracks = [t for t in figs["map"]["data"] if "track" in (t.get("name") or "") and t.get("line")]
    assert map_tracks and {t["line"]["color"] for t in map_tracks} <= set(PL.RAMP_TGT) | set(PL.RAMP_ITC) | {PL.RAMP_FOLD, T.GREY_TRACK}
    err_lines = [t for t in figs["err"]["data"] if (t.get("name") or "").startswith("track #") and t.get("line") and "±" not in t["name"]]
    for t in err_lines:
        c = t["line"]["color"]
        base = re.match(r"rgba\((\d+),(\d+),(\d+),", c)
        hexc = "#%02x%02x%02x" % tuple(int(x) for x in base.groups()) if base else c
        assert hexc in set(PL.RAMP_TGT) | set(PL.RAMP_ITC), c


# ── (g) figures ───────────────────────────────────────────────────────────────
DECOR = ("leader", "radar", "raw obs", "±1σ", " matched", " start", " end", "coast strip", "blind zone", "_anchor")   # decorations (and invisible axis anchors) may skip the hover


def test_g_figures_legend_iff_two_series_solid_hairline_grids_no_dual_axes_ink_text_tooltips():
    _, figs, _ = _live(F1_LATE)
    assert set(figs) == {"map", "sep", "err", "vel", "meas"}
    for k, f in figs.items():
        L, data = f["layout"], f["data"]
        named = [t for t in data if t.get("name") and t.get("showlegend", True) is not False]
        assert bool(L.get("showlegend")) == (len(named) >= 2), (k, len(named), L.get("showlegend"))
        axes = {a: v for a, v in L.items() if re.match(r"^[xy]axis\d*$", a)}
        assert axes
        for a, v in axes.items():
            assert "overlaying" not in v, (k, a)                                                        # never a dual axis
            if v.get("showgrid", True):
                assert v.get("griddash") == "solid" and v.get("gridwidth") == 1 and v.get("gridcolor") == T.GRID, (k, a)
        for a in L.get("annotations", []):
            nm = str(a.get("name") or "")
            if nm.startswith(("handover_tag_", "cpa_tag")):
                assert a.get("text") == "▾" and a.get("hovertext"), (k, a)                                # the measurement quad: a MARK (hover carries the words)
                continue
            if nm.startswith(("handover_label_", "cpa_label", "endlbl_")):                                # error / velocity cards (user 2026-09-11: "what are these lines" ->
                assert a.get("text") and a.get("text") != "▾" and a.get("font", {}).get("color") in INKS, (k, a)   # every line is LABELLED, in ink, at the top edge / line end)
                continue
            if nm == "track_status":                                                                      # THE ONE documented exception (2026-09-15 "add a track status to that top
                assert k == "map" and re.fullmatch(r"<b>(#\d+ )?[A-Z][A-Z ]+</b>", str(a.get("text")))     # panel"): a STATE tag, so it wears the state colour like the html tiles/chips
                assert a.get("font", {}).get("color") in (T.GREEN, T.AMBER, T.FAIL, T.INK3), (k, a.get("font"))
                continue
            assert a.get("font", {}).get("color") in INKS, (k, a.get("name"), a.get("font"))            # text never in a series colour
        # the card chrome is SHAPES (no text of their own): card frame, the shaded header band, the gold CPA hairline
        for s in L.get("shapes", []):
            nm = str(s.get("name") or "")
            assert "text" not in s, (k, nm)
            if nm.startswith("hdr_"):
                assert s["type"] == "rect" and s["fillcolor"] == PL.HDR_BAND and s["layer"] == "below" and (s["line"] or {}).get("width") == 0, (k, s)
            elif nm.startswith("cpa_line_"):
                assert s["type"] == "line" and s["line"]["color"] == T.GOLD and s["line"]["width"] == 1 and s["x0"] == s["x1"], (k, s)
        for t in data:
            if t.get("textfont"):
                assert t["textfont"].get("color") in INKS, (k, t.get("name"))
        assert L.get("hovermode") in ("closest", "x unified"), k
        assert L.get("hoverlabel", {}).get("font", {}).get("color") == T.INK
        for t in data:
            name = t.get("name") or ""
            if t.get("x") in ([None], []) or t.get("mode") == "none":
                continue                                                                                # legend proxies (x=[None]) carry no data
            if name and not any(w in name for w in DECOR):
                assert t.get("hoverinfo") != "skip" or t.get("hovertemplate"), (k, name)              # a tooltip on every data series
    # the map pills: role colour on the border, primary ink on the text
    pills = {a["name"]: a for a in figs["map"]["layout"]["annotations"] if str(a.get("name", "")).startswith("pill_")}
    assert set(pills) == {"pill_tgt", "pill_itc"}
    assert pills["pill_tgt"]["font"]["color"] == T.INK and pills["pill_tgt"]["bordercolor"] == T.TARGET and pills["pill_tgt"]["borderwidth"] == 2
    assert pills["pill_itc"]["font"]["color"] == T.INK and pills["pill_itc"]["bordercolor"] == T.INTERCEPTOR
    # legend present exactly where >= 2 series: map (truths + tracks), sep (3D · horizontal · CPA), meas (grouped) — the error panel at
    # 07:24:15 grades ONE track (#203) and so carries none
    assert figs["map"]["layout"]["showlegend"] is True and figs["sep"]["layout"]["showlegend"] is True and figs["meas"]["layout"]["showlegend"] is True
    assert figs["err"]["layout"]["showlegend"] is False
    # 2 px series lines (the de-emphasised DEPARTED segments are the documented 1 px exception), >= 8 px markers with a 2 px surface ring
    for t in figs["sep"]["data"]:
        if t.get("mode") == "lines":
            assert t["line"]["width"] >= 2, t.get("name")
    stars = [t for t in figs["map"]["data"] if t.get("marker", {}).get("symbol") == "star"]
    assert stars and all(t["marker"]["size"] >= 8 and t["marker"]["line"]["width"] == 2 for t in stars)
