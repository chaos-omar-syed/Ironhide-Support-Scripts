"""DATA SOURCE — the operator's landing page: ONE big mode selector (LIVE RADAR UNIT preselected · ARCHIVE REPLAY) and
only the selected mode's content.

LIVE: MRU number -> mx host by the site convention 10.1NN.28.205 ("→ IP" shown; Advanced: custom host, port, database,
run-name filter, auto-assign patterns, history) -> big CONNECT: probes the unit (3 s timeout), lists every run_* collection
newest first ("<name> · <id8> · <start PDT> · <last data age>"), auto-selects the newest run with recent 106 / 143 data
and follows it at once.  Then: LIVE INFO (every 2 s, indexed queries over the last 60 s of the run's data: last-data age
· radar tracks / observations · MAVLink feeds detected table · host / run / start / antenna / transmitter / latency),
MAVLINK ROLE ASSIGNMENT (TARGET / INTERCEPTOR multiselects over the detected ids, prefilled by the patterns, Swap, no id
in both roles — drives the engine immediately; amber "waiting for MAVLink feeds" until one appears, rescanned every 5 s)
and SAVE TO ARCHIVE (last N minutes / time range prefilled with the run's MAVLink window / whole run; label; background
job with progress; completion callout + "Switch to this flight").
ARCHIVE: flight selectbox from flights.json (the 8/28 flights + every saved one), "Load flight and open Live view",
archive contents, and the same role assignment over the archive's feed names (the replay transport and the
jump-to-pass buttons live in the SIDEBAR, where the plots are — never on this page).

Typography is larger on THIS page only (an operator screen): a page-scoped CSS block on the st.container(key="ds_page")
wrapper — rem units, so the sidebar "Text size" scale applies; the theme tokens are untouched.
"""
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from ih import archive as AR  # noqa: E402
from ih import data as D  # noqa: E402
from ih import feed as F  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import theme as T  # noqa: E402

D.init_state()
s = st.session_state
T.topbar("Ironhide · Data source · 1 / 2", badge=T.build_badge())
# NO MONGO ON A PAGE SWITCH / SIDEBAR CLICK (2026-09-15): the LIVE INFO fragment's body also runs inline during the main
# script run, so every rerun of this page paid F.live_info (1.2-1.4 s on MRU91) + AR.preview on top of the Live page's own
# fetch.  This flag is set by the MAIN script run only (a fragment rerun does not re-execute the module); _live_info_view
# pops it and re-renders the LAST info instead of querying.  The probe / run table already only run on Connect.
s["_ds_main_run"] = True

RESCAN_S = 5.0       # LIVE INFO cadence while no MAVLink feed is detected (2 s once feeds are there)
INFO_S = 2.0
MODE_LABEL = {"live": "LIVE RADAR UNIT", "archive": "ARCHIVE REPLAY"}
SCOPE_LABEL = {"last": "Last N minutes", "range": "Time range (PDT)", "run": "Whole run"}

