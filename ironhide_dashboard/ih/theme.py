"""CHAOS template look for Streamlit — dark, not black-on-black — laid out with the
dataviz method.  Tokens are validated by tests/test_palette.py (WCAG relative
luminance + OKLab ΔE): every ink >= 4.5:1 on the card, every entity / status colour
>= 3:1 on the card, target vs interceptor ΔE(OKLab x100) >= 15.

  surfaces   page #101215 · card #1a1d22 · card-2 / inputs #23272e · rule #30353d
             · grid hairline rgba(255,255,255,.16) solid
  inks       primary #f5f6f7 · secondary #b3b9c2 · muted #7d858f  (three, no more)
  entities   target #e66767 (bands .35 / .15 + 1 px edge .6; 2nd target-side track #b5474a)
             · interceptor #3987e5 · closest-so-far ★ #ffd166 · raw obs ✕ #b3b9c2 @ .55
             · zero line #7d858f · de-emphasis grey = muted ink
             (coasting is a GAP in the error line, never a hue)
  status     good #3ddc84 · warning #f5b942 · fail #ff5d5d · n/a = muted ink
             (meaning only, always paired with an ICON + word: the icon wears the colour, the word stays in the text ink;
             icons are small inline stroke SVGs on a 16 px grid — ICONS — never dingbats; tone on a tile / chip / callout is
             a 2 px TOP rule, square corners, never a left-border accent)
  identity   CHAOS red #e5222b ONLY as the wordmark square / card-header mark / callout edge

Type: one sans (Inter stack) for values/body, one mono for labels/ticks/tables.
ONE scale (rem, so the sidebar "Text size" control scales EVERYTHING through html{font-size:16px*--ih-scale}):
.75 labels / kickers / footer · .85 status strips / card headers / tile & chip subs / captions · 1 body (widget labels,
callout body, kv, tables, buttons) · 1.15 callout titles · 1.3 wordmark / tile units · 1.6 h1 · 2.0 chip values · 3.2 tile hero.
Body text is >= 16 px at the Normal scale.  text_scale(): Normal 1.0 / Large 1.15 / X-Large 1.3 (default X-Large on the
Large-1440p preset, Large on Desktop 1080p, Normal on Laptop; TEXT_SCALE_DEFAULT); the ih-status top line is 1.15; the panel iframe and every plotly figure get the same factor through ui.font_px = FONT_PX[preset] x scale.
Spacing rhythm 4/8/12/16/24 px, gap-based inside every component (flex / grid gap, no margin-spaced siblings).  Hit targets
>= 44 px (--hit 2.75rem) on buttons, radio options, toggles, selects and inputs.  Letter-spacing capped at .08em (nothing
clips in a narrow column: no nowrap / overflow:hidden / ellipsis anywhere in this CSS).

Font rule (root cause of the "ARROW_RIGHT" leak): font-family is applied ONLY to text
elements we own (.ih-* classes) and to explicit Streamlit text test-ids / elements —
never through a wildcard or bare descendant selector (`[data-testid="stSidebar"] *`,
`summary span`, …), because those also hit Streamlit's Material-icon spans
(span[data-testid="stIconMaterial"] / .material-symbols-rounded /
[data-testid="stExpanderToggleIcon"]), whose ligature then renders as its icon NAME.
Small-caps mono (uppercase + letter-spacing) is reserved for our .ih-* labels; Streamlit
widget / expander labels stay sentence-case sans and wrap.  Sidebar >= 320 px.
"""
from __future__ import annotations

import glob
import html as _html
import os
import time

import streamlit as st

# ── tokens ───────────────────────────────────────────────────────────────────
RED = "#e5222b"                                     # CHAOS identity accent (wordmark / header mark / callout edge)
INK, INK2, INK3 = "#f5f6f7", "#b3b9c2", "#7d858f"   # primary / secondary / muted
SURFACE, CARD, CARD2, RULE = "#101215", "#1a1d22", "#23272e", "#30353d"
GRID = "rgba(255,255,255,.16)"                      # hairline, solid
ZERO = INK3                                         # zero line: muted ink
TARGET, INTERCEPTOR, GOLD = "#e66767", "#3987e5", "#ffd166"
TARGET_DARK = "#b5474a"                             # second target-side track in the error panel (handover / duplicate)
OBS, OBS_ALPHA = INK2, 0.55                         # raw obs ✕
GREY_TRACK = INK3                                   # de-emphasis series (horizontal separation)
GREEN, AMBER, FAIL, NA = "#3ddc84", "#f5b942", "#ff5d5d", INK3


def _svg(paths: str) -> str:
    """One inline stroke icon on a 16 px grid: currentColor, 1.75 px round strokes, no fill (one style for every glyph)."""
    return ('<svg class="ih-i" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>')


