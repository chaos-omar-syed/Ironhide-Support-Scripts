"""Chrome round 3 (2026-09-11), revised 2026-09-14 ("text is very massive on the live view even at Normal"): the panel chrome
follows the figure font without over-sizing it.  ih.plots READOUT_PX / READOUT_SUB_PX / TITLE_PX = 18 / 12 / 16
-> the per-card header strip is err_strip_px(18, 12)[0] = 52 px; the card header row is HEADER_PX = 32 px (was 36) with a
primary-ink title at the measured figure font, never under 14 px (was >= 18 / font + 4); the view-lock pill is an inline chip in the
map header's right group (no longer an absolute overlay on the plot).  A fitted 1080p browser panel (688 px) MUST stay two-column
(sep >= SEP_MIN_PX 110) at the 53 px plot-area floor: ERR_MIN = 4*(53+52) + 8 + 28 + 30 = 486, sep = 688 - 64 - 486 = 138.
Text size defaults: Large 1440p -> X-Large, Desktop 1080p -> Large, Laptop -> Normal; the fixed ih-status row is ONE row at 1.3 rem."""
import re

from ih import liveserver as LS
from ih import plots as PL
from ih import theme as T

SCALE = {0.75, 0.85, 1.0, 1.15, 1.3, 1.6, 2.0, 3.2}
FONT_RE = re.compile(r"font(?:-size)?:\s*(?:\d{3}\s+)?([\d.]+)rem")


def test_error_card_geometry_constants():
    assert (PL.READOUT_PX, PL.READOUT_SUB_PX, PL.TITLE_PX) == (18, 12, 16)   # 2026-09-14 text shrink (were 20 / 15 / 20)
    assert LS.ERR_HDR_PX == PL.err_strip_px(PL.READOUT_PX, PL.READOUT_SUB_PX)[0] == 52   # the header strip follows the plots constants
    assert LS.ERR_T == 8 and LS.ERR_B == 28 and LS.ERR_GAP == 10 and LS.SEP_MIN_PX == 110   # unchanged
    assert LS.MIN_CARD_PX == 53                                           # the plot-area floor (a fitted 1080p panel stays two-column with room to spare: see module docstring)
    assert LS.ERR_MIN_PX == 4 * (LS.MIN_CARD_PX + LS.ERR_HDR_PX) + LS.ERR_T + LS.ERR_B + 3 * LS.ERR_GAP == 486
    assert LS.ONE_ERR_PX == LS.ERR_MIN_PX == 486                          # stacked-mode error panel = the floor
    assert LS.ONE_VEL_PX == 3 * (LS.MIN_CARD_PX + LS.ERR_HDR_PX) + LS.ERR_T + LS.ERR_B + 2 * LS.ERR_GAP == 371