# page-scoped operator-screen typography (rem only -> follows the global text scale); nothing here touches ih.theme tokens
PAGE_CSS = """<style>
/* operator-screen typography: the theme's ONE scale (.75 / .85 / 1 / 1.15 / 1.3 / 1.6 / 2 / 3.2 rem), a step up where it helps reading at arm's length */
.st-key-ds_page p, .st-key-ds_page li, .st-key-ds_page label, .st-key-ds_page dd, .st-key-ds_page [data-testid="stWidgetLabel"] p,
.st-key-ds_page [data-testid="stCaptionContainer"] p, .st-key-ds_page .stCaption { font-size: 1rem !important; line-height: 1.5; }
.st-key-ds_page .ih-headline .t { font-size: 2rem; }
.st-key-ds_page .ih-headline .s { font-size: 1rem; }
.st-key-ds_page .ih-card-h span { font-size: 1.15rem; letter-spacing: .04em; }
.st-key-ds_page .ih-card-h .r { font-size: .85rem; }
.st-key-ds_page .ih-kv dt { font-size: .75rem; } .st-key-ds_page .ih-kv dd { font-size: 1rem; line-height: 1.5; }
.st-key-ds_page .ih-callout .k { font-size: .85rem; } .st-key-ds_page .ih-callout .t { font-size: 1.3rem; } .st-key-ds_page .ih-callout .b { font-size: 1rem; line-height: 1.55; }
.st-key-ds_page table.ih-table th { font-size: .75rem; }
.st-key-ds_page table.ih-table td, .st-key-ds_page table.ih-table td.num { font-size: 1rem; padding: .7rem .5rem; }
/* hit targets >= 44 px (--hit): buttons, radio options, selects, inputs; the big mode selector and CONNECT / SAVE are taller still */
.st-key-ds_page .stButton button { min-height: 3rem; font-size: 1rem; }
.st-key-ds_page .stButton button[kind="primary"] { min-height: 3.6rem; font-size: 1.15rem; font-weight: 600; }
.st-key-ds_page [data-baseweb="select"] > div { min-height: 3.4rem; font-size: 1rem; }
.st-key-ds_page [data-baseweb="select"] [role="option"], .st-key-ds_page [data-baseweb="menu"] li { font-size: 1rem; padding: .7rem .8rem; }
.st-key-ds_page [data-baseweb="input"] input, .st-key-ds_page [data-baseweb="base-input"] input { font-size: 1.3rem; min-height: var(--hit); }
/* the CONNECT row is the page: MRU number label 1.6 rem, the field 3.2 rem tall with a 2 rem numeral, the resolved address 1.6 rem, Connect 2 rem (user 2026-09-14: "the MRU text size is still tiny") */
.st-key-ds_connect [data-testid="stWidgetLabel"] p, .st-key-ds_connect [data-testid="stWidgetLabel"] label { font-size: 1.6rem !important; color: var(--ink); }
.st-key-ds_connect [data-testid="stNumberInput"] input { font-size: 2rem !important; min-height: 4rem; font-family: var(--mono); }
.st-key-ds_connect [data-testid="stNumberInput"] button { min-height: 2rem; min-width: 3rem; }
.st-key-ds_connect .stButton button[kind="primary"] { min-height: 4.4rem; font-size: 2rem; }
.st-key-ds_page .stRadio [role="radiogroup"] label { font-size: 1.15rem; min-height: var(--hit); align-items: center; }
.st-key-ds_page .stRadio [role="radiogroup"] { gap: .6rem; }
.st-key-ds_mode_box [role="radiogroup"] { flex-wrap: wrap; gap: 1rem; }
.st-key-ds_mode_box [role="radiogroup"] label { padding: 1.1rem 2rem; border: 1px solid var(--rule); border-radius: 2px; background: var(--card); }
.st-key-ds_mode_box [role="radiogroup"] label p { font-size: 2rem !important; font-weight: 600; letter-spacing: .04em; }   /* user: "increase text size of Live Radar Unit" (2 rem = a step of the type scale) */
.st-key-ds_page .ds-resolve { font: 500 1.6rem/1.4 var(--mono); padding-top: 2.6rem; color: var(--ink); }
/* LIVE INFO tiles: label · value · sub · icon+word status row; tone = 2 px TOP rule (square corners), gap-spaced column */
.st-key-ds_page .ds-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: .8rem; margin: .4rem 0 1rem; }
.st-key-ds_page .ds-tile { display: flex; flex-direction: column; gap: .35rem; background: var(--card); border-top: 2px solid var(--rule); padding: 1rem 1.1rem .9rem; min-width: 0; }
.st-key-ds_page .ds-tile.ok { border-top-color: var(--green); } .st-key-ds_page .ds-tile.amber { border-top-color: var(--amber); }
.st-key-ds_page .ds-tile.fail { border-top-color: var(--fail); } .st-key-ds_page .ds-tile.na { border-top-color: var(--rule); }
.st-key-ds_page .ds-kicker { font: 500 .75rem/1.3 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--ink2); }
.st-key-ds_page .ds-big { font: 600 2rem/1.1 var(--sans); display: flex; flex-wrap: wrap; align-items: baseline; gap: .4rem; color: var(--ink); letter-spacing: -.02em; }
.st-key-ds_page .ds-big small { font: 500 1rem/1 var(--sans); color: var(--ink2); letter-spacing: 0; }
.st-key-ds_page .ds-sub { font: 500 .85rem/1.4 var(--mono); color: var(--ink2); }
.st-key-ds_page .ds-st { font: 500 .75rem/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--ink2); padding-top: .2rem; }
</style>"""


AGE_WORD = {"ok": "LIVE", "amber": "STALE", "fail": "LOST"}      # last-data age words (same as the sidebar's)


def _tile(kicker: str, value: str, unit: str = "", sub: str = "", tone: str = "na", word: str | None = None) -> str:
    """LIVE INFO tile: kicker · value (+unit) · sub · status row (icon + word) for ok / amber / fail; na = neutral (no row)."""
    u = f"<small>{T.esc(unit)}</small>" if unit else ""
    sb = f'<div class="ds-sub">{sub}</div>' if sub else ""
    st_ = f'<div class="ds-st">{T.glyph(tone, word)}</div>' if tone in ("ok", "amber", "fail") else ""
    return f'<div class="ds-tile {tone}"><div class="ds-kicker">{T.esc(kicker)}</div><div class="ds-big">{value}{u}</div>{sb}{st_}</div>'


def _age_tone(age: float | None) -> str:
    if age is None:
        return "na"
    return "ok" if age <= F.LIVE_STALE_S else ("amber" if age <= 60 else "fail")


# ── mode selector ────────────────────────────────────────────────────────────
def _mode_changed() -> None:
    D.set_ds_mode(s["_ds_mode_w"])


