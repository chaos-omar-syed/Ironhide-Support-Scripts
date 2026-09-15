"""Satellite basemap tiles (the ONLY part of the author's track_correlation/live_correlator.py the dashboard
uses — vendored under the same module name so ``ih.data.sat_payload`` keeps working).

Esri World Imagery tiles are fetched at RUN TIME over the internet (nothing is bundled) and composited into a
dimmed JPEG for the given EN box about the antenna.  Needs Pillow; offline the caller gets ``{"err": ...}``.
"""
import math

import corr_lib as C

ANT_LL = [None]       # (lat, lon) once known — enables the satellite basemap
SAT_CACHE = {}        # quantized EN box -> satmap JSON payload
SAT_MAX_TILES = 48    # tile budget per composite (7x7 at the chosen zoom): wide views drop z until they fit
SAT_WORKERS = 8       # concurrent Esri tile GETs

def _satmap_payload(x0, x1, y0, y1):
    """Fetch+composite Esri World Imagery tiles for an EN box (m about the
    antenna), pre-dimmed so plot lines read on top. Lazy imports — heavy
    modules must never load at server startup (background exit-144 lesson)."""
    key = (round(x0, -2), round(x1, -2), round(y0, -2), round(y1, -2))
    if key in SAT_CACHE:
        return SAT_CACHE[key]
    if ANT_LL[0] is None:
        return {"err": "antenna unknown"}
    import io as _io
    import base64 as _b64
    import urllib.request as _rq
    from PIL import Image, ImageEnhance
    olat, olon = ANT_LL[0]
    _fr = C.EnuFrame((olat, olon, 0.0))                              # exact WGS-84 corners (the old 111 320 m/deg placed the tile ~0.36 % off in N)
    latmn, lonmn, _ = _fr.lla(x0, y0, 0.0)
    latmx, lonmx, _ = _fr.lla(x1, y1, 0.0)

    def d2n(lat, lon, z):
        n = 2 ** z
        xr = (lon + 180.0) / 360.0 * n
        yr = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return xr, yr

    def n2d(x, y, z):
        n = 2 ** z
        return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))),
                x / n * 360.0 - 180.0)
    z = 18   # <= 1.2 m/px: z18 (0.6 m/px at 34° N) for boxes up to ~1.7 km; z drops until the box needs <= SAT_MAX_TILES tiles (wide views)
    while z > 10:
        xa_, ya_ = d2n(latmx, lonmn, z)
        xb_, yb_ = d2n(latmn, lonmx, z)
        xa, xb = int(min(xa_, xb_)), int(max(xa_, xb_))
        ya, yb = int(min(ya_, yb_)), int(max(ya_, yb_))
        if (xb - xa + 1) * (yb - ya + 1) <= SAT_MAX_TILES:
            break
        z -= 1
    im = Image.new("RGB", ((xb - xa + 1) * 256, (yb - ya + 1) * 256), (40, 40, 40))
    got = 0
    from concurrent.futures import ThreadPoolExecutor

    def _get(xy):
        xt, yt = xy
        try:
            url = (f"https://server.arcgisonline.com/ArcGIS/rest/services/"
                   f"World_Imagery/MapServer/tile/{z}/{yt}/{xt}")
            return xy, Image.open(_io.BytesIO(_rq.urlopen(url, timeout=6).read()))
        except Exception:
            return xy, None
    coords = [(xt, yt) for xt in range(xa, xb + 1) for yt in range(ya, yb + 1)]
    with ThreadPoolExecutor(max_workers=SAT_WORKERS) as ex:   # the tiles used to be fetched one after another (~3 s for 30): the whole box lands in well under a second
        for (xt, yt), tile in ex.map(_get, coords):
            if tile is not None:
                im.paste(tile, ((xt - xa) * 256, (yt - ya) * 256))
                got += 1
    if not got:
        return {"err": "no tiles (offline?)"}
    im = ImageEnhance.Brightness(im).enhance(0.38)          # dim harder -> lines pop
    lt, lolf = n2d(xa, ya, z)
    lb, lort = n2d(xb + 1, yb + 1, z)
    buf = _io.BytesIO()
    im.save(buf, format="JPEG", quality=72)
    out = {"img": "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode(),
           "x0": _fr.enu(lt, lolf, 0.0)[0], "x1": _fr.enu(lb, lort, 0.0)[0],           # composite extents through the same exact frame as the corners
           "y0": _fr.enu(lb, lolf, 0.0)[1], "y1": _fr.enu(lt, lolf, 0.0)[1]}
    if len(SAT_CACHE) > 24:
        SAT_CACHE.clear()
    SAT_CACHE[key] = out
    return out

