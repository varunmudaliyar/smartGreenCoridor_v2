#!/usr/bin/env python3
"""
Phase 1: Extract DENSE Road Network from TomTom Routing API
============================================================
Strategy:
  - Create a dense grid of points across Andheri bounding box
  - Compute routes between all neighboring grid points using TomTom Routing API
  - Each route returns detailed road geometry (every turn, every street)
  - Combine ALL route geometries into a unified road network
  - Identify intersections where roads cross/meet
  - Extract traffic signal locations from OSM-tagged data in the route responses
  - Generate SUMO .nod.xml + .edg.xml → netconvert → .net.xml

Features:
  ✅ Checkpoint/resume — saves after every batch
  ✅ API key rotation
  ✅ MUCH denser than Flow API (~500-1000 road segments expected)

Usage:
  python phase1_extract_roads.py              # Start or resume
  python phase1_extract_roads.py --reset      # Fresh start
"""

import os
import sys
import json
import math
import time
import hashlib
import subprocess
import requests
from collections import defaultdict
from itertools import combinations

# ============================================================================
# CONFIGURATION
# ============================================================================

API_KEYS = [
    "Miz071oJZwwNh7y9EEvGWHnLdU44uZw4",
    "3yQ8eHK7zA1jO96iAfi6biOGYHuEttC2",
]

# Andheri bounding box (same as before)
BBOX = {
    "north": 19.1350,
    "south": 19.1000,
    "west": 72.8230,
    "east": 72.8700,
}

# Grid for route origin/destination points
# Denser grid = more routes = more streets discovered
GRID_LAT_STEP = 0.0025   # ~278m between grid points
GRID_LON_STEP = 0.0030   # ~280m between grid points

# We'll also add diagonal routes for better coverage
# and boundary-to-boundary routes

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUT_DIR = os.path.join(PROJECT_DIR, "sumo_data")
CHECKPOINT_FILE = os.path.join(OUT_DIR, "_checkpoint_phase1_routing.json")
RAW_ROUTES_FILE = os.path.join(OUT_DIR, "tomtom_routes.json")

# TomTom Routing API
ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute"

# Topology building
NODE_SNAP_THRESHOLD = 12.0   # meters — points closer than this merge

# Checkpoint
CHECKPOINT_EVERY = 10  # Save after every N routes

# ============================================================================
# API KEY MANAGER
# ============================================================================

class APIKeyManager:
    def __init__(self, keys):
        self.keys = list(keys)
        self.current_idx = 0
        self.consecutive_fails = 0
        self.total_calls = 0

    @property
    def current_key(self):
        return self.keys[self.current_idx]

    def report_success(self):
        self.consecutive_fails = 0
        self.total_calls += 1

    def report_failure(self):
        self.consecutive_fails += 1
        if self.consecutive_fails >= 8:
            return self._rotate()
        return True

    def _rotate(self):
        old = self.current_key
        self.current_idx = (self.current_idx + 1) % len(self.keys)
        self.consecutive_fails = 0
        if self.current_key == old:
            print(f"\n  ❌ ALL API KEYS EXHAUSTED after {self.total_calls} calls!")
            return False
        print(f"\n  🔄 Key rotation: ...{old[-4:]} → ...{self.current_key[-4:]}")
        return True

    def status(self):
        return f"Key ...{self.current_key[-4:]} | {self.total_calls} calls"

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


def route_key(origin, dest):
    """Unique key for a route pair."""
    return f"{origin[0]:.5f},{origin[1]:.5f}_{dest[0]:.5f},{dest[1]:.5f}"

# ============================================================================
# STEP 1: Generate route pairs
# ============================================================================

