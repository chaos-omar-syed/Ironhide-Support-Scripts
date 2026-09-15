# Script inventory — what built the Seawall Week Report, and what survives in this skill

Survey of `track_correlation/`, `Seawall_Aug2026_Tools/`, `analysis_scripts/`,
`8_27_flightlogs/` and the served `VP_TrackAnalysis/mru91_track2895/` tree
(2026-09-15). Use it to know which tool answers which question and which old
scripts to ignore.

## The canonical chain (all in `scripts/`)

| Script | Role |
|---|---|
| `postprocess_report.py` + a campaign JSON | **The week-report builder.** X days → one report per day (tracking or engagement format from the two day modules, no other figure code) → `Rollup.html`, `Radar_Rollup.html`, standalone single-file exports, `index.html`. Config-driven; every narrative lives in the JSON. A day may point at a dump dir or a `mongo` spec (then it shells out to the dumper first). |
| `tracking_tab.py` | Tracking-day module: `init_mission(mdir, day, ant, geoid_n, spec)`, `prep_from_dump(dump_dir, out_mdir, target_pattern, flights, ant_hae_m)` synthesises the mission bundle from any dump, `build_flight_tab`, `build_summary_tab`, `maps_fig`, `timeline`. Byte-identical figures to the 8/26 mission report. |
| `engagement_tab.py` | Engagement-day module: `build_engagement_tab(cfg, outdir)`, `build_overview_section` (story figure, realistic pass gate), `build_fov_section` / `_fov_render` (seeker cone + IR player), dump loaders `load_dump_truth` (freeze scrub), `load_dump_track13`, `epoch_pdt`. CPA window t−10…t+5 s is hardcoded on purpose. |
| `eng_plots.py` | Engagement figures: `condition()` input hygiene (sort/dedupe, frozen runs, horizontal and vertical teleports), `cpa`, `gif3d`, `topdown`, `closing`, `guidance_fig`, `err_window_fig`, `dive_bottom`. |
| `report_figs.py` | Generic figure set for `post_correlate`: RAW + rotated maps, az/el stack with RMSE and containment, pos/vel, measurement rate, seeker basket. |
| `corr_lib.py` | Shared loaders and math: exact WGS-84 `EnuFrame`, feet-MSL → m HAE, `clean_truth`, `auto_correlate` (moving-truth gate), `az_bias_deg`, `err_stack`, `meas_times`, `prep_matched`, seeker geometry. Only site constant: `GEOID_N = −31.4`. |
| `spa_errors.py` | chaos-spa adapter returning spa's own error numbers (RMSE95, CONFIRMED+UPDATED only). Needs `chaos-spa/src` importable. |
| `post_correlate.py` | Generic CLI truth-vs-track report for one window on any MRU (mongo or npz). Most reusable single tool of the set. |
| `mission_report_builder.py` | Generic single-mission JSON-driven builder (same two day modules, no rollup). |
| `toxic_zones.py` | Where tracks go bad: coasting points, track deaths, coverage grid over Esri satellite; `TruthInterp`, `pick_truth_csvs`, `load_target_tracks` are reused by the rollup. Standard gates: target-side median < 150 m, ACTIVE = confirmed measurement-updated within ±0.75 s, moving ≥ 2.5 m/s, turning > 6°/s ±3 s. |
| `live_correlator.py` | Live dashboard (:8898) and the Esri `_satmap_payload` the heat maps import. |
| `aoa_bias_correct.py` | Az/el bias fit + correction (chaotic correlation live, geometry fallback offline). |
| `dump_run_window.py` | **New.** Standalone mongo → archive-layout dumper (replaces `quickdump.py`, which needs the dashboard's `ih` package). |
| `build_flight_manifest.py` | **New.** Dumps → `flights.json` + `DAY_SUMMARY.md` skeleton. Recreates the flight / lap / pass / side / coverage / steal metrics that were only ever computed by lost scratch scripts. |

## Superseded — do not use

| Old | Replaced by |
|---|---|
| `week_report.py` (hand-written single page) | `postprocess_report.py` + campaign JSON |
| `build_0828_tabs.py` | `report_0828.py` → now the engagement path of `postprocess_report.py` |
| `mission_report.py` (8/26 original, runs at import) | `tracking_tab.py` |
| `intercept_report_pipeline.py`, `today_report_build.py`, `cache_dump_0827.py` | engagement path of `postprocess_report.py`; cache dumps are now the archive layout |
| `quickdump.py` (needs `ironhide_dashboard/ih`, streamlit) | `dump_run_window.py` |
| `seawall_archiver.py` (MRU91 one-shot sweep, retries forever) | `dump_run_window.py` per window; keep the archiver's column contract |
| `analysis_scripts/corr_lib.py`, `mission_20260826/pipeline/corr_lib.py` | **stale equirectangular ENU (+0.36 % N at 33.75°N)** — never import |

## One-off investigation scripts (recipes, hardcoded to Aug 2026)

`report_0828.py`, `build_0828_report.py`, `stakeholder_0828.py` (steal anatomy
and concerns rollup — logic now in the manifest builder and rollup),
`ql_flight_build.py` / `ql_f1_err.py` (quicklook with state-coded top-downs),
`blindzone_map.py` (c·pw/2 rings from the modes jsonnet), `traj_mine.py`
(clean straight-leg trajectories for guidance back-testing), `alt_compare.py`
(feed altitude = PX4 MSL to ±2 m; site geoid from receivers −29.4 vs archiver
−31.4), `px4_vs_track.py` (frame audit: mapping, rotation, velocity, datum),
`alt_vel_sources.py`, `story_fig.py`, `replay_mac_0827.py` (re-run the real
MacTracker with and without the 100 m altitude floor), `build_spa_joint_0827.py`,
`spa_prepost_0827.py`, the `8_27_flightlogs/` ulog + IR set (`ulg_summary`,
`make_plots`, `dash_story`, `guidance_story`, `registration_check`,
`cam_warp_fit`, `cam_warp_dive_fix`), and the `fov/` set (`fov_prep*`,
`fov_render*`, `make_player28`; now folded into `engagement_tab.build_fov_section`).

Several of these read session scratchpad paths that no longer exist
(`obs_matched_0827.csv`, `/tmp/ql_f1_truth.npz`, `/tmp/chaotic_post15`).
Treat them as documentation of a method, not as runnable tools.

## Served folders and their builders

| `:8899/` folder | Builder |
|---|---|
| `seawall_week_report/` | `postprocess_report.py` + `seawall_week_0824.json` |
| `seawall_week_summary/` | hand-authored HTML, no script |
| `mission_20260826/` | `pipeline/fast_extract*.py → mission_analysis.py → mission_report.py` (now `tracking_tab`) |
| `intercept_0827/` | `today_report_build.py` (cache-first) |
| `flightdata_0827/` | the `8_27_flightlogs/` set |
| `flightdata_0828/` | `report_0828.py`, `build_0828_report.py`, `stakeholder_0828.py` |
| `mavlink_correlation/` (8/25) | orphan, no builder survives |
| `toxic_zones_0828/`, `toxic_zones_week/` | `toxic_zones.py` |
| `traj_mine/`, `alt_compare/`, `px4_vs_track/`, `blindzones_0828/`, `quicklook_f*_0828/`, `fov_0828/`, `spa_*` | the matching one-off script |

## Manifest readers

Every builder that consumes `flights.json` does `json.load(...)["runs"][0]["flights"]`
keyed by `f["n"]`. Only the 8/28 manifest carried `passes[].miss_m`, `steal_events`,
`*_track_spans` and a `method` block; earlier days drift (`median_horiz_err_m` vs
`med_horiz_err_m`, `t0_pdt` as HH:MM:SS on 8/25). `build_flight_manifest.py`
writes the full 8/28 schema for every day. `DAY_SUMMARY.md` is read by nobody;
it is the analyst's narrative companion.

## Archive CSV contract (from `seawall_archiver.py`, kept by the new dumper)

- `mavlink/<target_id>.csv`: `t_epoch,time_local,lat,lon,alt_ft_wire,E_m,N_m,U_m_hae,speed_mps,vel_n_mps,vel_e_mps,vert_spd_wire_ftmin,validposition` — `alt_ft_wire` is feet MSL, `vert_spd_wire_ftmin` is ft/min, `U_m_hae` is metres HAE about the run's antenna. Invalid-position rows are kept and flagged. Quickdumps may lack `speed_mps`; derive from `vel_n/vel_e`.
- `tracks/track_<id>.csv`: `t_epoch,time_local,E_m,N_m,U_m,vE_mps,vN_mps,vU_mps,sigE_m,sigN_m,sigU_m,total_associations,track_state,last_update_t[,truth_match_id,truth_match_conf,contributors,…]` — `track_state` 1 tentative / 2 confirmed; unchanged `last_update_t` = coasting.
- `obs/obs.csv`: raw detections with `truth_target_id` when the build stamps them.
- `meta.json`: antenna origin under `antenna_origin_lat_lon_haeM` (older dumps: `antenna`); per deployment, read it every time.
