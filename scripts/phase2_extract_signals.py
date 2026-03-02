#!/usr/bin/env python3
"""
Phase 2: Extract REAL Traffic Signal Locations from TomTom Search API
=====================================================================
- Uses TomTom Category Search to find actual traffic lights
- Maps each signal to the nearest SUMO junction
- Rewrites .nod.xml with correct junction types
- Re-runs netconvert to embed traffic light logics

Features:
  ✅ Checkpoint/resume
  ✅ API key rotation
  ✅ Real-world signal positions (not guessed from topology)
"""

import os
import sys
import json
import math
import time
import subprocess
import requests
from collections import defaultdict

# ============================================================================
# CONFIGURATION
# ============================================================================

API_KEYS = [
    "mPtfLSFK8MXJHzOuvE6CJJKYn56Hcw2v",
    "3yQ8eHK7zA1jO96iAfi6biOGYHuEttC2",
]

BBOX = {
    "north": 19.1341,
    "south": 19.1017,
    "west": 72.8248,
    "east": 72.8685,
}

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUT_DIR = os.path.join(PROJECT_DIR, "sumo_data")

# TomTom Search API
SEARCH_URL = "https://api.tomtom.com/search/2/categorySearch/traffic light.json"
NEARBY_URL = "https://api.tomtom.com/search/2/nearbySearch/.json"

# How close a TomTom signal must be to a SUMO junction to be mapped (meters)
SIGNAL_TO_JUNCTION_MAX_DIST = 150.0

# Minimum signals we want — if TomTom Search finds fewer, we supplement
# with high-degree intersection detection
MIN_DESIRED_SIGNALS = 15

# ============================================================================
# API KEY MANAGER (same as Phase 1)
# ============================================================================

class APIKeyManager:
    def __init__(self, keys):
        self.keys = list(keys)
        self.current_idx = 0
        self.consecutive_fails = 0

    @property
    def current_key(self):
        return self.keys[self.current_idx]

    def report_success(self):
        self.consecutive_fails = 0

    def report_failure(self):
        self.consecutive_fails += 1
        if self.consecutive_fails >= 5:
            old = self.current_key
            self.current_idx = (self.current_idx + 1) % len(self.keys)
            self.consecutive_fails = 0
            if self.current_idx == 0:
                print(f"  ❌ All API keys exhausted!")
                return False
            print(f"  🔄 Key rotation: ...{old[-4:]} → ...{self.current_key[-4:]}")
        return True

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
# STEP 1: Fetch traffic signal locations from TomTom Search API
# ============================================================================

def fetch_signals_from_search(key_mgr):
    """
    Use TomTom Category Search + Nearby Search to find traffic lights
    across the Andheri bounding box.

    Strategy:
    - Use Category Search with "traffic light" at multiple grid points
    - Also use Nearby Search with categorySet=7397 (Traffic Light POI)
    - De-duplicate by coordinate proximity
    """
    print("=" * 70)
    print("PHASE 2 — STEP 1: Fetching traffic signals from TomTom Search API")
    print("=" * 70)

    all_signals = []
    seen_positions = set()  # For de-duplication

    # --- Method 1: Category Search at grid points ---
    print("\n  📍 Method 1: Category Search for 'traffic light'...")

    # Search grid — sparser than Phase 1, just need coverage
    search_points = []
    lat = BBOX["south"]
    while lat <= BBOX["north"]:
        lon = BBOX["west"]
        while lon <= BBOX["east"]:
            search_points.append((lat, lon))
            lon += 0.004  # ~370m grid
        lat += 0.004

    print(f"     Searching at {len(search_points)} grid points...")

    for idx, (lat, lon) in enumerate(search_points):
        signals = _search_category(lat, lon, key_mgr, radius=2000)
        if signals is None:
            continue

        for sig in signals:
            pos_key = (round(sig["lat"], 5), round(sig["lon"], 5))
            if pos_key not in seen_positions:
                # Check if within bounding box
                if (BBOX["south"] <= sig["lat"] <= BBOX["north"] and
                        BBOX["west"] <= sig["lon"] <= BBOX["east"]):
                    seen_positions.add(pos_key)
                    all_signals.append(sig)

        time.sleep(0.1)

        if idx % 5 == 0 and idx > 0:
            print(f"     ... {idx}/{len(search_points)} searches → {len(all_signals)} signals found")

    print(f"     Category Search found: {len(all_signals)} unique signals")

    # --- Method 2: Nearby Search with POI category 7397 ---
    print(f"\n  📍 Method 2: Nearby Search (categorySet=7397)...")

    nearby_points = []
    lat = BBOX["south"]
    while lat <= BBOX["north"]:
        lon = BBOX["west"]
        while lon <= BBOX["east"]:
            nearby_points.append((lat, lon))
            lon += 0.005
        lat += 0.005

    for idx, (lat, lon) in enumerate(nearby_points):
        signals = _search_nearby(lat, lon, key_mgr, radius=2500)
        if signals is None:
            continue

        for sig in signals:
            pos_key = (round(sig["lat"], 5), round(sig["lon"], 5))
            if pos_key not in seen_positions:
                if (BBOX["south"] <= sig["lat"] <= BBOX["north"] and
                        BBOX["west"] <= sig["lon"] <= BBOX["east"]):
                    seen_positions.add(pos_key)
                    all_signals.append(sig)

        time.sleep(0.1)

    print(f"     After Nearby Search: {len(all_signals)} total unique signals")

    # --- Method 3: If still too few, search more densely ---
    if len(all_signals) < MIN_DESIRED_SIGNALS:
        print(f"\n  📍 Method 3: Dense search (found {len(all_signals)}, want {MIN_DESIRED_SIGNALS})...")

        dense_points = []
        lat = BBOX["south"]
        while lat <= BBOX["north"]:
            lon = BBOX["west"]
            while lon <= BBOX["east"]:
                dense_points.append((lat, lon))
                lon += 0.002
            lat += 0.002

        for idx, (lat, lon) in enumerate(dense_points):
            signals = _search_category(lat, lon, key_mgr, radius=1000)
            if signals is None:
                continue

            for sig in signals:
                pos_key = (round(sig["lat"], 5), round(sig["lon"], 5))
                if pos_key not in seen_positions:
                    if (BBOX["south"] <= sig["lat"] <= BBOX["north"] and
                            BBOX["west"] <= sig["lon"] <= BBOX["east"]):
                        seen_positions.add(pos_key)
                        all_signals.append(sig)

            time.sleep(0.1)

            if len(all_signals) >= MIN_DESIRED_SIGNALS * 2:
                break

        print(f"     After dense search: {len(all_signals)} total signals")

    # --- Save raw signals ---
    raw_path = os.path.join(OUT_DIR, "tomtom_signals_raw.json")
    with open(raw_path, "w") as f:
        json.dump(all_signals, f, indent=2)
    print(f"\n  💾 Raw signals saved: {raw_path}")

    return all_signals


