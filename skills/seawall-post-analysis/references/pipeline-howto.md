# Seawall post-process report — operating procedure

The canonical pipeline is `track_correlation/postprocess_report.py` driving the two
report modules (`tracking_tab.py`, `engagement_tab.py` + `eng_plots.py`) and the
`toxic_zones.py` gates for the radar rollup. Copies of every script are in
`../scripts/`, but the copies still `sys.path.insert` the absolute
`/home/omar.syed/Test_Environment/track_correlation`, so run from there.

Environment for everything: `micromamba run -n sensorenv python …`
(python 3.13, plotly 6.8, pandas, numpy, matplotlib, PIL, cv2 4.11, pyulog, pymap3d,
pymongo, streamlit; `ffmpeg` is at `~/.local/share/mamba/envs/sensorenv/bin/ffmpeg`).
Alternatively `PY=/home/omar.syed/.local/share/mamba/envs/sensorenv/bin/python`.

Paths used below:

    TC=/home/omar.syed/Test_Environment/track_correlation
    WEEK=/home/omar.syed/Test_Environment/Seawall_Ironhide_Testing/Seawall_Week_of_<M-DD>   # archive root for the campaign
    SERVED=/home/omar.syed/Test_Environment/VP_TrackAnalysis/mru91_track2895              # http.server :8899 root

---

## 1. Dump a run window to the archive layout

### 1a. `quickdump.py` (thin CLI over `ih.archive.save_archive`) — the canonical dump

Confirmed: `quickdump.main()` only parses args, resolves the host, converts `HHMM` → epoch
with a fixed UTC−7, and calls `AR.save_archive(host, port, db, run, t0, t1, label=…, root=…, out_name=name, register=…, mru=…)`.
`save_archive` is the same function the Ironhide dashboard's "Save to archive" card uses, so the
CLI and the dashboard write byte-identical layouts. Today `ih` resolves to
`/home/omar.syed/Test_Environment/ironhide_dashboard/ih/` (identical to `ironhide_dashboard_served/ih/`).

Exact help (`micromamba run -n sensorenv python quickdump.py --help`):

```
usage: quickdump.py [-h] [--day DAY] [--host HOST] [--mru MRU] [--port PORT] [--db DB] [--run RUN] [--root ROOT] [--label LABEL]
                    [--register]
                    t0 t1 name

positional arguments:
  t0             window start HHMM (PDT)
  t1             window end HHMM (PDT)
  name           output folder name under <root>/<day>/ (e.g. e65bd4f9_flight1_quickdump)

options:
  --day DAY      PDT calendar day of the window (default today)
  --host HOST    mongo host (default 10.191.28.205, or from --mru)
  --mru MRU      MRU number -> host 10.1NN.28.205 (used when --host is not given)
  --port PORT
  --db DB
  --run RUN      run collection (run_<hex>)
  --root ROOT    archive root (default: ih.data.ARCHIVE_ROOT = current test week Seawall_Ironhide_Testing/Seawall_Week_of_9-14, or $IH_ARCHIVE_ROOT)
  --label LABEL  flight label for meta.json / flights.json (default: the folder name)
  --register     append the window to <day>/flights.json as a replayable flight
```

Command (one dump per flight window, or one per day if the day is short):

```bash
cd $TC
micromamba run -n sensorenv python quickdump.py 0930 1015 <RUN8>_flight1_quickdump \
    --mru <NN> --run run_<full hex> --day 2026-09-16 --root $WEEK --register
```

- `--run` is REQUIRED in practice (default is the 8/28 MRU91 run). List runs on a unit with
  `micromamba run -n sensorenv python post_correlate.py --host 10.1NN.28.205 --list-runs` or
  `aoa_bias_correct.py --mru NN --list-runs`.
