"""safety/hazard_feeds.py: INFORMATION ONLY, never part of IsSafe. Fixtures are REAL
payloads (fetched 2026-09-24), trimmed; polygons decimated with the site test re-checked.
The network is blocked."""
import gzip
import inspect
import io
import json
import re
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from safety import hazard_feeds as hf

# HMS smoke KML 2026-09-22 (decimated): the 12-15Z polygon over the site, the latest (20-23:30Z) one touching the map, one over Cuba (from 2026-09-24, re-dated
# 12-15Z of the same day: one HMS file holds one day's analyses)
SMOKE_KML = (
    '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2" xmlns:'
    'gx="http://www.google.com/kml/ext/2.2"><Document><name>HMS Smoke Mapping-20260922</name>'
    '<Folder><name>Smoke (Light)</name><Placemark><description><![CDATA[Start Time: 2026265 1'
    '300UTC<br>End Time: 2026265 1500UTC<br>Density: Light<br>Satellite: GOES-EAST]]></descri'
    'ption><styleUrl>#Smoke_Light_style</styleUrl><Polygon><tessellate>1</tessellate><outerBo'
    'undaryIs><LinearRing><coordinates>-100.067710,34.763768,0 -96.022029,36.112329,0 -91.464'
    '820,36.391342,0 -83.760968,36.081327,0 -80.720246,35.487134,0 -77.361759,35.425131,0 -73'
    '.533084,34.743100,0 -72.525538,31.596455,0 -75.641180,29.565862,0 -79.299348,28.062293,0'
    ' -82.306486,26.481221,0 -85.143116,26.326214,0 -87.248210,23.427488,0 -88.934790,20.8689'
    '14,0 -90.320286,18.802151,0 -89.485927,17.278871,0 -89.233322,16.299072,0 -90.052373,15.'
    '587187,0 -91.284776,15.013087,0 -91.699917,14.061705,0 -93.632340,12.384182,0 -95.947114'
    ',12.301512,0 -97.380069,13.300447,0 -97.366291,15.298318,0 -99.109260,16.507373,0 -103.9'
    '38590,19.405433,0 -107.693210,20.163245,0 -108.473980,18.241446,0 -107.124850,16.150283,'
    '0 -107.012900,11.146999,0 -112.980670,10.294460,0 -117.837560,13.368767,0 -119.878490,16'
    '.701419,0 -114.840760,21.067451,0 -112.575930,23.263385,0 -113.115590,25.984620,0 -112.0'
    '93690,27.454317,0 -110.015450,27.190230,0 -106.054150,24.009715,0 -104.274120,22.593767,'
    '0 -103.853110,23.588875,0 -104.949640,26.170412,0 -104.710760,26.972072,0 -103.181410,26'
    '.453635,0 -101.834520,27.333755,0 -102.954010,31.174485,0 -102.316760,33.740712,0 -100.0'
    '67710,34.763768,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark><Pla'
    'cemark><description><![CDATA[Start Time: 2026265 2000UTC<br>End Time: 2026265 2330UTC<br'
    '>Density: Light<br>Satellite: GOES-EAST]]></description><styleUrl>#Smoke_Light_style</st'
    'yleUrl><Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>-89.'
    '377205,25.231980,0 -93.426422,23.840390,0 -95.058667,22.040735,0 -92.547521,20.889793,0 '
    '-90.151479,21.643134,0 -88.906359,20.345711,0 -90.036375,18.525130,0 -91.176864,16.72547'
    '3,0 -90.224711,14.318961,0 -89.534146,12.205413,0 -91.040834,10.029086,0 -94.797100,9.54'
    '7780,0 -97.622139,10.824279,0 -98.281305,14.570075,0 -98.720756,19.948113,0 -100.876160,'
    '24.091504,0 -101.430700,27.648961,0 -99.102675,32.284120,0 -96.063127,35.056840,0 -90.30'
    '2023,35.864509,0 -85.453537,37.097146,0 -81.064265,37.976055,0 -78.202604,36.856497,0 -7'
    '6.538969,34.345350,0 -76.842400,31.237805,0 -79.562808,28.684806,0 -82.968552,26.822374,'
    '0 -87.912369,25.504021,0 -89.377205,25.231980,0</coordinates></LinearRing></outerBoundar'
    'yIs></Polygon></Placemark><Placemark><description><![CDATA[Start Time: 2026265 1200UTC<b'
    'r>End Time: 2026265 1500UTC<br>Density: Light<br>Satellite: GOES-WEST]]></description><s'
    'tyleUrl>#Smoke_Light_style</styleUrl><Polygon><tessellate>1</tessellate><outerBoundaryIs'
    '><LinearRing><coordinates>-76.409302,20.591941,0 -76.423999,20.625011,0 -76.425836,20.64'
    '5220,0 -76.414814,20.687474,0 -76.405628,20.689311,0 -76.394605,20.683799,0 -76.383583,2'
    '0.667265,0 -76.376234,20.634197,0 -76.376234,20.613989,0 -76.379908,20.604803,0 -76.3909'
    '31,20.591943,0 -76.398279,20.584595,0 -76.409302,20.591941,0</coordinates></LinearRing><'
    '/outerBoundaryIs></Polygon></Placemark></Folder></Document></kml>'
)

