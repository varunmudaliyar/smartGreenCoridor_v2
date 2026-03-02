#!/usr/bin/env python3
"""
Fix edge_speeds.xml — use correct SUMO format for speed overrides.
Instead of <edge id="..." speed="..."/> (which tries to redefine edges),
we need to use <edgeData> or simply patch speeds via calibrators/additionals.

Actually the correct approach is: don't use edge_speeds.xml at all.
Instead, set speeds directly in the route file or use meandata.
The simplest fix: remove edge_speeds.xml from config and let the
network use its built-in speeds from Phase 1 + 2.
"""

import os
import json

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sumo_data")

print("🔧 Fixing SUMO configuration...")

# ============================================================================
# Option 1: Remove edge_speeds.xml from config (simplest, works now)
# ============================================================================

# Fix simulation.sumocfg
for cfg_name in ["simulation.sumocfg", "view_network.sumocfg"]:
    cfg_path = os.path.join(OUT_DIR, cfg_name)
    if not os.path.exists(cfg_path):
        continue

    with open(cfg_path, "r") as f:
        content = f.read()

    # Remove edge_speeds.xml from additional-files
    content = content.replace(",edge_speeds.xml", "")
    content = content.replace("edge_speeds.xml,", "")
    content = content.replace("edge_speeds.xml", "")

    # Clean up empty additional-files if needed
    content = content.replace('<additional-files value=""/>', '')
    content = content.replace('<additional-files value=","/>', '')

    with open(cfg_path, "w") as f:
        f.write(content)

    print(f"  ✅ Fixed: {cfg_name}")

# ============================================================================
# Option 2: Rewrite edge_speeds.xml as proper calibrators (for later use)
# ============================================================================

# Load edge traffic data
traffic_path = os.path.join(OUT_DIR, "edge_traffic_data.json")
if os.path.exists(traffic_path):
    with open(traffic_path, "r") as f:
        edge_traffic = json.load(f)

    # Write as proper SUMO calibrator format
    # This uses <calibrator> elements which CAN set speed on edges
    calib_path = os.path.join(OUT_DIR, "edge_speeds_calibrators.xml")
    with open(calib_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<additional>\n')

        count = 0
        for eid, et in edge_traffic.items():
            if et.get("match_distance_m", -1) < 0:
                continue  # Skip unmatched edges (no real data)

            speed = et.get("real_speed_ms", 13.89)
            if speed < 1.0:
                speed = 1.0

            f.write(f'    <calibrator id="cal_{eid}" edge="{eid}" '
                    f'pos="10" output="calibrator_output.xml">\n')
            f.write(f'        <flow begin="0" end="{3600}" '
                    f'speed="{speed:.2f}" vehsPerHour="0"/>\n')
            f.write(f'    </calibrator>\n')
            count += 1

        f.write('</additional>\n')

    print(f"  💾 Calibrators: {calib_path} ({count} edges with real speeds)")
    print(f"     (Not loaded by default — add to config if you want real speeds)")
else:
    print(f"  ⚠️  No edge_traffic_data.json found")

print(f"\n✅ Done! Now try:")
print(f'   sumo-gui -c "sumo_data\\view_network.sumocfg"')