# ── LIVE: connect ────────────────────────────────────────────────────────────
def _mru_changed() -> None:
    try:
        s["mru_number"] = int(s["_mru_w"])
    except (TypeError, ValueError):
        pass


def _follow(run: str, row: dict | None = None) -> None:
    """Follow one run collection: buffers / origin / info / spans dropped, engine source -> live."""
    pr = s.get("live_probe") or {}
    row = row or next((r for r in pr.get("runs", []) if r["name"] == run), {})
    s["live_run"], s["live_run_name"] = run, row.get("friendly", "")
    for k in ("_live_buf", "live_ant", "live_tx", "_live_info", "_mav_span", "_ids_seen", "_save_from_w", "_save_to_w", "_save_label_w"):
        s.pop(k, None)
    s["_live_start_t"] = row.get("start_t")
    s["_live_follow_wall"] = time.time()        # views/1_live.py: the OFFLINE grace window starts when we start following a run
    for k in ("_last_ok_wall", "_snap_age_now", "_tick_dur_s", "last_snap", "_live_info_wall"):
        s.pop(k, None)
    D.set_source("live")
    D.reset_derived()


def _run_selected() -> None:
    run = s.get("_run_w") or s.get("live_run")                          # widget-backed key: gone when the selectbox unmounted (KeyError 2026-09-15)
    if run:
        _follow(run)


def _connect() -> None:
    host = F.live_host(s)
    if not host:
        s["_connect_err"] = "no host: set an MRU number (1–99) or a custom host under Advanced"
        return
    if int(s.get("live_port") or 0) < 1024:   # a dropped widget key once left port 1 behind
        s["live_port"] = 27017
    if not str(s.get("live_db") or "").strip():
        s["live_db"] = "sensor_store"
    port, db = int(s["live_port"]), str(s["live_db"])
    with st.spinner(f"Connecting to {host}:{port}…"):
        res = F.connect(host, port, db, s.get("live_run_filter", ""))
    s["live_host"], s["live_probe"], s["_connect_note"], s["_connect_err"] = host, res["probe"], res["reason"], None
    if res["ok"] and res["run"]:
        _follow(res["run"], res["row"])
        s["_run_w"] = res["run"]
    elif res["ok"]:
        s["live_run"], s["live_run_name"] = "", ""
    st.rerun()


# ── LIVE: info panel (fragment) ──────────────────────────────────────────────
def _live_info_view() -> None:
    host, port, db, run = s["live_host"], int(s["live_port"]), s["live_db"], s["live_run"]
    cached = s.get("_live_info")
    if s.pop("_ds_main_run", False) and cached is not None:
        info = cached                                   # main script run (page switch / sidebar click): re-render, never query
    else:
        info = F.live_info(host, port, db, run, start_t=s.get("_live_start_t"))
        s["_live_info"], s["_live_info_wall"] = info, time.time()
    if info["ok"] and s.get("_live_start_t") is None and info.get("start_t"):
        s["_live_start_t"] = info["start_t"]
    stamp = time.strftime("%H:%M:%S", time.localtime(s.get("_live_info_wall") or time.time()))
    T.card_header("Live info", f"refreshed {stamp} · indexed queries over the last {int(info['window_s'])} s of data")
    if not info["ok"]:
        T.callout("OFFLINE", f"{host}:{port} · {run[:12]}… not answering", T.esc(info["err"] or ""), tone="red")
        return
    ids = {f["id"] for f in info["feeds"]}
    if ids - set(s.get("_ids_seen") or ()):                       # a NEW MAVLink id: the role card (main body) must learn it -> whole page rerun
        s["_ids_seen"] = sorted(set(s.get("_ids_seen") or ()) | ids)
        st.rerun(scope="app")
    age = info["data_age"]
    alive = sum(1 for f in info["feeds"] if f["state"] == "alive")
    n143, n103 = info["n143_w"], info["n103_w"]
    tone = _age_tone(age)
    tiles = [
        _tile("Last data", T.esc(D.fmt_age(age)), "ago", f"newest document {D.pdt_hms(info['newest_t'])} PDT", tone, AGE_WORD.get(tone)),
        _tile("Radar tracks · last 60 s", str(n143), "docs", (f"newest track {D.pdt_hms(info['newest_143_t'])}" if info["newest_143_t"] else "no TRACKS document yet"),
              "ok" if n143 else "na", "TRACKING"),
        _tile("Observations · last 60 s", str(n103), "dwells", f"{info['n106_w']} air-traffic docs · {info['n_adsb_docs_w']} ADS-B ignored", "ok" if n103 else "na", "DETECTING"),
        _tile("MAVLink feeds", str(len(info["feeds"])), "detected", (f"{alive} alive" if info["feeds"] else "waiting"),
              "ok" if alive else ("amber" if info["feeds"] else "fail"), "ALIVE" if alive else ("STALE" if info["feeds"] else "NO FEED")),
    ]
    st.markdown('<div class="ds-tiles">' + "".join(tiles) + "</div>", unsafe_allow_html=True)
    ant, tx = info.get("ant"), info.get("tx_lla")
    start = info.get("start_t")
    T.kv([
        ("Host", f"<b>{T.esc(host)}:{port}</b> · {T.esc(F.live_unit(s))} · ping <b>{info['latency_ms']}</b> ms"),
        ("Run", f"<b>{T.esc(s.get('live_run_name') or info.get('friendly') or 'unnamed')}</b> · <code>{T.esc(run)}</code>"),
        ("Start", (f"<b>{D.pdt_full(start)}</b> · {D.fmt_age(info['newest_t'] - start)} of data" if start else "unknown")),
        ("Antenna", (f"{ant[0]:.5f}, {ant[1]:.5f}, {ant[2]:.1f} m HAE" if ant else "unknown until a TRACKS document exists (truth cannot be placed in ENU)")),
        ("Transmitter", (f"{tx[0]:.5f}, {tx[1]:.5f}, {tx[2]:.1f} m HAE (bistatic geometry from the run)" if tx else "co-located with the receiver → bistatic = 2 × monostatic")),
    ])
    if info["feeds"]:
        rows = [[f["id"], D.role_of(f["id"]), f["rows"], D.pdt_hms(f["first_t"]), D.pdt_hms(f["last_t"]), D.fmt_age(f["age"]),
                 T.glyph({"alive": "ok", "stale": "amber", "down": "fail"}[f["state"]], f["state"].upper())] for f in info["feeds"]]
        T.table(["MAVLink id", "role", "rows (60 s)", "first", "last", "age", "state"], rows, num_cols={2, 5})
    else:
        T.callout("WAITING", "Waiting for MAVLink feeds",
                  f"no MAVLINK block-106 row in the last {int(info['window_s'])} s of data · {info['n_adsb_docs_w']} ADS-B documents ignored · "
                  f"rescanning every {RESCAN_S:g} s. Radar tracks render grey with no truth match until a feed appears.", tone="amber")


