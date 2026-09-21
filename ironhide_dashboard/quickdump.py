"""Quickdump a time window of an MRU run to archive CSVs (mavlink/ · tracks/ · obs/ · meta.json).

Thin CLI over ironhide_dashboard/ih/archive.py — the dashboard's SAVE TO ARCHIVE card and this script write the IDENTICAL
layout through the same function (ih.archive.save_archive): the 8/28 quickdump columns first, then the truth-match columns
appended to the track CSVs (truth_match_id, truth_match_conf, contributors) and the new obs/obs.csv (rr, bistatic range /
rate, truth_target_id, truth_match_type).  Both block-143 layouts (2026.3.x NED x_state, 2026.9.x ECEF + LLA); every query
windowed on the INDEXED time_spec.full_sec (the old float range on time_spec_float scanned the whole run).

Usage: quickdump.py HHMM HHMM outdir_name --run run_<hex> [--mru NN | --host IP] [--day YYYY-MM-DD] [--port 27017]
                    [--db sensor_store] [--root <archive root>] [--label TEXT] [--register]
  HHMM..HHMM are PDT clock times on --day (default: today); output = <root>/<day>/<outdir_name>/ (root defaults to
  ih.data.ARCHIVE_ROOT = $IH_ARCHIVE_ROOT or <repo>/data/archives).  --register also appends the window to
  <day>/flights.json as a replayable flight (the dashboard does; the historical CLI did not).  --mru NN resolves the host
  by the 10.1NN.28.205 convention when --host is not given.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))     # the dashboard root (this script lives next to app.py)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_MRU = 91          # --host wins; else --mru NN -> 10.1NN.28.205 (ih.feed.resolve_mru_host); default MRU91
DEFAULT_RUN = None        # --run is required (run_<hex> collection; the dashboard's Data source page lists them)
PDT = datetime.timezone(datetime.timedelta(hours=-7), "PDT")


def hhmm_to_epoch(day: str, hhmm: str) -> float:
    """'2026-08-28', '0719' -> epoch of 07:19:00 PDT that day."""
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=int(hhmm[:2]), minute=int(hhmm[2:4]), second=0, microsecond=0, tzinfo=PDT)
    return d.timestamp()


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("t0", help="window start HHMM (PDT)")
    ap.add_argument("t1", help="window end HHMM (PDT)")
    ap.add_argument("name", help="output folder name under <root>/<day>/ (e.g. e65bd4f9_flight1_quickdump)")
    ap.add_argument("--day", default=datetime.datetime.now(PDT).strftime("%Y-%m-%d"), help="PDT calendar day of the window (default today)")
    ap.add_argument("--host", default=None, help=f"mongo host (default: from --mru, MRU{DEFAULT_MRU})")
    ap.add_argument("--mru", type=int, default=None, help="MRU number -> host 10.1NN.28.205 (used when --host is not given)")
    ap.add_argument("--port", type=int, default=27017)
    ap.add_argument("--db", default="sensor_store")
    ap.add_argument("--run", default=DEFAULT_RUN, required=True, help="run collection (run_<hex>)")
    ap.add_argument("--root", default=None, help="archive root (default: ih.data.ARCHIVE_ROOT = $IH_ARCHIVE_ROOT or <repo>/data/archives)")
    ap.add_argument("--label", default=None, help="flight label for meta.json / flights.json (default: the folder name)")
    ap.add_argument("--register", action="store_true", help="append the window to <day>/flights.json as a replayable flight")
    a = ap.parse_args(argv)

    from ih import archive as AR
    from ih import feed as F

    host = a.host or F.resolve_mru_host(a.mru or DEFAULT_MRU)
    t0, t1 = hhmm_to_epoch(a.day, a.t0), hhmm_to_epoch(a.day, a.t1)

    def prog(frac: float, msg: str) -> None:
        print(f"\r{100 * frac:5.1f} %  {msg:<60s}", end="", file=sys.stderr, flush=True)

    res = AR.save_archive(host, a.port, a.db, a.run, t0, t1, label=a.label or a.name, root=a.root, out_name=a.name, register=a.register,
                          progress=prog, mru=a.mru)
    m = res["meta"]
    print(file=sys.stderr)
    print(f"QUICKDUMP {a.name}: {m['mavlink_rows']} mavlink rows ({', '.join(m['mavlink_ids']) or 'no MAVLink'}), "
          f"{m['tracks']} tracks / {m['track_rows']} rows (NED {m['layouts']['ned']} · ECEF {m['layouts']['ecef']}), {m['obs_rows']} obs rows -> {res['dir']}"
          + (f" · flights.json flight {res['n']}" if res["n"] is not None else ""))
    return res


if __name__ == "__main__":
    main()
