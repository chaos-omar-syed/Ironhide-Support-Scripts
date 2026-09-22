"""Constants, the 8/28 Seawall archive (quickdump CSVs) as replayable arrays,
feed cleaning shared by archive + live paths, satellite tiles, replay clock and
session-state defaults."""
from __future__ import annotations

import copy
import glob
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import streamlit as st

# ── Paths / constants ────────────────────────────────────────────────────────
# Data is NOT shipped with the repo (range data).  Two roots, both overridable by environment variable:
#   IH_QUICKDUMP_DIR  the built-in 8/28 Seawall day (flights.json + <run8>_<flight>_quickdump/ CSV dirs) — default <repo>/data/2026-08-28
#   IH_ARCHIVE_ROOT   where "Save to archive" writes / reads saved flights (<root>/<day>/flights.json)   — default <repo>/data/archives
# Either may be absent: the archive index is then empty (Data source page shows where to put data; live mode is unaffected).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.join(REPO_ROOT, "data")
BASE = os.environ.get("IH_QUICKDUMP_DIR") or os.path.join(DATA_ROOT, "2026-08-28")
FLIGHTS_JSON = f"{BASE}/flights.json"
RUN, DAY, SITE, UNIT = "e65bd4f9", "2026-08-28", "Seawall", "MRU91"
ANT_LAT, ANT_LON, ANT_HAE = 33.748055963056764, -115.33920402695068, 140.51095000131696
ANT_LL = (ANT_LAT, ANT_LON)
PDT = timezone(timedelta(hours=-7), "PDT")
PDT_OFFSET_S = 7 * 3600
TARGET_ID = "mav14550_1_1"
OBS_MIN_RANGE_M = 736.2
# raw radar obs (block 103 DWELL_WITH_OBS) were NOT quickdumped; the only archive copy is the F1
# npz written by track_correlation/ql_flight_build.py: key "obs" = (t, az_rad, el_rad, rng_m)
# (optional; $IH_OBS_NPZ or <quickdump dir>/ql_f1_full.npz — the replay simply has no raw obs without it)
OBS_NPZ = {1: os.environ.get("IH_OBS_NPZ") or os.path.join(BASE, "ql_f1_full.npz")}
OBS_KEEP_S = 420.0  # live: rolling obs buffer (live_correlator OBS_BUF convention, 7 min)

TELEPORT_MPS = 250.0  # truth teleport gate
MAX_GAP_S = 2.5  # never interpolate truth across a wider gap
GATE_M = 150.0  # horizontal association gate track <-> truth
FRESH_S = 1.5  # fresh track state: state time - last_update_t
MEAS_LAG_S = 1.25  # state published < this after last_update_t == measurement update (else coasting)
FPM_TO_MPS = 0.00508

FLIGHT_WINDOWS = {
    1: (1787926789.1, 1787927267.9),
    2: (1787928936.2, 1787929670.0),
    3: (1787930137.8, 1787930525.2),
    4: (1787931271.9, 1787931774.9),
}
FLIGHT_DIRS = {
    1: "e65bd4f9_flight1_quickdump",
    2: "e65bd4f9_flights23_quickdump",
    3: "e65bd4f9_flights23_quickdump",
    4: "e65bd4f9_flight4_quickdump",
}
REPLAY_LEAD_S, REPLAY_TAIL_S = 20.0, 10.0
REPLAY_TICK_S = 1.0  # fragment cadence while the replay plays (live mode uses refresh_s, default 2 s)

# truth array columns (t, E, N, U, vE, vN, vU)  — metres about the antenna, m/s
TR = dict(t=0, E=1, N=2, U=3, vE=4, vN=5, vU=6)
# track array columns (corr_lib.load_tracks layout + velocity sigmas): t,E,N,U,sigE,sigN,sigU,lu,assoc,state,vE,vN,vU,sigvE,sigvN,sigvU
# cols 13..15 = 1σ of the filtered velocity per ENU axis (m/s; NaN when the state has no covariance — NEVER 0); every
# track array in the dashboard (live snapshot, archive replay, saved archives, the 8/28 quickdumps padded) is (m, TK_W)
TK = dict(t=0, E=1, N=2, U=3, sE=4, sN=5, sU=6, lu=7, assoc=8, state=9, vE=10, vN=11, vU=12, svE=13, svN=14, svU=15)
TK_W = 16
TRUTH_COLS = ["t_epoch", "lat", "lon", "E_m", "N_m", "U_m_hae", "vel_e_mps", "vel_n_mps", "vert_spd_wire_ftmin", "validposition"]

# MAVLink truth role assignment (Data source page).  Empty lists = "use the patterns" (role_of's rule);
# the patterns are comma-separated tokens matched as whole ids / number groups (so "mavlink_1" never
# matches "mavlink_14551").
TGT_PATTERN_DEFAULT, ITC_PATTERN_DEFAULT = "14550,mavlink_1", "14551,mavlink_2"
# Where "Save to archive" writes: <root>/<YYYY-MM-DD>/<run8>_<label>/ (tests override).  Default = the CURRENT test week
# (Seawall_Ironhide_Testing/Seawall_Week_of_M-DD, 2026-09-15 reorg); override with $IH_ARCHIVE_ROOT.  The built-in 8/28 index stays on BASE.
ARCHIVE_ROOT = os.environ.get("IH_ARCHIVE_ROOT") or os.path.join(DATA_ROOT, "archives")
BUILTIN_FLIGHTS = tuple(FLIGHT_WINDOWS)   # the 8/28 flight numbers that exist only when the quickdump dir is present


def has_builtin() -> bool:
    """Is the built-in 8/28 quickdump day present (``<IH_QUICKDUMP_DIR>/flights.json``)?"""
    return os.path.isfile(FLIGHTS_JSON)


def data_hint() -> str:
    """One-line 'where to put data' message for the empty state (Data source page / sidebar)."""
    return (f"No archive flights found. Put the 8/28 quickdump day at {BASE} (flights.json + *_quickdump/ dirs; "
            f"$IH_QUICKDUMP_DIR) or save a flight from a live run into {ARCHIVE_ROOT} ($IH_ARCHIVE_ROOT). "
            "See data/README.md.  Live mode does not need any of this.")