# WFIGS incident records (real): Yellow Lake + Leon near the site (2026 YTD layer), Jose NE + Fowl (current NM records, 2026-09-24)
FIRES_JSON = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","co'
    'ordinates":[-101.8267,33.7954]},"properties":{"IncidentName":"Yellow Lake","IncidentSize'
    '":159,"PercentContained":100,"FireDiscoveryDateTime":1772394868000,"ModifiedOnDateTime_d'
    't":1773869064383,"FireOutDateTime":null,"ContainmentDateTime":1772927245000,"ControlDate'
    'Time":1772926993000,"POOCounty":"Lubbock","POOState":"US-TX","IncidentTypeCategory":"WF"'
    ',"UniqueFireIdentifier":"2026-TXTXS-262103"}},{"type":"Feature","geometry":{"type":"Poin'
    't","coordinates":[-102.0979,33.8165]},"properties":{"IncidentName":"Leon","IncidentSize"'
    ':46.6,"PercentContained":100,"FireDiscoveryDateTime":1788895047000,"ModifiedOnDateTime_d'
    't":1789091658743,"FireOutDateTime":null,"ContainmentDateTime":1788900858000,"ControlDate'
    'Time":1789090229000,"POOCounty":"Hockley","POOState":"US-TX","IncidentTypeCategory":"WF"'
    ',"UniqueFireIdentifier":"2026-TXTXS-268232"}},{"type":"Feature","geometry":{"type":"Poin'
    't","coordinates":[-103.0809,35.422]},"properties":{"IncidentName":"Jose NE","IncidentSiz'
    'e":32.4,"PercentContained":null,"FireDiscoveryDateTime":1779658800000,"ModifiedOnDateTim'
    'e_dt":1790180596903,"FireOutDateTime":null,"ContainmentDateTime":null,"ControlDateTime":'
    'null,"POOCounty":"Quay","POOState":"US-NM","IncidentTypeCategory":"WF","UniqueFireIdenti'
    'fier":"2026-NMN4S-000228"}},{"type":"Feature","geometry":{"type":"Point","coordinates":['
    '-103.7858,35.5693]},"properties":{"IncidentName":"Fowl","IncidentSize":822,"PercentConta'
    'ined":null,"FireDiscoveryDateTime":1788388620000,"ModifiedOnDateTime_dt":1790181833123,"'
    'FireOutDateTime":null,"ContainmentDateTime":null,"ControlDateTime":null,"POOCounty":"Har'
    'ding","POOState":"US-NM","IncidentTypeCategory":"WF","UniqueFireIdentifier":"2026-NMN4S-'
    '000562"}}]}'
)

# WFIGS perimeters (real, generalised server-side): Oklahoma Flat (MultiPolygon with a sliver) and Leon
PERIMETERS_JSON = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"MultiPolyg'
    'on","coordinates":[[[[-102.409,33.7753],[-102.4139,33.7821],[-102.4138,33.7943],[-102.41'
    '1,33.7942],[-102.4098,33.7865],[-102.407,33.7862],[-102.409,33.7753]]],[[[-102.4084,33.7'
    '752],[-102.4084,33.7752],[-102.4083,33.7752],[-102.4084,33.7752]]]]},"properties":{"poly'
    '_IncidentName":"Oklahoma Flat","poly_GISAcres":1383.2715559793514,"attr_IncidentSize":19'
    '8,"attr_PercentContained":100,"attr_IncidentTypeCategory":"WF","attr_UniqueFireIdentifie'
    'r":"2026-TXTXS-262805","attr_FireDiscoveryDateTime":1774033903000,"attr_ModifiedOnDateTi'
    'me_dt":1774964418910}},{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[-'
    '102.0927,33.8142],[-102.0944,33.8175],[-102.0886,33.8176],[-102.0896,33.8138],[-102.0927'
    ',33.8142]]]},"properties":{"poly_IncidentName":"Leon","poly_GISAcres":46.69437428119661,'
    '"attr_IncidentSize":46.6,"attr_PercentContained":100,"attr_IncidentTypeCategory":"WF","a'
    'ttr_UniqueFireIdentifier":"2026-TXTXS-268232","attr_FireDiscoveryDateTime":1788895047000'
    ',"attr_ModifiedOnDateTime_dt":1789091658743}}]}'
)

# SPC Day 1 categorical 2025-06-05 2000Z (archive; site in ENH), trimmed to the map
SPC_20250605_JSON = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"MultiPolyg'
    'on","coordinates":[[[[-90.01246872338672,48.265988297024805],[-99.18,41.98],[-82.0745922'
    '8550829,43.042628875270374],[-82.459,41.878],[-75.977,44.604],[-101.68,40.86],[-106.02,3'
    '3.45],[-120.65,39.33],[-116.38,44.37],[-105.669,49.171],[-93.805,48.857],[-90.0124687233'
    '8672,48.265988297024805]]],[[[-69.86911349721494,42.42871276987525],[-76.265,34.375],[-7'
    '9.725,32.427],[-80.09,26.58],[-83.014,28.465],[-85.675,29.436],[-96.85,29.14],[-99.86,31'
    '.84],[-67.42416025739237,45.74430121035698],[-68.352,43.817],[-69.86911349721494,42.4287'
    '1276987525]],[[-87.49,33.58],[-87.19,33.31],[-85.18,34.12],[-84.98,34.9],[-85.48,35.35],'
    '[-86.5,35.09],[-87.25,34.26],[-87.49,33.58]]]]},"properties":{"DN":2,"VALID":"2025060520'
    '00","EXPIRE":"202506061200","ISSUE":"202506051954","LABEL":"TSTM","LABEL2":"General Thun'
    'derstorms Risk","stroke":"#55BB55","fill":"#C1E9C1"}},{"type":"Feature","geometry":{"typ'
    'e":"Polygon","coordinates":[[[-67.42416025739237,45.74430121035698],[-87.98,34.99],[-102'
    '.21607258587169,29.64787751134154],[-102.52,29.71],[-94.76,36.48],[-104.02,40.58],[-104.'
    '86,33.92],[-104.35,41.96],[-88.57,38.49],[-70.611,45.956],[-68.217,47.605],[-67.42416025'
    '739237,45.74430121035698]],[[-76.52,41.07],[-74.21,41.93],[-74.05,42.47],[-74.4,42.9],[-'
    '75.2,43.16],[-76.26,43.07],[-80.1,41.86],[-80.49,41.32],[-80.22,40.74],[-79.68,40.36],[-'
    '78.78,40.29],[-76.52,41.07]]]},"properties":{"DN":3,"VALID":"202506052000","EXPIRE":"202'
    '506061200","ISSUE":"202506051954","LABEL":"MRGL","LABEL2":"Marginal Risk","stroke":"#005'
    '500","fill":"#66A366"}},{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[['
    '-105.18536188608611,30.52568410731795],[-104.9,37.65],[-104.68,40.33],[-103.75,40.52],[-'
    '101.9,38.85],[-94.76,36.48],[-97.43,34.26],[-101.81,30.74],[-103.419,28.868],[-104.879,2'
    '9.863],[-105.18536188608611,30.52568410731795]],[[-101.15,31.76],[-100.29,32.21],[-99.42'
    ',33.04],[-99.25,33.96],[-99.78,34.31],[-100.97,34.56],[-101.71,34.62],[-102.27,34.61],[-'
    '102.89,34.47],[-103.5,34.12],[-103.96,33.42],[-104.08,32.65],[-103.91,30.73],[-103.38,30'
    '.44],[-102.75,30.44],[-102.12,31.0],[-101.15,31.76]],[[-102.64,36.87],[-101.86,36.42],[-'
    '99.12,35.61],[-97.98,35.58],[-97.18,36.05],[-97.22,36.75],[-98.53,37.41],[-100.13,37.97]'
    ',[-101.95,38.24],[-102.45,37.93],[-102.7,37.63],[-102.66,37.29],[-102.64,36.87]]]},"prop'
    'erties":{"DN":4,"VALID":"202506052000","EXPIRE":"202506061200","ISSUE":"202506051954","L'
    'ABEL":"SLGT","LABEL2":"Slight Risk","stroke":"#DDAA00","fill":"#FFE066"}},{"type":"Featu'
    're","geometry":{"type":"Polygon","coordinates":[[[-101.15,31.76],[-102.12,31.0],[-102.75'
    ',30.44],[-103.38,30.44],[-103.91,30.73],[-104.08,32.65],[-103.96,33.42],[-103.5,34.12],['
    '-102.89,34.47],[-102.27,34.61],[-101.71,34.62],[-100.97,34.56],[-99.78,34.31],[-99.25,33'
    '.96],[-99.42,33.04],[-100.29,32.21],[-101.15,31.76]]]},"properties":{"DN":5,"VALID":"202'
    '506052000","EXPIRE":"202506061200","ISSUE":"202506051954","LABEL":"ENH","LABEL2":"Enhanc'
    'ed Risk","stroke":"#FF6600","fill":"#FFA366"}}]}'
)

