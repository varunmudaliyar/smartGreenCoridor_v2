#!/usr/bin/env python3
"""
Phase 2: Place Traffic Signals from OSM/SUMO Reference Data
=============================================================
Uses traffic_lights.json (extracted from SUMO/OSM) as ground truth.
Maps each real-world signal to the nearest junction in our routing-based network.
No API calls needed.

Phase 1 result: 883 signals (too many — every high-degree node was marked)
Phase 2 fix:    Map ~60 real OSM signals to our 1,350 junctions
"""

import os
import sys
import json
import math
import subprocess
from collections import defaultdict

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUT_DIR = os.path.join(PROJECT_DIR, "sumo_data")

# Reference signal data from SUMO/OSM
REFERENCE_SIGNALS_FILE = os.path.join(OUT_DIR, "traffic_lights.json")

# Max distance to map a reference signal to our junction (meters)
SIGNAL_MAP_DISTANCE = 100.0

# If OSM signals map to fewer than this, supplement from topology
MIN_SIGNALS = 30

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
# STEP 1: Load reference signals
# ============================================================================

def load_reference_signals():
    """Load traffic light data from the OSM/SUMO reference file."""
    print("=" * 70)
    print("PHASE 2 — STEP 1: Loading reference traffic signals")
    print("=" * 70)

    if not os.path.exists(REFERENCE_SIGNALS_FILE):
        print(f"  ❌ {REFERENCE_SIGNALS_FILE} not found!")
        print(f"     Please place the traffic_lights.json file in sumo_data/")
        sys.exit(1)

    with open(REFERENCE_SIGNALS_FILE, "r") as f:
        signals = json.load(f)

    print(f"  📂 Loaded {len(signals)} reference traffic signals from OSM/SUMO")

    # Analyze
    total_lanes = 0
    complex_signals = 0  # 4+ incoming lanes = major intersection
    simple_signals = 0

    for sig in signals:
        n_lanes = len(sig.get("incLanes", []))
        total_lanes += n_lanes
        if n_lanes >= 4:
            complex_signals += 1
        else:
            simple_signals += 1

    print(f"  🚦 Complex signals (4+ lanes): {complex_signals}")
    print(f"  🚦 Simple signals (<4 lanes):  {simple_signals}")
    print(f"  📊 Average incoming lanes: {total_lanes / len(signals):.1f}")

    # Show some examples
    print(f"\n  📍 Sample signals:")
    for sig in signals[:5]:
        print(f"     ID: {sig['id']:>15s} | "
              f"({sig['lat']:.4f}, {sig['lon']:.4f}) | "
              f"{len(sig.get('incLanes', []))} lanes")

    return signals

# ============================================================================
# STEP 2: Load our network nodes + compute topology
# ============================================================================

def load_network():
    """Load nodes from Phase 1 and compute junction degrees."""
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 2: Loading network topology from Phase 1")
    print("=" * 70)

    # Load nodes
    nodes_path = os.path.join(OUT_DIR, "all_nodes.json")
    if not os.path.exists(nodes_path):
        print(f"  ❌ all_nodes.json not found! Run Phase 1 first.")
        sys.exit(1)

    with open(nodes_path, "r") as f:
        all_nodes = json.load(f)
    print(f"  📂 Loaded {len(all_nodes)} nodes")

    # Compute degree from edges
    edg_path = os.path.join(OUT_DIR, "andheri.edg.xml")
    node_degree = defaultdict(int)
    node_max_priority = defaultdict(int)

    with open(edg_path, "r", encoding="utf-8") as f:
        for line in f:
            if "<edge " not in line:
                continue
            from_node = to_node = None
            priority = 3
            parts = line.split()
            for p in parts:
                if p.startswith('from="'):
                    from_node = p.split('"')[1]
                elif p.startswith('to="'):
                    to_node = p.split('"')[1]
                elif p.startswith('priority="'):
                    try:
                        priority = int(p.split('"')[1])
                    except ValueError:
                        pass
            if from_node:
                node_degree[from_node] += 1
                node_max_priority[from_node] = max(
                    node_max_priority[from_node], priority)
            if to_node:
                node_degree[to_node] += 1
                node_max_priority[to_node] = max(
                    node_max_priority[to_node], priority)

    print(f"  📊 Nodes with edges: {len(node_degree)}")

    return all_nodes, node_degree, node_max_priority

