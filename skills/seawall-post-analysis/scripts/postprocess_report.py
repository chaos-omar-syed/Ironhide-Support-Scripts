#!/usr/bin/env python
"""STANDARD post-process report pipeline.

    X days of data (live mongo runs or archived dumps)
        -> one report per day (tracking or engagement format, from the two
           canonical modules tracking_tab / engagement_tab — no other figure code)
        -> a rollup index that stitches the days together.

Usage:  python postprocess_report.py <campaign.json>

Every narrative (day key messages, engagement summaries, rollup text) lives in
the config; the code is mission-agnostic and reusable for future events.

Day inputs:
  "dump": <archiver/quickdump/dump_run_window dir>   use archived data (offline)
  "mongo": {"mru": 43 | "host": ip, "run": <hex prefix|run_<hex>|friendly>,
            "t0": "YYYY-MM-DD HH:MM" | "HH:MM", "t1": ..  OR  "jobs": "A-B",
            "label": str, "root": dir (opt), "no_adsb": bool (opt)}
        -> runs scripts/dump_run_window.py first (writes <root>/<day>/<run8>_<label>/),
           then proceeds offline; the resolved dir is stored back into "dump".
        Valid on a day, a tracking "prep", an engagement, a radar_rollup day.

Campaign-level keys (all optional, defaults reproduce the Seawall 8/24 report):
  "tz"                  IANA zone for EVERY clock string (default America/Los_Angeles)
  "target_pattern"      truth csv glob for the target     (default mav14550_1_1.csv;
                        radar_rollup default: biggest mav14550*.csv)
  "interceptor_pattern" truth csv glob for the interceptor (default mav14551_2_*.csv)
  "dump_root"           where mongo-day dumps land (default <out_root>/dumps)
  "geoid_n"             site HAE-MSL (m) passed to the dumper for mongo days
  Per-day / per-engagement / per-rollup-day "target_pattern" / "interceptor_pattern"
  override the campaign value.

Day types:
  "tracking":    tracking_tab   — Summary tab + one tab per flight/run (1..N flights)
  "engagement":  engagement_tab — one tab per engagement (3-D, top-down,
                 closing distance/speed, heading error, error window, state)

Output tree:  <out_root>/<day_id>/report.html  +  <out_root>/index.html
"""
import os, re, sys, json, subprocess, html as _html
import numpy as np
import engagement_tab as ET
import tracking_tab as TT

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPER = os.path.join(HERE, "dump_run_window.py")

# legacy (Seawall) truth-file globs — used only when neither the campaign nor the
# day/engagement names a pattern, so old configs reproduce byte-for-byte
LEGACY_TARGET, LEGACY_INTERCEPTOR = "mav14550_1_1.csv", "mav14551_2_*.csv"
CAMPAIGN = dict(tz="America/Los_Angeles", target_pattern=None, interceptor_pattern=None,
                dump_root=None, geoid_n=None, out_root=None, title="")


def configure(cfg):
    """Campaign-level defaults (tz, truth patterns, dump root) -> module state +
    the tz of the two report modules. Called by main(); call it yourself when
    driving tracking_day_tabs / engagement_day_tabs / build_radar_rollup directly."""
    CAMPAIGN.update(tz=cfg.get("tz") or "America/Los_Angeles",
                    target_pattern=cfg.get("target_pattern") or None,
                    interceptor_pattern=cfg.get("interceptor_pattern") or None,
                    dump_root=cfg.get("dump_root") or None,
                    geoid_n=cfg.get("geoid_n"), out_root=cfg.get("out_root"),
                    title=cfg.get("title", ""))
    ET.set_tz(CAMPAIGN["tz"])
    TT.set_tz(CAMPAIGN["tz"])
    return CAMPAIGN


def _pat_explicit(kind, *scopes):
    """The innermost explicit '<kind>_pattern' among scopes, then the campaign's;
    None when nobody set one."""
    key = f"{kind}_pattern"
    for sc in scopes:
        if isinstance(sc, dict) and sc.get(key):
            return sc[key]
    return CAMPAIGN.get(key)


def _pat(kind, *scopes):
    """Truth csv glob for kind in ('target', 'interceptor'): explicit > campaign > legacy."""
    return _pat_explicit(kind, *scopes) or (LEGACY_TARGET if kind == "target" else LEGACY_INTERCEPTOR)


def _meta_antenna(dump):
    """Antenna origin [lat, lon, hae] from a dump's meta.json (every key spelling), or None."""
    try:
        meta = json.load(open(os.path.join(dump, "meta.json")))
    except Exception:
        return None
    ant = (meta.get("antenna_origin_lat_lon_haeM") or meta.get("antenna_origin")
           or meta.get("antenna"))
    return [float(x) for x in ant[:3]] if ant and len(ant) >= 3 else None


def _local_time_arg(v, day_hint):
    """mongo-spec time -> dump_run_window --t0/--t1 text. Full 'YYYY-MM-DD HH:MM[:SS]'
    passes through; a bare 'HH:MM[:SS]' or quickdump-style 'HHMM' is put on day_hint
    (the day's id/date) so it never silently means 'today'."""
    s = str(v).strip()
    if re.fullmatch(r"\d{4}", s):
        s = f"{s[:2]}:{s[2:]}"
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        if not day_hint:
            raise ValueError(f"mongo time {v!r} has no date and the day has no id/date")
        return f"{day_hint} {s}"
    return s


def dumper_cmd(mg, day_hint=None):
    """The exact dump_run_window.py command line for one mongo spec -> (cmd, root)."""
    cmd = [sys.executable, DUMPER]
    if mg.get("host"):
        cmd += ["--host", str(mg["host"])]
    elif mg.get("mru") is not None:
        cmd += ["--mru", str(int(mg["mru"]))]
    else:
        raise ValueError("mongo spec needs 'mru' or 'host'")
    cmd += ["--run", str(mg.get("run") or "latest")]
    if mg.get("jobs"):
        cmd += ["--jobs", str(mg["jobs"])]
    elif mg.get("t0") is not None and mg.get("t1") is not None:
        cmd += ["--t0", _local_time_arg(mg["t0"], day_hint), "--t1", _local_time_arg(mg["t1"], day_hint)]
    elif mg.get("t0_pdt") and mg.get("t1_pdt"):          # old quickdump-style keys
        cmd += ["--t0", _local_time_arg(mg["t0_pdt"], day_hint),
                "--t1", _local_time_arg(mg["t1_pdt"], day_hint)]
    else:
        raise ValueError("mongo spec needs 'jobs': 'A-B' or 't0'/'t1'")
    if mg.get("label"):
        cmd += ["--label", str(mg["label"])]
    root = os.path.abspath(mg.get("root") or CAMPAIGN["dump_root"]
                           or os.path.join(CAMPAIGN["out_root"] or ".", "dumps"))
    cmd += ["--root", root, "--tz", str(mg.get("tz") or CAMPAIGN["tz"])]
    gn = mg.get("geoid_n", CAMPAIGN["geoid_n"])
    if gn is not None:
        cmd += ["--geoid-n", str(gn)]
    if mg.get("antenna"):
        cmd += ["--antenna", ",".join(str(x) for x in mg["antenna"])]
    for flag in ("no_adsb", "no_obs", "overwrite"):
        if mg.get(flag):
            cmd += ["--" + flag.replace("_", "-")]
    for k in ("port", "db", "chunk_s", "max_track_range_m"):
        if mg.get(k) is not None:
            cmd += ["--" + k.replace("_", "-"), str(mg[k])]
    return cmd, root


