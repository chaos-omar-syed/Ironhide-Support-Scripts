---
name: seawall-post-analysis
description: Post-analysis of an MRU radar test day or campaign against MAVLink drone truth — dump the run, build the flight manifest (flights, laps, passes, steals), condition the feeds, run the analyses (az/el bias, coverage, fragmentation, drop causes, CPA, track steals, heat map), assemble the day reports and rollup with the canonical Seawall pipeline, and write the narrative. Use for "post-process this test day", "build the campaign report", "what happened on flight N", "did we get a track steal", "quick look at run X".
---

# Seawall post-analysis

You are the analyst. The scripts in `scripts/` do the deterministic work (dumping,
segmentation, metrics, figures, report assembly). You do the judgment: which feed
is the target, which passes are real, whether a track was stolen or merely dragged,
what the day's key message is, and what to leave out. Never invent a figure the
pipeline did not produce, and never publish a number you have not seen the script
print.

Everything runs in the `sensorenv` environment:
`micromamba run -n sensorenv python scripts/<tool>.py …`. Paths in this file are
relative to the skill folder. Read `references/data-gotchas.md` before touching
data and `references/report-rules.md` before writing anything a human will read.

## Quick look in one command (the default path)

For "what happened today", do not hand-author anything. Dump, then:

```
cd scripts
python quicklook_report.py --type auto --day 2026-08-28 --dumps <dump1> <dump2> --out <report_root>
# straight from a live unit (dump + manifest + build, ~30 s for a 13-minute window):
python quicklook_report.py --type auto --mongo "mru=43,run=6aecec5e,jobs=21627-23147,label=ep1" --target 'mavlink_1_2*' --interceptor none --out <report_root>
```
`--tz` sets the local zone (default America/Los_Angeles, DST-aware); target and
interceptor patterns default to `mav*_1_*` / `mav*_2_*` and must be given when a
site names its drones differently.

It builds the manifest if none is given, fills `templates/tracking_day.json` or
`templates/engagement_day.json` from it (windows, laps, best realistic pass per
flight, the track riding the target at that pass, antenna, run id, patterns),
writes the filled config next to the output, and runs the standard build.
Measured: 4 s for a two-flight tracking day, 37 s for a four-engagement day
(3-D GIFs dominate), FOV video extra when ffmpeg and ulogs exist. Every default
report therefore starts from the same two building blocks: the 8/26 tracking
format and the 8/27 engagement format. Edit the written JSON (narratives, a
different pass, a track override) and re-run with `--build-only <json>`.
Details: `references/quicklook.md`.

The full workflow below is for campaigns (several days, radar rollup) and for
days where the automatic choices need your judgment.

## Workflow

Work through the phases in order. Each phase names its script, its inputs, what
you check before moving on, and the judgment calls that are yours.

### 0. Intake — pin the campaign

Establish, and write down in a scratch `campaign_notes.md`:

- MRU number → mongo host `10.1NN.28.205` (or an archive directory if the unit is gone).
- Run(s): hex prefix or friendly name; `scripts/dump_run_window.py --mru N --list-runs`.
- Day type per day: **tracking** (drone flies a pattern, radar tracks it) or
  **engagement** (interceptor vs target, close passes). This picks the report format.
- Which MAVLink ids are target and interceptor. Do not assume; list the ids the
  dump produces and confirm from geometry (the interceptor launches later and
  closes on the target). Duplicate ids (`mav14551_2_0/_1/_34`) are one aircraft.
- Site geoid undulation (`--geoid-n`, −31.4 m at the Seawall SoCal site), timezone.
- Onboard logs (ulog), IR video, and the ulog↔UTC clock offset, if an engagement
  day is to get the seeker-FOV section.

If the campaign already has dumps and `flights.json` manifests, start at phase 3.

### 1. Dump — `scripts/dump_run_window.py`

One dump per flight window or job range, into the archive layout every offline
tool reads (`<root>/<day>/<run8>_<label>/{mavlink,tracks,obs,jobs}/…, meta.json`).
Handles both block-143 layouts and queries only the indexed time field.

```
python scripts/dump_run_window.py --mru 91 --run 9b22a989 --t0 "2026-08-26 08:30" --t1 "2026-08-26 09:25" --label flights12 --root <archive>
python scripts/dump_run_window.py --mru 43 --run 6aecec5e --jobs 21627-23147 --label ep1 --root <archive>
```