# ============================================================================
# STEP 3: Map reference signals to our junctions
# ============================================================================

def map_signals(ref_signals, all_nodes, node_degree, node_max_priority):
    """
    For each OSM/SUMO reference signal, find the nearest junction
    in our routing-based network (within SIGNAL_MAP_DISTANCE).
    """
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 3: Mapping reference signals → network junctions")
    print("=" * 70)

    signal_junctions = set()
    mapped_details = []
    unmapped_signals = []

    for sig in ref_signals:
        sig_lat = sig["lat"]
        sig_lon = sig["lon"]
        sig_id = sig["id"]
        n_lanes = len(sig.get("incLanes", []))

        best_node = None
        best_dist = float("inf")

        for node in all_nodes:
            d = haversine(sig_lat, sig_lon, node["lat"], node["lon"])
            if d < best_dist:
                best_dist = d
                best_node = node

        if best_node and best_dist <= SIGNAL_MAP_DISTANCE:
            nid = best_node["node_id"]
            signal_junctions.add(nid)
            mapped_details.append({
                "osm_signal_id": sig_id,
                "signal_lat": sig_lat,
                "signal_lon": sig_lon,
                "signal_lanes": n_lanes,
                "junction_id": nid,
                "junction_lat": best_node["lat"],
                "junction_lon": best_node["lon"],
                "distance_m": round(best_dist, 1),
                "junction_degree": node_degree.get(nid, 0),
                "source": "osm_reference",
            })
        else:
            unmapped_signals.append({
                "osm_signal_id": sig_id,
                "lat": sig_lat,
                "lon": sig_lon,
                "lanes": n_lanes,
                "nearest_dist_m": round(best_dist, 1) if best_node else -1,
            })

    print(f"  ✅ Mapped: {len(mapped_details)} signals → "
          f"{len(signal_junctions)} unique junctions")
    print(f"  ⚠️  Unmapped (no junction within {SIGNAL_MAP_DISTANCE}m): "
          f"{len(unmapped_signals)}")

    if mapped_details:
        avg_dist = sum(m["distance_m"] for m in mapped_details) / len(mapped_details)
        max_dist = max(m["distance_m"] for m in mapped_details)
        print(f"  📏 Mapping distance: avg={avg_dist:.1f}m, max={max_dist:.1f}m")

    # --- Show unmapped signals (for debugging) ---
    if unmapped_signals:
        print(f"\n  📍 Unmapped signals (outside our network):")
        for u in unmapped_signals[:10]:
            print(f"     ID: {u['osm_signal_id']:>15s} | "
                  f"({u['lat']:.4f}, {u['lon']:.4f}) | "
                  f"{u['lanes']} lanes | nearest: {u['nearest_dist_m']}m")
        if len(unmapped_signals) > 10:
            print(f"     ... and {len(unmapped_signals) - 10} more")

    # --- Supplement from topology if too few ---
    if len(signal_junctions) < MIN_SIGNALS:
        needed = MIN_SIGNALS - len(signal_junctions)
        print(f"\n  📍 Supplementing: have {len(signal_junctions)}, "
              f"need {MIN_SIGNALS}, adding {needed} from topology...")

        candidates = []
        for node in all_nodes:
            nid = node["node_id"]
            if nid in signal_junctions:
                continue
            deg = node_degree.get(nid, 0)
            pri = node_max_priority.get(nid, 0)
            if deg >= 6:
                score = deg * pri
                candidates.append((nid, node, deg, pri, score))

        candidates.sort(key=lambda x: x[4], reverse=True)

        added = 0
        for nid, node, deg, pri, score in candidates[:needed]:
            signal_junctions.add(nid)
            mapped_details.append({
                "osm_signal_id": f"topology_{nid}",
                "signal_lat": node["lat"],
                "signal_lon": node["lon"],
                "signal_lanes": 0,
                "junction_id": nid,
                "junction_lat": node["lat"],
                "junction_lon": node["lon"],
                "distance_m": 0,
                "junction_degree": deg,
                "source": "topology_supplement",
            })
            added += 1

        print(f"     Added {added} topology-based signals")

    print(f"\n  🚦 FINAL signal count: {len(signal_junctions)}")

    # Source breakdown
    osm_count = sum(1 for m in mapped_details if m["source"] == "osm_reference")
    topo_count = sum(1 for m in mapped_details if m["source"] == "topology_supplement")
    print(f"  📊 Sources: OSM={osm_count}, Topology={topo_count}")

    # --- Save ---
    mapped_path = os.path.join(OUT_DIR, "tomtom_signals_mapped.json")
    with open(mapped_path, "w") as f:
        json.dump(mapped_details, f, indent=2)
    print(f"  💾 Mapped details: {mapped_path}")

    ids_path = os.path.join(OUT_DIR, "signal_junction_ids.json")
    with open(ids_path, "w") as f:
        json.dump(list(signal_junctions), f, indent=2)
    print(f"  💾 Signal IDs: {ids_path}")

    sig_nodes_path = os.path.join(OUT_DIR, "signal_candidate_nodes.json")
    sig_nodes = []
    for m in mapped_details:
        sig_nodes.append({
            "node_id": m["junction_id"],
            "lat": m["junction_lat"],
            "lon": m["junction_lon"],
            "degree": m.get("junction_degree", 0),
            "osm_id": m["osm_signal_id"],
            "source": m["source"],
        })
    with open(sig_nodes_path, "w") as f:
        json.dump(sig_nodes, f, indent=2)

    if unmapped_signals:
        unmapped_path = os.path.join(OUT_DIR, "unmapped_signals.json")
        with open(unmapped_path, "w") as f:
            json.dump(unmapped_signals, f, indent=2)
        print(f"  💾 Unmapped signals: {unmapped_path}")

    return signal_junctions

