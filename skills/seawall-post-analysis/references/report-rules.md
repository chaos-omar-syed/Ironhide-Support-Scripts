# Report rules — BINDING presentation rules & report structure

These are the user's stated rules from the Seawall Aug 2026 week (MRU91) and the follow-up sessions. They are binding:
do not "improve" on them. Canonical implementations: `track_correlation/tracking_tab.py` (8/26 format),
`track_correlation/engagement_tab.py` (8/27 format), `track_correlation/postprocess_report.py` (campaign pipeline + Radar Rollup),
`track_correlation/eng_plots.py` (figure hygiene), config `track_correlation/seawall_week_0824.json`.
Reference outputs: `:8899/seawall_week_report/` (rollup `index.html`, per-day `report.html`, `Radar_Rollup_standalone.html`).

---

## 1. Terminology

| Say | Never say | Note |
|-----|-----------|------|
| **track steal** | "capture", "reverse capture", "reverse-capture" | EVERY identity flip is a track steal, whichever direction (8/28 = 3 track steals: 129, 177, 1551). Swept from all config narratives. |
| **run** = radar/mongo run only (run id, friendly name) | "run" for a flight subdivision | e.g. "run 9b22a989 (Turquoise_Emu)" |
| **Flight N · Lap N** | "F1R1", "F1 Run 1", "R2", lap called "run" | Tab labels and captions. Code keys may stay `(1,2)` internally. |
| Plain day title: `8/26 — Tracking (2 flights × 2 laps)` | editorial parentheticals: "(cal shakeout)", "(best CPAs)", "clean day", "messy" | Also tab labels / pane headings: strictly factual — no "dive", "best pass", "nose-dive" adjectives in headings (allowed inside narrative where the data shows it). |
| **corruption** (identity holds, state runs away) vs **steal** (identity flips) | mixing the two | See data-gotchas D4. |
| **association failure** (detections present) vs **detection dropout** (obs gap) | "dropout" for everything | data-gotchas G10. |
| "target" / "interceptor" roles; MAVLink ids in parentheses once | bare ids in prose | e.g. "target (mav14550_1_1)". |
| Source tag on every CPA number: **truth-truth / ulog / vs-track / radar-only** | an untagged CPA | The 8/27 DAY_SUMMARY passes table is the model. |

---

## 2. What to show / hide

| Rule | Detail | Implemented |
|------|--------|-------------|
| **CPA markers/labels only < 75 m** | "real engagement attempts"; label in metres; gold star = best; dashed 75 m "engagement-attempt bar" on the distance timeline | `engagement_tab.build_overview_section` |
| **Az bias displayed only when \|az\| > 1°** | otherwise not shown on maps/cards (still computed, in tables) | `postprocess_report` az-bias cfg |
| **No MRU / antenna marker on any top-down** | none on the heat map either | `build_overview_section`, `_rr_heat_fig` ("report rule") |
| **Distance axes capped (~800 m)** on CPA-window distance plots | for CPA readability; on FULL-window timelines "a linear cap just clips the curve into walls" → log y there | `eng_plots.closing`, `build_overview_section` |
| **Log y-axis for full-engagement distance timelines** | ticks from {15,25,50,75,100,150,250,500,1000,2000,4000}, no minor ticks; pass dots ON the curve; framed white legend above gridlines | `build_overview_section` |
| **Annotate data gaps > 2.5 s** on top-downs ("⚠ Ns gap") | never draw chords across gaps (None-breaks) | `eng_plots.topdown`, `_gap_line` |
| **CPA window t−10 … t+5 s** — BINDING, hardcoded, not configurable | applies to gif3d, top-down, guidance, closing, state table | `engagement_tab`: `pre, post = 10.0, 5.0` |
| **Animations at 1× real-time** | gif3d 135 frames @ 9 fps (15 s window); steal GIF ±10 s = 101 fr @ 5 fps; FOV video 5 fps real-time | `gif3d`, `_rr_steal_gif`, `_fov_render` |
| **Every example carries RUN ID + PDT timestamp + track number** | in label AND caption ("we are missing runids") | Radar Rollup §1–§4 |
| **Late-born track**: CPA-to-track via ±120 s fallback; window stays t−10..+5 | never widen the displayed window | `build_engagement_tab` |
| **Error panels auto-fit**: robust range p99+σ, floors az/el 1°, range 10 m, alt 15 m; stats font 15 bold with units on every stat; ±1σ shaded, ±3σ dotted | nothing silently clipped → no fake gaps | `eng_plots.err_window_fig`, tracking `angle_stack` |
| **Overview story figure**: full-window ground-track map + distance timeline; target + interceptor TRUTH only — **NO radar track on it** | | `build_overview_section` |
| **Drop known-junk tracks from plots when the user says so** (8/26: trk 305 dropped) | record it in config, not code | campaign json |
| **No dashed/dotted styles in trajectory overviews** (confused with removed off-plan dashes) → solid lines, two panels (truth / track) | traj_mine only | `traj_mine.py` |
| **Legacy one-off pages** (autopsy / dash / quicklooks) NOT linked from the campaign report | | `postprocess_report` |
| **FOV section = the FOV player** (rendered animation + IN-FOV skip controls, 0.5/1/2/4×, event chips, UTC clock), not a static plot + stills | "the FOV player, not whatever you made up" | `build_fov_section`, `_FOVP_JS` |
| **GIF hygiene**: shared 256-colour palette, `dither=NONE`, optimize → ~550 KB (per-frame dithering over a photo = 33 MB); saturated colours + white path-effect halos over satellite; zoom box from the DISPLAY window, not the padded load window | | `_rr_steal_gif` |

