# data/ — range data lives here (NOT in git)

Everything under `data/` except this file is git-ignored. The dashboard runs without any of it
(live mode needs only a reachable MRU Mongo); this folder feeds the **archive replay**.

Two roots, both overridable by environment variable (read once at import time):

| env var             | default                              | what                                                                              |
|---------------------|--------------------------------------|-----------------------------------------------------------------------------------|
| `IH_QUICKDUMP_DIR`  | `data/2026-08-28`                    | the built-in 8/28 Seawall day: `flights.json` + `<run8>_<flight>_quickdump/` dirs |
| `IH_ARCHIVE_ROOT`   | `data/archives`                      | flights saved from a live run ("Save data to archive"), `<root>/<YYYY-MM-DD>/…`   |
| `IH_OBS_NPZ`        | `<IH_QUICKDUMP_DIR>/ql_f1_full.npz`  | optional raw-obs array for built-in flight 1 (key `obs` = t, az_rad, el_rad, rng_m) |

## Layout

```
data/
  2026-08-28/                        # IH_QUICKDUMP_DIR (the built-in day; flight numbers 1-4)
    flights.json                     # {"day": "2026-08-28", "runs": [{"run": "e65bd4f9", "flights": [{"n": 1, "label": "Flight 1",
                                     #   "t0": <epoch>, "t1": <epoch>, "segments": [...], "passes": [...]}, ...]}]}
    e65bd4f9_flight1_quickdump/      # one dir per dumped window (ih.data.FLIGHT_DIRS maps flight n -> dir)
      meta.json                      # {"antenna": [lat_deg, lon_deg, hae_m], ...}
      mavlink/mav14550_1_1.csv       # MAVLink truth per target_id: t_epoch, lat, lon, E_m, N_m, U_m_hae, vel_e_mps, vel_n_mps,
                                     #   vert_spd_wire_ftmin, validposition (ih.data.TRUTH_COLS)
      tracks/track_<id>.csv          # radar tracks: t_epoch, E_m, N_m, U_m, sigE_m, sigN_m, sigU_m, last_update_t,
                                     #   total_associations, track_state, vE_mps, vN_mps, vU_mps [, sigvE_mps, sigvN_mps, sigvU_mps,
                                     #   truth_match_id, truth_match_conf, contributors]
      obs/obs.csv                    # (newer dumps) raw detections: t, az_rad, el_rad, rng_m, rr_mps, bistatic range/rate, truth match
    e65bd4f9_flights23_quickdump/
    e65bd4f9_flight4_quickdump/
    ql_f1_full.npz                   # optional (IH_OBS_NPZ)
  archives/                          # IH_ARCHIVE_ROOT — written by the dashboard's "Save data to archive" or by quickdump.py
    2026-09-15/
      flights.json                   # same schema; each flight entry carries "dir": "<run8>_<label>" (relative to this day) and, after the
                                     #   airborne audit, "saved_t0"/"saved_t1" (raw window), "airborne_segments", "airborne_s" (t0/t1 = trimmed)
      <run8>_<label>/                # same layout as a quickdump dir (mavlink/ tracks/ obs/ meta.json)
```

`python quickdump.py HHMM HHMM <name> --run run_<hex> --mru NN --register` writes a window of a live run into the
archive root and registers it in `flights.json` — identical to the dashboard's Save card (both call
`ih.archive.save_archive`, which also runs the airborne audit; `python -m ih.archive audit --root … [--day …] [--dry-run]`
re-audits existing days).

The built-in `2026-08-28` day is the MRU91 Seawall intercept test; it is range data and is distributed separately.