STATE_DEFAULTS = {
    "source": "archive",  # archive | live — the ENGINE source (what the Live page renders); "mode" mirrors it (set_source keeps both)
    "mode": "archive",  # CONTRACT (A/C read): "live" | "archive" — always == source (init_state re-derives it from source)
    "flight": 1,
    "ds_mode": "live",  # Data-source page mode selector (live | archive): LIVE preselected; the page renders only that mode's content
    "mru_number": 91,  # CONTRACT: MRU number -> mx host 10.1NN.28.205 (feed.resolve_mru_host)
    "live_custom_host": "",  # Advanced: host/IP override (Tailscale etc.); wins over the MRU convention when set
    "live_run_name": "",  # friendly name of the followed run (runs collection) for the sidebar / status line
    "truth_tgt_ids": [],  # MAVLink target_ids assigned TARGET (Data source page); [] = pattern rule
    "truth_itc_ids": [],  # MAVLink target_ids assigned INTERCEPTOR; [] = pattern rule
    "role_ids": {"target": [], "interceptor": []},  # CONTRACT (C reads via P["role_ids"]): echo of truth_tgt_ids / truth_itc_ids
    "tgt_pattern": TGT_PATTERN_DEFAULT,  # auto-assign patterns (Advanced)
    "itc_pattern": ITC_PATTERN_DEFAULT,
    "speed": 1.0,
    "playing": False,
    "anchor_t": FLIGHT_WINDOWS[1][0] - REPLAY_LEAD_S,
    "anchor_wall": 0.0,
    "live_host": "",
    "live_port": 27017,
    "live_db": "sensor_store",
    "live_run": "",
    "live_lag": 1.0,
    "live_probe": None,
    "live_run_filter": "",
    "hist_s": 180,
    "refresh_s": 2.0,  # live fetch cadence (s); never below 1 s
    "freeze": False,  # stop redraws while inspecting/zooming (clock + ingest keep running)
    "map_half": 800,
    "trail_s": 20,
    "show_sat": True,
    "show_blind": False,
    "show_other": True,  # legacy key (other-track scatter was removed from the map); kept so old sessions load
    "show_obs": True,  # raw obs ✕ overlay on the track-quality panel
    "show_free_tracks": False,  # uncorrelated (ADS-B / clutter) radar tracks on the map while MAVLink truth is present (2026-09-15: off)
    "spec_window": 120,
    "cpa_gate_m": 70.0,  # a closest approach is a CPA only when sep < gate AND the pass is over (engine.cpa_gate)
    "screen": "Laptop",  # 2026-09-15 default (user on a laptop; Normal text) — sidebar "Screen size" preset -> panel height (ih.liveserver.layout)
    "frame_mode": "engagement",  # CONTRACT (A reads): engagement (default box) | follow | fit_flight (footprint) | fixed (map_half box) — FRAME_MODES
    "view_rev": 0,  # bumped on every EXPLICIT frame change (preset / Fixed<->Follow / zoom slider / Reset map view): the panel re-applies the range then
    "tgt_tid": None,
    "track_events": [],  # [(t, role, old_id, new_id)] track-id changes this session (engine.analyze; capped at 50)
    "cpa_run": None,  # (sep, t, E, N, horiz) running closest-so-far (never gated)
    "cpa_ok": None,  # the last closest-so-far that PASSED the CPA gate (what the plots mark with the gold star)
    "cpa_trk_run": None,  # (sep, t, E, N, horiz) running closest-so-far of interceptor truth <-> TARGET TRACK (engine, never gated)
    "cpa_trk_ok": None,  # ... the last one that passed the same CPA gate (A["cpa_trk"]: the target-red ring ★ on the separation card)
    "last_snap": None,
}


def init_state() -> None:
    s = st.session_state
    for k, v in STATE_DEFAULTS.items():
        s.setdefault(k, copy.deepcopy(v) if isinstance(v, (list, dict)) else v)
    s["mode"] = "live" if s.get("source") == "live" else "archive"                      # contract mirror: ``source`` is the storage
    s["role_ids"] = {"target": list(s.get("truth_tgt_ids") or []), "interceptor": list(s.get("truth_itc_ids") or [])}
    try:
        refresh_registry()   # flights saved from a live run (flights.json entries with a "dir") join the archive index
    except Exception:
        pass
    nums = flight_numbers()
    if nums and int(s.get("flight", 1)) not in nums:
        s["flight"] = nums[0]
        s["anchor_t"] = FLIGHT_WINDOWS[nums[0]][0] - REPLAY_LEAD_S
    elif not nums and s.get("source") != "live":
        s["source"] = s["mode"] = "live"          # nothing to replay: the engine idles in live mode until Connect
        s["ds_mode"] = "live"


def set_source(src: str) -> None:
    """Switch the ENGINE source ('live' | 'archive'); keeps the contract key ``mode`` in step; a change drops derived state."""
    s = st.session_state
    src = "live" if src == "live" else "archive"
    if src == "archive" and not FLIGHT_WINDOWS:   # nothing to replay (no data present): the engine stays in live mode
        src = "live"
    if s.get("source") != src:
        reset_derived()
    s["source"], s["mode"] = src, src


def set_ds_mode(m: str) -> None:
    """Data-source page mode selector.  ARCHIVE REPLAY is always available -> the engine follows at once; LIVE follows only
    when a run is already being followed (otherwise the engine keeps its source until Connect succeeds)."""
    s = st.session_state
    m = "live" if m == "live" else "archive"
    s["ds_mode"] = m
    if m == "archive" or (s.get("live_host") and s.get("live_run")):
        set_source(m)


def disconnect() -> None:
    """Sidebar 'Disconnect': stop following the run (buffers, origin, probe dropped); the engine falls back to the replay."""
    s = st.session_state
    for k in ("_live_buf", "live_ant", "live_tx", "_live_info", "_live_start_t", "_mav_span", "_run_detail", "_run_w"):
        s.pop(k, None)
    s["live_run"], s["live_run_name"], s["live_probe"] = "", "", None
    set_source("archive")


def goto_save() -> None:
    """Live-page 'Save…' shortcut (A renders the button with on_click=D.goto_save): the entry script switches to the
    Data-source page in LIVE mode with the SAVE TO ARCHIVE card flagged (``save_focus``)."""
    s = st.session_state
    s["ds_mode"], s["_goto_save"], s["save_focus"] = "live", True, True


# ── Time helpers ─────────────────────────────────────────────────────────────
def pdt_hms(t: float | None) -> str:
    return "—" if t is None or not np.isfinite(t) else datetime.fromtimestamp(float(t), PDT).strftime("%H:%M:%S")


def pdt_full(t: float | None) -> str:
    return "—" if t is None else datetime.fromtimestamp(float(t), PDT).strftime("%Y-%m-%d %H:%M:%S PDT")


def hms_to_epoch(hms: str, day: str = DAY) -> float:
    return datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=PDT).timestamp()


def to_pdt_dt64(t: np.ndarray) -> np.ndarray:
    """epoch seconds -> naive datetime64[ms] in PDT (for plotly axes)."""
    t = np.asarray(t, float)
    out = np.full(len(t), np.datetime64("NaT", "ms"))
    ok = np.isfinite(t)
    out[ok] = ((t[ok] - PDT_OFFSET_S) * 1000.0).astype("int64").astype("datetime64[ms]")
    return out