# ── role assignment (both modes) ────────────────────────────────────────────
def _apply_roles() -> None:
    err = D.set_roles(list(s.get("_tgt_ids_w") or []), list(s.get("_itc_ids_w") or []))
    if err:
        s["_roles_err"] = err


def _swap_roles() -> None:
    a = D.role_assignment()
    tgt, itc = list(a["target"]), list(a["interceptor"])
    if not tgt and not itc:                                        # nothing explicit yet: swap the pattern prefill
        tgt, itc = list(s.get("_tgt_ids_w") or []), list(s.get("_itc_ids_w") or [])
    D.set_roles(itc, tgt)


def _roles_card(ids: list[str], waiting_msg: str) -> None:
    T.card_header("MAVLink role assignment", "TARGET truth · INTERCEPTOR truth · several ids in one role = component-id merge")
    a = D.role_assignment()
    options = sorted(set(map(str, ids)) | set(a["target"]) | set(a["interceptor"]))
    if not options:
        T.callout("WAITING", "Waiting for MAVLink feeds", waiting_msg, tone="amber")
        return
    if a["target"] or a["interceptor"]:
        tgt = [i for i in a["target"] if i in options]
        itc = [i for i in a["interceptor"] if i in options]
    else:                                                          # prefill by the auto-assign patterns (refreshed until the operator assigns)
        tgt = [i for i in options if D.role_of(i) == "target"]
        itc = [i for i in options if D.role_of(i) == "interceptor"]
    s["_tgt_ids_w"], s["_itc_ids_w"] = tgt, itc
    c1, c2, c3 = st.columns([3, 3, 1])
    c1.multiselect("TARGET truth (MAVLink ids)", options, key="_tgt_ids_w", on_change=_apply_roles,
                   help="The MAVLink target_ids whose positions are the TARGET drone's truth. Several ids (component ids of one vehicle) are merged into one truth.")
    c2.multiselect("INTERCEPTOR truth (MAVLink ids)", options, key="_itc_ids_w", on_change=_apply_roles,
                   help="The MAVLink target_ids whose positions are the INTERCEPTOR's truth. An id can never be in both roles.")
    c3.button("Swap", key="swap_roles", on_click=_swap_roles, width="stretch", help="Exchange the TARGET and INTERCEPTOR assignments.")
    err = s.pop("_roles_err", None)
    if err:
        T.callout("REFUSED", "One id in both roles", T.esc(err), tone="amber")
    T.kv([
        ("Now", T.esc(D.roles_label()) + (" · <b>explicit</b>" if a["target"] or a["interceptor"] else " · from the auto-assign patterns")),
        ("Rule", f"explicit assignment → patterns <code>{T.esc(s.get('tgt_pattern'))}</code> / <code>{T.esc(s.get('itc_pattern'))}</code> (whole-token match) → "
                 "built-in 14550 / 14551 / system-id rule. A change re-merges the truth on the next tick (live buffers are re-read)."),
    ])