def generate_route_pairs():
    """
    Create origin-destination pairs that will trace out all roads.

    Strategy:
    1. Dense grid points
    2. Connect each point to its neighbors (N, S, E, W, NE, NW, SE, SW)
    3. Add long diagonal routes across the entire area
    4. Add boundary-to-boundary routes
    """
    print("  📐 Generating route pairs...")

    # Build grid
    grid_points = []
    lat = BBOX["south"]
    while lat <= BBOX["north"]:
        lon = BBOX["west"]
        while lon <= BBOX["east"]:
            grid_points.append((round(lat, 6), round(lon, 6)))
            lon += GRID_LON_STEP
        lat += GRID_LAT_STEP

    print(f"     Grid: {len(grid_points)} points "
          f"({GRID_LAT_STEP}° × {GRID_LON_STEP}°)")

    # Organize grid into rows/cols for neighbor lookup
    rows = defaultdict(list)
    for lat, lon in grid_points:
        row_key = round(lat / GRID_LAT_STEP)
        rows[row_key].append((lat, lon))

    # Sort each row by longitude
    for key in rows:
        rows[key].sort(key=lambda p: p[1])

    # --- Strategy 1: Neighbor routes (horizontal + vertical + diagonal) ---
    pairs = set()
    sorted_keys = sorted(rows.keys())

    for ri, row_key in enumerate(sorted_keys):
        row = rows[row_key]

        # Horizontal: each point to its east neighbor
        for ci in range(len(row) - 1):
            pairs.add((row[ci], row[ci + 1]))
            # Skip one for longer routes too
            if ci + 2 < len(row):
                pairs.add((row[ci], row[ci + 2]))

        # Vertical + diagonal: connect to next row
        if ri + 1 < len(sorted_keys):
            next_row = rows[sorted_keys[ri + 1]]
            for p1 in row:
                # Find closest point in next row
                for p2 in next_row:
                    d = haversine(p1[0], p1[1], p2[0], p2[1])
                    if d < 800:  # Within ~800m
                        pairs.add((p1, p2))

        # Skip-row connections for longer vertical routes
        if ri + 2 < len(sorted_keys):
            skip_row = rows[sorted_keys[ri + 2]]
            for ci, p1 in enumerate(row):
                if ci < len(skip_row):
                    pairs.add((p1, skip_row[ci]))

    # --- Strategy 2: Long cross-area routes ---
    # North-South corridors
    north_points = [p for p in grid_points if p[0] > BBOX["north"] - 0.005]
    south_points = [p for p in grid_points if p[0] < BBOX["south"] + 0.005]
    for np in north_points[:5]:
        for sp in south_points[:5]:
            pairs.add((np, sp))

    # East-West corridors
    east_points = [p for p in grid_points if p[1] > BBOX["east"] - 0.005]
    west_points = [p for p in grid_points if p[1] < BBOX["west"] + 0.005]
    for ep in east_points[:5]:
        for wp in west_points[:5]:
            pairs.add((ep, wp))

    # --- Strategy 3: Diagonal routes ---
    corners = [
        (BBOX["south"], BBOX["west"]),
        (BBOX["south"], BBOX["east"]),
        (BBOX["north"], BBOX["west"]),
        (BBOX["north"], BBOX["east"]),
    ]
    center = ((BBOX["south"] + BBOX["north"]) / 2,
              (BBOX["west"] + BBOX["east"]) / 2)

    for c in corners:
        pairs.add((c, center))
        pairs.add((center, c))

    for c1, c2 in combinations(corners, 2):
        pairs.add((c1, c2))

    # --- Strategy 4: Extra dense routes through known busy areas ---
    # Andheri station area, SV Road, Link Road, Western Express Highway
    key_locations = [
        (19.1190, 72.8460),  # Andheri West station
        (19.1090, 72.8370),  # SV Road / DN Nagar
        (19.1160, 72.8300),  # Link Road
        (19.1270, 72.8400),  # Lokhandwala
        (19.1080, 72.8540),  # WEH / MIDC
        (19.1250, 72.8550),  # Oshiwara
        (19.1100, 72.8300),  # Juhu Lane
        (19.1150, 72.8430),  # DB Marg area
        (19.1200, 72.8350),  # 4 Bungalows
        (19.1050, 72.8450),  # Azad Nagar
        (19.1300, 72.8500),  # Jogeshwari
        (19.1180, 72.8590),  # Marol
        (19.1120, 72.8480),  # Veera Desai
        (19.1040, 72.8300),  # Juhu south
        (19.1060, 72.8580),  # SEEPZ
    ]

    for i, loc1 in enumerate(key_locations):
        for loc2 in key_locations[i + 1:]:
            pairs.add((loc1, loc2))
            pairs.add((loc2, loc1))

        # Also connect key locations to nearby grid points
        for gp in grid_points:
            if haversine(loc1[0], loc1[1], gp[0], gp[1]) < 600:
                pairs.add((loc1, gp))
                pairs.add((gp, loc1))

    pairs_list = list(pairs)
    print(f"     Total route pairs: {len(pairs_list)}")
    return pairs_list

