# Campaign JSON — complete schema for `postprocess_report.py`

Reference for the config consumed by
`/home/omar.syed/Test_Environment/track_correlation/postprocess_report.py`
(an identical copy lives in `../scripts/postprocess_report.py`). Derived by reading the
code and the real campaign file `track_correlation/seawall_week_0824.json` (the one that
produced the Seawall Week of 8-24 report). Every key below is quoted with the exact
code site that reads it. "Campaign-specific" means the analyst must set it per campaign;
"constant" means the value is a pipeline convention that should be copied verbatim.

Invocation: `python postprocess_report.py <campaign.json>` — the script has NO argparse,
no `--help`, no `--day`/`--only` flags; `sys.argv[1]` is opened with `json.load`.
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
| `dump` | abs path | cond. | — | `ensure_dump(day)` (engagement: per-engagement `dump` wins; tracking: `prep.dump` or `day.dump`) | archive dir: `<…>/<date>/<run8>[_label]/` containing `mavlink/`, `tracks/`, `meta.json` |
| `mongo` | `{host, run, t0_pdt, t1_pdt}` | cond. | — | `ensure_dump()` shells out to `quickdump.py <t0_pdt> <t1_pdt> <dump_out>` | **BROKEN for anything but the 8/28 defaults** — see §8 item "mongo day input". Always pre-dump with quickdump and use `dump`. |
| `dump_out` | str | no | `f"{mongo.run}_quickdump"` | `ensure_dump()` | only with `mongo`; same caveat |

---

## 3. `days[]` with `type: "tracking"` (module `tracking_tab.py`)

Builds a Summary tab + one tab per `flight_keys` entry. Data path: either an existing
mission dir (`mdir`) or `prep` (synthesize the mission dir from a dump).

| key | type | req | default | consumed by | notes |
|---|---|---|---|---|---|
| `mdir` | abs path | one of `mdir`/`prep` | — | `tracking_day_tabs()`: `TT.init_mission(mdir, spec=None, …)` | **`spec=None` selects the LEGACY 8/26 mission structure** (`tracking_tab.init_mission`, lines 137-155: hard-coded FLIGHTS/RUNS/INBOUND/track-id lists for MRU91 run 9b22a989). Only valid for `VP_TrackAnalysis/mru91_track2895/mission_20260826`. A new campaign MUST use `prep`. |
| `prep` | object | one of | — | `tracking_day_tabs()` → `TT.prep_from_dump(dump, out_mdir, target_pattern, flights, ant_hae_m)` then `init_mission(..., spec={"gate_m": …})` (GENERIC structure) | |
| `prep.dump` | abs path | yes (or `day.dump`) | — | `prep_from_dump()` reads `<dump>/mavlink/<target_pattern>` (largest match) and `<dump>/tracks/track_*.csv` | |
| `prep.target_pattern` | glob | yes | — | `prep_from_dump()`: `max(glob(f"{dump}/mavlink/{pattern}"), key=size)` | e.g. `"mav14550_1_1.csv"`; the exact `target_id` naming differs per day (8/25 was `mav14551_2_2.csv`) — `ls <dump>/mavlink/` first |
| `prep.ant_hae_m` | float (m HAE) | yes | — | `prep_from_dump()`: truthv U column = `alt_ft_wire - ant_hae_m` (feet minus metres, deliberately: `init_mission` inverts it with `(U + ANT_HAE)*0.3048 + GEOID_N`) | MUST equal `init.ant[2]`. Read from the dump's `meta.json` (`antenna[2]` or `antenna_origin_lat_lon_haeM[2]`). |
| `prep.flights_pdt` | list of `[date, hms, hms]` | yes | — | `tracking_day_tabs()`: `ET.epoch_pdt(d, a), ET.epoch_pdt(d, b)` (fixed UTC−7) | **Exactly TWO entries.** `init_mission` loads only `fast2_F1.npz` and `fast2_F2.npz` (line 123) and `build_summary_tab` iterates `for fl in (1, 2)` (line 1212) — one flight → `KeyError: 2`; three → F3 silently has no tracks. |
| `prep.out_mdir` | abs path | no | `<day_dir>/mission_inputs` | `prep_from_dump()` writes `truthv.npz`, `fast2_F1.npz`, `fast2_F2.npz`, `analysis_stage1.json` (rewritten every run — no cache) | |
| `prep.gate_m` | float | no | `350.0` | `spec["gate_m"]` → `init_mission`: tracks whose median distance to truth < gate are FLIGHT_TRACKS (top 10 by samples); PLOT_TRACKS = those with med < 200 m and ≥ 20 samples (max 6) | constant unless truth is very biased (8/25 pre-cal) |
| `init` | object | no (but effectively required) | `{}` | `tracking_day_tabs()` passes `day`, `ant`, `geoid_n` through to `TT.init_mission` | omit `ant` → `DEFAULT_ANT` = MRU91 8/26 antenna (33.7480633, −115.3392025, 139.88) |
| `init.day` | `[Y, M, D]` | no | `(2026, 8, 26)` | `TT.DAY` — only used by `hhmm()` in the legacy branch; harmless for GENERIC but set it anyway | |
| `init.ant` | `[lat, lon, hae_m]` | yes in practice | MRU91 | `TT.ANT_LAT/LON/HAE` — truth E/N/U reconstruction and AGL clamp | per-deployment (from `meta.json`) |
| `init.geoid_n` | float m | no | `-31.4` (`DEFAULT_GEOID_N`, Seawall site) | `TT.GEOID_N`: truth alt ft-MSL → m-HAE | **site constant** — a new site needs its own HAE−MSL undulation |
| `notes_html` | HTML | no | `""` | `TT.build_summary_tab(notes_html=…)` | **`""` falls back to the ORIGINAL 8/26 "Key findings" narrative (hard-coded text about tracks 94/917, −1.45°, …)**. Always author non-empty text. |
| `flight_keys` | list of `"F<n>R<m>"` | no | `[]` | `tracking_day_tabs()`: label `Flight n · Lap m`; `TT.build_flight_tab(k)` → `RUNS[(n, m)]` | With `prep` (no `spec.runs`), RUNS = `{(i,1)}` → valid keys are **`F1R1` and `F2R1` only**; `F1R2` raises `KeyError` → pane replaced by "flight pane …: not available (…)" (build continues). |

