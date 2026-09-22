# Campaign JSON — complete schema for `postprocess_report.py`

Reference for the config consumed by `../scripts/postprocess_report.py` (the skill copy is
the maintained one since 2026-09-15; `track_correlation/postprocess_report.py` is the older
original). Derived by reading the code and the real campaign file
`../templates/seawall_week_0824.json` (the one that produced the Seawall Week of 8-24 report,
which the current code still reproduces byte-for-byte modulo plotly's random div ids).
Every key below is quoted with the code site that reads it. "Campaign-specific" means the
analyst must set it per campaign; "constant" means the value is a pipeline convention that
should be copied verbatim.

**2026-09-15 generalisation** — the pipeline runs for ANY MRU / site / timezone / drone ids:
campaign-level `tz`, `target_pattern`, `interceptor_pattern`, `dump_root`; live-mongo day
inputs via `dump_run_window.py`; 1..N flights on tracking days; exact WGS-84 truth frame on
prep days; ffmpeg-less builds keep the engagement tab (only the FOV block is replaced);
missing antenna is warned about loudly. Every default reproduces the 8/24 report.

Invocation: `cd ../scripts && micromamba run -n sensorenv python postprocess_report.py <campaign.json>`
— the script has NO argparse, no `--help`, no `--day`/`--only` flags; `sys.argv[1]` is opened
with `json.load`.
Unknown keys anywhere are ignored (every read is `day.get(...)` / `cfg.get(...)`), so
`"_comment"` keys are safe at every level EXCEPT inside fixed-length list rows
(`flights_pdt` triples, `steal_examples` 5-tuples, `init.day`, `init.ant`).

---

## 1. Top level

| key | type | req | default | consumed by | campaign-specific? |
|---|---|---|---|---|---|
| `title` | str | yes | — | `main()` → `ET.write_page(Rollup.html, cfg["title"], …)`; `write_radar_only()` appends `" — Radar Rollup"` | yes |
| `sub` | str | no | `""` | `main()` → Rollup/Radar_Rollup page sub-line | yes |
| `out_root` | abs path | yes | — | `main()`: `os.makedirs(root)`; every output lands under it | yes — put it under the :8899 served root (see §7) |
| `rollup_html` | HTML str | no | `""` | `main()`: appended after the auto-built Days table in the "Overall Rollup" tab | yes (narrative) |
| `days` | list | yes | — | `main()` iterates; one `<out_root>/<id>/report.html` per entry | yes |
| `radar_rollup` | object | no | absent → no Radar Rollup tab / no `Radar_Rollup*.html` | `main()` → `build_radar_rollup(cfg["radar_rollup"], root)` | yes |
| `server_base` | URL str | no | `None` | `write_standalone_rollup()`: rewrites `href="YYYY-MM-DD/report.html"` → `<server_base>/YYYY-MM-DD/report.html` in the `*_standalone.html` exports | yes (`http://172.18.1.28:8899/<report dir name>`) |
| `tz` | IANA name | no | `"America/Los_Angeles"` | `configure()` → `ET.set_tz` / `TT.set_tz`: EVERY clock string (`flights_pdt`, `laps_pdt`, `window_pdt`, `cpa_seed_pdt`, `t_steal_pdt`, `t_pdt`) is parsed in this zone and every printed time / "time (PDT)" label uses its abbreviation at that instant (`PDT`/`PST`/`CEST`…). DST-aware (zoneinfo). Also passed to `dump_run_window.py --tz` for `mongo` inputs | yes for a non-Pacific site; August Pacific dates are unchanged (PDT = UTC−7) |
| `target_pattern` | glob | no | `None` → legacy `mav14550_1_1.csv` (radar rollup: biggest `mav14550*.csv`) | `_pat("target", …)`: tracking `prep`, engagement `target.pattern`, steal/corruption views, `turn_zoom`, `radar_rollup.days[]` (`toxic_zones.pick_truth_csvs` — LARGEST match). Per-day / per-engagement / per-rollup-day `target_pattern` override | yes — `ls <dump>/mavlink/` |
| `interceptor_pattern` | glob | no | `None` → legacy `mav14551_2_*.csv` (rollup: `mav14551*.csv`) | same scopes as `target_pattern`; several compids are merged | yes |
| `dump_root` | abs path | no | `<out_root>/dumps` | root for `mongo` day inputs (`dump_run_window.py --root`) | when using `mongo` |
| `geoid_n` | float m | no | dumper default −31.4 | passed as `--geoid-n` to the dumper for `mongo` inputs (tracking days ALSO need `init.geoid_n`) | new site |

Outputs written by `main()` (all under `out_root`): `<day.id>/report.html` + `<day.id>/figs/`,
`Rollup.html`, `index.html` (meta-refresh → Rollup.html), `Rollup_standalone.html`
(every `figs/*.png|gif` inlined as data URIs), and when `radar_rollup` is present
`Radar_Rollup.html` + `Radar_Rollup_standalone.html`, plus `figs/` at the root for the
rollup's own GIF/PNGs.

---

## 2. `days[]` — keys common to both day types

| key | type | req | default | consumed by | notes |
|---|---|---|---|---|---|
| `id` | str | yes | — | `main()`: `day_dir = <out_root>/<id>` | MUST be `YYYY-MM-DD` if you want the standalone export's day links rewritten (`write_standalone_rollup` regex is `\d{4}-\d{2}-\d{2}/report\.html`). Anything else works but leaves dangling links in the lone file. |
| `title` | str | yes | — | `main()`: Rollup card. If it contains `" ("` and ends with `")"`, link text = part before ` (`, description = inside the parens; else description = `type` | e.g. `"9/16 — Tracking (2 flights)"` |
| `type` | `"tracking"` \| `"engagement"` | yes | — | `main()` dispatch to `tracking_day_tabs` / `engagement_day_tabs` (anything not `"tracking"` is treated as engagement) | |
| `sub` | str | no | `""` | `ET.write_page(...)` day page sub-line | e.g. `"run <run8> · post-cal"` |
| `key_message_html` | HTML | no | `""` | Rollup card, "Key message" column | narrative |
| `numbers_html` | HTML | no | `""` | Rollup card, "Numbers" column | narrative |
| `dump` | abs path | one of `dump`/`mongo` | — | `ensure_dump(obj, day)` (engagement: per-engagement `dump`/`mongo` wins, else the day's; tracking: `prep.dump`/`prep.mongo`, `day.dump`/`day.mongo`) | dump dir: `<…>/<date>/<run8>_<label>/` with `mavlink/`, `tracks/`, `meta.json` — `dump_run_window.py`, old `quickdump.py` and archiver layouts are all readable (columns are addressed by NAME; extra columns such as `time_local`/`speed_mps`/`truth_match_source`, and the `adsb/`, `obs/`, `jobs/` folders are ignored) |
| `mongo` | object | one of | — | `ensure_dump()` → `run_dumper()` builds the exact `scripts/dump_run_window.py` command line, streams its progress as `[dump] …`, parses the printed `DUMP <dir>` / `exists: <dir>` line and writes the directory back into `obj["dump"]` (cached on the spec, so several engagements sharing a day-level `mongo` dump once) | keys: `mru` (→ `10.1NN.28.205`) **or** `host`; `run` (hex prefix \| `run_<hex>` \| friendly name \| `latest`); `jobs: "A-B"` **or** `t0`/`t1` (`"YYYY-MM-DD HH:MM[:SS]"`, or a bare `"HH:MM"` / quickdump-style `"HHMM"` which is placed on the day's `date`/`id`); `label` (folder `<run8>_<label>`, default `HHMM-HHMM`); optional `root` (else campaign `dump_root`), `tz`, `geoid_n`, `antenna [lat,lon,hae]`, `no_adsb`, `no_obs`, `overwrite`, `port`, `db`, `chunk_s`, `max_track_range_m`. An existing complete dump is reused unless `overwrite`. Also valid on `radar_rollup.days[]` (single spec or list), `steal_views[]`, `corruption_views[]`, `turn_zoom` |
| `target_pattern` / `interceptor_pattern` | glob | no | campaign value | per-day override of the campaign globs (§1) | |

---

## 3. `days[]` with `type: "tracking"` (module `tracking_tab.py`)

Builds a Summary tab + one tab per `flight_keys` entry. Data path: either an existing
mission dir (`mdir`) or `prep` (synthesize the mission dir from a dump).

| key | type | req | default | consumed by | notes |
|---|---|---|---|---|---|
| `mdir` | abs path | one of `mdir`/`prep` | — | `tracking_day_tabs()`: `TT.init_mission(mdir, spec=None, …)` | **`spec=None` selects the LEGACY 8/26 mission structure** (`tracking_tab.init_mission`, lines 137-155: hard-coded FLIGHTS/RUNS/INBOUND/track-id lists for MRU91 run 9b22a989). Only valid for `VP_TrackAnalysis/mru91_track2895/mission_20260826`. A new campaign MUST use `prep`. |
| `prep` | object | one of | — | `tracking_day_tabs()` → `TT.prep_from_dump(dump, out_mdir, target_pattern, flights, ant_hae_m)` then `init_mission(..., spec={"gate_m": …})` (GENERIC structure) | |
| `prep.dump` | abs path | yes (or `day.dump`) | — | `prep_from_dump()` reads `<dump>/mavlink/<target_pattern>` (largest match) and `<dump>/tracks/track_*.csv` | |
| `prep.target_pattern` | glob | no (campaign `target_pattern`, else legacy `mav14550_1_1.csv`) | — | `prep_from_dump()`: `max(glob(f"{dump}/mavlink/{pattern}"), key=size)`; a no-match raises listing the ids present | e.g. `"mav14550_1_1.csv"`; the exact `target_id` naming differs per day (8/25 was `mav14551_2_2.csv`) — `ls <dump>/mavlink/` first |
| `prep.ant_hae_m` | float (m HAE) | no | `init.ant[2]` (with a console note) | `prep_from_dump()`: truthv U column = `alt_ft_wire - ant_hae_m` (feet minus metres, deliberately: `init_mission` inverts it with `(U + ANT_HAE)*0.3048 + GEOID_N`) | a value > 0.5 m away from `init.ant[2]` prints a WARN. Read from the dump's `meta.json` (`antenna[2]` or `antenna_origin_lat_lon_haeM[2]`). |
| `prep.flights_pdt` | list of `[date, hms, hms]` | yes | — | `tracking_day_tabs()`: `ET.epoch_pdt(d, a), ET.epoch_pdt(d, b)` in the campaign `tz` | **1..N entries** (one `fast2_F<n>.npz` each; `init_mission` globs `fast2_F*.npz`, the Summary iterates `sorted(FLIGHTS)`). Since 2026-09-15 — before, exactly two were required. |
| `prep.laps_pdt` | `{"<flight>,<lap>": [date, hms, hms]}` | no | one lap per flight | `spec["runs"]` → `Flight N · Lap N` tabs | list every key in `flight_keys` as `F<flight>R<lap>` |
| `prep.out_mdir` | abs path | no | `<day_dir>/mission_inputs` | `prep_from_dump()` writes `truthv.npz` (10 cols: …, lat, lon), `fast2_F<n>.npz`, `analysis_stage1.json` (rewritten every run — no cache) | truth E/N/U are rebuilt by `init_mission` with the EXACT `corr_lib.EnuFrame` from the stored lat/lon (the legacy 8-col layout keeps the equirectangular inverse for the served 8/26 mdir) |
| `prep.gate_m` | float | no | `350.0` | `spec["gate_m"]` → `init_mission`: tracks whose median distance to truth < gate are FLIGHT_TRACKS (top 10 by samples); PLOT_TRACKS = those with med < 200 m and ≥ 20 samples (max 6) | constant unless truth is very biased (8/25 pre-cal) |
| `init` | object | no (but effectively required) | `{}` | `tracking_day_tabs()` passes `day`, `ant`, `geoid_n` (+ the campaign `tz`) through to `TT.init_mission` | omit `ant` on a `prep` day → the dump's `meta.json` antenna is used with a `[WARN]`; no meta antenna either (or an `mdir` day) → `DEFAULT_ANT` = MRU91 8/26 antenna (33.7480633, −115.3392025, 139.88) with a loud `[WARN]` |
| `init.day` | `[Y, M, D]` | no | `(2026, 8, 26)` | `TT.DAY` — only used by `hhmm()` in the legacy branch; harmless for GENERIC but set it anyway | |
| `init.ant` | `[lat, lon, hae_m]` | yes in practice | MRU91 | `TT.ANT_LAT/LON/HAE` — truth E/N/U reconstruction and AGL clamp | per-deployment (from `meta.json`) |
| `init.geoid_n` | float m | no | `-31.4` (`DEFAULT_GEOID_N`, Seawall site) | `TT.GEOID_N`: truth alt ft-MSL → m-HAE | **site constant** — a new site needs its own HAE−MSL undulation |
| `notes_html` | HTML | no | `""` | `TT.build_summary_tab(notes_html=…)` | **`""` falls back to the ORIGINAL 8/26 "Key findings" narrative (hard-coded text about tracks 94/917, −1.45°, …)**. Always author non-empty text. |
| `flight_keys` | list of `"F<n>R<m>"` | no | `[]` | `tracking_day_tabs()`: label `Flight n · Lap m`; `TT.build_flight_tab(k)` → `RUNS[(n, m)]` | With `prep` and no `laps_pdt`, RUNS = `{(i,1)}` → valid keys are **`F<n>R1`**; an unknown key raises `KeyError` → pane replaced by "flight pane …: not available (…)" (build continues). |

What `init_mission` prints (success): `global az bias +X.XX deg; RAW vertical residual (track-truth) ±NN m (NOT removed; DU=0)` and
`pad truth-U NNN m; altitude clamp >20 m AGL: truth A -> B pts`. The empty-match guard prints
`init_mission: NO matched tracks this mission — bias/datum set to 0` (wrong `target_pattern`, wrong `ant`, or no target-side tracks).

Generated text on `prep` (GENERIC) days is computed from the data: the Summary `<h2>` is
"Mission rollup — <date>" + "<N> flight(s), <M> lap window(s). Flight n: hh:mm:ss–hh:mm:ss (x min)…",
every lap tab's window line says "truth range a–b km" (min/max truth ground range in the window),
and an empty `notes_html` renders a "not yet written" placeholder. The 8/26 literals
("MRU91 run Turquoise_Emu", "outbound to ~3.9 km and back", the 8/26 key findings) survive ONLY
on the legacy `mdir` path (`spec=None`), which is kept byte-identical for the served 8/26 report.

---

## 4. `days[]` with `type: "engagement"` (module `engagement_tab.py` + `eng_plots.py`)

One tab per `engagements[]` entry (optional leading Overview tab). Each tab = summary
paragraph → Overview PNG (ground tracks + distance-vs-time with <75 m passes starred) →
FOV player (if `fov`) → animated 3-D GIF → top-down → closing distance → closing speed →
heading error → track-metrics error window → state-at-CPA table → `post_html`.

| key | type | req | default | consumed by | notes |
|---|---|---|---|---|---|
| `date` | `YYYY-MM-DD` | yes | — | `engagement_day_tabs()`: `ET.epoch_pdt(date, hms)` for every clock string of the day | |
| `ant` | `[lat, lon, hae_m]` | yes | — | `cfg["ant"]` → `build_fov_section(ant=…)` (ulog ENU frame, MSL display) and `load_traj_csv_truth(…, ant)` | per-deployment from `meta.json` |
| `overview_html` | HTML | no | absent → no Overview tab | `engagement_day_tabs()` prepends `("Overview", html)` | narrative |
| `engagements` | list | yes | — | one tab each, `enumerate(start=1)` → `i` | |
| `engagements[].name` | str | yes | — | `<h3>` heading, GIF title, console tag `[name] tc=…` | e.g. `"9/16 Flight 2 — pass 3 (10:12:05 PDT)"` |
| `engagements[].dump` / `.mongo` | abs path / spec | one of (else the day's `dump`/`mongo`) | — | `ensure_dump(eng, day)` → `load_dump_truth` / `load_dump_track13` | may differ per engagement (8/28 used three quickdumps) |
| `engagements[].interceptor_pattern` | glob | no | day → campaign `interceptor_pattern` → legacy `"mav14551_2_*.csv"` | `ET.load_dump_truth(dump, pattern, t0, t1)` merged across matches, `drop_frozen` applied | MAVLink alias naming: `mav<port>_<sysid>_<compid>`; the interceptor publishes several compids (2_0, 2_1, 2_34) → keep the wildcard |
| `engagements[].target` | `{"pattern": glob}` **or** `{"traj_csv": path}` | `pattern` optional (falls back to the day/campaign `target_pattern`) | — | `pattern` → `load_dump_truth(dump, pattern, t0, t1)`; `traj_csv` → `load_traj_csv_truth(csv, ant)` (columns `utc_s, lat, lon, alt_hae_m` — onboard-GPS export, used 8/27 eng 2 when the MAVLink truth froze); optional `label` for the legend | |
| `engagements[].interceptor_label` / `.target_label` | str | no | derived from the truth files actually loaded (`ET.id_label`: compids of one port → the port number `14551`; anything else → the id, e.g. `mavlink_1_2`) | legend names "interceptor <id> (truth)" / "target <id> — TRUTH / radar TRACK n" | |
| `engagements[].window_pdt` | `[hms, hms]` | yes | — | `t0, t1` for loading truth (padded ±60 s inside `load_dump_truth`) and the Overview figure span | the whole flight/engagement, not just the pass |
| `engagements[].cpa_seed_pdt` | `hms` | yes | — | `cfg["cpa_seed"]`; `build_engagement_tab` refines the true CPA with `E.cpa` within ±20 s of the seed; track CPA searched ±20 s then ±120 s (late-born track) | from the manifest's `passes[].t_pdt` |
| `engagements[].track_id` | int | yes | — | `load_dump_track13(dump, tid)` → `<dump>/tracks/track_<tid>.csv` (13 cols) | `FileNotFoundError` if not in the dump window |
| `engagements[].gif` | rel path | no | `f"figs/eng{i}_3d.gif"` | `E.gif3d(..., os.path.join(day_dir, gif))`; embedded as `<img class="gif">` | keep the `figs/` prefix (standalone inliner only matches `src="figs/…"`) |
| `engagements[].summary_html` | HTML w/ `str.format` fields | no | `"<p>Interceptor closest approach: <b>{cpa_truth:.0f} m …</b>, <b>{cpa_track:.0f} m to radar track {track_id}</b>.</p>"` | `.format(cpa_truth=, cpa_track=, track_id=)` | only these three fields; any other `{…}` → `KeyError`; literal braces must be `{{ }}` |
| `engagements[].post_html` | HTML | no | `""` | appended after the tab (also after the failure stub) | narrative |
| `engagements[].tab_label` | str | no | `f"Engagement {i}"` | tab button text | |
| `engagements[].pre`, `.post` | float s | no | 18.0 / 5.0 | copied into `cfg` but **NOT read** by `build_engagement_tab` — the CPA plot window is the BINDING standard `t−10 … t+5` (line 219) | ignore / omit |
| `engagements[].fov` | object | no | absent → no FOV player | `build_fov_section(fov, outdir, tc, target, ant, trkV, name)` inside a try/except in `build_engagement_tab`: missing ulog → `""` with a console `[fov] …skipped` note; **missing ffmpeg (and no cached mp4) or any render error → the block is replaced by an "Seeker FOV section unavailable …" note and the tab still builds.** ffmpeg is resolved by `ET.ffmpeg_bin()`: `$SEAWALL_FFMPEG` (a non-executable value DISABLES it — handy to test the skip path), `PATH`, the interpreter's `bin/` (sensorenv), the legacy absolute path | |
| `fov.ulog` | path or glob | yes | — | `_ulog_series()`: `glob`, largest file wins; pyulog topics `vehicle_status, vehicle_gps_position, vehicle_local_position, vehicle_attitude` | interceptor (Zeus) PX4 log |
| `fov.zoff` | float s | yes | — | added to the ulog UTC anchor — the per-log flight-controller clock offset (8/27: 36002.0 = FC 10 h 00 m 02 s behind UTC; 8/28: 36001.3–36001.8) | **campaign-specific, measured by position xcorr vs truth** |
| `fov.video` | path | yes | — | IR mkv, decoded strictly sequentially (`cv2`; broken indexes, never seek) | |
| `fov.camwarp` | npz path | one of camwarp/cam | — | `np.load(cw)["tcorr"]` = per-frame true capture times | 8/27 style |
| `fov.cam` | `{"fps": f, "onset_frame": n}` or `{"fps": f, "utc0": epoch}` | one of | — | frame-0 UTC = ulog takeoff − onset_frame/fps (takeoff = first sustained vz < −1 m/s ≥ 2 s) | 8/28 style |
| `fov.target_label` | str | no | `"target truth"` | legend text | |

Constants baked into the engagement tab (not configurable): `MIN_PTS = 4` interceptor samples;
CPA window −10/+5 s; error-window ±12 s (`err_pre`/`err_post` per engagement); overview pass gate `< 75 m`, both craft ≥ 3 m/s, closing ≥ 8 m/s, ≥ 20 s apart;
FOV cone 12° full / 500 m, 5 Hz grid; PX4 `nav_state == 14` = OFFBOARD. Legend ids are derived (see `interceptor_label`).

Console on success per engagement: `[<name>] tc=<epoch> inter=N targ=N trk<id>=N cpa_truth=NNm cpa_trk=NNm`.
Failures (including missing dump / track csv / bad pattern) are caught in `engagement_day_tabs()` and print
`  <name> skipped: <error>`; the tab is replaced by "not buildable from archived data (<error>)" and the build
continues. A whole day failing (e.g. the dumper) is stubbed by `main()` as an "Error" tab and the campaign goes on.

---

## 5. `radar_rollup` (built by `postprocess_report.build_radar_rollup`, uses `toxic_zones.py` gates)

Everything numeric here is computed at build time from the dump dirs; only the narratives and
the example pointers come from config.

| key | type | req | default | consumed by | notes |
|---|---|---|---|---|---|
| `intro_html` | HTML | no | `""` | top of the tab | narrative |
| `az_bias_html` | HTML | no | `""` | section 1 text (before the computed az-bias bar chart) | narrative (the 8/24 file embeds a per-day table here) |
| `turns_html` | HTML | no | `""` | section 2 text (before the computed turn-penalty table) | narrative |
| `corruption_html` | HTML | no | `""` | section 3 | narrative |
| `steals_html` | HTML | no | `""` | section 4 | narrative |
| `heatmap_html` | HTML | no | `""` | section 5 (week heat map) | narrative |
| `days` | list | yes | — | one row per campaign day | |
| `days[].label` | str | yes | — | table/bar labels; must be unique | `"9/16"` |
| `days[].scoreable` | bool | no | `true` | `false` → row shows `note`, no computation | 8/25 (0 target-side tracks) |
| `days[].note` | str | no | `"not scoreable"` | with `scoreable:false` | |
| `days[].dirs` | list of abs paths | one of `dirs`/`mongo` if scoreable | — | `_rr_day_samples(dirs, target_pattern, interceptor_pattern)`: `TZ.pick_truth_csvs(d, …)` (target = LARGEST match of the pattern; default biggest `mavlink/mav14550*.csv`), `TZ.load_target_tracks` (median < 150 m); `meta.json` of `dirs[0]` read for the run label (`run_id8` or `run`, `friendly_name`) and the satellite tile antenna (`antenna_origin_lat_lon_haeM` / `antenna_origin` / `antenna`) | several dumps of one day are merged; a day that fails to load is shown as "not computable" and skipped |
| `days[].mongo` | spec or list of specs | one of | — | each spec dumped with `dump_run_window.py` (§2 `mongo`), results become `dirs` | |
| `days[].target_pattern` / `.interceptor_pattern` | glob | no | `radar_rollup.*_pattern` → campaign → toxic_zones defaults | truth loaders | |
| `days[].az_bias_cfg` | float deg | no | — | used only when the day is not scoreable / bias not computable; plotted with a `*` | analyst-provided |
| `cell_m` | float m | no | `50.0` | heat-map cell size; each cell scored over its 5×5 neighbourhood | 8/24 used `30.0` |
| `min_dwell` | int | no | `2` | min truth samples for a cell to exist; worst-cell table needs `dwell ≥ 8*min_dwell` | |
| `steal_examples` | list of `[day, run, track, time, what]` | no | — | rendered as a table in section 4 (no computation) | narrative (fixed 5-element rows — no `_comment` inside) |
| `steal_views` | list | no | `[]` | `_rr_steal_gif(sv, root)` (animated ±10 s 2-D view, cached by file) + `_rr_duel_fig(sv, root)` (distance-duel plot) | |
| `steal_views[].label` | str | yes | — | figure title | |
| `steal_views[].dump` | abs path | yes | — | truth + track csvs | |
| `steal_views[].date` | `YYYY-MM-DD` | yes | — | with `t_steal_pdt` → epoch | |
| `steal_views[].t_steal_pdt` | `hms` | yes | — | centre of the ±10 s animation | |
| `steal_views[].track_id` | int | yes | — | the stolen track | |
| `steal_views[].ant` | `[lat, lon, hae]` | yes | — | satellite tile geo-referencing (`_rr_satmap`) | |
| `steal_views[].cap` | HTML | no | `""` | caption | narrative |
| `steal_views[].gif` | rel path | no | `f"figs/steal_{tid}.gif"` | **cache key**: if the file exists it is reused (`[steal] reusing … (delete to re-render)`) | |
| `steal_views[].banner` / `.rel_label` | str | no | `"TRACK STEAL"` / `"steal"` | flashing banner text; clock suffix | |
| `steal_views[].interceptor_pattern` / `.target_pattern` | glob | no | campaign patterns → legacy `"mav14551_2_*.csv"` / `"mav14550_1_1.csv"` | truth loaders; `dump` may be replaced by a `mongo` spec; a missing `ant` falls back to the dump's `meta.json` antenna for the tiles | |
| `corruption_views` | list | no | `[]` | same schema/loader as `steal_views` (set `banner`/`rel_label` to `"CPA"`) **plus** `ET.E.err_window_fig(trkV, target, t, ±12 s)` az/el/range/alt error panels | |
| `az_bias_example` | object | no | absent → no example map | `TT.init_mission(ex["mdir"], spec=…, **init)` then `TT.maps_fig(flight, RUNS[(flight, run)], color_of(flight))` — RAW vs bias-rotated maps | default `spec=None` = the legacy 8/26 mission table (only `mission_20260826`). For a `prep`-generated `mission_inputs` dir set `"generic": true` (+ optional `"laps_pdt"` and `"gate_m"`) so the GENERIC structure is used; `flight`/`run` must then exist in that structure |
| `az_bias_example.mdir`, `.init{day,ant,geoid_n}`, `.flight` (default 1), `.run` (default 2), `.generic`, `.laps_pdt`, `.gate_m`, `.caption_html` | | | | | |
| `turn_zoom` | object | no | absent → no figure | `_rr_turn_zoom_fig(tz, root)` writes `figs/turn_zoom.png` (rewritten every run) | |
| `turn_zoom.dump`, `.date`, `.t_pdt`, `.tracks` (list: `[dying_track, successor]` — the FIRST id is checked for death within +3 s), `.win_s` (45), `.label`, `.cap`, `.target_pattern` | | | | | |

Console per scoreable day: `  [radar] <label> (<run8> (<friendly>)): az bias +X.XX° · active NN.N% · coast turns NN.N% vs straight NN.N% (N.Nx)`.
Satellite fetch failure prints `  [radar] satellite tiles unavailable — plain background` (offline / Esri blocked) and continues;
the heat-map section then carries a "Satellite imagery unavailable at build time" caption. Time labels in the tab use the zone
abbreviation of the first truth sample (`PDT`/`PST`/…).

---

## 6. What must be authored by the analyst vs what is computed

Authored (HTML strings and pointers — nothing here is derived from data by the code):

- `title`, `sub`, `rollup_html`, every `days[].title/sub/key_message_html/numbers_html`
- tracking `notes_html` (the whole Summary narrative below the auto tables)
- engagement `overview_html`, `summary_html` (with the three format fields), `post_html`, `tab_label`, `name`
- `radar_rollup.*_html`, `steal_examples`, `steal_views[].label/cap`, `corruption_views[].label/cap`, `turn_zoom.label/cap`, `az_bias_example.caption_html`, `days[].note`, `days[].az_bias_cfg`
- all the **pointers**: dump dirs, `target_pattern`s, `track_id`s, `window_pdt`, `cpa_seed_pdt`, `t_steal_pdt`, `t_pdt`, `flights_pdt`, `ant`, `ant_hae_m`, `fov.{ulog,video,zoff,cam|camwarp}`.
  These come from the day manifest (`<day>/flights.json`: `t0_pdt/t1_pdt`, `passes[].t_pdt`, `tracking.target_track_spans`, `tracking.steal_events`) and `meta.json` (`antenna`).

Computed at build time (never write these numbers into the config except as narrative):

- tracking: az bias (median track−truth bearing), vertical residual, per-lap horiz/3-D med/p95, measurement count/rate, coverage, longest gap, seeker pAcq per standoff, timelines, drop table
- engagement: true CPA time/distance (truth-truth and truth-track), overview pass list (<75 m), FOV in-cone events, state-at-CPA table, az/el/range/alt error window
- radar rollup: per-day az bias bars, active %, turn vs straight coasting penalty, far-turn death table, steal animations/duel plots, corruption error windows, week heat map (percentile-ranked), worst-cells table
- Rollup card tab counts, standalone data-URI inlining

---

## 7. Hard-coded paths / constants that must change for a new site, MRU or timezone

Grep basis: `omar.syed|MRU91|hours=-7|Los_Angeles|2026-08|707ccda9|9b22a989|214a1a56|e65bd4f9|33.748|115.339|arcgisonline|plotly.min.js|8899|10.191|GEOID|-31.4|-29.4|mav14550|mav14551|ffmpeg` over the pipeline modules.

### Timezone — FIXED 2026-09-15 (one campaign `tz`, DST-aware)
- `engagement_tab.TZ` (zoneinfo, `set_tz(name)`, `PDT` kept as an alias) drives `ET.epoch_pdt()` (tz-aware, accepts `HH:MM[:SS[.f]]`),
  `hms_local()`, `tz_abbr(t)` and every clock label in the engagement tab, the overview PNG axis, the IR frame strip and the
  radar rollup (`postprocess_report` uses `ET.TZ`/`ET.hms_local`/`ET.tz_abbr` everywhere; the two fixed `timedelta(hours=-7)` in
  `_rr_steal_gif` / `_rr_turn_zoom_fig` are gone).
- `tracking_tab.TZ` (`set_tz`, `init_mission(tz=…)`) with `TZ_ABBR` refreshed from the first flight window → every "time (PDT)"
  axis title / table header becomes "time (<abbr>)".
- `quicklook_report.py --tz` (default: the dump's `meta.json` `tz`, else LA) writes `"tz"` into the generated config;
  `dump_run_window.py --tz` names the day folder and the `time_local` column.
- Verified: every 8/25–8/28 clock string resolves to the same epoch as the old fixed −7 h converter (August = PDT); a
  December date differs by exactly 3600 s (PST) as it should.

### Site / antenna / geoid
- `corr_lib.GEOID_N = -31.4` (HAE−MSL, Seawall SoCal) → `mavlink_alt_hae_m()` used by `ih.archive.save_archive` when writing `U_m_hae` and by `seawall_archiver`. A new site with a different undulation gets a wrong truth altitude in every dump.
- `tracking_tab.DEFAULT_GEOID_N = -31.4`, `DEFAULT_ANT = (33.7480633, -115.3392025, 139.88)` (MRU91 8/26) — overridable via `init`, but the defaults silently apply when `init` is omitted.
- `engagement_tab._fov_render GEOID_N = -29.4` (display-only MSL axis in the FOV video).
- `seawall_archiver.FALLBACK_ANT = (33.74806, -115.33920, 139.88)`, `HOST = 10.191.28.205`, `RANGE_T0 = 2026-08-25`, `OUT_ROOT` (env `SEAWALL_OUT_ROOT`), `TIMELINE`/`RUN_CONTEXT` dicts — the archiver is the old full-run dumper (queries the UNINDEXED `time_spec_float`); prefer `quickdump.py`.
- `toxic_zones.path_traces` hover text `"MRU91 antenna"`; `build_html` sub-title "MRU91 · target = mav14550" (only in toxic_zones' own page, not the rollup).

### MRU / host / run ids
- `quickdump.DEFAULT_HOST = "10.191.28.205"`, `DEFAULT_RUN = "run_e65bd4f92a9a4d6aa883c5f9d896a6ca"` — pass `--mru NN` (→ `10.1NN.28.205`, `ih.feed.MRU_HOST_FMT`) or `--host`, and always `--run`.
- `ih.data.ARCHIVE_ROOT` default `/home/omar.syed/Test_Environment/ironhide/seawall/2026-09-14_week` (env `IH_ARCHIVE_ROOT`; quickdump `--root`).
- `postprocess_report.ensure_dump()` passes none of `--host/--run/--day/--root` → the `mongo` day input always dumps MRU91/e65bd4f9/today.
- `tracking_tab.init_mission` legacy branch: run 9b22a989 track ids and 08:35–09:18 windows; `DEFAULT_TITLE/SUB` "MRU91 Mission Report 2026-08-26"; Summary `<h2>` "Mission rollup — 2026-08-26, MRU91 run Turquoise_Emu, drone mav14550_1_1" (unconditional); default `notes_html` = the 8/26 findings.
- `toxic_zones.DAYS` / `OUTDIR` (8/25, 8/28 MRU91 dirs under `VP_TrackAnalysis/mru91_track2895/toxic_zones_0828`) — only for its own CLI, not used by the pipeline.

### Target naming — FIXED 2026-09-15
- Campaign `target_pattern` / `interceptor_pattern` (+ per-day / per-engagement / per-rollup-day overrides) reach every loader:
  `postprocess_report._pat()`, `toxic_zones.pick_truth_csvs(day_dir, target_pattern, interceptor_pattern)` (LARGEST match),
  steal/corruption views, `turn_zoom`. Without them the legacy globs apply (`mav14550_1_1.csv` / `mav14551_2_*.csv`; rollup
  `mav14550*.csv` / `mav14551*.csv`), so the 8/24 config reproduces.
- Legend ids come from the truth files actually loaded (`ET.truth_ids` + `ET.id_label`): `mav14551_2_0/2_1/2_34` → "14551",
  `mavlink_1_2` → "mavlink_1_2". `toxic_zones` page text no longer mentions MRU91 / mav14550 (`--site`, `--target`, `--interceptor`
  for its own CLI; the antenna hover reads "radar antenna (ENU origin)").

### Filesystem / tools
- `tracking_tab.py` / `engagement_tab.py` import siblings by their own directory (no absolute `track_correlation` path any more).
- ffmpeg: `engagement_tab.ffmpeg_bin()` = `$SEAWALL_FFMPEG` → `PATH` → `<interpreter dir>/ffmpeg` (sensorenv) → the legacy absolute path.
  Checked BEFORE any render; none found → the FOV block alone is replaced by an "unavailable" note (tab builds). A cached
  `figs/fov_<video>.mp4` never needs ffmpeg.
- `quickdump.py` sys.path: `<TE>/Seawall_Ironhide_Testing/ironhide_dashboard`, `<TE>/ironhide_dashboard`, `<TE>/chaos-spa/src` — today `ih` resolves to `/home/omar.syed/Test_Environment/ironhide/dashboard/ih/` (a sibling copy of `ironhide_dashboard_served/ih/`; `archive.py` and `data.py` are byte-identical in both).
- `toxic_zones.PLOTLY_JS = track_correlation/plotly.min.js` (own CLI only). The pipeline pages inline plotly via `pyo.get_plotlyjs()` (`engagement_tab.wrap_tabs`) — ~4.6 MB per page, no external file needed. (`tracking_tab.wrap_page` references `plotly.min.js` relatively but is NOT used by the pipeline.)
- Satellite imagery: `live_correlator._satmap_payload` → `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}`, needs outbound HTTPS; falls back to a plain dark background.
- Serving: `python3 -m http.server 8899 --bind 0.0.0.0` has been running in `/home/omar.syed/Test_Environment/ironhide/seawall/reports` since Aug 25 (pid 3002821). `server_base` in the 8/24 config is `http://172.18.1.28:8899/seawall_week_report` = that root + the `out_root` basename. A new campaign's `out_root` must be a sub-directory of that served root (or a symlink into it — `Seawall_Week_of_9-14/reports/` is meant to hold such symlinks).

### Physics/gating constants (constants, but know they exist)
- tracking: `AGL_MIN = 20 m` truth clamp, `max_horiz = 350 m` coast mask, seeker standoffs `600/450/300/150 m`, 12° FOV, obs match ±0.06 rad az / 300 m range / 3° el.
- engagement: see §4 list.
- radar rollup / toxic_zones: `MEDIAN_GATE_M 150`, `COAST_GAP_S 1.2`, `COVER_WIN_S 0.75`, `MOVING_MPS 2.5`, turning = heading rate > 6°/s (±3 s), far-turn ≥ 2500 m, worst-cell NMS 300 m, `TELEPORT_MPS 250`, `RANGE_GATE_M 20 km`.
- quickdump: `MAX_TRACK_RANGE_M 15 km`, `CHUNK_S 120`, socket timeout 120 s.

---

## 8. Things that would BREAK (or silently mislead) on a new MRU / site / timezone — status 2026-09-15

1. **`mdir` without `prep`** = the legacy 8/26 mission table (only `mission_20260826`). Unchanged by design (served 8/26 report). New campaigns: use `prep`. `radar_rollup.az_bias_example` now takes `"generic": true` for a prep-generated dir.
2. ~~Tracking day needs exactly two flight windows~~ **FIXED** — 1..N `flights_pdt` rows, `F<n>R<lap>` keys.
3. ~~`notes_html: ""` prints the 8/26 findings; Summary `<h2>` hard-coded~~ **FIXED** on prep days (placeholder + computed header / "truth range a–b km" lines). Legacy `mdir` path unchanged.
4. ~~Fixed −7 h converters~~ **FIXED** — campaign `tz` (§7).
5. **`GEOID_N`** is a site constant in three places: the dumper (`dump_run_window.py --geoid-n`, campaign `geoid_n` for `mongo` inputs), `init.geoid_n` on tracking days, and `corr_lib.GEOID_N` (−31.4) for the OLD quickdump/archiver writers. Set all of them for a new site; the pipeline cannot detect a mismatch.
6. ~~`mongo` day input broken~~ **FIXED** — `dump_run_window.py` with the full spec (§2), dir parsed from the dumper output, valid on days / prep / engagements / rollup days / views.
7. ~~Truth N compressed 0.36 % on prep days~~ **FIXED** — `prep_from_dump` stores lat/lon, `init_mission` rebuilds E/N/U with `corr_lib.EnuFrame`. Effect on 8/25: global az bias −2.17° → −2.12°, lap medians change by ≤ 3 m.
8. `speed_mps` missing in old quickdump CSVs — `prep_from_dump` derives `hypot(vel_n, vel_e)`; `dump_run_window.py` writes the column. Fine either way.
9. `meta.json` key variants — every reader accepts `antenna_origin_lat_lon_haeM | antenna_origin | antenna` (incl. `toxic_zones.satmap`).
10. Both block-143 layouts — handled by `dump_run_window.py` / `ih.archive`; the CSV layout is identical.
11. ~~Target not on port 14550 / interceptor not on 14551~~ **FIXED** — campaign patterns reach the rollup too. Verified with `mavlink_1_2*` (MRU43) and `mav*_1_*` (synthetic site).
12. `server_base` link rewrite only for `YYYY-MM-DD` day ids (two days on one date need distinct ids, e.g. `2026-10-01-eng` — its standalone link then stays relative).
13. ~~ffmpeg missing → whole tab stubbed~~ **FIXED** — only the FOV block is replaced; run under sensorenv for the real player.
14. Esri tiles blocked → plain background + a caption in the heat-map section (cosmetic).
15. Antenna omitted → meta.json antenna with `[WARN]`; nothing available → MRU91 default with a loud `[WARN]` (never silent).
16. Still hard-coded: `toxic_zones.DAYS/OUTDIR` (its own CLI defaults only), `engagement_tab._fov_render` display-only `GEOID_N = -29.4` (MSL axis label in the FOV video), `tracking_tab.DEFAULT_TITLE/SUB` (its own CLI), the 8/26 legacy mission table.
