#!/usr/bin/env python3
"""
Build Long Routes by CHAINING existing short routes
=====================================================
Takes the 508 short routes from route_bank.json and chains them.

Optimizations:
  - Batch bridge lookups (50 at a time)
  - Direct-connect check first (no duarouter needed)
  - Cache all bridges
  - SUMO validation only on final routes

Compatible with existing route_bank.json format.

Usage: python build_long_routes.py
"""

import os
import sys
import json
import math
import random
import subprocess
import time
import functools

print = functools.partial(print, flush=True)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(PROJECT_DIR, "sumo_data")

NET_PATH = os.path.join(OUT_DIR, "andheri.net.xml")
EDG_PATH = os.path.join(OUT_DIR, "andheri.edg.xml")
NODES_PATH = os.path.join(OUT_DIR, "all_nodes.json")
ROUTE_BANK_JSON = os.path.join(OUT_DIR, "route_bank.json")
ROUTE_BANK_XML = os.path.join(OUT_DIR, "route_bank.rou.xml")

TARGET_MEDIUM = 200
TARGET_LONG = 100


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def batch_find_bridges(pairs):
    """
    Find bridge routes for multiple (src_edge, dst_edge) pairs in ONE
    duarouter call. Much faster than calling duarouter 500 times.
    Returns: { (src, dst): [edge_list] or None }
    """
    if not pairs:
        return {}

    tmp_t = os.path.join(OUT_DIR, "_bb_trips.xml")
    tmp_r = os.path.join(OUT_DIR, "_bb_routes.xml")

    with open(tmp_t, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<trips>\n')
        for i, (src, dst) in enumerate(pairs):
            f.write(f'    <trip id="bb_{i}" depart="{i}.00" '
                    f'from="{src}" to="{dst}"/>\n')
        f.write('</trips>\n')

    results = {}

    try:
        subprocess.run([
            "duarouter", "-n", NET_PATH,
            "--route-files", tmp_t,
            "-o", tmp_r,
            "--ignore-errors", "true",
            "--no-warnings", "true",
        ], capture_output=True, text=True, cwd=OUT_DIR, timeout=120)

        if os.path.exists(tmp_r):
            with open(tmp_r, 'r', encoding='utf-8') as f:
                content = f.read()

            pos = 0
            while True:
                vid_start = content.find('id="bb_', pos)
                if vid_start < 0:
                    break
                vid_start += 4
                vid_end = content.find('"', vid_start)
                if vid_end < 0:
                    break
                vid = content[vid_start:vid_end]

                edges_start = content.find('edges="', vid_end)
                if edges_start < 0:
                    break

                next_veh = content.find('<vehicle', vid_end + 1)
                if next_veh > 0 and edges_start > next_veh:
                    pos = next_veh
                    continue

                edges_start += 7
                edges_end = content.find('"', edges_start)
                if edges_end < 0:
                    break

                edge_list = content[edges_start:edges_end].split()
                pos = edges_end

                try:
                    idx = int(vid.split('_')[1])
                    key = pairs[idx]
                    if len(edge_list) >= 2:
                        results[key] = edge_list
                except:
                    pass

    except:
        pass

    # Cleanup
    for fp in os.listdir(OUT_DIR):
        if fp.startswith('_bb_'):
            try:
                os.remove(os.path.join(OUT_DIR, fp))
            except:
                pass

    return results


def validate_routes_batch(routes_to_check):
    """Validate multiple routes in ONE sumo call."""
    if not routes_to_check:
        return set()

    vf = os.path.join(OUT_DIR, "_vb_check.rou.xml")

    with open(vf, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        f.write('    <vType id="vt" accel="2.6" decel="4.5" '
                'length="4.5" maxSpeed="16.67"/>\n')
        for i, (route_id, edge_list) in enumerate(routes_to_check):
            edges_str = ' '.join(edge_list)
            f.write(f'    <vehicle id="chk_{i}" type="vt" depart="{i}.00">\n')
            f.write(f'        <route edges="{edges_str}"/>\n')
            f.write(f'    </vehicle>\n')
        f.write('</routes>\n')

    valid_ids = set()

    try:
        # Run with --duration-log.statistics to see which vehicles complete
        result = subprocess.run([
            "sumo", "--no-step-log", "true",
            "-n", NET_PATH, "-r", vf,
            "--begin", "0",
            "--end", str(len(routes_to_check) + 5),
            "--no-warnings", "true",
            "--ignore-route-errors", "true",
        ], capture_output=True, text=True, cwd=OUT_DIR, timeout=60)

        if result.returncode == 0:
            # All routes valid
            valid_ids = {rid for rid, _ in routes_to_check}
        else:
            # Some failed — validate individually
            for rid, edge_list in routes_to_check:
                vf2 = os.path.join(OUT_DIR, "_vb_single.rou.xml")
                with open(vf2, 'w') as f2:
                    f2.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
                    f2.write('    <vType id="vt" accel="2.6" decel="4.5" '
                             'length="4.5" maxSpeed="16.67"/>\n')
                    f2.write(f'    <vehicle id="v0" type="vt" depart="0.00">\n')
                    f2.write(f'        <route edges="{" ".join(edge_list)}"/>\n')
                    f2.write(f'    </vehicle>\n')
                    f2.write('</routes>\n')

                try:
                    r2 = subprocess.run([
                        "sumo", "--no-step-log", "true",
                        "-n", NET_PATH, "-r", vf2,
                        "--begin", "0", "--end", "5",
                        "--no-warnings", "true",
                    ], capture_output=True, text=True, cwd=OUT_DIR, timeout=10)
                    if r2.returncode == 0:
                        valid_ids.add(rid)
                except:
                    pass

                try:
                    os.remove(vf2)
                except:
                    pass

    except:
        pass

    try:
        os.remove(vf)
    except:
        pass

    return valid_ids


def main():
    print("\n" + "=" * 70)
    print("🔗 BUILD LONG ROUTES — Chain short routes")
    print(f"   Target: {TARGET_MEDIUM} medium (31-60) + {TARGET_LONG} long (61+)")
    print("=" * 70)

    start = time.time()

    # ================================================================
    # Load existing bank
    # ================================================================
    print("\n📂 Loading route bank...")

    if not os.path.exists(ROUTE_BANK_JSON):
        print("  ❌ No route_bank.json!")
        sys.exit(1)

    with open(ROUTE_BANK_JSON, 'r') as f:
        bank = json.load(f)

    short_routes = bank.get('short_routes', [])
    existing_medium = bank.get('medium_routes', [])
    existing_long = bank.get('long_routes', [])

    print(f"  Short:  {len(short_routes)}")
    print(f"  Medium: {len(existing_medium)}")
    print(f"  Long:   {len(existing_long)}")

    if len(short_routes) < 10:
        print("  ❌ Not enough short routes!")
        sys.exit(1)

    # ================================================================
    # Load edge topology
    # ================================================================
    print("\n📏 Loading edge topology...")

    edge_to_node = {}
    with open(EDG_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            if '<edge ' not in line:
                continue
            eid = fn = tn = None
            for part in line.split():
                if part.startswith('id="'):
                    eid = part.split('"')[1]
                elif part.startswith('from="'):
                    fn = part.split('"')[1]
                elif part.startswith('to="'):
                    tn = part.split('"')[1]
            if eid and fn and tn:
                edge_to_node[eid] = (fn, tn)

    node_pos = {}
    with open(NODES_PATH, 'r') as f:
        for n in json.load(f):
            node_pos[n['node_id']] = (n['lat'], n['lon'])

    print(f"  ✅ {len(edge_to_node)} edges, {len(node_pos)} nodes")

    # ================================================================
    # Pre-compute all bridge routes (batch)
    # ================================================================
    print("\n🌉 Pre-computing bridge routes...")
    print("   (Finding how to connect end of route A to start of route B)")

    # Collect all unique (last_edge, first_edge) pairs we might need
    bridge_pairs_needed = set()
    for i in range(len(short_routes)):
        for j in range(len(short_routes)):
            if i == j:
                continue

            last_edge = short_routes[i]['edges'][-1]
            first_edge = short_routes[j]['edges'][0]

            last_to = edge_to_node.get(last_edge, (None, None))[1]
            first_from = edge_to_node.get(first_edge, (None, None))[0]

            if last_to == first_from:
                # Directly connected — no bridge needed
                continue

            bridge_pairs_needed.add((last_edge, first_edge))

    print(f"  Need bridges for: {len(bridge_pairs_needed)} pairs")

    # Batch resolve bridges (50 at a time)
    bridge_cache = {}
    pairs_list = list(bridge_pairs_needed)

    # Mark directly connected pairs
    direct_connect = set()
    for i in range(len(short_routes)):
        for j in range(len(short_routes)):
            if i == j:
                continue
            last_edge = short_routes[i]['edges'][-1]
            first_edge = short_routes[j]['edges'][0]
            last_to = edge_to_node.get(last_edge, (None, None))[1]
            first_from = edge_to_node.get(first_edge, (None, None))[0]
            if last_to == first_from:
                direct_connect.add((last_edge, first_edge))

    print(f"  Directly connected: {len(direct_connect)} pairs (free!)")

    # Only need duarouter for non-direct pairs
    # Sample a reasonable number (max 2000)
    if len(pairs_list) > 2000:
        pairs_list = random.sample(pairs_list, 2000)

    BRIDGE_BATCH = 100
    for batch_start in range(0, len(pairs_list), BRIDGE_BATCH):
        batch = pairs_list[batch_start:batch_start + BRIDGE_BATCH]
        results = batch_find_bridges(batch)
        bridge_cache.update(results)

        # Mark failures
        for pair in batch:
            if pair not in bridge_cache:
                bridge_cache[pair] = None

        done = batch_start + len(batch)
        if done % 500 == 0 or done >= len(pairs_list):
            found = sum(1 for v in bridge_cache.values() if v is not None)
            print(f"  ... {done}/{len(pairs_list)} | Found: {found} bridges")

    found_bridges = sum(1 for v in bridge_cache.values() if v is not None)
    print(f"\n  ✅ Bridges found: {found_bridges}/{len(bridge_cache)}")

    # ================================================================
    # Chain routes
    # ================================================================
    print("\n🔗 Chaining routes...")

    medium_routes = list(existing_medium)
    long_routes = list(existing_long)
    next_id = len(short_routes) + len(medium_routes) + len(long_routes)

    pending_validation = []  # (route_id, edge_list, category)
    attempts = 0
    max_attempts = 5000

    while attempts < max_attempts:
        if len(medium_routes) >= TARGET_MEDIUM and len(long_routes) >= TARGET_LONG:
            break

        attempts += 1

        # Pick chain length
        if len(long_routes) < TARGET_LONG:
            num_chains = random.choice([3, 4])
        else:
            num_chains = 2

        indices = random.sample(range(len(short_routes)),
                                min(num_chains, len(short_routes)))
        chosen = [short_routes[i] for i in indices]

        # Build chained edge list
        full_edges = list(chosen[0]['edges'])
        chain_ok = True

        for c in range(1, len(chosen)):
            prev_last = full_edges[-1]
            next_first = chosen[c]['edges'][0]

            # Check direct connection
            prev_to = edge_to_node.get(prev_last, (None, None))[1]
            next_from = edge_to_node.get(next_first, (None, None))[0]

            if prev_to == next_from:
                # Direct — just append (skip duplicate if first edge = last edge)
                for e in chosen[c]['edges']:
                    if e != full_edges[-1]:
                        full_edges.append(e)
            else:
                # Use cached bridge
                key = (prev_last, next_first)
                bridge = bridge_cache.get(key)

                if bridge is None:
                    chain_ok = False
                    break

                if len(bridge) > 25:
                    chain_ok = False
                    break

                # Append bridge edges (avoid duplicates)
                for be in bridge:
                    if be != full_edges[-1]:
                        full_edges.append(be)

                # Append next route (avoid duplicates)
                for e in chosen[c]['edges']:
                    if e != full_edges[-1]:
                        full_edges.append(e)

        if not chain_ok:
            continue

        # Remove consecutive duplicates
        deduped = [full_edges[0]]
        for e in full_edges[1:]:
            if e != deduped[-1]:
                deduped.append(e)
        full_edges = deduped

        ec = len(full_edges)
        if ec < 31:
            continue

        rid = f"route_{next_id}"
        next_id += 1

        if ec <= 60 and len(medium_routes) < TARGET_MEDIUM:
            pending_validation.append((rid, full_edges, 'medium'))
        elif ec > 60 and len(long_routes) < TARGET_LONG:
            pending_validation.append((rid, full_edges, 'long'))
        else:
            continue

        # Validate in batches of 20
        if len(pending_validation) >= 20:
            check_items = [(rid, edges) for rid, edges, _ in pending_validation]
            valid_ids = validate_routes_batch(check_items)

            for rid, edges, cat in pending_validation:
                if rid in valid_ids:
                    src_from = edge_to_node.get(edges[0], (None, None))[0]
                    dst_to = edge_to_node.get(edges[-1], (None, None))[1]
                    dist = 0
                    if src_from and dst_to:
                        sp = node_pos.get(src_from)
                        dp = node_pos.get(dst_to)
                        if sp and dp:
                            dist = round(haversine(sp[0], sp[1], dp[0], dp[1]))

                    route = {
                        'id': rid,
                        'src_edge': edges[0],
                        'dst_edge': edges[-1],
                        'distance_m': dist,
                        'edge_count': len(edges),
                        'edges': edges,
                    }

                    if cat == 'medium':
                        medium_routes.append(route)
                    else:
                        long_routes.append(route)

            pending_validation = []

            if (len(medium_routes) + len(long_routes)) % 50 == 0:
                print(f"  Attempt {attempts} | "
                      f"Medium: {len(medium_routes)}/{TARGET_MEDIUM} | "
                      f"Long: {len(long_routes)}/{TARGET_LONG}")

    # Flush remaining
    if pending_validation:
        check_items = [(rid, edges) for rid, edges, _ in pending_validation]
        valid_ids = validate_routes_batch(check_items)
        for rid, edges, cat in pending_validation:
            if rid in valid_ids:
                src_from = edge_to_node.get(edges[0], (None, None))[0]
                dst_to = edge_to_node.get(edges[-1], (None, None))[1]
                dist = 0
                if src_from and dst_to:
                    sp = node_pos.get(src_from)
                    dp = node_pos.get(dst_to)
                    if sp and dp:
                        dist = round(haversine(sp[0], sp[1], dp[0], dp[1]))
                route = {
                    'id': rid, 'src_edge': edges[0], 'dst_edge': edges[-1],
                    'distance_m': dist, 'edge_count': len(edges), 'edges': edges,
                }
                if cat == 'medium':
                    medium_routes.append(route)
                else:
                    long_routes.append(route)

    print(f"\n  ✅ Medium: {len(medium_routes)}")
    print(f"  ✅ Long:   {len(long_routes)}")

    # ================================================================
    # Save
    # ================================================================
    print("\n📦 Saving...")

    all_routes = short_routes + medium_routes + long_routes
    all_ec = [r['edge_count'] for r in all_routes]
    all_dist = [r['distance_m'] for r in all_routes if r['distance_m'] > 0]

    stats = {
        'total_routes': len(all_routes),
        'min_edges': min(all_ec),
        'max_edges': max(all_ec),
        'avg_edges': round(sum(all_ec) / len(all_ec), 1),
        'avg_distance_m': round(sum(all_dist) / len(all_dist)) if all_dist else 0,
        'short_count': len(short_routes),
        'medium_count': len(medium_routes),
        'long_count': len(long_routes),
    }

    route_bank = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'min_edge_count': 15,
        'statistics': stats,
        'short_routes': short_routes,
        'medium_routes': medium_routes,
        'long_routes': long_routes,
    }

    with open(ROUTE_BANK_JSON, 'w') as f:
        json.dump(route_bank, f, indent=2)
    print(f"  💾 JSON: {os.path.getsize(ROUTE_BANK_JSON) / 1024:.1f} KB")

    with open(ROUTE_BANK_XML, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(f'<!-- Route Bank: {len(all_routes)} routes -->\n')
        f.write(f'<!-- Short: {len(short_routes)} | Medium: {len(medium_routes)} | Long: {len(long_routes)} -->\n')
        f.write('<routes>\n')
        f.write('    <vType id="boost_car" accel="2.6" decel="4.5" sigma="0.5" '
                'length="4.5" maxSpeed="16.67" color="0.2,0.5,0.9"/>\n\n')
        for route in all_routes:
            edges_str = ' '.join(route['edges'])
            f.write(f'    <route id="{route["id"]}" edges="{edges_str}"/>\n')
        f.write('\n</routes>\n')
    print(f"  💾 XML:  {os.path.getsize(ROUTE_BANK_XML) / 1024:.1f} KB")

    # Final verify
    print(f"\n  🔍 Final verification...")
    test = medium_routes[:3] + long_routes[:3]
    if not test:
        test = short_routes[:5]

    vf = os.path.join(OUT_DIR, "_final_vfy.rou.xml")
    with open(vf, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        f.write('    <vType id="vt" accel="2.6" decel="4.5" length="4.5" maxSpeed="16.67"/>\n')
        for i, r in enumerate(test):
            f.write(f'    <vehicle id="v{i}" type="vt" depart="{i}.00">\n')
            f.write(f'        <route edges="{" ".join(r["edges"])}"/>\n')
            f.write(f'    </vehicle>\n')
        f.write('</routes>\n')

    try:
        res = subprocess.run([
            "sumo", "--no-step-log", "true",
            "-n", NET_PATH, "-r", vf,
            "--begin", "0", "--end", "50",
            "--no-warnings", "true",
        ], capture_output=True, text=True, cwd=OUT_DIR, timeout=30)
        print(f"  ✅ SUMO PASSED!" if res.returncode == 0
              else f"  ⚠️ {res.stderr[:200]}")
    except:
        pass

    try:
        os.remove(vf)
    except:
        pass

    elapsed = time.time() - start
    print(f"\n" + "=" * 70)
    print(f"✅ ROUTE BANK UPDATED")
    print(f"=" * 70)
    print(f"  Short  (15-30): {stats['short_count']}")
    print(f"  Medium (31-60): {stats['medium_count']}")
    print(f"  Long   (61+):   {stats['long_count']}")
    print(f"  TOTAL:          {stats['total_routes']}")
    print(f"  Edges:          {stats['min_edges']}-{stats['max_edges']} (avg {stats['avg_edges']})")
    print(f"  Time:           {elapsed:.1f}s")


if __name__ == "__main__":
    main()