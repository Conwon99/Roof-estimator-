from __future__ import annotations  # allow dict[str,float] on Python 3.8

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel
import httpx
import math
from typing import Optional

app = FastAPI(title="Roof Size Estimator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PITCH_FACTORS: dict[str, float] = {
    "flat":             1.000,
    "low_2_12":         1.014,
    "standard_4_12":    1.054,
    "moderate_6_12":    1.118,
    "steep_9_12":       1.250,
    "very_steep_12_12": 1.414,
}

PITCH_LABELS: dict[str, str] = {
    "flat":             "Flat (0:12)",
    "low_2_12":         "Low (2:12)",
    "standard_4_12":    "Standard (4:12)",
    "moderate_6_12":    "Moderate (6:12)",
    "steep_9_12":       "Steep (9:12)",
    "very_steep_12_12": "Very Steep (12:12)",
}

OSM_HEADERS = {"User-Agent": "RoofSizeEstimator/1.0"}


class EstimateRequest(BaseModel):
    address: str
    pitch: str = "standard_4_12"


# ── Geometry helpers ──────────────────────────────────────────────────────────

def polygon_area_sqft(coords: list[tuple[float, float]]) -> float:
    if len(coords) < 3:
        return 0.0
    R = 6_371_000
    ref_lat = coords[0][1]
    cos_lat = math.cos(math.radians(ref_lat))
    pts = [(R * math.radians(lon) * cos_lat, R * math.radians(lat)) for lon, lat in coords]
    n = len(pts)
    area = sum(pts[i][0] * pts[(i+1)%n][1] - pts[(i+1)%n][0] * pts[i][1] for i in range(n))
    return abs(area) / 2.0 * 10.7639


def point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    n, inside, j = len(poly), False, len(poly) - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def centroid_dist_sq(poly: list[tuple[float, float]], px: float, py: float) -> float:
    n = len(poly)
    return ((sum(p[0] for p in poly)/n - px)**2 + (sum(p[1] for p in poly)/n - py)**2)


# ── OSM helpers ───────────────────────────────────────────────────────────────

def _extract_best_way(elements: list[dict], lat: float, lon: float) -> Optional[dict]:
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    candidates = []
    for e in elements:
        if e["type"] != "way" or "nodes" not in e:
            continue
        coords = [nodes[n] for n in e["nodes"] if n in nodes]
        if len(coords) < 3:
            continue
        candidates.append((e, coords, polygon_area_sqft(coords)))
    if not candidates:
        return None
    containing = [c for c in candidates if point_in_polygon(lon, lat, c[1])]
    if containing:
        way, _, area = max(containing, key=lambda c: c[2])
    else:
        way, _, area = min(candidates, key=lambda c: centroid_dist_sq(c[1], lon, lat))
    return {"area_sqft": area, "osm_id": way["id"], "tags": way.get("tags", {})}


async def _fetch_way(client: httpx.AsyncClient, way_id: int, lat: float, lon: float) -> Optional[dict]:
    r = await client.get(
        f"https://api.openstreetmap.org/api/0.6/way/{way_id}/full.json",
        headers=OSM_HEADERS, timeout=10,
    )
    if r.status_code != 200:
        return None
    elements = r.json().get("elements", [])
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    ways = [e for e in elements if e["type"] == "way"]
    if not ways:
        return None
    way = ways[0]
    coords = [nodes[n] for n in way.get("nodes", []) if n in nodes]
    if len(coords) < 3:
        return None
    return {"area_sqft": polygon_area_sqft(coords), "osm_id": way["id"], "tags": way.get("tags", {})}


async def _fetch_relation(client: httpx.AsyncClient, rel_id: int, lat: float, lon: float) -> Optional[dict]:
    r = await client.get(
        f"https://api.openstreetmap.org/api/0.6/relation/{rel_id}/full.json",
        headers=OSM_HEADERS, timeout=15,
    )
    if r.status_code != 200:
        return None
    elements = r.json().get("elements", [])
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    ways_by_id = {e["id"]: e for e in elements if e["type"] == "way"}
    rels = [e for e in elements if e["type"] == "relation" and e["id"] == rel_id]
    if not rels:
        return None
    rel = rels[0]
    outer_ids = [
        m["ref"] for m in rel.get("members", [])
        if m["type"] == "way" and m.get("role") in ("outer", "")
    ]
    best_area, best_way_id = 0.0, None
    for wid in outer_ids:
        way = ways_by_id.get(wid)
        if not way:
            continue
        coords = [nodes[n] for n in way.get("nodes", []) if n in nodes]
        if len(coords) < 3:
            continue
        area = polygon_area_sqft(coords)
        if area > best_area:
            best_area, best_way_id = area, wid
    if best_way_id is None:
        return None
    return {"area_sqft": best_area, "osm_id": best_way_id, "tags": rel.get("tags", {})}


