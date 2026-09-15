# Ironhide test dashboard

Live radar test dashboard for MRU units (Streamlit + Plotly). It follows a unit's Mongo run collection in real
time, correlates the radar tracks with MAVLink drone truth, grades the target track (az / el / range / altitude /
velocity errors), shows the target–interceptor separation and CPA, and can save a time window of the run to a
replayable CSV archive.

* **Data source page** (landing) — *Connect*: type the MRU number (→ `10.1NN.28.205:27017`, or a custom host /
  Tailscale IP under *Advanced*), pick the run, assign MAVLink target ids to the TARGET / INTERCEPTOR roles;
  *Archive replay*: pick a saved flight; *Save data to archive*: dump a window of the live run.
* **Live page** — one-screen panel: map (satellite basemap, truth + tracks, blind-range rings), separation /
  CPA, error time series, measurement-space quad; tiles for track health; sidebar transport (archive) or unit
  block (live) plus the display controls (screen preset, text size, metrics window, frame mode, freeze …).
* Archive replay runs the **same engine as live** (it is the live-path test harness).

## Requirements

* Linux, Python 3.13 (`requirements.txt` is pinned to what runs today: streamlit 1.63.0, plotly 6.8.0,
  polars 1.41.2, pymongo 4.10.1, numpy 2.4.6, pandas 3.0.3, pillow 12.2.0, pymap3d 3.2.0, scipy 1.17.1).
* Two TCP ports reachable from the browsers that will use it, over LAN and Tailscale alike:
  **8901** (Streamlit) and **8902** (the panel data server — the Live page's charts and `plotly.min.js` load from
  it in the browser; if it is unreachable the panel status reads *unreachable*, if it cannot bind the page falls
  back to plain Streamlit charts).
* Network access to the unit's Mongo (`10.1NN.28.205:27017`, no auth) for live mode; internet access for the
  Esri satellite tiles (optional — the map works without them).
* Only for the browser tests: Firefox + geckodriver (or Chrome, see `tests/e2e_chrome.py`).

## Install

```bash
git clone … && cd Ironhide-Support-Scripts/ironhide_dashboard
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt        # or: ./run_dashboard.sh --venv .venv
# dev / tests:
.venv/bin/pip install -r requirements-dev.txt
```

## Run / stop

```bash
./run_dashboard.sh                    # detached (setsid + nohup), logs to dashboard.log, prints LAN + Tailscale URLs
./run_dashboard.sh --port 8901 --panel-port 8902 --python /path/to/python
./run_dashboard.sh --foreground       # streamlit in this terminal
./stop_dashboard.sh [--port 8901]
```

`run_dashboard.sh` launches `streamlit run app.py --server.fileWatcherType none` (hot-reload OFF — Streamlit's
file watcher resets module singletons such as the panel server; never run the served instance with it on).

## Configuration