- `--day` defaults to today; the window is `HHMM..HHMM` PDT (fixed −7 h) on that day.
- The first line of stderr `WARNING streamlit.runtime.caching…: No runtime found, using MemoryCacheStorageManager` is harmless (ih imports streamlit).
- Progress is a single stderr line `NN.N %  <block> · HH:MM:SS of HH:MM:SS`; success prints
  `QUICKDUMP <name>: N mavlink rows (<ids>), N tracks / N rows (NED n · ECEF n), N obs rows -> <dir> · flights.json flight N`.
- Wall time: one query per 120 s slice × three block types; a 45-min window on an ADS-B-heavy unit is a few minutes.
  Every query is windowed on the INDEXED `time_spec.full_sec` (never `time_spec_float`).

What lands where (`ih.archive` docstring, verified against `e65bd4f9_flight1_quickdump/`):

```
<root>/<YYYY-MM-DD>/<name>/
    mavlink/<target_id>.csv   t_epoch,time_pdt,lat,lon,alt_ft_wire,E_m,N_m,U_m_hae,vel_n_mps,vel_e_mps,vert_spd_wire_ftmin,validposition
    tracks/track_<tid>.csv    t_epoch,time_pdt,E_m,N_m,U_m,vE_mps,vN_mps,vU_mps,sigE_m,sigN_m,sigU_m,total_associations,track_state,last_update_t,
                              truth_match_id,truth_match_conf,contributors,sigvE_mps,sigvN_mps,sigvU_mps
    obs/obs.csv               t_epoch,time_pdt,az_rad,el_rad,rng_m,rr_mps,bi_rng_m,bi_rng_rate_mps,truth_target_id,truth_match_type
    meta.json                 run (8-hex), run_collection, label, what, window, window_pdt, antenna [lat,lon,hae], tx_lla, frame, mru, host,
                              port, db, saved_at(_pdt), roles{target,interceptor}, mavlink_rows, track_rows, tracks, obs_rows, layouts{ned,ecef},
                              n_adsb_docs, mavlink_ids, columns
+ <root>/<YYYY-MM-DD>/flights.json  (only with --register: a "saved window" entry, passes not computed)
```

- Truth E/N/U: `corr_lib.EnuFrame` about the run's antenna origin (read from any block-143 payload), altitude
  feet-MSL → m WGS-84 HAE via `corr_lib.mavlink_alt_hae_m` (GEOID_N −31.4, site constant).
- **Both block-143 layouts** are decoded by `ih.feed.track_rows`: 2026.3.x (`x_state` NED + `p_cov`) and 2026.9.x
  (`x_state_ecef` + `p_cov_ecef` + `latitude_rad/longitude_rad/altitude_m`). The CSV layout is the same either way;
  `meta.json.layouts` tells you which was seen. Tracks farther than 15 km from the antenna are dropped.
- NOTE the mavlink CSV has NO `speed_mps` column (the old `seawall_archiver.py` layout did). Consequences in §5.
- `validposition == 0` rows are kept in the file; every reader drops them.

### 1b. `seawall_archiver.py` — legacy whole-run dumper (not canonical)

`SEAWALL_OUT_ROOT=$WEEK PYTHONPATH=$TC micromamba run -n sensorenv python seawall_archiver.py` dumps EVERY run with
MAVLink activity since `RANGE_T0 = 2026-08-25` from `HOST = 10.191.28.205` into `<root>/<day>/<run8>/`
(mavlink/ + tracks/ + meta.json with `run_id8`, `friendly_name`, `antenna_origin_lat_lon_haeM`; the mavlink CSV
HAS `speed_mps`). It queries the unindexed `time_spec_float` (slow on live units), decodes only the NED layout,
retries forever, and has MRU91 constants baked in. Use it only if you need the archiver-layout dump (e.g. for the
seeker panels, §5 item on `speed_mps`), and edit `HOST`/`RANGE_T0`/`FALLBACK_ANT` first. Its output dirs are what
the 8/26 and 8/27 days of the 8/24 campaign used (`9b22a989/`, `214a1a56/`).

### 1c. Dashboard "Save to archive"