async def _fetch_bbox(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[dict]:
    delta = 0.001
    bbox = f"{lon-delta},{lat-delta},{lon+delta},{lat+delta}"
    r = await client.get(
        f"https://api.openstreetmap.org/api/0.6/map.json?bbox={bbox}",
        headers=OSM_HEADERS, timeout=12,
    )
    if r.status_code != 200:
        return None
    elements = [
        e for e in r.json().get("elements", [])
        if e["type"] == "node" or (e["type"] == "way" and "building" in e.get("tags", {}))
    ]
    return _extract_best_way(elements, lat, lon)


# ── Geocoder ─────────────────────────────────────────────────────────────────

async def geocode_address(address: str) -> dict:
    params = {"q": address, "format": "json", "limit": 1, "addressdetails": 1}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params=params, headers=OSM_HEADERS, timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    if not data:
        raise HTTPException(status_code=404, detail="Address not found. Try a more specific address.")
    result = data[0]
    return {
        "lat": float(result["lat"]),
        "lon": float(result["lon"]),
        "display_name": result["display_name"],
        "osm_type": result.get("osm_type"),
        "osm_id": result.get("osm_id"),
    }


# ── Building footprint fetcher ────────────────────────────────────────────────

async def fetch_building_footprint(
    lat: float, lon: float,
    osm_type: Optional[str] = None,
    osm_id: Optional[int] = None,
) -> Optional[dict]:
    async with httpx.AsyncClient() as client:
        if osm_type == "way" and osm_id:
            result = await _fetch_way(client, osm_id, lat, lon)
            if result:
                return result
        if osm_type == "relation" and osm_id:
            result = await _fetch_relation(client, osm_id, lat, lon)
            if result:
                return result
        return await _fetch_bbox(client, lat, lon)


# ── API endpoint ──────────────────────────────────────────────────────────────

# Registered on both paths: Netlify preserves the original URL path in the
# Lambda event, so /api/estimate arrives correctly.  The /estimate alias
# covers the (rare) case where a proxy strips the /api prefix.
@app.post("/api/estimate")
@app.post("/estimate")
async def estimate_roof(req: EstimateRequest):
    if req.pitch not in PITCH_FACTORS:
        raise HTTPException(status_code=400, detail=f"Unknown pitch '{req.pitch}'.")

    geo = await geocode_address(req.address)
    building = await fetch_building_footprint(
        geo["lat"], geo["lon"],
        osm_type=geo.get("osm_type"),
        osm_id=geo.get("osm_id"),
    )

    pitch_factor = PITCH_FACTORS[req.pitch]
    pitch_label  = PITCH_LABELS[req.pitch]

    if building:
        fp_sqft   = building["area_sqft"]
        roof_sqft = fp_sqft * pitch_factor
        floors    = int(building["tags"].get("building:levels", 1) or 1)
        return {
            "success": True,
            "formatted_address": geo["display_name"],
            "latitude": geo["lat"],
            "longitude": geo["lon"],
            "footprint_sqft": round(fp_sqft, 1),
            "roof_sqft": round(roof_sqft, 1),
            "footprint_m2": round(fp_sqft / 10.7639, 1),
            "roof_m2": round(roof_sqft / 10.7639, 1),
            "floors": floors,
            "pitch_label": pitch_label,
            "pitch_factor": pitch_factor,
            "osm_id": building["osm_id"],
            "confidence": "high",
            "message": "Building footprint found in OpenStreetMap.",
        }
    else:
        return {
            "success": False,
            "formatted_address": geo["display_name"],
            "latitude": geo["lat"],
            "longitude": geo["lon"],
            "footprint_sqft": None,
            "roof_sqft": None,
            "footprint_m2": None,
            "roof_m2": None,
            "floors": None,
            "pitch_label": pitch_label,
            "pitch_factor": pitch_factor,
            "osm_id": None,
            "confidence": "unavailable",
            "message": (
                "No building footprint found in OpenStreetMap for this address. "
                "Use the satellite view link to measure manually."
            ),
        }


@app.get("/")
@app.get("/api/health")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "Roof Size Estimator"}


# ── Netlify / Lambda entrypoint ───────────────────────────────────────────────

_mangum = Mangum(app, lifespan="off")

def handler(event, context):
    """
    Explicit handler so Netlify can find the entrypoint.
    Wraps Mangum so any unhandled exception returns JSON (not an HTML crash page).
    """
    try:
        return _mangum(event, context)
    except Exception as exc:
        import json, traceback
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "detail": str(exc),
                "traceback": traceback.format_exc()[-2000:],
            }),
        }