# ============================================================================
# STEP 2: Fetch routes from TomTom Routing API
# ============================================================================

def fetch_route(origin, dest, api_key):
    """
    Fetch a route from TomTom Routing API.
    Returns list of coordinate points along the route.
    """
    o_str = f"{origin[0]:.6f},{origin[1]:.6f}"
    d_str = f"{dest[0]:.6f},{dest[1]:.6f}"

    url = f"{ROUTING_URL}/{o_str}:{d_str}/json"
    params = {
        "key": api_key,
        "routeType": "fastest",
        "traffic": "true",
        "travelMode": "car",
        "routeRepresentation": "polyline",
    }

    try:
        resp = requests.get(url, params=params, timeout=15)

        if resp.status_code in (401, 403):
            return None, "auth_fail"
        if resp.status_code == 429:
            time.sleep(1)
            return None, "rate_limit"
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"

        data = resp.json()
        routes = data.get("routes", [])
        if not routes:
            return None, "no_route"

        route = routes[0]
        legs = route.get("legs", [])

        # Extract all coordinate points from all legs
        all_points = []
        for leg in legs:
            points = leg.get("points", [])
            for p in points:
                all_points.append({
                    "lat": p["latitude"],
                    "lon": p["longitude"],
                })

        if len(all_points) < 2:
            return None, "no_points"

        # Also extract route summary for metadata
        summary = route.get("summary", {})

        return {
            "origin": {"lat": origin[0], "lon": origin[1]},
            "dest": {"lat": dest[0], "lon": dest[1]},
            "points": all_points,
            "lengthInMeters": summary.get("lengthInMeters", 0),
            "travelTimeInSeconds": summary.get("travelTimeInSeconds", 0),
            "trafficDelayInSeconds": summary.get("trafficDelayInSeconds", 0),
        }, "ok"

    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ConnectionError:
        return None, "connection"
    except Exception as e:
        return None, f"error:{e}"