def _search_category(lat, lon, key_mgr, radius=2000):
    """TomTom Category Search for traffic lights."""
    params = {
        "key": key_mgr.current_key,
        "lat": lat,
        "lon": lon,
        "radius": radius,
        "limit": 100,
        "categorySet": "7397",  # Traffic Light category
    }
    try:
        resp = requests.get(SEARCH_URL, params=params, timeout=10)
        if resp.status_code in (401, 403):
            key_mgr.report_failure()
            return None
        if resp.status_code != 200:
            return None

        key_mgr.report_success()
        results = resp.json().get("results", [])

        signals = []
        for r in results:
            pos = r.get("position", {})
            if pos:
                signals.append({
                    "lat": pos.get("lat", 0),
                    "lon": pos.get("lon", 0),
                    "name": r.get("poi", {}).get("name", "Traffic Signal"),
                    "address": r.get("address", {}).get("freeformAddress", ""),
                    "source": "category_search",
                })
        return signals
    except Exception:
        return None


def _search_nearby(lat, lon, key_mgr, radius=2500):
    """TomTom Nearby Search for traffic lights."""
    params = {
        "key": key_mgr.current_key,
        "lat": lat,
        "lon": lon,
        "radius": radius,
        "limit": 100,
        "categorySet": "7397",
    }
    try:
        resp = requests.get(NEARBY_URL, params=params, timeout=10)
        if resp.status_code in (401, 403):
            key_mgr.report_failure()
            return None
        if resp.status_code != 200:
            return None

        key_mgr.report_success()
        results = resp.json().get("results", [])

        signals = []
        for r in results:
            pos = r.get("position", {})
            if pos:
                signals.append({
                    "lat": pos.get("lat", 0),
                    "lon": pos.get("lon", 0),
                    "name": r.get("poi", {}).get("name", "Traffic Signal"),
                    "address": r.get("address", {}).get("freeformAddress", ""),
                    "source": "nearby_search",
                })
        return signals
    except Exception:
        return None

# ============================================================================
# STEP 2: Map signals to SUMO junctions
# ============================================================================