# IEM spc_mcd.geojson (valid=2025-06-06T02:00Z, hours=6): MD 1129 (touches the map), 1133 (covers the site), 1134 (far away)
MCD_JSON = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"year":2025,"num'
    '":1129,"issue":"2025-06-05T20:46:00Z","expire":"2025-06-05T22:45:00Z","watch_confidence"'
    ':null,"concerning":"TORNADO WATCH 367"},"geometry":{"type":"Polygon","coordinates":[[[-1'
    '03.02,33.48],[-103.37,33.56],[-103.64,33.81],[-103.64,34.25],[-103.67,34.61],[-103.48,34'
    '.79],[-103.07,34.71],[-102.85,34.43],[-102.75,33.98],[-102.55,33.71],[-102.59,33.51],[-1'
    '03.02,33.48]]]}},{"type":"Feature","properties":{"year":2025,"num":1133,"issue":"2025-06'
    '-05T23:05:00Z","expire":"2025-06-06T01:00:00Z","watch_confidence":null,"concerning":"TOR'
    'NADO WATCH 367"},"geometry":{"type":"Polygon","coordinates":[[[-102.65,34.0],[-101.84,34'
    '.11],[-100.91,33.86],[-100.51,33.35],[-101.4,33.27],[-102.49,33.52],[-102.65,34.0]]]}},{'
    '"type":"Feature","properties":{"year":2025,"num":1134,"issue":"2025-06-05T23:44:00Z","ex'
    'pire":"2025-06-06T01:45:00Z","watch_confidence":null,"concerning":"SEVERE THUNDERSTORM W'
    'ATCH 368"},"geometry":{"type":"Polygon","coordinates":[[[-102.25,30.87],[-101.87,30.89],'
    '[-101.64,30.18],[-102.8,29.85],[-103.77,29.99],[-103.6,30.5],[-102.25,30.87]]]}}]}'
)

