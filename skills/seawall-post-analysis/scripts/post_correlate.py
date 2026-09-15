#!/usr/bin/env python
"""Post-process MAVLink-truth vs VP-track correlation report for a time window.

Works against any MRU's mongo (or a saved npz). All errors are RAW radar
output by default — no bias removal; the measured az bias is annotated and an
alignment-check rotated panel is shown. The only default adjustment is the
truth altitude datum (median track-truth dU — a truth-reference defect).

Examples (times are LOCAL in --tz; run with sensorenv python):
  python post_correlate.py --host 10.191.28.205 --t0 14:30 --t1 15:00
  python post_correlate.py --host 10.146.28.205 --list-runs
  python post_correlate.py --host 10.191.28.205 --t0 "2026-08-26 08:35" \
      --t1 "2026-08-26 08:53" --target 14550 --agl-min 20 --seeker --obs
  python post_correlate.py --npz flights/flight_20260826_091844.npz \
      --t0 09:00 --t1 09:14

Report sections: top-down EN (RAW + rotated), az/el error stack (obs overlay,
RMSE + 95.5% containment), 3D pos + |Δv| velocity error (filter state vs
truth-REPORTED velocity), measurement rate, optional seeker basket (quad of
standoffs + cartoon), per-track init/drop table, rollup stats.
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corr_lib as C
import report_figs as F


# bulk npz track layout (fast-extract / live-tool saves):
# (t,E,N,U,vE,vN,vU,sE,sN,sU,assoc,state,lu) -> load_tracks layout
# (t,E,N,U,sE,sN,sU,lu,assoc,state,vE,vN,vU)
BULK2STD = [0, 1, 2, 3, 7, 8, 9, 12, 10, 11, 4, 5, 6]


def load_npz(path, t0, t1, target, only):
    Z = np.load(path)
    win = lambda A: A[(A[:, 0] >= t0) & (A[:, 0] <= t1)]
    truths = {k[4:]: C.clean_truth(win(Z[k]))
              for k in Z.files if k.startswith("mav_")}
    truths = {k: v for k, v in truths.items()
              if len(v) >= 5 and (not target or target in k)}
    tracks = {}
    for k in Z.files:
        if not k.startswith("trk_"):
            continue
        tid = int(k[4:])
        if only and tid not in only:
            continue
        A = win(Z[k])
        if not len(A):
            continue
        if A.shape[1] >= 13:
            tracks[tid] = A[:, BULK2STD]
        else:                       # very old 10-col saves: no velocities
            tracks[tid] = np.column_stack([A, np.full((len(A), 3), np.nan)])
    obs = win(Z["obs"]) if "obs" in Z.files else None
    return truths, tracks, obs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("data source")
    src.add_argument("--host", default="10.191.28.205", help="mongo host (mx node)")
    src.add_argument("--port", type=int, default=27017)
    src.add_argument("--db", default="sensor_store")
    src.add_argument("--run", default=None, help="explicit run_<hex> collection "
                     "(default: most-overlapping run in the window)")
    src.add_argument("--list-runs", action="store_true",
                     help="list run_* collections with time spans and exit")
    src.add_argument("--npz", default=None,
                     help="offline mode: bulk npz (mav_*/trk_*[/obs] arrays) "
                          "instead of mongo")
    win = ap.add_argument_group("window / selection")
    win.add_argument("--t0", help="window start, local time in --tz")
    win.add_argument("--t1", help="window end, local time in --tz")
    win.add_argument("--tz", default="America/Los_Angeles",
                     help="IANA tz for --t0/--t1 and plot timestamps")
    win.add_argument("--target", default=None,
                     help="substring filter on truth target_id (e.g. 14550)")
    win.add_argument("--tracks", default=None,
                     help="comma-separated track ids (skip auto-correlation)")
    gat = ap.add_argument_group("correlation / cleaning")
    gat.add_argument("--gate", type=float, default=150.0,
                     help="auto-correlation gate: median horiz over MOVING-truth "
                          "samples, m")
    gat.add_argument("--min-speed", type=float, default=2.0,
                     help="truth speed (m/s) above which a sample counts as "
                          "moving for gating")
    gat.add_argument("--min-dur", type=float, default=10.0,
                     help="drop auto-correlated tracks shorter than this (s); "
                          "single-hit fragments coast ~8 s by tracker design")
    gat.add_argument("--keep-adsb", action="store_true",
                     help="do NOT positionally exclude tracks matching ADS-B "
                          "truth streams")
    gat.add_argument("--agl-min", type=float, default=0.0,
                     help="clamp truth to > this many m AGL (kills takeoff/"
                          "landing junk; 20 is a good field value; 0 = off)")
    gat.add_argument("--max-horiz", type=float, default=350.0,
                     help="mask matched samples farther than this from truth "
                          "(coast excursions)")
    err = ap.add_argument_group("error conventions")
    err.add_argument("--az-bias", default="none",
                     help="'none' (RAW, default — bias annotated but INCLUDED "
                          "in errors), 'auto' (estimate & remove), or a fixed "
                          "deg value to remove")
    err.add_argument("--alt-datum", default="auto",
                     help="truth altitude datum handling: 'auto' (median "
                          "track-truth dU removed), '0' (off), or fixed m value")
    err.add_argument("--vu-scale", default="auto",
                     help="truth vertical_speed unit scale: 'auto' (wire is "
                          "ft/min -> m/s via 0.3048/60) or a float (1 = already "
                          "m/s)")
    err.add_argument("--alt-units", default="feet", choices=["feet", "meters"],
                     help="AIR_TRAFFIC 'altitude' wire units: 'feet' (default, "
                          "MSL -> HAE conversion applied) or 'meters' (already "
                          "HAE, no conversion)")
    err.add_argument("--geoid-n", type=float, default=None,
                     help="geoid undulation HAE-MSL (m) for MSL->HAE; default "
                          "uses corr_lib.GEOID_N (-31.4 m, SoCal). Set per site.")
    ext = ap.add_argument_group("extra sections")
    ext.add_argument("--obs", action="store_true",
                     help="pull raw DWELL_WITH_OBS detections and overlay "
                          "matched obs on the angle plots (mongo mode; npz "
                          "uses an 'obs' array when present)")
    ext.add_argument("--seeker", action="store_true",
                     help="add the seeker-basket quad + cartoon (closing "
                          "samples only)")
    ext.add_argument("--standoffs", default="600,450,300,150",
                     help="seeker standoff ranges, m, comma-separated")
    ext.add_argument("--fov", type=float, default=12.0, help="seeker FOV, deg")
    ap.add_argument("--label", default=None,
                    help="report title label (e.g. 'MRU46 cal-check flight')")
    ap.add_argument("--out", default=None, help="output dir")
    args = ap.parse_args()

    C.set_local_tz(args.tz)
    C.MOVING_MPS = args.min_speed

    if args.list_runs:
        try:
            db = C.connect(args.host, args.port, db=args.db)
            names = C.run_names(db)
            rows = C.run_spans(db)
        except Exception as e:
            sys.exit(f"can't reach mongo at {args.host}:{args.port} — {e!r}")
        for a, b, name in sorted(rows, key=lambda r: -r[1]):
            f = lambda x: datetime.fromtimestamp(x, tz=C.LOCAL_TZ).strftime(
                "%Y-%m-%d %H:%M")
            print(f"{name}  {names.get(name, ''):20s} {f(a)} -> {f(b)} "
                  f"({(b-a)/3600:.1f} h)")
        return

    if not args.t0 or not args.t1:
        ap.error("--t0 and --t1 are required (except with --list-runs)")
    t0, t1 = C.parse_local(args.t0), C.parse_local(args.t1)
    only = set(int(x) for x in args.tracks.split(",")) if args.tracks else None
    adsb_truths = {}
    obs_raw = None
    run = None

    if args.npz:
        truths, tracks, obs_raw = load_npz(args.npz, t0, t1, args.target, only)
        run = os.path.basename(args.npz)
        print(f"offline npz {run}: {len(truths)} truth streams, {len(tracks)} "
              f"tracks (no ADS-B exclusion — npz carries MAVLink only)")
    else:
        try:
            db = C.connect(args.host, args.port, db=args.db)
            run = args.run or (C.find_runs(db, t0, t1) or [None])[0]
        except Exception as e:
            sys.exit(f"can't reach mongo at {args.host}:{args.port} — {e!r}")
        if not run:
            sys.exit("no run collection overlaps that window (--list-runs to see)")
        col = db[run]
        nm = C.run_names(db).get(run)
        print(f"run {run}{f' ({nm})' if nm else ''}, window {args.t0} - {args.t1} "
              f"{args.tz}")
        ant = C.antenna_origin(col, t0, t1)
        if ant is None:
            sys.exit("no TRACKS in that window — is the run/window right?")
        frame = C.EnuFrame(ant)
        truths = C.load_truth(col, frame, t0, t1, target=args.target,
                              alt_units=args.alt_units, geoid_n=args.geoid_n)
        tracks = C.load_tracks(col, t0, t1, only=only)
        print("truth streams:", {k: len(v) for k, v in truths.items()})
        print(f"tracks loaded: {len(tracks)}")
        if not args.keep_adsb and not only:
            adsb_truths = C.load_adsb_truth(col, frame, t0, t1,
                                            alt_units=args.alt_units,
                                            geoid_n=args.geoid_n)
            print(f"nearby ADS-B truth streams for positional exclusion: "
                  f"{len(adsb_truths)}")
        if args.obs:
            obs_raw = C.load_obs(col, t0, t1)
            print(f"raw observations in window: {len(obs_raw)}")
    if not truths:
        sys.exit("no (moving) MAVLink truth in window")

    # optional AGL clamp (single-target flights: apply to each stream)
    pad_note = ""
    if args.agl_min > 0:
        for tgt in list(truths):
            T2, pad_u = C.clamp_agl(truths[tgt], args.agl_min)
            if len(T2) >= 5:
                truths[tgt] = T2
                pad_note = f" · truth clamped >{args.agl_min:g} m AGL (pad U {pad_u:.0f} m)"
            else:
                print(f"  AGL clamp would empty {tgt} — skipped for it")

    # correlate
    if only:
        pairs = []
        for tid in sorted(only):
            if tid not in tracks:
                print(f"  WARNING: track {tid} not found in window")
                continue
            best = None
            for tgt, T in truths.items():
                Mm = C.match_track(tracks[tid], T)
                if len(Mm) < 4:
                    continue
                med = float(np.median(np.hypot(Mm[:, 11], Mm[:, 12])))
                if best is None or med < best[3]:
                    best = (tid, tgt, Mm, med)
            if best:
                pairs.append(best)
    else:
        pairs, excluded = C.auto_correlate(
            tracks, truths, gate_m=args.gate,
            adsb_truths=adsb_truths or None)   # positional exclusion only —
        # radar classification / truth_match deliberately NOT used (untrusted)
        for tid, why in excluded.items():
            print(f"  excluded track {tid}: {why}")
        short = [p for p in pairs if p[2][-1, 0] - p[2][0, 0] < args.min_dur]
        if short:
            print(f"  hidden {len(short)} fragment(s) < {args.min_dur:.0f} s: "
                  + ", ".join(str(p[0]) for p in short))
        pairs = [p for p in pairs if p[2][-1, 0] - p[2][0, 0] >= args.min_dur]
    if not pairs:
        sys.exit("nothing correlated (try --gate, --tracks, or --agl-min 0)")
    for tid, tgt, Mm, med in pairs:
        print(f"  track {tid} <-> {tgt}: n={len(Mm)} med horiz {med:.0f} m")

    # conventions: bias measured always; removed only if asked
    bias_meas = C.az_bias_deg([p[2] for p in pairs])
    if args.az_bias == "none":
        remove_bias = 0.0
    elif args.az_bias == "auto":
        remove_bias = bias_meas
    else:
        remove_bias = float(args.az_bias)
    du_all = np.concatenate([p[2][:, 13] for p in pairs])
    if args.alt_datum == "auto":
        du = float(np.median(du_all))
    else:
        du = float(args.alt_datum)
    print(f"az bias measured {bias_meas:+.2f}° "
          f"({'INCLUDED in errors (RAW)' if remove_bias == 0 else f'{remove_bias:+.2f}° removed'})"
          f" · truth alt datum {du:+.0f} m removed")

    # prep matched samples (death-coast truncation + excursion mask)
    prepped = []
    for tid, tgt, Mm, med in pairs:
        m = C.prep_matched(Mm, max_horiz=args.max_horiz)
        if m is not None:
            prepped.append((tid, tgt, m))
    if not prepped:
        sys.exit("nothing left after prep — loosen --max-horiz?")

    # single merged truth stream for velocity/obs matching: the correlated one(s)
    used_tgts = sorted({tgt for _, tgt, _ in prepped})
    Tall = np.vstack([truths[tgt] for tgt in used_tgts])
    Tall = Tall[np.argsort(Tall[:, 0])]
    vu = C.vu_scale(Tall) if args.vu_scale == "auto" else float(args.vu_scale)
    vu_ok = C.track_vu_usable({tid: tracks[tid] for tid, _, _ in prepped})
    if vu != 1.0:
        print(f"truth vertical_speed converted ft/min -> m/s (scale {vu:.5f})")
    if not vu_ok:
        print("track vertical rate not usable on this build -> velocity error "
              "is HORIZONTAL only")

    obs_matched = None
    if obs_raw is not None and len(obs_raw):
        obs_matched = C.match_obs(obs_raw, Tall, du=du)
        print(f"obs matched to truth: {len(obs_matched)}")

    ctx = F.Ctx(Tall, prepped, tracks, du=du, bias=bias_meas,
                remove_bias=remove_bias, obs=obs_matched, vu=vu, vu_ok=vu_ok)

    outd = args.out or ("corr_" + args.t0.replace(":", "").replace(" ", "_")
                        + "_" + args.t1.replace(":", "").replace(" ", "_"))
    os.makedirs(outd, exist_ok=True)
    with open(os.path.join(outd, "plotly.min.js"), "w") as f:
        f.write(C.get_plotlyjs())

    s = F.run_stats(ctx, t0, t1)
    raw_tag = ("RAW — no bias removal" if remove_bias == 0
               else f"az bias {remove_bias:+.2f}° removed")
    parts = [f"<p><b>Window:</b> {args.t0}–{args.t1} {args.tz} · "
             f"<b>errors:</b> {raw_tag} · <b>measured az bias:</b> {bias_meas:+.2f}° · "
             f"<b>truth alt datum:</b> {du:+.0f} m removed{pad_note}<br>"
             f"<b>horiz err med/p95:</b> {s['horiz_med']:.0f} / {s['horiz_p95']:.0f} m · "
             f"<b>3D ENU med/p95:</b> {s['e3d_med']:.0f} / {s['e3d_p95']:.0f} m · "
             f"<b>measurements:</b> {s['meas']} ({s['rate']:.2f} Hz avg) · "
             f"<b>tracked coverage:</b> {s['cov']*100:.0f}% (meas within 2 s, "
             f"first→last detection) · <b>longest gap:</b> {s['max_gap']:.0f} s</p>"]
    parts.append("<div class='cap'>Top-down EN — RAW left, az-bias-rotated right "
                 "(one legend controls both panels)</div>"
                 + F.div(F.maps_fig(ctx), 470)
                 + "<p class='note'>Reading the plots: solid line = filter track "
                   "state · filled dots = filter measurement updates"
                 + (" · grey × = raw radar observations" if obs_matched is not None
                    else "") + " · radar sits at (0,0).</p>")
    parts.append("<div class='cap'>Az (top) / El (bottom) error — " + raw_tag
                 + " · band ±1σ, dotted edges ±3σ · shared time axis</div>"
                 + F.div(F.angle_stack(ctx), 640))
    parts.append("<div class='cap'>3D position error (top) / velocity error |Δv| "
                 "(bottom) — filter state vs truth-REPORTED velocity "
                 "(solid = 10 s rolling mean, faint = per-sample"
                 + ("" if vu_ok else "; vertical rate excluded — this build's "
                    "track state does not publish a usable one") + ")</div>"
                 + F.div(F.posvel_fig(ctx), 600))
    parts.append("<div class='cap'>Tracking measurement rate (all correlated "
                 "tracks merged, 30 s smoothed)</div>"
                 + F.div(F.rate_fig(ctx, t0, t1,
                         obs_count=(len(obs_matched) if obs_matched is not None
                                    else None)), 320))
    seeker_stats = {}
    if args.seeker:
        standoffs = tuple(int(x) for x in args.standoffs.split(","))
        qfig, seeker_stats = F.seeker_quad(ctx, standoffs=standoffs,
                                           fov_deg=args.fov)
        if seeker_stats:
            parts.append("<div class='cap'>Seeker off-boresight angle to TRUTH — "
                         "seeker ahead of the TRACK state along the track "
                         "velocity; closing samples only; dashed circle = "
                         f"{args.fov:g}° FOV</div>"
                         + F.seeker_cartoon(seeker_stats, standoffs, args.fov)
                         + F.div(qfig, 700))
        else:
            parts.append("<p class='note'>seeker section skipped — no closing "
                         "samples with usable track velocity</p>")

    # per-track init/drop table
    rows = ""
    summary = {}
    for tid, tgt, m in prepped:
        A = tracks[tid]
        ft = C.meas_times(A)
        ft = ft[(ft >= t0) & (ft <= t1)]
        ini = C.interp_truth(Tall, ft[0]) if len(ft) else None
        drp = C.interp_truth(Tall, ft[-1]) if len(ft) else None
        f = lambda ts: datetime.fromtimestamp(ts, tz=C.LOCAL_TZ).strftime("%H:%M:%S")
        rng = lambda tr: f"{math.hypot(tr[0], tr[1]):.0f}" if tr is not None else "—"
        dEc, dNc, dUc = ctx.corrected(m)
        summary[tid] = dict(truth=tgt, n=len(m), meas=int(len(ft)),
                            horiz_med_m=round(float(np.median(np.hypot(dEc, dNc))), 1),
                            e3d_med_m=round(float(np.median(
                                np.sqrt(dEc**2 + dNc**2 + dUc**2))), 1))
        rows += (f"<tr><td>{tid}</td><td>{tgt}</td><td>{len(m)}</td>"
                 f"<td>{summary[tid]['horiz_med_m']}</td>"
                 f"<td>{summary[tid]['e3d_med_m']}</td><td>{len(ft)}</td>"
                 f"<td>{f(ft[0]) if len(ft) else '—'} @ {rng(ini)} m</td>"
                 f"<td>{f(ft[-1]) if len(ft) else '—'} @ {rng(drp)} m</td></tr>")
    parts.append("<div class='cap'>Per-track rollup</div>"
                 "<table><tr><th>track</th><th>truth</th><th>matched n</th>"
                 "<th>horiz med (m)</th><th>3D med (m)</th><th>meas</th>"
                 "<th>first meas @ truth rng</th><th>last meas @ truth rng</th>"
                 f"</tr>{rows}</table>")

    title = args.label or f"Track correlation {args.t0}–{args.t1}"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><script src="plotly.min.js"></script>
<style>body{{margin:0;background:{F.SURFACE};color:{F.INK};
font:15px/1.55 system-ui,sans-serif;padding:1.5rem}}
main{{max-width:1120px;margin:0 auto}} h1{{font-size:1.35rem;margin:.2rem 0 .6rem}}
.cap{{font-weight:600;margin:1.1rem 0 .2rem}}
.note{{color:{F.INK2};font-size:.88rem}}
.plot{{border:1px solid #e4e3df;border-radius:6px;margin:.2rem 0 .8rem;padding:.2rem}}
table{{border-collapse:collapse;font-size:.9rem;margin:.4rem 0 1rem}}
th,td{{border:1px solid #e4e3df;padding:.25rem .6rem;text-align:right;
white-space:nowrap}} th{{background:#f4f3f0}}
th:first-child,td:first-child{{text-align:left}}</style></head><body><main>
<h1>{title}</h1>
<p class='note'>run <code>{run}</code> · target(s) {', '.join(used_tgts)}</p>
{''.join(parts)}</main></body></html>"""
    with open(os.path.join(outd, "index.html"), "w") as f:
        f.write(html)
    with open(os.path.join(outd, "summary.json"), "w") as f:
        json.dump({"run": run, "window": [args.t0, args.t1], "tz": args.tz,
                   "az_bias_measured_deg": round(bias_meas, 2),
                   "az_bias_removed_deg": round(remove_bias, 2),
                   "alt_datum_m": round(du, 1), "vu_scale": vu,
                   "track_vu_usable": vu_ok, "stats": s,
                   "seeker_pacq_pct": seeker_stats,
                   "tracks": {str(k): v for k, v in summary.items()}}, f, indent=1)
    print(f"report: {outd}/index.html")


if __name__ == "__main__":
    main()
