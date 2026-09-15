"""Animated SVG vehicle icons (data URIs) for the map + chips.

interceptor(): Vespid-class silhouette — slender body, ogive nose, mid-body swept
wings, cruciform tail fins, nozzle — with a flickering exhaust and a soft halo.
target(): quadcopter top-down — X frame, four rotor discs (spinning), body pod, halo.

Both are drawn nose-up (heading 0° = north) in a 100x100 viewBox centred at (50,50)
and rotated by `heading_deg` (clockwise from north, i.e. degrees(atan2(vE, vN))).
Animation is SMIL inside the SVG document.  On the map the icons ARE Plotly layout
images in DATA coordinates (x/y = E/N, sizex/sizey in metres — ih.plots.heads_images),
so they can never detach from the trails: the same axis transform positions both.  To
keep the SMIL animations from restarting every tick, the heading is quantised to
HDG_STEP_DEG buckets and the data URI is cached per (role, bucket, colours) — Plotly
only re-sets an <image>'s href when its ``source`` string changes, so the halo /
exhaust / rotors keep running while only x/y move.  The same markup (animate=False)
is inlined in the status-strip feed glyphs and the "More" chips.

Colours default to the ih.theme dark tokens (interceptor #3987e5, target #e66767, page
surface #101215, exhaust = warning amber, nozzle = secondary ink).
"""
from functools import lru_cache
from urllib.parse import quote

HDG_STEP_DEG = 5.0   # heading quantisation step for the cached map icons


def _uri(svg: str) -> str:
    return "data:image/svg+xml;utf8," + quote(svg)

def _halo(color: str) -> str:
    return (f'<circle cx="50" cy="50" r="30" fill="none" stroke="{color}" stroke-width="1.5" opacity=".35">'
            f'<animate attributeName="r" values="26;34;26" dur="2.4s" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values=".35;.05;.35" dur="2.4s" repeatCount="indefinite"/></circle>')

def interceptor_svg(heading_deg: float = 0.0, color: str = "#3987e5", surface: str = "#101215",
                    halo: bool = True, size: int = 100, exhaust_color: str = "#f5b942", nozzle_color: str = "#b3b9c2",
                    animate: bool = True) -> str:
    """Nose-up Vespid silhouette. Body ~64 units long, wings ~52 span, fins ~26 span.
    animate=False -> no SMIL at all (static glyphs re-rendered every tick, e.g. the status strip)."""
    a1 = ('<animate attributeName="points" values="-3,0 3,0 0,8; -3.5,0 3.5,0 0,12; -3,0 3,0 0,8" dur=".28s" repeatCount="indefinite"/>'
          '<animate attributeName="opacity" values=".9;.55;.9" dur=".28s" repeatCount="indefinite"/>') if animate else ""
    a2 = '<animate attributeName="points" values="-1.6,0 1.6,0 0,4; -2,0 2,0 0,6; -1.6,0 1.6,0 0,4" dur=".2s" repeatCount="indefinite"/>' if animate else ""
    exhaust = ('<g transform="translate(50,84)">'
               f'<polygon points="-3,0 3,0 0,9" fill="{exhaust_color}" opacity=".9">{a1}</polygon>'
               f'<polygon points="-1.6,0 1.6,0 0,5" fill="#ffffff" opacity=".85">{a2}</polygon></g>')
    body = (
        # wings (mid-body, swept back), drawn first so the body overlaps their roots
        f'<path d="M50,52 L24,64 L26,68 L50,60 L74,68 L76,64 Z" fill="{color}" opacity=".85"/>'
        # tail fins (cruciform: two lateral + one vertical seen edge-on as a thin bar)
        f'<path d="M50,74 L37,84 L39,86 L50,80 L61,86 L63,84 Z" fill="{color}" opacity=".85"/>'
        f'<rect x="49.2" y="72" width="1.6" height="12" fill="{color}"/>'
        # body: ogive nose to tail
        f'<path d="M50,16 C54.5,22 56,30 56,40 L56,78 C56,81 54.5,83 52,83.5 L48,83.5 C45.5,83 44,81 44,78 L44,40 C44,30 45.5,22 50,16 Z" '
        f'fill="{color}" stroke="{surface}" stroke-width="1.2"/>'
        # panel line + nozzle
        f'<line x1="44" y1="50" x2="56" y2="50" stroke="{surface}" stroke-width=".8" opacity=".7"/>'
        f'<rect x="46.5" y="83" width="7" height="3" fill="{nozzle_color}"/>'
    )
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">'
           f'<g transform="rotate({heading_deg:.1f} 50 50)">{_halo(color) if (halo and animate) else ""}{exhaust}{body}</g></svg>')
    return svg

