"""Ironhide test dashboard — entrypoint (Streamlit multipage via st.navigation).

Pages: DATA SOURCE (default landing: connect to a live MRU by number, or pick an archive flight) · LIVE.
The sidebar is MODE-AWARE (ih.data.is_live):
  LIVE    -> the unit block: MRU · IP:port · run · last-data age glyph · TGT / INT truth mapping · Disconnect · refresh interval
  ARCHIVE -> the replay transport: play / restart / speed / jump-to-pass buttons (verified passes) (+ clock)
and ONE "Display" section with every view control (screen size, text size, metrics window, path history, map frame,
zoom, reset view, CPA gate, satellite, blind rings, raw obs, freeze).  Session-state contract keys the pages / engine read:
mode ("live" | "archive", mirrors source), role_ids, mru_number, frame_mode, text_scale — see ih.data.STATE_DEFAULTS.

Run (see README.md):
  ./run_dashboard.sh                       # detached, logs to dashboard.log
  streamlit run app.py --server.fileWatcherType none   # foreground
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from ih import data as D  # noqa: E402  (also puts track_correlation + chaos-spa on sys.path)
from ih import feed as F  # noqa: E402
from ih import liveserver as LS  # noqa: E402
from ih import theme as T  # noqa: E402

T.setup()
D.init_state()
D.apply_query_params()   # ?flight=1&t=07:22:31[&play=1] deep link (once per session)


@st.cache_resource(show_spinner=False)
def _panel_server_boot(port: int):
    """The panel data server (:8902), up from process start (Data source is the landing page: a user arriving there must find the
    port bound before the first Live-page run).  cache_resource keeps the server OBJECT across Streamlit module hot-reloads;
    LS.adopt re-binds the (reset) module globals to it — the same pattern as views/1_live.py (idempotent: start() is a singleton)."""
    return LS.start(port)


LS.adopt(_panel_server_boot(LS.DEFAULT_PORT) or LS.start(LS.DEFAULT_PORT))   # second call = the 30 s bind retry after a failure; adopt(None) is a no-op
s = st.session_state


AGE_WORD = {"ok": "LIVE", "amber": "STALE", "fail": "LOST"}   # last-data age words (shared with the Data-source LIVE INFO tile)


def age_cls(age: float | None) -> str:
    """Last-data age class: ok < 10 s (live) · amber < 60 s · fail beyond · na unknown."""
    if age is None:
        return "na"
    return "ok" if age <= F.LIVE_STALE_S else ("amber" if age <= 60 else "fail")


def _age_glyph(age: float | None) -> str:
    """Last-data age as icon + word + age: green LIVE · amber STALE · red LOST · muted unknown."""
    cls = age_cls(age)
    if cls == "na":
        return T.glyph("na", "NO DATA YET")
    return f"{T.glyph(cls, AGE_WORD[cls])} <b>{T.esc(D.fmt_age(age))}</b> ago"


with st.sidebar:
    st.markdown('<div style="display:flex;flex-direction:column;gap:4px;padding:4px 0"><div class="ih-wordmark"><span class="sq"></span>CHAOS</div>'
                '<div class="ih-crumb">Ironhide test dashboard</div></div>', unsafe_allow_html=True)
    T.side_label("Source")
    if D.is_live():
        run = s.get("live_run") or ""
        run_name = s.get("live_run_name") or (run[4:12] if run else "")
        unit = F.live_unit(s)
        st.markdown(T.source_line_html("live", mru=s.get("mru_number") if not str(s.get("live_custom_host") or "").strip() else None,
                                       host=s.get("live_host") or "", run=run_name if run else ""), unsafe_allow_html=True)
        snap = s.get("last_snap") or {}
        # LAST DATA is the age of the newest document NOW, from the LAST GOOD snapshot (views/1_live.py keeps _snap_age_now
        # = that tick's data_age + how long ago the tick ran).  It must survive a failed / slow tick: the sidebar read
        # "NO DATA YET" while a perfectly good snapshot was on screen (2026-09-15).
        age = s.get("_snap_age_now")
        if age is None:
            age = snap.get("data_age")
        T.kv([
            ("Unit", f"<b>{T.esc(unit)}</b> · {T.esc(s.get('live_host') or '—')}:{s.get('live_port')}"),
            ("Run", (f"<b>{T.esc(run_name)}</b> · <code>{T.esc(run[:12])}…</code>" if run else "<b>not following a run</b> — Data source page")),
            ("Last data", _age_glyph(age)),
            ("Truth roles", T.esc(D.roles_label())),
        ])
        st.button("Save data to archive…", type="primary", key="side_save", on_click=D.goto_save, width="stretch",
                  help="Save a time range of this run (MAVLink + tracks + obs) as a replayable archive flight — opens the save card on the Data source page.")
        st.button("Disconnect", on_click=D.disconnect, width="stretch", help="Stop following the run (buffers dropped); the Live page falls back to the archive replay.")
        st.select_slider("Refresh interval (s)", [1.0, 2.0, 5.0], key="refresh_s", format_func=lambda v: f"{v:g}",
                         help="How often the Live panel fetches and redraws in live mode (the archive replay always ticks every 1 s). "
                              "A slow unit stretches the EFFECTIVE cadence to the last tick's duration + 0.5 s so the fetches never pile up.")
        # the EFFECTIVE cadence (views/1_live.py: max(refresh interval, last tick + 0.5 s)) — on MRU91 one tick can take 3 s,
        # and a 1 s interval then queued every page interaction behind a backlog of ticks.
        # computed HERE (the sidebar renders before the page script, so views/1_live.py's CADENCE_S is one rerun behind)
        _set = max(1.0, float(s.get("refresh_s", 2.0) or 2.0))
        _dur = float(s.get("_tick_dur_s") or 0.0)
        _cad = round(max(_set, _dur + 0.5), 1)
        s["_cadence_s"] = _cad
        st.caption(f"ticks every {_cad:g} s" + (f" · last fetch {_dur:.1f} s" if _dur else "")
                   + (f" · interval set to {_set:g} s" if _cad > _set else ""))
    elif not D.flight_numbers():
        st.caption("No archive flights found — see the Data source page (data/README.md) or connect to a live unit.")
    else:
        fl = int(s["flight"])
        fi = D.flight_info(fl)
        st.markdown(T.source_line_html("archive", day=D.day_md(str(fi.get("day") or D.DAY)), flight=(fi.get("label") or f"Flight {fl}").replace("Flight ", ""),
                                       window=f"{D.pdt_hms(D.FLIGHT_WINDOWS[fl][0])}–{D.pdt_hms(D.FLIGHT_WINDOWS[fl][1])} PDT"), unsafe_allow_html=True)
        T.side_label("Replay transport")
        # FULL WIDTH, one per row: in a 2-column sidebar these two labels are ~110-130 px of text in a ~103 px box, so
        # "Restart replay" (and "Pause replay" at X-Large) wrapped onto two lines and the row grew (clip audit 2026-09-14).
        st.button("Pause replay" if s["playing"] else "Play replay", on_click=D.toggle_play, width="stretch", help="Start or pause the archive replay clock.")
        st.button("Restart replay", on_click=D.restart, width="stretch", help="Jump back to 20 s before the flight window and play.")
        # TIME SLIDER (2026-09-15): scrub the replay clock across the flight window.  Widget key seeded from the clock each main run
        # (never a bare session key: Streamlit drops widget-backed keys on unmount); dragging seeks and PAUSES so the frame stays put.
        _t0, _t1 = D.FLIGHT_WINDOWS[int(s["flight"])]
        _lo, _hi = int(_t0 - D.REPLAY_LEAD_S), int(_t1)
        s["_seek_w"] = int(min(max(D.now_t(), _lo), _hi))
        def _seek_from_slider():
            D.seek(float(s["_seek_w"]), keep_playing=False)
        st.slider("Replay time", min_value=_lo, max_value=_hi, step=1, key="_seek_w", format=" ", on_change=_seek_from_slider,
                  help="Drag to any moment of the flight (pauses the replay; press Play to continue).")
        st.caption(f"{D.pdt_hms(_lo)}  ◂  {D.pdt_hms(float(s['_seek_w']))}  ▸  {D.pdt_hms(_hi)}")
        st.radio("Replay speed (× real time)", [1.0, 2.0, 4.0], key="speed", horizontal=True, format_func=lambda v: f"{v:g}×", on_change=D.speed_changed,
                 help="How fast the replay clock runs compared with real time (applies from now on; the clock never jumps).")
        verified = [p for p in D.passes(fl) if p.get("verified")]
        if verified:                                                     # jump-to-pass lives with the transport (where the plots are), never on the Data-source page
            T.side_label("Jump to pass")
            for i, p in enumerate(verified):            # one per row: "Pass 1 · 07:21:43 · 78 m" needs ~200 px and wrapped onto
                miss = p.get("miss_m")                  # 2-3 lines in a half-width sidebar column (clip audit 2026-09-14)
                label = f"Pass {p.get('n', i + 1)} · {D.pdt_hms(p['t'])}" + (f" · {float(miss):.0f} m" if miss is not None else "")
                st.button(label, key=f"jump_pass_{fl}_{p.get('n', i + 1)}", on_click=D.jump_to_pass, args=(p["t"],), width="stretch",
                          help=f"Seek to {D.PASS_LEAD_S:g} s before this pass and play. Truth-truth closest approach {float(miss):.0f} m." if miss is not None
                          else f"Seek to {D.PASS_LEAD_S:g} s before this pass and play.")
        st.caption(f"clock {D.pdt_hms(D.now_t())} · {'playing' if s['playing'] else 'paused'} · ticks every {D.REPLAY_TICK_S:g} s")
    # 1366x768: the Display section alone scrolls 2093 px (Normal) / 2583 px (Large) in a 682 px sidebar, so on the Laptop
    # preset it starts COLLAPSED and the four rarely-touched toggles live one level deeper ("More controls").
    # The nested expander is NOT called "Advanced": tests/e2e_browser.py clicks the FIRST /Advanced/ summary in the
    # document (the Data-source custom-host section) and the sidebar precedes the main block in the DOM.
    _laptop = str(s.get("screen", "")) == "Laptop"
    with st.expander("Display", expanded=not _laptop):
        st.radio("Screen size", list(LS.PRESETS), key="screen", horizontal=True, on_change=D.bump_view_rev,
                 help="Fits the one-screen panel to your display: Laptop (pick this on a 1366x768 / 13-14 in screen — it also "
                      "drops the text size to Normal), Desktop 1080p (default) or Large 1440p and up. "
                      "The panel also adapts to the window width by itself; this sets its height and text size.")
        if "text_scale" not in s:                                   # the widget's default follows the screen preset (ih.theme.text_scale maps the label)
            s["text_scale"] = T.TEXT_SCALE_DEFAULT.get(str(s.get("screen", "")), "Normal")   # Large 1440p -> X-Large, Desktop 1080p -> Large, Laptop -> Normal
        st.radio("Text size", ["Normal", "Large", "X-Large"], key="text_scale", horizontal=True, on_change=D.bump_view_rev,
                 help="Scales every label, tile, caption and plot font (default: Large on Desktop 1080p, X-Large on Large 1440p).")
        st.select_slider("Metrics time window (s)", [60, 120, 300], key="spec_window",
                         help="How many seconds of history the error statistics and the right-hand plots cover.")
        st.select_slider("Path history on map (s)", [10, 15, 20, 25, 30], key="trail_s",
                         help="How many seconds of each vehicle's path the map draws behind it.")
        st.radio("Map frame", list(D.FRAME_MODES), key="frame_mode", horizontal=True, format_func=D.FRAME_MODES.get, on_change=D.bump_view_rev,
                 help="Engagement box (default): frames the recent action / the gated passes and re-frames gently. Follow vehicles: re-centres on the "
                      "vehicles when one nears the edge. Fit whole flight: the whole footprint, never re-centres. Fixed: a box of the zoom half-width.")
        st.select_slider("Map zoom (m)", [400, 800, 1500, 3000], key="map_half", disabled=D.frame_mode() not in ("follow", "fixed"),
                         on_change=D.bump_view_rev,
                         help="Follow / Fixed frames only: distance from the map centre to its edge; smaller = closer zoom.")
        st.button("Reset map view", on_click=D.bump_view_rev, width="stretch",
                  help="Release a locked (zoomed / panned) map view and re-apply the current frame.")
        st.slider("Closest-approach gate (m)", 20.0, 200.0, step=10.0, format="%.0f", key="cpa_gate_m",
                  help="A closest approach counts as a CPA (gold star, hairlines, label) only when the two vehicles come within this "
                       "distance AND then separate again — so a launch or a pass that never gets close is not declared a CPA.")
        with st.expander("More controls", expanded=False):
            st.toggle("Satellite imagery", key="show_sat", help="Esri World Imagery under the map (cached tiles).")
            st.toggle("Radar blind-range rings (pulse width)", key="show_blind",
                      help="Dotted rings at the blind range c·(pulse width)/2 of each waveform mode: inside them the radar cannot detect.")
            st.toggle("Show uncorrelated radar tracks (ADS-B, clutter) while MAVLink truth is present", key="show_free_tracks",
                      help="Off: only tracks correlated to a MAVLink drone draw while truth is present (uncorrelated tracks always draw when there is no truth at all).")
            st.toggle("Show raw radar detections on error plots", key="show_obs",
                      help="Raw radar observations (block 103) as small ✕ marks on the track-quality plots, as observation minus target-truth errors.")
            st.toggle("Freeze display (pause updates)", key="freeze",
                      help="Stop redrawing while you zoom or inspect. The clock and data ingest keep running; switch off to catch up.")

# NOTE the page scripts live in views/, NOT pages/: a pages/ directory next to the entry script switches Streamlit to its legacy
# multipage discovery for a fresh process, so a direct /live URL ran views/1_live.py STANDALONE (no sidebar controls, no CSS, no deep
# link) until some session had run st.navigation once.  With no pages/ directory the entry script always runs first.
pages = [
    st.Page("views/3_data_source.py", title="Data source", default=True),
    st.Page("views/1_live.py", title="Live"),
]
pg = st.navigation(pages)
if s.pop("_goto_save", False):          # Live-page "Save…" shortcut (D.goto_save): land on the Data source page's save card
    st.switch_page(pages[0])
pg.run()