# ── LIVE: save to archive ───────────────────────────────────────────────────
def _epoch_on(day: str, tm: datetime.time) -> float:
    return datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=tm.hour, minute=tm.minute, second=tm.second, tzinfo=D.PDT).timestamp()


def _switch_saved(n: int) -> None:
    D.clear_flight_caches()
    D.refresh_registry()
    D.set_ds_mode("archive")
    D.set_flight(int(n))


def _job_view(job: dict) -> None:
    if job["state"] == "running":
        @st.fragment(run_every=1.0)
        def _prog() -> None:
            j = AR.job(job["id"]) or job
            st.progress(min(1.0, max(0.0, float(j["frac"]))), text=f"saving… {100 * j['frac']:.0f} % · {j['msg']}")
            if j["state"] != "running":
                st.rerun(scope="app")
        _prog()
    elif job["state"] == "done":
        res = job["result"]
        if s.get("_save_registered") != job["id"]:                 # the worker appended flights.json; the registry refresh happens here, on the main thread
            D.clear_flight_caches()
            D.refresh_registry()
            s["_save_registered"] = job["id"]
        m = res["meta"]
        T.callout("SAVED", f"{res['label']} · flight {res['n']} · {D.fmt_age((job['t_end'] or time.time()) - job['t_start'])}",
                  f"<code>{T.esc(res['dir'])}/</code><br><b>{m['mavlink_rows']}</b> MAVLink rows ({T.esc(', '.join(m['mavlink_ids']) or 'no MAVLink truth')}) · "
                  f"<b>{m['tracks']}</b> tracks / {m['track_rows']} states (NED {m['layouts']['ned']} · ECEF {m['layouts']['ecef']}) · "
                  f"<b>{m['obs_rows']}</b> observations · appended to {res['day']}/flights.json", tone="green")
        st.button("Switch to this flight", type="primary", key="switch_saved", on_click=_switch_saved, args=(res["n"],), width="stretch",
                  help="Replay the saved window through the archive path (Data source → ARCHIVE REPLAY).")
    else:
        T.callout("SAVE FAILED", "The archive job stopped", T.esc(job.get("error") or "unknown error"), tone="red")


@st.cache_data(ttl=30.0, show_spinner=False)
def _preview_cached(host: str, port: int, db: str, run: str, t0: float, t1: float) -> dict:
    return AR.preview(host, port, db, run, t0, t1)


def _preview(host: str, port: int, db: str, run: str, t0: float, t1: float) -> dict:
    """AR.preview's document counts, CACHED on a 15 s window bucket (ttl 30 s).  The save card lives in an expander, whose
    body Streamlit executes even while collapsed, so this counting query used to run on every rerun of the page."""
    q = 15.0
    return _preview_cached(host, int(port), db, run, round(float(t0) / q) * q, round(float(t1) / q) * q)


def _save_card(info: dict) -> None:
    T.card_header("Save to archive", "quickdump layout + obs/obs.csv + truth-match columns → a replayable flight")
    if s.pop("save_focus", False):
        T.callout("SAVE", "Save requested from the Live page", "Pick the window below and press Save to archive.", tone="amber")
    host, port, db, run = s["live_host"], int(s["live_port"]), s["live_db"], s["live_run"]
    newest = info.get("newest_t") or time.time()
    start_t = s.get("_live_start_t") or info.get("start_t")
    day, r8 = AR.day_of(newest), AR.run8(run)
    span = s.get("_mav_span")
    if span is None:
        span = F.mavlink_span(host, port, db, run)
        s["_mav_span"] = span
    if span.get("first_t") and span.get("last_t") and span["last_t"] > span["first_t"]:
        d0, d1, note = span["first_t"], span["last_t"], "prefilled with the run's MAVLink truth window (first → last MAVLINK row)"
    else:
        d0, d1, note = newest - 600.0, newest, "no MAVLink truth in this run — prefilled with the last 10 minutes of data"
    s.setdefault("_save_scope_w", "last")
    scope = st.radio("What to save", list(SCOPE_LABEL), key="_save_scope_w", horizontal=True, format_func=SCOPE_LABEL.get,
                     help="Last N minutes up to the newest data · an explicit PDT time range (prefilled with the MAVLink truth window) · the whole run.")
    if scope == "last":
        s.setdefault("_save_min_w", 10)
        mins = st.select_slider("Last N minutes (up to the newest data)", [2, 5, 10, 15, 30, 60], key="_save_min_w")
        t0, t1 = newest - 60.0 * float(mins), newest
    elif scope == "range":
        s.setdefault("_save_from_w", D.epoch_to_naive_pdt(d0).time().replace(microsecond=0))
        s.setdefault("_save_to_w", D.epoch_to_naive_pdt(d1).time().replace(microsecond=0))
        c1, c2 = st.columns(2)
        c1.time_input("From (PDT)", key="_save_from_w", step=1, help="Window start, PDT clock time on the run's day.")
        c2.time_input("To (PDT)", key="_save_to_w", step=1, help="Window end, PDT clock time (a time before From wraps to the next day).")
        t0, t1 = _epoch_on(day, s["_save_from_w"]), _epoch_on(day, s["_save_to_w"])
        if t1 <= t0:
            t1 += 86400.0
        st.caption(note)
    else:
        t0, t1 = float(start_t or newest - 3600.0), newest
    s.setdefault("_save_label_w", D.next_flight_label(day, r8))
    st.text_input("Label", key="_save_label_w", help="Folder suffix and flight name: <day>/<run8>_<label>/ and 'Flight N' in the archive list.")
    out_dir = os.path.join(D.ARCHIVE_ROOT, day, f"{r8}_{AR.safe_label(s['_save_label_w'])}")
    pv = _preview(host, port, db, run, t0, t1)
    docs = (f"<b>{pv['n106']}</b> air traffic · <b>{pv['n143']}</b> tracks · <b>{pv['n103']}</b> dwells (documents in the window)" if pv["ok"]
            else f"count unavailable: {T.esc(pv['err'] or '')}")
    T.kv([("Window", f"<b>{D.pdt_hms(t0)}–{D.pdt_hms(t1)}</b> PDT · {D.fmt_age(t1 - t0)}"), ("Output", f"<code>{T.esc(out_dir)}/</code>"), ("Documents", docs)])
    job = AR.job(s.get("_save_job"))
    if st.button("Save to archive", type="primary", key="save_btn", width="stretch", disabled=bool(job and job["state"] == "running"),
                 help="Runs in the background (indexed 120 s chunks, long socket timeout); progress shows below; the page stays usable."):
        s["_save_job"] = AR.start_save(host=host, port=port, db=db, run=run, t0=t0, t1=t1, label=s["_save_label_w"], mru=s.get("mru_number"),
                                       roles=D.roles_key(), root=D.ARCHIVE_ROOT)
        st.rerun()
    if job:
        _job_view(job)