def run_dumper(mg, day_hint=None):
    """Run dump_run_window.py for a mongo spec (streaming its progress) and return
    the dump directory it wrote (parsed from its 'DUMP <dir>' / 'exists: <dir>' line;
    derived from <root>/<day>/<run8>_<label> when the line is missing)."""
    cmd, root = dumper_cmd(mg, day_hint)
    print("[mongo] dump_run_window:", " ".join(cmd), flush=True)
    out_dir, lines = None, []
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        line = line.rstrip("\n")
        if not line.strip() or "streamlit" in line:
            continue
        lines.append(line)
        print("  [dump] " + line, flush=True)
        m = re.match(r"^\s*(?:DUMP|exists:)\s+(\S+)", line)
        if m:
            out_dir = m.group(1)
    rc = p.wait()
    if rc != 0:
        raise RuntimeError(f"dump_run_window.py failed rc={rc}: " + " | ".join(lines[-3:]))
    if not out_dir:
        run = str(mg.get("run") or "")
        run8 = (run[4:] if run.startswith("run_") else run)[:8]
        day = day_hint or ""
        if mg.get("t0") and re.match(r"\d{4}-\d{2}-\d{2}", str(mg["t0"])):
            day = str(mg["t0"])[:10]
        out_dir = os.path.join(root, day, f"{run8}_{mg.get('label', '')}".rstrip("_"))
        print(f"  [dump] no DUMP line parsed — assuming {out_dir}")
    if not os.path.isdir(os.path.join(out_dir, "mavlink")):
        raise FileNotFoundError(f"dumper finished but {out_dir}/mavlink is missing")
    return out_dir


def ensure_dump(obj, day=None, tag="day"):
    """Resolve the dump directory of obj (a day / prep / engagement / rollup-day
    dict): 'dump' wins; else its own 'mongo' spec, else the enclosing day's, is
    dumped ONCE with dump_run_window.py and the directory is written back to
    obj['dump'] (and cached on the spec so several engagements share one dump)."""
    if obj.get("dump"):
        return obj["dump"]
    mg = obj.get("mongo") or (day or {}).get("mongo")
    if not mg:
        raise KeyError(f"{tag}: neither 'dump' nor 'mongo' given")
    if not mg.get("_dump"):
        src = day or obj
        hint = src.get("date") or (src.get("id") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(src.get("id", ""))) else None)
        mg["_dump"] = run_dumper(mg, hint)
    obj["dump"] = mg["_dump"]
    return obj["dump"]


def _resolve_ant(day, dump, tag):
    """Antenna origin for a day: explicit config first; else the dump's meta.json
    (loudly); else the tracking module's MRU91 default (very loudly)."""
    ant = day.get("ant") or day.get("init", {}).get("ant")
    if ant:
        return tuple(float(x) for x in ant)
    ant = _meta_antenna(dump) if dump else None
    if ant:
        print(f"  [WARN] {tag}: no antenna in the config — using meta.json antenna {ant} from {dump}")
        return tuple(ant)
    print(f"  [WARN] {tag}: NO antenna in the config or in a dump meta.json — falling back to "
          f"tracking_tab.DEFAULT_ANT {TT.DEFAULT_ANT} (MRU91 8/26). Set init.ant / ant for a new site!")
    return tuple(TT.DEFAULT_ANT)


def tracking_day_tabs(day, day_dir):
    dump = None
    mdir = day.get("mdir")
    spec = None
    tag = f"{day.get('id', '?')} (tracking)"
    if not mdir:
        pr = day["prep"]
        dump = day.get("dump") or pr.get("dump") or ensure_dump(pr if pr.get("mongo") else day, day, tag)
        ant = _resolve_ant(day, dump, tag)
        ant_hae = pr.get("ant_hae_m")
        if ant_hae is None:
            ant_hae = ant[2]
            print(f"  [note] {tag}: prep.ant_hae_m not set — using ant[2] = {ant_hae}")
        elif abs(float(ant_hae) - ant[2]) > 0.5:
            print(f"  [WARN] {tag}: prep.ant_hae_m {ant_hae} differs from ant[2] {ant[2]} — truth altitude will be off")
        flights = [(ET.epoch_pdt(d, a), ET.epoch_pdt(d, b)) for d, a, b in pr["flights_pdt"]]
        tpat = _pat("target", pr, day)
        mdir = TT.prep_from_dump(dump, pr.get("out_mdir", f"{day_dir}/mission_inputs"),
                                 tpat, flights, float(ant_hae))
        spec = {"gate_m": pr.get("gate_m", 350.0)}
        if pr.get("laps_pdt"):
            # optional lap windows {"<flight>,<lap>": [date, hms, hms]} -> tracking_tab
            # RUNS, so a prep day gets "Flight N · Lap N" tabs like the served 8/26;
            # without them every flight is one lap (F<n>R1)
            spec["runs"] = {k: (ET.epoch_pdt(d, a), ET.epoch_pdt(d, b))
                            for k, (d, a, b) in pr["laps_pdt"].items()}
        day.setdefault("init", {}).setdefault("ant", list(ant))
    kw = {"tz": CAMPAIGN["tz"]}
    init = day.get("init", {})
    if "day" in init: kw["day"] = tuple(init["day"])
    if "ant" in init:
        kw["ant"] = tuple(init["ant"])
    else:
        print(f"  [WARN] {tag}: init.ant not set — tracking_tab.DEFAULT_ANT {TT.DEFAULT_ANT} "
              f"(MRU91 8/26) is used for the truth frame")
    if "geoid_n" in init: kw["geoid_n"] = init["geoid_n"]
    TT.init_mission(mdir, spec=spec, **kw)
    tabs = [("Summary", TT.build_summary_tab(notes_html=day.get("notes_html", "")))]
    for k in day.get("flight_keys", []):
        m = re.fullmatch(r"F(\d+)R(\d+)", k)
        lbl = f"Flight {m.group(1)} · Lap {m.group(2)}" if m else k
        try:
            tabs.append((lbl, TT.build_flight_tab(k)))
        except Exception as e:
            print(f"  flight {k} skipped: {e}")
            tabs.append((lbl, f'<p class="cap">flight pane {lbl}: not available ({e})</p>'))
    return tabs


def engagement_day_tabs(day, day_dir):
    tabs = []
    if day.get("overview_html"):
        tabs.append(("Overview", day["overview_html"]))
    date = day["date"]
    for i, eng in enumerate(day["engagements"], start=1):
        label = eng.get("tab_label", f"Engagement {i}")
        try:
            dump = ensure_dump(eng if (eng.get("dump") or eng.get("mongo")) else day, day,
                               f"{day.get('id')} / {eng.get('name', label)}")
            ant = _resolve_ant(day, dump, f"{day.get('id')} / {eng.get('name', label)}")
            t0 = ET.epoch_pdt(date, eng["window_pdt"][0])
            t1 = ET.epoch_pdt(date, eng["window_pdt"][1])
            tgt = eng["target"]
            ipat = _pat("interceptor", eng, day)
            tpat = tgt.get("pattern") or _pat("target", eng, day)
            target = (ET.load_traj_csv_truth(tgt["traj_csv"], ant)
                      if "traj_csv" in tgt else
                      ET.load_dump_truth(dump, tpat, t0, t1))
            trk13 = ET.load_dump_track13(dump, eng["track_id"])
            cfg = dict(name=eng["name"],
                       interceptor=ET.E.drop_frozen(ET.load_dump_truth(dump, ipat, t0, t1)),
                       target=target, track=trk13[:, :4], track_id=eng["track_id"],
                       trkV=trk13, cpa_seed=ET.epoch_pdt(date, eng["cpa_seed_pdt"]),
                       fov=eng.get("fov"), ant=ant,
                       pre=eng.get("pre", 18.0), post=eng.get("post", 5.0),
                       err_pre=eng.get("err_pre", 12.0), err_post=eng.get("err_post", 12.0),
                       # legend ids = the truth files actually loaded
                       interceptor_label=eng.get("interceptor_label")
                                         or ET.id_label(ET.truth_ids(dump, ipat), "14551"),
                       target_label=eng.get("target_label") or tgt.get("label")
                                    or ET.id_label(ET.truth_ids(dump, tpat), "14550"),
                       gif_name=eng.get("gif", f"figs/eng{i}_3d.gif"),
                       summary_html=eng.get("summary_html",
                           "<p>Interceptor closest approach: <b>{cpa_truth:.0f} m to target "
                           "truth</b>, <b>{cpa_track:.0f} m to radar track {track_id}</b>.</p>"))
            tabs.append((label, ET.build_engagement_tab(cfg, day_dir) + eng.get("post_html", "")))
        except Exception as e:
            print(f"  {eng.get('name', label)} skipped: {e}")
            tabs.append((label, f"<h3>{eng.get('name', label)}</h3><p class='cap'>not buildable from "
                                f"archived data ({e})</p>" + eng.get("post_html", "")))
    return tabs