ICONS = {  # status / state icons (HTML side).  Plotly marker SYMBOLS (★ CPA, ✕ obs) stay symbols inside the figures.
    "ok": _svg('<circle cx="8" cy="8" r="6"/><path d="M5.2 8.3l1.9 1.9 3.7-4.2"/>'),                       # ring + check
    "amber": _svg('<path d="M8 2.6L14.2 13H1.8z"/><path d="M8 6.4v3.2"/><path d="M8 11.6v.1"/>'),           # triangle + !
    "fail": _svg('<circle cx="8" cy="8" r="6"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/>'),             # ring + x
    "na": _svg('<circle cx="8" cy="8" r="6"/><path d="M5.5 8h5"/>'),                                          # ring + dash
    "red": _svg('<circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2.4" fill="currentColor" stroke="none"/>'),   # live dot
    "gold": _svg('<path d="M8 2.2l1.8 3.7 4 .6-2.9 2.8.7 4L8 11.4l-3.6 1.9.7-4L2.2 6.5l4-.6z"/>'),           # star (CPA)
    "pause": _svg('<path d="M5.5 3.5v9M10.5 3.5v9"/>'),
    "reset": _svg('<path d="M3.6 8a4.4 4.4 0 1 0 1.3-3.1"/><path d="M3.4 2.9v2.6H6"/>'),
}
STATUS = {  # class -> (color, icon svg, word) — meaning is carried by icon + word; the colour is on the icon only
    "ok": (GREEN, ICONS["ok"], "OK"),
    "amber": (AMBER, ICONS["amber"], "WARN"),
    "fail": (FAIL, ICONS["fail"], "FAIL"),
    "na": (NA, ICONS["na"], "N/A"),
}
FEED_CLS = {"alive": "ok", "stale": "amber", "frozen": "fail", "down": "fail", "none": "na"}
MONO = '"JetBrains Mono","SF Mono",Menlo,Consolas,monospace'
SANS = 'Inter,"Helvetica Neue",Helvetica,Arial,sans-serif'
FOOTER_LEFT = "© CHAOS INC · PROPRIETARY · DO NOT DISTRIBUTE"
TEXT_SCALES = {"Normal": 1.0, "Large": 1.15, "X-Large": 1.3}    # sidebar "Text size" (key text_scale) -> html font-size factor
TEXT_SCALE_DEFAULT = {"Large 1440p": "X-Large", "Desktop 1080p": "Large"}   # per screen preset (the crew's 3796 px wall reads the Large preset at X-Large); Laptop stays Normal
BASE_FONT_PX = 16


def text_scale_label(state=None) -> str:
    st_ = st.session_state if state is None else state
    lab = st_.get("text_scale")
    if lab in TEXT_SCALES:
        return str(lab)
    return TEXT_SCALE_DEFAULT.get(str(st_.get("screen", "")), "Normal")


def text_scale(state=None) -> float:
    """The global text factor (1.0 / 1.15 / 1.3) from the sidebar control, default by preset."""
    return float(TEXT_SCALES[text_scale_label(state)])


def scale_css(scale: float) -> str:
    return f"<style>:root{{--ih-scale:{scale:g};}} html{{font-size:calc({BASE_FONT_PX}px * {scale:g}) !important;}}</style>"

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
:root { --ink:__INK__; --ink2:__INK2__; --ink3:__INK3__; --red:__RED__; --surface:__SURFACE__; --card:__CARD__; --card2:__CARD2__; --rule:__RULE__;
        --green:__GREEN__; --amber:__AMBER__; --fail:__FAIL__; --na:__NA__; --gold:__GOLD__; --target:__TARGET__; --interceptor:__INTERCEPTOR__;
        --mono:__MONO__; --sans:__SANS__; --hit:2.75rem; }
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] { background:var(--surface) !important; color:var(--ink); }
[data-testid="stHeader"] { background:rgba(0,0,0,0) !important; }
[data-testid="stDecoration"] { display:none; }
#MainMenu, footer { visibility:hidden; }
/* 2026-09-15 (laptop 1366x768): the header band is EMPTY (menu / decoration / deploy / status all hidden) yet reserved 60 px
   (Normal) / 69 px (Large) at the top of a 682 px viewport.  Keep it NON-ZERO: the sidebar re-open chevron lives in it. */
[data-testid="stHeader"] { height:2.25rem !important; min-height:0 !important; }
/* ... and every injected CSS block (theme CSS, scale_css, the page CSS) renders a ZERO-HEIGHT stElementContainer
   that still eats a 16 px vertical-block gap - three of them pushed the first real element from y=16 to y=64.  Hide the
   containers whose markdown holds NOTHING BUT a style element (never one with visible markup next to it). */