def fetch_all_routes(pairs):
    """Fetch all routes with checkpoint/resume."""
    print("\n" + "=" * 70)
    print("PHASE 1 — STEP 2: Fetching routes from TomTom Routing API")
    print("=" * 70)

    key_mgr = APIKeyManager(API_KEYS)
    os.makedirs(OUT_DIR, exist_ok=True)

    # --- Check for checkpoint ---
    routes = []
    completed_keys = set()
    start_idx = 0
    failed = 0

    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                cp = json.load(f)
            routes = cp.get("routes", [])
            completed_keys = set(cp.get("completed_keys", []))
            failed = cp.get("failed", 0)
            print(f"  📂 Resumed from checkpoint: {len(routes)} routes, "
                  f"{len(completed_keys)} pairs done, {failed} failed")
        except Exception:
            pass

    # Filter out already-completed pairs
    remaining = []
    for p in pairs:
        k = route_key(p[0], p[1])
        if k not in completed_keys:
            remaining.append(p)

    total_pairs = len(pairs)
    already_done = total_pairs - len(remaining)
    print(f"  📊 Total pairs: {total_pairs} | Done: {already_done} | Remaining: {len(remaining)}")
    print(f"  🔑 {key_mgr.status()}")

    if not remaining:
        print("  ✅ All routes already fetched!")
        return routes

    # --- Test API key ---
    test_origin, test_dest = remaining[0]
    test_result, test_status = fetch_route(test_origin, test_dest, key_mgr.current_key)
    if test_status == "auth_fail":
        print(f"  ⚠️  Key ...{key_mgr.current_key[-4:]} failed, rotating...")
        if not key_mgr.report_failure():
            _save_checkpoint(routes, completed_keys, failed)
            return routes
        test_result, test_status = fetch_route(test_origin, test_dest, key_mgr.current_key)

    if test_result:
        print(f"  ✅ API works! First route: {len(test_result['points'])} points, "
              f"{test_result['lengthInMeters']}m")
        routes.append(test_result)
        completed_keys.add(route_key(test_origin, test_dest))
        key_mgr.report_success()
        remaining = remaining[1:]
    else:
        print(f"  ⚠️  First test: {test_status} (continuing...)")

    # --- Main fetch loop ---
    for idx, (origin, dest) in enumerate(remaining):
        rk = route_key(origin, dest)

        result, status = fetch_route(origin, dest, key_mgr.current_key)

        if status == "ok" and result:
            routes.append(result)
            completed_keys.add(rk)
            key_mgr.report_success()

        elif status in ("auth_fail", "rate_limit"):
            if not key_mgr.report_failure():
                print(f"\n  ⏸️  All keys exhausted at {already_done + idx}/{total_pairs}")
                _save_checkpoint(routes, completed_keys, failed)
                print(f"  💾 Progress saved! Add new API key and re-run.")
                return routes

            # Retry with new key
            result2, status2 = fetch_route(origin, dest, key_mgr.current_key)
            if status2 == "ok" and result2:
                routes.append(result2)
                completed_keys.add(rk)
                key_mgr.report_success()
            else:
                completed_keys.add(rk)  # Skip to avoid infinite retry
                failed += 1
        else:
            completed_keys.add(rk)
            failed += 1

        # Rate limiting
        if idx % 5 == 0:
            time.sleep(0.05)

        # Checkpoint
        if idx % CHECKPOINT_EVERY == 0 and idx > 0:
            _save_checkpoint(routes, completed_keys, failed)

        # Progress
        done_now = already_done + idx + 1
        if idx % 50 == 0 and idx > 0:
            total_points = sum(len(r["points"]) for r in routes)
            print(f"  ... {done_now}/{total_pairs} → {len(routes)} routes, "
                  f"{total_points} total points, {failed} failed "
                  f"[{key_mgr.status()}]")

    # --- Done ---
    total_points = sum(len(r["points"]) for r in routes)
    print(f"\n  ✅ Fetched {len(routes)} routes with {total_points} total coordinate points")
    print(f"  ⚠️  Failed: {failed}")

    # Save final
    with open(RAW_ROUTES_FILE, "w") as f:
        json.dump(routes, f)
    print(f"  💾 Saved: {RAW_ROUTES_FILE}")

    # Clean up checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)

    return routes


def _save_checkpoint(routes, completed_keys, failed):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({
            "routes": routes,
            "completed_keys": list(completed_keys),
            "failed": failed,
            "timestamp": time.time(),
        }, f)

# ============================================================================
# STEP 3: Build network topology from routes
# ============================================================================