What `init_mission` prints (success): `global az bias +X.XX deg; RAW vertical residual (track-truth) ±NN m (NOT removed; DU=0)` and
`pad truth-U NNN m; altitude clamp >20 m AGL: truth A -> B pts`. The empty-match guard prints
`init_mission: NO matched tracks this mission — bias/datum set to 0` (wrong `target_pattern`, wrong `ant`, or no target-side tracks).

Hard-coded text inside the tracking Summary tab you cannot override from config
(`tracking_tab.build_summary_tab`, lines 1236-1262): the `<h2>` reads
**"Mission rollup — 2026-08-26, MRU91 run Turquoise_Emu, drone mav14550_1_1"**, followed by
"Two flights, each flying two out-and-back racetrack loops to ~3.9 km … Flight 1: 08:35:24–08:52:21 … Flight 2: 08:58:56–09:18:44",
regardless of day. `build_flight_tab` also prints "outbound to ~3.9 km and back" in every window line. A new
campaign must edit these strings (or accept the wrong header).

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
| `engagements[].dump` | abs path | yes (else `ensure_dump(day)`) | — | `load_dump_truth` / `load_dump_track13` | may differ per engagement (8/28 used three quickdumps) |
| `engagements[].interceptor_pattern` | glob | no | `"mav14551_2_*.csv"` | `ET.load_dump_truth(dump, pattern, t0, t1)` merged across matches, `drop_frozen` applied | MAVLink alias naming: `mav<port>_<sysid>_<compid>`; the interceptor publishes several compids (2_0, 2_1, 2_34) → keep the wildcard |
| `engagements[].target` | `{"pattern": glob}` **or** `{"traj_csv": path}` | yes | — | `pattern` → `load_dump_truth(dump, pattern, t0, t1)`; `traj_csv` → `load_traj_csv_truth(csv, ant)` (columns `utc_s, lat, lon, alt_hae_m` — onboard-GPS export, used 8/27 eng 2 when the MAVLink truth froze) | |
| `engagements[].window_pdt` | `[hms, hms]` | yes | — | `t0, t1` for loading truth (padded ±60 s inside `load_dump_truth`) and the Overview figure span | the whole flight/engagement, not just the pass |
| `engagements[].cpa_seed_pdt` | `hms` | yes | — | `cfg["cpa_seed"]`; `build_engagement_tab` refines the true CPA with `E.cpa` within ±20 s of the seed; track CPA searched ±20 s then ±120 s (late-born track) | from the manifest's `passes[].t_pdt` |
| `engagements[].track_id` | int | yes | — | `load_dump_track13(dump, tid)` → `<dump>/tracks/track_<tid>.csv` (13 cols) | `FileNotFoundError` if not in the dump window |
| `engagements[].gif` | rel path | no | `f"figs/eng{i}_3d.gif"` | `E.gif3d(..., os.path.join(day_dir, gif))`; embedded as `<img class="gif">` | keep the `figs/` prefix (standalone inliner only matches `src="figs/…"`) |
| `engagements[].summary_html` | HTML w/ `str.format` fields | no | `"<p>Interceptor closest approach: <b>{cpa_truth:.0f} m …</b>, <b>{cpa_track:.0f} m to radar track {track_id}</b>.</p>"` | `.format(cpa_truth=, cpa_track=, track_id=)` | only these three fields; any other `{…}` → `KeyError`; literal braces must be `{{ }}` |
| `engagements[].post_html` | HTML | no | `""` | appended after the tab (also after the failure stub) | narrative |
| `engagements[].tab_label` | str | no | `f"Engagement {i}"` | tab button text | |
| `engagements[].pre`, `.post` | float s | no | 18.0 / 5.0 | copied into `cfg` but **NOT read** by `build_engagement_tab` — the CPA plot window is the BINDING standard `t−10 … t+5` (line 219) | ignore / omit |
| `engagements[].fov` | object | no | absent → no FOV player | `build_fov_section(fov, outdir, tc, target, ant, trkV, name)`; returns `""` with a console `[fov] …skipped` note when ulog/video missing | |
| `fov.ulog` | path or glob | yes | — | `_ulog_series()`: `glob`, largest file wins; pyulog topics `vehicle_status, vehicle_gps_position, vehicle_local_position, vehicle_attitude` | interceptor (Zeus) PX4 log |
| `fov.zoff` | float s | yes | — | added to the ulog UTC anchor — the per-log flight-controller clock offset (8/27: 36002.0 = FC 10 h 00 m 02 s behind UTC; 8/28: 36001.3–36001.8) | **campaign-specific, measured by position xcorr vs truth** |
| `fov.video` | path | yes | — | IR mkv, decoded strictly sequentially (`cv2`; broken indexes, never seek) | |
| `fov.camwarp` | npz path | one of camwarp/cam | — | `np.load(cw)["tcorr"]` = per-frame true capture times | 8/27 style |
| `fov.cam` | `{"fps": f, "onset_frame": n}` or `{"fps": f, "utc0": epoch}` | one of | — | frame-0 UTC = ulog takeoff − onset_frame/fps (takeoff = first sustained vz < −1 m/s ≥ 2 s) | 8/28 style |
| `fov.target_label` | str | no | `"target truth"` | legend text | |

