#!/usr/bin/env python
"""quicklook_report.py — ONE command from dump dir(s) (+ flights.json) to a default
day report built from the canonical modules (tracking_tab / engagement_tab).

    quicklook_report.py --type tracking|engagement|auto --day 2026-08-28 --dumps DIR [DIR ...]
                        [--manifest flights.json] [--target GLOB] [--interceptor GLOB]
                        [--ant lat,lon,hae] [--geoid-n -31.4] [--tz America/Los_Angeles]
                        [--out DIR] [--title ...] [--flights 1,2] [--pass 1:3,4:5]
                        [--radar-rollup] [--server-base URL] [--config-out day.json] [--no-build]
    quicklook_report.py --mongo "mru=43,run=6aecec5e,jobs=21627-23147,label=ep1" [--dump-root DIR] ...
                        # live unit: dump the window with dump_run_window.py first, then as above
                        # (keys: mru|host, run, jobs=A-B | t0=..,t1=.. ("YYYY-MM-DD HH:MM"), label, root, no_adsb)
    quicklook_report.py --build-only day.json        # re-run only the build after editing the JSON

What it does
  1. loads --manifest (or runs build_flight_manifest.py on the dumps when absent)
  2. picks the day template (templates/tracking_day.json | engagement_day.json;
     auto = engagement when any flight has a realistic pass, else tracking)
  3. fills every auto-fillable field: day, run id, antenna (meta.json, both key
     spellings), target / interceptor patterns, flight windows -> flights_pdt or
     window_pdt, laps -> laps_pdt / flight_keys, per flight the best realistic pass
     -> cpa_seed_pdt and the target-side track riding the target at that time ->
     track_id, out_root, factual numbers_html
  4. writes the filled config next to the output (<out>/<day>_quicklook.json)
  5. runs postprocess_report.main on it and prints wall-clock timing per phase
Narrative fields (notes_html, key_message_html, rollup_html, post_html, overview_html)
stay EMPTY — the analyst fills them in the written JSON and re-runs --build-only.
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "templates")
TZ_NAME = "America/Los_Angeles"          # --tz; DST-aware (PDT/PST), written to the config as "tz"
TZ = ZoneInfo(TZ_NAME)
PDT = TZ                                 # compatibility alias
sys.path.insert(0, HERE)

TIMES = []          # (label, seconds) — filled by the phase wrappers


# ----------------------------------------------------------------- helpers
def _t(label, t0):
    TIMES.append((label, time.perf_counter() - t0))


def hms_of(s):
    """'2026-08-26T08:35:19.229000-07:00' | '08:35:19' | '08:35:19.2' -> 'HH:MM:SS'."""
    s = str(s)
    if "T" in s:
        s = s.split("T", 1)[1]
    return s[:8]


def set_tz(name):
    global TZ_NAME, TZ, PDT
    TZ_NAME = name or "America/Los_Angeles"
    TZ = ZoneInfo(TZ_NAME)
    PDT = TZ


def epoch_pdt(day, hms):
    """campaign-local 'HH:MM:SS' on day -> epoch (tz-aware; name kept for compatibility)."""
    return datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()


def md(day):
    y, m, d = day.split("-")
    return f"{int(m)}/{int(d)}"


def load_meta(dumps):
    """run8, friendly name, antenna, geoid_n, mru from the first meta.json that has them."""
    out = dict(run8=None, friendly=None, ant=None, geoid_n=None, mru=None)
    for d in dumps:
        mp = os.path.join(d, "meta.json")
        if not os.path.exists(mp):
            continue
        m = json.load(open(mp))
        r = m.get("run_id8") or m.get("run") or ""
        r = r[4:] if str(r).startswith("run_") else str(r)
        out["run8"] = out["run8"] or (r[:8] or None)
        out["friendly"] = out["friendly"] or m.get("friendly_name")
        out["ant"] = out["ant"] or m.get("antenna_origin_lat_lon_haeM") or m.get("antenna") or m.get("antenna_origin")
        out["geoid_n"] = out["geoid_n"] if out["geoid_n"] is not None else m.get("geoid_n_m")
        out["mru"] = out["mru"] or m.get("mru")
    return out


def mavlink_ids(dumps):
    return sorted({os.path.basename(p)[:-4] for d in dumps
                   for p in glob.glob(os.path.join(d, "mavlink", "*.csv"))})


def resolve_pattern(dumps, user_glob, default_glob):
    """A truth-file glob for the campaign config (with .csv), derived from the files
    actually present: alias-broken 'mavlink_*' pseudo-ids are excluded and several
    compids collapse to '<common prefix>*.csv' (e.g. mav14551_2_*.csv)."""
    pat = (user_glob or default_glob)
    pat = pat[:-4] if pat.endswith(".csv") else pat
    if not any(c in pat for c in "*?["):
        return pat + ".csv"
    ids = [n for n in mavlink_ids(dumps) if fnmatch.fnmatch(n, pat) and not n.startswith("mavlink_")]
    if not ids:
        return pat + ".csv"
    if len(ids) == 1:
        return ids[0] + ".csv"
    pre = os.path.commonprefix(ids)
    pre = pre[:pre.rfind("_") + 1] if "_" in pre else pre
    return pre + "*.csv"


def manifest_flights(man):
    fl = []
    for r in man.get("runs", []):
        fl.extend(r.get("flights", []))
    fl.sort(key=lambda f: f.get("t0", 0))
    return fl


def manifest_run8(man):
    for r in man.get("runs", []):
        run = str(r.get("run") or "")
        if run:
            return (run[4:] if run.startswith("run_") else run)[:8]
    return None


_JUNK = ("grounded", "parked", "taxiing", "not reproduc", "not recomputable", "predate",
         "pre-liftoff", "frozen-sample", "below the 80 m gate")


def pass_ok(p):
    """Candidate pass = an air-to-air minimum: interceptor airborne and both craft
    moving (>= 3 m/s when the manifest carries speeds), miss < 600 m, no
    grounded/parked/artefact note (old manifests). The manifest's 'realistic'
    flag is deliberately NOT used as a gate: its closing-speed term is estimated
    over 3 s of 1 Hz separation and under-reads at the flattening minimum of an
    overtake (8/28: the 53/37/21/32 m passes read 3-8 m/s closing and would be
    dropped, while the served report features exactly those). engagement_tab
    refines the CPA from the real samples anyway."""
    if p.get("interceptor_grounded"):
        return False
    miss = p.get("miss_m")
    if miss is None or miss >= 600:
        return False
    for k in ("tgt_speed_mps", "itc_speed_mps"):
        if p.get(k) is not None and p[k] < 3.0:
            return False
    if "realistic" in p:                      # new schema: notes are informational
        return True
    note = str(p.get("note", "")).lower()
    return not any(k in note for k in _JUNK)


def parse_spans(flight):
    """{tid: (t0_hms, t1_hms, med_m|None)} from target_track_spans (engagement
    manifests) or rider_spans_pdt (tracking manifests)."""
    tr = flight.get("tracking", {})
    out = {}
    for tid, txt in (tr.get("target_track_spans") or {}).items():
        m = re.match(r"\s*(\d\d:\d\d:\d\d)-(\d\d:\d\d:\d\d)(?:,\s*med\s*([\d.]+))?", str(txt))
        if m:
            out[int(tid)] = (m.group(1), m.group(2), float(m.group(3)) if m.group(3) else None)
    if not out:
        for tid, v in (tr.get("rider_spans_pdt") or {}).items():
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                out[int(tid)] = (hms_of(v[0]), hms_of(v[1]), None)
    return out


def track_at(flight, day, t_hms, dumps, target_csv):
    """Target-side track id alive at t_hms: manifest spans first (smallest median
    offset wins), else a scan of the dumps' tracks/ around the pass."""
    t = epoch_pdt(day, t_hms)
    cands = []
    for tid, (a, b, med) in parse_spans(flight).items():
        if epoch_pdt(day, a) - 2.0 <= t <= epoch_pdt(day, b) + 2.0:
            cands.append((med if med is not None else float("inf"), tid))
    if cands:
        cands.sort()
        return cands[0][1], "manifest span"
    # fallback: scan the dumps (median horizontal distance to truth in +-8 s)
    import numpy as np
    import pandas as pd
    import engagement_tab as ET
    best = (float("inf"), None)
    for d in dumps:
        try:
            targ = ET.load_dump_truth(d, target_csv, t - 40, t + 40)
        except FileNotFoundError:
            continue
        for fp in glob.glob(os.path.join(d, "tracks", "track_*.csv")):
            if os.path.getsize(fp) < 400:
                continue
            df = pd.read_csv(fp, usecols=["t_epoch", "E_m", "N_m"])
            df = df[(df.t_epoch >= t - 8) & (df.t_epoch <= t + 8)]
            if len(df) < 2:
                continue
            dist = np.hypot(df.E_m - np.interp(df.t_epoch, targ[:, 0], targ[:, 1]),
                            df.N_m - np.interp(df.t_epoch, targ[:, 0], targ[:, 2]))
            med = float(np.median(dist))
            if med < 150 and med < best[0]:
                best = (med, int(os.path.basename(fp)[6:-4]))
    return best[1], f"dump scan (med {best[0]:.0f} m)" if best[1] is not None else "none"