---

## 3. Rollup card format

- Card title = report title up to the parenthetical (no-wrap link); sub-line = descriptor + tab count.
- **Numbers = labelled line-per-metric**, one per line:
  ```
  Az bias: −1.45°
  Coverage: 85 %
  Best pass: 21 m (truth-truth)
  ```
  Never dot-separated fragments ("−1.45° · 85 % · 21 m").
- Rollup index = **"Overall Rollup"** tab (auto Days table + ONE narrative arc paragraph — no duplicate per-day table, no geographic footnote, no "#1 carry-forward" line) + **"Radar Rollup"** tab (§4c below).
- Exports written alongside: `Radar_Rollup.html` (radar tab only), `Radar_Rollup_standalone.html` (every image inlined — the one-file handoff "so someone doesn't have to download everything"), `Rollup_standalone.html`, full-tree zip. Day-report links rewritten to served URLs in standalone files.

---

## 4. Report structures

### 4a. Tracking day — the 8/26 format (`tracking_tab`)
Tabs: **Summary** + one tab per **Flight N · Lap N**.
- Summary: metrics table per flight/lap (coverage %, RAW horiz med / p90, az bias, el/alt bias, meas rate, fragmentation, gaps), key-findings narrative (config-supplied), seeker cartoon + pAcq rollup.
- Per-lap tab (byte-identical to `mission_20260826`): `maps_fig` (RAW map left + az-bias-rotated map right, ONE legend), `angle_stack` (az / el / range / alt error, shared time axis, one legend entry per track, obs overlay), `posvel_fig` (3-D pos err + horizontal |Δv|; vU only when `vu_ok`), `rate_fig`, `seeker_quad` (+ `perp_fig` on inbound laps), run stats.
- Inputs via `init_mission(mdir | spec)`; any archiver dump works through `prep_from_dump`.

### 4b. Engagement day — the 8/27 format (`engagement_tab`); **tab order user-mandated**
Per engagement tab:
1. **Summary** paragraph (`summary_html`, may use `{cpa_truth} {cpa_track} {track_id}`).
2. **OVERVIEW section** (kept at the top — the user caught it being dropped): story figure = full-window ground-track map (target orange / interceptor blue, no radar track, stars < 75 m labelled in metres, gold = best) + distance timeline (log y, dots on curve, 75 m dashed bar).
3. **Seeker FOV — flight log + IR** = the FOV player (computed LIVE from ulog + IR; skipped with a console note when absent).
4. **3-D best pass** animated GIF (t−10..+5, 1×).
5. Rest: 2-D top-down (gap-annotated, no MRU marker) → closing distance → closing speed → heading error vs LOS → state-at-CPA table → `err_window_fig` → `extra_sections`.