def _rr_day_samples(dirs, target_pattern=None, interceptor_pattern=None):
    """Moving-target-truth samples for one day (merged across its dump dirs),
    with active/turning flags — the toxic_zones standard gates throughout:
    target-side tracks = median<150 m to truth; ACTIVE = a confirmed,
    measurement-updated track sample within ±0.75 s; moving = truth ≥2.5 m/s;
    TURNING = sustained truth heading rate > 6°/s. target_pattern None = the
    toxic_zones default (biggest mav14550*.csv)."""
    import toxic_zones as TZ
    out = dict(tt=[], E=[], N=[], U=[], covered=[], turning=[], paths=[], loaded=[])
    for d in dirs:
        tgt_csv, _ = TZ.pick_truth_csvs(d, target_pattern, interceptor_pattern)
        truth = TZ.TruthInterp(tgt_csv)
        tracks = TZ.load_target_tracks(d, truth)
        out["loaded"].append((d, truth, tracks))
        conf_t = [tr["t"][(~tr["coasting"]) & (tr["state"] == 2)] for tr in tracks]
        conf_t = (np.sort(np.concatenate(conf_t))
                  if conf_t and any(len(x) for x in conf_t) else np.array([]))
        mv = truth.spd >= TZ.MOVING_MPS
        tt, E, N, U = truth.t[mv], truth.E[mv], truth.N[mv], truth.U[mv]
        if len(conf_t) and len(tt):
            i = np.clip(np.searchsorted(conf_t, tt), 1, len(conf_t) - 1)
            near = np.minimum(np.abs(tt - conf_t[i - 1]), np.abs(conf_t[i] - tt))
            covered = near <= TZ.COVER_WIN_S
        else:
            covered = np.zeros(len(tt), bool)
        if len(tt) > 2:
            vE, vN = np.gradient(E, tt), np.gradient(N, tt)
            hdg = np.unwrap(np.arctan2(vE, vN))
            rate = np.abs(np.degrees(np.gradient(hdg, tt)))
            turning = (rate > 6.0) & (np.gradient(tt) < 2.0)
            if turning.any():
                # a drop that starts in the turn coasts into the straight —
                # attribute samples within ±3 s of a turn to the turn
                t_turn = tt[turning]
                i = np.clip(np.searchsorted(t_turn, tt), 1, len(t_turn) - 1)
                near = np.minimum(np.abs(tt - t_turn[i - 1]), np.abs(t_turn[i] - tt))
                turning = near <= 3.0
        else:
            turning = np.zeros(len(tt), bool)
        out["tt"].append(tt); out["E"].append(E); out["N"].append(N); out["U"].append(U)
        out["covered"].append(covered); out["turning"].append(turning)
        out["paths"].append((truth.t, truth.E, truth.N))
    for k in ("tt", "E", "N", "U", "covered", "turning"):
        out[k] = np.concatenate(out[k]) if out[k] else np.array([])
    return out


