# Quick look in one command — `scripts/quicklook_report.py`

Every default report starts from the same two building blocks: a **tracking day** is
the `tracking_tab` format (Summary + `Flight N · Lap N` tabs), an **engagement day** is
the `engagement_tab` format (one tab per flight at its best pass). The two day
templates in `templates/` are those blocks as commented, runnable campaign configs;
`quicklook_report.py` fills one of them from the dumps + `flights.json` and builds it
with the canonical `postprocess_report.py`, printing wall-clock timing per phase.

```
cd skills/seawall-post-analysis/scripts
micromamba run -n sensorenv python quicklook_report.py \
    --type tracking|engagement|auto --day 2026-08-28 --dumps DIR [DIR ...] \
    [--manifest flights.json] [--target GLOB] [--interceptor GLOB] [--ant lat,lon,hae] \
    [--geoid-n -31.4] [--out DIR] [--title ...] [--flights 1,2] [--pass 1:3,4:5] \
    [--radar-rollup] [--server-base URL] [--config-out day.json] [--no-build]

micromamba run -n sensorenv python quicklook_report.py --build-only <out>/<day>_quicklook.json
```

Run it from `scripts/` (the modules import each other by sibling path) under the
`sensorenv` interpreter. Outputs: `<out>/<day>/report.html`, `<out>/Rollup.html`
(+ `index.html`, `Rollup_standalone.html`), and the filled config
`<out>/<day>_quicklook.json` (override with `--config-out`).

## The two templates

| file | day type | tabs | needs |
|---|---|---|---|
| `templates/tracking_day.json` | `tracking` via `prep` (mission inputs synthesised from ONE dump dir) | Summary + one per `flight_keys` entry (`F<flight>R<lap>` → "Flight N · Lap N") | exactly two flight windows (`fast2_F1/F2`); optional `laps_pdt` |
| `templates/engagement_day.json` | `engagement` | one per `engagements[]` entry (`tab_label`) | per entry: dump, target/interceptor patterns, whole-flight `window_pdt`, `cpa_seed_pdt`, `track_id` |

Both carry every field of the real 8/24 config (`templates/seawall_week_0824.json`) for a
single day, with `_comment*` keys explaining each field and whether it is **AUTO**
(fillable from `flights.json` / `meta.json`) or **NARRATIVE** (analyst). `_`-prefixed keys
are ignored by the code; the Radar Rollup block ships disabled as
`_radar_rollup_disabled` (rename to `radar_rollup`, or pass `--radar-rollup`) and the
FOV block as `_fov_example` (rename to `fov` when the ulog + IR + zoff exist).

Every `<PLACEHOLDER>` outside `_` keys must be replaced before a build;
`--build-only` refuses a config that still has one and lists them.

## What gets auto-filled

| field | source |
|---|---|
| `days[].id`, `date`, `init.day`, titles (`M/D — Tracking (2 flights × 2 laps)`, `M/D — Engagements (N flights)`) | `--day` |
| run id in `sub` lines | `meta.json` `run_id8` / `run` (`run_<hex>` stripped to 8 hex), else `flights.json runs[0].run` |
| `ant`, `init.ant`, `prep.ant_hae_m` | `meta.json` `antenna` **or** `antenna_origin_lat_lon_haeM` (both spellings), else `flights.json runs[0].antenna`, else `--ant` |
| `init.geoid_n` | `meta.json geoid_n_m`, else `--geoid-n` (default −31.4) |
| target pattern | `--target` (glob resolved against the files present), else `flights.json tracking.target` + `.csv` |
| interceptor pattern | `--interceptor`, else derived from `mavlink/` names matching `mav*_2_*` — alias-broken `mavlink_*` files excluded, several compids collapse to `mav14551_2_*.csv` |
| tracking `prep.flights_pdt` | the two flights' `t0_pdt`/`t1_pdt` (ISO or `HH:MM:SS`). One flight → split at the middle lap boundary (or midpoint); > 2 flights → first two (`--flights a,b` to choose) |
| tracking `prep.laps_pdt` + `flight_keys` | `segments[type="loop"]` per flight; a flight without loops = one lap |
| engagement `window_pdt` | the flight's `t0_pdt`/`t1_pdt` (whole flight — drives the Overview figure) |
| engagement `cpa_seed_pdt`, `name`, `gif` | the flight's **best candidate pass**: smallest `miss_m` among passes with the interceptor airborne and both craft ≥ 3 m/s (< 600 m; old manifests: no grounded/parked/not-reproduced note). Ties → the pass whose riding track has the smaller median offset, then the later pass. `--pass F:N` forces a pass |
| engagement `track_id` | the `target_track_spans` (or `rider_spans_pdt`) entry whose span contains the pass, smallest "med … m off target" wins; else a scan of `tracks/` for the track within 150 m of the target truth in ±8 s |
| engagement `dump` | the `--dumps` dir holding `tracks/track_<id>.csv` and covering the pass time |
| `numbers_html` (rollup card) | manifest facts as labelled lines: tracking = az bias / coverage / median error raw → az-corrected; engagement = best pass / passes < 75 m / track steals (only when the manifest has `steal_events`). **Verify before publishing** |
| `summary_html` (engagement) | the factual default `Closest approach {cpa_truth} m truth-truth; {cpa_track} m to radar track {track_id}` |