Constants baked into the engagement tab (not configurable): `MIN_PTS = 4` interceptor samples;
CPA window −10/+5 s; error-window ±12 s; overview pass gate `< 75 m`, both craft ≥ 3 m/s, closing ≥ 8 m/s, ≥ 20 s apart;
FOV cone 12° full / 500 m, 5 Hz grid; PX4 `nav_state == 14` = OFFBOARD; legend names
"interceptor 14551 (truth)" / "target 14550 — TRUTH/TRACK" (`build_engagement_tab` lines 240-243).

Console on success per engagement: `[<name>] tc=<epoch> inter=N targ=N trk<id>=N cpa_truth=NNm cpa_trk=NNm`.
Failures are caught in `engagement_day_tabs()` and print `  <name> skipped: <error>`; the tab is replaced by
"not buildable from archived data (<error>)" and the build continues.

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
| `days[].dirs` | list of abs paths | yes if scoreable | — | `_rr_day_samples(dirs)`: `TZ.pick_truth_csvs(d)` (target = biggest `mavlink/mav14550*.csv`), `TZ.load_target_tracks` (median < 150 m); `meta.json` of `dirs[0]` read for the run label (`run_id8` or `run`, `friendly_name`) and the satellite tile antenna (`antenna_origin_lat_lon_haeM` / `antenna_origin` / `antenna`) | several dumps of one day are merged |
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
| `steal_views[].interceptor_pattern` / `.target_pattern` | glob | no | `"mav14551_2_*.csv"` / `"mav14550_1_1.csv"` | truth loaders | |
| `corruption_views` | list | no | `[]` | same schema/loader as `steal_views` (set `banner`/`rel_label` to `"CPA"`) **plus** `ET.E.err_window_fig(trkV, target, t, ±12 s)` az/el/range/alt error panels | |
| `az_bias_example` | object | no | absent → no example map | `TT.init_mission(ex["mdir"], spec=None, **init)` then `TT.maps_fig(flight, RUNS[(flight, run)], color_of(flight))` — RAW vs bias-rotated maps | **`spec=None` → legacy 8/26 structure again**: only works with `mission_20260826`. For a new campaign, either point `mdir` at a `prep`-generated `mission_inputs` dir AND accept that `RUNS` is the legacy 8/26 table (it will KeyError / mis-window) — i.e. omit this block, or change the code to pass a spec. |
| `az_bias_example.mdir`, `.init{day,ant,geoid_n}`, `.flight` (default 1), `.run` (default 2), `.caption_html` | | | | | |
| `turn_zoom` | object | no | absent → no figure | `_rr_turn_zoom_fig(tz, root)` writes `figs/turn_zoom.png` (rewritten every run) | |
| `turn_zoom.dump`, `.date`, `.t_pdt`, `.tracks` (list: `[dying_track, successor]` — the FIRST id is checked for death within +3 s), `.win_s` (45), `.label`, `.cap`, `.target_pattern` | | | | | |