def build_topology(routes):
    """
    Convert route geometries into SUMO nodes and edges.

    Strategy:
    1. Collect ALL coordinate points from all routes
    2. Find clusters of points (intersections) — where multiple routes
       pass through the same area
    3. Between intersections, the route segments become edges
    4. Edge shape comes from the intermediate coordinates

    This is the key innovation: by overlaying hundreds of routes,
    intersections naturally emerge where routes converge.
    """
    print("\n" + "=" * 70)
    print("PHASE 1 — STEP 3: Building dense road network topology")
    print("=" * 70)

    # --- Step A: Split each route into road segments ---
    # A "segment" is a straight-ish piece of road between two turns
    # We detect turns by looking at angle changes

    MIN_SEGMENT_LENGTH = 20.0    # meters — minimum edge length
    ANGLE_THRESHOLD = 25.0       # degrees — turn detection

    all_segments = []

    for route in routes:
        pts = route["points"]
        if len(pts) < 2:
            continue

        # Walk along the route, break at significant turns
        current_seg = [pts[0]]

        for i in range(1, len(pts)):
            current_seg.append(pts[i])

            if i < len(pts) - 1:
                # Check angle at this point
                if len(current_seg) >= 2:
                    # Vector from prev to current
                    v1_lat = pts[i]["lat"] - pts[i - 1]["lat"]
                    v1_lon = pts[i]["lon"] - pts[i - 1]["lon"]
                    # Vector from current to next
                    v2_lat = pts[i + 1]["lat"] - pts[i]["lat"]
                    v2_lon = pts[i + 1]["lon"] - pts[i]["lon"]

                    # Angle between vectors
                    dot = v1_lat * v2_lat + v1_lon * v2_lon
                    mag1 = math.sqrt(v1_lat ** 2 + v1_lon ** 2)
                    mag2 = math.sqrt(v2_lat ** 2 + v2_lon ** 2)

                    if mag1 > 0 and mag2 > 0:
                        cos_angle = max(-1, min(1, dot / (mag1 * mag2)))
                        angle = math.degrees(math.acos(cos_angle))

                        if angle > ANGLE_THRESHOLD:
                            # Significant turn — break segment here
                            seg_len = sum(
                                haversine(
                                    current_seg[j]["lat"], current_seg[j]["lon"],
                                    current_seg[j + 1]["lat"], current_seg[j + 1]["lon"]
                                )
                                for j in range(len(current_seg) - 1)
                            )
                            if seg_len >= MIN_SEGMENT_LENGTH:
                                all_segments.append(current_seg[:])
                            # Start new segment from this turn point
                            current_seg = [pts[i]]

        # Don't forget the last segment
        if len(current_seg) >= 2:
            seg_len = sum(
                haversine(
                    current_seg[j]["lat"], current_seg[j]["lon"],
                    current_seg[j + 1]["lat"], current_seg[j + 1]["lon"]
                )
                for j in range(len(current_seg) - 1)
            )
            if seg_len >= MIN_SEGMENT_LENGTH:
                all_segments.append(current_seg)

    print(f"  📏 Raw road segments from routes: {len(all_segments)}")

    # --- Step B: Create nodes at segment endpoints, snap nearby ones ---
    nodes = {}
    node_counter = [0]

    def get_or_create_node(lat, lon):
        for nid, (nlat, nlon) in nodes.items():
            if haversine(lat, lon, nlat, nlon) <= NODE_SNAP_THRESHOLD:
                return nid
        node_counter[0] += 1
        nid = f"n{node_counter[0]}"
        nodes[nid] = (lat, lon)
        return nid

    # --- Step C: Build edges ---
    edges = []
    edge_counter = [0]
    seen_pairs = set()

    for seg in all_segments:
        from_node = get_or_create_node(seg[0]["lat"], seg[0]["lon"])
        to_node = get_or_create_node(seg[-1]["lat"], seg[-1]["lon"])

        if from_node == to_node:
            continue

        # Deduplicate
        pair = (from_node, to_node)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        # Shape from intermediate points
        shape_parts = []
        for p in seg[1:-1]:
            shape_parts.append(f"{p['lon']:.7f},{p['lat']:.7f}")
        shape_str = " ".join(shape_parts) if shape_parts else ""

        # Estimate speed from segment length and route data
        seg_len = sum(
            haversine(seg[j]["lat"], seg[j]["lon"],
                      seg[j + 1]["lat"], seg[j + 1]["lon"])
            for j in range(len(seg) - 1)
        )

        # Default speed based on segment length (longer = likely arterial)
        if seg_len > 500:
            speed = 16.67  # 60 km/h — arterial
            lanes = 2
            priority = 11
        elif seg_len > 200:
            speed = 11.11  # 40 km/h — collector
            lanes = 2
            priority = 8
        elif seg_len > 80:
            speed = 8.33   # 30 km/h — local
            lanes = 1
            priority = 5
        else:
            speed = 5.56   # 20 km/h — residential
            lanes = 1
            priority = 3

        edge_counter[0] += 1
        edges.append({
            "id": f"e{edge_counter[0]}",
            "from": from_node,
            "to": to_node,
            "priority": priority,
            "numLanes": lanes,
            "speed": round(speed, 2),
            "shape": shape_str,
            "length": round(seg_len, 1),
        })

        # Reverse edge (bidirectional)
        rev_pair = (to_node, from_node)
        if rev_pair not in seen_pairs:
            seen_pairs.add(rev_pair)
            rev_shape = []
            for p in reversed(seg[1:-1]):
                rev_shape.append(f"{p['lon']:.7f},{p['lat']:.7f}")
            rev_shape_str = " ".join(rev_shape) if rev_shape else ""

            edge_counter[0] += 1
            edges.append({
                "id": f"e{edge_counter[0]}",
                "from": to_node,
                "to": from_node,
                "priority": priority,
                "numLanes": lanes,
                "speed": round(speed, 2),
                "shape": rev_shape_str,
                "length": round(seg_len, 1),
            })

    print(f"  🔵 Nodes (intersections): {len(nodes)}")
    print(f"  🔗 Edges (road segments):  {len(edges)}")

    # --- Step D: Identify traffic signal locations ---
    # Nodes with high degree (many roads meeting) are signal candidates
    node_degree = defaultdict(int)
    node_max_priority = defaultdict(int)
    for edge in edges:
        node_degree[edge["from"]] += 1
        node_degree[edge["to"]] += 1
        node_max_priority[edge["from"]] = max(
            node_max_priority[edge["from"]], edge["priority"])
        node_max_priority[edge["to"]] = max(
            node_max_priority[edge["to"]], edge["priority"])

    signal_candidates = set()
    for nid in nodes:
        deg = node_degree.get(nid, 0)
        pri = node_max_priority.get(nid, 0)
        if deg >= 6 and pri >= 5:
            signal_candidates.add(nid)
        elif deg >= 4 and pri >= 8:
            signal_candidates.add(nid)

    print(f"  🚦 Signal-candidate nodes: {len(signal_candidates)}")

    # Degree distribution
    deg_dist = defaultdict(int)
    for d in node_degree.values():
        deg_dist[d] += 1
    print(f"  📊 Degree distribution: {dict(sorted(deg_dist.items()))}")

    return nodes, edges, signal_candidates