def target_svg(heading_deg: float = 0.0, color: str = "#e66767", surface: str = "#101215",
               halo: bool = True, size: int = 100, animate: bool = True) -> str:
    """Nose-up quadcopter top-down: X frame, 4 spinning rotors, body pod, nose mark.
    animate=False -> static rotors, no halo (status-strip glyphs)."""
    arms = f'<path d="M50,50 L30,30 M50,50 L70,30 M50,50 L30,70 M50,50 L70,70" stroke="{color}" stroke-width="3.2" stroke-linecap="round"/>'
    def rotor(cx, cy, dur, rev):
        d = f'{dur}s'
        direction = "360" if not rev else "-360"
        return (f'<g transform="translate({cx},{cy})">'
                f'<circle r="11" fill="{color}" opacity=".18"/>'
                f'<circle r="11" fill="none" stroke="{color}" stroke-width="1.2" opacity=".7"/>'
                f'<rect x="-10.5" y="-1.3" width="21" height="2.6" rx="1.3" fill="{color}" opacity=".9">'
                + (f'<animateTransform attributeName="transform" type="rotate" from="0" to="{direction}" dur="{d}" repeatCount="indefinite"/>' if animate else "") + '</rect>'
                f'<circle r="2" fill="{surface}" stroke="{color}" stroke-width="1"/></g>')
    rotors = rotor(30, 30, .45, False) + rotor(70, 30, .5, True) + rotor(30, 70, .5, True) + rotor(70, 70, .45, False)
    pod = (f'<rect x="43" y="40" width="14" height="22" rx="5" fill="{color}" stroke="{surface}" stroke-width="1.2"/>'
           f'<polygon points="50,34 46,41 54,41" fill="{surface}" stroke="{color}" stroke-width="1.2"/>')   # nose marker
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">'
           f'<g transform="rotate({heading_deg:.1f} 50 50)">{_halo(color) if (halo and animate) else ""}{arms}{rotors}{pod}</g></svg>')
    return svg

def interceptor(heading_deg: float = 0.0, **kw) -> str:
    return _uri(interceptor_svg(heading_deg, **kw))

def target(heading_deg: float = 0.0, **kw) -> str:
    return _uri(target_svg(heading_deg, **kw))


def heading_q(hdg_deg: float, step: float = HDG_STEP_DEG) -> float:
    """Heading (deg clockwise from north) snapped to the nearest ``step`` bucket in [0, 360)."""
    q = round(float(hdg_deg) / step) * step
    return float(q % 360.0)


@lru_cache(maxsize=512)
def icon_uri(role: str, hdg_q: float, color: str, surface: str, animate: bool = True) -> str:
    """Cached data URI of the map icon for one (role, quantised heading, colours).
    role: 'tgt' | 'target' | 'itc' | 'interceptor'.  Same inputs -> the identical string,
    so Plotly keeps the <image> element (and its SMIL clocks) between relayouts."""
    fn = interceptor_svg if role in ("itc", "interceptor") else target_svg
    return _uri(fn(float(hdg_q), color=color, surface=surface, halo=True, animate=animate))