Same `save_archive`, run as a background job from the Ironhide dashboard; writes to `ih.data.ARCHIVE_ROOT`
(default `Seawall_Week_of_9-14`, env `IH_ARCHIVE_ROOT`) and registers the window in `flights.json`.

---

## 2. Build the day manifest — `flights.json`

`postprocess_report.py` does NOT read `flights.json`. The manifest is the analyst's (Claude's) source for
every number that goes into the campaign JSON: flight windows, pass times, target/interceptor track ids,
steal events, per-flight tracking stats and issues. A script `../scripts/build_flight_manifest.py` will exist
to produce it from a dump dir; its role is:

- read `<dump>/mavlink/*.csv` + `<dump>/tracks/*.csv` (+ `meta.json` for antenna/run), identify the target and
  interceptor feeds by pattern, freeze-clean and merge the interceptor compids;
- split the day into flights on target airborne gaps (8/28 method: gap > 5 min, airborne = U above ground + 5 m or speed > 2 m/s);
- per flight: `t0/t1` + `t0_pdt/t1_pdt`, `drone_ids`, `airborne_minutes`, `segments` (loop/transit/hover), `passes[]`
  (3-D separation local minima with `n, t_pdt, miss_m, source, note`), `tracking{target, n_tracks, coverage_pct, median_horiz_err_m,
  fragmentation_track_count, az_bias_deg, target_track_spans{tid: "hh:mm:ss-hh:mm:ss, med NN m off target"},
  interceptor_track_spans, steal_events[{track, after_pass, pass_t_pdt, post_pass_median_dist_to_*}]}`, `issues[]`;
- write `<day>/flights.json` with the 8/28 schema: `{"day", "runs": [{"run": <run8 or run_<hex>>, "flights": [...]}], "day_issues": [...], "method": {...}}`.
  (`ih.data.append_flight_entry` merges by `dir` into the same file, so `--register` entries and manifest entries coexist.)

Mapping manifest → campaign JSON:

| manifest | campaign key |
|---|---|
| `flights[].t0_pdt/t1_pdt` (clock part) | tracking `prep.flights_pdt[i] = [date, hms, hms]`; engagement `window_pdt` |
| `passes[].t_pdt` of the pass you feature | `engagements[].cpa_seed_pdt` (name/tab_label carry the pass number and miss) |
| `tracking.target_track_spans` (the id alive at the pass) | `engagements[].track_id`, `corruption_views[].track_id` |
| `tracking.steal_events[].track/pass_t_pdt` | `steal_views[]`, `steal_examples[]` |
| `tracking.az_bias_deg`, `coverage_pct`, `median_horiz_err_m` | `numbers_html`, `az_bias_cfg` (non-scoreable days), narrative |
| `meta.json.antenna` | `init.ant`, `prep.ant_hae_m`, `days[].ant`, `steal_views[].ant` |
| `issues[]`, `day_issues[]` | `notes_html`, `key_message_html`, `*_html` narratives |

Also read `ls <dump>/mavlink/` — the exact `target_id` file names (`mav14550_1_1.csv` vs `mav14550_1_2.csv`,
`mav14551_2_0/2_1/2_34.csv`) decide every `*_pattern`.

---

## 3. Author the campaign JSON

Start from `../templates/campaign_template.json` (fully commented; every narrative field present but empty).
Full key reference: `campaign-config.md`. Checklist before running:

1. `out_root` = `$SERVED/<campaign>_report`, `server_base` = `http://172.18.1.28:8899/<campaign>_report`.
2. Every day `id` is `YYYY-MM-DD`.
3. Tracking day: `prep` (never bare `mdir`), exactly two `flights_pdt`, `flight_keys` = `["F1R1","F2R1"]`,
   `init.ant` + `prep.ant_hae_m` from `meta.json`, `notes_html` NON-EMPTY.