def fmt_age(sec: float | None) -> str:
    """Human age: '45 s' · '12 min' · '3.2 h' · '9 d' ('—' for None / NaN)."""
    if sec is None or not np.isfinite(sec):
        return "—"
    sec = max(0.0, float(sec))
    if sec < 90:
        return f"{sec:.0f} s"
    if sec < 5400:
        return f"{sec / 60:.0f} min"
    if sec < 172800:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 86400:.0f} d"


def epoch_to_naive_pdt(t: float) -> datetime:
    return datetime.fromtimestamp(float(t), PDT).replace(tzinfo=None)


def naive_pdt_to_epoch(d: datetime) -> float:
    return d.replace(tzinfo=PDT).timestamp()


# ── flights.json ─────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def load_flights() -> dict:
    """The built-in 8/28 index PLUS every flight saved from a live run: any ``<ARCHIVE_ROOT>/<day>/flights.json``
    entry that carries a ``dir`` (written by ih.archive.save_archive) — keyed by its flight number ``n``."""
    flights: dict[int, dict] = {}
    j: dict = {"day": DAY, "runs": []}
    if has_builtin():    # the built-in day is optional (data is not shipped with the repo)
        try:
            with open(FLIGHTS_JSON) as f:
                j = json.load(f)
            flights = {int(fl["n"]): {**fl, "day": DAY, "run": j["runs"][0].get("run", RUN)} for fl in j["runs"][0]["flights"]}
        except Exception:
            flights, j = {}, {"day": DAY, "runs": []}
    for fl in saved_flights():
        flights[int(fl["n"])] = fl
    return {"day": j, "flights": flights}


def flight_numbers() -> list[int]:
    """Every replayable flight number (built-in + saved), sorted; [] when no data is present."""
    try:
        return sorted(int(n) for n in load_flights()["flights"])
    except Exception:
        return []


def saved_flights(root: str | None = None) -> list[dict]:
    """Flights saved from a live run (entries with a ``dir``) from every ``<root>/*/flights.json`` (root = ARCHIVE_ROOT),
    each with ``day`` / ``run`` / absolute ``path`` filled in.  Never raises (a broken file is skipped)."""
    out = []
    for fj in sorted(glob.glob(os.path.join(root or ARCHIVE_ROOT, "*", "flights.json"))):
        try:
            with open(fj) as f:
                j = json.load(f)
        except Exception:
            continue
        day = str(j.get("day") or os.path.basename(os.path.dirname(fj)))
        for r in j.get("runs", []):
            for fl in r.get("flights", []):
                if not fl.get("dir"):
                    continue
                d = str(fl["dir"])
                out.append({**fl, "n": int(fl["n"]), "day": day, "run": r.get("run", ""), "path": d if os.path.isabs(d) else os.path.join(os.path.dirname(fj), d)})
    return out


def refresh_registry() -> dict:
    """Merge the saved flights into FLIGHT_WINDOWS / FLIGHT_DIRS (the replay clock, bundle loader and frame
    key off those) and return {n: flight dict}.  Called by init_state and after every save.
    The window taken is the entry's t0/t1 = the AIRBORNE-AUDITED (trimmed) replay window (ih.archive.apply_audit; the
    raw saved window is saved_t0/saved_t1 and the CSVs on disk still hold it), so a replay never plays the dead time."""
    fl = load_flights()["flights"]
    for n, fi in fl.items():
        if fi.get("dir"):
            FLIGHT_WINDOWS[int(n)] = (float(fi["t0"]), float(fi["t1"]))
            FLIGHT_DIRS[int(n)] = fi["path"]
    if not has_builtin():   # the 8/28 constants stay importable but are not replayable without the quickdump dir
        for n in BUILTIN_FLIGHTS:
            if n not in fl:
                FLIGHT_WINDOWS.pop(n, None)
                FLIGHT_DIRS.pop(n, None)
    return fl


def flight_dir(n: int) -> str:
    d = FLIGHT_DIRS[int(n)]
    return d if os.path.isabs(d) else os.path.join(BASE, d)


def day_md(day: str) -> str:
    """'2026-08-28' -> '8/28'."""
    try:
        return f"{int(str(day)[5:7])}/{int(str(day)[8:10])}"
    except ValueError:
        return str(day)


def flight_short(n: int) -> str:
    """'8/28 · Flight 1' (sidebar / status line)."""
    fi = flight_info(n)
    return f"{day_md(str(fi.get('day') or DAY))} · {fi.get('label') or f'Flight {int(n)}'}"


def flight_label(n: int) -> str:
    """'8/28 · Flight 1 · 07:19–07:27 · 3 passes' (passes = verified count) — the archive flight selectbox label."""
    fi = flight_info(n)
    t0, t1 = float(fi["t0"]), float(fi["t1"])
    npass = sum(1 for p in passes(n) if p["verified"])
    return f"{flight_short(n)} · {pdt_hms(t0)[:5]}–{pdt_hms(t1)[:5]} · {npass} pass{'' if npass == 1 else 'es'}"


def _known_flight_numbers(root: str) -> set[int]:
    """Every flight number the registry knows: the built-in 8/28 flights + every saved flight under ``root``."""
    out: set[int] = set()
    try:
        with open(FLIGHTS_JSON) as f:
            out |= {int(fl["n"]) for fl in json.load(f)["runs"][0]["flights"]}
    except Exception:
        pass
    return out | {int(fl["n"]) for fl in saved_flights(root)}


