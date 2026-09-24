"""Review fixes for the radar map: outline casing adapts to the alert colour, SPC outlook
holes shared with a higher category are not stroked twice, civil products get their EAS
level, and the basemap no longer depends on CARTO (API-key watermark tiles since 2026).

Reuses test_radar's helpers (solid-colour basemaps, the real TO.W.0032 / TXZ035 shapes).
"""
import io
import urllib.request

import pytest

from safety import config
from safety import nws_alerts as na
from safety import radar as rd
from safety.tests.test_radar import (SPC_MRGL, TOW_32, TXZ035, _alert, _cfg,  # noqa: F401
                                     _render, _thumb, _xy, offline)


def _theme(theme):
    t = rd.Thumbnailer.__new__(rd.Thumbnailer)
    t.colors = rd._THEME[theme]
    return t


# ---- casing -------------------------------------------------------------------------
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_nws_outline_stands_out_from_the_basemap_once_cased(theme):
    t = _theme(theme)
    base = t.colors["base"]
    for event, hexcolor in na.EVENT_COLORS.items():
        rgb = rd._never_green(rd._hex_rgb(hexcolor), rd.KIND_RGB[rd._event_kind(event)])
        casing = t._casing_for(rgb)
        # the line shows against the map by its own colour or by its casing...
        assert max(rd._contrast(rgb, base), rd._contrast(casing, base)) >= 3.0, event
        # ...and the colour still reads against its casing
        assert rd._contrast(rgb, casing) >= 3.0, event


def test_pale_and_dark_alert_colours_get_the_opposite_ink():
    light, dark = _theme("light"), _theme("dark")
    for pale in ("#FFE4C4", "#FFFF00", "#FFE4B5", "#AFEEEE"):   # dust storm, tornado watch...
        assert light._casing_for(rd._hex_rgb(pale)) == rd._THEME["light"]["ink"]
    for deep in ("#8B0000", "#4B0082"):                          # flash flood, hazmat
        assert dark._casing_for(rd._hex_rgb(deep)) == rd._THEME["dark"]["ink"]
    red = (255, 0, 0)                                  # enough contrast: the usual casing
    assert light._casing_for(red) == (255, 255, 255) and dark._casing_for(red) == (0, 0, 0)


@pytest.mark.usefixtures("offline")
def test_a_pale_dust_storm_veto_is_cased_in_ink_on_the_day_map(tmp_path):
    from PIL import Image
    t = _thumb(_cfg(tmp_path), "light")
    t._basemap = Image.new("RGB", t._basemap.size, rd._THEME["light"]["base"])
    img = _render(t, [_alert("KLUB.DS.W.0002", "Dust Storm Warning", "#FFE4C4", 0, TOW_32,
                             vetoes=True)])
    x, y = _xy(t, 33.79, -101.6788)                     # on the polygon's east edge
    row = [img.getpixel((i, y)) for i in range(x - 8, x + 9)]
    assert (255, 228, 196) in row and rd._THEME["light"]["ink"] in row