# ── LIVE section ─────────────────────────────────────────────────────────────
def _live_section() -> None:
    T.card_header("Connect to a radar unit", "enter the unit number, press Connect — the newest run with recent data is followed automatically")
    box = st.container(key="ds_connect")                             # keyed container: the big-type CSS for the connect row scopes to it
    c1, c2 = box.columns([1, 2])
    s["_mru_w"] = int(s.get("mru_number") or 91)
    c1.number_input("MRU number", min_value=1, max_value=99, step=1, key="_mru_w", on_change=_mru_changed,
                    help="The unit number; its mx node is 10.1NN.28.205 (MRU91 → 10.191.28.205). Advanced overrides the host.")
    host = F.live_host(s)
    c2.markdown(f'<div class="ds-resolve">→ <b>{T.esc(host or "no host")}</b>:{int(s["live_port"])} · {T.esc(s["live_db"])}'
                f'{" · custom host" if str(s.get("live_custom_host") or "").strip() else " · 10.1NN.28.205 convention"}</div>', unsafe_allow_html=True)
    with st.expander("Advanced: custom host, port, database, run filter, auto-assign patterns, history"):
        a1, a2, a3 = st.columns([2, 1, 1])
        # Streamlit drops WIDGET-BACKED session keys when a widget unmounts (this expander is collapsed most of the time): a bare
        # key="live_port" came back as the input's minimum (port 1 -> "Connection refused", 2026-09-15) and live_db as "".  Each
        # field therefore edits a widget key seeded from the real state and copies back on change (the MRU number does the same).
        s["_host_w"] = str(s.get("live_custom_host") or "")
        a1.text_input("Custom host or IP (overrides the MRU number)", key="_host_w", placeholder="100.77.96.104", on_change=lambda: s.__setitem__("live_custom_host", str(s.get("_host_w") or "").strip()),
                      help="A Tailscale address or name when the LAN convention does not apply. Leave empty to use 10.1NN.28.205.")
        s["_port_w"] = int(s.get("live_port") or 27017)
        a2.number_input("Mongo port", key="_port_w", step=1, min_value=1, max_value=65535, on_change=lambda: s.__setitem__("live_port", int(s.get("_port_w") or 27017)),
                        help="Mongo TCP port on the unit (27017 by default).")
        s["_db_w"] = str(s.get("live_db") or "sensor_store")
        a3.text_input("Database name", key="_db_w", on_change=lambda: s.__setitem__("live_db", str(s.get("_db_w") or "sensor_store").strip() or "sensor_store"),
                      help="Mongo database holding the run_<hex> collections (sensor_store).")
        b1, b2, b3 = st.columns(3)
        b1.text_input("Run name filter (substring)", key="live_run_filter", placeholder="8e3fcceb",
                      help="Only list run collections whose name contains this text (applied on Connect).")
        b2.text_input("Target auto-assign patterns", key="tgt_pattern",
                      help="Comma-separated whole tokens that mark a MAVLink id as TARGET truth (default 14550, mavlink_1).")
        b3.text_input("Interceptor auto-assign patterns", key="itc_pattern",
                      help="Comma-separated whole tokens that mark a MAVLink id as INTERCEPTOR truth (default 14551, mavlink_2).")
        s["_hist_w"] = int(s.get("hist_s", D.STATE_DEFAULTS["hist_s"]))          # widget key decoupled from the value key: widget keys are
        st.select_slider("History carried per snapshot (s)", [60, 120, 180, 300, 600], key="_hist_w", on_change=_hist_w,   # dropped when the widget unmounts
                         help="How many seconds of truth and track history the live view keeps (the first fetch is bounded to this; later ticks page forward).")
    if box.button("Connect", type="primary", key="connect_btn", width="stretch",
                 help="Probe the unit, list its runs newest first and follow the newest run with recent radar / air-traffic data."):
        _connect()
    if s.get("_connect_err"):
        T.callout("NO HOST", "Nothing to connect to", T.esc(s["_connect_err"]), tone="amber")
    pr = s.get("live_probe")
    if pr is None:
        T.callout("NOT CONNECTED", "Set the MRU number and press Connect",
                  "Every server operation has an 8 s timeout, so nothing blocks the UI. The Live page keeps showing the archive replay until a run is followed.", tone="grey")
        return
    if not pr["ok"]:
        T.callout("NO REPLY", f"{s['live_host']}:{s['live_port']} did not answer within {F.PROBE_TIMEOUT_MS // 1000} s",
                  T.esc(pr["err"] or "") + f"<br>probed {time.strftime('%H:%M:%S', time.localtime(pr['ts']))} · check the MRU number / VPN, then Connect again.", tone="red")
        return
    rows = pr["runs"]
    if not rows:
        T.callout("CONNECTED", f"{s['live_host']}:{s['live_port']} · no run collections", f"{pr.get('n_total', 0)} run_* collections match the filter.", tone="grey")
        return
    names = [r["name"] for r in rows]
    labels = {r["name"]: F.run_label(r, pr["ts"]) for r in rows}
    s["_run_w"] = s.get("live_run") if s.get("live_run") in names else names[0]
    T.card_header(f"Run to follow · {len(names)} run collections · newest first",
                  f"probed {time.strftime('%H:%M:%S', time.localtime(pr['ts']))} · ping {pr['latency_ms']} ms · {pr.get('n_total', len(names))} on the unit")
    st.selectbox("Run collection", names, key="_run_w", on_change=_run_selected, format_func=lambda n: labels.get(n, n),
                 help="name · id8 · start (PDT) · age of the newest document. Selecting a run follows it immediately.")
    if s.get("_connect_note"):
        st.caption(f"auto-selected on Connect: {s['_connect_note']}")
    if not s.get("live_run"):
        st.button("Follow this run", key="use_run", type="primary", width="stretch", on_click=_run_selected)
        T.callout("CONNECTED", "Not following a run yet", "Pick a run above and press Follow this run.", tone="grey")
        return
    waiting = not ((s.get("_live_info") or {}).get("feeds"))
    st.fragment(run_every=RESCAN_S if waiting else INFO_S)(_live_info_view)()
    info = s.get("_live_info") or {}
    ids = sorted(set(s.get("_ids_seen") or ()) | {f["id"] for f in info.get("feeds", [])})
    roles = D.role_assignment() if hasattr(D, "role_assignment") else {}
    roles_open = not (roles.get("target") and roles.get("interceptor")) if isinstance(roles, dict) else True
    with st.expander("MAVLink truth roles (target · interceptor)", expanded=bool(roles_open)):
        _roles_card(ids, f"no MAVLINK block-106 row in the last {int(F.INFO_WINDOW_S)} s of the run's data — rescanning every {RESCAN_S:g} s "
                         f"({info.get('n_adsb_docs_w', 0)} ADS-B documents ignored). Assign roles as soon as an id appears.")
    with st.expander("Save to archive", expanded=bool(s.get("save_focus"))):
        _save_card(info)