[data-testid="stMainBlockContainer"] [data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"] > style:only-child),
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"] > style:only-child) { display:none !important; }
/* sidebar page-nav links: "Data source" / "Live" were clipped (clientHeight 28 vs scrollHeight 56) - one line, never wrapped */
[data-testid="stSidebarNav"] a p { white-space:nowrap; }
[data-testid="stAppDeployButton"], .stAppDeployButton, [data-testid="stStatusWidget"] { display:none !important; }   /* no "Deploy" over the build badge */
/* main column: FULL width (layout="wide"), panel starts high — no max-width cap on any screen */
.block-container, [data-testid="stMainBlockContainer"], [data-testid="stAppViewBlockContainer"] { padding:1rem 1.5rem 1.5rem !important; max-width:100% !important; }
h1,h2,h3 { font-family:var(--sans); letter-spacing:-.01em; }
p, li, label, .stMarkdown { font-size:1rem; }

/* fonts: explicit Streamlit text elements only (never a wildcard / bare span — that breaks the Material icon ligatures) */
label, [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label, [data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li,
[data-testid="stCaptionContainer"] p, [data-testid="stExpander"] summary p, .stButton button, .stRadio label p, .stSelectbox input, .stSelectbox [role="option"],
.stSlider [data-testid="stSliderThumbValue"], .stSlider [data-testid="stSliderTickBarMin"], .stSlider [data-testid="stSliderTickBarMax"] { font-family:var(--sans); }

/* sidebar: >= 360 px so the longest label ("Metrics time window (s)") fits on <= 2 lines; labels wrap at WORD boundaries only.
   NEVER overflow-wrap:anywhere / word-break on Streamlit widget internals: it fractured slider values one digit per line
   ("120" -> 1 / 2 / 0 around the thumb).  Anywhere-wrapping is reserved for OUR .ih-* text (long run names). */
[data-testid="stSidebar"] { background:var(--card) !important; border-right:1px solid var(--rule); }
[data-testid="stSidebar"][aria-expanded="true"] { min-width:360px !important; max-width:400px !important; }
[data-testid="stSidebar"] p, [data-testid="stSidebar"] label, [data-testid="stSidebar"] summary { white-space:normal; }
[data-testid="stSidebar"] .ih-status span, [data-testid="stSidebar"] .ih-crumb { overflow-wrap:anywhere; }
/* Streamlit 1.63 lays the widget label out as a FLEX ROW (label text + help icon) whose text box neither wraps nor lets
   its content show: at Large text in a 360 px sidebar "Closest-approach gate (m)" lost its "(m)" AND its help icon.
   The text box may WRAP (word boundaries) and shrink (min-width:0); the help icon keeps its natural size and stays inside.
   Still NEVER overflow-wrap:anywhere / word-break here (that is what fractured the slider values one digit per line). */
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] { white-space:normal; overflow:visible; min-width:0; flex-wrap:wrap; }
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] > div, [data-testid="stSidebar"] [data-testid="stWidgetLabel"] [data-testid="stMarkdownContainer"] { flex:1 1 auto; min-width:0; white-space:normal; overflow:visible; }
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p, [data-testid="stSidebar"] [data-testid="stWidgetLabel"] label { white-space:normal; overflow:visible; }
[data-testid="stSidebar"] [data-testid="stTooltipHoverTarget"], [data-testid="stSidebar"] [data-testid="stTooltipIcon"], [data-testid="stSidebar"] [data-testid="stWidgetLabel"] svg { flex:none; }
[data-testid="stSliderThumbValue"], [data-testid="stSliderTickBar"] *, [data-testid="stSliderTickBarMin"], [data-testid="stSliderTickBarMax"] { white-space:nowrap; overflow-wrap:normal; word-break:normal; }
/* Streamlit 1.63 renders the slider's thumb value and end labels as MARKDOWN (<div data-testid=stMarkdownContainer><p>120</p></div>) inside
   the thumb: the sidebar "labels wrap" rule above ([data-testid="stSidebar"] p { white-space:normal }) also matched THAT <p>, re-enabling
   wrapping inside the few-px-wide thumb box, and Streamlit's own word-break:break-word on markdown paragraphs then stacked "120" as 1 / 2 / 0.
   The slider paragraphs are pinned explicitly (after the sidebar rule, !important): one line, never broken, whatever their box width. */
[data-testid="stSliderThumbValue"] p, [data-testid="stSliderTickBar"] p, .stSlider [data-testid="stMarkdownContainer"] p, .stSlider [data-testid="stMarkdownContainer"]
  { white-space:nowrap !important; overflow-wrap:normal !important; word-break:normal !important; width:max-content; max-width:none; }
[data-testid="stSliderThumbValue"] { width:max-content; }
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding:12px 16px 24px; }
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label { font-size:1rem !important; color:var(--ink2); white-space:normal; }
[data-testid="stCaptionContainer"] p { font-size:1rem !important; color:var(--ink2); white-space:normal; }
[data-testid="stSidebar"] .stButton button, .ih-controls .stButton button { background:var(--card2); border:1px solid var(--rule); color:var(--ink); font-weight:600; font-size:1rem; }
/* the 360 px sidebar: trim the button side padding so a long label ("Pass 1 · 07:21:43 · 78 m") keeps one line (clip audit 2026-09-14) */
[data-testid="stSidebar"] .stButton button { padding-left:.5rem; padding-right:.5rem; }
[data-testid="stSidebar"] .stButton button:hover { border-color:var(--red); color:var(--ink); }
/* hit targets: every real control (button, radio option, toggle, select, input) is >= 44 px tall (--hit = 2.75rem at the 16 px base) */
.stButton button, [data-testid="stSidebar"] .stButton button { min-height:var(--hit); }
.stRadio [role="radiogroup"] label, [data-testid="stCheckbox"] label { min-height:var(--hit); align-items:center; }
.stRadio [role="radiogroup"] { flex-wrap:wrap; row-gap:.25rem; }   /* horizontal radios WRAP in the 360 px sidebar at Large / X-Large text (clip audit 2026-09-11) */
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"] { min-height:var(--hit); }
[data-testid="stExpander"] details { background:var(--card); border:1px solid var(--rule); }
[data-testid="stExpander"] summary { white-space:normal; }
[data-testid="stExpander"] summary { min-height:var(--hit); }
[data-testid="stSidebar"] [data-testid="stExpander"] summary p { font-size:1rem !important; font-weight:600; line-height:1.4; color:var(--ink); white-space:normal; }
[data-testid="stMain"] [data-testid="stExpander"] summary p { font-size:1rem !important; font-weight:500; line-height:1.4; color:var(--ink2); white-space:normal; }
div[data-testid="stPlotlyChart"] { background:var(--card); }

/* ONE top line: wordmark · crumb · live status · build badge */
.ih-top { display:flex; flex-direction:column; gap:2px; padding:2px 0 8px; border-bottom:1px solid var(--rule); margin-bottom:10px; }
.ih-top .row1 { display:flex; justify-content:space-between; align-items:center; gap:8px 16px; height:1.7rem; overflow:hidden; }
.ih-top .l, .ih-top .r { display:flex; align-items:center; gap:8px 16px; min-width:0; white-space:nowrap; overflow:hidden; }
/* FIXED-HEIGHT status row: content may wrap inside it, the box never grows — the panel below never moves (2026-09-14).
   The height is --st-rows WRAPPED LINES: a line that carries a glyph span (inline-flex svg) is ~1.59em tall, not the 1.5em
   line-height, so a row costs 1.7em (measured + slack) plus the 4 px flex row-gap between rows — in em, so it tracks the
   "Text size" factor.  Too small clipped "trk #177 TRACK CHANGED" by 6-7 px at 1366 px x Large / X-Large (2026-09-14). */