# IEM lsrs_by_point (2025-06-05 20Z .. 06-06 06Z, 90 mi): re-issues included
LSR_JSON = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"valid":"2025-06'
    '-05T21:34:00Z","magnitude":null,"city":"5 NW Rogers","county":"Roosevelt","state":"NM","'
    'remark":"Corrects previous tornado report from 5 NW Rogers. NWS Employee reported tornad'
    'o on the ground between 3:34 and 3:36 PM MDT 5 NW of Rogers.","wfo":"ABQ","typetext":"TO'
    'RNADO","product_id":"202506052207-KABQ-NWUS55-LSRABQ","unit":null,"qualifier":null},"geo'
    'metry":{"type":"Point","coordinates":[-103.29,34.03]}},{"type":"Feature","properties":{"'
    'valid":"2025-06-05T21:40:00Z","magnitude":null,"city":"5 NW Rogers","county":"Roosevelt"'
    ',"state":"NM","remark":"NWS Employee reported tornado on the ground between 3:40","wfo":'
    '"ABQ","typetext":"TORNADO","product_id":"202506052153-KABQ-NWUS55-LSRABQ","unit":null,"q'
    'ualifier":null},"geometry":{"type":"Point","coordinates":[-103.29,34.03]}},{"type":"Feat'
    'ure","properties":{"valid":"2025-06-05T23:32:00Z","magnitude":2.5,"city":"2 ESE Tatum","'
    'county":"Lea","state":"NM","remark":"Egg to Tennis Ball size hail reported by the public'
    ' via ","wfo":"MAF","typetext":"HAIL","product_id":"202506060006-KMAF-NWUS54-LSRMAF","uni'
    't":"Inch","qualifier":"E"},"geometry":{"type":"Point","coordinates":[-103.28,33.24]}},{"'
    'type":"Feature","properties":{"valid":"2025-06-05T23:32:00Z","magnitude":3.0,"city":"2 E'
    'SE Tatum","county":"Lea","state":"NM","remark":"Corrects previous hail report from 2 ESE'
    ' Tatum. Hail measured up to 3.5 inches in diameter.","wfo":"MAF","typetext":"HAIL","prod'
    'uct_id":"202506060013-KMAF-NWUS54-LSRMAF","unit":"Inch","qualifier":"E"},"geometry":{"ty'
    'pe":"Point","coordinates":[-103.28,33.24]}},{"type":"Feature","properties":{"valid":"202'
    '5-06-05T23:35:00Z","magnitude":null,"city":"5 N Levelland","county":"Hockley","state":"T'
    'X","remark":null,"wfo":"LUB","typetext":"TORNADO","product_id":"202506052336-KLUB-NWUS54'
    '-LSRLUB","unit":null,"qualifier":null},"geometry":{"type":"Point","coordinates":[-102.37'
    ',33.65]}},{"type":"Feature","properties":{"valid":"2025-06-05T23:35:00Z","magnitude":nul'
    'l,"city":"7 N Levelland","county":"Hockley","state":"TX","remark":"Corrects location of '
    'previous tornado report from 5 N Levelland.","wfo":"LUB","typetext":"TORNADO","product_i'
    'd":"202506052341-KLUB-NWUS54-LSRLUB","unit":null,"qualifier":null},"geometry":{"type":"P'
    'oint","coordinates":[-102.37,33.71]}},{"type":"Feature","properties":{"valid":"2025-06-0'
    '6T00:05:00Z","magnitude":5.0,"city":"7 S Anton","county":"Hockley","state":"TX","remark"'
    ':"Social media photo shows giant hailstone recovered about","wfo":"LUB","typetext":"HAIL'
    '","product_id":"202506060438-KLUB-NWUS54-LSRLUB","unit":"Inch","qualifier":"E"},"geometr'
    'y":{"type":"Point","coordinates":[-102.17,33.71]}},{"type":"Feature","properties":{"vali'
    'd":"2025-06-06T00:05:00Z","magnitude":5.0,"city":"7 S Anton","county":"Hockley","state":'
    '"TX","remark":"Social media photo shows giant hailstone recovered about","wfo":"LUB","ty'
    'petext":"HAIL","product_id":"202506060913-KLUB-NWUS54-LSRLUB","unit":"Inch","qualifier":'
    '"E"},"geometry":{"type":"Point","coordinates":[-102.17,33.71]}},{"type":"Feature","prope'
    'rties":{"valid":"2025-06-06T00:07:00Z","magnitude":null,"city":"Reese Center","county":"'
    'Lubbock","state":"TX","remark":"Brief funnel cloud observed from Reese Center.","wfo":"L'
    'UB","typetext":"FUNNEL CLOUD","product_id":"202506060007-KLUB-NWUS54-LSRLUB","unit":null'
    ',"qualifier":null},"geometry":{"type":"Point","coordinates":[-102.02,33.59]}},{"type":"F'
    'eature","properties":{"valid":"2025-06-06T00:49:00Z","magnitude":null,"city":"1 E Shallo'
    'water","county":"Lubbock","state":"TX","remark":"Report from mPING: Street/road flooding'
    '; Street/road clo","wfo":"LUB","typetext":"FLASH FLOOD","product_id":"202506060117-KLUB-'
    'NWUS54-LSRLUB","unit":null,"qualifier":null},"geometry":{"type":"Point","coordinates":[-'
    '101.98,33.69]}},{"type":"Feature","properties":{"valid":"2025-06-06T00:55:00Z","magnitud'
    'e":null,"city":"1 SE Reese Center","county":"Lubbock","state":"TX","remark":"Roofs remov'
    'ed from buildings, sheds rolled, at 179 and 1","wfo":"LUB","typetext":"TSTM WND DMG","pr'
    'oduct_id":"202506060913-KLUB-NWUS54-LSRLUB","unit":null,"qualifier":null},"geometry":{"t'
    'ype":"Point","coordinates":[-102.01,33.58]}},{"type":"Feature","properties":{"valid":"20'
    '25-06-06T00:40:00Z","magnitude":95.0,"city":"Smyer","county":"Hockley","state":"TX","rem'
    'ark":"Gusts ranging from 63 to 95 mph observed from 738 PM to ","wfo":"LUB","typetext":"'
    'TSTM WND GST","product_id":"202506060049-KLUB-NWUS54-LSRLUB","unit":"MPH","qualifier":"M'
    '"},"geometry":{"type":"Point","coordinates":[-102.17,33.59]}}]}'
)


def _ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


T0 = _ts(2025, 6, 6, 0, 30)          # MD 1133 in effect, SPC 20Z Day 1 valid
SITE = (33.748, -101.958)
BOX = (SITE[1] - 1, SITE[0] - 1, SITE[1] + 1, SITE[0] + 1)


def _cfg(**kw):
    c = dict(GEOCODE=SITE, RADAR_THUMB_HALF_DEG=1.0, HAZARD_FEEDS_ENABLED=True,
             HAZARD_FEEDS_POLL_SEC=600, HAZARD_STALE_AFTER_SEC=600, HAZARD_LSR_HOURS=24,
             LOCAL_TZ="America/Chicago", NWS_USER_AGENT="ttu-test")
    c.update(kw)
    return SimpleNamespace(**c)


def _burning(now, names=("Yellow Lake", "Leon")):
    """The real WFIGS records, modelled while they burned: containment not yet reported,
    discovered 3 h and last edited 1 h before `now`."""
    d = json.loads(FIRES_JSON)
    for f in d["features"]:
        p = f["properties"]
        if p["IncidentName"] in names:
            p.update(PercentContained=None, ContainmentDateTime=None, ControlDateTime=None,
                     FireDiscoveryDateTime=(now - 3 * 3600) * 1000,
                     ModifiedOnDateTime_dt=(now - 3600) * 1000)
    return json.dumps(d).encode()