| what | where |
|------|-------|
| Streamlit port / bind address / dark theme | `.streamlit/config.toml` (`[server] port = 8901`, `address = "0.0.0.0"`) — `--server.port` on the command line overrides |
| Panel data server port | `IH_LIVE_PORT` (default 8902) |
| Built-in 8/28 archive day | `IH_QUICKDUMP_DIR` (default `data/2026-08-28`) — see `data/README.md` |
| Saved flights root ("Save to archive") | `IH_ARCHIVE_ROOT` (default `data/archives`) |
| Optional raw-obs npz for built-in flight 1 | `IH_OBS_NPZ` |
| Optional chaos-spa grader | `IH_SPA_SRC=<chaos-spa>/src` (or install it); `IH_NO_SPA=1` forces the legacy grader |
| Optional local track_correlation checkout | `IH_TRACK_CORRELATION=<dir>` (adds `blindzone_map` → blind-range rings; the dashboard's own copies of `corr_lib` / `spa_errors` / `live_correlator` live in `ih/vendor/`) |
| plotly.min.js served to the panel | `IH_PLOTLY_JS=<file>`; default = the copy bundled with the `plotly` python package (same plotly.js 3.6.0), else a CDN redirect |
| Screen presets | `ih/liveserver.py` `PRESETS` (Laptop 600 px, Desktop 1080p 860 px, Large 1440p 1180 px panel height); sidebar *Screen size* / `?screen=` |
| Deep link | `?flight=1&t=07:22:31[&play=1][&screen=Laptop][&ds=live]` |

## Data

Range data is **not** in the repo. `data/README.md` documents the two roots and the exact layout
(`flights.json` + `<run8>_<flight>_quickdump/{meta.json, mavlink/*.csv, tracks/track_*.csv, obs/obs.csv}`).
Without any data the Data source page shows where to put it and live mode is fully usable.
`quickdump.py HHMM HHMM <name> --run run_<hex> --mru NN --register` dumps a window of a live run from the command
line through the same writer as the Save card.

## Optional: chaos-spa (official grading)

By default the engine grades the target track with its **legacy in-house math** (the build badge in the page
footer reads `grader: legacy`; bistatic range = |Tx→T| + |Rx→T| − |Tx→Rx| with the TX position from the snapshot,
co-located TX → 2·range; range rate = the analytic opening rate). To get the **official chaos-spa grading**
(`grader: spa`, spa's gating / trimmed RMSE / velocity-state errors / eigen-axis containment), install the private
chaos-spa repo next to this checkout:

```bash
git clone git@bitbucket.org:caoscapital/chaos-spa.git
.venv/bin/pip install -e chaos-spa            # or: export IH_SPA_SRC=$PWD/chaos-spa/src   (or PYTHONPATH=…/chaos-spa/src)
```

`ih/vendor/spa_errors.py` is only an adapter — it imports `spa.*`; nothing from chaos-spa is copied here.
Tests that assert spa-graded numbers skip when `import spa` fails.

## Tests

Unit / AppTest suites (no browser). **Run `test_apptest.py`, `test_paths.py` and `test_live_mongo.py` each in its own
pytest process** — they cross-contaminate Streamlit state in one process — and **never let two pytest runs share
`IH_LIVE_PORT`**:

```bash
export IH_LIVE_PORT=8939 IH_QUICKDUMP_DIR=/path/to/2026-08-28      # the quickdump day is needed by most suites (else they skip)
E2E="--ignore=tests/e2e_browser.py --ignore=tests/e2e_overlay.py --ignore=tests/e2e_chrome.py --ignore=tests/e2e_laptop.py --ignore=tests/e2e_cards.py"
pytest -q tests/test_apptest.py -p no:cacheprovider
pytest -q tests/test_paths.py -p no:cacheprovider
pytest -q tests/test_live_mongo.py -p no:cacheprovider              # fake in-process Mongo; one test probes the real MRU39 if reachable
pytest -q tests/ $E2E --ignore=tests/test_apptest.py --ignore=tests/test_paths.py --ignore=tests/test_live_mongo.py -p no:cacheprovider
```

`tests/test_portable.py` checks the clean-checkout behaviour (no PYTHONPATH, no chaos-spa, no data).
Browser end-to-end (minutes; Firefox + geckodriver on PATH or `IH_GECKODRIVER` / `IH_FIREFOX`):
`pytest -q tests/e2e_browser.py [-k wrapper]`; `tests/e2e_overlay.py`, `tests/e2e_laptop.py`, `tests/e2e_cards.py`
(screenshots under `tests/_e2e_shots/`, git-ignored); Chrome variant `tests/e2e_chrome.py` (`IH_CHROME_BIN`,
`IH_CHROMEDRIVER`, or Selenium Manager downloads Chrome for Testing).

## Troubleshooting

* **Connect shows NO REPLY / probe timeout** — the unit's `:27017` is not reachable from this host (VPN / Tailscale
  down, unit off, wrong MRU number → check the resolved IP on the card). Custom host under *Advanced*.
* **Panel status "unreachable" / charts frozen** — the browser cannot reach `http://<host>:8902` (`IH_LIVE_PORT`):
  open the port on the LAN / Tailscale firewall. Server-side "not bound" → another process holds the port.
* **Port "fell to 1" / panel on a wrong port** — `IH_LIVE_PORT` was unset or non-numeric when Streamlit started;
  set it and restart (run_dashboard.sh exports it).
* **Things reset / duplicate servers after editing code** — Streamlit hot-reload re-ran the modules; always start
  with `--server.fileWatcherType none` (the script does) and restart to pick up changes.
* **Tests hang or fail on the panel port** — two pytest processes shared `IH_LIVE_PORT`; give each its own port and
  run the three big suites sequentially.
* **`grader: legacy` in the footer** — chaos-spa is not importable; see *Optional: chaos-spa*.
* **No archive flights** — see `data/README.md` (`IH_QUICKDUMP_DIR` / `IH_ARCHIVE_ROOT`).

`docs/CHANGES_2026-09-10.md` is the change log; `docs/design_*` hold the layout design canvases and their QA scripts.
