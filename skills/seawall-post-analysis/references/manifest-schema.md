# `flights.json` — the day manifest

One file per test day, next to that day's dump directories. It is the single
machine-readable statement of what flew, when, and what the radar did about it.
Every report builder keys off `runs[0].flights[].n`; `DAY_SUMMARY.md` beside it
is the analyst's narrative and is read by nobody.

`scripts/build_flight_manifest.py` writes both. You edit `DAY_SUMMARY.md`
(narrative, notes on judgment calls) and, if needed, correct `flights.json`
(for example, demote a pass that the rule kept but the geometry shows was a
frozen-sample artefact) and record why in `issues`.

## Schema (the full 8/28 form, written for every day)

```
{
  "day": "2026-08-28",
  "runs": [ { "run": "e65bd4f9",                      run id, 8-hex prefix
              "flights": [ {
    "n": 1,                                         1-based flight number
    "t0", "t1":            epoch seconds
    "t0_pdt", "t1_pdt":    ISO-8601 with offset
    "drone_ids":           ["mav14550_1_1 (target)", "mav14551_2_* merged (interceptor)"]
    "airborne_minutes":    8.0
    "segments":  [ {"type": "loop"|"climbout/transit"|"engagement"|"return/land",
                    "n", "t0", "t1", "t0_pdt", "t1_pdt"} ]
    "passes":    [ {"n", "t_pdt": "07:21:43", "miss_m": 78, "horiz_m": 78,
                    "source": "truth-truth"|"ulog-truth"|"radar-only",
                    "realistic": true,              both ≥ 3 m/s and closing ≥ 8 m/s
                    "note": "..."} ]
    "tracking": {
       "target":                      "mav14550_1_1",
       "coverage_pct":                fresh target-side state while truth moves
       "med_horiz_err_m", "med_horiz_err_azcorr_m", "p90_horiz_err_m",
       "az_bias_deg", "el_bias_deg", "alt_bias_m",
       "meas_rate_hz", "n_measurements",
       "fragmentation_track_count",   distinct target-side ids
       "rider_track_ids", "rider_spans_pdt",
       "handovers":                   [{"t_pdt", "from_track", "to_track", "coverage_gap_s", "cause_hint"}]
       "coverage_gaps":               [{"t0_pdt", "dur_s", "cause_hint"}],  "gap_total_s"
       "range_apexes":                [{"t_pdt", "rng_m"}]
       "interceptor_track_count", "fight_track_count", "fight_track_count_any_contact",
       "steal_events":                [{"track", "after_pass", "pass_t_pdt", "direction": "target->interceptor"|"interceptor->target",
                                        "post_pass_median_dist_to_interceptor_m", "post_pass_median_dist_to_target_m", "track_end_pdt"}]
       "target_track_spans", "interceptor_track_spans":   {"<track_id>": "07:19:49-07:20:19, med 43.4 m off target"}
    },
    "issues": [ "free text, one anomaly per entry" ]
  } ] } ],
  "day_issues": [ "..." ],
  "method": { ...the exact algorithm strings, see below... }
}
```

## The method (what the numbers mean)

| Field | Rule |
|---|---|
| target feed | `mav*_1_*` files concatenated, dedup on 0.5 s rounded time |
| interceptor feed | `mav*_2_*` per-feed frozen runs (≥ 3 identical positions) dropped keeping the first, merged, dedup 0.5 s, > 250 m/s teleports dropped |
| flights | split on target airborne gaps > 5 min; airborne = U above ground + 5 m or speed > 2 m/s; ground = median U of parked low-speed samples; windows trimmed to first/last airborne sample |
| laps (tracking days) | split at range troughs between range apexes |
| passes | 1 Hz grid, 3-D separation of the two feeds, no interpolation across > 2.5 s gaps in either feed, local minima < 600 m with 80 m prominence per contiguous segment; `realistic` = both craft ≥ 3 m/s and closing ≥ 8 m/s; parked-interceptor dips flagged |
| coverage | percent of 1 Hz moving samples (airborne and speed > 2 m/s) with a fresh (t − last_update_t ≤ 1.5 s) target-side track state within 1.5 s and 150 m |
| track side | target-side if median horizontal distance to target < 150 m and ≤ median distance to interceptor (≥ 3 samples); interceptor-side symmetric |
| steal | a side-assigned track with ≥ 5 pre-pass samples on its side whose post-pass (≤ 120 s) median distance flips to the other craft within 120 m; both directions are "track steals" |
| drag-and-die | side track pulled > 100 m off its craft after a pass and dead within 30 s, no flip |
| corruption | identity holds through the pass but the state error exceeds the miss (state runs away); found from the error window, not the manifest |
| cause hints | turning (truth heading rate > 8°/s within ±3 s), pad/climb (AGL < 20 m), interceptor within 200 m, straight cruise, Doppler notch (truth radial speed < 3.5 m/s) |

## Roles are yours to set

The target and interceptor globs default to `mav*_1_*` and `mav*_2_*`, which
matched the Seawall pads. On 8/25 the only tracked drone was `mav14551_2_2`.
Look at the ids the dump produced and at the geometry before accepting the
defaults; pass `--target` / `--interceptor` explicitly when in doubt and write
the choice into `drone_ids`.

## Known drift in old manifests

8/25 and 8/26 use `median_horiz_err_m`, 8/27 and 8/28 `med_horiz_err_m`; 8/25
writes `t0_pdt` as `HH:MM:SS`; only 8/28 carries `passes[].miss_m`,
`steal_events` and `method`. Readers of old days must tolerate this. New
manifests always use the full form above.