def _routes(now=T0, **over):
    r = dict(over)                     # overrides first: the first matching fragment wins
    for k, v in (("hms_smoke", SMOKE_KML.encode()), ("WFIGS_Incident", _burning(now)),
                 ("WFIGS_Interagency", PERIMETERS_JSON.encode()),
                 ("day1otlk", SPC_20250605_JSON.encode()), ("spc_mcd", MCD_JSON.encode()),
                 ("lsrs_by_point", LSR_JSON.encode())):
        r.setdefault(k, v)
    return r


def _install(monkeypatch, routes, calls=None):
    """Fake hf._get: first URL fragment that matches wins. bytes -> 200 (+validators),
    int -> that HTTP status (304 = not modified), Exception -> raised."""
    def fake(url, ua, etag=None, last_modified=None, **kw):
        if calls is not None:
            calls.append((url, etag, last_modified))
        for frag, val in routes.items():
            if frag in url:
                if isinstance(val, Exception):
                    raise val
                if val == 304:
                    return 304, None, etag, last_modified
                if isinstance(val, int):
                    raise urllib.error.HTTPError(url, val, "status", {}, None)
                return 200, val, '"v1"', "Thu, 24 Sep 2026 17:00:00 GMT"
        raise urllib.error.URLError("no route for " + url)
    monkeypatch.setattr(hf, "_get", fake)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access in a unit test")
    monkeypatch.setattr(hf.urllib.request, "urlopen", boom)


def _poller(monkeypatch, now=T0, cfg=None, calls=None, **over):
    _install(monkeypatch, _routes(now, **over), calls)
    p = hf.HazardFeedsPoller(cfg or _cfg())
    p.poll_now(now)
    return p


# ---- the contract: information only ------------------------------------------------
def test_component_is_always_safe_and_info_only(monkeypatch):
    p = _poller(monkeypatch)
    c = p.component(T0)
    # an alarming picture: site in ENH, an MD over the site, a fire 13 km away...
    assert c["spc"]["category"] == "ENH" and c["spc"]["mds"][0]["at_site"]
    assert c["fires"] and c["lsr"]
    assert c["safe"] is True and c["info_only"] is True and c["enabled"] is True
    json.dumps(c)                                   # the state file must serialize it
    u = hf.unavailable_component(_cfg())
    assert u["safe"] is True and u["info_only"] is True and u["enabled"] is False
    assert set(c) == set(u) and set(c["spc"]) == set(u["spc"])
    assert set(u["feeds"]) == set(hf.FEED_NAMES)


def test_monitor_never_ands_hazard_info(env, write_inputs):
    """Even a (buggy) info component claiming safe=False must not change IsSafe."""
    from safety.monitor import SafetyMonitor
    if "hazard_feeds" not in inspect.signature(SafetyMonitor.__init__).parameters:
        pytest.skip("SafetyMonitor does not take hazard_feeds yet")

    class Hostile:
        def component(self, now=None):
            return {"safe": False, "info_only": False, "feeds": {}}

    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    plain = SafetyMonitor(env["cfg"], env["log"], env["poller"]).evaluate()
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], hazard_feeds=Hostile())
    st = m.evaluate()
    assert st["is_safe"] == plain["is_safe"] and st["reasons"] == plain["reasons"]
    assert st["components"]["hazard_info"]["safe"] is True


# ---- per-feed parsing and views ----------------------------------------------------
def test_smoke_falls_back_to_yesterday_and_reports_the_site(monkeypatch):
    calls = []
    now = _ts(2026, 9, 23, 3, 0)                     # local evening: no 09-23 file yet
    p = _poller(monkeypatch, now=now, calls=calls, hms_smoke20260923=404)
    urls = [u for u, _e, _l in calls if "hms_smoke" in u]
    assert urls[0].endswith("hms_smoke20260923.kml") and urls[1].endswith("20260922.kml")
    s = p.component(now)["smoke"]
    assert s["file_date"] == "2026-09-22"
    # over the site in the 13-15Z analysis, not in the latest (20-23:30Z) one
    assert s["site_in_smoke_today"] and not s["site_in_smoke"]
    assert s["density_today"] == "Light" and s["density"] is None
    assert s["windows_at_site"] == ["08:00–10:00 CDT (13:00–15:00 UTC)"]
    assert "earlier" in s["text"] and "15:00–18:30 CDT" in s["text"]
    ov = [o for o in p.overlays(now) if o["kind"] == "smoke"]
    assert len(ov) == 1 and ov[0]["style"]["fill_alpha"] > 0   # latest window only


def test_kml_with_doctype_is_refused_and_only_that_feed_fails(monkeypatch):
    evil = SMOKE_KML.replace("<kml", '<!DOCTYPE kml [<!ENTITY a "aaaa">]><kml', 1)
    with pytest.raises(ValueError, match="DOCTYPE"):
        hf.parse_hms_kml(evil.encode(), SITE, BOX)
    p = _poller(monkeypatch, hms_smoke=evil.encode())
    c = p.component(T0)
    assert not c["feeds"]["smoke"]["ok"] and "DOCTYPE" in c["feeds"]["smoke"]["error"]
    assert c["smoke"] is None
    assert all(c["feeds"][n]["ok"] for n in hf.FEED_NAMES if n != "smoke")