def _rr_heat_fig(cells, sat, day_paths, title, cell_m):
    """Week-combined active-track heat map over satellite terrain. Complete
    regular grid (sparse centers make plotly stretch cells); per-day flight
    pattern overlays; no antenna marker (report rule)."""
    import toxic_zones as TZ
    C = float(cell_m)
    ex = [c["E"] for c in cells] or [0.0]
    ny = [c["N"] for c in cells] or [0.0]
    xs = (np.arange(round(min(ex) / C - .5), round(max(ex) / C + .5)) * C + C / 2).tolist()
    ys = (np.arange(round(min(ny) / C - .5), round(max(ny) / C + .5)) * C + C / 2).tolist()
    xi = {round(v, 1): i for i, v in enumerate(xs)}
    yi = {round(v, 1): i for i, v in enumerate(ys)}
    z = [[None] * len(xs) for _ in ys]
    txt = [[""] * len(xs) for _ in ys]
    for c in cells:
        z[yi[round(c["N"], 1)]][xi[round(c["E"], 1)]] = round(c["cov"], 3)
        txt[yi[round(c["N"], 1)]][xi[round(c["E"], 1)]] = (
            f'active {c["cov"]*100:.0f}% · dwell {c["dwell"]} s · '
            f'alt {c["alt"]:.0f} m · {TZ.landmark(c["E"], c["N"], c["alt"])}')
    # PERCENTILE-ranked coloring (not raw %): most cells are near-perfect, so a
    # linear scale washes out — rank spreads the color range over the actual
    # distribution, and the bottom 15% of cells clamp to solid red
    covs = np.array([c["cov"] for c in cells], float)
    order = covs.argsort(kind="stable").argsort()
    rank = order / max(len(covs) - 1, 1)
    for c, r in zip(cells, rank):
        c["rank"] = float(r)
    for c in cells:
        z[yi[round(c["N"], 1)]][xi[round(c["E"], 1)]] = round(c["rank"], 3)
        txt[yi[round(c["N"], 1)]][xi[round(c["E"], 1)]] = (
            f'active {c["cov"]*100:.0f}% (p{c["rank"]*100:.0f} of cells) · '
            f'dwell {c["dwell"]} smp · alt {c["alt"]:.0f} m · '
            f'{TZ.landmark(c["E"], c["N"], c["alt"])}')
    heat = {"type": "heatmap", "x": xs, "y": ys, "z": z, "text": txt,
            "name": "active",
            "hovertemplate": "E %{x:.0f} m, N %{y:.0f} m<br>%{text}<extra></extra>",
            "hoverongaps": False, "zmin": 0, "zmax": 1, "opacity": 0.96,
            "colorscale": [[0.0, "#e11d2e"], [0.15, "#e11d2e"], [0.40, "#ff8c00"],
                           [0.62, "#ffe14d"], [0.82, "#6ad0e8"], [1.0, "#1f6fd0"]],
            "colorbar": {"title": {"text": "percentile"}, "tickformat": ".0%",
                         "outlinewidth": 0, "thickness": 14, "len": 0.7}}
    data = [heat]
    pal = ["#ff8a80", "#ffd166", "#4dd0e1", "#c792ea"]
    for i, (lbl, (t, E, N)) in enumerate(day_paths):
        decim = max(1, len(t) // 6000)
        x, y = TZ._gap_line(np.asarray(t), np.asarray(E), np.asarray(N), decim)
        data.append({"type": "scatter", "mode": "lines", "name": f"{lbl} target path",
                     "x": x, "y": y, "line": {"color": pal[i % len(pal)], "width": 1.1},
                     "opacity": 0.55, "hoverinfo": "skip", "connectgaps": False})
    lay = {"title": {"text": title, "font": {"size": 15}},
           "paper_bgcolor": "white", "plot_bgcolor": "#1c2126",
           "font": {"size": 12},
           "xaxis": {"title": {"text": "East of antenna (m)"}, "gridcolor": "#3a3f45",
                     "zeroline": False},
           "yaxis": {"title": {"text": "North of antenna (m)"}, "gridcolor": "#3a3f45",
                     "zeroline": False, "scaleanchor": "x", "scaleratio": 1},
           "legend": {"orientation": "h", "y": -0.10},
           "margin": {"l": 70, "r": 20, "t": 48, "b": 60}, "height": 760}
    if sat:
        lay["images"] = [{"source": sat["img"], "xref": "x", "yref": "y",
                          "x": sat["x0"], "y": sat["y1"],
                          "sizex": sat["x1"] - sat["x0"], "sizey": sat["y1"] - sat["y0"],
                          "xanchor": "left", "yanchor": "top",
                          "sizing": "stretch", "layer": "below", "opacity": 1.0}]
    return {"data": data, "layout": lay}


def _rr_satmap(meta, x0, x1, y0, y1):
    """Esri satellite payload for an EN box, with a retry (transient fetches)."""
    for attempt in (1, 2):
        try:
            import live_correlator as LC
            ant = (meta.get("antenna_origin_lat_lon_haeM")
                   or meta.get("antenna_origin") or meta.get("antenna"))
            LC.ANT_LL[0] = tuple(ant[:2])
            pl = LC._satmap_payload(x0, x1, y0, y1)
            if pl and "img" in pl:
                return pl
            print(f"  [radar] satmap attempt {attempt}: {pl}")
        except Exception as e:
            print(f"  [radar] satmap attempt {attempt} error: {e!r}")
    print("  [radar] satellite tiles unavailable — plain background")
    return None


def _rr_lighten(fig):
    """The toxic_zones figures are dark-page styled; re-paper them for the
    light report page (plot area stays dark under the satellite imagery)."""
    lay = fig["layout"]
    lay["paper_bgcolor"] = "white"
    lay["font"] = {"color": "#444", "size": 12}
    if "title" in lay and isinstance(lay["title"], dict):
        lay["title"].setdefault("font", {})["color"] = "#222"
    lay["legend"] = {"orientation": "h", "y": -0.12}
    return fig


def _rr_az_bias(loaded):
    """Per-day azimuth bias, the tracking-module way: median signed angular
    residual (track azimuth − truth azimuth) over every target-side track
    sample. Re-reads only the kept tracks' csvs (cheap)."""
    import pandas as _pd
    difs = []
    for d, truth, tracks in loaded:
        for tr in tracks:
            fp = os.path.join(d, "tracks", "track_" + tr["name"].replace("trk ", "") + ".csv")
            df = _pd.read_csv(fp, usecols=["t_epoch", "E_m", "N_m"])
            t = df["t_epoch"].to_numpy(float)
            tE, tN, _, ok = truth.query(t)
            az_k = np.degrees(np.arctan2(df["E_m"].to_numpy(float), df["N_m"].to_numpy(float)))
            az_t = np.degrees(np.arctan2(tE, tN))
            dif = (az_k - az_t + 180.0) % 360.0 - 180.0
            difs.append(dif[ok])
    dif = np.concatenate(difs) if difs else np.array([])
    return float(np.median(dif)) if len(dif) else float("nan")


def _rr_steal_gif(sv, root):
    """Animated 2-D track-steal view over satellite imagery, ONE code path for
    every steal: interceptor (blue) and target (orange) with velocity arrows
    and fading trails, the stolen track (green diamond + trail) hopping sides,
    t−10 … t+10 s around the steal at 5 fps real-time. Config carries only
    data: dump, date, steal time, track id, antenna origin."""
    import datetime as _dt
    import io
    import base64
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    tid = sv["track_id"]
    gif = sv.get("gif", f"figs/steal_{tid}.gif")
    banner = sv.get("banner", "TRACK STEAL")
    if os.path.exists(os.path.join(root, gif)):
        print(f"  [steal] reusing {gif} (delete to re-render)")
        return gif
    dump = ensure_dump(sv, None, f"steal view {sv.get('label', tid)}")
    ts0 = ET.epoch_pdt(sv["date"], sv["t_steal_pdt"])
    w0, w1 = ts0 - 10.0, ts0 + 10.0
    inter = ET.E.drop_frozen(ET.load_dump_truth(dump, _pat("interceptor", sv), w0 - 15, w1 + 15))
    targ = ET.E.drop_frozen(ET.load_dump_truth(dump, _pat("target", sv), w0 - 15, w1 + 15))
    trk = ET.load_dump_track13(dump, tid)[:, :4]
    trk = trk[(trk[:, 0] >= w0 - 2) & (trk[:, 0] <= w1 + 2)]
    if len(inter) < 4 or len(targ) < 4:
        print(f"  [steal] trk {tid}: too few truth samples — view skipped")
        return None
    # zoom to the DISPLAY window only (the load pad would triple the box)
    gg = np.arange(w0, w1 + 1e-6, 0.5)
    allE = np.concatenate([np.interp(gg, inter[:, 0], inter[:, 1]),
                           np.interp(gg, targ[:, 0], targ[:, 1])]
                          + ([trk[:, 1]] if len(trk) else []))
    allN = np.concatenate([np.interp(gg, inter[:, 0], inter[:, 2]),
                           np.interp(gg, targ[:, 0], targ[:, 2])]
                          + ([trk[:, 2]] if len(trk) else []))
    x0, x1 = float(allE.min()) - 60, float(allE.max()) + 60
    y0, y1 = float(allN.min()) - 60, float(allN.max()) + 60
    # keep the box near-square so the satellite image isn't stretched
    side = max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x0, x1, y0, y1 = cx - side / 2, cx + side / 2, cy - side / 2, cy + side / 2
    ant = sv.get("ant") or _meta_antenna(dump)
    if not sv.get("ant"):
        print(f"  [steal] trk {tid}: no 'ant' in the view — satellite tile georeference from "
              f"{'meta.json ' + str(ant) if ant else 'NOWHERE (plain background)'}")
    sat = _rr_satmap({"antenna_origin_lat_lon_haeM": ant}, x0, x1, y0, y1) if ant else None

    PDTZ = ET.TZ                                       # campaign tz
    fig, ax = plt.subplots(figsize=(7.4, 6.9), dpi=90)
    fig.patch.set_facecolor("#fcfcfb")
    if sat:
        img = Image.open(io.BytesIO(base64.b64decode(sat["img"].split(",", 1)[1])))
        ax.imshow(img, extent=[sat["x0"], sat["x1"], sat["y0"], sat["y1"]],
                  origin="upper", zorder=0)
    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
    ax.set_xlabel("E, m"); ax.set_ylabel("N, m")
    ax.set_title(sv["label"], fontsize=12.5, fontweight="600", loc="left")
    ax.tick_params(labelsize=9)

    S1, S2, S3, TERM = "#1e6fe0", "#ff7a00", "#00d466", "#ff2d2d"
    import matplotlib.patheffects as _pe
    halo = [_pe.withStroke(linewidth=3, foreground="white")]
    tr_i, = ax.plot([], [], color=S1, lw=3.0, alpha=0.95, zorder=3)
    tr_t, = ax.plot([], [], color=S2, lw=3.0, alpha=0.95, zorder=3)
    tr_k, = ax.plot([], [], color=S3, lw=2.8, ls=":", alpha=1.0, zorder=4)
    m_i, = ax.plot([], [], "o", ms=12, color=S1, mec="white", mew=2.0, zorder=6)
    m_t, = ax.plot([], [], "o", ms=12, color=S2, mec="white", mew=2.0, zorder=6)
    m_k, = ax.plot([], [], "D", ms=13, mfc="none", mec=S3, mew=3.2, zorder=7)
    q_i = ax.quiver([0], [0], [0], [0], color=S1, scale=1, scale_units="xy",
                    angles="xy", width=0.006, zorder=5)
    q_t = ax.quiver([0], [0], [0], [0], color=S2, scale=1, scale_units="xy",
                    angles="xy", width=0.006, zorder=5)
    lb_i = ax.annotate("interceptor", (0, 0), textcoords="offset points", xytext=(10, 10),
                       fontsize=11, fontweight="700", color=S1, zorder=8,
                       path_effects=halo)
    lb_t = ax.annotate("target", (0, 0), textcoords="offset points", xytext=(10, -14),
                       fontsize=11, fontweight="700", color=S2, zorder=8,
                       path_effects=halo)
    lb_k = ax.annotate(f"trk {tid}", (0, 0), textcoords="offset points", xytext=(-12, 12),
                       ha="right", fontsize=11, fontweight="700", color=S3, zorder=8,
                       path_effects=halo)
    clock = ax.annotate("", (0.02, 0.975), xycoords="axes fraction", fontsize=11,
                        fontweight="600", color="white", va="top",
                        bbox=dict(boxstyle="round,pad=0.25", fc="black", alpha=0.55))
    flash = ax.annotate(banner, (0.5, 0.90), xycoords="axes fraction",
                        ha="center", fontsize=19, fontweight="800", color=TERM,
                        path_effects=halo)
    flash.set_visible(False)

    def pos(a, t):
        return (float(np.interp(t, a[:, 0], a[:, 1])), float(np.interp(t, a[:, 0], a[:, 2])))

    def vel(a, t):
        return (float(np.interp(t + 0.5, a[:, 0], a[:, 1]) - np.interp(t - 0.5, a[:, 0], a[:, 1])),
                float(np.interp(t + 0.5, a[:, 0], a[:, 2]) - np.interp(t - 0.5, a[:, 0], a[:, 2])))

    frames = []
    grid = np.arange(w0, w1 + 1e-6, 0.2)          # 101 frames @ 5 fps = 1x real-time
    for t in grid:
        for a, tr_, m_, q_, lb_ in ((inter, tr_i, m_i, q_i, lb_i),
                                    (targ, tr_t, m_t, q_t, lb_t)):
            h = a[(a[:, 0] >= t - 6.0) & (a[:, 0] <= t)]
            tr_.set_data(h[:, 1], h[:, 2])
            x, y = pos(a, t)
            m_.set_data([x], [y]); lb_.xy = (x, y)
            vx, vy = vel(a, t)
            q_.set_offsets([[x, y]]); q_.set_UVC([vx * 3.0], [vy * 3.0])
        if len(trk):
            hk = trk[(trk[:, 0] >= t - 6.0) & (trk[:, 0] <= t)]
            tr_k.set_data(hk[:, 1], hk[:, 2])
            j = int(np.searchsorted(trk[:, 0], t)) - 1
            if 0 <= j < len(trk) and abs(trk[j, 0] - t) < 1.2:
                m_k.set_data([trk[j, 1]], [trk[j, 2]]); lb_k.xy = (trk[j, 1], trk[j, 2])
                m_k.set_visible(True); lb_k.set_visible(True)
            else:
                m_k.set_visible(False); lb_k.set_visible(False)
        rel = t - ts0
        clock.set_text(f"{_dt.datetime.fromtimestamp(t, PDTZ).strftime('%H:%M:%S')} {ET.tz_abbr(t)} · "
                       f"{sv.get('rel_label', 'steal')} {rel:+.1f} s")
        flash.set_visible(abs(rel) <= 1.5 and int(t * 4) % 2 == 0)
        fig.canvas.draw()
        buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
        frames.append(Image.fromarray(buf.copy()))
    plt.close(fig)
    os.makedirs(os.path.join(root, "figs"), exist_ok=True)
    # shared 128-color palette, no dithering: the satellite background stays
    # pixel-identical between frames so the GIF compresses ~5x
    pal = frames[0].quantize(colors=256, method=Image.MEDIANCUT)
    frames = [f.quantize(palette=pal, dither=Image.NONE) for f in frames]
    frames[0].save(os.path.join(root, gif), save_all=True, append_images=frames[1:],
                   duration=200, loop=0, optimize=True)
    print(f"  [steal] wrote {gif} ({len(frames)} frames, 1x real-time)")
    return gif



def _rr_duel_fig(sv, root, win=30.0):
    """Distance-duel plot for one steal (the plot the static anatomy figures
    carried): the stolen track's horizontal distance to the target and to the
    interceptor, ±win s around the steal — the lines swap at the crossing."""
    dump = ensure_dump(sv, None, f"steal view {sv.get('label', sv['track_id'])}")
    ts0 = ET.epoch_pdt(sv["date"], sv["t_steal_pdt"])
    trk = ET.load_dump_track13(dump, sv["track_id"])[:, :4]
    m = (trk[:, 0] >= ts0 - win) & (trk[:, 0] <= ts0 + win)
    trk = trk[m]
    inter = ET.E.drop_frozen(ET.load_dump_truth(
        dump, _pat("interceptor", sv), ts0 - win - 10, ts0 + win + 10))
    targ = ET.E.drop_frozen(ET.load_dump_truth(
        dump, _pat("target", sv), ts0 - win - 10, ts0 + win + 10))
    if len(trk) < 3 or len(inter) < 3 or len(targ) < 3:
        return ""
    rel = (trk[:, 0] - ts0).tolist()
    dT = np.hypot(trk[:, 1] - np.interp(trk[:, 0], targ[:, 0], targ[:, 1]),
                  trk[:, 2] - np.interp(trk[:, 0], targ[:, 0], targ[:, 2]))
    dI = np.hypot(trk[:, 1] - np.interp(trk[:, 0], inter[:, 0], inter[:, 1]),
                  trk[:, 2] - np.interp(trk[:, 0], inter[:, 0], inter[:, 2]))
    fig = {"data": [
        {"type": "scatter", "mode": "lines+markers", "name": "trk → target",
         "x": rel, "y": np.round(dT, 1).tolist(),
         "line": {"color": "#ff7a00", "width": 2.2}, "marker": {"size": 4}},
        {"type": "scatter", "mode": "lines+markers", "name": "trk → interceptor",
         "x": rel, "y": np.round(dI, 1).tolist(),
         "line": {"color": "#1e6fe0", "width": 2.2}, "marker": {"size": 4}}],
        "layout": {"title": {"text": f"trk {sv['track_id']} — distance to each aircraft "
                                     f"(the swap is the steal)", "font": {"size": 13}},
                   "paper_bgcolor": "white", "plot_bgcolor": "white", "font": {"size": 12},
                   "xaxis": {"title": {"text": "time from steal (s)"}, "gridcolor": "#b6bcc6",
                             "zeroline": False},
                   "yaxis": {"title": {"text": "horizontal distance (m)"},
                             "gridcolor": "#b6bcc6", "rangemode": "tozero"},
                   "shapes": [{"type": "line", "x0": 0, "x1": 0, "yref": "paper",
                               "y0": 0, "y1": 1,
                               "line": {"color": "#c62828", "dash": "dot", "width": 1.5}}],
                   "legend": {"orientation": "h", "y": 1.12},
                   "margin": {"l": 60, "r": 20, "t": 60, "b": 45}, "height": 330}}
    return ET.div(fig)


def _rr_turn_zoom_fig(tz, root):
    """ONE figure of JUST the turn for the drops-on-turns section: truth path
    through the turn, the dying track thick with an X at the death point, the
    successor track picking up. Zoomed to the turn only."""
    import datetime as _dt
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t0 = ET.epoch_pdt(tz["date"], tz["t_pdt"])
    win = float(tz.get("win_s", 45.0))
    dump = ensure_dump(tz, None, f"turn zoom {tz.get('label', '')}")
    targ = ET.load_dump_truth(dump, _pat("target", tz), t0 - win, t0 + win)
    ps = ET.hms_local                                  # campaign tz
    abbr = ET.tz_abbr(t0)
    SURF, INK, INK2, MUTED, GRIDC, BASE = ("#fcfcfb", "#0b0b0b", "#52514e",
                                           "#898781", "#98a0ac", "#7f8791")
    rc = {"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
          "axes.edgecolor": BASE, "axes.labelcolor": INK2, "axes.grid": True,
          "grid.color": GRIDC, "grid.linewidth": 1.1, "xtick.color": MUTED,
          "ytick.color": MUTED, "text.color": INK, "font.size": 11.5,
          "axes.titlesize": 12.5, "axes.titleweight": "600",
          "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False}
    pal = ["#8e24aa", "#c62828", "#1a7f37"]
    with plt.rc_context(rc):
        fig, ax = plt.subplots(figsize=(9.5, 7.2))
        ax.plot(targ[:, 1], targ[:, 2], color="#4a76c9", lw=1.6, label="target truth")
        for i, tid in enumerate(tz["tracks"]):
            trk = ET.load_dump_track13(dump, tid)[:, :4]
            m = (trk[:, 0] >= t0 - win) & (trk[:, 0] <= t0 + win)
            trk = trk[m]
            if not len(trk):
                continue
            died = trk[-1, 0] < t0 + 3.0 and i == 0
            ax.plot(trk[:, 1], trk[:, 2], color=pal[i % len(pal)], lw=4.0,
                    label=f"trk {tid}" + (f" — died {ps(trk[-1, 0])} {abbr} in the turn"
                                          if died else " (picks up)"))
            if died:
                ax.plot(trk[-1, 1], trk[-1, 2], marker="x", ms=16, mew=3.5,
                        color="#111", zorder=8)
                ax.annotate(f"trk {tid} \u2020", (trk[-1, 1], trk[-1, 2]),
                            textcoords="offset points", xytext=(14, 10), fontsize=12,
                            fontweight="700",
                            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=BASE))
        ax.set_aspect("equal")
        ax.margins(0.12)
        ax.set_xlabel("E, m"); ax.set_ylabel("N, m")
        leg = ax.legend(loc="lower right", fontsize=10, frameon=True,
                        facecolor="white", edgecolor=BASE, framealpha=0.95)
        leg.set_zorder(20)
        ax.set_title(tz.get("label", "Track drop at the turn"))
        os.makedirs(os.path.join(root, "figs"), exist_ok=True)
        fn = "figs/turn_zoom.png"
        fig.savefig(os.path.join(root, fn), dpi=130, bbox_inches="tight")
        plt.close(fig)
    return (f'<div class="center"><img src="{fn}" style="max-width:100%"></div>'
            f'<p class="cap">{tz.get("cap", "")}</p>')


def build_radar_rollup(rr, root):
    """The Radar Rollup tab: every radar issue of the campaign in one place.
    Narratives come from the config; the az-bias bars, turn/coast statistics,
    coasting/track-death overlays, examples tables, and the week heat map are
    all computed HERE from the archiver dumps (one code path, toxic_zones
    standard gates + figures), with Esri satellite terrain under the maps."""
    import json as _json
    import shutil
    import datetime as _dt
    from types import SimpleNamespace
    import toxic_zones as TZ
    CELL = float(rr.get("cell_m", 50.0))
    MINDW = int(rr.get("min_dwell", 2))
    ps = ET.hms_local                                  # campaign tz (set_tz)
    abbr = ET.tz_abbr()                                # refined below from the data

    # ---- one pass: load every scoreable day's dumps, compute everything -----
    daysD, rows = [], []
    for dspec in rr["days"]:
        lbl = dspec["label"]
        if not dspec.get("scoreable", True):
            rows.append(f'<tr><td>{lbl}</td><td colspan="4">'
                        f'{dspec.get("note", "not scoreable")}</td></tr>')
            continue
        dirs = list(dspec.get("dirs") or [])
        if not dirs and dspec.get("mongo"):
            # live-mongo rollup day: one or several dump_run_window specs
            specs = dspec["mongo"] if isinstance(dspec["mongo"], list) else [dspec["mongo"]]
            for k, mg in enumerate(specs):
                dirs.append(ensure_dump({"mongo": mg}, None, f"radar_rollup {lbl} #{k + 1}"))
            dspec["dirs"] = dirs
        if not dirs:
            rows.append(f'<tr><td>{lbl}</td><td colspan="4">no dump dirs</td></tr>')
            print(f"  [radar] {lbl}: no 'dirs' / 'mongo' — day skipped")
            continue
        try:
            S = _rr_day_samples(dirs, _pat_explicit("target", dspec, rr),
                                _pat_explicit("interceptor", dspec, rr))
        except Exception as e:
            rows.append(f'<tr><td>{lbl}</td><td colspan="4">not computable ({_html.escape(str(e))})</td></tr>')
            print(f"  [radar] {lbl}: FAILED ({e}) — day skipped in the rollup")
            continue
        meta = _json.load(open(os.path.join(dirs[0], "meta.json")))
        if len(S["tt"]):
            abbr = ET.tz_abbr(float(S["tt"][0]))
        run_lbl = f'{meta.get("run_id8", meta.get("run", "?"))}'
        if meta.get("friendly_name"):
            run_lbl += f' ({meta["friendly_name"]})'
        cov, trn = S["covered"], S["turning"]
        act = float(cov.mean()) if len(cov) else 0.0
        c_t = float((~cov[trn]).mean()) if trn.any() else float("nan")
        c_s = float((~cov[~trn]).mean()) if (~trn).any() else float("nan")
        ratio = c_t / c_s if c_s and np.isfinite(c_t) else float("nan")
        rows.append(f"<tr><td>{lbl} · {run_lbl}</td><td>{act*100:.1f}%</td>"
                    f"<td>{c_t*100:.1f}%</td><td>{c_s*100:.1f}%</td>"
                    f"<td><b>{ratio:.1f}×</b></td></tr>")
        bias = _rr_az_bias(S["loaded"])
        print(f"  [radar] {lbl} ({run_lbl}): az bias {bias:+.2f}° · active {act*100:.1f}% · "
              f"coast turns {c_t*100:.1f}% vs straight {c_s*100:.1f}% ({ratio:.1f}x)")
        daysD.append(dict(label=lbl, run=run_lbl, S=S, meta=meta, bias=bias))

    # ---- 1 · az bias: live-computed bars + config table ---------------------
    bx, by, btxt = [], [], []
    for dspec in rr["days"]:
        dd = next((d for d in daysD if d["label"] == dspec["label"]), None)
        if dd is not None and np.isfinite(dd["bias"]):
            bx.append(dspec["label"]); by.append(dd["bias"])
            btxt.append(f'{dd["bias"]:+.2f}°')
        elif dspec.get("az_bias_cfg") is not None:
            bx.append(dspec["label"] + " *"); by.append(float(dspec["az_bias_cfg"]))
            btxt.append(f'{dspec["az_bias_cfg"]:+.2f}° *')
    bias_fig = {"data": [{"type": "bar", "x": bx, "y": by, "text": btxt,
                          "textposition": "outside",
                          "marker": {"color": ["#b3261e" if abs(v) > 1 else "#1a7f37"
                                               for v in by]}}],
                "layout": {"title": {"text": "Azimuth bias by day — computed from the "
                                             "archived tracks at build time", "font": {"size": 14}},
                           "paper_bgcolor": "white", "plot_bgcolor": "white",
                           "font": {"size": 12},
                           "yaxis": {"title": {"text": "azimuth bias (°)"},
                                     "gridcolor": "#b6bcc6", "zeroline": True,
                                     "zerolinecolor": "#888"},
                           "shapes": [{"type": "line", "x0": -0.5, "x1": len(bx) - 0.5,
                                       "y0": s * 1.0, "y1": s * 1.0,
                                       "line": {"color": "#c62828", "dash": "dot", "width": 1}}
                                      for s in (-1, 1)],
                           "margin": {"l": 60, "r": 20, "t": 42, "b": 40}, "height": 320}}
    html = rr.get("intro_html", "")
    html += ("<h4>1 · Azimuth bias</h4>" + rr.get("az_bias_html", "")
             + ET.div(bias_fig)
             + '<p class="cap">Red dotted lines = the 1° reporting bar. '
               '* = from the day report (no target-side tracks in the dump to recompute).</p>')
    ex = rr.get("az_bias_example")
    if ex:
        kw = {}
        ini = ex.get("init", {})
        if "day" in ini: kw["day"] = tuple(ini["day"])
        if "ant" in ini: kw["ant"] = tuple(ini["ant"])
        if "geoid_n" in ini: kw["geoid_n"] = ini["geoid_n"]
        kw["tz"] = CAMPAIGN["tz"]
        # spec=None (default) = the legacy 8/26 mission table (only mission_20260826).
        # A prep-generated mission dir needs "generic": true (+ optional "laps_pdt"
        # {"<flight>,<lap>": [date, hms, hms]} and "gate_m"), like a tracking day.
        ex_spec = None
        if ex.get("generic") or ex.get("laps_pdt"):
            ex_spec = {"gate_m": ex.get("gate_m", 350.0)}
            if ex.get("laps_pdt"):
                ex_spec["runs"] = {k: (ET.epoch_pdt(d, a), ET.epoch_pdt(d, b))
                                   for k, (d, a, b) in ex["laps_pdt"].items()}
        TT.init_mission(ex["mdir"], spec=ex_spec, **kw)
        fl, rn = int(ex.get("flight", 1)), int(ex.get("run", 2))
        html += (TT.div(TT.maps_fig(fl, TT.RUNS[(fl, rn)], TT.color_of(fl)), 470)
                 + f'<p class="cap">{ex.get("caption_html", "")}</p>')

    # ---- 2 · turns: stats table + existing coasting/deaths overlays + examples
    html += ("<h4>2 · Track drops on turns</h4>" + rr.get("turns_html", "")
             + '<table class="st"><tr><td><b>day · run</b></td><td><b>active (moving target)</b></td>'
               '<td><b>coasting in turns</b></td><td><b>coasting straight</b></td>'
               '<td><b>turn penalty</b></td></tr>' + "".join(rows) + "</table>"
             + '<p class="cap">Computed from the archived dumps at build time — '
               'active = a confirmed, measurement-updated target-side track within '
               '±0.75 s; turning = sustained truth heading rate &gt; 6°/s (±3 s); '
               'hover/ground samples (&lt;2.5 m/s) excluded.</p>')
    # ONE figure of JUST the turn: the dying track + its successor, zoomed
    if rr.get("turn_zoom"):
        html += _rr_turn_zoom_fig(rr["turn_zoom"], root)

    # far-turn drops only (the N/E far edges are the ones that matter):
    # track deaths inside a turn, >=2.5 km out, while the target kept flying
    death_rows = []
    for dd in daysD:
        S = dd["S"]
        dt_med = float(np.median(np.diff(S["tt"]))) if len(S["tt"]) > 1 else 1.0
        for d_, truth, trks in S["loaded"]:
            for de in TZ.track_deaths(trks, truth):
                j = int(np.argmin(np.abs(S["tt"] - de["t"]))) if len(S["tt"]) else -1
                in_turn = bool(S["turning"][j]) if (j >= 0 and
                           abs(float(S["tt"][j]) - de["t"]) <= 3.0) else False
                far = float(np.hypot(de["E"], de["N"])) >= 2500.0
                mv_after = np.count_nonzero((S["tt"] > de["t"]) & (S["tt"] <= de["t"] + 20.0))
                if in_turn and far and mv_after * dt_med >= 12.0:
                    death_rows.append((de["gap"], dd["label"], dd["run"], de["name"],
                                       ps(de["t"]), TZ.landmark(de["E"], de["N"], de["U"])))
    death_rows.sort(key=lambda r: -r[0])
    ex_rows = "".join(
        f"<tr><td>{lb}</td><td>{run}</td><td><b>{nm}</b></td><td>{tt} {abbr}</td>"
        f"<td>{wh}</td><td>{gp:.1f} s</td></tr>"
        for gp, lb, run, nm, tt, wh in death_rows[:8])
    html += ('<p><b>Far-turn drops to investigate</b> (track deaths inside the far N/E '
             'turns, ≥2.5 km out, while the target kept flying):</p>'
             '<table class="st"><tr><td><b>day</b></td><td><b>run</b></td><td><b>track</b></td>'
             '<td><b>died at</b></td><td><b>where</b></td><td><b>untracked</b></td></tr>'
             + ex_rows + "</table>")

    # ---- 3 · track corruption: 2-D pass view + CPA error window -------------
    html += "<h4>3 · Track corruption at close passes</h4>" + rr.get("corruption_html", "")
    for cv in rr.get("corruption_views", []):
        gif = _rr_steal_gif(cv, root)
        if gif:
            html += (f'<div class="center"><img src="{gif}" style="max-width:100%"></div>'
                     f'<p class="cap">{cv.get("cap", "")}</p>')
        tcv = ET.epoch_pdt(cv["date"], cv["t_steal_pdt"])
        cv_dump = ensure_dump(cv, None, f"corruption view {cv.get('label', cv['track_id'])}")
        trkV = ET.load_dump_track13(cv_dump, cv["track_id"])
        targ_cv = ET.load_dump_truth(cv_dump, _pat("target", cv), tcv - 60, tcv + 60)
        f_ew = ET.E.err_window_fig(trkV, targ_cv, tcv, pre=12.0, post=12.0,
                                   title=f"trk {cv['track_id']} metrics through the pass — "
                                         f"az/el/range/alt error, filter ±1σ / ±3σ")
        if f_ew is not None:
            html += ET.div(f_ew)

    # ---- 4 · steals: narrative + animated views + duel plots ----------------
    html += "<h4>4 · Track steals on close passes</h4>" + rr.get("steals_html", "")
    if rr.get("steal_examples"):
        html += ('<table class="st"><tr><td><b>day</b></td><td><b>run</b></td>'
                 f'<td><b>track</b></td><td><b>time ({abbr})</b></td><td><b>what happened</b></td></tr>'
                 + "".join(f"<tr><td>{d}</td><td>{r}</td><td><b>{k}</b></td>"
                           f"<td>{t}</td><td>{w}</td></tr>"
                           for d, r, k, t, w in rr["steal_examples"]) + "</table>")
    for sv in rr.get("steal_views", []):
        gif = _rr_steal_gif(sv, root)
        if gif:
            html += (f'<div class="center"><img src="{gif}" style="max-width:100%"></div>'
                     f'<p class="cap">{sv.get("cap", "")}</p>')
        html += _rr_duel_fig(sv, root)

    # ---- 4 · week heat map (fine cells) + worst-cell examples ---------------
    week_cells, day_paths, cell_day, cell_trk = {}, [], {}, {}
    for dd in daysD:
        S = dd["S"]
        ix = np.floor(S["E"] / CELL).astype(int)
        iy = np.floor(S["N"] / CELL).astype(int)
        for j in range(len(S["covered"])):
            k = (int(ix[j]), int(iy[j]))
            tot, cv, us = week_cells.get(k, (0, 0, 0.0))
            week_cells[k] = (tot + 1, cv + int(S["covered"][j]), us + float(S["U"][j]))
            dt_, dc_ = cell_day.setdefault(k, {}).get(dd["label"], (0, 0))
            cell_day[k][dd["label"]] = (dt_ + 1, dc_ + int(S["covered"][j]))
        for d_, truth, trks in S["loaded"]:
            for tr in trks:
                m = tr["coasting"] & tr["ok"]
                cx = np.floor(tr["tE"][m] / CELL).astype(int)
                cy = np.floor(tr["tN"][m] / CELL).astype(int)
                for a, b in zip(cx, cy):
                    kk = (int(a), int(b))
                    key = (dd["label"], tr["name"])
                    cell_trk.setdefault(kk, {})[key] = cell_trk.get(kk, {}).get(key, 0) + 1
        t_all = np.concatenate([p[0] for p in S["paths"]])
        e_all = np.concatenate([p[1] for p in S["paths"]])
        n_all = np.concatenate([p[2] for p in S["paths"]])
        day_paths.append((dd["label"], (t_all, e_all, n_all)))

    # fine cells define the footprint (they hug the truth line), but each cell
    # is SCORED over its 5x5 bin neighborhood (±2 bins ≈ ±75 m at 30 m cells) —
    # per-cell dwell is too small to score alone and rank-colors as noise
    W = 2
    cells = []
    for k, (tot, cv, us) in week_cells.items():
        if tot < MINDW:
            continue
        stot = scv = 0
        for dx in range(-W, W + 1):
            for dy in range(-W, W + 1):
                t2, c2, _ = week_cells.get((k[0] + dx, k[1] + dy), (0, 0, 0.0))
                stot += t2; scv += c2
        cells.append(dict(key=k, E=(k[0] + .5) * CELL, N=(k[1] + .5) * CELL,
                          cov=scv / max(stot, 1), dwell=stot, alt=us / tot))
    sat = None
    if cells and daysD:
        ex = [c["E"] for c in cells]; ny = [c["N"] for c in cells]
        sat = _rr_satmap(daysD[0]["meta"], min(ex) - 300, max(ex) + 300,
                         min(ny) - 300, max(ny) + 300)
    html += ("<h4>5 · Where tracks go bad — week heat map</h4>"
             + rr.get("heatmap_html", "")
             + ET.div(_rr_heat_fig(cells, sat, day_paths,
                      f"Active-track percentage around the flight pattern — "
                      f"all scoreable days combined ({CELL:.0f} m cells)", CELL))
             + ("" if (sat or not cells) else
                '<p class="cap">Satellite imagery unavailable at build time (no HTTPS to the '
                'Esri World_Imagery tiles) — heat map drawn on a plain background.</p>'))
    # worst cells with NON-MAX SUPPRESSION: the bad cells cluster, so without a
    # spatial separation gate every row describes the same pocket (and the same
    # long-coasting track dominates all of their windows)
    worst = []
    for c in sorted((c for c in cells if c["dwell"] >= 8 * MINDW),
                    key=lambda c: (c["cov"], -c["dwell"])):
        if all(np.hypot(c["E"] - w0["E"], c["N"] - w0["N"]) >= 300.0 for w0 in worst):
            worst.append(c)
        if len(worst) == 6:
            break
    wrows = []
    for c in worst:
        per, tkc = {}, {}
        for dx in range(-W, W + 1):
            for dy in range(-W, W + 1):
                kk = (c["key"][0] + dx, c["key"][1] + dy)
                for d, (t, cv) in cell_day.get(kk, {}).items():
                    a, b = per.get(d, (0, 0)); per[d] = (a + t, b + cv)
                for key2, n in cell_trk.get(kk, {}).items():
                    tkc[key2] = tkc.get(key2, 0) + n
        per_day = " · ".join(f"{d} {cv/t*100:.0f}%" for d, (t, cv) in sorted(per.items()))
        tk = sorted(tkc.items(), key=lambda kv: -kv[1])[:2]
        tk_txt = (" · ".join(f"{d0} <b>{n0}</b> ({n1} smp)" for (d0, n0), n1 in tk)
                  if tk else "—")
        wrows.append(f'<tr><td>{TZ.landmark(c["E"], c["N"], c["alt"])}</td>'
                     f'<td>E {c["E"]:+.0f} / N {c["N"]:+.0f}</td>'
                     f'<td>{c["cov"]*100:.0f}%</td><td>{c["dwell"]}</td>'
                     f'<td>{per_day}</td><td>{tk_txt}</td></tr>')
    html += ('<p><b>Worst cells — where to look</b> (top coasting track named per cell):</p>'
             '<table class="st"><tr><td><b>where</b></td><td><b>cell (m)</b></td>'
             '<td><b>active</b></td><td><b>dwell (smp)</b></td><td><b>per-day active</b></td>'
             '<td><b>top coasting tracks</b></td></tr>' + "".join(wrows) + "</table>")
    return html


def main(cfg_path):
    cfg = json.load(open(cfg_path))
    root = cfg["out_root"]
    os.makedirs(root, exist_ok=True)
    configure(cfg)
    print(f"=== campaign tz {CAMPAIGN['tz']} · target pattern "
          f"{CAMPAIGN['target_pattern'] or LEGACY_TARGET + ' (legacy default)'} · interceptor pattern "
          f"{CAMPAIGN['interceptor_pattern'] or LEGACY_INTERCEPTOR + ' (legacy default)'}")
    cards = []
    for day in cfg["days"]:
        did = day["id"]
        day_dir = os.path.join(root, did)
        os.makedirs(day_dir + "/figs", exist_ok=True)
        print(f"=== {did} ({day['type']})")
        try:
            tabs = (tracking_day_tabs(day, day_dir) if day["type"] == "tracking"
                    else engagement_day_tabs(day, day_dir))
        except Exception as e:
            # a broken day (bad dump path, dumper failure, wrong pattern) must not
            # take the campaign down: stub the page, keep building the others
            import traceback
            print(f"  DAY {did} FAILED: {e!r}")
            traceback.print_exc(limit=3)
            tabs = [("Error", f'<p class="cap">day not buildable: {_html.escape(repr(e))}</p>')]
        ET.write_page(f"{day_dir}/report.html", day["title"], day.get("sub", ""), tabs)
        # card: link text = title up to the parenthetical (so it never wraps
        # mid-phrase); the parenthetical joins the sub-line with the tab count
        title = day["title"]
        if " (" in title and title.endswith(")"):
            link_txt, desc = title[:title.index(" (")], title[title.index(" (") + 2:-1]
        else:
            link_txt, desc = title, day["type"]
        cards.append(
            f'<tr><td style="white-space:nowrap">'
            f'<a href="{did}/report.html"><b>{_html.escape(link_txt)}</b></a>'
            f'<br><span class="cap">{_html.escape(desc)} · {len(tabs)} tabs</span></td>'
            f'<td>{day.get("key_message_html", "")}</td>'
            f'<td>{day.get("numbers_html", "")}</td></tr>')
    rollup = (f"<h3>Days</h3><table class='st'>"
              f"<tr><td><b>Day report</b></td><td><b>Key message</b></td>"
              f"<td><b>Numbers</b></td></tr>{''.join(cards)}</table>"
              + cfg.get("rollup_html", ""))
    tabs = [("Overall Rollup", rollup)]
    radar_html = None
    if cfg.get("radar_rollup"):
        print("=== radar rollup")
        radar_html = build_radar_rollup(cfg["radar_rollup"], root)
        tabs.append(("Radar Rollup", radar_html))
    ET.write_page(f"{root}/Rollup.html", cfg["title"], cfg.get("sub", ""), tabs)
    # directory-URL / legacy-link continuity: index.html redirects to Rollup.html
    with open(f"{root}/index.html", "w") as f:
        f.write('<!doctype html><meta http-equiv="refresh" content="0; url=Rollup.html">'
                '<a href="Rollup.html">Rollup</a>')
    write_standalone_rollup(root, cfg.get("server_base"))
    if radar_html is not None:
        write_radar_only(root, cfg, radar_html)


def write_radar_only(root, cfg, radar_html):
    """Radar-only report: just the Radar Rollup tab, plus a single-file version
    with every image embedded (no folder, no downloads needed)."""
    ET.write_page(f"{root}/Radar_Rollup.html", cfg["title"] + " — Radar Rollup",
                  cfg.get("sub", ""), [("Radar Rollup", radar_html)])
    write_standalone_rollup(root, cfg.get("server_base"),
                            src="Radar_Rollup.html", dst="Radar_Rollup_standalone.html")


def write_standalone_rollup(root, server_base=None, src="Rollup.html", dst="Rollup_standalone.html"):
    """Single-file rollup export: every figs/ image inlined as a data URI so
    Rollup_standalone.html can be shared ALONE (no folder tree). Day-report
    links are rewritten to the served URLs when server_base is configured
    (they would dangle in a lone file otherwise)."""
    import base64
    h = open(f"{root}/{src}").read()

    def _inline(mm):
        p = os.path.join(root, mm.group(2))
        if not os.path.exists(p):
            return mm.group(0)
        mime = "image/gif" if p.endswith(".gif") else "image/png"
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        return f'{mm.group(1)}data:{mime};base64,{b64}"'

    h = re.sub(r'(src=")(figs/[^"]+)"', _inline, h)
    if server_base:
        h = re.sub(r'href="(\d{4}-\d{2}-\d{2}/report\.html)"',
                   lambda mm: f'href="{server_base.rstrip("/")}/{mm.group(1)}"', h)
    out = f"{root}/{dst}"
    open(out, "w").write(h)
    print(f"WROTE {out} ({os.path.getsize(out) >> 20} MB, single file)")


if __name__ == "__main__":
    main(sys.argv[1])