.ih-top .ih-status { --st-rows:1; --st-row2:0px; height:calc(1.7em + 4px); overflow:hidden; align-content:flex-start; flex-wrap:nowrap; white-space:nowrap; font-size:1.3rem; gap:0 8px; }
.ih-status .c-ok { color:var(--green) !important; } .ih-status .c-amber { color:var(--amber) !important; } .ih-status .c-fail { color:var(--fail) !important; }   /* 2026-09-15: ONE row ("2 rows just squishes the plot"), 1rem so the inline counter fits 945 px at Large */   /* row 1 may wrap to --st-rows lines (at 1366 px x Large "trk #177 TRACK CHANGED" takes a second one) PLUS the row-2 counter line (1rem = 1.6em of the 1.15rem row-1 type) — measured 107 px at Large, the box is 114 px */
/* 2026-09-15: the second row is now a DELIBERATE row, not slack — row 1 is the source / clock / transport / target track,
   row 2 the target track's update counter in full words.  So the box is TWO rows at every width (the >= 2400 px one-row
   variant is gone), .br forces the break and the row-2 spans are one type step smaller (1rem, on the scale). */
.ih-top .ih-status .br { flex-basis:100%; height:0; margin:0; padding:0; }
.ih-top .ih-status .s2 { font-size:1rem; }
/* explicit separators: "2.0 HZ 228 SAMPLES" used to run together (only a flex gap between the parts).  ::before content is
   NOT part of textContent, so every status assertion that reads the text still sees exactly the words we emit. */
.ih-top .ih-status span + span::before { content:"·"; margin-right:.5em; color:var(--ink3); }
.ih-top .ih-status .br::before, .ih-top .ih-status .br + span::before { content:none; margin:0; }
@media (max-width: 1440px) { .ih-top .ih-status { font-size:1.05rem; } }   /* 2026-09-17 pm: at 1366 px the full counter (3 states + Hz) clipped the Hz reading at 1.3rem; 1.12rem fits with the sidebar open */
/* Streamlit dims every element of a running fragment ("stale") after 0.5 s — on a slow laptop / Tailscale link that reads as the whole
   page flashing once a second.  The data is never stale for more than a tick: keep full opacity, no transition. */
[data-testid="stMain"] .stale-element, [data-testid="stMain"] [data-stale="true"] { opacity:1 !important; transition:none !important; }
.ih-wordmark { display:inline-flex; align-items:center; gap:.32em; font:700 1.3rem/1 var(--sans); color:var(--ink); letter-spacing:.02em; }
.ih-wordmark .sq { display:inline-block; width:.62em; height:.62em; background:var(--red); }
.ih-crumb { font:500 1rem/1.3 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); }
.ih-badge { font:500 .85rem/1 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); border:1px solid var(--rule); padding:4px 8px; }

/* status glyphs: small inline STROKE SVGs (16 px grid, currentColor) + the word — never colour alone, never a dingbat.
   .g carries the status colour (icon only); the word beside it stays in the text ink. .gw = icon + word, gap-spaced. */
.g { display:inline-flex; align-items:center; line-height:1; flex:none; }
.g svg { width:1em; height:1em; display:block; }
.gw { display:inline-flex; align-items:center; gap:.3em; vertical-align:text-bottom; }
.g.ok, .st.ok, .k.ok { color:var(--green); } .g.amber, .st.amber, .k.amber { color:var(--amber); }
.g.fail, .st.fail, .k.fail { color:var(--fail); } .g.na, .st.na, .k.na { color:var(--ink3); } .g.gold { color:var(--gold); } .g.red { color:var(--red); }
.g.reset, .g.pause { color:var(--amber); }

/* status strip: glyph + word, never color alone; feed glyphs = vehicle icon in a status ring */
.ih-status { display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; font:500 1.15rem/1.5 var(--mono); letter-spacing:.06em; text-transform:uppercase; color:var(--ink2); font-variant-numeric:tabular-nums; }   /* tabular digits: the clock / sample count changing width used to nudge every chip after it */
.ih-status b { color:var(--ink); font-weight:500; }
/* the reserved "· PAUSED" slot: EMPTY span, width painted by the pseudo-element (textContent stays clean, so "PAUSED" in the
   status text still means PAUSED) — inserting the words mid-strip used to shove every chip after them sideways */
.ih-status .fg { display:inline-flex; align-items:center; justify-content:center; width:1.5rem; height:1.5rem; border-radius:50%; border:2px solid var(--na); background:var(--card2); vertical-align:middle; }
.ih-status .fg + .fg { margin-left:2px; }
.ih-status .fg svg { width:1.1rem; height:1.1rem; display:block; }
.ih-status .fg.ok { border-color:var(--green); } .ih-status .fg.amber { border-color:var(--amber); } .ih-status .fg.fail { border-color:var(--fail); } .ih-status .fg.na { border-color:var(--na); }

/* one-line card header: mono small caps + red mark */
.ih-card-h { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 8px; font:500 1rem/1.4 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); border-bottom:1px solid var(--rule); padding:0 0 6px; margin:16px 0 8px; }
.ih-card-h::before { content:""; display:inline-block; width:6px; height:6px; background:var(--red); flex:none; align-self:center; }
.ih-card-h .r { margin-left:auto; text-transform:none; letter-spacing:0; color:var(--ink3); }

/* THREE tiles, one row, same size: label · hero value · one sub line · status glyph+word (toned tiles).  Since 2026-09-14 they render
   inside the More expander, not above the panel ("only the target track is useful, and it is on the plot").
   Tone = a 2 px TOP rule (square corners) + the glyph+word row — never a left-border accent, never colour alone. */