def append_flight_entry(day: str, run8: str, entry: dict, root: str | None = None) -> int:
    """Append (or replace by ``dir``) a flight entry in ``<root>/<day>/flights.json`` — the 8/28 schema (day -> runs[] ->
    flights[]) — WITHOUT touching Streamlit (safe from the save worker thread).  The flight number ``n`` is assigned
    here: max over every known flight + 1, unless the same ``dir`` was saved before (its number is kept).  Atomic
    write (tmp + os.replace).  Returns n.  The caller refreshes the in-process registry (register_saved_flight /
    clear_flight_caches + refresh_registry) from the main thread."""
    root = root or ARCHIVE_ROOT
    path = os.path.join(root, day, "flights.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    j = {"day": day, "runs": []}
    if os.path.exists(path):
        try:
            with open(path) as f:
                j = json.load(f)
        except Exception:
            j = {"day": day, "runs": []}
    j.setdefault("runs", [])
    r = next((x for x in j["runs"] if x.get("run") == run8), None)
    if r is None:
        r = {"run": run8, "flights": []}
        j["runs"].append(r)
    r.setdefault("flights", [])
    known = _known_flight_numbers(root) | {int(x["n"]) for x in r["flights"] if "n" in x}
    prev = next((x for x in r["flights"] if x.get("dir") == entry.get("dir")), None)
    n = int(prev["n"]) if prev and "n" in prev else (max(known) + 1 if known else 1)
    entry = {**entry, "n": n}
    r["flights"] = [x for x in r["flights"] if x.get("dir") != entry.get("dir")] + [entry]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(j, f, indent=1)
    os.replace(tmp, path)
    return n


def clear_flight_caches() -> None:
    """Drop the cached flight index / bundles / frames (after a save or an ARCHIVE_ROOT change)."""
    for fn in (load_flights, flight_frame, archive_bundle):
        try:
            fn.clear()
        except Exception:
            pass


def register_saved_flight(day: str, run8: str, entry: dict) -> int:
    """append_flight_entry + refresh the in-process registry (main thread only).  Returns n."""
    n = append_flight_entry(day, run8, entry)
    clear_flight_caches()
    refresh_registry()
    return n


def next_flight_label(day: str, run8: str) -> str:
    """'Flight N' with N = flights already saved for this run on this day + 1."""
    k = sum(1 for fl in saved_flights() if fl.get("day") == day and fl.get("run") == run8)
    return f"Flight {k + 1}"


def flight_info(n: int) -> dict:
    return load_flights()["flights"][int(n)]


def engagement_window(fi: dict) -> tuple[float, float]:
    for s in fi.get("segments", []):
        if s.get("type") == "engagement":
            return float(s["t0"]), float(s["t1"])
    return float(fi["t0"]), float(fi["t1"])


def airborne_segments(n: int) -> list[dict]:
    """The AIRBORNE segments of a saved flight (ih.archive.audit_window, in its flights.json entry): [{t0, t1, t0_pdt,
    t1_pdt, drones, air_t0, air_t1, airborne_s}] — the padded replay spans with the airborne leg inside each.
    [] for the built-in 8/28 flights and for any save made before the audit existed."""
    try:
        return list(flight_info(n).get("airborne_segments") or [])
    except Exception:
        return []


def airborne_note(fi: dict | int) -> str:
    """'airborne 07:13:40–07:19:11 · 5.5 min' (two legs: 'airborne 2 legs 07:13:40–07:19:11 · 5.5 min') for the replay
    slider caption; '' when the flight carries no audit."""
    fi = flight_info(fi) if isinstance(fi, int) else fi
    segs = list(fi.get("airborne_segments") or [])
    if not segs:
        return ""
    air = float(fi.get("airborne_s") or sum(float(s.get("airborne_s") or 0) for s in segs))
    span = f"{pdt_hms(segs[0]['air_t0'])}–{pdt_hms(segs[-1]['air_t1'])}"
    lead = "airborne" if len(segs) == 1 else f"airborne {len(segs)} legs"
    return f"{lead} {span} · {air / 60.0:.1f} min"


def airborne_trim_note(fi: dict | int) -> str:
    """The audit result of a saved flight for the save card / archive contents:
    'airborne 5.5 of 21.0 min · replay trimmed to 07:13:20–07:19:26' — or 'no airborne segment found · full window kept
    (21.0 min)' when nothing flew.  '' when the entry carries no audit at all."""
    fi = flight_info(fi) if isinstance(fi, int) else fi
    if fi.get("airborne_s") is None and not fi.get("airborne_segments"):
        return ""
    s0, s1 = float(fi.get("saved_t0", fi["t0"])), float(fi.get("saved_t1", fi["t1"]))
    segs = list(fi.get("airborne_segments") or [])
    if not segs:
        return f"no airborne segment found · full window kept ({(s1 - s0) / 60.0:.1f} min)"
    air = float(fi.get("airborne_s") or 0.0)
    return (f"airborne {air / 60.0:.1f} of {(s1 - s0) / 60.0:.1f} min · replay trimmed to "
            f"{pdt_hms(float(fi['t0']))}–{pdt_hms(float(fi['t1']))}")


def passes(n: int) -> list[dict]:
    """Passes for a flight with epoch time and a `verified` flag (inside the
    interceptor-airborne window and reproduced offline)."""
    fi = flight_info(n)
    e0, e1 = engagement_window(fi)
    out = []
    for p in fi.get("passes", []):
        t = float(p["t"]) if p.get("t") is not None else hms_to_epoch(p["t_pdt"], str(fi.get("day") or DAY))
        ok = (e0 <= t <= e1) and ("NOT " not in p.get("note", ""))
        out.append({**p, "t": t, "verified": ok, "flight": n})
    return out


# ── Feed cleaning (shared by archive CSVs and live mongo rows) ───────────────
def clean_feed(df: pl.DataFrame) -> pl.DataFrame:
    """validposition==1, time-sorted, stale republishes (identical consecutive lat/lon) dropped."""
    if df.is_empty():
        return df
    df = df.filter(pl.col("validposition") == 1).sort("t_epoch")
    if df.is_empty():
        return df
    chg_prev = (pl.col("lat") != pl.col("lat").shift(1)).fill_null(True) | (pl.col("lon") != pl.col("lon").shift(1)).fill_null(True)
    chg_next = (pl.col("lat") != pl.col("lat").shift(-1)).fill_null(True) | (pl.col("lon") != pl.col("lon").shift(-1)).fill_null(True)
    # keep the FIRST and the LAST row of a run of identical positions (2026-09-15: a drone parked on the pad kept only its first
    # row, which aged out of the "now" window -> no icon on the map while the feed was alive; the last row carries the current time)
    return df.filter(chg_prev | chg_next)


def merge_truth(frames: list[pl.DataFrame], lo: float, hi: float) -> np.ndarray:
    """Merge cleaned per-feed frames of one drone -> truth array (n,7) TR layout.
    0.5 s dedup across feeds, then >TELEPORT_MPS jumps dropped."""
    frames = [f for f in frames if not f.is_empty()]
    if not frames:
        return np.zeros((0, 7))
    df = pl.concat([f.select(TRUTH_COLS) for f in frames]).sort("t_epoch")
    df = df.filter((pl.col("t_epoch") >= lo) & (pl.col("t_epoch") <= hi))
    if df.is_empty():
        return np.zeros((0, 7))
    df = df.with_columns(((pl.col("t_epoch") * 2).round(0) / 2).alias("_tb")).unique(subset="_tb", keep="first", maintain_order=True)
    a = df.select(["t_epoch", "E_m", "N_m", "U_m_hae", "vel_e_mps", "vel_n_mps", "vert_spd_wire_ftmin"]).to_numpy().astype(float)
    a[:, 6] *= FPM_TO_MPS
    keep = np.ones(len(a), bool)
    last = 0
    for i in range(1, len(a)):
        dt = max(a[i, 0] - a[last, 0], 1e-3)
        step = np.sqrt(((a[i, 1:4] - a[last, 1:4]) ** 2).sum())
        if step / dt > TELEPORT_MPS:
            keep[i] = False
        else:
            last = i
    return a[keep]


def feed_status(raw: dict, t_now: float) -> dict:
    """Health of one raw MAVLink feed as of t_now.
    raw: {"name", "role", "t", "lat", "lon"} arrays of valid rows (uncleaned)."""
    t = raw["t"]
    i1 = int(np.searchsorted(t, t_now, side="right"))
    if i1 == 0:
        return {"name": raw["name"], "role": raw["role"], "state": "none", "age": None, "frozen": False}
    age = float(t_now - t[i1 - 1])
    frozen = False
    if i1 >= 3:
        la, lo = raw["lat"][i1 - 3 : i1], raw["lon"][i1 - 3 : i1]
        frozen = bool(np.all(la == la[0]) and np.all(lo == lo[0]))
    if age > 15:
        state = "down"
    elif frozen:
        state = "frozen"
    elif age > 2.5:
        state = "stale"
    else:
        state = "alive"
    return {"name": raw["name"], "role": raw["role"], "state": state, "age": age, "frozen": frozen}


_ROLE_RE = re.compile(r"mav(?:link)?[_-]?(\d+)(?:[_-](\d+))?")


def pattern_tokens(pattern: str | None) -> list[str]:
    return [t.strip().lower() for t in str(pattern or "").split(",") if t.strip()]


def pattern_matches(tid: str, pattern: str | None) -> bool:
    """Does an auto-assign pattern ("14550,mavlink_1") match a MAVLink target_id?  Each token must match a
    WHOLE number group / name: "mavlink_1" matches mavlink_1 and mavlink_1_7 but never mavlink_14551 (the
    substring shortcut that once merged both drones into one truth); "14550" matches mav14550_1_1 and
    mavlink_14550."""
    s_ = str(tid).lower()
    for tok in pattern_tokens(pattern):
        if re.search(rf"(?<![0-9a-z]){re.escape(tok)}(?![0-9])", s_) or (tok.isdigit() and re.search(rf"(?<!\d){tok}(?!\d)", s_)):
            return True
    return False


def role_by_pattern(tid: str) -> str:
    """The built-in rule: 'target' for the 14550 MAVLink feed (or MAVLink system id 1), 'interceptor' otherwise.
    Archive feed names: mav14550_1_1 (target) / mav14551_2_0, _2_1, _2_34 (interceptor, three feeds merged);
    live target_id: 'mavlink_14550' / 'mavlink_14551'.  The first number is the UDP port when it has >= 4
    digits, else the MAVLink system id.  (The previous `"mavlink_1" in tid` shortcut classed the
    interceptor feed mavlink_14551 as TARGET, merging both drones into one truth.)"""
    s_ = str(tid).lower()
    m = _ROLE_RE.search(s_)
    if m:
        first, sysid = m.group(1), m.group(2)
        if len(first) >= 4:
            if first == "14550":
                return "target"
            if first == "14551":
                return "interceptor"
            return "target" if sysid == "1" else "interceptor"
        return "target" if first == "1" else "interceptor"
    if "14550" in s_:
        return "target"
    return "interceptor"


def _roles_from_state() -> tuple:
    """(tgt ids, itc ids, tgt pattern, itc pattern) from session state — defaults when there is no session
    (bare mode / worker threads)."""
    try:
        s = st.session_state
        return (tuple(s.get("truth_tgt_ids") or ()), tuple(s.get("truth_itc_ids") or ()),
                str(s.get("tgt_pattern") or TGT_PATTERN_DEFAULT), str(s.get("itc_pattern") or ITC_PATTERN_DEFAULT))
    except Exception:
        return ((), (), TGT_PATTERN_DEFAULT, ITC_PATTERN_DEFAULT)


def role_of(tid: str, roles: tuple | None = None) -> str:
    """Role of a MAVLink target_id: 'target' | 'interceptor'.
    Precedence: (1) the session's explicit assignment (Data source page: ``truth_tgt_ids`` / ``truth_itc_ids``,
    or the ``roles`` tuple = roles_key() when given, e.g. from a worker thread); (2) an id in NEITHER list — even
    when the lists are non-empty — falls back to the patterns, so an extra feed that appears later still gets a
    role: the session's auto-assign patterns (``tgt_pattern`` / ``itc_pattern``, whole-token match) first, then
    the built-in 14550 / 14551 / system-id rule (role_by_pattern)."""
    tgt, itc, tp, ip = roles if roles is not None else _roles_from_state()
    s_ = str(tid)
    if s_ in tgt:
        return "target"
    if s_ in itc:
        return "interceptor"
    if tp != TGT_PATTERN_DEFAULT or ip != ITC_PATTERN_DEFAULT:   # custom patterns win over the built-in rule
        if pattern_matches(s_, tp) and not pattern_matches(s_, ip):
            return "target"
        if pattern_matches(s_, ip) and not pattern_matches(s_, tp):
            return "interceptor"
    return role_by_pattern(s_)


def role_assignment() -> dict:
    """{"target": [ids], "interceptor": [ids]} — the session's explicit assignment (may be empty lists)."""
    tgt, itc, _, _ = _roles_from_state()
    return {"target": list(tgt), "interceptor": list(itc)}


def roles_key() -> tuple:
    """Hashable (tgt ids, itc ids, tgt pattern, itc pattern) — the st.cache_data key of archive_bundle and the
    ``roles`` argument of role_of for worker threads."""
    tgt, itc, tp, ip = _roles_from_state()
    return (tuple(sorted(tgt)), tuple(sorted(itc)), tp, ip)


def collapse_ids(ids: list[str]) -> str:
    """'mav14551_2_*' when >= 2 ids share a prefix (up to the last separator), 'a, b' otherwise, '—' for none."""
    ids = sorted({str(i) for i in ids if str(i)})
    if not ids:
        return "—"
    if len(ids) == 1:
        return ids[0]
    pre = os.path.commonprefix(ids)
    cut = max(pre.rfind("_"), pre.rfind("-"), pre.rfind(":"))
    if cut >= 3:
        return pre[: cut + 1] + "*"
    return ", ".join(ids)


def roles_label() -> str:
    """'TGT mav14550_1_1 · INT mav14551_2_*' — from the explicit assignment, else from the last snapshot's feeds
    (grouped by role_of), 'TGT — · INT —' when nothing is assigned / detected."""
    a = role_assignment()
    tgt, itc = list(a["target"]), list(a["interceptor"])
    if not tgt and not itc:
        try:
            feeds = (st.session_state.get("last_snap") or {}).get("feeds") or []
        except Exception:
            feeds = []
        for f in feeds:
            (tgt if role_of(f["name"]) == "target" else itc).append(f["name"])
    return f"TGT {collapse_ids(tgt)} · INT {collapse_ids(itc)}"


def set_roles(tgt_ids: list[str], itc_ids: list[str]) -> str | None:
    """Apply an assignment (both lists) — refused with a message when an id sits in both roles; on success the
    DERIVED state is cleared so the Live page re-merges truth immediately.

    2026-09-17: the raw live buffer is KEPT.  ih.feed keeps its truth / track / obs rows PER MAVLINK ID and
    applies the roles at merge time (feed._buffers_to_snapshot -> feed_role), so a role change re-merges the
    whole buffered window on the very next tick — dropping ``_live_buf`` only threw away up to 180 s of truth
    and tracks (and, with ``live_ant``, re-probed the antenna origin), and after a role change mid-engagement
    the separation series restarted at the click: the CPA of a pass that had just happened was lost (MRU91 run
    3212ae22, assigning mav14550_1_1 as TARGET once its feed appeared at 06:50:37).  Re-merging from the kept
    buffer recovers it (simulated: 28.4 m @ 06:55:47 kept vs 52.6 m with the buffer dropped)."""
    both = sorted(set(map(str, tgt_ids)) & set(map(str, itc_ids)))
    if both:
        return f"{', '.join(both)} cannot be both TARGET and INTERCEPTOR — previous assignment kept"
    s = st.session_state
    s["truth_tgt_ids"] = [str(x) for x in tgt_ids]
    s["truth_itc_ids"] = [str(x) for x in itc_ids]
    s["role_ids"] = {"target": list(s["truth_tgt_ids"]), "interceptor": list(s["truth_itc_ids"])}   # contract echo (C reads P["role_ids"])
    reset_derived()
    return None


def _track_meta(df: pl.DataFrame) -> dict | None:
    """Latest non-empty truth match of one track CSV in the snapshot's ``track_meta`` CONTRACT shape
    {truth_match_id: str|None, truth_match_conf: float|nan, contributors: str(JSON)|None, t: epoch} — None when the
    columns are absent (the 8/28 quickdumps predate them; ih.archive.save_archive writes them)."""
    if "truth_match_id" not in df.columns:
        return None
    v = df.filter(pl.col("truth_match_id").cast(pl.Utf8).fill_null("") != "")
    if v.is_empty():
        return {"truth_match_id": None, "truth_match_conf": float("nan"), "contributors": None, "t": float(df["t_epoch"].max() or 0.0)}
    r = v.row(-1, named=True)
    conf = r.get("truth_match_conf")
    try:
        conf = float("nan") if conf is None or conf == "" else float(conf)
    except (TypeError, ValueError):
        conf = float("nan")
    contrib = r.get("contributors") if "contributors" in df.columns else None
    return {"truth_match_id": str(r["truth_match_id"]), "truth_match_conf": conf,
            "contributors": None if contrib in (None, "") else str(contrib), "t": float(r["t_epoch"])}


# ── Archive bundle (one flight of quickdump CSVs -> arrays) ──────────────────
@st.cache_data(show_spinner="Loading archive…")
def archive_bundle(flight: int, roles: tuple | None = None) -> dict:
    """One archive flight -> arrays.  ``roles`` = roles_key() (the live path and the Data-source page pass it so a
    changed TARGET / INTERCEPTOR assignment re-merges the truth; the default reads the session as role_of does).
    Saved flights (ih.archive.save_archive) read their antenna from meta.json; ``track_meta`` = {tid: {truth_match_id,
    truth_match_conf, contributors, t}} from the optional CSV columns (empty for the 8/28 quickdumps)."""
    d = flight_dir(flight)
    t0, t1 = FLIGHT_WINDOWS[flight]
    lo, hi = t0 - REPLAY_LEAD_S - 60, t1 + REPLAY_TAIL_S + 60
    feeds = {"target": [], "interceptor": []}
    raw = []
    ant = (ANT_LAT, ANT_LON, ANT_HAE)
    saved_roles = {}
    try:
        with open(os.path.join(d, "meta.json")) as f:
            mj = json.load(f)
        a = mj.get("antenna")
        if a and len(a) >= 3:
            ant = (float(a[0]), float(a[1]), float(a[2]))
        for r_, ids_ in (mj.get("roles") or {}).items():                  # the mapping the flight was SAVED with (2026-09-15: the replay
            for i_ in ids_ or ():                                          # of a 9/15 flight called the target the interceptor — the un-aliased
                saved_roles[str(i_)] = r_                                  # id "mavlink_2_*" matched the interceptor pattern)
    except Exception:
        pass
    explicit = set((roles[0] if roles else ()) or ()) | set((roles[1] if roles else ()) or ())
    for f in sorted(glob.glob(os.path.join(d, "mavlink", "*.csv"))):
        name = os.path.splitext(os.path.basename(f))[0]
        role = role_of(name, roles) if (name in explicit or name not in saved_roles) else saved_roles[name]   # explicit > saved > patterns
        if role not in ("target", "interceptor"):
            role = "interceptor"
        df = pl.read_csv(f, infer_schema_length=100000)
        if df.is_empty() or not set(TRUTH_COLS) <= set(df.columns):
            continue
        v = df.filter((pl.col("validposition") == 1) & (pl.col("t_epoch") >= lo) & (pl.col("t_epoch") <= hi)).sort("t_epoch")
        raw.append({"name": name, "role": role, "t": v["t_epoch"].to_numpy().astype(float), "lat": v["lat"].to_numpy(), "lon": v["lon"].to_numpy()})
        feeds[role].append(clean_feed(df))
    tgt = merge_truth(feeds["target"], lo, hi)
    itc = merge_truth(feeds["interceptor"], lo, hi)

    parts = []
    meta: dict[int, dict] = {}
    for f in glob.glob(os.path.join(d, "tracks", "track_*.csv")):
        tid = int(os.path.splitext(os.path.basename(f))[0].split("_")[1])
        df = pl.read_csv(f, infer_schema_length=100000).filter((pl.col("t_epoch") >= lo) & (pl.col("t_epoch") <= hi))
        if df.is_empty():
            continue
        num = ["t_epoch", "E_m", "N_m", "U_m", "sigE_m", "sigN_m", "sigU_m", "last_update_t", "total_associations", "track_state", "vE_mps", "vN_mps", "vU_mps",
               "sigvE_mps", "sigvN_mps", "sigvU_mps"]     # TK layout; the last three are NaN for archives written before them (8/28 quickdumps)
        a = df.select([pl.col(c).cast(pl.Float64, strict=False) if c in df.columns else pl.lit(None, dtype=pl.Float64).alias(c) for c in num]).to_numpy().astype(float)
        parts.append(np.column_stack([np.full(len(a), tid, float), a]))
        m = _track_meta(df)
        if m is not None:
            meta[tid] = m
    tracks = np.vstack(parts) if parts else np.zeros((0, TK_W + 1))
    tracks = tracks[np.argsort(tracks[:, 1], kind="stable")]
    return {
        "flight": flight, "t0": t0, "t1": t1, "tgt": tgt, "itc": itc, "raw": raw, "tracks": tracks,
        "ant": ant, "n_tracks": int(len(np.unique(tracks[:, 0]))) if len(tracks) else 0, "track_meta": meta,
    }


OBS_COLS = 8  # obs array columns: (t, az_rad, el_rad, rng_m, rr_mps, bi_rng_m, bi_rr_mps, match_code) — NaN when the source lacks them


@st.cache_data(show_spinner=False)
def archive_obs(flight: int) -> np.ndarray:
    """Raw obs (n,8) = (t, az_rad, el_rad, rng_m, rr_mps, bi_rng_m, bi_rr_mps, match_code) sorted by t for an archive
    flight (rr / bistatic columns are NaN: amb_dop and the bistatic fields were not archived — the engine
    falls back to 2 x mono for the co-located TX); empty when that flight was never dumped (only F1 has an
    npz) or the file is gone — never raises."""
    path = OBS_NPZ.get(int(flight))
    if not path or not os.path.exists(path):
        return np.zeros((0, OBS_COLS))
    try:
        o = np.asarray(np.load(path, allow_pickle=True)["obs"], float)
        if o.ndim != 2 or o.shape[1] < 4:
            return np.zeros((0, OBS_COLS))
        o = o[np.isfinite(o[:, :4]).all(axis=1), :4]
        o = np.column_stack([o, np.full((len(o), OBS_COLS - 4), np.nan)])
        return o[np.argsort(o[:, 0], kind="stable")]
    except Exception:
        return np.zeros((0, OBS_COLS))


def slice_obs(a: np.ndarray, ta: float, tb: float) -> np.ndarray:
    if a is None or not len(a):
        return np.zeros((0, OBS_COLS))
    i0, i1 = int(np.searchsorted(a[:, 0], ta, side="left")), int(np.searchsorted(a[:, 0], tb, side="right"))
    return a[i0:i1]


def slice_truth(a: np.ndarray, ta: float, tb: float) -> np.ndarray:
    if not len(a):
        return a
    i0, i1 = np.searchsorted(a[:, 0], [ta, tb], side="right")
    i0 = int(np.searchsorted(a[:, 0], ta, side="left"))
    return a[i0:i1]


def slice_tracks(arr: np.ndarray, ta: float, tb: float) -> dict[int, np.ndarray]:
    """(n,17) tid-prefixed track rows sorted by t -> {tid: (m,16) TK-layout array} in [ta, tb]."""
    if not len(arr):
        return {}
    i0 = int(np.searchsorted(arr[:, 1], ta, side="left"))
    i1 = int(np.searchsorted(arr[:, 1], tb, side="right"))
    sub = arr[i0:i1]
    if not len(sub):
        return {}
    o = np.lexsort((sub[:, 1], sub[:, 0]))
    s = sub[o]
    bounds = np.flatnonzero(np.diff(s[:, 0])) + 1
    return {int(p[0, 0]): p[:, 1:] for p in np.split(s, bounds)}


# ── Map frame (FIXED footprint) ───────────────────────────────────────────────
LIVE_FRAME = (-300.0, 4100.0, -800.0, 1600.0)   # last known footprint (Seawall e65bd4f9 box, E / N about the radar) — live-mode fallback
FRAME_PAD, FRAME_Q = 0.15, 100.0                # footprint padding fraction and quantisation (m)


def square_frame(x0: float, x1: float, y0: float, y1: float, pad: float = FRAME_PAD, q: float = FRAME_Q) -> tuple[float, float, float, float]:
    """A bounding box -> the SQUARE map frame around its centre: half = max half-extent × (1 + pad),
    quantised up to ``q`` m (the map keeps equal aspect; constrain="range" shows what fits)."""
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    half = max(0.5 * (x1 - x0), 0.5 * (y1 - y0)) * (1.0 + pad)
    half = float(np.ceil(max(half, q) / q) * q)
    cx, cy = float(np.round(cx / q) * q), float(np.round(cy / q) * q)
    return cx - half, cx + half, cy - half, cy + half


@st.cache_data(show_spinner=False)
def flight_frame(flight: int) -> tuple[float, float, float, float]:
    """FIXED map frame for an archive flight: the footprint of BOTH vehicles' truth over the whole
    flight window, padded 15 %, square, quantised to 100 m — computed once per flight (cached)."""
    b = archive_bundle(int(flight))
    t0, t1 = FLIGHT_WINDOWS[int(flight)]
    pts = [a for a in (slice_truth(b["tgt"], t0, t1), slice_truth(b["itc"], t0, t1)) if a is not None and len(a)]
    if not pts:
        return square_frame(*LIVE_FRAME)
    E = np.concatenate([a[:, TR["E"]] for a in pts])
    N = np.concatenate([a[:, TR["N"]] for a in pts])
    ok = np.isfinite(E) & np.isfinite(N)
    if not ok.any():
        return square_frame(*LIVE_FRAME)
    return square_frame(float(E[ok].min()), float(E[ok].max()), float(N[ok].min()), float(N[ok].max()))


def bump_view_rev() -> None:
    """Sidebar callback: an EXPLICIT frame change (preset / Fixed<->Follow / zoom slider / Reset map view) —
    the panel re-applies the server's map range (animated) and releases any user view lock."""
    st.session_state["view_rev"] = int(st.session_state.get("view_rev", 0)) + 1


def live_frame() -> tuple[float, float, float, float]:
    """FIXED frame in live mode: the last known footprint preset (Seawall box), squared."""
    return square_frame(*LIVE_FRAME, pad=0.0)


FRAME_MODES = {"engagement": "Engagement box", "follow": "Follow vehicles", "fit_flight": "Fit whole flight", "fixed": "Fixed (zoom slider)"}


def frame_mode() -> str:
    """The sidebar map-frame mode (contract key ``frame_mode``): engagement | follow | fit_flight | fixed."""
    m = str(st.session_state.get("frame_mode") or "engagement")
    return m if m in FRAME_MODES else "engagement"


def current_frame() -> tuple[float, float, float, float] | None:
    """The FIXED footprint box for 'fit_flight' (flight footprint / live preset); None for engagement / follow / fixed
    (the viz side derives the engagement box and the map_half box itself from ``frame_mode``)."""
    if frame_mode() != "fit_flight":
        return None
    return live_frame() if is_live() else flight_frame(int(st.session_state.get("flight", 1)))



# ── Satellite tiles (Esri World Imagery via live_correlator, cached) ─────────
@st.cache_data(show_spinner=False, ttl=6 * 3600)
def sat_payload(x0: float, x1: float, y0: float, y1: float, ant: tuple[float, float]) -> dict:
    try:
        import live_correlator as LC  # lazy: heavy module, needs internet for tiles

        LC.ANT_LL[0] = (float(ant[0]), float(ant[1]))
        return LC._satmap_payload(float(x0), float(x1), float(y0), float(y1))
    except Exception as ex:  # offline etc. — never break the page
        return {"err": str(ex)}


# ── Replay clock ─────────────────────────────────────────────────────────────
def replay_bounds(flight: int | None = None) -> tuple[float, float]:
    n = int(flight or st.session_state.get("flight", 1))
    if n not in FLIGHT_WINDOWS:          # no archive data at all: a degenerate window (the pages show the empty state)
        nums = sorted(FLIGHT_WINDOWS)
        if not nums:
            return 0.0, 0.0
        n = nums[0]
    t0, t1 = FLIGHT_WINDOWS[n]
    return t0 - REPLAY_LEAD_S, t1 + REPLAY_TAIL_S


def is_live() -> bool:
    return st.session_state.get("source") == "live"


def now_t() -> float:
    """Current analysis time: wall clock (minus lag) when live, replay clock otherwise."""
    s = st.session_state
    if is_live():
        return time.time() - float(s.get("live_lag", 1.0))
    lo, hi = replay_bounds()
    if s.get("playing"):
        t = s["anchor_t"] + (time.time() - s["anchor_wall"]) * float(s.get("_speed_eff", s["speed"]))
        if t >= hi:
            s["anchor_t"], s["playing"] = hi, False
            return hi
        return float(min(max(t, lo), hi))
    return float(min(max(s["anchor_t"], lo), hi))


def play() -> None:
    s = st.session_state
    s["anchor_t"] = now_t()
    s["anchor_wall"] = time.time()
    s["_speed_eff"] = float(s["speed"])
    s["playing"] = True


def pause() -> None:
    s = st.session_state
    s["anchor_t"] = now_t()
    s["playing"] = False


def toggle_play() -> None:
    pause() if st.session_state.get("playing") else play()


def seek(t: float, keep_playing: bool | None = None) -> None:
    s = st.session_state
    was = s.get("playing") if keep_playing is None else keep_playing
    s["anchor_t"] = float(t)
    s["anchor_wall"] = time.time()
    s["_speed_eff"] = float(s["speed"])
    s["playing"] = bool(was)
    s["cpa_run"] = None
    s["cpa_ok"] = None
    s["cpa_trk_run"] = None          # the track-CPA pair (interceptor truth <-> target track, engine) follows the truth pair
    s["cpa_trk_ok"] = None
    s["tgt_tid"] = None
    s["track_events"] = []           # a seek is a new context: no handover carried across it
    s["_yrng_mem"] = {}              # ... nor the held axis ranges (ih.plots.stable_range)
    s["_itc_tid_prev"] = None


def restart() -> None:
    seek(replay_bounds()[0], keep_playing=True)


PASS_LEAD_S = 25.0   # "Jump to pass" lands this many seconds BEFORE the pass: the closing approach is what you watch, and the CPA gate needs it to arm


def jump_to_pass(t_pass: float) -> None:
    """Sidebar 'Jump to pass' (archive mode): seek to PASS_LEAD_S before the pass and play."""
    seek(float(t_pass) - PASS_LEAD_S, keep_playing=True)


def speed_changed() -> None:
    """Replay-speed callback (sidebar radio / Data-source radio): re-anchor the clock at the CURRENT
    replay time (computed with the OLD effective speed) so the new speed applies from now on.  Before
    this the clock was anchor_t + elapsed × speed, so switching 1× -> 4× after a minute of play jumped
    the replay 3 minutes forward (and the map with it)."""
    s = st.session_state
    if s.get("playing"):
        s["anchor_t"] = now_t()
        s["anchor_wall"] = time.time()
    s["_speed_eff"] = float(s["speed"])


def apply_query_params() -> None:
    """Deep link, applied ONCE per browser session: ?flight=1&t=07:22:31 [&play=1] [&screen=Laptop] [&ds=archive|live]
    seeks the archive replay to that PDT time (paused unless play=1), may preselect the screen preset and the Data-source
    page's mode selector (ds=archive also makes the engine follow the replay) — reproducible screenshots / sharing a moment."""
    s = st.session_state
    if s.get("_qp_done"):
        return
    s["_qp_done"] = True
    try:
        qp = st.query_params
        fl, t, play_, screen, ds = qp.get("flight"), qp.get("t"), qp.get("play"), qp.get("screen"), qp.get("ds")
    except Exception:
        return
    if screen:   # "Laptop" | "Desktop 1080p" | "Large 1440p" (QA screenshots at a given width)
        from . import liveserver as _LS

        if screen in _LS.PRESETS:
            s["screen"] = screen
    if str(ds or "").lower() in ("live", "archive"):   # the Data-source page opens in that mode (LIVE is the default landing)
        set_ds_mode(str(ds).lower())
    try:
        if fl and int(fl) in FLIGHT_WINDOWS:
            s["flight"] = int(fl)
            seek(replay_bounds(int(fl))[0], keep_playing=False)
        if t:
            # 2026-09-22: the HH:MM:SS is a PDT clock on the FLIGHT'S OWN DAY (a saved 9/17 flight used to resolve against the
            # built-in 8/28 day, so every shared link landed at the window start)
            n = s.get("flight")
            day = str((flight_info(int(n)).get("day") if n is not None and int(n) in FLIGHT_WINDOWS else None) or DAY)
            seek(hms_to_epoch(str(t).strip(), day), keep_playing=str(play_ or "").lower() in ("1", "true", "yes"))
    except (ValueError, TypeError):
        pass


def set_flight(n: int) -> None:
    st.session_state["flight"] = int(n)
    seek(replay_bounds(n)[0], keep_playing=False)


def reset_derived() -> None:
    st.session_state["cpa_run"] = None
    st.session_state["cpa_ok"] = None
    st.session_state["cpa_trk_run"] = None   # the track-CPA pair (engine) resets with the truth pair
    st.session_state["cpa_trk_ok"] = None
    st.session_state["tgt_tid"] = None
    st.session_state["last_snap"] = None
    st.session_state["track_events"] = []   # engine.analyze appends (t, role, old_id, new_id) on every target / interceptor track-id change
    st.session_state["_itc_tid_prev"] = None
