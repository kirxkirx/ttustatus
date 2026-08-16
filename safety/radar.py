"""MRMS radar component for the SafetyMonitor — a simple 50 km "any rain" ring.

Every poll (night only, like WU) it fetches the latest MRMS composite-reflectivity frame
from the Iowa Environmental Mesonet (free, no key), and:
  * CHECK: declares unsafe if any echo >= RADAR_DBZ is within RADAR_TRIGGER_KM of the dome.
    Deliberately simple — no upwind/downwind logic, just a plain radius.
  * THUMBNAIL: renders a TTU-centered map (dark OSM tiles from Carto, cached to disk so
    they are NOT re-downloaded every cycle) with the radar overlaid, the 50 km ring, and
    10 km / 10 mi scale bars, saved where the status page can load it.

Live check: unsafe while rain is within the ring, and for RADAR_LATCH_SEC (15 min) after
the LAST in-ring detection — a post-rain freeze that clear frames do NOT cancel, so the
roof does not reopen the instant a cell's edge leaves the ring, and a blind feed cannot
drop the veto either. The radar does NOT rely on the WU latch, which only arms when rain
reaches a nearby station, not for a ranged echo. Fail-safe: a fetch error or stale frame
with no recent detection => unavailable (no veto on its own). Needs Pillow; absent =>
radar disabled.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                       # pragma: no cover - optional dep
    Image = ImageDraw = ImageFont = None

from . import config as _config

log = logging.getLogger("ttu.safety.radar")

ARCHIVE = ("https://mesonet.agron.iastate.edu/archive/data/"
           "%Y/%m/%d/GIS/mrms/lcref_%Y%m%d%H%M.png")
GRID_W, GRID_H, PX, UL_LON, UL_LAT = 7000, 3500, 0.01, -129.995, 54.995
UA = {"User-Agent": _config.NWS_USER_AGENT}   # one configurable contact UA for all fetches


def site_in_coverage(lat, lon, margin_deg=0.5):
    """MRMS is a CONUS product (lon -130..-60, lat 20..55). Outside it every pixel read
    is out of bounds and the ring scan would report an eternally 'clear' frame — so the
    component must refuse to run there, loudly, instead of being silently safe."""
    return (UL_LAT - GRID_H * PX + margin_deg < lat < UL_LAT - margin_deg
            and UL_LON + margin_deg < lon < UL_LON + GRID_W * PX - margin_deg)


def deps_available():
    return Image is not None


# ---- HTTP -----------------------------------------------------------------
def _get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


# ---- geometry -------------------------------------------------------------
def latlon_to_px(lat, lon):
    return int(round((lon - UL_LON) / PX)), int(round((UL_LAT - lat) / PX))


def dest_point(lat, lon, dist_km, bearing_deg):
    br, p1, l1, dr = (math.radians(bearing_deg), math.radians(lat),
                      math.radians(lon), dist_km / 6371.0)
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(p1),
                         math.cos(dr) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _deg2num(lat, lon, z):
    n = 2 ** z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def _num2deg(xt, yt, z):
    n = 2 ** z
    return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yt / n)))), xt / n * 360 - 180)


def _dbz(idx):
    return None if idx == 255 else -32.0 + idx * 0.5


# ---- radar fetch + 50 km check --------------------------------------------
def latest_frame(max_back=8):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if now.minute % 2:
        now -= timedelta(minutes=1)
    for i in range(max_back):
        ts = now - timedelta(minutes=2 * i)
        try:
            req = urllib.request.Request(ts.strftime(ARCHIVE), method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return ts, ts.strftime(ARCHIVE)
        except Exception:
            continue
    return None, None


def check_rain(cfg, img):
    """Scan the radar grid within RADAR_TRIGGER_KM of the dome. Returns (in_ring, nearest_km,
    count) — nearest_km is the closest echo >= RADAR_DBZ, or None if the ring is clear."""
    lat0, lon0 = cfg.GEOCODE
    px = img.load()
    coslat = math.cos(math.radians(lat0))
    dlat = cfg.RADAR_TRIGGER_KM / 111.0
    dlon = cfg.RADAR_TRIGGER_KM / (111.0 * coslat)
    c0, r0 = latlon_to_px(lat0, lon0)
    dc = int(dlon / PX) + 1
    dr = int(dlat / PX) + 1
    nearest = None
    count = 0
    for r in range(max(0, r0 - dr), min(GRID_H, r0 + dr + 1)):
        lat = UL_LAT - r * PX
        for c in range(max(0, c0 - dc), min(GRID_W, c0 + dc + 1)):
            v = px[c, r]
            v = v if isinstance(v, int) else v[0]
            d = _dbz(v)
            if d is None or d < cfg.RADAR_DBZ:
                continue
            lon = UL_LON + c * PX
            dist = math.hypot((lat - lat0) * 111.0, (lon - lon0) * 111.0 * coslat)
            if dist <= cfg.RADAR_TRIGGER_KM:
                count += 1
                if nearest is None or dist < nearest:
                    nearest = dist
    return (nearest is not None), (round(nearest, 1) if nearest is not None else None), count


# ---- thumbnail (tiles cached to disk) -------------------------------------
def _dbz_color(d):
    if d is None or d < 5:
        return None
    for thr, c in [(65, (255, 255, 255)), (55, (230, 0, 140)), (50, (190, 0, 0)),
                   (45, (230, 60, 20)), (40, (240, 170, 0)), (35, (230, 230, 0)),
                   (30, (0, 210, 0)), (20, (0, 150, 60)), (10, (3, 120, 170)), (5, (4, 63, 120))]:
        if d >= thr:
            return c
    return None


def _region(cfg):
    lat0, lon0 = cfg.GEOCODE
    h = cfg.RADAR_THUMB_HALF_DEG
    return lat0 - h, lat0 + h, lon0 - h, lon0 + h


def _cache_key(cfg, tile_url):
    s = (f"{cfg.GEOCODE}|{cfg.RADAR_THUMB_HALF_DEG}|{cfg.RADAR_THUMB_PX}|"
         f"{cfg.RADAR_TILE_ZOOM}|{tile_url}")
    return hashlib.md5(s.encode()).hexdigest()[:10]


# overlay colours per basemap theme (dark labels/lines are invisible on a light basemap)
_THEME = {
    "dark":  {"ink": (255, 255, 255), "stroke": (0, 0, 0), "ring": (90, 200, 255),
              "label": (120, 210, 255), "text": (200, 215, 235)},
    "light": {"ink": (25, 30, 40), "stroke": (255, 255, 255), "ring": (10, 90, 200),
              "label": (10, 90, 200), "text": (40, 55, 75)},
}


def _font(sz):
    for p in ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            pass
    return ImageFont.load_default()


class Thumbnailer:
    """Builds and caches a tile basemap once, then composites radar each cycle.

    Parametrized by tile URL / output path / theme so the same machinery makes both the
    night (dark) and day (light) maps.
    """

    def __init__(self, cfg, tile_url, thumb_path, theme):
        self.cfg = cfg
        self.tile_url = tile_url
        self.thumb_path = thumb_path
        self.colors = _THEME.get(theme, _THEME["dark"])
        self._basemap = None        # PIL RGB image at (_ox, _oy)
        self._tilebox = None        # (gx0, gy0, gx1, gy1, z) for the mercator mapping
        self._remap = None          # per-thumb-pixel -> radar palette index source (col,row)
        self._ox = cfg.RADAR_THUMB_PX
        self._oy = cfg.RADAR_THUMB_PX   # replaced with the true aspect in _build_basemap

    # basemap (static): fetch tiles once, cache the composited image to disk
    def _build_basemap(self):
        cfg = self.cfg
        z = cfg.RADAR_TILE_ZOOM
        latmin, latmax, lonmin, lonmax = _region(cfg)
        x0f, y0f = _deg2num(latmax, lonmin, z)
        x1f, y1f = _deg2num(latmin, lonmax, z)
        gx0, gy0, gx1, gy1 = int(x0f * 256), int(y0f * 256), int(x1f * 256), int(y1f * 256)
        self._tilebox = (gx0, gy0, gx1, gy1, z)
        # Non-square output matching the Mercator canvas aspect, so px/km is equal on both
        # axes: the 50 km ring draws as a true circle and one scale bar is valid in every
        # direction (a square would stretch E-W by 1/cos(lat) ~ 1.2x here).
        cw, ch = gx1 - gx0, gy1 - gy0
        self._ox = cfg.RADAR_THUMB_PX
        self._oy = max(1, round(self._ox * ch / cw))

        cache = os.path.join(cfg.RADAR_CACHE_DIR, f"basemap_{_cache_key(cfg, self.tile_url)}.png")
        if os.path.exists(cache):
            try:
                self._basemap = Image.open(cache).convert("RGB")
                log.info("radar basemap loaded from cache %s (tiles not re-fetched)", cache)
                return
            except Exception:
                pass
        canvas = Image.new("RGB", (cw, ch), (16, 20, 30))
        n = 0
        expected = (int(x1f) - int(x0f) + 1) * (int(y1f) - int(y0f) + 1)
        for tx in range(int(x0f), int(x1f) + 1):
            for ty in range(int(y0f), int(y1f) + 1):
                try:
                    t = Image.open(io.BytesIO(_get(
                        self.tile_url.format(z=z, x=tx, y=ty)))).convert("RGB")
                    canvas.paste(t, (tx * 256 - gx0, ty * 256 - gy0))
                    n += 1
                except Exception:
                    log.warning("radar tile fetch failed z%s x%s y%s", z, tx, ty)
        self._basemap = canvas.resize((self._ox, self._oy), Image.LANCZOS)
        # Only persist a COMPLETE basemap — never cache a partial/blank one forever.
        if n == expected and n > 0:
            try:
                os.makedirs(cfg.RADAR_CACHE_DIR, exist_ok=True)
                self._basemap.save(cache)
                log.info("radar basemap built from %d tiles and cached to %s", n, cache)
            except Exception:
                log.exception("could not cache radar basemap")
        else:
            log.warning("radar basemap incomplete (%d/%d tiles) — not cached; will retry",
                        n, expected)

    # reproject lookup (static for a fixed region): thumb pixel -> radar (col,row)
    def _build_remap(self):
        ox, oy = self._ox, self._oy
        gx0, gy0, gx1, gy1, z = self._tilebox
        remap = []
        for j in range(oy):
            gy = gy0 + (j + 0.5) / oy * (gy1 - gy0)
            row = []
            for i in range(ox):
                gx = gx0 + (i + 0.5) / ox * (gx1 - gx0)
                lat, lon = _num2deg(gx / 256, gy / 256, z)
                row.append(latlon_to_px(lat, lon))
            remap.append(row)
        self._remap = remap

    def _mv(self, lat, lon):
        gx0, gy0, gx1, gy1, z = self._tilebox
        xf, yf = _deg2num(lat, lon, z)
        return ((xf * 256 - gx0) / (gx1 - gx0) * self._ox,
                (yf * 256 - gy0) / (gy1 - gy0) * self._oy)

    def render(self, img, frame_txt):
        """Composite radar over the cached basemap, draw ring + scale bars, save the PNG."""
        cfg = self.cfg
        if self._basemap is None:
            self._build_basemap()
        if self._remap is None:
            self._build_remap()
        ox, oy = self._ox, self._oy
        base = self._basemap.copy().convert("RGBA")
        ov = Image.new("RGBA", (ox, oy), (0, 0, 0, 0))
        op = ov.load()
        rp = img.load()
        for j in range(oy):
            rowmap = self._remap[j]
            for i in range(ox):
                c, r = rowmap[i]
                if 0 <= c < GRID_W and 0 <= r < GRID_H:
                    v = rp[c, r]
                    v = v if isinstance(v, int) else v[0]
                    col = _dbz_color(_dbz(v))
                    if col:
                        op[i, j] = (col[0], col[1], col[2], 205)
        im = Image.alpha_composite(base, ov).convert("RGB")
        d = ImageDraw.Draw(im)
        lat0, lon0 = cfg.GEOCODE
        ink, stroke = self.colors["ink"], self.colors["stroke"]

        # 50 km ring
        ring = [self._mv(*dest_point(lat0, lon0, cfg.RADAR_TRIGGER_KM, b))
                for b in range(0, 361, 6)]
        d.line(ring, fill=self.colors["ring"], width=2)
        # observatory crosshair
        cx, cy = self._mv(lat0, lon0)
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=ink, width=2)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            d.line([cx + dx * 6, cy + dy * 6, cx + dx * 11, cy + dy * 11], fill=ink, width=2)
        d.text((cx + 8, cy + 6), "%g km" % cfg.RADAR_TRIGGER_KM, fill=self.colors["label"],
               font=_font(13), stroke_width=2, stroke_fill=stroke)

        # scale bars (10 km and 10 mi), bottom-left
        f = _font(12)

        def px_for(km):
            x2, _ = self._mv(*dest_point(lat0, lon0, km, 90))
            x1, _ = self._mv(lat0, lon0)
            return abs(x2 - x1)
        bx, by = 14, oy - 34
        for label, km in (("10 km", 10.0), ("10 mi", 16.0934)):
            L = px_for(km)
            d.line([bx, by, bx + L, by], fill=ink, width=3)
            d.line([bx, by - 3, bx, by + 3], fill=ink, width=2)
            d.line([bx + L, by - 3, bx + L, by + 3], fill=ink, width=2)
            d.text((bx + L + 5, by - 7), label, fill=ink, font=f,
                   stroke_width=2, stroke_fill=stroke)
            by += 15
        # frame time, top-left
        d.text((8, 6), frame_txt, fill=self.colors["text"], font=_font(12),
               stroke_width=2, stroke_fill=stroke)

        tmp = self.thumb_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.thumb_path) or ".", exist_ok=True)
            im.save(tmp, format="PNG")     # .tmp ext -> must state the format
            os.replace(tmp, self.thumb_path)
            return True
        except Exception:
            log.exception("could not write radar thumbnail %s", self.thumb_path)
            return False


# ---- poller ----------------------------------------------------------------
class RadarPoller:
    def __init__(self, cfg, eventlog):
        self.cfg = cfg
        self.log = eventlog
        self._lock = threading.Lock()
        # night (dark) map, plus an optional day (light) map for the daytime page style
        self._thumbs = []
        if deps_available():
            self._thumbs.append(Thumbnailer(cfg, cfg.RADAR_TILE_URL,
                                            cfg.RADAR_THUMB_PATH, "dark"))
            if cfg.RADAR_DAY_ENABLED:
                self._thumbs.append(Thumbnailer(cfg, cfg.RADAR_TILE_URL_DAY,
                                                cfg.RADAR_THUMB_PATH_DAY, "light"))
        self._last_ok_ts = None
        self._last_poll_ts = None
        self._in_ring = False
        self._nearest_km = None
        self._count = 0
        self._frame_utc = None
        self._thumb_ok = False
        self._coverage_warned = False
        # The post-rain freeze must survive a daemon restart (systemd Restart=always
        # would otherwise silently drop the veto mid-event) — persist last_rain_ts like
        # the WU/GLM latches.
        self._last_rain_ts = None
        try:
            with open(cfg.RADAR_LATCH_FILE, encoding="utf-8") as f:
                lr = json.load(f).get("last_rain_ts")
            if isinstance(lr, (int, float)) and math.isfinite(lr):
                self._last_rain_ts = lr
                if (time.time() - lr) < cfg.RADAR_LATCH_SEC:
                    log.info("restored radar post-rain freeze (%d min old)",
                             int((time.time() - lr) / 60))
        except Exception:
            pass

    def _save_rain_ts(self):
        try:
            tmp = self.cfg.RADAR_LATCH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_rain_ts": self._last_rain_ts}, f)
            os.replace(tmp, self.cfg.RADAR_LATCH_FILE)
        except Exception:
            log.exception("cannot persist radar latch")

    def poll_now(self, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            self._last_poll_ts = now
        if not deps_available():
            return {"ok": False, "error": "pillow not installed"}
        if not site_in_coverage(*self.cfg.GEOCODE):
            if not self._coverage_warned:
                self._coverage_warned = True
                log.error("site %s is OUTSIDE the MRMS CONUS grid — radar layer disabled "
                          "(it would otherwise report a false 'clear')", self.cfg.GEOCODE)
                self.log.record("CONFIG", reason="site outside MRMS coverage",
                                result="radar layer disabled")
            return {"ok": False, "error": "site outside MRMS coverage"}
        ts, url = latest_frame()
        if not ts:
            log.warning("no recent MRMS frame")
            return {"ok": False, "error": "no frame"}
        try:
            img = Image.open(io.BytesIO(_get(url)))
            img.load()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("radar fetch failed: %s", e)
            return {"ok": False, "error": str(e)}
        in_ring, nearest, count = check_rain(self.cfg, img)
        frame_txt = "MRMS " + ts.strftime("%Y-%m-%d %H:%MZ")
        # render every configured map (dark + optional light); primary = the first (dark)
        results = [t.render(img, frame_txt) for t in self._thumbs]
        thumb_ok = bool(results and results[0])
        with self._lock:
            self._last_ok_ts = now
            self._frame_utc = ts.isoformat()
            self._in_ring = in_ring
            self._nearest_km = nearest
            self._count = count
            self._thumb_ok = thumb_ok
            if in_ring:
                self._last_rain_ts = now
                self._save_rain_ts()
        if in_ring:
            self.log.record("RADAR-RAIN", reason=f"echo within {self.cfg.RADAR_TRIGGER_KM:g} km",
                            source="mrms", result="unsafe",
                            nearest=f"{nearest}km", pixels=count)
        return {"ok": True, "in_ring": in_ring, "nearest_km": nearest, "count": count}

    def maybe_poll(self, sun_alt, now=None):
        # Radar polls DAY AND NIGHT (unlike WU/GLM): the data is free, daytime rain
        # matters too, and the page's radar map stays live around the clock. The
        # sun_alt argument is kept for interface symmetry but not used.
        if now is None:
            now = time.time()
        if not deps_available():
            return None
        with self._lock:
            due = (self._last_poll_ts is None
                   or (now - self._last_poll_ts) >= self.cfg.RADAR_POLL_INTERVAL)
        if due:
            return self.poll_now(now)
        return None

    def component(self, sun_alt, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            fresh = (self._last_ok_ts is not None
                     and (now - self._last_ok_ts) <= self.cfg.RADAR_STALE_AFTER_SEC)
            live_unsafe = fresh and self._in_ring
            # FREEZE after the last in-ring detection: hold the veto for RADAR_LATCH_SEC
            # even once frames come back clear. Rain leaving the 50 km ring is not by
            # itself a reason to reopen — the cell can turn back, and the roof should not
            # chase the radar edge. The window is bounded, so a genuinely departed cell
            # (or a blind feed) self-clears instead of sticking. It also covers an echo
            # inside the ring but over no WU station, where WU never latches at all.
            latched = (self._last_rain_ts is not None
                       and (now - self._last_rain_ts) < self.cfg.RADAR_LATCH_SEC)
            # Pillow absent => radar never really ran; never veto from an artificial state.
            unsafe = deps_available() and (live_unsafe or latched)
            available = deps_available() and fresh
            polling_active = deps_available()      # radar polls day and night
            return {
                # Unsafe on a fresh in-ring frame, OR while the post-rain freeze holds. A stale
                # frame with no recent detection is "unknown" (available False), no veto.
                # Observation fields are exported ONLY while fresh — a stale in_ring/nearest
                # must never be rendered as a current observation.
                "safe": not unsafe,
                "latched": bool(latched and not live_unsafe),
                # countdown of the post-rain freeze, so the page can say how long is left
                # instead of just asserting "recent rain"
                "seconds_remaining": (
                    max(0, round(self._last_rain_ts + self.cfg.RADAR_LATCH_SEC - now))
                    if latched else 0),
                "freeze_sec": self.cfg.RADAR_LATCH_SEC,
                "enabled": deps_available(),
                "available": available,
                "in_ring": bool(fresh and self._in_ring),
                "nearest_km": self._nearest_km if fresh else None,
                "pixels": self._count if fresh else 0,
                "frame_utc": self._frame_utc,
                "age_s": round(now - self._last_ok_ts) if self._last_ok_ts else None,
                "trigger_km": self.cfg.RADAR_TRIGGER_KM,
                "dbz": self.cfg.RADAR_DBZ,
                "polling_active": polling_active,
                "thumb_available": self._thumb_ok,
                "thumb_path": self.cfg.RADAR_THUMB_PATH,
                "thumb_path_day": (self.cfg.RADAR_THUMB_PATH_DAY
                                   if self.cfg.RADAR_DAY_ENABLED else None),
                "attribution": self.cfg.RADAR_ATTRIBUTION,
                "source": "NOAA/NSSL MRMS composite reflectivity via IEM",
            }


def unavailable_component(cfg):
    return {
        "safe": True, "enabled": False, "available": False, "in_ring": False,
        "latched": False, "seconds_remaining": 0, "freeze_sec": cfg.RADAR_LATCH_SEC,
        "nearest_km": None, "pixels": 0, "frame_utc": None, "age_s": None,
        "trigger_km": cfg.RADAR_TRIGGER_KM, "dbz": cfg.RADAR_DBZ, "polling_active": False,
        "thumb_available": False, "thumb_path": cfg.RADAR_THUMB_PATH, "thumb_path_day": None,
        "attribution": cfg.RADAR_ATTRIBUTION,
        "source": "NOAA/NSSL MRMS composite reflectivity via IEM (disabled)",
    }