.ih-tiles { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:12px; margin:0 0 10px; }
.ih-tile { display:flex; flex-direction:column; gap:6px; background:var(--card); border-top:2px solid var(--rule); padding:12px 16px 12px; min-width:0; overflow-wrap:anywhere; }
.ih-tile .k { font:500 .85rem/1.3 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); }
.ih-tile .v { font:600 3.2rem/1 var(--sans); display:flex; flex-wrap:wrap; align-items:baseline; gap:6px; color:var(--ink); letter-spacing:-.02em; font-variant-numeric:proportional-nums; }
.ih-tile .v small { font:500 1.3rem/1 var(--sans); color:var(--ink2); letter-spacing:0; }
.ih-tile .s { font:500 1rem/1.4 var(--mono); color:var(--ink2); }
.ih-tile .s b { color:var(--ink); font-weight:500; }
.ih-tile .s .closing { color:var(--green); } .ih-tile .s .opening { color:var(--ink3); }
.ih-tile .s .muted { color:var(--ink3); } .ih-tile .s .cpa { color:var(--ink); font-weight:500; }
.ih-tile .st { font:500 .85rem/1 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); padding-top:2px; }
.ih-tile.gold { border-top-color:var(--gold); } .ih-tile.ok { border-top-color:var(--green); } .ih-tile.amber { border-top-color:var(--amber); } .ih-tile.fail { border-top-color:var(--fail); } .ih-tile.na { border-top-color:var(--rule); }

/* stat chips (small tiles) — the "More" expander */
.ih-chips { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:8px; }
.ih-chip { display:flex; flex-direction:column; gap:4px; background:var(--card2); border-top:2px solid var(--rule); padding:10px 12px; min-width:0; overflow-wrap:anywhere; }
.ih-chip .k { font:500 .85rem/1.3 var(--mono); display:flex; align-items:center; gap:6px; color:var(--ink2); }
.ih-chip .k .ic { display:inline-flex; flex:none; } .ih-chip .k .ic svg { width:1.2rem; height:1.2rem; display:block; }
.ih-chip .v { font:600 2.0rem/1.15 var(--sans); color:var(--ink); letter-spacing:-.01em; font-variant-numeric:proportional-nums; }
.ih-chip .s { font:500 1rem/1.4 var(--mono); color:var(--ink2); }
.ih-chip .st { font:500 .85rem/1 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); padding-top:4px; }
.ih-chip.ok { border-top-color:var(--green); } .ih-chip.amber { border-top-color:var(--amber); } .ih-chip.fail { border-top-color:var(--fail); } .ih-chip.na { border-top-color:var(--rule); }
div[data-testid="stIFrame"] iframe, iframe[title="st.iframe"] { background:var(--surface); }

/* TRACK CHANGES card (More expander): last 5 track-id changes, newest on top, current ids bold */
.ih-events { display:flex; flex-direction:column; gap:2px; background:var(--card2); border-top:2px solid var(--rule); padding:10px 12px; margin:8px 0; min-width:0; }
.ih-events .k { font:500 .85rem/1.3 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); padding-bottom:4px; }
.ih-events .e { font:400 1rem/1.6 var(--mono); color:var(--ink2); overflow-wrap:anywhere; }
.ih-events .e b { color:var(--ink); font-weight:600; } .ih-events .e .r { color:var(--ink3); } .ih-events .none { color:var(--ink3); }

/* callouts + text blocks (kicker = glyph + word; tone = 2 px top rule) */
.ih-callout { display:flex; flex-direction:column; gap:4px; background:var(--card); border-top:2px solid var(--red); padding:12px 16px; margin:8px 0 16px; min-width:0; overflow-wrap:anywhere; }
.ih-callout.grey { border-top-color:var(--rule); } .ih-callout.green { border-top-color:var(--green); } .ih-callout.amber { border-top-color:var(--amber); }
.ih-callout .k { font:500 .85rem/1.3 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); }
.ih-callout .t { font:600 1.15rem/1.3 var(--sans); color:var(--ink); }
.ih-callout .b { font:400 1rem/1.5 var(--sans); color:var(--ink2); }
.ih-callout .b b, .ih-kv dd b { color:var(--ink); font-weight:600; }
.ih-callout .b code, .ih-kv code { font:400 1rem/1 var(--mono); color:var(--ink); background:var(--card2); padding:1px 4px; }
.ih-kv { display:grid; grid-template-columns:minmax(5rem,max-content) minmax(0,1fr); gap:4px 12px; margin:4px 0 12px; }
.ih-kv dt { font:500 .85rem/1.5rem var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink3); }
.ih-kv dd { margin:0; font:400 1rem/1.5 var(--sans); color:var(--ink2); min-width:0; overflow-wrap:normal; word-break:keep-all; }   /* 2026-09-15: wrap at spaces / "·" only — "10.191.28.205:27017" broke as "2701 / 7" */
.ih-label { font:500 .85rem/1.3 var(--mono); color:var(--red); letter-spacing:.08em; text-transform:uppercase; margin:4px 0 8px; }
.ih-label.grey { color:var(--ink2); }
.ih-h1 { font:700 1.6rem/1.1 var(--sans); color:var(--ink); margin:0 0 8px; letter-spacing:-.01em; }
.ih-sub { font:400 1rem/1.5 var(--sans); color:var(--ink2); max-width:64rem; margin:0 0 16px; }
.ih-sub b { color:var(--ink); font-weight:600; }
.ih-list { list-style:none; padding:0; margin:4px 0 12px; }
.ih-list li { display:grid; grid-template-columns:1.5rem 1fr; gap:8px; padding:8px 0; border-bottom:1px solid var(--rule); }
.ih-list li .n { font:500 .85rem/1.6 var(--mono); color:var(--red); }
.ih-list li .h { font:600 1rem/1.4 var(--sans); color:var(--ink); }
.ih-list li .d { font:400 1rem/1.4 var(--sans); color:var(--ink2); }
.ih-rule { border:0; border-top:1px solid var(--rule); margin:16px 0; }
.ih-footer { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px 16px; font:500 .85rem/1.4 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink3); border-top:1px solid var(--rule); padding-top:12px; margin-top:16px; }
/* 1366x768: the legend was 97 px / 5 lines (Normal) and 131 px / 6 lines (Large) of a 682 px viewport.  The text itself is
   shortened in views/1_live.py; this tightens the type on short screens.  It is never truncated or hidden: it still wraps. */
