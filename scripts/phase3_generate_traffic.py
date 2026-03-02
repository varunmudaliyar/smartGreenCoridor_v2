#!/usr/bin/env python3
"""
Phase 3: Generate REALISTIC Traffic Demand from TomTom Flow API
================================================================
SMART CACHING: Fetches from TomTom ONCE, caches locally, reuses forever.
  - First run: ~400 API calls → saves to JSON cache
  - Every run after: 0 API calls → loads from cache
  - Manual refresh: python phase3_generate_traffic.py --refresh

Usage:
  python phase3_generate_traffic.py            # Uses cache (0 API calls)
  python phase3_generate_traffic.py --refresh  # Force fresh data from TomTom
"""

import os
import sys
import json
import math
import time
import random
import subprocess
import requests
from collections import defaultdict
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUT_DIR = os.path.join(PROJECT_DIR, "sumo_data")

API_KEYS = [
    "5liqaneG8HxlWT3e1PvhZioj57vqdbST",
    "mPtfLSFK8MXJHzOuvE6CJJKYn56Hcw2v",
    "3yQ8eHK7zA1jO96iAfi6biOGYHuEttC2",
]

# Bounding box — Andheri
BBOX = {
    "north": 19.1350,
    "south": 19.0980,
    "west": 72.8220,
    "east": 72.8700,
}

# Traffic generation settings
SIMULATION_DURATION = 3600
BASE_VEHICLES = 400
CONGESTION_MULTIPLIER = 2.0

# Hospital data
KNOWN_HOSPITALS = [
    (19.1190, 72.8460, "Kokilaben Dhirubhai Ambani Hospital"),
    (19.1080, 72.8370, "Holy Spirit Hospital"),
    (19.1270, 72.8400, "Criticare Hospital"),
    (19.1160, 72.8300, "SRV Hospital"),
    (19.1090, 72.8540, "Seven Hills Hospital"),
    (19.1250, 72.8550, "Akshar Hospital"),
    (19.1100, 72.8300, "Nanavati Hospital"),
    (19.1150, 72.8430, "Dr LH Hiranandani Hospital"),
    (19.1200, 72.8350, "Sujay Hospital"),
    (19.1050, 72.8450, "Cooper Hospital"),
]
NUM_HOSPITALS = 8

# TomTom URLs
FLOW_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"
INCIDENTS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"

# Cache files
CACHE_FLOW = os.path.join(OUT_DIR, "tomtom_traffic_flow.json")
CACHE_INCIDENTS = os.path.join(OUT_DIR, "tomtom_incidents.json")
CACHE_META = os.path.join(OUT_DIR, "cache_metadata.json")

# ============================================================================
# PARSE CLI FLAGS
# ============================================================================

FORCE_REFRESH = "--refresh" in sys.argv

# ============================================================================
# CACHE MANAGER
# ============================================================================

def is_cache_valid():
    """Check if cached TomTom data exists."""
    if not os.path.exists(CACHE_FLOW):
        return False
    if not os.path.exists(CACHE_INCIDENTS):
        return False
    if not os.path.exists(CACHE_META):
        return False

    with open(CACHE_META, "r") as f:
        meta = json.load(f)

    print(f"  📦 Cache found!")
    print(f"     Fetched: {meta.get('fetched_at', 'unknown')}")
    print(f"     Flow segments: {meta.get('flow_segments', 0)}")
    print(f"     Incidents: {meta.get('incidents', 0)}")
    print(f"     API calls used: {meta.get('api_calls', 0)}")
    return True


def save_cache_meta(flow_count, incident_count, api_calls):
    """Save cache metadata."""
    meta = {
        "fetched_at": datetime.now().isoformat(),
        "flow_segments": flow_count,
        "incidents": incident_count,
        "api_calls": api_calls,
        "bbox": BBOX,
    }
    with open(CACHE_META, "w") as f:
        json.dump(meta, f, indent=2)