`--type auto` = engagement if any flight has a candidate pass, else tracking.
Without `--manifest` the script runs `build_flight_manifest.py` on the dumps
(`<out>/manifest/flights.json`, ~3 s for the 8/28 quickdumps) — the archive is never written.

Why the manifest's `realistic` flag is **not** used as the gate: its closing-speed term
is estimated over 3 s of 1 Hz separation and under-reads at the flattening minimum of
an overtake — on 8/28 the 53/37/21/32 m passes read 3–8 m/s closing and would all be
dropped, while the served report features exactly those. The interceptor-grounded and
both-craft-moving parts are kept; `engagement_tab` refines the CPA from the real samples.

## What stays empty (the analyst's part)

`rollup_html`, `notes_html` (tracking Summary key findings — an explicit "not yet written"
placeholder renders; prep days never fall back to the 8/26 text), `key_message_html`,
`overview_html`, per-engagement `post_html`, and the key message you prepend to
`summary_html` (only `{cpa_truth}` `{cpa_track}` `{track_id}` are substituted; any other
`{brace}` raises). Also yours: `title`/`sub`, `server_base` + `out_root` under the :8899
root when sharing, `fov` blocks, and the Radar Rollup narratives/`steal_views` if enabled.
Rules for all of it: `report-rules.md`.

## Re-running only the build after editing the JSON

```
micromamba run -n sensorenv python quicklook_report.py --build-only <out>/<day>_quicklook.json
```
(= `postprocess_report.py <cfg>` plus the placeholder check and per-phase timings.)
Nothing is cached on these paths, so a re-run costs the same as the first build; the 3-D
GIFs are rewritten each time. Steal/corruption GIFs and FOV mp4s (if you enable them)
are reused when present — delete the file to re-render.

## Measured timings (this box, real archives, 2026-09-15)

| build | command | wall clock |
|---|---|---|
| tracking 8/26 (`9b22a989`, 1990 track csvs, 2 flights × 2 laps) | `--type tracking --manifest .../2026-08-26/flights.json --target mav14550_1_1.csv` | **4.4 s** total: prep_from_dump 3.0 s, init_mission 0.1 s, Summary 0.1 s, 4 lap tabs 0.2 s each, page + rollup + standalone < 0.1 s |
| engagement 8/28 (3 quickdumps, 4 flights) | `--type engagement --manifest .../2026-08-28/flights.json` | **37 s** total: 4 engagement tabs 8.8–9.6 s each (the 135-frame matplotlib 3-D GIF dominates), page/rollup < 0.1 s |
| manifest-less 8/28 (`build_flight_manifest.py` first) | `--type auto` (no `--manifest`) | + 2.6 s |

Both well under the 2-minute target. The only slow steps are per-engagement 3-D GIFs
(~9 s each, no cache) and — if enabled — `--radar-rollup` (~10–40 s: heat map + Esri
tiles) and FOV renders (~1.5–2 min each on the first build, needs ffmpeg).

## Validation against the served 8/24 report

- **8/26 tracking**: same tab set (Summary, Flight 1 · Lap 1, Flight 1 · Lap 2,
  Flight 2 · Lap 1, Flight 2 · Lap 2) and same pane set per lap tab (top-down RAW/rotated,
  az/el error stack, 3-D position + velocity error, measurement rate, seeker quad + 12° FOV
  line, timeline: 5 plotly figures + 1 table). Differences, all expected from going through
  `prep_from_dump` instead of the hand-prepared 8/26 mdir: generic Summary header
  (`Mission rollup — 2026-08-26`, flights listed from the data), lap 2 of each flight
  runs to landing (the mdir excluded a landing phase), inbound leg = second half of each
  lap, global az bias −1.48° vs −1.45°, key findings = placeholder.
- **8/28 engagement**: 4 tabs `Flight 1..4`, names `8/28 Flight N — pass P (HH:MM:SS PDT)`
  with passes 53/37/21/32 m → tracks 177/874/1291/1511 (identical to the served config),
  same pane set (Overview PNG, 3-D GIF, top-down, closing distance, closing speed, heading
  error, track metrics, state table) minus the `Seeker FOV player` heading — no `fov`
  block, so `build_fov_section` is never called; the console says so
  (`[fov] no 'fov' block in any engagement — Seeker FOV section skipped`).

## Pipeline hooks added for the templates (minimal)

- `postprocess_report.tracking_day_tabs`: optional `prep.laps_pdt`
  (`{"<flight>,<lap>": [date, hms, hms]}`) → `spec["runs"]`, so a prep day gets
  `Flight N · Lap N` tabs like the served 8/26 (previously prep days were one lap per flight).
- `tracking_tab.build_summary_tab`: on prep (`GENERIC`) days the header is built from the
  mission structure instead of the hard-coded 8/26 text, and an empty `notes_html`
  renders a "not yet written" placeholder instead of the 8/26 key findings.
- `tracking_tab.prep_from_dump`: quickdump mavlink csvs have no `speed_mps`; it is now
  derived as `hypot(vel_n, vel_e)` (the seeker/moving gate needs it) instead of zeros.