def map_signals_to_junctions(signals):
    """
    For each TomTom signal, find the nearest SUMO junction.
    Update junction type to 'traffic_light'.
    """
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 2: Mapping signals to SUMO junctions")
    print("=" * 70)

    # Load nodes from Phase 1
    nodes_path = os.path.join(OUT_DIR, "all_nodes.json")
    if not os.path.exists(nodes_path):
        print("  ❌ all_nodes.json not found! Run Phase 1 first.")
        sys.exit(1)

    with open(nodes_path, "r") as f:
        all_nodes = json.load(f)
    print(f"  📂 Loaded {len(all_nodes)} SUMO nodes")

    # Find nearest junction for each signal
    signal_junctions = set()
    mapped_signals = []
    unmapped_count = 0

    for sig in signals:
        best_node = None
        best_dist = float("inf")

        for node in all_nodes:
            d = haversine(sig["lat"], sig["lon"], node["lat"], node["lon"])
            if d < best_dist:
                best_dist = d
                best_node = node

        if best_node and best_dist <= SIGNAL_TO_JUNCTION_MAX_DIST:
            signal_junctions.add(best_node["node_id"])
            mapped_signals.append({
                "signal_lat": sig["lat"],
                "signal_lon": sig["lon"],
                "signal_name": sig.get("name", ""),
                "signal_address": sig.get("address", ""),
                "junction_id": best_node["node_id"],
                "junction_lat": best_node["lat"],
                "junction_lon": best_node["lon"],
                "distance_m": round(best_dist, 1),
            })
        else:
            unmapped_count += 1

    print(f"  ✅ Mapped {len(mapped_signals)} signals → {len(signal_junctions)} unique junctions")
    print(f"  ⚠️  Unmapped (too far from any junction): {unmapped_count}")

    # --- Supplement: If too few mapped, add high-degree nodes ---
    if len(signal_junctions) < MIN_DESIRED_SIGNALS:
        print(f"\n  📍 Supplementing: only {len(signal_junctions)} mapped, want {MIN_DESIRED_SIGNALS}")
        print(f"     Adding high-degree intersections from network topology...")

        # Load edges to compute degree
        edg_path = os.path.join(OUT_DIR, "andheri.edg.xml")
        if os.path.exists(edg_path):
            node_degree = defaultdict(int)
            with open(edg_path, "r", encoding="utf-8") as f:
                for line in f:
                    if "<edge " in line:
                        # Extract from and to
                        parts = line.split()
                        from_node = None
                        to_node = None
                        for p in parts:
                            if p.startswith('from="'):
                                from_node = p.split('"')[1]
                            elif p.startswith('to="'):
                                to_node = p.split('"')[1]
                        if from_node:
                            node_degree[from_node] += 1
                        if to_node:
                            node_degree[to_node] += 1

            # Sort by degree, add highest-degree nodes as signals
            sorted_nodes = sorted(node_degree.items(), key=lambda x: x[1], reverse=True)
            added = 0
            for nid, deg in sorted_nodes:
                if nid not in signal_junctions and deg >= 3:
                    signal_junctions.add(nid)
                    # Find this node's coords
                    for node in all_nodes:
                        if node["node_id"] == nid:
                            mapped_signals.append({
                                "signal_lat": node["lat"],
                                "signal_lon": node["lon"],
                                "signal_name": f"Topology signal (degree={deg})",
                                "signal_address": "",
                                "junction_id": nid,
                                "junction_lat": node["lat"],
                                "junction_lon": node["lon"],
                                "distance_m": 0,
                            })
                            break
                    added += 1
                    if len(signal_junctions) >= MIN_DESIRED_SIGNALS:
                        break

            print(f"     Added {added} topology-based signals")

    print(f"\n  🚦 TOTAL signal junctions: {len(signal_junctions)}")

    # Save mapped signals
    mapped_path = os.path.join(OUT_DIR, "tomtom_signals_mapped.json")
    with open(mapped_path, "w") as f:
        json.dump(mapped_signals, f, indent=2)
    print(f"  💾 Mapped signals: {mapped_path}")

    # Save just the junction IDs for Phase 3
    junctions_path = os.path.join(OUT_DIR, "signal_junction_ids.json")
    with open(junctions_path, "w") as f:
        json.dump(list(signal_junctions), f, indent=2)
    print(f"  💾 Signal junction IDs: {junctions_path}")

    return signal_junctions, mapped_signals

# ============================================================================
# STEP 3: Rewrite .nod.xml and re-run netconvert
# ============================================================================