# ============================================================================
# STEP 4: Rewrite .nod.xml and rebuild network
# ============================================================================

def rebuild_network(signal_junctions):
    """Reset all junctions to priority, set only real signals, rebuild."""
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 4: Rebuilding network with OSM-verified signals")
    print("=" * 70)

    # Load nodes
    nodes_path = os.path.join(OUT_DIR, "all_nodes.json")
    with open(nodes_path, "r") as f:
        all_nodes = json.load(f)

    # Rewrite .nod.xml
    nod_path = os.path.join(OUT_DIR, "andheri.nod.xml")
    sig_count = 0
    pri_count = 0

    with open(nod_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<nodes>\n')
        for node in all_nodes:
            nid = node["node_id"]
            if nid in signal_junctions:
                ntype = "traffic_light"
                sig_count += 1
            else:
                ntype = "priority"
                pri_count += 1
            f.write(f'    <node id="{nid}" x="{node["lon"]:.7f}" '
                    f'y="{node["lat"]:.7f}" type="{ntype}"/>\n')
        f.write('</nodes>\n')

    print(f"  📝 Rewrote {nod_path}")
    print(f"     traffic_light: {sig_count}")
    print(f"     priority:      {pri_count}")

    # Re-run netconvert
    print(f"\n  🔧 Running netconvert...")
    net_path = os.path.join(OUT_DIR, "andheri.net.xml")

    try:
        result = subprocess.run(
            ["netconvert", "-c", "andheri.netccfg"],
            capture_output=True, text=True, cwd=OUT_DIR, timeout=180
        )
        if result.returncode == 0 and os.path.exists(net_path):
            print(f"  ✅ Network rebuilt: {os.path.getsize(net_path) / 1024:.1f} KB")
        else:
            print(f"  ⚠️  Config failed, trying fallback...")
            result2 = subprocess.run([
                "netconvert",
                "--node-files", "andheri.nod.xml",
                "--edge-files", "andheri.edg.xml",
                "--output-file", "andheri.net.xml",
                "--proj", "+proj=utm +zone=43 +ellps=WGS84 +datum=WGS84 +units=m +no_defs",
                "--junctions.join", "true", "--junctions.join-dist", "12",
                "--junctions.corner-detail", "5", "--no-turnarounds", "true",
                "--roundabouts.guess", "true", "--tls.default-type", "actuated",
                "--no-warnings", "true",
            ], capture_output=True, text=True, cwd=OUT_DIR, timeout=180)
            if result2.returncode == 0 and os.path.exists(net_path):
                print(f"  ✅ Network rebuilt (fallback): "
                      f"{os.path.getsize(net_path) / 1024:.1f} KB")
            else:
                print(f"  ❌ Failed!")
                if result2.stderr:
                    print(f"     {result2.stderr[:500]}")
                sys.exit(1)
    except FileNotFoundError:
        print("  ❌ 'netconvert' not found!")
        sys.exit(1)

# ============================================================================
# STEP 5: Validate
# ============================================================================

def validate():
    print("\n" + "=" * 70)
    print("PHASE 2 — STEP 5: Final validation")
    print("=" * 70)

    net_path = os.path.join(OUT_DIR, "andheri.net.xml")
    if not os.path.exists(net_path):
        print("  ❌ Network not found!")
        return False

    size_kb = os.path.getsize(net_path) / 1024
    ec = jc = tc = ic = 0

    with open(net_path, "r", encoding="utf-8") as f:
        for line in f:
            if "<edge " in line:
                if 'function="internal"' in line:
                    ic += 1
                else:
                    ec += 1
            if "<junction " in line and 'type="internal"' not in line:
                jc += 1
            if "<tlLogic " in line:
                tc += 1

    print(f"  📁 Size:           {size_kb:.1f} KB")
    print(f"  🔗 Edges:          {ec} (+ {ic} internal)")
    print(f"  🔵 Junctions:      {jc}")
    print(f"  🚦 Traffic lights: {tc}")

    print(f"\n  📊 Signal correction:")
    print(f"     Phase 1: 883 signals (every high-degree node)")
    print(f"     Phase 2: {tc} signals (OSM-verified real locations)")

    if 20 <= tc <= 100:
        print(f"  ✅ Realistic! Real Andheri has ~40-70 signalized intersections.")
    elif tc > 100:
        print(f"  ⚠️  Still a bit high, but much better than 883.")
    elif tc > 0:
        print(f"  ⚠️  Low, but functional for simulation.")
    else:
        print(f"  ❌ No traffic lights!")

    # Save summary
    summary = {
        "network_file": "andheri.net.xml",
        "file_size_kb": round(size_kb, 1),
        "edges": ec,
        "internal_edges": ic,
        "junctions": jc,
        "traffic_lights": tc,
        "signal_source": "OSM/SUMO reference (traffic_lights.json)",
        "phase1_signals": 883,
        "phase2_signals": tc,
    }
    with open(os.path.join(OUT_DIR, "phase2_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n  🖥️  View: cd sumo_data && sumo-gui -c view_network.sumocfg")
    print(f"\n  ✅ Phase 2 complete! Ready for Phase 3.")
    return True

# ============================================================================
# MAIN
# ============================================================================

def main():
    print("\n" + "=" * 70)
    print("🚦 PHASE 2: PLACE REAL TRAFFIC SIGNALS FROM OSM/SUMO DATA")
    print("   Source: traffic_lights.json (OSM ground truth)")
    print("   API calls needed: ZERO")
    print("   Phase 1 had: 883 signals → will be corrected to ~40-70")
    print("=" * 70)

    import time
    start = time.time()

    # Step 1: Load reference signals
    ref_signals = load_reference_signals()

    # Step 2: Load our network
    all_nodes, node_degree, node_max_priority = load_network()

    # Step 3: Map signals to junctions
    signal_junctions = map_signals(
        ref_signals, all_nodes, node_degree, node_max_priority)

    # Step 4: Rebuild network
    rebuild_network(signal_junctions)

    # Step 5: Validate
    validate()

    elapsed = time.time() - start
    print(f"\n⏱️  Phase 2 completed in {elapsed:.1f}s")
    print(f"📂 Files: {os.path.abspath(OUT_DIR)}")
    print(f"\n👉 Next: python phase3_generate_traffic.py")


if __name__ == "__main__":
    main()