4. Engagement day: `date`, `ant`, per-engagement `dump`, `target.pattern`, `interceptor_pattern`, `window_pdt`,
   `cpa_seed_pdt`, `track_id`, `gif` under `figs/`, `summary_html` uses only `{cpa_truth}`, `{cpa_track}`, `{track_id}`.
   `fov` only when the ulog + IR video + a measured `zoff` exist (else omit — the section is skipped cleanly).
5. `radar_rollup.days[].dirs` — every scoreable dump dir; the target must be a `mav14550*.csv` (code limit).
   Omit `az_bias_example` (legacy-only). `steal_views`/`corruption_views`/`turn_zoom` only for events you have ids and times for.
6. `python -c "import json; json.load(open('<cfg>'))"` — JSON validity (no trailing commas).

---

## 4. Run `postprocess_report.py`

```bash
cd $TC
micromamba run -n sensorenv python postprocess_report.py /path/to/<campaign>.json 2>&1 | tee /path/to/<campaign>_build.log
```

There is no `--help`; `python postprocess_report.py --help` just raises
`FileNotFoundError: [Errno 2] No such file or directory: '--help'` (it tries to `json.load` the argument).
No `--day`, `--only`, dry-run or cache flags exist. Imports are `engagement_tab as ET`, `tracking_tab as TT`
(+ lazy `toxic_zones`, `live_correlator`, matplotlib, PIL, cv2, pyulog) — all from `$TC`, so run from that directory
(the script also resolves `quickdump.py` via `HERE`).

Background run (agent shells reset cwd between calls — put the `cd` inside the command, use absolute paths):

```bash
nohup bash -c 'cd /home/omar.syed/Test_Environment/track_correlation && micromamba run -n sensorenv python postprocess_report.py /abs/<campaign>.json' \
    > /abs/<campaign>_build.log 2>&1 &
echo $!            # keep the pid; kill with `kill <pid>` — NOT `pkill -f postprocess_report` (see §5)
tail -f /abs/<campaign>_build.log
```

### Runtime expectations (measured on the 8/24 campaign, 4 days, 6 engagements)

| stage | first build | rebuild (caches warm) |
|---|---|---|
| tracking day via `prep` (2 flights) | ~5 s (prep_from_dump scans every `tracks/*.csv`) | same — no cache |
| tracking day via `mdir` | ~1 s | same |
| engagement tab: 3-D GIF (`E.gif3d`, 135 frames matplotlib) | ~8–10 s each | same — GIF rewritten every run |
| engagement tab: FOV mp4 render (`_fov_render`, 5 fps, sequential IR decode + ffmpeg) | ~1.5–2 min each | 0 s — `[fov] reusing rendered figs/fov_<video>.mp4` |
| radar rollup: `_rr_day_samples` over all dirs + heat map | ~10–20 s | same |
| radar rollup: steal / corruption GIFs (101 frames + Esri tiles) | ~20–40 s each | 0 s — `[steal] reusing figs/steal_<tid>.gif (delete to re-render)` |
| standalone exports (base64 inlining) | ~2 s | same |

Whole 8/24 campaign with warm caches: **~65 s** (Sep 2 build: 11:46:28 → 11:47:31). First build with six FOV
videos: ~10–12 min. Memory is modest (< 2 GB).

### Outputs

```
<out_root>/
  <day id>/report.html             tabbed day page, plotly inlined (5–9 MB each)
  <day id>/figs/eng_<tid>_<hhmmss>.gif, ovw_<tid>_<hhmmss>.png, fov_<video>.mp4, fov_<tag>_<frame>.jpg
  <day id>/mission_inputs/         (tracking days without prep.out_mdir) truthv.npz, fast2_F1/F2.npz, analysis_stage1.json
  Rollup.html                      "Overall Rollup" tab (Days card table + rollup_html) [+ "Radar Rollup" tab]
  index.html                       meta-refresh → Rollup.html (directory-URL continuity)
  Rollup_standalone.html           Rollup.html with every figs/*.png|gif inlined as data URIs; day links → server_base
  Radar_Rollup.html                radar tab alone (when radar_rollup present)
  Radar_Rollup_standalone.html     single shareable file (~7.6 MB for 8/24) — the thing that gets emailed
  figs/steal_<tid>.gif, corruption_<tid>.gif, turn_zoom.png
```