Console per scoreable day: `  [radar] <label> (<run8> (<friendly>)): az bias +X.XX° · active NN.N% · coast turns NN.N% vs straight NN.N% (N.Nx)`.
Satellite fetch failure prints `  [radar] satellite tiles unavailable — plain background` (offline / Esri blocked) and continues.

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

### Timezone — three different "PDT"s (WILL break Nov–Mar or outside Pacific)
- `engagement_tab.PDT = timezone(timedelta(hours=-7))` (line 1008) → `ET.epoch_pdt()` is used by `postprocess_report` for **every** clock string in the config, tracking `flights_pdt` included. Also `state_table` (line 61), `build_overview_section` (131, 203), `_rr_steal_gif` (321), `_rr_turn_zoom_fig` (465), `build_radar_rollup` (524).
- `quickdump.PDT = timezone(timedelta(hours=-7), "PDT")` and `ih.data.PDT` likewise → dump windows and `time_pdt` columns.
- `tracking_tab.PDT = ZoneInfo("America/Los_Angeles")` (DST-aware) → tick labels / `ps()` on tracking pages; `corr_lib.LOCAL_TZ` likewise.
- Consequence: during PST (after the first Sunday of November) the fixed −7 h converters are 1 h off UTC while the tracking-page labels are right → windows land one hour early relative to labels. For a unit in another region every "PDT" label is wrong. No config knob; edit the constants.