@media (max-height:850px) { .ih-footer { display:block; font-size:.75rem; line-height:1.3; letter-spacing:.04em; padding-top:8px; margin-top:8px; }
                            .ih-footer span + span::before { content:" · "; } }   /* one flowing paragraph: the two flex items were 1 + 2 lines, the text is 2 */
.ih-side-label { font:500 .85rem/1.3 var(--mono); color:var(--red); letter-spacing:.08em; text-transform:uppercase; margin:16px 0 4px; }

/* tables: tabular figures only here */
table.ih-table { width:100%; border-collapse:collapse; font:400 1rem/1.4 var(--sans); }
table.ih-table th { text-align:left; font:500 .85rem/1.3 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); padding:8px; border-bottom:1px solid var(--rule); }
table.ih-table td { padding:8px; border-bottom:1px solid var(--rule); color:var(--ink); vertical-align:middle; }
table.ih-table td.num { font:400 1rem/1.4 var(--mono); font-variant-numeric:tabular-nums; text-align:right; }
</style>
"""
_TOKENS = {"__RED__": RED, "__INK__": INK, "__INK2__": INK2, "__INK3__": INK3, "__SURFACE__": SURFACE, "__CARD__": CARD, "__CARD2__": CARD2, "__RULE__": RULE,
           "__GREEN__": GREEN, "__AMBER__": AMBER, "__FAIL__": FAIL, "__NA__": NA, "__GOLD__": GOLD, "__TARGET__": TARGET, "__INTERCEPTOR__": INTERCEPTOR,
           "__MONO__": MONO, "__SANS__": SANS}
CSS = _CSS
for _k, _v in _TOKENS.items():
    CSS = CSS.replace(_k, _v)


def esc(s: object) -> str:
    return _html.escape(str(s))


def dot(cls: str, icon: str | None = None) -> str:
    """The coloured icon alone: '<span class="g ok"><svg…/></span>' (cls = STATUS class or ICONS key; ``icon`` overrides the shape)."""
    svg = ICONS.get(icon or cls)
    return f'<span class="g {esc(cls)}">{svg}</span>' if svg else ""


def glyph(cls: str, word: str | None = None, icon: str | None = None) -> str:
    """Status icon + word, gap-spaced: '<span class="gw"><span class="g ok"><svg…/></span>OK</span>'. Never colour alone —
    the icon wears the status colour, the word stays in the text ink.  cls: ok | amber | fail | na (STATUS, default word)
    or any ICONS key with an explicit word (red → LIVE, gold → CPA, …)."""
    if cls in STATUS:
        _, _, w = STATUS[cls]
    elif cls in ICONS and word is not None:
        w = word
    else:
        return ""
    return f'<span class="gw">{dot(cls, icon)}{esc(word if word is not None else w)}</span>'


def build_badge() -> str:
    """'build 2026-09-10 11:42 · grader: spa <ver>' (or 'grader: legacy' without chaos-spa) from the newest source file."""
    here = os.path.dirname(os.path.abspath(__file__))
    files = glob.glob(os.path.join(here, "*.py")) + glob.glob(os.path.join(os.path.dirname(here), "pages", "*.py"))
    mt = max((os.path.getmtime(f) for f in files), default=time.time())
    import ih

    ver = ""
    if ih.spa_available():
        try:
            import spa

            ver = getattr(spa, "__version__", "") or ""
        except Exception:
            ver = ""
        ver = " · grader: spa" + ("" if not ver or "unknown" in ver else f" {ver}")
    else:
        ver = " · grader: legacy"          # chaos-spa (optional official grader) not installed — in-house error math
    return f"build {time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))}{ver}"


# ── page chrome ──────────────────────────────────────────────────────────────
def setup() -> None:
    """Call once, first thing, from the entrypoint."""
    st.set_page_config(
        page_title="Ironhide test dashboard",
        page_icon=":material/radar:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(scale_css(text_scale()), unsafe_allow_html=True)   # sidebar "Text size": html font-size = 16 px x scale (everything is rem)


def topbar_html(crumb: str, badge: str | None = None, status_parts: list[str] | None = None, status_parts2: list[str] | None = None) -> str:
    """ONE top line: wordmark + crumb (left) · optional live status strip (middle) · build badge (right).
    ``status_parts2`` is the status box's SECOND row (the target-track update counter)."""
    right = f'<span class="ih-badge">{esc(badge)}</span>' if badge else ""
    mid = status_html(status_parts, status_parts2) if status_parts else ""
    # 2026-09-14: the status strip has its OWN row with a FIXED height (2 lines under 2400 px, 1 above) — its text changes every tick
    # (PAUSED, TRACK CHANGED, state words) used to wrap / unwrap the flex row and move the whole panel 32-36 px ("everything jumps")
    return (f'<div class="ih-top"><div class="row1"><div class="l"><span class="ih-wordmark"><span class="sq"></span>CHAOS</span>'
            f'<span class="ih-crumb">{esc(crumb)}</span></div><div class="r">{right}</div></div>{mid}</div>')