def test_fitted_1080p_browser_panel_keeps_two_columns():
    fit = LS._fit_two(688)
    assert fit is not None and fit == (688 - 2 * LS.HEADER_PX - LS.ERR_MIN_PX, LS.ERR_MIN_PX) == (138, 486)   # the error panel takes its floor, the separation card the rest
    assert fit[0] >= LS.SEP_MIN_PX
    assert (fit[1] - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - LS.ERR_HDR_PX >= LS.MIN_CARD_PX
    for col_h in (720, 860):                                              # taller columns: floor-first (720), then the ~32 % split (860)
        sep, err = LS._fit_two(col_h)
        assert sep + err == col_h - 2 * LS.HEADER_PX and sep >= LS.SEP_MIN_PX
        assert (err - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - LS.ERR_HDR_PX >= LS.MIN_CARD_PX
    assert LS._fit_two(720) == (720 - 2 * LS.HEADER_PX - LS.ERR_MIN_PX, LS.ERR_MIN_PX)
    assert LS._fit_two(860)[1] > LS.ERR_MIN_PX and abs(LS._fit_two(860)[0] / (860 - 2 * LS.HEADER_PX) - LS.SEP_FRAC) < 0.01
    assert LS._fit_two(600) is None                                       # too short for a >= 110 px separation card -> stacked


def test_presets_stay_two_column_with_32px_headers():
    assert LS.HEADER_PX == 32
    Ld, Lg, Ll = LS.layout("Desktop 1080p", 1.0), LS.layout("Large 1440p", 1.0), LS.layout("Laptop", 1.0)
    assert (Ld["mode"], Ld["panel"], Ld["header"], Ld["vel"]) == ("two", 860, LS.HEADER_PX, LS.vel_strip_px(860)) and Ld["vel"] == 206
    assert (Lg["mode"], Lg["panel"], Lg["header"], Lg["vel"]) == ("two", 1180, LS.HEADER_PX, LS.vel_strip_px(1180)) and Lg["vel"] == 230
    one = LS.one_row(Ll["panel"], 1366, Ll["font_px"], 1.0)                 # 2026-09-15 two-up first row: a square-plot map beside the separation card, both panel - ONE header
    assert (Ll["mode"], Ll["panel"], Ll["header"], Ll["sep"], Ll["err"], Ll["vel"]) == ("one", 600, LS.HEADER_PX, 600 - LS.HEADER_PX, LS.ERR_MIN_PX, LS.ONE_VEL_PX)
    assert (Ll["sep"], Ll["map"]) == (one["sep"], one["map"]) == (568, 568) and one["plot"] == one["plot_w"]   # follows LS.one_row(): square plot area
    for L in (Ld, Lg):                                                    # derived, not pinned: map = panel - 2 headers - strip; (sep, err) = _fit_two(panel)
        assert L["map"] == L["panel"] - 2 * L["header"] - L["vel"] and (L["sep"], L["err"]) == LS._fit_two(L["panel"])
        assert 2 * L["header"] + L["map"] + L["vel"] == L["panel"] == 2 * L["header"] + L["sep"] + L["err"]
        assert L["sep"] >= LS.SEP_MIN_PX and (L["err"] - LS.ERR_T - LS.ERR_B - 3 * LS.ERR_GAP) / 4 - LS.ERR_HDR_PX >= LS.MIN_CARD_PX
    assert Ll["map"] == Ll["sep"] == Ll["panel"] - Ll["header"]              # one-mode: header + figure = the iframe height, for BOTH cards of the first row


def test_panel_chrome_text_is_larger_and_primary_ink():
    js, css = LS.responsive_js(), LS.panel_css()
    assert f"HEADER={LS.HEADER_PX}" in js and f"ERR_HDR={LS.ERR_HDR_PX}" in js and f"MIN_CARD={LS.MIN_CARD_PX}" in js and f"ERR_MIN={LS.ERR_MIN_PX}" in js and f"SEP_MIN={LS.SEP_MIN_PX}" in js
    assert "HEADER=32" in js and "ERR_HDR=52" in js and "MIN_CARD=53" in js and "ERR_MIN=486" in js and "SEP_MIN=110" in js   # the JS re-fits with the server's numbers
    assert 'setProperty("--hf",Math.max(14,g.font)+"px")' in js              # header title / status text = the measured figure font, never under 14 px (2026-09-14: the +4 read as "massive")
    assert 'setProperty("--cf",Math.max(13,g.font-1)+"px")' in js
    assert f"height:{LS.HEADER_PX}px" in css and "height:32px" in css and "height:36px" not in css   # the .h row follows HEADER_PX
    assert "var(--hf,1rem)" in css and f".h>span:first-child{{color:{T.INK};}}" in css   # title in primary ink
    assert "font-size:calc(var(--hf,1rem) * .85)" in css                     # caption / right text at .85 of the title
    assert f"color:{T.INK3}" in css and "text-overflow" not in css
    assert f"#st,#tw,#hud{{font:500 var(--hf,1rem)/1 {T.MONO}" in css        # status text follows --hf
    # the view-lock pill is an inline chip IN the map header's right group (.h .r) at .85 of the title, not an absolute overlay on the plot
    # (2026-09-14: the DOM map overlay .ov / .ov-head IS absolutely positioned over the plot area by design — the rule under
    #  test is the PILL, which must stay an inline chip in the header, so check the pill's own rule, not the whole sheet)
    pill_rule = re.search(r"#pill\{[^}]*\}", css).group(0)
    assert "position:absolute" not in pill_rule and "#pill{cursor:pointer;" in css and f"font:500 calc(var(--hf,1rem) * .85)/1 {T.MONO}" in css
    for rule in re.findall(r"#(?:st|tw|hud|pill)\{[^}]*\}", css):
        assert "position:absolute" not in rule, rule
    body = LS.panel_body()
    pill = body.index('<span id="pill" hidden')
    assert body.rindex('<div class="h"', 0, pill) < body.rindex('<span class="r">', 0, pill)      # inside a header's right group ...
    assert body.index('<div id="map"', pill) < body.index('<div class="h"', pill)                  # ... of the MAP header (the #map figure follows it)


def test_theme_sizes_on_scale_and_status_line_larger():
    sizes = {float(v) for v in FONT_RE.findall(T.CSS)}
    assert sizes and sizes <= SCALE and min(sizes) >= 0.75
    assert ".ih-status { display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; font:500 1.15rem/1.5 var(--mono);" in T.CSS
    for need in (".ih-tile .k { font:500 .85rem", ".ih-tile .s { font:500 1rem", ".ih-tile .v { font:600 3.2rem", ".ih-card-h { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 8px; font:500 1rem"):
        assert need in T.CSS, need
    ih_rules = "".join(r for r in re.findall(r"\.ih-[^{]*\{[^}]*\}", T.CSS) if not r.startswith(".ih-top"))   # the crew-facing .ih-* components never clip
    for bad in ("nowrap", "overflow:hidden", "overflow: hidden", "text-overflow", "ellipsis"):   # (.ih-top is the ONE fixed-height block: the status row
        assert bad not in ih_rules, bad                                                          #  reserves 2 lines so the panel below never moves; e2e checks it fits)
    # 2026-09-15: the fixed status box is ONE row at EVERY width ("2 rows just squishes the plot") — one 1.7em line + the
    # 4 px flex row-gap, in em so it tracks the "Text size" factor; the row never wraps (nowrap + white-space:nowrap) and
    # overflow:hidden clips rather than growing, so the panel below never moves.  The type is 1.3rem, the gap 0 8px.
    assert ".ih-top .ih-status { --st-rows:1; --st-row2:0px; height:calc(1.7em + 4px); overflow:hidden;" in T.CSS
    assert "align-content:flex-start; flex-wrap:nowrap; white-space:nowrap; font-size:1.3rem; gap:0 8px; }" in T.CSS
    assert "@media (min-width: 2400px) { .ih-top .ih-status { --st-rows:1; } }" not in T.CSS   # no width media query at all
    assert "--st-rows:2" not in T.CSS and ".ph" not in T.CSS                                  # never two rows; no placeholder (.ph) rules
    # status parts carry no icons any more: the colour rides on .c-ok / .c-amber / .c-fail, the words stay in the text ink
    for cls, tok in (("c-ok", "--green"), ("c-amber", "--amber"), ("c-fail", "--fail")):
        assert ".ih-status .%s { color:var(%s) !important; }" % (cls, tok) in T.CSS, cls
    assert '.ih-top .ih-status span + span::before { content:"\u00b7"; margin-right:.5em; color:var(--ink3); }' in T.CSS


def test_text_scale_defaults_per_preset():
    assert T.TEXT_SCALE_DEFAULT == {"Large 1440p": "X-Large", "Desktop 1080p": "Large"}
    assert T.text_scale_label({"screen": "Large 1440p"}) == "X-Large" and T.text_scale({"screen": "Large 1440p"}) == 1.3
    assert T.text_scale_label({"screen": "Desktop 1080p"}) == "Large" and T.text_scale({"screen": "Desktop 1080p"}) == 1.15
    assert T.text_scale_label({"screen": "Laptop"}) == "Normal" and T.text_scale({}) == 1.0
    assert T.text_scale({"screen": "Large 1440p", "text_scale": "Normal"}) == 1.0   # an explicit pick wins
    src = open(__file__.replace("tests/test_chrome_round3.py", "app.py")).read()
    assert 'T.TEXT_SCALE_DEFAULT.get(str(s.get("screen", "")), "Normal")' in src   # the sidebar radio's initial value honours the table