Check: `meta.json` antenna origin is per deployment (read it, never reuse
yesterday's); the MAVLink files you expect exist and are not all stationary;
`tracks/` is non-empty; the 143 layout was detected.

### 2. Manifest — `scripts/build_flight_manifest.py`

Turns a day's dumps into `flights.json` + a `DAY_SUMMARY.md` skeleton: flight
windows (airborne gaps > 5 min), laps for tracking days, passes for engagement
days (1 Hz 3-D separation, 80 m prominence, no interpolation across gaps),
track sides, coverage, riders/handovers, drop hints, and track steals by the
formal rule. See `references/manifest-schema.md`.

```
python scripts/build_flight_manifest.py --day 2026-08-28 --dumps <d1> <d2> --target 'mav*_1_*' --interceptor 'mav*_2_*' --out <archive>/2026-08-28
python scripts/build_flight_manifest.py --day 2026-08-26 --dumps <d> --target 'mav14550_1_1' --interceptor none --type tracking --out <archive>/2026-08-26
```
Options: `--type auto|tracking|engagement`, `--windows HH:MM:SS-HH:MM:SS,…` to
pin flight windows, `--trim-speed` for speed-based window trimming, `--tz`.
Validated against the 8/26 and 8/28 references (windows, passes, steals, az
bias, riders all reproduced) and against the synthetic answer key.

Your judgment here, recorded in `DAY_SUMMARY.md` notes:
- **Passes**: keep only realistic ones (both craft ≥ 3 m/s, closing ≥ 8 m/s);
  a "pass" against a parked interceptor, a formation-flight dip, or a frozen
  sample is not a pass. Refine CPA time ±10 s; 1 Hz truth under-reads a 40 m/s
  crossing by about 2×.
- **Steals vs drag vs corruption vs runaway**: the manifest classifies them
  (`steal_events`, `drag_and_die`, `corruption_events`); read all three plus
  `feed_freezes`, `clutter_tracks`, `pinned_altitude_tracks`,
  `speed_filter_kills` before writing a word. Steal = identity flips sides
  after a pass; drag-and-die = pulled off and dies without flipping;
  corruption = identity holds, state runs away at the pass and returns; a
  track that diverges after the pass and never returns is a post-pass runaway
  (8/27 trk 633) and is judged from the error window. Name every event with
  run id, track id, and local time. Everything that flips is a "track steal"
  — never "capture".
- **Passes flagged FROZEN FEED** were computed on a dead-reckoned or frozen
  truth sample; quote them as unreliable and prefer onboard logs.
- **Frozen or teleporting truth**: if the target feed froze during a pass,
  recover the CPA from onboard logs or from head-on closing track pairs
  (`references/data-gotchas.md` §D) and mark the pass source accordingly.

### 3. Analyses

Run what the day type calls for; every number in the report must trace to one of these.

| Question | Tool | Notes |
|---|---|---|
| Azimuth / elevation bias, before/after correction | `scripts/aoa_bias_correct.py` | Live: `--mru --run --jobs` (chaotic correlation). Archive: `--archive <dump> --t0 --t1 --target` (any quickdump-layout dump, dashboard saves included). Read the per-track pages (`aoa_bias_page_trk<ID>.png`) before quoting the pooled number: if one track's own median disagrees, refit with `--tracks` on the good one(s). Act only when range-flat, consistent across tracks, and above the 1° bar. |
| Coverage, fragmentation, riders, handovers, gaps | manifest `tracking` block | Per flight. Coverage is fresh target-side states while the truth moves. |
| Where tracks go bad (heat map, coast in turns vs straights, toxic cells) | `scripts/toxic_zones.py` | `--day "label:<dump>"` per day; 30 m cells, percentile colouring; exclude days with zero target-side tracks. |
| Why tracks dropped | manifest `issues` hints | Classify each gap: turning, pad/climb, interceptor within 200 m, Doppler notch, straight cruise. Check track speed at the last published sample for the Delta 200 m/s speed-filter kill. |
| Passes, CPA, closing geometry, 3-D replay | `engagement_tab` via the pipeline | CPA window is t−10…t+5 s, 1× real time. Late-born track → ±120 s fallback. |
| Seeker FOV + IR | `engagement_tab.build_fov_section` via config | Needs ulog path/glob, per-log zoff, IR mkv, camera anchor; ffmpeg required for the player. Skip cleanly when absent. |

### 4. Campaign config — `templates/campaign_template.json`

Single days: `templates/tracking_day.json` / `templates/engagement_day.json`
(what the quick look fills). Campaigns: the two-day `campaign_template.json`,
or the real worked example `templates/seawall_week_0824.json`.

Copy the template, fill one `days[]` entry per day (tracking entries point at
the dump + flights; engagement entries carry the pass windows, ulog/IR data),
and the `radar_rollup` block (which days are scoreable, steal/corruption views,
the az-bias example, the turn zoom). Every key is documented in
`references/campaign-config.md`. Narrative fields stay empty until phase 6.

### 5. Build — `scripts/postprocess_report.py`

```
cd scripts && python postprocess_report.py <campaign.json>
```

Produces `<out_root>/<day>/report.html` per day, `index.html` (Overall Rollup +
Radar Rollup tabs), and the standalone single-file exports. Operating details,
runtimes, and failure messages are in `references/pipeline-howto.md`. Serve
`<out_root>` with a plain HTTP server; do not build under a background job
without `cd`-ing into `scripts/` first.

Check every figure once. A plot that says nothing, or contradicts the manifest,
is a bug to fix, not a caption to write around.

### 6. Narrative and publish

Write, in this order: the one-arc campaign paragraph, each day's key message
and labelled numbers line, the radar-rollup section narratives (az bias, turns,
corruption at close passes, steals, heat map), then each engagement's pass
notes. Rules in `references/report-rules.md` are binding: plain titles, run id
+ time + track on every example, CPA labels only under 75 m, az bias shown only
when |az| > 1°, no antenna marker on top-downs, RAW errors with the bias
annotated not removed.

Rebuild, re-open, and run the pre-publish checklist at the end of
`references/report-rules.md`.

## Self-test (run after any change to `scripts/`)

1. **Recreate the Seawall week.** Copy the reference campaign
   `templates/seawall_week_0824.json`, set `out_root` to a scratch dir, and build.
   The four day pages and the Radar Rollup must reproduce the reference numbers
   (az bias −2.20 / −1.45 / −0.19 / −0.45°, steal gifs for 177 and 1551,
   corruption gif for 633). Tracking-day pages are byte-identical to the served
   originals; engagement pages differ only by the FOV video sections when ffmpeg
   is absent.
2. **Manifest against the reference days.** `build_flight_manifest.py` on
   `2026-08-28` (three quickdumps) must find 4 flights, best passes 53/37/21/32 m,
   steals 177 (F1 pass 3) and 1551 (F4 pass 6), coverage ≈ 99.8/84.3/92.3/90.9 %;
   on `2026-08-26/9b22a989` two flights with az bias −1.45/−1.43°.
3. **Synthetic edge cases.** `scripts/make_synthetic_day.py --out <dir>` fabricates
   a two-flight day with an answer key (`ANSWER_KEY.json`): duplicate interceptor
   ids, a frozen target feed around a pass, a GPS teleport, a parked-interceptor
   dip, real passes at 60/25/45 m, a steal in each direction, a corruption, a
   drag-and-die, a speed-filter kill, a pinned-altitude track, clutter and a
   far ADS-B-like track. Run the manifest builder on it and diff against the key;
   every injected event must be found and every decoy rejected.

## What the scripts cannot decide (your job)

- Which drone is which when ids are ambiguous, and whether two ids are one aircraft.
- Whether a dip in separation is a real pass.
- Steal vs drag vs corruption, and which one example carries the story.
- Whether a red heat-map cell is geometry, a turn, a crossing, or a feed defect.
- Whether a day is scoreable at all (8/25: zero target-side tracks — exclude).
- What to cut. The user rejected panels they did not ask for; when in doubt, fewer figures.

## Files

- `scripts/dump_run_window.py` — mongo run window / job range → archive layout (both 143 layouts, indexed queries).
- `scripts/build_flight_manifest.py` — dumps → `flights.json` + `DAY_SUMMARY.md` skeleton.
- `scripts/aoa_bias_correct.py` — az/el bias fit and correction (chaotic correlation live; geometry fallback offline).
- `scripts/toxic_zones.py` — coverage / coasting heat map over satellite terrain.
- `scripts/postprocess_report.py`, `tracking_tab.py`, `engagement_tab.py`, `eng_plots.py`, `report_figs.py`, `corr_lib.py`, `live_correlator.py` (satellite tiles + live dashboard), `spa_errors.py` (chaos-spa grading adapter) — the canonical report pipeline and its libraries.
- `scripts/post_correlate.py` — one-window truth-vs-track report for any MRU (mongo or npz); the quick answer when a full day report is overkill.
- `scripts/mission_report_builder.py` — single-mission JSON-driven report (same day modules, no rollup).
- `scripts/make_synthetic_day.py` — fabricated edge-case day with an answer key, for testing.
- `templates/seawall_week_0824.json` — the real Seawall campaign config, the worked example.
- `templates/campaign_template.json` — commented two-day campaign config for a new campaign.
- `templates/tracking_day.json`, `templates/engagement_day.json` — the two common single-day templates every default report starts from.
- `scripts/quicklook_report.py` — one command: manifest → filled template → standard build, with timings.
- `references/data-gotchas.md` — conditioning and interpretation catalogue (read first).
- `references/report-rules.md` — binding presentation rules and pre-publish checklist.
- `references/campaign-config.md` — every campaign JSON key.
- `references/pipeline-howto.md` — operating procedure, failure messages, re-run tips.
- `references/manifest-schema.md` — `flights.json` schema and the segmentation method.
- `references/script-inventory.md` — what every legacy script did and which survived into this skill.
- `references/quicklook.md` — the one-command path: auto-filled fields, what the analyst edits, re-run, timings.