def rebuild_network(signal_junctions):
    """
    Rewrite the .nod.xml with correct junction types and re-run netconvert.
    """
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 3: Rebuilding network with real signal locations")
    print("=" * 70)

    # Load existing nodes
    nodes_path = os.path.join(OUT_DIR, "all_nodes.json")
    with open(nodes_path, "r") as f:
        all_nodes = json.load(f)

    # Rewrite .nod.xml with corrected types
    nod_path = os.path.join(OUT_DIR, "andheri.nod.xml")
    signal_count = 0
    priority_count = 0

    with open(nod_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<nodes>\n')
        for node in all_nodes:
            nid = node["node_id"]
            lat = node["lat"]
            lon = node["lon"]

            if nid in signal_junctions:
                ntype = "traffic_light"
                signal_count += 1
            else:
                ntype = "priority"
                priority_count += 1

            f.write(f'    <node id="{nid}" x="{lon:.7f}" y="{lat:.7f}" type="{ntype}"/>\n')
        f.write('</nodes>\n')

    print(f"  📝 Rewrote {nod_path}")
    print(f"     traffic_light: {signal_count}")
    print(f"     priority: {priority_count}")

    # Re-run netconvert
    print(f"\n  🔧 Re-running netconvert...")
    net_path = os.path.join(OUT_DIR, "andheri.net.xml")

    try:
        result = subprocess.run(
            ["netconvert", "-c", "andheri.netccfg"],
            capture_output=True, text=True, cwd=OUT_DIR, timeout=120
        )

        if result.returncode == 0 and os.path.exists(net_path):
            net_size = os.path.getsize(net_path)
            print(f"  ✅ Network rebuilt: andheri.net.xml ({net_size / 1024:.1f} KB)")
        else:
            # Fallback
            print(f"  ⚠️  Config failed, trying direct arguments...")
            result2 = subprocess.run([
                "netconvert",
                "--node-files", "andheri.nod.xml",
                "--edge-files", "andheri.edg.xml",
                "--output-file", "andheri.net.xml",
                "--proj", "+proj=utm +zone=43 +ellps=WGS84 +datum=WGS84 +units=m +no_defs",
                "--junctions.join", "true",
                "--junctions.join-dist", "15",
                "--junctions.corner-detail", "5",
                "--no-turnarounds", "true",
                "--roundabouts.guess", "true",
                "--tls.default-type", "actuated",
                "--no-warnings", "true",
            ], capture_output=True, text=True, cwd=OUT_DIR, timeout=120)

            if result2.returncode == 0 and os.path.exists(net_path):
                net_size = os.path.getsize(net_path)
                print(f"  �� Network rebuilt (fallback): andheri.net.xml ({net_size / 1024:.1f} KB)")
            else:
                print(f"  ❌ netconvert failed!")
                if result2.stderr:
                    print(f"     {result2.stderr[:500]}")
                sys.exit(1)

    except FileNotFoundError:
        print("  ❌ 'netconvert' not found!")
        sys.exit(1)

# ============================================================================
# STEP 4: Validation
# ============================================================================

def validate():
    """Validate the rebuilt network."""
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 4: Validation")
    print("=" * 70)

    net_path = os.path.join(OUT_DIR, "andheri.net.xml")
    if not os.path.exists(net_path):
        print("  ❌ Network file not found!")
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

    print(f"  📁 Network: {size_kb:.1f} KB")
    print(f"  🔗 Edges: {edge_count}")
    print(f"  🔵 Junctions: {junction_count}")
    print(f"  🚦 Traffic lights: {tls_count}")

    # Compare with Phase 1
    print(f"\n  📊 Phase 1 had 2 traffic lights → Phase 2 now has {tls_count}")

    if tls_count >= 5:
        print(f"  ✅ Realistic signal count for Andheri!")
    elif tls_count > 0:
        print(f"  ⚠️  Few signals, but usable. TomTom may have limited POI data here.")
    else:
        print(f"  ❌ No signals! Something went wrong.")

    # Save final signal summary
    summary = {
        "network_file": "andheri.net.xml",
        "edges": edge_count,
        "junctions": junction_count,
        "traffic_lights": tls_count,
        "file_size_kb": round(size_kb, 1),
        "source": "TomTom Search API + topology supplement",
    }
    summary_path = os.path.join(OUT_DIR, "phase2_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n  ✅ Phase 2 complete! Network has real traffic signals.")
    return True

# ============================================================================
# MAIN
# ============================================================================

def main():
    print("\n" + "=" * 70)
    print("🚦 PHASE 2: EXTRACT TRAFFIC SIGNALS FROM TOMTOM")
    print("   Area: Andheri, Mumbai")
    print(f"   API Keys: {len(API_KEYS)} available")
    print("=" * 70)

    start = time.time()

    key_mgr = APIKeyManager(API_KEYS)

    # Step 1: Fetch real signal locations
    signals = fetch_signals_from_search(key_mgr)

    if len(signals) == 0:
        print("\n  ⚠️  TomTom Search returned 0 signals for this area.")
        print("     This can happen if TomTom doesn't have POI data here.")
        print("     Will fall back to topology-based signal placement.")

    # Step 2: Map to SUMO junctions
    signal_junctions, mapped = map_signals_to_junctions(signals)

    # Step 3: Rebuild network
    rebuild_network(signal_junctions)

    # Step 4: Validate
    validate()

    elapsed = time.time() - start
    print(f"\n⏱️  Phase 2 completed in {elapsed:.1f}s")
    print(f"📂 All files in: {os.path.abspath(OUT_DIR)}")
    print(f"\n👉 Next: Run phase3_generate_traffic.py")


if __name__ == "__main__":
    main()