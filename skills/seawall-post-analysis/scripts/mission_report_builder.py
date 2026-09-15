#!/usr/bin/env python
"""GENERIC mission-report builder. No per-mission code — everything is declared
in a JSON config; every pane is produced by exactly two modules:

  tracking day    -> tracking_tab   (init_mission / build_summary_tab / build_flight_tab)
  engagement day  -> engagement_tab (build_engagement_tab, standard dump loaders)

Usage:  python mission_report_builder.py <config.json>

Config schema (all narrative text lives HERE, not in code):
{
  "title": str, "sub": str, "out_dir": str,
  "rollup": {"label": str, "html": str},                  # optional first tab
  "days": [
    {"label": str, "type": "tracking",
     "mdir": str                                          # ready mission dir, OR
     "prep": {"dump": str, "target_pattern": str, "ant_hae_m": float,
              "flights_pdt": [["YYYY-MM-DD","HH:MM:SS","HH:MM:SS"], ...],
              "out_mdir": str},
     "init": {"day": [y,m,d], "ant": [lat,lon,hae], "geoid_n": float},   # optional overrides
     "notes_html": str, "flight_keys": ["F1R1", ...]},
    {"label": str, "type": "engagement", "date": "YYYY-MM-DD",
     "ant": [lat,lon,hae],                                # needed for traj_csv targets
     "engagements": [
       {"name": str, "dump": str,
        "interceptor_pattern": "mav14551_2_*.csv",
        "target": {"pattern": "mav14550_1_1.csv"} OR {"traj_csv": path},
        "window_pdt": ["HH:MM:SS","HH:MM:SS"], "track_id": int,
        "cpa_seed_pdt": "HH:MM:SS", "gif": "figs/x.gif",
        "summary_html": str, "post_html": str}]}
  ]
}
"""
import os, sys, json
import numpy as np
import engagement_tab as ET
import tracking_tab as TT


def build_tracking_day(day):
    mdir = day.get("mdir")
    if not mdir:
        pr = day["prep"]
        flights = [(ET.epoch_pdt(d, a), ET.epoch_pdt(d, b)) for d, a, b in pr["flights_pdt"]]
        mdir = TT.prep_from_dump(pr["dump"], pr["out_mdir"], pr["target_pattern"],
                                 flights, pr["ant_hae_m"])
        print(f"[{day['label']}] prepped mission dir {mdir}")
    kw = {}
    init = day.get("init", {})
    if "day" in init: kw["day"] = tuple(init["day"])
    if "ant" in init: kw["ant"] = tuple(init["ant"])
    if "geoid_n" in init: kw["geoid_n"] = init["geoid_n"]
    TT.init_mission(mdir, **kw)
    html = TT.build_summary_tab(notes_html=day.get("notes_html", ""))
    for k in day.get("flight_keys", []):
        try:
            html += TT.build_flight_tab(k)
        except Exception as e:
            print(f"[{day['label']}] flight {k} skipped: {e}")
            html += f'<p class="cap">flight pane {k}: not available ({e})</p>'
    return html


def build_engagement_day(day, out_dir):
    html = ""
    for eng in day["engagements"]:
        date = day["date"]
        dump = eng["dump"]
        t0 = ET.epoch_pdt(date, eng["window_pdt"][0])
        t1 = ET.epoch_pdt(date, eng["window_pdt"][1])
        tgt_spec = eng["target"]
        if "traj_csv" in tgt_spec:
            target = ET.load_traj_csv_truth(tgt_spec["traj_csv"], tuple(day["ant"]))
        else:
            target = ET.load_dump_truth(dump, tgt_spec["pattern"], t0, t1)
        trk13 = ET.load_dump_track13(dump, eng["track_id"])
        cfg = dict(name=eng["name"],
                   interceptor=ET.E.drop_frozen(
                       ET.load_dump_truth(dump, eng["interceptor_pattern"], t0, t1)),
                   target=target, track=trk13[:, :4], track_id=eng["track_id"],
                   trkV=trk13, cpa_seed=ET.epoch_pdt(date, eng["cpa_seed_pdt"]),
                   gif_name=eng.get("gif", f"figs/{eng['track_id']}_3d.gif"),
                   summary_html=eng.get("summary_html",
                       "<p>Interceptor closest approach: <b>{cpa_truth:.0f} m to target "
                       "truth</b>, <b>{cpa_track:.0f} m to radar track {track_id}</b>.</p>"))
        try:
            html += ET.build_engagement_tab(cfg, out_dir)
        except Exception as e:
            print(f"[{eng['name']}] skipped: {e}")
            html += (f"<h3>{eng['name']}</h3><p class='cap'>engagement pane not "
                     f"available from the archived data ({e})</p>")
        html += eng.get("post_html", "")
    return html


def main(cfg_path):
    cfg = json.load(open(cfg_path))
    out = cfg["out_dir"]
    os.makedirs(out + "/figs", exist_ok=True)
    tabs = []
    if cfg.get("rollup"):
        tabs.append((cfg["rollup"]["label"], cfg["rollup"]["html"]))
    for day in cfg["days"]:
        print(f"=== {day['label']} ({day['type']})")
        if day["type"] == "tracking":
            tabs.append((day["label"], build_tracking_day(day)))
        else:
            tabs.append((day["label"], build_engagement_day(day, out)))
    ET.write_page(f"{out}/report.html", cfg["title"], cfg.get("sub", ""), tabs)


if __name__ == "__main__":
    main(sys.argv[1])