# ============================================================================
# STEP 4: Write SUMO files + netconvert
# ============================================================================

def write_sumo_files(nodes, edges, signal_candidates):
    print("\n" + "=" * 70)
    print("PHASE 1 — STEP 4: Writing SUMO network files")
    print("=" * 70)

    os.makedirs(OUT_DIR, exist_ok=True)

    # .nod.xml
    nod_path = os.path.join(OUT_DIR, "andheri.nod.xml")
    with open(nod_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<nodes>\n')
        for nid, (lat, lon) in nodes.items():
            ntype = "traffic_light" if nid in signal_candidates else "priority"
            f.write(f'    <node id="{nid}" x="{lon:.7f}" y="{lat:.7f}" type="{ntype}"/>\n')
        f.write('</nodes>\n')
    print(f"  💾 Nodes: {nod_path} ({len(nodes)} nodes, "
          f"{len(signal_candidates)} signals)")

    # .edg.xml
    edg_path = os.path.join(OUT_DIR, "andheri.edg.xml")
    with open(edg_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<edges>\n')
        for edge in edges:
            shape_attr = f' shape="{edge["shape"]}"' if edge["shape"] else ""
            f.write(f'    <edge id="{edge["id"]}" '
                    f'from="{edge["from"]}" to="{edge["to"]}" '
                    f'priority="{edge["priority"]}" '
                    f'numLanes="{edge["numLanes"]}" '
                    f'speed="{edge["speed"]}"'
                    f'{shape_attr}/>\n')
        f.write('</edges>\n')
    print(f"  💾 Edges: {edg_path} ({len(edges)} edges)")

    # .netccfg
    cfg_path = os.path.join(OUT_DIR, "andheri.netccfg")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<configuration>\n')
        f.write('    <input>\n')
        f.write('        <node-files value="andheri.nod.xml"/>\n')
        f.write('        <edge-files value="andheri.edg.xml"/>\n')
        f.write('    </input>\n')
        f.write('    <output>\n')
        f.write('        <output-file value="andheri.net.xml"/>\n')
        f.write('    </output>\n')
        f.write('    <processing>\n')
        f.write('        <geometry.remove value="false"/>\n')
        f.write('        <roundabouts.guess value="true"/>\n')
        f.write('        <junctions.join value="true"/>\n')
        f.write('        <junctions.join-dist value="12"/>\n')
        f.write('        <junctions.corner-detail value="5"/>\n')
        f.write('        <no-turnarounds value="true"/>\n')
        f.write('    </processing>\n')
        f.write('    <projection>\n')
        f.write('        <proj value="+proj=utm +zone=43 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>\n')
        f.write('    </projection>\n')
        f.write('    <tls_building>\n')
        f.write('        <tls.default-type value="actuated"/>\n')
        f.write('    </tls_building>\n')
        f.write('    <report>\n')
        f.write('        <no-warnings value="true"/>\n')
        f.write('    </report>\n')
        f.write('</configuration>\n')
    print(f"  💾 Config: {cfg_path}")

    # Save metadata
    nodes_path = os.path.join(OUT_DIR, "all_nodes.json")
    with open(nodes_path, "w") as f:
        json.dump([
            {"node_id": nid, "lat": lat, "lon": lon}
            for nid, (lat, lon) in nodes.items()
        ], f, indent=2)

    signals_path = os.path.join(OUT_DIR, "signal_candidate_nodes.json")
    with open(signals_path, "w") as f:
        json.dump([
            {"node_id": nid, "lat": nodes[nid][0], "lon": nodes[nid][1]}
            for nid in signal_candidates
        ], f, indent=2)

    signal_ids_path = os.path.join(OUT_DIR, "signal_junction_ids.json")
    with open(signal_ids_path, "w") as f:
        json.dump(list(signal_candidates), f, indent=2)

    # SUMO view config
    view_path = os.path.join(OUT_DIR, "view_network.sumocfg")
    with open(view_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<configuration>\n')
        f.write('    <input>\n')
        f.write('        <net-file value="andheri.net.xml"/>\n')
        f.write('    </input>\n')
        f.write('    <time>\n')
        f.write('        <begin value="0"/>\n')
        f.write('        <end value="100"/>\n')
        f.write('    </time>\n')
        f.write('</configuration>\n')

    # Run netconvert
    print(f"\n  🔧 Running netconvert...")
    net_path = os.path.join(OUT_DIR, "andheri.net.xml")

    try:
        result = subprocess.run(
            ["netconvert", "-c", "andheri.netccfg"],
            capture_output=True, text=True, cwd=OUT_DIR, timeout=180
        )
        if result.returncode == 0 and os.path.exists(net_path):
            net_size = os.path.getsize(net_path)
            print(f"  ✅ Network generated: andheri.net.xml ({net_size / 1024:.1f} KB)")
        else:
            print(f"  ⚠️  Config failed, trying direct CLI...")
            if result.stderr:
                print(f"     Error: {result.stderr[:300]}")
            result2 = subprocess.run([
                "netconvert",
                "--node-files", "andheri.nod.xml",
                "--edge-files", "andheri.edg.xml",
                "--output-file", "andheri.net.xml",
                "--proj", "+proj=utm +zone=43 +ellps=WGS84 +datum=WGS84 +units=m +no_defs",
                "--junctions.join", "true",
                "--junctions.join-dist", "12",
                "--junctions.corner-detail", "5",
                "--no-turnarounds", "true",
                "--roundabouts.guess", "true",
                "--tls.default-type", "actuated",
                "--no-warnings", "true",
            ], capture_output=True, text=True, cwd=OUT_DIR, timeout=180)
            if result2.returncode == 0 and os.path.exists(net_path):
                net_size = os.path.getsize(net_path)
                print(f"  ✅ Network (fallback): {net_size / 1024:.1f} KB")
            else:
                print(f"  ❌ netconvert failed!")
                if result2.stderr:
                    print(f"     {result2.stderr[:500]}")
                sys.exit(1)
    except FileNotFoundError:
        print("  ❌ 'netconvert' not found!")
        sys.exit(1)

    return net_path

# ============================================================================
# STEP 5: Validation
# ============================================================================

def validate():
    print("\n" + "=" * 70)
    print("PHASE 1 — STEP 5: Validation")
    print("=" * 70)

    net_path = os.path.join(OUT_DIR, "andheri.net.xml")
    if not os.path.exists(net_path):
        print("  ❌ Network not found!")
        return False

    size_kb = os.path.getsize(net_path) / 1024

    edge_count = 0
    junction_count = 0
    tls_count = 0

    with open(net_path, "r", encoding="utf-8") as f:
        for line in f:
            if "<edge " in line and 'function="internal"' not in line:
                edge_count += 1
            if "<junction " in line and 'type="internal"' not in line:
                junction_count += 1
            if "<tlLogic " in line:
                tls_count += 1

    print(f"  📁 Size: {size_kb:.1f} KB")
    print(f"  🔗 Edges: {edge_count}")
    print(f"  🔵 Junctions: {junction_count}")
    print(f"  🚦 Traffic lights: {tls_count}")

    if edge_count > 200:
        print(f"\n  ✅ Dense network! Ready for Phase 2.")
    elif edge_count > 50:
        print(f"\n  ✅ Moderate network. Usable for simulation.")
    else:
        print(f"\n  ⚠️  Sparse network. May need more routes.")

    print(f"\n  🖥️  View in SUMO: sumo-gui -c {os.path.join(OUT_DIR, 'view_network.sumocfg')}")
    return True

# ============================================================================
# MAIN
# ============================================================================

def main():
    if "--reset" in sys.argv:
        for f in [CHECKPOINT_FILE, RAW_ROUTES_FILE]:
            if os.path.exists(f):
                os.remove(f)
                print(f"  🗑️  Deleted {os.path.basename(f)}")

    print("\n" + "=" * 70)
    print("🚀 PHASE 1: EXTRACT DENSE ROAD NETWORK VIA TOMTOM ROUTING API")
    print("   Area: Andheri, Mumbai")
    print(f"   API Keys: {len(API_KEYS)} available")
    print("   Method: Route-based network reconstruction")
    print("=" * 70)

    start = time.time()

    # Step 1: Generate route pairs
    pairs = generate_route_pairs()

    # Step 2: Fetch routes
    routes = fetch_all_routes(pairs)

    if len(routes) < 5:
        print("\n  ❌ Too few routes fetched.")
        sys.exit(1)

    # Step 3: Build topology
    nodes, edges, signals = build_topology(routes)

    if len(edges) < 10:
        print("\n  ❌ Too few edges.")
        sys.exit(1)

    # Step 4: Write SUMO files
    write_sumo_files(nodes, edges, signals)

    # Step 5: Validate
    validate()

    elapsed = time.time() - start
    print(f"\n⏱️  Phase 1 completed in {elapsed:.1f}s")
    print(f"📂 Files in: {os.path.abspath(OUT_DIR)}")
    print(f"\n👉 Next: Open sumo-gui to check the network, then run Phase 2")


if __name__ == "__main__":
    main()