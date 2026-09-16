# aoa_bias_correct.py — AoA bias from MAVLink truth

Measures the azimuth and elevation bias of an MRU's tracks against the drone's
own GPS (MAVLink truth), fits a robust correction, and shows the tracks before
and after that correction. Input is an MRU number, a run, and a job range.

```
MRU91 azimuth bias −1.45°  →  apply a +1.45° yaw correction
median horizontal miss 68 m → 25 m
```

Single self-contained file. The only dependency is the `sensorenv` environment; no
environment variables to set (the script pins JAX to CPU itself before importing chaotic).

## Quick start

```bash
# live unit: MRU number, run (hex prefix / friendly name / latest), job ids
micromamba run -n sensorenv python aoa_bias_correct.py \
    --mru 43 --run 6aecec5e --jobs 21627-23147

# restrict to one MAVLink target when several drones were up
micromamba run -n sensorenv python aoa_bias_correct.py \
    --mru 91 --run Turquoise_Emu --jobs 2500-2900 --target 14550

# what runs are on a unit
micromamba run -n sensorenv python aoa_bias_correct.py --mru 91 --list-runs

# offline: a seawall_archiver CSV dump (no job ids in the archive -> time window)
micromamba run -n sensorenv python aoa_bias_correct.py \
    --archive /home/omar.syed/Test_Environment/Seawall_Ironhide_Testing/Seawall_Week_of_8-24/seawall_0824_data/2026-08-26/9b22a989 \
    --t0 "2026-08-26 08:35" --t1 "2026-08-26 08:53" --target 14550
```

Outputs land in `aoa_bias_<label>/` (or `--out`):

| File | What it is |
|---|---|
| `aoa_bias_page_all.png`, `aoa_bias_page_trk<ID>.png` | One page per track (plus an all-tracks page): top-down **before / after** on top, azimuth error vs time **before / after** below, thick lines, no radar marker. Each page's title carries that track's own median az error next to the pooled correction, so a badly associated track cannot hide in the pool. `--pages N` caps the per-track pages (longest first, default 12). These are the ones to paste in a report. |
| `aoa_bias.png` | Six-panel detail: both overlays, horizontal-error histogram, az and el error vs time with the fitted bias and 95% CI, az error vs range. |
| `aoa_bias.html` | Interactive (plotly) version of the overlays and error series, hover for time stamps. |
| `summary.json` | Every number: windows, per-job status, correlated pairs, fit statistics, applied rotation, suggested yaw correction, and which correlation engine produced it. |

The console prints the same summary:

```
=== AoA bias fit ===
  samples: 1678 fresh/moving/>300 m on 10 track(s)
  AZ bias  -1.46°   95% CI [-1.47, -1.44]   MAD-σ 0.32°   per-track spread 0.65°   slope vs range -0.12°/km
  EL bias  +0.47°   95% CI [+0.47, +0.48]   MAD-σ 0.21°   (≡ +21 m altitude)
  range bias +16.4 m
  horiz err median  raw 70 m  ->  corrected 28 m   (p90 119 -> 51)
  suggested yaw_offset correction: +1.46° (sign: see notes in summary.json)
```

## How it works

### 1. Resolve the source

* `--mru N` becomes the mx-node mongo host `10.1NN.28.205` (override with `--host`).
* `--run` accepts a hex prefix of the run id (`6aecec5e`), the full `run_<hex>`
  collection name, the friendly run name (`Lavender_Bison`), or `latest`.
* `--jobs` accepts ranges and lists: `100-400`, `9-12,40,100-105`.

Job ids are also resolved to wall-clock windows from the block-136 JOB_EVENT
documents (indexed by `block_type + job_id`, so this is instant) for the
per-job status report in `summary.json`. Jobs that did not reach
`JOB_COMPLETE_SP` are kept but flagged.

### 2. Correlate tracks to truth — chaotic's own stack

For a mongo source the script does **not** implement its own correlation. It
drives the official chaotic evaluation pipeline end to end:

```
DataLoader(run_uuid, mongo_host)
    .load_tracks_dataframe(j0, j1)          # block 143
    .load_obs_dataframe(j0, j1)             # block 103
    .load_air_traffic_dataframe(j0, j1)     # block 106, MAVLINK rows kept
→ evaluate_tracks_chaotic.prepare_grading_inputs(
      load_all_truth=True, recorrelate_tracks=True, target_ids=<MAVLink ids>)
      # obs-history plurality vote (recorrelate_tracks_via_obs_history) when the
      # obs carry truth_target; otherwise the tracker's runtime truth_match
→ track_eval_utils.grade_correlated_tracks(track_df, obs_df)
      # corr_meas_df: measurement-space az / el / range error per rx link
```