def best_pass(flight, day, dumps, target_csv, force_n=None):
    """Best candidate pass: smallest miss; ties -> the pass whose riding track has
    the smaller median offset, then the later pass. force_n (from --pass F:N)
    selects that pass number regardless of the gate."""
    if force_n is not None:
        ok = [p for p in flight.get("passes", []) if p.get("n") == force_n]
        if not ok:
            sys.exit(f"--pass: flight {flight.get('n')} has no pass n={force_n}")
    else:
        ok = [p for p in flight.get("passes", []) if pass_ok(p)]
    if not ok:
        return None, None
    scored = []
    for p in ok:
        tid, how = track_at(flight, day, hms_of(p["t_pdt"]), dumps, target_csv)
        med = next((m for t, (_, _, m) in parse_spans(flight).items() if t == tid and m is not None),
                   float("inf"))
        scored.append((float(p.get("miss_m", 1e9)), med, -epoch_pdt(day, hms_of(p["t_pdt"])), p, tid, how))
    scored.sort(key=lambda s: s[:3])
    _, _, _, p, tid, how = scored[0]
    return p, (tid, how)


def dump_for(dumps, tid, day, t_hms, target_csv):
    """The dump dir holding track_<tid>.csv and covering the pass time."""
    t = epoch_pdt(day, t_hms)
    have = [d for d in dumps if tid is not None and os.path.exists(os.path.join(d, "tracks", f"track_{tid}.csv"))]
    if len(have) == 1:
        return have[0]
    import pandas as pd
    for d in (have or dumps):
        for fp in glob.glob(os.path.join(d, "mavlink", target_csv)):
            tt = pd.read_csv(fp, usecols=["t_epoch"]).t_epoch
            if len(tt) and tt.min() - 5 <= t <= tt.max() + 5:
                return d
    return (have or dumps)[0]


