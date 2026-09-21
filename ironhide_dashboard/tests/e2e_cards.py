"""Real-browser "jump inventory" of the time-series CARD figures (sep / err / vel) — the user (2026-09-15, screenshot of the
four error cards with their y tick labels cut at the left edge: "200" -> "00", "−2.5" without its sign): "Can we also make
these plots jump a little less".  Selenium + headless Firefox against a running Streamlit instance (tests/e2e_browser.py
helpers); not collected by ``pytest tests/`` (no ``test_`` prefix).  Run explicitly:

  cd ironhide_dashboard && python tests/e2e_cards.py [--sizes=1366x768] [--texts=Normal,Large] [--secs=40] [--tag=after]
  (without IH_E2E_URL a side instance is started; as pytest: -m pytest -q tests/e2e_cards.py)

Every second of a 1x replay of flight 1 from 07:21:40 (across the 07:21:57 target-track handover) the sampler inside the
panel iframe records, per card figure: ``_fullLayout._size`` (l / t / w / h = the plot area), every y axis range + rendered
tick label texts + label boxes, the x range + tick texts, the header-strip annotation boxes, and flags a y tick label whose
box starts left of the figure (clipped) or a strip annotation overlapping the plot area.  The inventory = how many samples
changed each metric.  Verdict (after the first 3 s): _size never changes; a y range changes <= 2 times per card and never
twice within 10 s; the tick label SET changes only when its range changes; 0 clipped labels; 0 strip / plot overlaps."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_browser as E  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not E.HAVE_TOOLS, reason="geckodriver / firefox / selenium missing")]

T0 = "07:21:40"
FIGS = ("sep", "err", "vel")
SETTLE_S = 3.0                    # the first reacts (fonts / geometry fit) are allowed to move things
RANGE_DWELL_S = 10.0
MAX_RANGE_CHANGES = 2

SAMPLER_JS = r"""
var MS=arguments[0];
window.__ihCards={rows:[]};
function r1(v){ return Math.round(v*10)/10; }
window.__ihCards.tick=function(){
  var row={t:Math.round(performance.now()), figs:{}};
  ['sep','err','vel'].forEach(function(id){ var gd=document.getElementById(id); if(!gd||!gd._fullLayout) return;
    var fl=gd._fullLayout, sz=fl._size, g=gd.getBoundingClientRect(), f={size:{l:r1(sz.l),t:r1(sz.t),w:r1(sz.w),h:r1(sz.h)}, W:r1(g.width), H:r1(g.height), ax:{}, clipped:[], overlaps:[], ann:{}, font:(fl.font&&fl.font.size)||null, marginL:fl.margin.l};
    var pa={l:g.left+sz.l, r:g.left+sz.l+sz.w, t:g.top+sz.t, b:g.top+sz.t+sz.h}, rowRects=[];   // the plot area of EVERY subplot row (the header strips sit in the gaps between them)
    Object.keys(fl).forEach(function(k){ if(!/^yaxis\d*$/.test(k)) return; var ya=fl[k]; if(!ya||!ya.range) return;
      var sub=ya._mainSubplot||('xy'+k.replace('yaxis','')), labels=[], boxes=[], dom=ya.domain||[0,1], rowRect={l:pa.l, r:pa.r, t:g.top+sz.t+sz.h*(1-dom[1]), b:g.top+sz.t+sz.h*(1-dom[0])};
      rowRects.push(rowRect);
      gd.querySelectorAll('.subplot.'+sub+' .ytick text').forEach(function(t){ var r=t.getBoundingClientRect(); if(!r.width) return; labels.push(t.textContent); boxes.push({l:r1(r.left-g.left), r:r1(r.right-g.left), w:r1(r.width)}); if(r.left<g.left-0.5) f.clipped.push({ax:k, text:t.textContent, by:r1(g.left-r.left)}); });
      var xk=k.replace('y','x'), xa=fl[xk]||fl.xaxis, xl=[]; if(xa&&xa.range) gd.querySelectorAll('.subplot.'+sub+' .xtick text').forEach(function(t){ if(t.getBoundingClientRect().width) xl.push(t.textContent); });
      f.ax[k]={range:ya.range.map(function(v){ return Math.round(v*1e4)/1e4; }), ticks:labels, boxes:boxes, xrange:xa&&xa.range?xa.range.slice():null, xticks:xl, dom:ya.domain?ya.domain.map(r1):null}; });
    gd.querySelectorAll('.infolayer g.annotation').forEach(function(a){ var t=a.querySelector('text'), txt=t?t.textContent.trim():''; if(!txt) return; var r=a.getBoundingClientRect();
      var nm=(a.getAttribute('data-index')||''); f.ann[txt.slice(0,18)+'#'+nm]={l:r1(r.left-g.left), t:r1(r.top-g.top), w:r1(r.width), h:r1(r.height)};
      rowRects.forEach(function(rr){ var ox=Math.min(r.right,rr.r)-Math.max(r.left,rr.l), oy=Math.min(r.bottom,rr.b)-Math.max(r.top,rr.t); if(ox>3&&oy>3) f.overlaps.push({text:txt.slice(0,40), by:r1(oy)}); }); });
    row.figs[id]=f; });
  window.__ihCards.rows.push(row);
};
window.__ihCards.iv=setInterval(window.__ihCards.tick, MS);
window.__ihCards.tick();
return true;
"""
STOP_JS = "try{ clearInterval(window.__ihCards.iv); }catch(e){} return (window.__ihCards||{}).rows||[];"


def _changes(vals: list) -> int:
    """How many consecutive-sample transitions changed the value."""
    return sum(1 for a, b in zip(vals, vals[1:]) if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True))


def inventory(rows: list, settle_s: float = SETTLE_S) -> dict:
    """Per figure: samples, changes of _size, per axis: range / tick-set / tick-box / x-range changes, clipped labels, overlaps,
    and the range change TIMES (s) for the dwell check.  Samples in the first ``settle_s`` are dropped."""
    if not rows:
        return {}
    t0 = rows[0]["t"]
    keep = [r for r in rows if (r["t"] - t0) / 1000.0 >= settle_s]
    inv = {}
    for fid in FIGS:
        fr = [r["figs"][fid] for r in keep if fid in r["figs"]]
        ts = [(r["t"] - t0) / 1000.0 for r in keep if fid in r["figs"]]
        if not fr:
            continue
        d = {"samples": len(fr), "size_changes": _changes([f["size"] for f in fr]), "sizes": sorted({json.dumps(f["size"]) for f in fr}),
             "clipped_samples": sum(1 for f in fr if f["clipped"]), "clipped_examples": [c for f in fr for c in f["clipped"]][:4],
             "overlap_samples": sum(1 for f in fr if f["overlaps"]), "overlap_examples": [c for f in fr for c in f["overlaps"]][:4],
             "ann_box_changes": _changes([{k: (v["l"], v["t"], v["h"]) for k, v in f["ann"].items() if not k.startswith(("now", "last", "track", "no "))} for f in fr]),
             "axes": {}}
        for k in sorted({k for f in fr for k in f["ax"]}):
            rng = [f["ax"][k]["range"] if k in f["ax"] else None for f in fr]
            tk = [f["ax"][k]["ticks"] if k in f["ax"] else None for f in fr]
            bx = [[(b["l"], b["w"]) for b in f["ax"][k]["boxes"]] if k in f["ax"] else None for f in fr]
            xr = [f["ax"][k]["xrange"] if k in f["ax"] else None for f in fr]
            xt = [f["ax"][k]["xticks"] if k in f["ax"] else None for f in fr]
            change_t = [ts[i + 1] for i in range(len(rng) - 1) if rng[i] != rng[i + 1]]
            tick_change_t = [ts[i + 1] for i in range(len(tk) - 1) if tk[i] != tk[i + 1]]
            d["axes"][k] = {"range_changes": len(change_t), "range_change_t": [round(v, 1) for v in change_t], "ranges": sorted({json.dumps(r) for r in rng}),
                            "tick_set_changes": len(tick_change_t), "tick_change_t": [round(v, 1) for v in tick_change_t], "tick_box_changes": _changes(bx),
                            "xrange_changes": _changes(xr), "xtick_set_changes": _changes(xt), "n_xticks": sorted({len(x) for x in xt if x is not None})}
        inv[fid] = d
    return inv


def verdict(inv: dict, tag: str) -> list[str]:
    """The failures (empty = green)."""
    bad = []
    for fid, d in inv.items():
        if d["size_changes"]:
            bad.append(f"{tag} {fid}: plot area (_size) changed {d['size_changes']}x: {d['sizes'][:4]}")
        if d["clipped_samples"]:
            bad.append(f"{tag} {fid}: clipped y tick labels in {d['clipped_samples']} samples: {d['clipped_examples']}")
        if d["overlap_samples"]:
            bad.append(f"{tag} {fid}: header strip over the plot area in {d['overlap_samples']} samples: {d['overlap_examples']}")
        for k, a in d["axes"].items():
            if a["range_changes"] > MAX_RANGE_CHANGES:
                bad.append(f"{tag} {fid}.{k}: y range changed {a['range_changes']}x at {a['range_change_t']}")
            ct = a["range_change_t"]
            if any(b - x < RANGE_DWELL_S for x, b in zip(ct, ct[1:])):
                bad.append(f"{tag} {fid}.{k}: two y range changes within {RANGE_DWELL_S:.0f} s: {ct}")
            extra = [t for t in a["tick_change_t"] if t not in ct]
            if extra and fid != "sep":            # the separation card autoranges (the flight-long window grows): its ticks may move with the data
                bad.append(f"{tag} {fid}.{k}: tick label set changed without a range change at {extra}")
    return bad


def sample(base: str, w: int, h: int, text: str, secs: float, tag: str = "") -> tuple[dict, list]:
    name = f"cards_{w}x{h}_{text.replace('-', '')}" + (f"_{tag}" if tag else "")
    b = E.Browser(w, h, name)
    try:
        b.open_live(base, flight=1, t=T0, play=1)
        E._sidebar_radio(b, "Text size", text)       # ALWAYS set it: the default scale is per screen preset
        b.wait_panel()
        b.wait_made()
        time.sleep(1.0)
        b.pjs(SAMPLER_JS, 1000)
        time.sleep(float(secs))
        rows = b.pjs(STOP_JS)
        b.shot("cards")
        inv = inventory(rows)
        E._dump(b, "cards", {"rows": rows, "inventory": inv})
        errs = b.console_errors()
        return inv, errs
    finally:
        b.quit()


def _print(inv: dict, tag: str) -> None:
    for fid, d in inv.items():
        print(f"[{tag}] {fid}: n={d['samples']} size_changes={d['size_changes']} clipped_samples={d['clipped_samples']} overlaps={d['overlap_samples']} ann_box_changes={d['ann_box_changes']} sizes={d['sizes'][:3]}")
        for k, a in d["axes"].items():
            print(f"    {k}: range_changes={a['range_changes']} @{a['range_change_t']} tick_set_changes={a['tick_set_changes']} tick_box_changes={a['tick_box_changes']} xrange_changes={a['xrange_changes']} xtick_set_changes={a['xtick_set_changes']} n_xticks={a['n_xticks']} ranges={a['ranges'][:3]}")


@pytest.mark.parametrize("text", ["Normal", "Large"])
def test_cards_steady_1366x768(app, text):
    inv, errs = sample(app, 1366, 768, text, 40.0)
    _print(inv, f"1366x768 {text}")
    bad = verdict(inv, f"1366x768 {text}")
    assert not bad, "\n".join(bad)
    assert not errs, errs


if __name__ == "__main__":
    url = os.environ.get("IH_E2E_URL"); proc = None
    root = os.environ.get("IH_E2E_ROOT") or E.ROOT
    if not url:
        port, lport = E.free_port(), E.free_port()
        env = {**os.environ, "IH_LIVE_PORT": str(lport), "PYTHONPATH": E.PYPATH}
        os.makedirs(E.SHOTS, exist_ok=True)
        log = open(os.path.join(E.SHOTS, "side_cards.log"), "w")
        proc = subprocess.Popen([E.STREAMLIT, "run", "app.py", "--server.port", str(port), "--server.headless", "true"], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        url = f"http://127.0.0.1:{port}"; t0 = time.time()
        while time.time() - t0 < 90 and not E._http_ok(url + "/healthz"):
            time.sleep(0.5)
    sizes, texts, secs, tag = ((1366, 768),), ("Normal", "Large"), 40.0, ""
    for arg in sys.argv[1:]:
        if arg.startswith("--sizes="): sizes = tuple(tuple(int(x) for x in p.lower().split("x")) for p in arg.split("=", 1)[1].split(","))
        elif arg.startswith("--texts="): texts = tuple(arg.split("=", 1)[1].split(","))
        elif arg.startswith("--secs="): secs = float(arg.split("=", 1)[1])
        elif arg.startswith("--tag="): tag = arg.split("=", 1)[1]
    try:
        for (w, h) in sizes:
            for tx in texts:
                inv, errs = sample(url.rstrip("/"), w, h, tx, secs, tag)
                lab = f"{w}x{h} {tx} {tag}".strip()
                _print(inv, lab)
                for line in verdict(inv, lab):
                    print("  FAIL", line)
                if errs:
                    print("  console:", errs[:3])
    finally:
        if proc is not None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except Exception: pass
