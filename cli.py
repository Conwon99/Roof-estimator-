#!/usr/bin/env python3
"""
Command-line interface for roof size estimation.

Usage:
    python cli.py "1600 Pennsylvania Ave NW, Washington, DC"
    python cli.py "350 5th Ave, New York, NY 10118" --pitch steep_9_12
    python cli.py "10 Downing St, London, UK" --pitch moderate_6_12
"""

import argparse
import asyncio
import math
import sys
from typing import Optional

try:
    import httpx
except ImportError:
    sys.exit("httpx is required: pip install httpx")

PITCH_FACTORS: dict[str, float] = {
    "flat":             1.000,
    "low_2_12":         1.014,
    "standard_4_12":    1.054,
    "moderate_6_12":    1.118,
    "steep_9_12":       1.250,
    "very_steep_12_12": 1.414,
}

OSM_HEADERS = {"User-Agent": "RoofSizeEstimatorCLI/1.0"}


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

def _best_way_from_elements(elements: list[dict], lat: float, lon: float) -> Optional[dict]:
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

    return {"area_sqft": area, "tags": way.get("tags", {})}


async def fetch_footprint(lat: float, lon: float, osm_type: Optional[str], osm_id: Optional[int]) -> Optional[dict]:
    async with httpx.AsyncClient() as client:
        if osm_type == "way" and osm_id:
            r = await client.get(
                f"https://api.openstreetmap.org/api/0.6/way/{osm_id}/full.json",
                headers=OSM_HEADERS, timeout=20,
            )
            if r.status_code == 200:
                elements = r.json().get("elements", [])
                nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
                ways = [e for e in elements if e["type"] == "way"]
                if ways:
                    coords = [nodes[n] for n in ways[0].get("nodes", []) if n in nodes]
                    if len(coords) >= 3:
                        return {"area_sqft": polygon_area_sqft(coords), "tags": ways[0].get("tags", {})}

        if osm_type == "relation" and osm_id:
            r = await client.get(
                f"https://api.openstreetmap.org/api/0.6/relation/{osm_id}/full.json",
                headers=OSM_HEADERS, timeout=25,
            )
            if r.status_code == 200:
                elements = r.json().get("elements", [])
                nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
                ways_by_id = {e["id"]: e for e in elements if e["type"] == "way"}
                rels = [e for e in elements if e["type"] == "relation" and e["id"] == osm_id]
                if rels:
                    outer_ids = [m["ref"] for m in rels[0].get("members", []) if m["type"]=="way" and m.get("role") in ("outer","")]
                    best_area, best_tags = 0.0, {}
                    for wid in outer_ids:
                        w = ways_by_id.get(wid)
                        if not w:
                            continue
                        coords = [nodes[n] for n in w.get("nodes", []) if n in nodes]
                        if len(coords) < 3:
                            continue
                        a = polygon_area_sqft(coords)
                        if a > best_area:
                            best_area = a
                            best_tags = rels[0].get("tags", {})
                    if best_area > 0:
                        return {"area_sqft": best_area, "tags": best_tags}

        # Fallback: bbox search
        delta = 0.001
        bbox = f"{lon-delta},{lat-delta},{lon+delta},{lat+delta}"
        r = await client.get(
            f"https://api.openstreetmap.org/api/0.6/map.json?bbox={bbox}",
            headers=OSM_HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return None
        elements = [
            e for e in r.json().get("elements", [])
            if e["type"] == "node" or (e["type"] == "way" and "building" in e.get("tags", {}))
        ]
        return _best_way_from_elements(elements, lat, lon)


async def geocode(address: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers=OSM_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    if not data:
        sys.exit(f"Address not found: {address!r}")
    return {
        "lat": float(data[0]["lat"]),
        "lon": float(data[0]["lon"]),
        "display_name": data[0]["display_name"],
        "osm_type": data[0].get("osm_type"),
        "osm_id": data[0].get("osm_id"),
    }


async def run(address: str, pitch: str) -> None:
    factor = PITCH_FACTORS.get(pitch)
    if factor is None:
        sys.exit(f"Unknown pitch: {pitch!r}. Choices: {', '.join(PITCH_FACTORS)}")

    print(f"\nGeocoding '{address}' ...")
    geo = await geocode(address)
    print(f"  {geo['display_name']}")
    print(f"  lat={geo['lat']:.6f}, lon={geo['lon']:.6f}  (osm_type={geo['osm_type']}, id={geo['osm_id']})")

    print("Fetching building footprint ...")
    building = await fetch_footprint(geo["lat"], geo["lon"], geo["osm_type"], geo["osm_id"])

    print()
    if building:
        fp   = building["area_sqft"]
        roof = fp * factor
        floors = int(building["tags"].get("building:levels", 1) or 1)
        print(f"  Footprint  : {fp:>10,.1f} ft²   ({fp/10.7639:,.1f} m²)")
        print(f"  Roof area  : {roof:>10,.1f} ft²   ({roof/10.7639:,.1f} m²)  [pitch={pitch}, ×{factor}]")
        print(f"  Storeys    : {floors}")
    else:
        print("  No building footprint found in OpenStreetMap.")
        print(f"  Satellite view → https://www.google.com/maps/@{geo['lat']},{geo['lon']},18z/data=!3m1!1e3")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate roof size from a street address.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Pitch choices: " + ", ".join(PITCH_FACTORS),
    )
    parser.add_argument("address", help="Full street address")
    parser.add_argument(
        "--pitch",
        default="standard_4_12",
        choices=list(PITCH_FACTORS),
        metavar="PITCH",
        help="Roof pitch (default: standard_4_12)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.address, args.pitch))


if __name__ == "__main__":
    main()
