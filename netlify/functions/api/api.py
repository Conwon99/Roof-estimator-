"""
Netlify serverless function — pure Python stdlib, zero external dependencies.
Handles POST /api/estimate and GET /api/health (via redirect in netlify.toml).
"""
import json
import math
import urllib.request
import urllib.parse

PITCH_FACTORS = {
    "flat":             1.000,
    "low_2_12":         1.014,
    "standard_4_12":    1.054,
    "moderate_6_12":    1.118,
    "steep_9_12":       1.250,
    "very_steep_12_12": 1.414,
}
PITCH_LABELS = {
    "flat":             "Flat (0:12)",
    "low_2_12":         "Low (2:12)",
    "standard_4_12":    "Standard (4:12)",
    "moderate_6_12":    "Moderate (6:12)",
    "steep_9_12":       "Steep (9:12)",
    "very_steep_12_12": "Very Steep (12:12)",
}
HEADERS = {"User-Agent": "RoofSizeEstimator/1.0"}


# ── Geometry helpers ──────────────────────────────────────────────────────────

def polygon_area_sqft(coords):
    """Shoelace formula on (lon, lat) pairs, projected to metres → ft²."""
    if len(coords) < 3:
        return 0.0
    R = 6_371_000
    cos_lat = math.cos(math.radians(coords[0][1]))
    pts = [(R * math.radians(lon) * cos_lat, R * math.radians(lat)) for lon, lat in coords]
    n = len(pts)
    area = sum(pts[i][0] * pts[(i+1)%n][1] - pts[(i+1)%n][0] * pts[i][1] for i in range(n))
    return abs(area) / 2.0 * 10.7639


def point_in_polygon(px, py, poly):
    n, inside, j = len(poly), False, len(poly) - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def nearest_way(elements, lat, lon):
    """Return the best building footprint from a list of OSM elements."""
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    candidates = []
    for e in elements:
        if e["type"] != "way" or "nodes" not in e:
            continue
        coords = [nodes[n] for n in e["nodes"] if n in nodes]
        if len(coords) >= 3:
            candidates.append((e, coords, polygon_area_sqft(coords)))
    if not candidates:
        return None
    containing = [c for c in candidates if point_in_polygon(lon, lat, c[1])]
    pool = containing if containing else candidates
    way, _, area = max(pool, key=lambda c: c[2]) if containing else \
                   min(candidates, key=lambda c: (sum(p[0] for p in c[1])/len(c[1]) - lon)**2 + (sum(p[1] for p in c[1])/len(c[1]) - lat)**2)
    return {"area_sqft": area, "osm_id": way["id"], "tags": way.get("tags", {})}


# ── HTTP helper ───────────────────────────────────────────────────────────────

def get_json(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── OSM data fetchers ─────────────────────────────────────────────────────────

def geocode(address):
    qs = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1, "addressdetails": 1})
    data = get_json(f"https://nominatim.openstreetmap.org/search?{qs}", timeout=8)
    if not data:
        return None
    r = data[0]
    return {
        "lat": float(r["lat"]), "lon": float(r["lon"]),
        "display_name": r["display_name"],
        "osm_type": r.get("osm_type"), "osm_id": r.get("osm_id"),
    }