The `*_standalone.html` files are the only outputs safe to copy elsewhere alone; `report.html` pages reference
`figs/` relatively and the FOV player `fetch()`es its mp4 as a blob (python `http.server` has no Range support,
so the whole file is downloaded — 30–45 MB per engagement).

### How the :8899 server / `server_base` are used

A plain `python3 -m http.server 8899 --bind 0.0.0.0` has been serving `$SERVED` since Aug 25 (pid 3002821; log
`/tmp/track2895_http.log`). Nothing in the pipeline starts or talks to it; it only matters that `out_root` is under
`$SERVED` so `http://172.18.1.28:8899/<out_root basename>/` resolves to `index.html` → `Rollup.html`, and that
`server_base` equals that URL so the standalone export's day links point at the served day reports. If the server is
gone: `cd $SERVED && nohup python3 -m http.server 8899 --bind 0.0.0.0 > /tmp/track2895_http.log 2>&1 &`.
Convention for the week folder: `$WEEK/reports/<name>` → symlink to `$SERVED/<name>`.

---

## 5. Verify

Open (browser on the LAN): `http://172.18.1.28:8899/<campaign>_report/` → Rollup with one card per day (check the
`N tabs` count on each card — a failed pane still counts, so open every day), then the Radar Rollup tab (az-bias bars
for every scoreable day, turn table, heat map with satellite underlay). Open each day page and click every tab; on
engagement tabs check the CPA numbers in the summary paragraph are not `nan` and the FOV player loads.

### Console on success (patterns to grep in the log)

```
=== 2026-09-16 (tracking)
global az bias -0.42 deg; RAW vertical residual (track-truth) +18 m (NOT removed; DU=0)
pad truth-U -14 m; altitude clamp >20 m AGL: truth 3097 -> 2210 pts
WROTE <out_root>/2026-09-16/report.html (5.7 MB)
=== 2026-09-17 (engagement)
[9/17 Flight 1 — pass 3 (07:23:47 PDT)] tc=1787927027.0 inter=131 targ=15 trk177=29 cpa_truth=53m cpa_trk=46m
  [fov] reusing rendered figs/fov_raw_camera_….mp4 (delete it to force a re-render)     # or: [fov] wrote … (N frames @5 fps real-time)
WROTE <out_root>/2026-09-17/report.html (5.0 MB)
=== radar rollup
  [radar] 9/16 (9b22a989 (Turquoise_Emu)): az bias -1.45° · active 85.1% · coast turns 24.0% vs straight 13.1% (1.8x)
  [steal] wrote figs/steal_177.gif (101 frames, 1x real-time)
WROTE <out_root>/Rollup.html (5.4 MB)
WROTE <out_root>/Rollup_standalone.html (7 MB, single file)
WROTE <out_root>/Radar_Rollup.html (5.4 MB)
WROTE <out_root>/Radar_Rollup_standalone.html (7 MB, single file)
```

Anything printed as `  <name> skipped: …` or `  flight F1R1 skipped: …` means a pane was replaced by an error stub —
the run still exits 0. `grep -n "skipped\|Traceback\|unavailable" <log>` after every build.

### Common failure messages and fixes