def topbar(crumb: str, badge: str | None = None, status_parts: list[str] | None = None, status_parts2: list[str] | None = None) -> None:
    st.markdown(topbar_html(crumb, badge, status_parts, status_parts2), unsafe_allow_html=True)


def status_html(parts: list[str], parts2: list[str] | None = None) -> str:
    """ONE <div class="ih-status"> with NO nested block: ``parts2`` starts after a zero-height flex break (.br) and its
    spans carry .s2.  Flat markup on purpose — every test / tool reads the strip with
    ``re.search(r'<div class="ih-status">(.*?)</div>')`` and a nested <div> would cut the capture at the first row."""
    row1 = "".join(f"<span>{p}</span>" for p in parts)
    row2 = ('<span class="br"></span>' + "".join(f'<span class="s2">{p}</span>' for p in (parts2 or ""))) if parts2 else ""
    return '<div class="ih-status">' + row1 + row2 + "</div>"


def status(parts: list[str]) -> None:
    """One-line mono status strip. Parts may carry inline <b>/<span class="g ok">."""
    st.markdown(status_html(parts), unsafe_allow_html=True)


line = status  # backwards-compatible name


def feed_glyph(role: str, state: str, age: float | None = None, n_alive: int | None = None, n_total: int | None = None) -> str:
    """Vehicle icon inside a status ring: green alive / amber stale / red frozen-or-down / muted none.
    The word travels in the title (hover) — the strip stays one line."""
    from . import icons
    cls = FEED_CLS.get(state, "na")
    svg = (icons.interceptor_svg if role == "interceptor" else icons.target_svg)(0, color=INTERCEPTOR if role == "interceptor" else TARGET,
                                                                                   surface=CARD2, halo=False, size=16, animate=False)
    age_s = "—" if age is None else f"{age:.1f} s"
    cnt = "" if n_total is None else f" · {n_alive}/{n_total} alive"
    return f'<span class="fg {cls}" title="MAVLink {esc(role)} · {esc(state.upper())} · age {age_s}{cnt}">{svg}</span>'


def card_header(text: str, right: str | None = None) -> None:
    r = f'<span class="r">{esc(right)}</span>' if right else ""
    st.markdown(f'<div class="ih-card-h"><span>{esc(text)}</span>{r}</div>', unsafe_allow_html=True)


def label(text: str, grey: bool = False) -> None:
    st.markdown(f'<div class="ih-label{" grey" if grey else ""}">{esc(text)}</div>', unsafe_allow_html=True)


def headline(label_text: str, title: str, sub: str | None = None) -> None:
    h = f'<div class="ih-label">{esc(label_text)}</div><div class="ih-h1">{esc(title)}</div>'
    if sub:
        h += f'<p class="ih-sub">{sub}</p>'
    st.markdown(h, unsafe_allow_html=True)


TILE_WORD = {"gold": "CPA"}   # default status word per tone (STATUS words otherwise); na / '' = neutral: no status row


def tile_html(label_text: str, value: str, unit: str = "", sub_html: str = "", tone: str = "", word: str | None = None) -> str:
    """One banner tile: mono label · hero value (+ small unit) · one mono sub line (may carry inline spans) · status row.
    tone: gold | ok | amber | fail -> 2 px TOP rule in the tone colour + an icon+word status row (``word`` overrides the
    default OK / WARN / FAIL / CPA); na | '' = neutral (rule-coloured top edge, no status row)."""
    u = f"<small>{esc(unit)}</small>" if unit else ""
    s = f'<div class="s">{sub_html}</div>' if sub_html else ""
    st_ = ""
    if tone in ("ok", "amber", "fail", "gold"):
        st_ = f'<div class="st">{glyph(tone, word if word is not None else TILE_WORD.get(tone))}</div>'
    return f'<div class="ih-tile {esc(tone)}"><div class="k">{esc(label_text)}</div><div class="v">{esc(value)}{u}</div>{s}{st_}</div>'


def tiles_html(tiles: list[str]) -> str:
    return f'<div class="ih-tiles">{"".join(tiles)}</div>'


def tiles(tiles_: list[str]) -> None:
    st.markdown(tiles_html(tiles_), unsafe_allow_html=True)


def chips_html(items: list[tuple[str, str, str, str]]) -> str:
    """items: (label sentence-case, value, sub, status_class in ok|amber|fail|na|'').
    A status class colours the 2 px TOP rule AND adds an icon+word status line; '' = neutral (no status)."""
    from . import icons
    cells = []
    for k, v, s, cls in items:
        stl = f'<div class="st">{glyph(cls)}</div>' if cls in STATUS else ""
        sub = f'<div class="s">{esc(s)}</div>' if s else ""
        kl = k.lower()
        ic = ""
        if not kl.startswith("mavlink"):          # vehicle icons only on the FEED chips (not "Target track" etc.)
            pass
        elif "interceptor" in kl:
            ic = f'<span class="ic">{icons.interceptor_svg(30, color=INTERCEPTOR, surface=CARD2, halo=False, size=18)}</span>'
        elif "target" in kl:
            ic = f'<span class="ic">{icons.target_svg(0, color=TARGET, surface=CARD2, halo=False, size=18)}</span>'
        cells.append(f'<div class="ih-chip {esc(cls)}"><div class="k">{ic}{esc(k)}</div><div class="v">{esc(v)}</div>{sub}{stl}</div>')
    return f'<div class="ih-chips">{"".join(cells)}</div>'


def chips(items: list[tuple[str, str, str, str]]) -> None:
    st.markdown(chips_html(items), unsafe_allow_html=True)


_TONE_CLS = {"red": "fail", "green": "ok", "amber": "amber", "grey": "na"}   # callout tone -> status icon class