def unfilled(obj, path=""):
    """Every remaining <PLACEHOLDER> outside '_'-prefixed keys."""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).startswith("_"):
                continue
            bad += unfilled(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += unfilled(v, f"{path}[{i}]")
    elif isinstance(obj, str) and re.fullmatch(r"<[^<>]+>", obj.strip()):
        bad.append(f"{path} = {obj}")
    return bad


def fmt_num(v, f):
    return f.format(v) if isinstance(v, (int, float)) else None


# ----------------------------------------------------------------- fillers
def fill_common(cfg, a, meta, run8, kind):
    mru = meta.get("mru") or "?"
    friendly = f" ({meta['friendly']})" if meta.get("friendly") else ""
    cfg["title"] = a.title or f"Quick look — {a.day} ({'engagements' if kind == 'engagement' else 'tracking'})"
    cfg["sub"] = (f"MRU{mru} · run {run8}{friendly} · quick-look build "
                  f"(auto-filled from flights.json; narratives pending)")
    cfg["out_root"] = os.path.abspath(a.out)
    cfg["server_base"] = a.server_base or ""
    cfg["tz"] = TZ_NAME
    cfg["target_pattern"] = a.target_csv
    cfg["interceptor_pattern"] = a.inter_glob
    cfg["rollup_html"] = ""
    rr = cfg.pop("_radar_rollup_disabled", None)
    if a.radar_rollup and rr is not None:
        rr["days"] = [{"label": md(a.day), "dirs": [os.path.abspath(d) for d in a.dumps],
                       "target_pattern": a.target_csv, "interceptor_pattern": a.inter_glob}]
        cfg["radar_rollup"] = rr
    elif rr is not None:
        cfg["_radar_rollup_disabled"] = rr


def fill_tracking(cfg, a, man, meta, ant, target_csv):
    run8 = meta["run8"] or manifest_run8(man) or "run?"
    fill_common(cfg, a, meta, run8, "tracking")
    day = cfg["days"][0]
    flights = manifest_flights(man)
    if not flights:
        sys.exit("manifest has no flights")
    if a.flights:
        want = [int(x) for x in a.flights.split(",")]
        flights = [f for f in flights if f.get("n") in want]
    wins = []   # (t0_hms, t1_hms, [lap windows])
    for f in flights:
        laps = [(hms_of(s["t0_pdt"]), hms_of(s["t1_pdt"])) for s in f.get("segments", []) if s.get("type") == "loop"]
        wins.append((hms_of(f["t0_pdt"]), hms_of(f["t1_pdt"]), laps))
    # tracking_tab takes 1..N flight windows (one fast2_F<n> each) — no more splitting
    # a single flight in two or dropping the third; --flights a,b still selects
    print(f"[quicklook] {len(wins)} flight window(s) -> tracking day with {len(wins)} flight(s)")
    pr = day["prep"]
    pr["dump"] = os.path.abspath(a.dumps[0])
    if len(a.dumps) > 1:
        print(f"[quicklook] tracking prep reads ONE dump dir — using {a.dumps[0]} (pass the dir covering both flights first)")
    pr["target_pattern"] = target_csv
    pr["ant_hae_m"] = float(ant[2])
    pr["flights_pdt"] = [[a.day, t0, t1] for t0, t1, _ in wins]
    laps_pdt, keys = {}, []
    for fi, (t0, t1, laps) in enumerate(wins, start=1):
        laps = laps if len(laps) >= 2 else [(t0, t1)]
        for k, (l0, l1) in enumerate(laps, start=1):
            laps_pdt[f"{fi},{k}"] = [a.day, l0, l1]
            keys.append(f"F{fi}R{k}")
    pr["laps_pdt"] = laps_pdt
    day["flight_keys"] = keys
    pr.setdefault("gate_m", 350.0)
    nl = sorted({len([k for k in keys if k.startswith(f"F{i}R")]) for i in range(1, len(wins) + 1)})
    lap_txt = f" × {nl[0]} laps" if len(nl) == 1 and nl[0] > 1 else ""
    day["id"] = a.day
    day["title"] = f"{md(a.day)} — Tracking ({len(wins)} flight{'s' if len(wins) != 1 else ''}{lap_txt})"
    day["sub"] = f"run {run8}"
    y, m, d = (int(x) for x in a.day.split("-"))
    day["init"] = {"day": [y, m, d], "ant": [float(ant[0]), float(ant[1]), float(ant[2])],
                   "geoid_n": float(a.geoid_n if a.geoid_n is not None else (meta.get("geoid_n") if meta.get("geoid_n") is not None else -31.4))}
    day["notes_html"] = ""
    day["key_message_html"] = ""
    # factual numbers from the manifest (labelled line per metric)
    trs = [f.get("tracking", {}) for f in flights]
    az = [t.get("az_bias_deg") for t in trs if t.get("az_bias_deg") is not None]
    cov = [t.get("coverage_pct") for t in trs if t.get("coverage_pct") is not None]
    her = [t.get("med_horiz_err_m", t.get("median_horiz_err_m")) for t in trs]
    her = [h for h in her if h is not None]
    cor = [t.get("med_horiz_err_azcorr_m") for t in trs if t.get("med_horiz_err_azcorr_m") is not None]
    lines = []
    if az:
        lines.append(f"Az bias: <b>{sum(az) / len(az):+.2f}°</b>")
    if cov:
        lines.append(f"Coverage: <b>{sum(cov) / len(cov):.0f}%</b>")
    if her:
        s = f"Median error: {sum(her) / len(her):.0f} m raw"
        if cor:
            s += f" → <b>{sum(cor) / len(cor):.0f} m</b> az-corrected"
        lines.append(s)
    day["numbers_html"] = "<br>".join(lines)
    for t0, t1, laps in wins:
        print(f"[quicklook] flight {t0}–{t1}  laps: {laps or '(whole flight)'}")
    return cfg


def fill_engagement(cfg, a, man, meta, ant, target_csv, inter_glob):
    run8 = meta["run8"] or manifest_run8(man) or "run?"
    fill_common(cfg, a, meta, run8, "engagement")
    day = cfg["days"][0]
    proto = day["engagements"][0]
    flights = manifest_flights(man)
    if a.flights:
        want = [int(x) for x in a.flights.split(",")]
        flights = [f for f in flights if f.get("n") in want]
    force = {}
    for tok in (a.pass_ or "").split(","):
        if tok.strip():
            fl_, pn = tok.split(":")
            force[int(fl_)] = int(pn)
    engs, n_lt75, n_all, steals, best_all = [], 0, 0, 0, None
    for f in flights:
        n = f.get("n")
        ok = [p for p in f.get("passes", []) if pass_ok(p)]
        n_all += len(ok)
        n_lt75 += sum(1 for p in ok if p.get("miss_m") is not None and p["miss_m"] < 75)
        steals += len(f.get("tracking", {}).get("steal_events") or [])
        p, trk = best_pass(f, a.day, a.dumps, target_csv, force.get(n))
        if p is None:
            print(f"[quicklook] flight {n}: no air-to-air pass in the manifest — no engagement tab "
                  f"({len(f.get('passes', []))} listed passes)")
            continue
        tid, how = trk
        if tid is None:
            print(f"[quicklook] flight {n}: pass {p.get('n')} ({hms_of(p['t_pdt'])}) has NO target-side track "
                  f"at the pass — tab skipped (add it by hand with a traj_csv / other track)")
            continue
        t_hms = hms_of(p["t_pdt"])
        dump = dump_for(a.dumps, tid, a.day, t_hms, target_csv)
        e = json.loads(json.dumps(proto))
        abbr = datetime.fromtimestamp(epoch_pdt(a.day, t_hms), TZ).strftime("%Z") or TZ_NAME
        e["name"] = f"{md(a.day)} Flight {n} — pass {p.get('n', '?')} ({t_hms} {abbr})"
        e["tab_label"] = f"Flight {n}"
        e["dump"] = os.path.abspath(dump)
        e["interceptor_pattern"] = inter_glob
        e["target"] = {"pattern": target_csv}
        e["window_pdt"] = [hms_of(f["t0_pdt"]), hms_of(f["t1_pdt"])]
        e["cpa_seed_pdt"] = t_hms
        e["track_id"] = int(tid)
        e["gif"] = f"figs/eng_{tid}_{t_hms.replace(':', '')}.gif"
        e["post_html"] = ""
        e.setdefault("summary_html", "<p>Closest approach <b>{cpa_truth:.0f} m truth-truth</b>; "
                                     "<b>{cpa_track:.0f} m</b> to radar track {track_id}.</p>")
        engs.append(e)
        miss = p.get("miss_m")
        if miss is not None and (best_all is None or miss < best_all):
            best_all = miss
        print(f"[quicklook] flight {n}: {'forced' if n in force else 'best'} pass {p.get('n')} @ {t_hms} "
              f"({miss} m {p.get('source', '')}) -> track {tid} [{how}] in {os.path.basename(dump)}")
    if not engs:
        sys.exit("no engagement could be configured (no realistic passes with a target-side track)")
    day["id"] = a.day
    day["date"] = a.day
    day["title"] = f"{md(a.day)} — Engagements ({len(engs)} flights)"
    day["sub"] = f"run {run8}"
    day["ant"] = [float(ant[0]), float(ant[1]), float(ant[2])]
    day["overview_html"] = ""
    day["engagements"] = engs
    day["key_message_html"] = ""
    lines = []
    if best_all is not None:
        lines.append(f"Best pass: <b>{best_all:.0f} m</b> (truth-truth, manifest)")
    lines.append(f"Passes &lt;75 m: {n_lt75} of {n_all} realistic")
    if any("steal_events" in f.get("tracking", {}) for f in flights):
        lines.append(f"Track steals: <b>{steals}</b>")
    day["numbers_html"] = "<br>".join(lines)
    return cfg


# ----------------------------------------------------------------- build
def _wrap(mod, name, label):
    orig = getattr(mod, name)

    def w(*args, **kw):
        t0 = time.perf_counter()
        try:
            return orig(*args, **kw)
        finally:
            _t(label(*args, **kw) if callable(label) else label, t0)
    setattr(mod, name, w)


def run_build(cfg_path):
    t_imp = time.perf_counter()
    import postprocess_report as P
    import engagement_tab as ET
    import tracking_tab as TT
    _t("import pipeline modules", t_imp)
    _wrap(TT, "prep_from_dump", "prep_from_dump (mission inputs from the dump)")
    _wrap(TT, "init_mission", "init_mission (truth frame, matching, bias)")
    _wrap(TT, "build_summary_tab", "Summary tab")
    _wrap(TT, "build_flight_tab", lambda k, *a, **kw: f"flight tab {k}")
    _wrap(ET, "build_engagement_tab", lambda cfg, *a, **kw: f"engagement tab [{cfg.get('name')}] (3-D gif + panes)")
    _wrap(ET, "write_page", lambda path, *a, **kw: f"write {os.path.relpath(path, os.path.dirname(os.path.dirname(path)))}")
    _wrap(P, "build_radar_rollup", "radar rollup")
    _wrap(P, "write_standalone_rollup", "standalone rollup export")
    cfg = json.load(open(cfg_path))
    n_fov = sum(1 for d in cfg.get("days", []) for e in d.get("engagements", []) if e.get("fov"))
    if any(d.get("type") == "engagement" for d in cfg.get("days", [])) and not n_fov:
        print("[fov] no 'fov' block in any engagement — Seeker FOV section skipped "
              "(needs interceptor ulog + IR video + clock offset + ffmpeg; see references/quicklook.md)")
    t0 = time.perf_counter()
    P.main(cfg_path)
    _t("TOTAL build (postprocess_report.main)", t0)


def print_times():
    print("\n[timing] wall-clock per phase")
    for lbl, s in TIMES:
        print(f"  {s:7.1f} s  {lbl}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--type", default="auto", choices=["auto", "tracking", "engagement"])
    ap.add_argument("--day", help="YYYY-MM-DD (campaign-local calendar day, see --tz)")
    ap.add_argument("--dumps", nargs="+", help="dump dir(s): mavlink/, tracks/, meta.json")
    ap.add_argument("--manifest", help="flights.json (default: run build_flight_manifest.py on the dumps)")
    ap.add_argument("--target", help="target truth csv/glob, e.g. mav14550_1_1.csv (default: manifest tracking.target)")
    ap.add_argument("--interceptor", help="interceptor truth glob, e.g. 'mav14551_2_*.csv' (default: derived from mavlink/)")
    ap.add_argument("--ant", help="lat,lon,hae override (default: meta.json)")
    ap.add_argument("--geoid-n", type=float, default=None, help="site HAE-MSL (default meta.json geoid_n_m, else -31.4)")
    ap.add_argument("--tz", default=None, help="campaign IANA timezone for every clock string (default: the dump's "
                                              "meta.json 'tz', else America/Los_Angeles)")
    ap.add_argument("--mongo", help="live unit instead of --dumps: 'mru=43,run=6aecec5e,jobs=21627-23147,label=ep1' "
                                    "(or host=, t0=/t1= 'YYYY-MM-DD HH:MM', root=, no_adsb=1); runs dump_run_window.py")
    ap.add_argument("--dump-root", help="where --mongo dumps land (default <out>/dumps)")
    ap.add_argument("--out", help="report root (default ./quicklook_<day>)")
    ap.add_argument("--title", help="Rollup page title")
    ap.add_argument("--flights", help="comma list of manifest flight numbers to use (tracking: exactly two)")
    ap.add_argument("--pass", dest="pass_", help="engagement: force the featured pass per flight, e.g. '1:3,4:5' (flight:pass n)")
    ap.add_argument("--radar-rollup", action="store_true", help="also build the Radar Rollup tab (slower)")
    ap.add_argument("--server-base", default="", help="served URL of out_root for the standalone export links")
    ap.add_argument("--config-out", help="where to write the filled config (default <out>/<day>_quicklook.json)")
    ap.add_argument("--no-build", action="store_true", help="write the config only")
    ap.add_argument("--build-only", metavar="CFG", help="skip filling; build this (edited) config with timings")
    a = ap.parse_args()

    t_all = time.perf_counter()
    if a.build_only:
        cfg = json.load(open(a.build_only))
        bad = unfilled(cfg)
        if bad:
            sys.exit("unfilled placeholders in the config:\n  " + "\n  ".join(bad))
        run_build(os.path.abspath(a.build_only))
        _t("TOTAL quicklook", t_all)
        print_times()
        return
    if a.mongo:
        # live unit: dump first (dump_run_window.py via postprocess_report.run_dumper),
        # then continue exactly as with --dumps
        t0 = time.perf_counter()
        import postprocess_report as P
        mg = {}
        for tok in a.mongo.split(","):
            if "=" in tok:
                k, v = tok.split("=", 1)
                mg[k.strip()] = v.strip()
        for k in ("mru", "port"):
            if k in mg: mg[k] = int(mg[k])
        for k in ("no_adsb", "no_obs", "overwrite"):
            if k in mg: mg[k] = str(mg[k]).lower() in ("1", "true", "yes")
        a.out = os.path.abspath(a.out or f"quicklook_{a.day or 'mongo'}")
        os.makedirs(a.out, exist_ok=True)
        P.CAMPAIGN.update(tz=a.tz or TZ_NAME, out_root=a.out,
                          dump_root=os.path.abspath(a.dump_root) if a.dump_root else None,
                          geoid_n=a.geoid_n)
        dump = P.run_dumper(mg, a.day)
        a.dumps = (a.dumps or []) + [dump]
        _t("dump_run_window.py (live mongo -> dump dir)", t0)
        if not a.day:
            m = json.load(open(os.path.join(dump, "meta.json")))
            a.day = m.get("day") or os.path.basename(os.path.dirname(dump))
            print(f"[quicklook] --day taken from the dump: {a.day}")
    if not (a.day and a.dumps):
        ap.error("--day and --dumps are required (or --mongo SPEC, or --build-only CFG)")
    for d in a.dumps:
        if not os.path.isdir(os.path.join(d, "mavlink")):
            sys.exit(f"not a dump dir (no mavlink/): {d}")
    a.out = os.path.abspath(a.out or f"quicklook_{a.day}")
    os.makedirs(a.out, exist_ok=True)
    # timezone: --tz > the dump's meta.json 'tz' (dump_run_window writes it) > LA
    if not a.tz:
        for d in a.dumps:
            try:
                a.tz = json.load(open(os.path.join(d, "meta.json"))).get("tz") or None
            except Exception:
                pass
            if a.tz:
                break
    set_tz(a.tz or "America/Los_Angeles")
    print(f"[quicklook] timezone {TZ_NAME}")

    # 1. manifest
    t0 = time.perf_counter()
    if a.manifest:
        man = json.load(open(a.manifest))
        _t(f"load manifest {a.manifest}", t0)
    else:
        bfm = os.path.join(HERE, "build_flight_manifest.py")
        if not os.path.exists(bfm):
            sys.exit("no --manifest and scripts/build_flight_manifest.py not found")
        mdir = os.path.join(a.out, "manifest")
        cmd = [sys.executable, bfm, "--day", a.day, "--dumps", *a.dumps, "--out", mdir]
        if a.target:
            cmd += ["--target", a.target[:-4] if a.target.endswith(".csv") else a.target]
        if a.interceptor:
            cmd += ["--interceptor", a.interceptor[:-4] if a.interceptor.endswith(".csv") else a.interceptor]
        if a.type != "auto":
            cmd += ["--type", a.type]
        cmd += ["--tz", TZ_NAME]
        print("[quicklook] building manifest:", " ".join(cmd))
        subprocess.run(cmd, check=True)
        man = json.load(open(os.path.join(mdir, "flights.json")))
        _t("build_flight_manifest.py", t0)

    # 2. inputs common to both templates
    t0 = time.perf_counter()
    meta = load_meta(a.dumps)
    if a.ant:
        ant = [float(x) for x in a.ant.split(",")]
    else:
        ant = meta["ant"] or next((r.get("antenna") for r in man.get("runs", []) if r.get("antenna")), None)
    if not ant or len(ant) < 3:
        sys.exit("no antenna origin: meta.json lacks 'antenna'/'antenna_origin_lat_lon_haeM' — pass --ant lat,lon,hae")
    flights = manifest_flights(man)
    if a.flights:                          # --type auto must judge only the selected flights
        want = [int(x) for x in a.flights.split(",")]
        flights = [f for f in flights if f.get("n") in want] or flights
    man_target = next((f.get("tracking", {}).get("target") for f in flights if f.get("tracking", {}).get("target")), None)
    if a.target:
        target_csv = resolve_pattern(a.dumps, a.target, "mav*_1_*")
    elif man_target:                       # 'mav14550_1_1' or 'mav14550_1_1+mav14550_1_2'
        ids = man_target.split("+")
        target_csv = (ids[0] + ".csv" if len(ids) == 1
                      else resolve_pattern(a.dumps, os.path.commonprefix(ids) + "*", "mav*_1_*"))
    else:
        target_csv = resolve_pattern(a.dumps, None, "mav*_1_*")
    inter_glob = resolve_pattern(a.dumps, a.interceptor, "mav*_2_*")
    a.target_csv, a.inter_glob = target_csv, inter_glob        # -> campaign-level patterns
    kind = a.type
    if kind == "auto":
        kind = "engagement" if any(pass_ok(p) for f in flights for p in f.get("passes", [])) else "tracking"
        print(f"[quicklook] --type auto -> {kind}")
    print(f"[quicklook] day {a.day} · run {meta['run8'] or manifest_run8(man)} · antenna {ant} · "
          f"target {target_csv} · interceptor {inter_glob} · {len(flights)} flights in manifest")

    # 3. fill the template
    tpl = os.path.join(TEMPLATES, "tracking_day.json" if kind == "tracking" else "engagement_day.json")
    cfg = json.load(open(tpl))
    cfg = (fill_tracking(cfg, a, man, meta, ant, target_csv) if kind == "tracking"
           else fill_engagement(cfg, a, man, meta, ant, target_csv, inter_glob))
    bad = unfilled(cfg)
    if bad:
        sys.exit("internal: unfilled placeholders remain:\n  " + "\n  ".join(bad))
    cfg_path = os.path.abspath(a.config_out or os.path.join(a.out, f"{a.day}_quicklook.json"))
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    json.dump(cfg, open(cfg_path, "w"), indent=1, ensure_ascii=False)
    _t("fill template -> config", t0)
    print(f"[quicklook] wrote config {cfg_path}  (template {os.path.basename(tpl)})")
    if a.no_build:
        print_times()
        return

    # 4. build
    run_build(cfg_path)
    _t("TOTAL quicklook", t_all)
    print_times()
    print(f"\n[quicklook] report: {a.out}/{a.day}/report.html   rollup: {a.out}/Rollup.html")
    print(f"[quicklook] edit narratives in {cfg_path} then: quicklook_report.py --build-only {cfg_path}")


if __name__ == "__main__":
    main()