| message | cause | fix |
|---|---|---|
| `init_mission: NO matched tracks this mission — bias/datum set to 0` then `flight F1R1 skipped: …` / empty panes | `prep.target_pattern` matched the wrong feed, `init.ant`/`ant_hae_m` wrong (truth lands far from tracks), or genuinely no target-side tracks (8/25) | `ls <dump>/mavlink/`; compare `meta.json.antenna` with `init.ant`; check `analysis_stage1.json` `tracks[*].med` in `out_mdir` |
| `KeyError: 2` from `build_summary_tab` / `FileNotFoundError: …/fast2_F2.npz` | tracking day with one `flights_pdt` entry | give exactly two windows (split a single flight at its midpoint if needed) |
| `flight F1R2 skipped: KeyError: (1, 2)` | `flight_keys` with a lap number ≠ 1 on a `prep` day | use `F1R1`, `F2R1` |
| Summary tab header says "Mission rollup — 2026-08-26, MRU91 run Turquoise_Emu" | hard-coded in `tracking_tab.build_summary_tab` | edit lines 1236-1240 of `tracking_tab.py` for the new campaign |
| Summary tab shows 8/26 "Key findings" about tracks 94/917 | `notes_html` empty | author `notes_html` |
| `<name> skipped: <name>: no valid CPA near seed (interceptor pts in seed window: N)` | `cpa_seed_pdt` off by more than ~20 s, interceptor pattern wrong, or the interceptor truth was frozen (all samples removed by `drop_frozen`) | re-read `passes[].t_pdt`; check `mavlink/mav14551_2_*.csv` has moving rows around the seed |
| `<name> skipped: too few samples in CPA window (inter N, targ M) — check inputs` | truth gap at the pass (< 4 samples in t−10…t+5) | pick another pass or use `target.traj_csv` (onboard GPS) |
| `FileNotFoundError: no truth rows for <pattern> in <dump>` (empty match guard in `load_dump_truth`) | pattern/dump mismatch or window outside the dump | fix `pattern`, `dump`, `window_pdt`, `date` |
| `FileNotFoundError: <dump>/tracks/track_<id>.csv` | track id not in this dump's window | check the manifest's `target_track_spans` for that dump; use the right quickdump dir |
| `KeyError: 'antenna_origin_lat_lon_haeM'` | only from `toxic_zones.py`'s own CLI on a quickdump (`meta.json` has `antenna` instead); the pipeline's `_rr_satmap` accepts `antenna` | not a pipeline error; for toxic_zones CLI add the key or use an archiver dump |
| `ValueError: max() arg is an empty sequence` in `pick_truth_csvs` | radar_rollup day dir has no `mavlink/mav14550*.csv` | the target must be on port 14550 for the rollup, or edit `toxic_zones.pick_truth_csvs` |
| Seeker quad empty / no "Inside 12° FOV" line on a `prep` tracking day | `speed_mps` missing in quickdump mavlink CSV → truth speed 0 → moving gate `m[:,15] > 4` drops everything | use an archiver-layout dump, or add a `speed_mps` = hypot(vel_n, vel_e) column to the truth CSV before `prep` |
| `[radar] satmap attempt 1 error: …` ×2 then `satellite tiles unavailable — plain background` | no HTTPS to `server.arcgisonline.com` | cosmetic; re-run when online (steal GIFs are cached — delete them to re-render with imagery) |
| `[fov] flight log not available (…) — FOV section skipped` / `IR video / camera model not available` | wrong glob/path, or `cam` lacks `fps`+`onset_frame`/`utc0` and no `camwarp` | fix paths; the tab still builds |
| `<name> skipped: ffmpeg transcode failed rc=…` or `FileNotFoundError: …/sensorenv/bin/ffmpeg` | ffmpeg not on PATH and fallback path missing | run under `micromamba run -n sensorenv`; `which ffmpeg` |
| `ModuleNotFoundError: cv2 / pyulog` | wrong interpreter | sensorenv only |
| plotly `ValueError: … kaleido … Chrome` | not raised by this pipeline (no `fig.to_image`/`write_image`; all PNG/GIF/mp4 are matplotlib/PIL/cv2). Only appears if someone adds plotly rasterization | keep figures as `pyo.plot(... output_type="div")`; if rasterizing is needed install kaleido + Chrome |
| `[mongo] quickdump: …` then `FileNotFoundError` on `<run>_quickdump/mavlink` | `mongo` day input: `ensure_dump` passes no `--host/--run/--day/--root` and returns a relative name | dump beforehand with quickdump (§1) and set `dump` |
| Build stops mid-way with no traceback when run in the background | the launching shell was killed — typically `pkill -f postprocess_report` matched the `bash -c '… postprocess_report.py …'` wrapper (self-kill) or the agent's own shell | kill by pid; if you must pattern-match use `pkill -f "python postprocess_report.py"` from a shell whose command line does not contain that string |
| Streamlit `No runtime found, using MemoryCacheStorageManager` | `ih` import in quickdump | harmless |
| Engagement summary shows `nan m to radar track` | track CPA not found within ±120 s | late-born/early-dead track; say so in `summary_html` or choose the track alive at the pass |