def test_fires_listed_only_while_plausibly_burning(monkeypatch):
    p = _poller(monkeypatch)
    fires = p.component(T0)["fires"]
    by = {f["name"]: f for f in fires}
    yl = by["Yellow Lake"]
    assert yl["kind"] == "incident" and yl["on_map"] and not yl["has_perimeter"]
    assert yl["text"].startswith("Yellow Lake fire · 159 ac, containment n/a")
    assert by["Leon"]["has_perimeter"]              # joined on UniqueFireIdentifier
    # Oklahoma Flat's perimeter is on the map and WFIGS still calls it "current", but it
    # is 100 % contained and was last edited in March: neither listed nor drawn
    assert "Oklahoma Flat" not in by
    assert not any(o["key"] == "perim:2026-TXTXS-262805" for o in p.overlays(T0))
    assert "Jose NE" not in by and "Fowl" not in by  # both beyond the 150 km radius
    # the real records as filed: 100 % contained -> not listed, perimeters included
    p2 = _poller(monkeypatch, WFIGS_Incident=FIRES_JSON.encode())
    assert p2.component(T0)["fires"] == []
    assert not any(o["kind"] == "fire_perimeter" for o in p2.overlays(T0))
    perim = dict(out_ts=None, contained_ts=None, controlled_ts=None, contained_pct=40.0,
                 discovered_ts=T0 - 30 * 86400, updated_ts=T0 - 3600)
    assert hf._perimeter_active(perim, T0)          # a perimeter alone can still be listed
    assert not hf._perimeter_active(dict(perim, contained_pct=100.0), T0)
    assert not hf._perimeter_active(dict(perim, updated_ts=T0 - 4 * 86400), T0)
    assert not hf._perimeter_active(dict(perim, contained_ts=T0 - 60), T0)
    assert not hf._perimeter_active(dict(perim, updated_ts=None, discovered_ts=None), T0)
    base = dict(out_ts=None, contained_ts=None, controlled_ts=None, contained_pct=None,
                acres=5000.0, discovered_ts=T0 - 60 * 86400, updated_ts=T0 - 3600)
    assert hf._fire_active(base, T0)                 # large and still being updated
    assert not hf._fire_active(dict(base, acres=50.0), T0)          # small and old
    assert not hf._fire_active(dict(base, updated_ts=T0 - 5 * 86400), T0)
    assert not hf._fire_active(dict(base, out_ts=T0 - 60), T0)
    kinds = {o["kind"] for o in p.overlays(T0)}
    assert {"fire", "fire_perimeter"} <= kinds


def test_spc_site_category_and_outlines(monkeypatch):
    p = _poller(monkeypatch)
    spc = p.component(T0)["spc"]
    assert spc["category"] == "ENH" and spc["label"] == "Enhanced Risk"
    assert spc["color"] == "#FF6600" and spc["on_map"] == ["MRGL", "SLGT", "ENH"]
    assert "valid Thu 15:00 CDT – Fri 07:00 CDT" in spc["text"]
    ov = [o for o in p.overlays(T0) if o["kind"] == "spc_outlook"]
    assert sorted(o["category"] for o in ov) == ["ENH", "MRGL", "SLGT"]   # no TSTM
    assert all(o["style"]["dash"] and o["style"]["fill_alpha"] == 0 for o in ov)
    rank = {o["category"]: o["rank"] for o in ov}
    assert rank["ENH"] < rank["SLGT"] < rank["MRGL"]    # higher risk drawn on top
    # an outlook past its EXPIRE is never presented as current
    p.poll_now(_ts(2025, 6, 6, 12, 5))
    late = p.component(_ts(2025, 6, 6, 12, 5))["spc"]
    assert late["category"] is None and "expired" in late["label"]
    assert hf._spc_time("2025-06-05T20:00:00+00:00", None) == hf._spc_time(None,
                                                                           "202506052000")


def _is_green(hexcol):
    r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
    return g > r + 40 and g > b + 40


def test_hazards_are_never_drawn_green(monkeypatch):
    cols = [c for _r, _n, s, f in hf.SPC_CATEGORIES.values() for c in (s, f)]
    cols += [c for _s, c in hf.LSR_KINDS.values()] + [c for c, _a in hf.SMOKE_FILL.values()]
    cols += [hf.MD_COLOR, hf.FIRE_COLOR]
    p = _poller(monkeypatch)
    for o in p.overlays(T0):
        cols += [v for v in (o["style"]["stroke"], o["style"]["fill"]) if v]
    assert cols and all(re.fullmatch(r"#[0-9A-F]{6}", c) for c in cols)
    assert not [c for c in cols if _is_green(c)]
    assert _is_green("#55BB55") and _is_green("#005500")   # SPC's own TSTM / MRGL


def test_mesoscale_discussions_in_effect_now(monkeypatch):
    p = _poller(monkeypatch)
    mds = p.component(T0)["spc"]["mds"]
    assert [m["num"] for m in mds] == [1133]          # 1129 expired, 1134 off the map
    assert mds[0]["at_site"] and "TORNADO WATCH 367" in mds[0]["text"]
    assert mds[0]["url"] == "https://www.spc.noaa.gov/products/md/2025/md1133.html"
    assert [o["label"] for o in p.overlays(T0) if o["kind"] == "spc_md"] == ["MD 1133"]
    assert p.component(_ts(2025, 6, 6, 1, 30))["spc"]["mds"] == []


def test_storm_reports_reissues_map_and_window(monkeypatch):
    calls = []
    p = _poller(monkeypatch, calls=calls)
    url = next(u for u, _e, _l in calls if "lsrs_by_point" in u)
    assert "radius_miles=91" in url and "begints=2025-06-05T00%3A30Z" in url
    lsr = p.component(T0)["lsr"]
    places = [r["place"] for r in lsr]
    # the 5.0 in hailstone re-sent in a summary is listed once; the 5 N Levelland
    # tornado is replaced by its correction; NM reports (off the map) are dropped
    assert places.count("7 S Anton") == 1 and "5 N Levelland" not in places
    assert "7 N Levelland" in places
    assert not any("Rogers" in x or "Tatum" in x for x in places)
    assert lsr[0]["time_ts"] >= lsr[-1]["time_ts"]  # newest first
    hail = next(r for r in lsr if r["place"] == "7 S Anton")
    assert hail["text"].startswith("HAIL 5.00 in (est.) · 7 S Anton (Hockley Co., TX)")
    gust = next(r for r in lsr if r["type"] == "TSTM WND GST")
    assert gust["mag_text"] == "95 mph"
    sym = {o["lsr_kind"]: o["style"]["symbol"] for o in p.overlays(T0) if o["kind"] == "lsr"}
    assert sym["tornado"] == "triangle" and sym["flood"] == "diamond"
    # 24 h later the reports have aged out of the window
    assert p.component(T0 + 25 * 3600)["lsr"] == []