def callout_html(kicker: str, title: str, body: str, tone: str = "red") -> str:
    """Callout: kicker = status icon + word (in the text ink), title, body; tone = the 2 px top rule (red / green / amber / grey)."""
    cls = {"red": "", "grey": " grey", "green": " green", "amber": " amber"}.get(tone, "")
    return (f'<div class="ih-callout{cls}"><div class="k">{glyph(_TONE_CLS.get(tone, "na"), kicker)}</div>'
            f'<div class="t">{esc(title)}</div><div class="b">{body}</div></div>')


def callout(kicker: str, title: str, body: str, tone: str = "red") -> None:
    st.markdown(callout_html(kicker, title, body, tone), unsafe_allow_html=True)


def kv_html(rows: list[tuple[str, str]]) -> str:
    """Compact definition list: mono small-caps term, body-size description (may carry <b>/<code>)."""
    return '<dl class="ih-kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def kv(rows: list[tuple[str, str]]) -> None:
    st.markdown(kv_html(rows), unsafe_allow_html=True)


def numbered(items: list[tuple[str, str]]) -> None:
    lis = "".join(
        f'<li><div class="n">{i}</div><div><div class="h">{esc(h)}</div><div class="d">{d}</div></div></li>'
        for i, (h, d) in enumerate(items, start=1)
    )
    st.markdown(f'<ul class="ih-list">{lis}</ul>', unsafe_allow_html=True)


def rule() -> None:
    st.markdown('<hr class="ih-rule">', unsafe_allow_html=True)


def footer(right: str = "IRONHIDE TEST DASHBOARD · PROTOTYPE") -> None:
    st.markdown(f'<div class="ih-footer"><span>{esc(FOOTER_LEFT)}</span><span>{esc(right)}</span></div>', unsafe_allow_html=True)


def table_html(cols: list[str], rows: list[list[object]], num_cols: set[int] | None = None) -> str:
    num_cols = num_cols or set()
    th = "".join(f"<th>{esc(c)}</th>" for c in cols)
    trs = []
    for r in rows:
        tds = "".join(
            f'<td class="{"num" if i in num_cols else ""}">{c if isinstance(c, str) and c.startswith("<") else esc(c)}</td>'
            for i, c in enumerate(r)
        )
        trs.append(f"<tr>{tds}</tr>")
    return f'<table class="ih-table"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def table(cols: list[str], rows: list[list[object]], num_cols: set[int] | None = None) -> None:
    st.markdown(table_html(cols, rows, num_cols), unsafe_allow_html=True)


def event_row(e) -> tuple:
    """(t, role, old, new, rule) from an engine track-change event (tuple or dict shape)."""
    if isinstance(e, dict):
        return (float(e.get("t", 0.0)), str(e.get("role", "target")), e.get("old"), e.get("new"), str(e.get("rule") or ""))
    t, role, old, new = (list(e) + [None] * 4)[:4]
    return (float(t), str(role), old, new, "")


def events_rows(events, cur_tgt=None, cur_itc=None, hms=None, n: int = 5) -> list[list]:
    """The last ``n`` track-id changes as JSON-able rows [hms, role, old, new, is_current, rule], newest LAST
    (the renderers reverse).  ``hms`` formats an epoch (ih.data.pdt_hms)."""
    hms = hms or (lambda t: f"{t:.0f}")
    out = []
    for e in list(events or ())[-n:]:
        t, role, old, new, rule = event_row(e)
        cur = new is not None and new == (cur_tgt if role == "target" else cur_itc)
        out.append([hms(t), role, old, new, bool(cur), rule])
    return out


def events_card_html(rows: list[list]) -> str:
    """TRACK CHANGES card: "07:22:31 · target track #129 → <b>#177</b> · rule" per row, newest on top."""
    lines = []
    for hms, role, old, new, cur, rule in reversed(rows or []):
        o = "—" if old is None else f"#{old}"
        n = "—" if new is None else f"#{new}"
        nn = f"<b>{esc(n)}</b>" if cur else esc(n)
        r = f' <span class="r">· {esc(rule)}</span>' if rule else ""
        lines.append(f'<div class="e">{esc(hms)} · {esc(role)} track {esc(o)} → {nn}{r}</div>')
    body = "".join(lines) if lines else '<div class="e none">no track-id change yet</div>'
    return f'<div class="ih-events"><div class="k">Track changes</div>{body}</div>'


def events_card(rows: list[list]) -> None:
    st.markdown(events_card_html(rows), unsafe_allow_html=True)


def source_line_html(mode: str, *, mru=None, host: str = "", run: str = "", flight=None, day: str = "8/28", window: str = "") -> str:
    """The sidebar's source line: "● LIVE · MRU 91 · run Beige_Badger" (red glyph) or "● ARCHIVE · 8/28 · Flight 1"
    (green glyph) — one .ih-status strip; B (data source / app) supplies the state, this renders it."""
    if str(mode).lower() == "live":
        who = f"MRU {esc(mru)}" if mru not in (None, "") else (esc(host) or "no host")
        run_s = f"run <b>{esc(run)}</b>" if run else "no run selected"
        return f'<div class="ih-status"><span>{glyph("red", "LIVE")}</span><span><b>{who}</b></span><span>{run_s}</span></div>'
    fl = f"Flight {esc(flight)}" if flight not in (None, "") else "no flight"
    w = f"<span>{esc(window)}</span>" if window else ""
    return f'<div class="ih-status"><span>{glyph("ok", "ARCHIVE")}</span><span><b>{esc(day)} · {fl}</b></span>{w}</div>'


def side_label(text: str) -> None:
    st.markdown(f'<div class="ih-side-label">{esc(text)}</div>', unsafe_allow_html=True)