### 4c. Radar Rollup tab — section order & contents (`build_radar_rollup`)
| § | Section | Contents |
|---|---------|----------|
| 1 | **Azimuth bias** | Day table + bar computed LIVE (`_rr_az_bias`: median signed az residual over kept target-side track CSVs; reproduces −1.45 / −0.21 / −0.44; 8/25 from config with `*`); ±1° reference lines; config narrative (cal arc). Example = the 8/26 Flight 1 · Lap 2 `maps_fig` pane as the BEFORE/AFTER (RAW vs rotated) figure. |
| 2 | **Track drops on turns** | LIVE per-day table: active %, coast-in-turns vs straight (toxic_zones gates: target-side med < 150 m; ACTIVE = confirmed + updated ±0.75 s; moving ≥ 2.5 m/s; turning = heading rate > 6°/s dilated ±3 s). **ONE plot only**: `_rr_turn_zoom_fig` of JUST the far NE turn (8/26 run 9b22a989 trk 367 in, ✕ at apex 08:46:41, successor trk 501). "Far-turn drops" table gated in-turn AND ≥ 2.5 km out AND target-keeps-flying (→ exactly trk 367 8/26 + trk 1511 8/28). Narrate 8/28's 0.8× inversion. |
| 3 | **Track corruption at close passes** | 2-D pass GIF with banner "CPA" (run 214a1a56 trk 633 @ 09:51:28) + `err_window_fig` ±12 s; narrative distinguishes corruption from steal; note the 8/28 F4 repeat. |
| 4 | **Track steals** | Steal-examples table (day · run · track · time · what) + ANIMATED steal view per steal (`_rr_steal_gif`: ±10 s @ 1× over satellite, interceptor blue / target orange with 6 s trails + velocity quivers, stolen track = green diamond + dotted trail hopping sides, PDT clock, "steal ±X.X s", flashing TRACK STEAL banner), each followed by `_rr_duel_fig` (track→target vs track→interceptor distance ±30 s, red vline at the steal). Steal moments from `flights.json` pass times. |
| 5 | **Heat map** | Week-combined percent-active over Esri satellite, 30 m cells scored over a ±75 m window, percentile colouring with bottom 15 % clamped red, per-day gap-aware path overlays, NO antenna marker, 8/25 excluded (0 target-side tracks); "Worst cells" table (landmark, per-day active %, top coasting track per cell) with ≥ 300 m NMS. |

All narratives live in the campaign config, never in code. Every §-example: RUN ID + PDT timestamp + track number.

---

## 5. Colour & style conventions

| Element | Colour | Where |
|---------|--------|-------|
| Target (truth) | red/orange (`#e34948` in toxic_zones/dashboard; orange in overview + steal GIFs) | overview, steal GIFs, heat-map overlays |
| Interceptor (truth) | blue (`#2a78d6`) | same |
| Truth vs track | truth **solid**, track **dashed** (forced/manual track dotted) | maps, timelines |
| Best CPA | gold star; other CPAs < 75 m plain stars | overview |
| Stolen track | green diamond + dotted trail | steal GIFs |
| Bias figures | RAW = orange, corrected/de-rotated = green (two-panel before/after) | `aoa_bias_correct.py`, `maps_fig` |
| Track colours | one legend entry per track, `corr_lib.TRACK_PALETTE` | angle_stack, maps |
| Heat map | **red `#e11d2e` → orange → yellow → cyan → blue `#1f6fd0`** @ 0.96 opacity, bottom 15 % clamped red; **no green-family scales** (they blend into desert terrain) | `_rr_heat_fig` |
| Error bands | ±1σ shaded, ±3σ dotted edges | `err_window_fig`, `angle_stack` |
| Text | never in a series colour; bold axes/ticks, darker grid (`report_figs.div()` funnel) | all |
| Caution | `live_correlator.py` uses the OPPOSITE role colours (target truth blue / interceptor red) — do not copy its palette into reports | — |

Live-dashboard carry-overs that also apply to report time-series: label every vertical line ("CPA 59 m · hh:mm:ss", "→ #177 · hh:mm:ss"); nothing may cover the last ~15 % of a time axis; colour = role, identity within a role = lightness step + dash + id pill; status = colour + glyph + word.

---

## 6. Numbers & provenance