# ── ARCHIVE section ──────────────────────────────────────────────────────────
def _flight_changed() -> None:
    D.set_flight(int(s["_flight_w"]))
    D.set_source("archive")


def _hist_w() -> None:
    s["hist_s"] = int(s["_hist_w"])


def _archive_section() -> None:
    T.card_header("Archive replay", "replay = live-path test harness · the 8/28 Seawall flights + every flight saved from a live run")
    flights = D.refresh_registry()
    nums = sorted(flights)
    if not nums:
        T.callout("NO ARCHIVE DATA", "nothing to replay yet",
                  f"{T.esc(D.data_hint())}<br>Expected: <code>{T.esc(D.BASE)}/flights.json</code> + <code>&lt;run8&gt;_&lt;flight&gt;_quickdump/</code> dirs, "
                  f"or saved flights under <code>{T.esc(D.ARCHIVE_ROOT)}/&lt;YYYY-MM-DD&gt;/</code> (LIVE mode → \"Save data to archive\").", tone="amber")
        return
    if int(s["flight"]) not in nums:
        D.set_flight(nums[0])
    fl = int(s["flight"])
    s["_flight_w"] = fl
    st.selectbox("Flight to replay", nums, key="_flight_w", on_change=_flight_changed, format_func=D.flight_label,
                 help="day · flight · window (PDT) · verified passes. Selecting a flight seeks to 20 s before its window, paused.")
    # Replay transport (seek/play/speed/jump-to-pass) lives where the plots are: the sidebar in
    # archive mode and the Live page. Here: choose the flight and go.
    if st.button("Load flight and open Live view", type="primary", width="stretch", key="ds_open_live",
                 help="Seeks to 20 s before the flight window (paused) and switches to the Live page."):
        D.set_flight(fl)
        try:
            st.switch_page("views/1_live.py")
        except Exception:
            pass
    b = D.archive_bundle(fl, D.roles_key())
    fi = D.flight_info(fl)
    T.card_header(f"Archive contents · {D.flight_label(fl)}")
    rows = [
        ("Truth", f"<b>{len(b['tgt'])}</b> target + <b>{len(b['itc'])}</b> interceptor samples · MAVLink CSVs "
                  f"({T.esc(', '.join(r['name'] for r in b['raw']) or 'none')})"),
        ("Cleaning", "validposition only · stale republishes dropped · 0.5 s dedup across feeds · &gt;250 m/s teleports dropped · no interpolation across &gt;2.5 s gaps"),
        ("Tracks", f"<b>{b['n_tracks']}</b> radar tracks · <b>{len(b['tracks'])}</b> states · track_*.csv (E/N/U about the antenna, σ, last_update_t, track_state)"
                   + (f" · <b>{sum(1 for m in b['track_meta'].values() if m.get('truth_match_id'))}</b> with a radar truth match" if b.get("track_meta") else "")),
        ("Antenna", f"{b['ant'][0]:.5f}, {b['ant'][1]:.5f}, {b['ant'][2]:.1f} m HAE"),
        ("Clock", "replay starts 20 s before the flight window · ticks at 1 s"),
    ]
    if fi.get("dir"):
        src = fi.get("source") or {}
        rows.insert(0, ("Saved from", f"<b>{T.esc(src.get('host') or '—')}</b> · run <code>{T.esc(str(src.get('run') or '')[:16])}</code> · {T.esc(src.get('saved_at_pdt') or '')}"
                                      f" · <code>{T.esc(fi['dir'])}</code>"))
    T.kv(rows)
    _roles_card([r["name"] for r in b["raw"]], "this archive flight has no MAVLink CSVs.")