def load_cached_flow():
    """Load cached flow data."""
    with open(CACHE_FLOW, "r") as f:
        return json.load(f)


def load_cached_incidents():
    """Load cached incidents."""
    with open(CACHE_INCIDENTS, "r") as f:
        return json.load(f)

# ============================================================================
# API KEY MANAGER
# ============================================================================

class APIKeyManager:
    def __init__(self, keys):
        self.keys = list(keys)
        self.idx = 0
        self.call_counts = {k: 0 for k in keys}
        self.fails = 0

    @property
    def key(self):
        return self.keys[self.idx]

    def success(self):
        self.call_counts[self.key] += 1
        self.fails = 0

    def fail(self):
        self.fails += 1
        if self.fails >= 3:
            old_idx = self.idx
            self.idx = (self.idx + 1) % len(self.keys)
            self.fails = 0
            print(f"  🔄 Key rotation: ...{self.keys[old_idx][-4:]} → ...{self.key[-4:]}")
            return self.idx != 0
        return True

    def total_calls(self):
        return sum(self.call_counts.values())

# ============================================================================
# UTILITY
# ============================================================================

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ============================================================================
# STEP 1: Fetch OR Load TomTom Traffic Flow
# ============================================================================

def get_traffic_flow(key_mgr):
    """Fetch from API if --refresh or no cache, otherwise load cache."""
    print("=" * 70)
    print("PHASE 3 — STEP 1: TomTom Traffic Flow Data")
    print("=" * 70)

    if not FORCE_REFRESH and is_cache_valid():
        print(f"\n  ✅ Using CACHED data (0 API calls)")
        print(f"     To refresh: python phase3_generate_traffic.py --refresh")
        return load_cached_flow()

    if FORCE_REFRESH:
        print(f"\n  🔄 FORCED REFRESH — fetching fresh data from TomTom...")
    else:
        print(f"\n  📡 No cache found — fetching from TomTom API (first time)...")

    flow_data = _fetch_flow_from_api(key_mgr)
    return flow_data