1. **RAW errors by default** — never remove radar biases from the data; az bias is ANNOTATED (and displayed only when |az| > 1°). A de-rotated number may appear only beside the RAW one ("68 m raw → 27 m after removing −1.45°"). Only the truth-altitude datum defect is ever corrected, and after the feet fix that is 0 (`DU = 0`).
2. **Label every run configuration** — every result block starts with exactly what was run: day, run id + friendly name, target/interceptor ids, window (PDT), data source (mongo run / archiver dump / quickdump / from_cache / ulog), gates (fresh s, median m, moving m/s), bias mode (RAW / auto / fixed deg), geoid N; for sims also profile, noise model, swept variable, seeker settings, seed count. The user runs variants back-to-back and loses track otherwise.
3. Times in **PDT** with the date; epoch/UTC in machine files. Antenna origin and geoid N stated once per report.
4. Every CPA / pass carries its **source** (truth-truth / ulog / vs-track / radar-only) and `*` when live-only or sub-prominence.
5. Coverage always with its definition (fresh ≤ 1.5 s, confirmed, target-side, moving > 2–2.5 m/s, airborne).
6. Biases pass the thresholded rule (|median| ≥ 0.5σ, above floor, same sign both flights) before being called a bias; quote robust median ± σ, not RMSE.
7. Live quicklook numbers are superseded by offline recompute; keep the live value in a note.

---

## 7. Anti-slop rules (user-stated)

- **No invented figures.** Tracking days use the 8/26 figure set, engagements the 8/27 set — EXACT, no re-created or "custom" variants. The user rejected a dark overlay and three-per-day custom figures as slop.
- **"Just take the plot I told you."** When the user names a figure (e.g. the 8/26 F1 · Lap 2 `maps_fig` pane), embed that pane — do not redraw it.
- **"Just show only the turn with one figure."** One figure per point; no galleries.
- **Remove panels the user didn't ask for** (the target-track-error dot-strip was removed from story figures; swim-lanes stay). Don't add gates, panels, or sections speculatively.
- **No pre-cached data when it can be computed live** ("why would we pre-cache data") — FOV events, az-bias bars, turn statistics, heat map are computed from the dumps at build time; only expensive renders (FOV mp4) are reused if present.
- **No editorial adjectives** in titles, tab labels, captions, cards.
- **No "chaos-spa graded" / tool-name branding** in user-facing text.
- **Don't reinvent correlation** when the official stack exists (mongo path = chaotic DataLoader → `prepare_grading_inputs` → `grade_correlated_tracks`; corr_lib geometry only as an archive fallback, labelled).
- Keep one code path per figure type (`condition()` inside every eng_plots figure; `err_window_fig` common to engagement and tracking; `build_overview_section` / `build_fov_section` common to every engagement tab).
- Narratives in config, not code; code stays mission-agnostic.
- Verify claims against data before writing them (successor track 501 not 391; 8/28 turn-ratio inversion; the "LOS" heat-map narrative was wrong).

---

## 8. Pre-publish checklist

1. Units: block-106 altitude converted ft→m HAE with the stated geoid N; vertical speed ft/min→m/s; wire `speed` (knots) unused; `_guard_alt_units` silent.
2. Antenna origin read from THIS run's meta/track, not hardcoded; NED→ENU mapping E=x[1], N=x[0], U=−x[2].
3. Truth cleaned: stale repeats stripped, frozen runs dropped, teleports removed, duplicate ids merged, airborne-gated; no interpolation across > 2.5–3 s; roles (target/interceptor ids) verified.
4. Tracks: fresh/coast from `last_update_t`; death-coast truncated; track U/vU NOT used for altitude claims on 2026.3.x; long mid-leg deaths checked for the 200 m/s cut; tentative excluded from graded stats.
5. Passes: < 75 m, realistic gate (≥ 3 m/s both, closing ≥ 8 m/s), refined ±10 s with `E.cpa`; pad/taxi/frozen artifacts excluded or starred; source tagged.
6. Steals per the formal rule; corruption vs steal wording correct; everything called "track steal".
7. Titles/tabs plain; "Flight N · Lap N"; "run" only for radar runs; no adjectives; no tool branding.
8. Az bias annotated not removed; shown only if |az| > 1°; RAW numbers primary; biases pass the threshold rule.
9. Figures: no MRU marker; gaps annotated; CPA window t−10..+5; 1× animations; log y on full timelines; distance caps on CPA windows; heat map red→blue percentile with 15 % clamp; correct role colours.
10. Every example/caption has RUN ID + PDT timestamp + track number; every result block starts with its run-configuration label.
11. Rollup: labelled line-per-metric cards; Overall Rollup = days table + one paragraph; Radar Rollup §1–§5 in order; standalone HTML exports regenerated.
12. Nothing pre-cached that could be computed live; no figures or panels beyond the canonical sets; config (not code) holds the narratives.
13. Numbers re-derived offline supersede live quicklook values; disagreements noted with `*`.
14. Served copy checked in a browser (headless Firefox if needed — no kaleido); embedded satellite underlays present (grep `/jpeg`).
