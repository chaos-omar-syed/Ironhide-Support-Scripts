"""Vendored helpers (imported by module name — ``ih`` puts this directory on sys.path):

* corr_lib.py        — MRU Mongo loaders, ENU frame, MAVLink unit conversions, track/truth cleaning.
* spa_errors.py      — adapter from dashboard arrays to the chaos-spa grading core (REQUIRES chaos-spa).
* live_correlator.py — Esri World Imagery tile compositing for the satellite basemap (needs Pillow + internet).

Origin: the author's local track_correlation toolkit (same author as this dashboard).  Nothing here is
copied from chaos-spa; spa_errors only IMPORTS it.
"""