def fetch_building(lat, lon, osm_type=None, osm_id=None):
    base = "https://api.openstreetmap.org/api/0.6"

    if osm_type == "way" and osm_id:
        try:
            els = get_json(f"{base}/way/{osm_id}/full.json", timeout=10).get("elements", [])
            nodes = {e["id"]: (e["lon"], e["lat"]) for e in els if e["type"] == "node"}
            ways = [e for e in els if e["type"] == "way"]
            if ways:
                coords = [nodes[n] for n in ways[0].get("nodes", []) if n in nodes]
                if len(coords) >= 3:
                    return {"area_sqft": polygon_area_sqft(coords), "osm_id": ways[0]["id"], "tags": ways[0].get("tags", {})}
        except Exception:
            pass

    if osm_type == "relation" and osm_id:
        try:
            els = get_json(f"{base}/relation/{osm_id}/full.json", timeout=15).get("elements", [])
            nodes = {e["id"]: (e["lon"], e["lat"]) for e in els if e["type"] == "node"}
            ways_by_id = {e["id"]: e for e in els if e["type"] == "way"}
            rels = [e for e in els if e["type"] == "relation" and e["id"] == osm_id]
            if rels:
                outer_ids = [m["ref"] for m in rels[0].get("members", []) if m["type"] == "way" and m.get("role") in ("outer", "")]
                best_area, best_id, best_tags = 0.0, None, {}
                for wid in outer_ids:
                    w = ways_by_id.get(wid)
                    if not w:
                        continue
                    coords = [nodes[n] for n in w.get("nodes", []) if n in nodes]
                    area = polygon_area_sqft(coords)
                    if area > best_area:
                        best_area, best_id, best_tags = area, wid, rels[0].get("tags", {})
                if best_id:
                    return {"area_sqft": best_area, "osm_id": best_id, "tags": best_tags}
        except Exception:
            pass

    # Fallback: bbox search
    try:
        d = 0.001
        bbox = f"{lon-d},{lat-d},{lon+d},{lat+d}"
        els = get_json(f"{base}/map.json?bbox={bbox}", timeout=12).get("elements", [])
        els = [e for e in els if e["type"] == "node" or (e["type"] == "way" and "building" in e.get("tags", {}))]
        return nearest_way(els, lat, lon)
    except Exception:
        return None


# ── Response helpers ──────────────────────────────────────────────────────────

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}

def ok(body):
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", **CORS}, "body": json.dumps(body)}

def err(status, detail):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", **CORS}, "body": json.dumps({"detail": detail})}


# ── Lambda entrypoint ─────────────────────────────────────────────────────────

def handler(event, context):
    method = event.get("httpMethod", "GET")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    if method == "GET":
        return ok({"status": "ok", "service": "Roof Size Estimator"})

    if method != "POST":
        return err(405, "Method not allowed")

    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return err(400, "Invalid JSON body")

    address = (body.get("address") or "").strip()
    pitch   = body.get("pitch", "standard_4_12")

    if not address:
        return err(400, "Address is required")
    if pitch not in PITCH_FACTORS:
        return err(400, f"Unknown pitch '{pitch}'")

    try:
        geo = geocode(address)
    except Exception as exc:
        return err(500, f"Geocoding error: {exc}")

    if not geo:
        return err(404, "Address not found. Try a more specific address.")

    try:
        building = fetch_building(geo["lat"], geo["lon"], geo.get("osm_type"), geo.get("osm_id"))
    except Exception:
        building = None

    pf    = PITCH_FACTORS[pitch]
    label = PITCH_LABELS[pitch]

    if building:
        fp   = building["area_sqft"]
        roof = fp * pf
        floors = int(building["tags"].get("building:levels", 1) or 1)
        return ok({
            "success": True,
            "formatted_address": geo["display_name"],
            "latitude":  geo["lat"],
            "longitude": geo["lon"],
            "footprint_sqft": round(fp, 1),
            "roof_sqft":      round(roof, 1),
            "footprint_m2":   round(fp   / 10.7639, 1),
            "roof_m2":        round(roof / 10.7639, 1),
            "floors":      floors,
            "pitch_label": label,
            "pitch_factor": pf,
            "osm_id":    building["osm_id"],
            "confidence": "high",
            "message":   "Building footprint found in OpenStreetMap.",
        })
    else:
        return ok({
            "success": False,
            "formatted_address": geo["display_name"],
            "latitude":  geo["lat"],
            "longitude": geo["lon"],
            "footprint_sqft": None, "roof_sqft": None,
            "footprint_m2":   None, "roof_m2":   None,
            "floors":      None,
            "pitch_label": label,
            "pitch_factor": pf,
            "osm_id":    None,
            "confidence": "unavailable",
            "message":   "No building footprint found in OpenStreetMap. Use the satellite view link to measure manually.",
        })