def _fetch_flow_from_api(key_mgr):
    """Actually call TomTom Flow API at grid points."""
    flow_data = []
    seen_segments = set()

    # Grid of query points
    lat_step = 0.002
    lon_step = 0.0025
    query_points = []

    lat = BBOX["south"]
    while lat <= BBOX["north"]:
        lon = BBOX["west"]
        while lon <= BBOX["east"]:
            query_points.append((lat, lon))
            lon += lon_step
        lat += lat_step

    print(f"  📍 Query grid: {len(query_points)} points")
    print(f"  📞 Estimated API calls: {len(query_points)}")

    fetched = 0
    failed = 0

    for idx, (lat, lon) in enumerate(query_points):
        params = {
            "key": key_mgr.key,
            "point": f"{lat},{lon}",
            "unit": "KMPH",
            "thickness": 10,
        }

        try:
            resp = requests.get(FLOW_URL, params=params, timeout=10)

            if resp.status_code == 429:
                time.sleep(2)
                if not key_mgr.fail():
                    print("  ❌ All keys rate-limited! Saving partial data...")
                    break
                continue

            if resp.status_code in (401, 403):
                if not key_mgr.fail():
                    break
                continue

            if resp.status_code != 200:
                failed += 1
                continue

            key_mgr.success()
            data = resp.json()
            flow_info = data.get("flowSegmentData", {})
            if not flow_info:
                continue

            current_speed = flow_info.get("currentSpeed", 0)
            free_flow_speed = flow_info.get("freeFlowSpeed", 0)
            current_tt = flow_info.get("currentTravelTime", 0)
            free_flow_tt = flow_info.get("freeFlowTravelTime", 0)
            confidence = flow_info.get("confidence", 0)
            road_closure = flow_info.get("roadClosure", False)
            coords = flow_info.get("coordinates", {}).get("coordinate", [])

            if not coords or len(coords) < 2:
                continue

            # De-duplicate
            seg_key = (
                round(coords[0].get("latitude", 0), 4),
                round(coords[0].get("longitude", 0), 4),
                round(coords[-1].get("latitude", 0), 4),
                round(coords[-1].get("longitude", 0), 4),
            )
            if seg_key in seen_segments:
                continue
            seen_segments.add(seg_key)

            congestion = max(0, 1 - (current_speed / free_flow_speed)) if free_flow_speed > 0 else 0

            flow_data.append({
                "query_lat": lat,
                "query_lon": lon,
                "current_speed_kmph": current_speed,
                "free_flow_speed_kmph": free_flow_speed,
                "current_travel_time": current_tt,
                "free_flow_travel_time": free_flow_tt,
                "congestion_ratio": round(congestion, 3),
                "confidence": confidence,
                "road_closure": road_closure,
                "coordinates": [
                    {"lat": c.get("latitude"), "lon": c.get("longitude")}
                    for c in coords
                ],
            })
            fetched += 1

        except requests.exceptions.Timeout:
            failed += 1
        except Exception:
            failed += 1

        time.sleep(0.12)

        if idx % 50 == 0 and idx > 0:
            print(f"  ... {idx}/{len(query_points)} → {fetched} segments "
                  f"[Key ...{key_mgr.key[-4:]} | {key_mgr.total_calls()} calls]")

    # Save cache
    with open(CACHE_FLOW, "w") as f:
        json.dump(flow_data, f, indent=2)
    print(f"\n  ✅ Fetched {len(flow_data)} unique flow segments")
    print(f"  💾 Cached: {CACHE_FLOW}")
    print(f"  📞 API calls used: {key_mgr.total_calls()}")

    # Print stats
    if flow_data:
        speeds = [s["current_speed_kmph"] for s in flow_data]
        congs = [s["congestion_ratio"] for s in flow_data]
        print(f"\n  📊 Traffic Flow Statistics:")
        print(f"     Speed: avg={sum(speeds)/len(speeds):.1f}, "
              f"min={min(speeds):.1f}, max={max(speeds):.1f} km/h")
        print(f"     Congestion: avg={sum(congs)/len(congs):.2f}, max={max(congs):.2f}")
        print(f"     Heavy (>0.5): {sum(1 for c in congs if c > 0.5)}")
        print(f"     Road closures: {sum(1 for s in flow_data if s['road_closure'])}")

    return flow_data

# ============================================================================
# STEP 2: Fetch OR Load Incidents
# ============================================================================