---

## 6. Add a day, re-run one day, iterate narratives

Caching actually implemented (everything else is recomputed on every run):

- `radar_rollup.steal_views[].gif` / `corruption_views[].gif`: reused if the file exists under `out_root` (delete to re-render).
- `fov` mp4: reused if `<day>/figs/fov_<video basename>.mp4` exists.
- IR frame strip jpgs `figs/fov_<tag>_<frame>.jpg` (only used by `_ir_extract`, cached by frame index).
- NOT cached: `prep_from_dump` mission inputs, 3-D GIFs (~10 s each), overview PNGs, `turn_zoom.png`, all plotly divs, rollup stats.

Because a warm rebuild is ~1 min, the normal loop for narrative edits is simply **edit JSON → full run → reload**.

**Add a day**: dump it (§1), manifest it (§2), append a `days[]` entry (and a `radar_rollup.days[]` entry with its
`dirs`), re-run. Existing day folders under `out_root` are overwritten in place; nothing is deleted, so stale
`figs/` from removed engagements linger (harmless).

**Re-run a single day without touching the others** — no CLI flag; use the module API from `$TC`:

```bash
cd $TC && micromamba run -n sensorenv python - <<'EOF'
import json, os, postprocess_report as P, engagement_tab as ET
cfg = json.load(open("/abs/<campaign>.json"))
root = cfg["out_root"]
day = next(d for d in cfg["days"] if d["id"] == "2026-09-17")
day_dir = os.path.join(root, day["id"]); os.makedirs(day_dir + "/figs", exist_ok=True)
tabs = P.tracking_day_tabs(day, day_dir) if day["type"] == "tracking" else P.engagement_day_tabs(day, day_dir)
ET.write_page(f"{day_dir}/report.html", day["title"], day.get("sub", ""), tabs)
EOF
```

(`main()` always rewrites `Rollup.html` from the full `days` list, so finish with one full run before sharing;
a trimmed copy of the JSON with only one day would produce a one-card Rollup.)

**Radar rollup only** (this is how the Sep 8 rebuild was done — `turn_zoom.png` and `Radar_Rollup_standalone.html`
were rewritten without the day pages):

```bash
cd $TC && micromamba run -n sensorenv python - <<'EOF'
import json, postprocess_report as P
cfg = json.load(open("/abs/<campaign>.json"))
html = P.build_radar_rollup(cfg["radar_rollup"], cfg["out_root"])
P.write_radar_only(cfg["out_root"], cfg, html)
EOF
```

**Rollup text only** (`rollup_html`, card texts): the cheapest correct path is still the full run (~1 min);
the standalone exporter (`P.write_standalone_rollup(root, server_base)`) can be re-invoked alone if only
`Rollup.html` was hand-edited.

**Force a re-render** of a steal/corruption animation or FOV video: delete the file named in the log line
(`figs/steal_<tid>.gif`, `<day>/figs/fov_<video>.mp4`) and re-run.

**Sharing**: `Radar_Rollup_standalone.html` / `Rollup_standalone.html` are self-contained (images inlined; day links
go to `server_base`). Zip the whole `out_root` when the recipient has no LAN access (the 8/24 report shipped as
`Seawall_Week_Rollup.zip` + `Seawall_Radar_Rollup.zip`).