def _channel_card() -> None:
    T.card_header("Live panel data channel", f"port {LS.DEFAULT_PORT} · env IH_LIVE_PORT")
    T.kv([
        ("Panel data", f"The Live page's charts update in-browser from <code>http://&lt;this host&gt;:{LS.DEFAULT_PORT}/figs.json</code> "
                       f"(and load <code>plotly.min.js</code> from the same port), so <b>port {LS.DEFAULT_PORT} must be reachable from the browser</b> "
                       "over LAN and Tailscale alike. If it is not, the panel's status reads <b>unreachable</b>; if the dashboard cannot bind it, the page falls back to plain Streamlit charts."),
        ("Status", (f"<b>serving</b> on 0.0.0.0:{LS.port()}" if LS.is_up() else f"<b>not bound</b>{' — ' + T.esc(LS.last_error()) if LS.last_error() else ''} (fallback charts)")),
    ])


# ── page ─────────────────────────────────────────────────────────────────────
st.markdown(PAGE_CSS, unsafe_allow_html=True)
with st.container(key="ds_page"):
    T.headline("DATA SOURCE", "Live radar unit, or a recorded flight", "")
    with st.container(key="ds_mode_box"):
        s["_ds_mode_w"] = s.get("ds_mode", "live")
        st.radio("Data source mode", list(MODE_LABEL), key="_ds_mode_w", horizontal=True, on_change=_mode_changed, format_func=MODE_LABEL.get,
                 help="LIVE follows a radar unit's mongo run; ARCHIVE REPLAY plays a recorded flight through the same live code path.")
    if s["ds_mode"] == "live":
        _live_section()
    else:
        _archive_section()
    with st.expander("Connection details (panel data port)"):   # technical: folded away (user: "the page does not need to be that complicated")
        _channel_card()
T.footer(f"{T.esc(F.live_unit(s) if D.is_live() else D.UNIT)} · {D.SITE} · ARCHIVE REPLAY = LIVE-PATH TEST HARNESS · PANEL DATA ON :{LS.DEFAULT_PORT} (BROWSER MUST REACH IT)")