# ---- SPC outlook holes ---------------------------------------------------------------
def _ring(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


@pytest.mark.usefixtures("offline")
def test_spc_hole_shared_with_a_higher_category_is_stroked_once(tmp_path):
    from PIL import Image
    t = _thumb(_cfg(tmp_path), "dark")
    t._basemap = Image.new("RGB", t._basemap.size, (16, 20, 30))
    outer = _ring(-102.8, 33.0, -101.1, 34.5)
    slgt = _ring(-102.3, 33.4, -101.6, 34.1)
    island = _ring(-102.7, 34.2, -102.5, 34.4)          # a genuine no-risk hole
    mrgl = {"kind": "spc_outlook", "key": "m", "label": "MRGL", "geometry": {
        "type": "Polygon", "coordinates": [outer, slgt[::-1], island[::-1]]}}
    sl = {"kind": "spc_outlook", "key": "s", "label": "SLGT",
          "geometry": {"type": "Polygon", "coordinates": [slgt]}}
    img = _render(t, [mrgl, sl])
    sand = rd._THEME["dark"]["mrgl"]
    # along SLGT's west edge only SLGT's yellow is drawn, never MRGL's sand dashes
    x, _ = _xy(t, 33.75, -102.3)
    col = [img.getpixel((xx, yy)) for yy in range(_xy(t, 34.0, 0)[1], _xy(t, 33.5, 0)[1])
           for xx in range(x - 2, x + 3)]
    assert rd.SPC_RGB["SLGT"] in col and sand not in col
    # the island's (dashed) east edge is still MRGL's outline
    xi, _ = _xy(t, 34.3, -102.5)
    edge = [img.getpixel((i, yy)) for yy in range(_xy(t, 34.4, 0)[1], _xy(t, 34.2, 0)[1])
            for i in range(xi - 2, xi + 3)]
    assert sand in edge
    assert rd._on_shared(slgt[::-1], rd._outer_vertices([sl["geometry"]]))
    assert not rd._on_shared(island, rd._outer_vertices([sl["geometry"]]))


def test_civil_products_fall_back_to_their_eas_level():
    assert rd._event_kind("Evacuation Immediate") == "warning"
    assert rd._event_kind("Civil  Emergency Message") == "warning"
    assert rd._event_kind("Local Area Emergency") == "statement"
    assert rd._event_kind("911 Telephone Outage") == "statement"
    assert rd._CIVIL_KINDS == na.CIVIL_KINDS


# ---- basemap -------------------------------------------------------------------------
def test_default_tiles_need_no_key_and_attribution_matches():
    assert "cartocdn" not in config.RADAR_TILE_URL + config.RADAR_TILE_URL_DAY
    assert config.RADAR_TILE_URL == config.RADAR_TILE_URL_DAY == config.OSM_TILE_URL
    assert config.RADAR_TILE_DARK_INVERT is True
    assert config.RADAR_ATTRIBUTION.startswith("© OpenStreetMap contributors · ")


class _Resp:
    def __init__(self, body, cache_control):
        self._b, self.headers = io.BytesIO(body), {"Cache-Control": cache_control}

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _png(rgb):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), rgb).save(buf, format="PNG")
    return buf.getvalue()


def test_night_basemap_inverts_light_tiles_and_refuses_blocked_ones(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    land = (242, 239, 233)                             # OSM's land colour
    replies = {"cc": "max-age=86400"}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=25: _Resp(_png(land), replies["cc"]))
    cfg = _cfg(tmp_path, RADAR_TILE_DARK_INVERT=True)
    night = rd.Thumbnailer(cfg, config.OSM_TILE_URL, str(tmp_path / "n.png"), "dark")
    night._build_basemap()
    px = night._basemap.getpixel((5, 5))
    inverted = rd.invert_lightness(Image.new("RGB", (1, 1), land)).getpixel((0, 0))
    assert px == inverted and max(px) < 40             # the night map is dark
    day = rd.Thumbnailer(cfg, config.OSM_TILE_URL, str(tmp_path / "d.png"), "light")
    day._build_basemap()
    assert day._basemap.getpixel((5, 5)) == land       # the day map is not inverted
    cached = sorted(p.name for p in (tmp_path / "cache").iterdir())
    assert len(cached) == 2                            # separate cache keys per theme
    # OSM's "Access blocked" tile: HTTP 200 + Cache-Control: no-cache -> never cached
    replies["cc"] = "no-cache"
    cfg2 = _cfg(tmp_path / "b", RADAR_TILE_DARK_INVERT=True)
    blocked = rd.Thumbnailer(cfg2, config.OSM_TILE_URL, str(tmp_path / "b.png"), "light")
    blocked._build_basemap()
    assert not (tmp_path / "b" / "cache").exists()
    with pytest.raises(ValueError, match="not cacheable"):
        rd._get("https://tile.openstreetmap.org/8/1/1.png", refuse_uncacheable=True)
    assert rd._get("https://example.invalid/frame.png")      # frames: header not checked