The fit and plots run on those graded samples. `summary.json` and the figure
title state which correlation was used.

Two chaotic behaviours to know about:

* `build_truth_df` upper-cases target ids, so the script normalises the
  requested ids the same way (otherwise `prepare_grading_inputs` raises).
* `is_valid_truth_target` drops zero-speed truth, so a **hovering drone never
  correlates**. If the valid set is empty the script reloads the MAVLink rows
  unfiltered and says so.

**Fallback.** The seawall_archiver CSV dump has no contributors, obs truth
targets, or truth_match, so chaotic cannot run on it. `--archive` (or
`--engine geometry`) uses the Seawall 8/24 moving-truth position gate instead:
a track is paired with the truth stream it stays within `--gate` metres of
(150 m) while the truth is moving faster than `--min-speed` (2 m/s). Output is
labelled FALLBACK.

### 3. Fit the bias robustly

Only samples that can carry angle information are used:

* measurement-updated states (no coasted predictions),
* truth moving (> `--min-speed`),
* mono range > `--rmin` (300 m; angle error at short range is dominated by position noise),
* coast excursions beyond `--max-horiz` (350 m) masked, and each track cut at its final measurement.

Azimuth and elevation bias are the **median** error over those samples, with:

* MAD sigma (robust spread),
* bootstrap 95% confidence interval on the median,
* per-track spread of the medians (a real array rotation shows the same bias on every track),
* slope of az error vs range. A true angular bias is flat with range; a fixed
  lateral offset falls off as 1/R. A large slope means the number is not a pure yaw error.

Elevation bias and a truth-altitude datum error are indistinguishable from a
single target, so the el bias is also reported as the equivalent altitude
offset in metres. It is not applied when the tracker pins altitude (track-U
std < 1 m) or with `--no-el`.

### 4. Apply and plot

Every track state is rotated by `−az_bias` about the radar (and `−el_bias` in
the vertical plane, slant range preserved). Errors are recomputed and the
before/after overlays, error series and histograms are drawn.

### Sign convention

```
az_bias = median(track_bearing − truth_bearing)      + = track reads clockwise of truth
yaw_offset correction = −az_bias
```

A physical array yaw error of +b (array clockwise of the model) makes tracks
read −b. Seawall 8/26 on MRU91: flight bias −1.46° / −1.44° against a
cal-geometry fit of +2.26° clockwise, so the yaw needs +1.45°. Verify the sign
against the unit's `yaw_offset_rad` convention before editing config.

## All options

```
source (mongo)        --mru N | --host H  --port 27017  --db sensor_store
                      --run <hex|run_<hex>|name|latest>  --jobs '100-400,512'
                      --pad 15 (s around each job window)  --list-runs
source (archive)      --archive DIR  --t0 "YYYY-MM-DD HH:MM"  --t1 ...  --tz America/Los_Angeles
correlation           --engine auto|chaotic|geometry   --use-tentative   --rx-node N
                      --target SUBSTR   --tracks 1,2,3   --gate 150   --min-dur 10   --keep-adsb
bias fit              --rmin 300   --min-speed 2   --max-horiz 350   --no-el
                      --alt-units feet|meters   --geoid-n N
output                --label TEXT   --out DIR   --pages 12 (per-track pages)
```

## Examples

`examples/` holds three complete outputs:

| Example | Engine | Az bias | Horizontal median raw → corrected |
|---|---|---|---|
| `MRU91_9b22a989_flight1` | geometry fallback (CSV archive) | −1.46° | 70 m → 28 m |
| `MRU91_9b22a989_flight2` | geometry fallback (CSV archive) | −1.44° | 66 m → 22 m |
| `MRU43_6aecec5e_jobs21627-23147` | chaotic obs-history plurality | −4.44° | 202 m → 83 m |

The MRU91 result is range-flat and agrees across both flights: a real array
rotation. The MRU43 case is an uncontrolled flight whose az error slopes with
range (+1°/km) and carries a +60 m range bias, so it demonstrates the chaotic
engine path but is not a calibration result.

## Units and frames baked in

* MAVLink block 106 `altitude` is **feet MSL**, `vertical_speed` is ft/min,
  `speed` is knots. Track block 143 is metres, WGS-84 HAE. The archive loader
  converts truth to HAE with a site geoid undulation (`--geoid-n`, default −31.4 m).
* ENU is about the rx antenna origin of the run (per deployment; taken from the
  obs sensor nodes or the track payload).
* The mongo `time_spec_float` field is unindexed; the fallback loaders query
  `time_spec.full_sec` instead so a busy run does not time out.