def get_incidents(key_mgr):
    """Fetch from API if --refresh or no cache, otherwise load cache."""
    print("\n" + "=" * 70)
    print("PHASE 3 — STEP 2: TomTom Traffic Incidents")
    print("=" * 70)

    if not FORCE_REFRESH and os.path.exists(CACHE_INCIDENTS):
        print(f"  ✅ Using CACHED incidents (0 API calls)")
        return load_cached_incidents()

    print(f"  📡 Fetching incidents from TomTom...")

    bbox_str = f"{BBOX['south']},{BBOX['west']},{BBOX['north']},{BBOX['east']}"
    incidents = []

    params = {
        "key": key_mgr.key,
        "bbox": bbox_str,
        "fields": (
            "{incidents{type,geometry{type,coordinates},"
            "properties{iconCategory,magnitudeOfDelay,events{description},"
            "startTime,endTime,from,to}}}"
        ),
        "language": "en-US",
        "categoryFilter": "0,1,2,3,4,5,6,7,8,9,10,11,14",
    }

    try:
        resp = requests.get(INCIDENTS_URL, params=params, timeout=15)
        if resp.status_code == 200:
            key_mgr.success()
            data = resp.json()

            for inc in data.get("incidents", []):
                props = inc.get("properties", {})
                geom = inc.get("geometry", {})
                coords = geom.get("coordinates", [])

                if geom.get("type") == "Point" and len(coords) >= 2:
                    lat, lon = coords[1], coords[0]
                elif geom.get("type") == "LineString" and coords:
                    mid = coords[len(coords) // 2]
                    lat, lon = mid[1], mid[0]
                else:
                    continue

                incidents.append({
                    "type": inc.get("type", "unknown"),
                    "lat": lat, "lon": lon,
                    "category": props.get("iconCategory", 0),
                    "delay_magnitude": props.get("magnitudeOfDelay", 0),
                    "from": props.get("from", ""),
                    "to": props.get("to", ""),
                    "events": [e.get("description", "") for e in props.get("events", [])],
                })
            print(f"  ✅ Found {len(incidents)} incidents")
        else:
            print(f"  ⚠️  Incidents API: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️  {e}")

    # Cache
    with open(CACHE_INCIDENTS, "w") as f:
        json.dump(incidents, f, indent=2)
    print(f"  💾 Cached: {CACHE_INCIDENTS}")

    return incidents

# ============================================================================
# STEP 3: Map traffic flow → SUMO edges
# ============================================================================

def map_flow_to_edges(flow_data, incidents):
    """Map TomTom flow segments to SUMO edges."""
    print("\n" + "=" * 70)
    print("PHASE 3 — STEP 3: Mapping traffic flow → SUMO edges")
    print("=" * 70)

    nodes_path = os.path.join(OUT_DIR, "all_nodes.json")
    with open(nodes_path, "r") as f:
        all_nodes = json.load(f)
    node_lookup = {n["node_id"]: n for n in all_nodes}

    edg_path = os.path.join(OUT_DIR, "andheri.edg.xml")
    edges = []

    with open(edg_path, "r", encoding="utf-8") as f:
        for line in f:
            if "<edge " not in line:
                continue
            eid = fn_id = tn_id = None
            priority = 3
            num_lanes = 1
            speed = 13.89

            for p in line.split():
                if p.startswith('id="'): eid = p.split('"')[1]
                elif p.startswith('from="'): fn_id = p.split('"')[1]
                elif p.startswith('to="'): tn_id = p.split('"')[1]
                elif p.startswith('priority="'):
                    try: priority = int(p.split('"')[1])
                    except: pass
                elif p.startswith('numLanes="'):
                    try: num_lanes = int(p.split('"')[1])
                    except: pass
                elif p.startswith('speed="'):
                    try: speed = float(p.split('"')[1])
                    except: pass

            if eid and fn_id and tn_id:
                fn = node_lookup.get(fn_id)
                tn = node_lookup.get(tn_id)
                if fn and tn:
                    edges.append({
                        "id": eid,
                        "from": fn_id, "to": tn_id,
                        "mid_lat": (fn["lat"] + tn["lat"]) / 2,
                        "mid_lon": (fn["lon"] + tn["lon"]) / 2,
                        "priority": priority,
                        "num_lanes": num_lanes,
                        "default_speed": speed,
                    })

    print(f"  📏 Edges with coordinates: {len(edges)}")

    # Flow midpoints
    flow_points = []
    for seg in flow_data:
        coords = seg["coordinates"]
        if coords:
            mid = coords[len(coords) // 2]
            flow_points.append({"lat": mid["lat"], "lon": mid["lon"], "segment": seg})

    print(f"  📊 Flow points: {len(flow_points)}")

    # Match edges → flow
    edge_traffic = {}
    matched = unmatched = 0

    for edge in edges:
        best_flow = None
        best_dist = float("inf")

        for fp in flow_points:
            d = haversine(edge["mid_lat"], edge["mid_lon"], fp["lat"], fp["lon"])
            if d < best_dist:
                best_dist = d
                best_flow = fp["segment"]

        if best_flow and best_dist < 200:
            real_speed_ms = max(1.0, best_flow["current_speed_kmph"] / 3.6)
            edge_traffic[edge["id"]] = {
                "edge_id": edge["id"],
                "real_speed_ms": round(real_speed_ms, 2),
                "real_speed_kmph": round(best_flow["current_speed_kmph"], 1),
                "free_flow_speed_kmph": round(best_flow["free_flow_speed_kmph"], 1),
                "congestion_ratio": best_flow["congestion_ratio"],
                "road_closure": best_flow.get("road_closure", False),
                "match_distance_m": round(best_dist, 1),
                "priority": edge["priority"],
                "num_lanes": edge["num_lanes"],
            }
            matched += 1
        else:
            edge_traffic[edge["id"]] = {
                "edge_id": edge["id"],
                "real_speed_ms": edge["default_speed"],
                "real_speed_kmph": round(edge["default_speed"] * 3.6, 1),
                "free_flow_speed_kmph": round(edge["default_speed"] * 3.6, 1),
                "congestion_ratio": 0.0,
                "road_closure": False,
                "match_distance_m": -1,
                "priority": edge["priority"],
                "num_lanes": edge["num_lanes"],
            }
            unmatched += 1

    print(f"  ✅ Matched: {matched} edges to real traffic")
    print(f"  ⚠️  Defaults: {unmatched}")

    # Incident mapping
    incident_edges = set()
    for inc in incidents:
        for edge in edges:
            d = haversine(edge["mid_lat"], edge["mid_lon"], inc["lat"], inc["lon"])
            if d < 100:
                incident_edges.add(edge["id"])
                if edge["id"] in edge_traffic:
                    edge_traffic[edge["id"]]["has_incident"] = True
                    edge_traffic[edge["id"]]["incident_type"] = inc["type"]

    print(f"  🚨 Edges with incidents: {len(incident_edges)}")

    # Save
    with open(os.path.join(OUT_DIR, "edge_traffic_data.json"), "w") as f:
        json.dump(edge_traffic, f, indent=2)

    # Stats
    congs = [et["congestion_ratio"] for et in edge_traffic.values() if et["match_distance_m"] >= 0]
    if congs:
        print(f"\n  📊 Network Congestion Profile:")
        print(f"     Free (<0.2):     {sum(1 for c in congs if c < 0.2)}")
        print(f"     Moderate (0.2-0.5): {sum(1 for c in congs if 0.2 <= c < 0.5)}")
        print(f"     Heavy (0.5-0.8):    {sum(1 for c in congs if 0.5 <= c < 0.8)}")
        print(f"     Severe (>0.8):      {sum(1 for c in congs if c >= 0.8)}")

    return edge_traffic, edges

# ============================================================================
# STEP 4: Generate congestion-weighted traffic demand
# ============================================================================

def generate_traffic_demand(edge_traffic):
    """Generate SUMO trips weighted by real congestion data."""
    print("\n" + "=" * 70)
    print("PHASE 3 — STEP 4: Generating congestion-weighted traffic demand")
    print("=" * 70)

    net_path = os.path.join(OUT_DIR, "andheri.net.xml")

    valid_edges = set()
    with open(net_path, "r", encoding="utf-8") as f:
        for line in f:
            if "<edge " in line and 'function="internal"' not in line:
                for p in line.split():
                    if p.startswith('id="'):
                        eid = p.split('"')[1]
                        if not eid.startswith(":"):
                            valid_edges.add(eid)

    print(f"  📏 Valid edges: {len(valid_edges)}")

    # Weight by congestion + priority + lanes
    weighted = []
    for eid in valid_edges:
        et = edge_traffic.get(eid, {})
        congestion = et.get("congestion_ratio", 0.1)
        priority = et.get("priority", 3)
        lanes = et.get("num_lanes", 1)
        weight = max(0.1, (priority / 5) * lanes * (0.5 + congestion * CONGESTION_MULTIPLIER))
        weighted.append((eid, weight))

    total_w = sum(w for _, w in weighted)
    edge_ids = [eid for eid, _ in weighted]
    edge_weights = [w / total_w for _, w in weighted]

    avg_cong = sum(et.get("congestion_ratio", 0) for et in edge_traffic.values()) / max(1, len(edge_traffic))
    num_vehicles = min(800, int(BASE_VEHICLES * (1 + avg_cong * CONGESTION_MULTIPLIER)))

    print(f"  🚗 Vehicles: {num_vehicles} (avg congestion: {avg_cong:.2f})")

    # Generate trips
    trips_path = os.path.join(OUT_DIR, "random_trips.xml")
    with open(trips_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<trips>\n')
        for i in range(num_vehicles):
            depart = round(i * (SIMULATION_DURATION / num_vehicles) + random.uniform(0, 2), 1)
            src = random.choices(edge_ids, weights=edge_weights, k=1)[0]
            dst = random.choices(edge_ids, weights=edge_weights, k=1)[0]
            attempts = 0
            while dst == src and attempts < 10:
                dst = random.choices(edge_ids, weights=edge_weights, k=1)[0]
                attempts += 1
            if dst != src:
                f.write(f'    <trip id="car_{i}" depart="{depart}" from="{src}" to="{dst}"/>\n')
        f.write('</trips>\n')

    # duarouter
    routes_path = os.path.join(OUT_DIR, "routes.rou.xml")
    try:
        result = subprocess.run([
            "duarouter", "-n", net_path, "-t", trips_path, "-o", routes_path,
            "--ignore-errors", "true", "--no-warnings", "true",
        ], capture_output=True, text=True, cwd=OUT_DIR, timeout=180)
        if result.returncode == 0 and os.path.exists(routes_path):
            print(f"  ✅ Routes: {os.path.getsize(routes_path)/1024:.1f} KB")
        else:
            print(f"  ⚠️  duarouter failed, using trips")
            import shutil
            shutil.copy2(trips_path, routes_path)
    except FileNotFoundError:
        print("  ⚠️  duarouter not found")
        import shutil
        shutil.copy2(trips_path, routes_path)

    # Edge speeds file
    speed_path = os.path.join(OUT_DIR, "edge_speeds.xml")
    with open(speed_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n')
        for eid in valid_edges:
            et = edge_traffic.get(eid, {})
            speed = max(1.0, et.get("real_speed_ms", 13.89))
            f.write(f'    <edge id="{eid}" speed="{speed:.2f}"/>\n')
        f.write('</additional>\n')
    print(f"  💾 Edge speeds: {speed_path}")

    return routes_path, num_vehicles

# ============================================================================
# STEP 5: Map hospitals
# ============================================================================

def map_hospitals():
    """Map hospitals to nearest network edges."""
    print("\n" + "=" * 70)
    print("PHASE 3 — STEP 5: Mapping hospitals to network")
    print("=" * 70)

    with open(os.path.join(OUT_DIR, "all_nodes.json"), "r") as f:
        all_nodes = json.load(f)

    edg_path = os.path.join(OUT_DIR, "andheri.edg.xml")
    edge_nodes = []
    with open(edg_path, "r", encoding="utf-8") as f:
        for line in f:
            if "<edge " not in line: continue
            eid = fn = tn = None
            for p in line.split():
                if p.startswith('id="'): eid = p.split('"')[1]
                elif p.startswith('from="'): fn = p.split('"')[1]
                elif p.startswith('to="'): tn = p.split('"')[1]
            if eid and fn: edge_nodes.append({"id": eid, "from": fn, "to": tn})

    hospitals = []
    used = set()

    for h_lat, h_lon, h_name in KNOWN_HOSPITALS[:NUM_HOSPITALS]:
        best_node = None
        best_d = float("inf")
        for n in all_nodes:
            d = haversine(h_lat, h_lon, n["lat"], n["lon"])
            if d < best_d:
                best_d = d
                best_node = n

        if not best_node or best_d > 500: continue

        nid = best_node["node_id"]
        found = None
        for en in edge_nodes:
            if en["id"] not in used and (en["from"] == nid or en.get("to") == nid):
                found = en["id"]
                break

        if found:
            used.add(found)
            hospitals.append({
                "id": len(hospitals), "name": h_name,
                "lat": best_node["lat"], "lon": best_node["lon"],
                "edge": found, "node": nid, "distance_m": round(best_d, 1),
            })
            print(f"  🏥 {h_name} → {found} ({best_d:.0f}m)")

    with open(os.path.join(OUT_DIR, "hospitals.json"), "w") as f:
        json.dump(hospitals, f, indent=2)
    print(f"\n  ✅ {len(hospitals)} hospitals mapped")
    return hospitals

# ============================================================================
# STEP 6: Hospital routes
# ============================================================================

def generate_hospital_routes(hospitals):
    """Generate validated routes between hospital pairs."""
    print("\n" + "=" * 70)
    print("PHASE 3 — STEP 6: Hospital-to-hospital routes")
    print("=" * 70)

    net_path = os.path.join(OUT_DIR, "andheri.net.xml")
    routes = []
    valid = failed = 0

    for i, src in enumerate(hospitals):
        for j, dst in enumerate(hospitals):
            if i == j: continue
            edges = _find_route(net_path, src["edge"], dst["edge"])
            if edges and len(edges) >= 2:
                routes.append({
                    "source_index": i, "dest_index": j,
                    "source_name": src["name"], "dest_name": dst["name"],
                    "route_edges": edges, "route_length": len(edges),
                    "distance_km": round(haversine(src["lat"], src["lon"], dst["lat"], dst["lon"]) / 1000, 2),
                })
                valid += 1
            else:
                failed += 1

    print(f"  ✅ Valid: {valid} | Failed: {failed}")

    data = {"total_hospitals": len(hospitals), "total_routes": len(routes),
            "assigned_routes": valid, "routes": routes}
    with open(os.path.join(OUT_DIR, "hospital_routes_validated.json"), "w") as f:
        json.dump(data, f, indent=2)
    return data


def _find_route(net_path, src, dst):
    tmp_t = os.path.join(OUT_DIR, "_tmp_trip.xml")
    tmp_r = os.path.join(OUT_DIR, "_tmp_route.xml")
    with open(tmp_t, "w") as f:
        f.write(f'<?xml version="1.0"?>\n<trips>\n'
                f'    <trip id="t" depart="0" from="{src}" to="{dst}"/>\n</trips>\n')
    try:
        subprocess.run(["duarouter", "-n", net_path, "-t", tmp_t, "-o", tmp_r,
                        "--ignore-errors", "true", "--no-warnings", "true"],
                       capture_output=True, text=True, cwd=OUT_DIR, timeout=30)
        if os.path.exists(tmp_r):
            with open(tmp_r) as f:
                for line in f:
                    if 'edges="' in line:
                        s = line.index('edges="') + 7
                        return line[s:line.index('"', s)].split()
    except: pass
    finally:
        for fp in [tmp_t, tmp_r]:
            try: os.remove(fp)
            except: pass
    return None

# ============================================================================
# STEP 7: SUMO config
# ============================================================================

def generate_config(routes_path):
    print("\n" + "=" * 70)
    print("PHASE 3 — STEP 7: SUMO configuration")
    print("=" * 70)

    # Vehicle types
    vt = os.path.join(OUT_DIR, "vehicle_types.xml")
    with open(vt, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n')
        f.write('    <vType id="car" accel="2.6" decel="4.5" sigma="0.5" length="4.5" maxSpeed="16.67" color="0.2,0.4,0.8"/>\n')
        f.write('    <vType id="ambulance" accel="3.5" decel="5.0" sigma="0.2" length="6.0" maxSpeed="25.0" color="1.0,0.0,0.0" vClass="emergency" guiShape="emergency"/>\n')
        f.write('</additional>\n')

    rb = os.path.basename(routes_path)

    # Headless config
    cfg = os.path.join(OUT_DIR, "simulation.sumocfg")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<configuration>\n')
        f.write(f'    <input>\n        <net-file value="andheri.net.xml"/>\n')
        f.write(f'        <route-files value="{rb}"/>\n')
        f.write(f'        <additional-files value="vehicle_types.xml,edge_speeds.xml"/>\n    </input>\n')
        f.write(f'    <time>\n        <begin value="0"/>\n        <end value="{SIMULATION_DURATION}"/>\n')
        f.write(f'        <step-length value="1"/>\n    </time>\n')
        f.write(f'    <processing>\n        <time-to-teleport value="300"/>\n    </processing>\n')
        f.write(f'    <report>\n        <no-warnings value="true"/>\n        <no-step-log value="true"/>\n    </report>\n')
        f.write(f'</configuration>\n')

    # GUI config
    gcfg = os.path.join(OUT_DIR, "view_network.sumocfg")
    with open(gcfg, "w", encoding="utf-8") as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<configuration>\n')
        f.write(f'    <input>\n        <net-file value="andheri.net.xml"/>\n')
        f.write(f'        <route-files value="{rb}"/>\n')
        f.write(f'        <additional-files value="vehicle_types.xml,edge_speeds.xml"/>\n    </input>\n')
        f.write(f'    <time>\n        <begin value="0"/>\n        <end value="{SIMULATION_DURATION}"/>\n    </time>\n')
        f.write(f'    <gui_only>\n        <start value="true"/>\n    </gui_only>\n')
        f.write(f'</configuration>\n')

    print(f"  💾 simulation.sumocfg")
    print(f"  💾 view_network.sumocfg")

# ============================================================================
# STEP 8: Validate
# ============================================================================

def validate(api_calls):
    print("\n" + "=" * 70)
    print("PHASE 3 — FINAL VALIDATION")
    print("=" * 70)

    required = [
        "andheri.net.xml", "hospitals.json", "hospital_routes_validated.json",
        "routes.rou.xml", "vehicle_types.xml", "edge_speeds.xml",
        "simulation.sumocfg", "tomtom_traffic_flow.json", "edge_traffic_data.json",
    ]

    ok = True
    for fname in required:
        fpath = os.path.join(OUT_DIR, fname)
        if os.path.exists(fpath):
            print(f"  ✅ {fname:45s} {os.path.getsize(fpath)/1024:>8.1f} KB")
        else:
            print(f"  ❌ {fname:45s} MISSING")
            ok = False

    print(f"\n  📞 API calls this run: {api_calls}")
    print(f"     Future runs: 0 (cached!)")

    if ok:
        print(f"\n  🖥️  Test: sumo-gui -c sumo_data/view_network.sumocfg")
        print(f"  🔄 Refresh data: python phase3_generate_traffic.py --refresh")

# ============================================================================
# MAIN
# ============================================================================

def main():
    print("\n" + "=" * 70)
    print("🚗 PHASE 3: REALISTIC TRAFFIC FROM TOMTOM (SMART CACHE)")
    print(f"   Mode: {'🔄 REFRESH (fetching fresh)' if FORCE_REFRESH else '📦 CACHE (0 API calls if cached)'}")
    print(f"   Area: Andheri, Mumbai")
    print(f"   Vehicles: {BASE_VEHICLES} base (congestion-adjusted)")
    print("=" * 70)

    start = time.time()
    key_mgr = APIKeyManager(API_KEYS)

    # Steps 1-2: Get traffic data (cached or fresh)
    flow_data = get_traffic_flow(key_mgr)
    incidents = get_incidents(key_mgr)

    # Save cache metadata
    save_cache_meta(len(flow_data), len(incidents), key_mgr.total_calls())

    # Steps 3-7: Process (always runs, no API calls)
    edge_traffic, edges = map_flow_to_edges(flow_data, incidents)
    routes_path, num_vehicles = generate_traffic_demand(edge_traffic)
    hospitals = map_hospitals()
    if len(hospitals) >= 2:
        generate_hospital_routes(hospitals)
    generate_config(routes_path)

    # Validate
    validate(key_mgr.total_calls())

    elapsed = time.time() - start
    print(f"\n⏱️  Phase 3 completed in {elapsed:.1f}s")
    print(f"👉 Next: Build backend + frontend")

if __name__ == "__main__":
    main()