def test_storm_report_text_carries_the_remark_without_reissue_bookkeeping(monkeypatch):
    lsr = _poller(monkeypatch).component(T0)["lsr"]
    # a correction whose remark is ONLY the bookkeeping: nothing left to show
    corrected = next(r for r in lsr if r["place"] == "7 N Levelland")
    assert corrected["remark"].lower().startswith("corrects")   # raw remark kept
    assert "“" not in corrected["text"] and "orrects" not in corrected["text"]
    spotted = [r for r in lsr if r["remark"] and not r["remark"].lower().startswith(
        ("corrects", "report duplicated"))]
    assert spotted and all("“%s”" % hf._lsr_remark(r["remark"]) in r["text"]
                           for r in spotted)
    assert hf._lsr_remark("Corrects previous hail report from 2 ESE Tatum. Hail measured "
                          "up to 3.5 inches in diameter.") \
        == "Hail measured up to 3.5 inches in diameter."
    assert hf._lsr_remark("Report duplicated with WFO ABQ. Trained spotter.") \
        == "Trained spotter."
    assert hf._lsr_remark(None) == "" and len(hf._lsr_remark("x" * 500)) == hf.LSR_REMARK_MAX


def _lsr_feature(i, t):
    return {"type": "Feature", "geometry": {"type": "Point",
                                            "coordinates": [-101.9 + 0.01 * i, 33.7]},
            "properties": {"valid": datetime.fromtimestamp(t, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"), "magnitude": 1.0, "city": "%d N Lubbock" % i,
                "county": "Lubbock", "state": "TX", "remark": "spotter %d" % i,
                "wfo": "LUB", "typetext": "HAIL", "product_id": "p%d" % i,
                "unit": "Inch", "qualifier": "M"}}


def test_totals_count_what_the_display_cap_hides(monkeypatch):
    feats = [_lsr_feature(i, T0 - 600 - 60 * i) for i in range(35)]
    body = json.dumps({"type": "FeatureCollection", "features": feats}).encode()
    p = _poller(monkeypatch, lsrs_by_point=body)
    c = p.component(T0)
    assert len(c["lsr"]) == hf.MAX_ITEMS
    assert c["totals"]["lsr"] == c["feeds"]["lsr"]["count"] == 35
    assert len([o for o in p.overlays(T0) if o["kind"] == "lsr"]) == 35
    assert c["totals"]["fires"] == len(c["fires"])


def _kml(*placemarks):
    """(density, 'YYYYDDD HHMM' start, end, ring as [(lon, lat)...]) -> an HMS KML."""
    pms = "".join(
        '<Placemark><description><![CDATA[Start Time: %s UTC<br>End Time: %s UTC<br>'
        'Density: %s<br>Satellite: GOES-EAST]]></description><Polygon><outerBoundaryIs>'
        '<LinearRing><coordinates>%s</coordinates></LinearRing></outerBoundaryIs></Polygon>'
        '</Placemark>' % (s, e, d, " ".join("%s,%s,0" % xy for xy in ring))
        for d, s, e, ring in placemarks)
    return ('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            '<Folder>%s</Folder></Document></kml>' % pms).encode()


def test_smoke_latest_analysis_is_the_files_not_the_latest_near_the_site(monkeypatch):
    # Heavy smoke over the site at 13-15Z, then a later 19-21Z analysis with smoke only
    # over Illinois: the site's smoke is EARLIER smoke, not the latest analysis
    site_ring = [(-102.5, 33.2), (-101.4, 33.2), (-101.4, 34.3), (-102.5, 34.3),
                 (-102.5, 33.2)]
    il_ring = [(-90.0, 40.0), (-88.0, 40.0), (-88.0, 41.0), (-90.0, 40.0)]
    body = _kml(("Heavy", "2026265 1300", "2026265 1500", site_ring),
                ("Light", "2026265 1900", "2026265 2100", il_ring))
    doc = hf.parse_hms_kml(body, SITE, BOX, "2026-09-22")
    assert doc["latest_window"] == (_ts(2026, 9, 22, 19, 0), _ts(2026, 9, 22, 21, 0))
    assert len(doc["polys"]) == 1                      # Illinois is not kept (off the map)
    now = _ts(2026, 9, 22, 22, 0)
    p = _poller(monkeypatch, now=now, hms_smoke=body)
    s = p.component(now)["smoke"]
    assert s["site_in_smoke"] is False and s["site_in_smoke_today"] is True
    assert s["density_today"] == "Heavy" and s["on_map"] == 0
    assert "earlier" in s["text"] and "(19:00–21:00 UTC)" in s["text"]
    assert not any(o["kind"] == "smoke" for o in p.overlays(now))   # no veil as "current"
    # the latest analysis IS over the site: then it is current
    body2 = _kml(("Heavy", "2026265 1300", "2026265 1500", il_ring),
                 ("Medium", "2026265 1900", "2026265 2100", site_ring))
    p2 = _poller(monkeypatch, now=now, hms_smoke=body2)
    s2 = p2.component(now)["smoke"]
    assert s2["site_in_smoke"] is True and s2["density"] == "Medium"
    assert [o["density"] for o in p2.overlays(now) if o["kind"] == "smoke"] == ["Medium"]


def test_attribution_credits_iem_for_the_mesoscale_discussions():
    assert "mesoscale discussions and NWS storm reports via IEM" in hf.SOURCE


# ---- robustness: independence, conditional GETs, staleness, cadence, clock ---------
def test_feeds_are_independent(monkeypatch):
    p = _poller(monkeypatch, lsrs_by_point=urllib.error.URLError("down"),
                day1otlk=b"<html>maintenance</html>")
    f = p.component(T0)["feeds"]
    assert not f["lsr"]["ok"] and "down" in f["lsr"]["error"]
    assert not f["spc_outlook"]["ok"] and f["spc_outlook"]["error"]
    assert all(f[n]["ok"] for n in hf.FEED_NAMES if n not in ("lsr", "spc_outlook"))
    c = p.component(T0)
    assert c["lsr"] == [] and c["spc"]["category"] is None and c["spc"]["mds"]


def test_conditional_get_reuses_the_parsed_copy(monkeypatch):
    calls = []
    p = _poller(monkeypatch, calls=calls)
    before = p.component(T0)["spc"]["category"]
    calls.clear()
    _install(monkeypatch, _routes(day1otlk=304), calls)
    p.poll_now(T0 + 700)
    sent = {u.split("?")[0].rsplit("/", 1)[-1]: (e, lm) for u, e, lm in calls}
    assert sent["day1otlk_cat.nolyr.geojson"] == ('"v1"', "Thu, 24 Sep 2026 17:00:00 GMT")
    c = p.component(T0 + 700)
    assert c["spc"]["category"] == before and c["feeds"]["spc_outlook"]["age_s"] == 0
    # a 304 we never asked for is an error, not an empty outlook
    q = _poller(monkeypatch, day1otlk=304)
    assert not q.component(T0)["feeds"]["spc_outlook"]["ok"]


def test_stale_feed_is_withheld_not_shown_as_current(monkeypatch):
    p = _poller(monkeypatch)
    late = T0 + p._stale_after("lsr") + 1
    c = p.component(late)
    assert not c["feeds"]["lsr"]["ok"] and c["feeds"]["lsr"]["error"].startswith("stale")
    assert c["lsr"] == [] and c["spc"]["category"] is None
    assert c["smoke"] is not None                    # smoke's cadence (30 min) is slower
    assert p.component(T0 + 10 * 3600)["available"] is False
    assert p.overlays(T0 + 10 * 3600) == []


def test_cadence_retry_and_backward_clock(monkeypatch):
    _install(monkeypatch, _routes(day1otlk=urllib.error.URLError("x")))
    p = hf.HazardFeedsPoller(_cfg())
    assert set(p.maybe_poll(T0)) == set(hf.FEED_NAMES)   # first call: everything
    assert p.maybe_poll(T0 + 60) is None
    assert set(p.maybe_poll(T0 + hf.FAIL_RETRY_SEC)) == {"spc_outlook"}   # failed: retry soon
    polled = p.maybe_poll(T0 + 601)
    assert polled and "smoke" not in polled          # HMS: 30 min cadence
    assert "smoke" in p.maybe_poll(T0 + 1801)
    assert set(p.maybe_poll(T0 - 3600)) == set(hf.FEED_NAMES)   # clock stepped back


def test_clock_step_withdraws_data_and_repolls(monkeypatch):
    p = _poller(monkeypatch)
    assert p.component(T0)["available"]
    p.clock_stepped(T0, T0 + 86400 * 150)
    c = p.component(T0)
    assert not c["available"] and c["lsr"] == [] and p.overlays(T0) == []
    assert set(p.maybe_poll(T0 + 10)) == set(hf.FEED_NAMES)
    assert p.component(T0 + 10)["available"]


def test_site_change_and_coverage(monkeypatch):
    cfg = _cfg()
    p = _poller(monkeypatch, cfg=cfg)
    cfg.GEOCODE = (33.9, -101.8)                    # GPS adoption moved the site
    assert not p.component(T0)["available"]
    assert set(p.maybe_poll(T0 + 5)) == set(hf.FEED_NAMES)
    calls = []
    q = _poller(monkeypatch, cfg=_cfg(GEOCODE=(48.14, 11.58)), calls=calls)   # Munich
    f = q.component(T0)["feeds"]
    # every feed is a US or North American product: each one says so, none is requested
    assert all("coverage" in f[n]["error"] for n in hf.FEED_NAMES)
    assert calls == [] and not q.component(T0)["available"]


def test_disabled_polls_nothing(monkeypatch):
    calls = []
    _install(monkeypatch, _routes(), calls)
    p = hf.HazardFeedsPoller(_cfg(HAZARD_FEEDS_ENABLED=False))
    assert p.maybe_poll(T0) is None and calls == []
    c = p.component(T0)
    assert c["enabled"] is False and c["feeds"]["lsr"]["error"] == "disabled"


def test_overlays_are_well_formed_and_bounded(monkeypatch):
    p = _poller(monkeypatch)
    ov = p.overlays(T0)
    kinds = {"smoke", "fire", "fire_perimeter", "spc_outlook", "spc_md", "lsr"}
    assert {o["kind"] for o in ov} == kinds
    assert len({o["key"] for o in ov}) == len(ov)
    for o in ov:
        assert o["geometry"]["type"] in ("Point", "Polygon", "MultiPolygon")
        assert isinstance(o["label"], str) and isinstance(o["rank"], int)
        assert set(o["style"]) == {"stroke", "fill", "fill_alpha", "width", "dash",
                                   "symbol", "size"}
    assert [o["key"] for o in p.overlays(T0)] == [o["key"] for o in ov]   # stable keys


# ---- HTTP helper -------------------------------------------------------------------
class _Resp(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_inflates_gzip_caps_size_and_maps_304(monkeypatch):
    body = json.dumps({"ok": 1}).encode()
    hdr = {"Content-Encoding": "gzip", "ETag": '"e"', "Last-Modified": "x"}
    monkeypatch.setattr(hf.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(gzip.compress(body), hdr))
    assert hf._get("https://x/", "ua") == (200, body, '"e"', "x")
    bomb = gzip.compress(b"0" * 200000)
    monkeypatch.setattr(hf.urllib.request, "urlopen", lambda req, timeout: _Resp(bomb, hdr))
    with pytest.raises(ValueError, match="inflates"):
        hf._get("https://x/", "ua", max_bytes=1000)
    monkeypatch.setattr(hf.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b"x" * 5000, {}))
    with pytest.raises(ValueError, match="larger"):
        hf._get("https://x/", "ua", max_bytes=1000)
    seen = {}

    def not_modified(req, timeout):
        seen.update(req.headers)
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
    monkeypatch.setattr(hf.urllib.request, "urlopen", not_modified)
    assert hf._get("https://x/", "ua", etag='"e"', last_modified="x") == \
        (304, None, '"e"', "x")
    assert seen["If-none-match"] == '"e"' and seen["Accept-encoding"] == "gzip"