### Site / antenna / geoid
- `corr_lib.GEOID_N = -31.4` (HAE−MSL, Seawall SoCal) → `mavlink_alt_hae_m()` used by `ih.archive.save_archive` when writing `U_m_hae` and by `seawall_archiver`. A new site with a different undulation gets a wrong truth altitude in every dump.
- `tracking_tab.DEFAULT_GEOID_N = -31.4`, `DEFAULT_ANT = (33.7480633, -115.3392025, 139.88)` (MRU91 8/26) — overridable via `init`, but the defaults silently apply when `init` is omitted.
- `engagement_tab._fov_render GEOID_N = -29.4` (display-only MSL axis in the FOV video).
- `seawall_archiver.FALLBACK_ANT = (33.74806, -115.33920, 139.88)`, `HOST = 10.191.28.205`, `RANGE_T0 = 2026-08-25`, `OUT_ROOT` (env `SEAWALL_OUT_ROOT`), `TIMELINE`/`RUN_CONTEXT` dicts — the archiver is the old full-run dumper (queries the UNINDEXED `time_spec_float`); prefer `quickdump.py`.
- `toxic_zones.path_traces` hover text `"MRU91 antenna"`; `build_html` sub-title "MRU91 · target = mav14550" (only in toxic_zones' own page, not the rollup).

### MRU / host / run ids
- `quickdump.DEFAULT_HOST = "10.191.28.205"`, `DEFAULT_RUN = "run_e65bd4f92a9a4d6aa883c5f9d896a6ca"` — pass `--mru NN` (→ `10.1NN.28.205`, `ih.feed.MRU_HOST_FMT`) or `--host`, and always `--run`.
- `ih.data.ARCHIVE_ROOT` default `/home/omar.syed/Test_Environment/Seawall_Ironhide_Testing/Seawall_Week_of_9-14` (env `IH_ARCHIVE_ROOT`; quickdump `--root`).
- `postprocess_report.ensure_dump()` passes none of `--host/--run/--day/--root` → the `mongo` day input always dumps MRU91/e65bd4f9/today.
- `tracking_tab.init_mission` legacy branch: run 9b22a989 track ids and 08:35–09:18 windows; `DEFAULT_TITLE/SUB` "MRU91 Mission Report 2026-08-26"; Summary `<h2>` "Mission rollup — 2026-08-26, MRU91 run Turquoise_Emu, drone mav14550_1_1" (unconditional); default `notes_html` = the 8/26 findings.
- `toxic_zones.DAYS` / `OUTDIR` (8/25, 8/28 MRU91 dirs under `VP_TrackAnalysis/mru91_track2895/toxic_zones_0828`) — only for its own CLI, not used by the pipeline.

### Target naming
- Default globs `mav14551_2_*.csv` (interceptor) and `mav14550_1_1.csv` (target) in `postprocess_report` (lines 91, 297-299, 420-422, 463, 644); `toxic_zones.pick_truth_csvs` hard-codes `mav14550*.csv` / `mav14551*.csv` (used by the radar rollup — a campaign whose target is not on MAVLink port 14550 gets `ValueError: max() arg is an empty sequence`).
- Engagement legend names "interceptor 14551" / "target 14550".

### Filesystem / tools
- `tracking_tab.py` line 38: `sys.path.insert(0, "/home/omar.syed/Test_Environment/track_correlation")` (absolute).
- `engagement_tab._fov_render`: ffmpeg fallback `/home/omar.syed/.local/share/mamba/envs/sensorenv/bin/ffmpeg` (after `shutil.which`).
- `quickdump.py` sys.path: `<TE>/Seawall_Ironhide_Testing/ironhide_dashboard`, `<TE>/ironhide_dashboard`, `<TE>/chaos-spa/src` — today `ih` resolves to `/home/omar.syed/Test_Environment/ironhide_dashboard/ih/` (a sibling copy of `ironhide_dashboard_served/ih/`; `archive.py` and `data.py` are byte-identical in both).
- `toxic_zones.PLOTLY_JS = track_correlation/plotly.min.js` (own CLI only). The pipeline pages inline plotly via `pyo.get_plotlyjs()` (`engagement_tab.wrap_tabs`) — ~4.6 MB per page, no external file needed. (`tracking_tab.wrap_page` references `plotly.min.js` relatively but is NOT used by the pipeline.)
- Satellite imagery: `live_correlator._satmap_payload` → `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}`, needs outbound HTTPS; falls back to a plain dark background.
- Serving: `python3 -m http.server 8899 --bind 0.0.0.0` has been running in `/home/omar.syed/Test_Environment/VP_TrackAnalysis/mru91_track2895` since Aug 25 (pid 3002821). `server_base` in the 8/24 config is `http://172.18.1.28:8899/seawall_week_report` = that root + the `out_root` basename. A new campaign's `out_root` must be a sub-directory of that served root (or a symlink into it — `Seawall_Week_of_9-14/reports/` is meant to hold such symlinks).

### Physics/gating constants (constants, but know they exist)
- tracking: `AGL_MIN = 20 m` truth clamp, `max_horiz = 350 m` coast mask, seeker standoffs `600/450/300/150 m`, 12° FOV, obs match ±0.06 rad az / 300 m range / 3° el.
- engagement: see §4 list.
- radar rollup / toxic_zones: `MEDIAN_GATE_M 150`, `COAST_GAP_S 1.2`, `COVER_WIN_S 0.75`, `MOVING_MPS 2.5`, turning = heading rate > 6°/s (±3 s), far-turn ≥ 2500 m, worst-cell NMS 300 m, `TELEPORT_MPS 250`, `RANGE_GATE_M 20 km`.
- quickdump: `MAX_TRACK_RANGE_M 15 km`, `CHUNK_S 120`, socket timeout 120 s.

---

## 8. Things that would BREAK (or silently mislead) on a new MRU / site / timezone

1. **`mdir` without `prep`** and **`radar_rollup.az_bias_example`** both call `init_mission(..., spec=None)` = the legacy 8/26 mission table. Only `mission_20260826` satisfies it. New campaigns: use `prep`; omit `az_bias_example` (or patch the code to pass a spec).
2. **Tracking day needs exactly two flight windows** and keys `F1R1`/`F2R1` (see §3).
3. **`notes_html: ""` prints the 8/26 findings**; the Summary `<h2>` is hard-coded 8/26 text either way.
4. **Fixed −7 h converters** vs DST-aware labels (see §7).
5. **`GEOID_N = -31.4`** baked into the dump writer (`corr_lib`) — another site's truth altitude is off by the undulation difference (tens of metres possible).
6. **`mongo` day input** dumps the wrong unit/run/day and returns a path that does not exist (quickdump writes under `<ARCHIVE_ROOT>/<day>/<name>/`, `ensure_dump` returns the bare `name`). Pre-dump instead.
7. **Truth N scale in `prep` tracking days**: `prep_from_dump` copies the dump's exact-ENU `E_m/N_m`, but `init_mission` inverts them with an equirectangular 111 320 m/deg (designed for the original extraction's equirect E/N) before `pymap3d.geodetic2enu` → truth N compressed ≈ 0.36 % (≈ 14 m north at 4 km), i.e. a small artificial range-scale bias in tracking-day errors. Not present on engagement days (they use the dump ENU directly).
8. **`speed_mps` missing in quickdump/`save_archive` mavlink CSVs** (columns are `…,U_m_hae,vel_n_mps,vel_e_mps,…`; the old `seawall_archiver` layout had `speed_mps`). `toxic_zones.TruthInterp` falls back to `hypot(vel_n, vel_e)` (rollup OK). `prep_from_dump` fills zeros → `init_mission` treats every pre-flight sample as parked (PAD_U still OK) but the "moving truth" gate `m[:, 15] > 4` in `seeker_quad`/`perp_fig` drops every sample → **seeker panels empty / pAcq missing** for tracking days built from quickdumps. Use an archiver-layout dump or add a `speed_mps` column before `prep`.
9. **`meta.json` key variants**: archiver dumps carry `run` (`run_<hex>`), `run_id8`, `friendly_name`, `antenna_origin_lat_lon_haeM`, `geoid_n_m`; quickdump / `save_archive` dumps carry `run` (8-hex), `antenna`, `label`, `window`, `mavlink_ids`, `layouts`. `_rr_satmap` accepts `antenna_origin_lat_lon_haeM | antenna_origin | antenna`; the run label falls back to `meta["run"]`. `toxic_zones.satmap` (own CLI only) accepts only the archiver key.
10. **Both block-143 layouts**: `save_archive` decodes 2026.3.x NED `x_state`/`p_cov` AND 2026.9.x `x_state_ecef`/`p_cov_ecef` + LLA (`ih.feed.track_rows`), so the CSV layout is identical either way; `seawall_archiver.pull_tracks` handles NED only (returns nothing on a 2026.9.x unit).
11. **Target not on port 14550 / interceptor not on 14551**: set every `*_pattern` explicitly AND accept that `radar_rollup` (via `toxic_zones.pick_truth_csvs`) cannot be pointed elsewhere without a code change.
12. **`server_base` link rewrite only for `YYYY-MM-DD` day ids**.
13. **ffmpeg missing** → `_fov_render` raises → the whole engagement tab becomes the "not buildable" stub (not just the FOV block). Run under `micromamba run -n sensorenv` (ffmpeg is in the env's `bin/`).
14. **Esri tiles blocked** → maps on plain background (cosmetic).
