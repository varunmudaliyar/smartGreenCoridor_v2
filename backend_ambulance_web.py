#!/usr/bin/env python3
"""
Smart Green Corridor — Backend Server v3.4
============================================
Flask + SUMO/TraCI + WebSocket

v3.4: Dynamic traffic refresh from TomTom
  - "Refresh Traffic" fetches real-time congestion
  - If < 45% → injects vehicles to boost congestion
  - If ≥ 45% → uses real traffic as-is
  - Real speeds applied to SUMO edges live

Run: python backend_ambulance_web.py
"""

from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import traci
import threading
import time
import json
import os
import sys
import math
import random
import functools
import requests as http_requests

print = functools.partial(print, flush=True)

# ============================================================================
# CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = 'smart-green-corridor-2025'
CORS(app, resources={r"/*": {"origins": ["http://localhost:3000"]}})
socketio = SocketIO(
    app,
    cors_allowed_origins=["http://localhost:3000"],
    async_mode='threading',
    ping_timeout=120,
    ping_interval=25
)

DATA_DIR = 'sumo_data'
SUMO_CONFIG = os.path.join(DATA_DIR, 'simulation.sumocfg')
SUMO_BINARY = 'sumo'
MIN_VEHICLES = 50
GREEN_CORRIDOR_DETECTION_DISTANCE = 100
SIGNAL_ACTIVATION_DISTANCE = 80
SIGNAL_RELEASE_DELAY = 3

# TomTom
TOMTOM_API_KEYS = [
    "mPtfLSFK8MXJHzOuvE6CJJKYn56Hcw2v",
    "3yQ8eHK7zA1jO96iAfi6biOGYHuEttC2",
]
TOMTOM_KEY_IDX = 0
FLOW_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"

BBOX = {
    "north": 19.1350, "south": 19.0980,
    "west": 72.8220, "east": 72.8700,
}

# Congestion target
MIN_CONGESTION_TARGET = 0.45  # 45%

# ============================================================================
# GLOBAL STATE
# ============================================================================

simulation_running = False
simulation_thread = None
connected_clients = set()
active_ambulances = {}
ambulance_counter = 0
ambulance_controlled_signals = {}
injected_vehicle_counter = 0

CACHED_TL_APPROACHES = {}
EDGE_FROM_NODE = {}

# Track last refresh
last_refresh_data = {
    'timestamp': None,
    'real_congestion': 0,
    'target_congestion': MIN_CONGESTION_TARGET,
    'vehicles_injected': 0,
    'flow_segments': 0,
    'boosted': False,
}

print("=" * 70)
print("🚦 SMART GREEN CORRIDOR — BACKEND v3.4")
print("   Dynamic TomTom traffic refresh + congestion boosting")
print("=" * 70)

# ============================================================================
# LOAD DATA
# ============================================================================

print("\n📂 Loading data...")

try:
    with open(os.path.join(DATA_DIR, 'hospitals.json'), 'r') as f:
        HOSPITALS = json.load(f)
    print(f"  ✅ {len(HOSPITALS)} hospitals")
except Exception as e:
    print(f"  ❌ hospitals.json: {e}")
    sys.exit(1)

try:
    with open(os.path.join(DATA_DIR, 'hospital_routes_validated.json'), 'r') as f:
        ROUTE_DATA = json.load(f)
    print(f"  ✅ {ROUTE_DATA['assigned_routes']} validated routes")
except Exception as e:
    print(f"  ❌ hospital_routes_validated.json: {e}")
    sys.exit(1)

TRAFFIC_FLOW = []
try:
    with open(os.path.join(DATA_DIR, 'tomtom_traffic_flow.json'), 'r') as f:
        TRAFFIC_FLOW = json.load(f)
    print(f"  ✅ {len(TRAFFIC_FLOW)} cached traffic flow segments")
except:
    print(f"  ⚠️  No cached traffic flow")

EDGE_TRAFFIC = {}
try:
    with open(os.path.join(DATA_DIR, 'edge_traffic_data.json'), 'r') as f:
        EDGE_TRAFFIC = json.load(f)
    print(f"  ✅ {len(EDGE_TRAFFIC)} edge traffic entries")
except:
    pass

TRAFFIC_INCIDENTS = []
try:
    with open(os.path.join(DATA_DIR, 'tomtom_incidents.json'), 'r') as f:
        TRAFFIC_INCIDENTS = json.load(f)
    print(f"  ✅ {len(TRAFFIC_INCIDENTS)} incidents")
except:
    pass

TRAFFIC_LIGHTS_REF = []
try:
    with open(os.path.join(DATA_DIR, 'traffic_lights.json'), 'r') as f:
        TRAFFIC_LIGHTS_REF = json.load(f)
    print(f"  ✅ {len(TRAFFIC_LIGHTS_REF)} reference traffic lights")
except:
    pass

# ============================================================================
# EDGE → FROM_NODE LOOKUP
# ============================================================================

def load_edge_from_nodes():
    global EDGE_FROM_NODE
    edg_path = os.path.join(DATA_DIR, 'andheri.edg.xml')
    if not os.path.exists(edg_path):
        print(f"  ⚠️  No edg.xml")
        return
    count = 0
    with open(edg_path, 'r', encoding='utf-8') as f:
        for line in f:
            if '<edge ' not in line:
                continue
            eid = fn = None
            for part in line.split():
                if part.startswith('id="'): eid = part.split('"')[1]
                elif part.startswith('from="'): fn = part.split('"')[1]
            if eid and fn:
                EDGE_FROM_NODE[eid] = fn
                count += 1
    print(f"  ✅ Edge→Node: {count} edges")

load_edge_from_nodes()

# ============================================================================
# TOMTOM LIVE FETCH
# ============================================================================

def fetch_live_traffic(sample_count=50):
    """
    Fetch real-time traffic from TomTom Flow API.
    Uses a grid sample of points across Andheri.
    Returns: (avg_congestion, flow_segments_list, api_calls_used)
    """
    global TOMTOM_KEY_IDX

    flow_data = []
    seen = set()
    api_calls = 0

    # Sample grid — fewer points for speed (50 instead of 380)
    lat_step = (BBOX['north'] - BBOX['south']) / math.ceil(math.sqrt(sample_count))
    lon_step = (BBOX['east'] - BBOX['west']) / math.ceil(math.sqrt(sample_count))

    points = []
    lat = BBOX['south']
    while lat <= BBOX['north']:
        lon = BBOX['west']
        while lon <= BBOX['east']:
            points.append((lat, lon))
            lon += lon_step
        lat += lat_step

    points = points[:sample_count]

    for lat, lon in points:
        key = TOMTOM_API_KEYS[TOMTOM_KEY_IDX]
        params = {
            "key": key,
            "point": f"{lat},{lon}",
            "unit": "KMPH",
            "thickness": 10,
        }

        try:
            resp = http_requests.get(FLOW_URL, params=params, timeout=8)
            api_calls += 1

            if resp.status_code == 429:
                TOMTOM_KEY_IDX = (TOMTOM_KEY_IDX + 1) % len(TOMTOM_API_KEYS)
                time.sleep(1)
                continue

            if resp.status_code != 200:
                continue

            data = resp.json()
            flow_info = data.get("flowSegmentData", {})
            if not flow_info:
                continue

            current = flow_info.get("currentSpeed", 0)
            freeflow = flow_info.get("freeFlowSpeed", 0)
            coords = flow_info.get("coordinates", {}).get("coordinate", [])
            closure = flow_info.get("roadClosure", False)

            if not coords or len(coords) < 2 or freeflow <= 0:
                continue

            # Dedup
            seg_key = (round(coords[0].get("latitude", 0), 4),
                       round(coords[0].get("longitude", 0), 4),
                       round(coords[-1].get("latitude", 0), 4),
                       round(coords[-1].get("longitude", 0), 4))
            if seg_key in seen:
                continue
            seen.add(seg_key)

            congestion = max(0, 1 - (current / freeflow))

            flow_data.append({
                "current_speed_kmph": current,
                "free_flow_speed_kmph": freeflow,
                "congestion_ratio": round(congestion, 3),
                "road_closure": closure,
                "coordinates": [
                    {"lat": c.get("latitude"), "lon": c.get("longitude")}
                    for c in coords
                ],
            })

        except:
            continue

        time.sleep(0.1)

    # Calculate average congestion
    if flow_data:
        avg_congestion = sum(s['congestion_ratio'] for s in flow_data) / len(flow_data)
    else:
        avg_congestion = 0

    return round(avg_congestion, 3), flow_data, api_calls


def apply_real_speeds_to_sumo(flow_data):
    """Apply TomTom speeds to SUMO edges in the running simulation."""
    if not simulation_running:
        return 0

    applied = 0

    try:
        all_edges = traci.edge.getIDList()
        edge_positions = {}

        # Get midpoint of each edge
        for eid in all_edges:
            if eid.startswith(':'):
                continue
            try:
                shape = traci.edge.getShape(eid)
                if shape:
                    mid = shape[len(shape) // 2]
                    x, y = mid
                    lon, lat = traci.simulation.convertGeo(x, y)
                    edge_positions[eid] = (lat, lon)
            except:
                continue

        # Build flow midpoints
        flow_points = []
        for seg in flow_data:
            coords = seg['coordinates']
            if coords:
                mid = coords[len(coords) // 2]
                flow_points.append({
                    'lat': mid['lat'], 'lon': mid['lon'],
                    'speed_kmph': seg['current_speed_kmph'],
                    'congestion': seg['congestion_ratio'],
                })

        if not flow_points:
            return 0

        # Match each edge to nearest flow point
        for eid, (elat, elon) in edge_positions.items():
            best_dist = float('inf')
            best_flow = None

            for fp in flow_points:
                dlat = elat - fp['lat']
                dlon = elon - fp['lon']
                dist = math.sqrt(dlat**2 + dlon**2) * 111000  # approx meters
                if dist < best_dist:
                    best_dist = dist
                    best_flow = fp

            if best_flow and best_dist < 200:
                new_speed = max(1.0, best_flow['speed_kmph'] / 3.6)
                try:
                    traci.edge.setMaxSpeed(eid, new_speed)
                    applied += 1
                except:
                    pass

    except Exception as e:
        print(f"  ⚠️ Speed apply error: {e}")

    return applied


def inject_vehicles_for_congestion(target_congestion, current_congestion):
    """
    Inject extra vehicles to boost congestion to target level.
    
    Logic:
      - If current = 20%, target = 45% → need to add 25% more load
      - More vehicles on high-priority roads = more congestion
      - Vehicles injected with 'now' departure = immediate
    """
    global injected_vehicle_counter

    if not simulation_running:
        return 0

    if current_congestion >= target_congestion:
        return 0  # Already at or above target

    gap = target_congestion - current_congestion  # e.g., 0.25

    # Calculate vehicles needed
    # Rule of thumb: each 100 vehicles adds ~10% congestion on this network
    vehicles_needed = int(gap * 1000)  # 0.25 gap = 250 vehicles
    vehicles_needed = max(50, min(vehicles_needed, 500))  # Cap 50-500

    print(f"  📈 Congestion boost: {current_congestion:.1%} → {target_congestion:.1%}")
    print(f"     Gap: {gap:.1%} → Injecting {vehicles_needed} vehicles")

    try:
        all_edges = traci.edge.getIDList()
        valid_edges = [e for e in all_edges
                       if not e.startswith(':') and not e.startswith('_')]

        if len(valid_edges) < 10:
            print(f"  ⚠️ Not enough edges")
            return 0

        # Weight edges by priority/lanes for realistic traffic
        weighted = []
        for eid in valid_edges:
            try:
                lane_count = traci.edge.getLaneNumber(eid)
                # More lanes = more traffic weight
                weight = max(1, lane_count * 2)
                weighted.append((eid, weight))
            except:
                weighted.append((eid, 1))

        total_w = sum(w for _, w in weighted)
        edge_ids = [e for e, _ in weighted]
        edge_weights = [w / total_w for _, w in weighted]

        injected = 0
        for i in range(vehicles_needed):
            src = random.choices(edge_ids, weights=edge_weights, k=1)[0]
            dst = random.choices(edge_ids, weights=edge_weights, k=1)[0]

            attempts = 0
            while dst == src and attempts < 10:
                dst = random.choices(edge_ids, weights=edge_weights, k=1)[0]
                attempts += 1

            if dst == src:
                continue

            injected_vehicle_counter += 1
            veh_id = f"boost_{injected_vehicle_counter}"

            try:
                traci.vehicle.add(
                    vehID=veh_id,
                    routeID='',
                    typeID='DEFAULT_VEHTYPE',
                    depart='now',
                    departLane='best',
                    departSpeed='max'
                )
                traci.vehicle.setRoute(veh_id, [src, dst])
                traci.vehicle.setColor(veh_id, (51, 130, 246, 255))  # Blue
                injected += 1
            except:
                # Route might be invalid — skip
                continue

        print(f"  ✅ Injected {injected}/{vehicles_needed} vehicles")
        return injected

    except Exception as e:
        print(f"  ⚠️ Injection error: {e}")
        return 0


# ============================================================================
# HELPERS
# ============================================================================

def get_route(source_idx, dest_idx):
    for route in ROUTE_DATA['routes']:
        if route['source_index'] == source_idx and route['dest_index'] == dest_idx:
            return route
    return None


def get_distance(x1, y1, x2, y2):
    return math.sqrt((x2 - x1)**2 + (y2 - y1)**2)


def get_upcoming_traffic_lights(vehicle_id):
    try:
        x, y = traci.vehicle.getPosition(vehicle_id)
        route = traci.vehicle.getRoute(vehicle_id)
        route_index = traci.vehicle.getRouteIndex(vehicle_id)
        upcoming_edges = route[route_index:route_index + 5]
        upcoming_signals = []

        for tl_id in traci.trafficlight.getIDList():
            try:
                for lane in traci.trafficlight.getControlledLanes(tl_id):
                    edge_id = traci.lane.getEdgeID(lane)
                    if edge_id in upcoming_edges:
                        lane_shape = traci.lane.getShape(lane)
                        if lane_shape:
                            tl_x, tl_y = lane_shape[-1]
                            dist = get_distance(x, y, tl_x, tl_y)
                            if dist <= GREEN_CORRIDOR_DETECTION_DISTANCE:
                                upcoming_signals.append({
                                    'tl_id': tl_id, 'distance': dist, 'edge': edge_id
                                })
                                break
            except:
                continue

        upcoming_signals.sort(key=lambda s: s['distance'])
        return upcoming_signals[:3]
    except:
        return []


def set_green_corridor(tl_id, ambulance_id):
    try:
        state = traci.trafficlight.getRedYellowGreenState(tl_id)
        green = state.replace('r','G').replace('R','G').replace('y','G').replace('Y','G')
        traci.trafficlight.setRedYellowGreenState(tl_id, green)

        if ambulance_id not in ambulance_controlled_signals:
            ambulance_controlled_signals[ambulance_id] = {}
        if tl_id not in ambulance_controlled_signals[ambulance_id]:
            ambulance_controlled_signals[ambulance_id][tl_id] = {
                'timestamp': time.time(), 'released': False
            }
        else:
            ambulance_controlled_signals[ambulance_id][tl_id]['timestamp'] = time.time()
        return True
    except:
        return False


def release_signal(tl_id, ambulance_id):
    try:
        traci.trafficlight.setProgram(tl_id, "0")
        if ambulance_id in ambulance_controlled_signals:
            if tl_id in ambulance_controlled_signals[ambulance_id]:
                ambulance_controlled_signals[ambulance_id][tl_id]['released'] = True
        return True
    except:
        return False


def release_ambulance_signals(ambulance_id):
    if ambulance_id not in ambulance_controlled_signals:
        return
    released = 0
    for tl_id, info in ambulance_controlled_signals[ambulance_id].items():
        if not info['released']:
            if release_signal(tl_id, ambulance_id):
                released += 1
    if released:
        print(f"  ⚪ Released {released} signals for {ambulance_id}")
    del ambulance_controlled_signals[ambulance_id]


def check_and_release_passed_signals(ambulance_id):
    if ambulance_id not in ambulance_controlled_signals:
        return
    try:
        now = time.time()
        for tl_id, info in list(ambulance_controlled_signals[ambulance_id].items()):
            if info['released']:
                continue
            if now - info['timestamp'] > SIGNAL_RELEASE_DELAY:
                release_signal(tl_id, ambulance_id)
    except:
        pass


# ============================================================================
# APPROACH MAP (v3.3 logic — group by FROM_NODE)
# ============================================================================

def build_approach_map(tl_id):
    if tl_id in CACHED_TL_APPROACHES:
        return CACHED_TL_APPROACHES[tl_id]

    node_groups = {}
    try:
        controlled_links = traci.trafficlight.getControlledLinks(tl_id)
        for link_idx, link_set in enumerate(controlled_links):
            if not link_set:
                continue
            try:
                link = link_set[0]
                if len(link) < 1:
                    continue
                incoming_lane = link[0]
                edge_id = traci.lane.getEdgeID(incoming_lane)
                if edge_id.startswith(':'):
                    continue

                from_node = EDGE_FROM_NODE.get(edge_id, f"_edge_{edge_id}")

                if from_node not in node_groups:
                    lane_shape = traci.lane.getShape(incoming_lane)
                    if not lane_shape:
                        continue
                    x, y = lane_shape[-1]
                    lon, lat = traci.simulation.convertGeo(x, y)
                    node_groups[from_node] = {
                        'from_node': from_node,
                        'incoming_lane': incoming_lane,
                        'link_indices': [link_idx],
                        'edges': [edge_id],
                        'lat': round(lat, 6),
                        'lon': round(lon, 6),
                    }
                else:
                    node_groups[from_node]['link_indices'].append(link_idx)
                    if edge_id not in node_groups[from_node]['edges']:
                        node_groups[from_node]['edges'].append(edge_id)
            except:
                continue
    except:
        CACHED_TL_APPROACHES[tl_id] = []
        return []

    approaches = list(node_groups.values())
    if len(approaches) > 6:
        approaches.sort(key=lambda a: len(a['link_indices']), reverse=True)
        approaches = approaches[:6]

    CACHED_TL_APPROACHES[tl_id] = approaches
    return approaches


def get_approach_color(state_str, link_indices):
    has_green = has_yellow = False
    for idx in link_indices:
        if idx >= len(state_str):
            continue
        char = state_str[idx]
        if char in ('G', 'g'):
            has_green = True
        elif char in ('Y', 'y'):
            has_yellow = True

    if has_green: return 'green', 'G'
    elif has_yellow: return 'yellow', 'Y'
    else: return 'red', 'r'


# ============================================================================
# SUMO SIMULATION
# ============================================================================

def run_sumo_simulation():
    global simulation_running, active_ambulances

    print("\n🚗 Starting SUMO simulation...")

    try:
        traci.start([
            SUMO_BINARY, '-c', SUMO_CONFIG,
            '--start', '--quit-on-end',
            '--step-length', '1',
            '--no-warnings', 'true',
            '--time-to-teleport', '300'
        ])

        print("  ✅ SUMO started")
        simulation_running = True
        step_count = 0
        last_log = time.time()
        approach_log_done = False
        CACHED_TL_APPROACHES.clear()

        while simulation_running and step_count < 36000:
            traci.simulationStep()
            step_count += 1
            time.sleep(0.5)

            sim_time = traci.simulation.getTime()
            vehicle_ids = traci.vehicle.getIDList()

            # ================================================================
            # GREEN CORRIDOR
            # ================================================================

            active_green_corridors = set()

            for amb_id in list(active_ambulances.keys()):
                if amb_id in vehicle_ids:
                    try:
                        road = traci.vehicle.getRoadID(amb_id)
                        speed = traci.vehicle.getSpeed(amb_id)
                        route = traci.vehicle.getRoute(amb_id)
                        route_index = traci.vehicle.getRouteIndex(amb_id)

                        active_ambulances[amb_id]['current_road'] = road
                        active_ambulances[amb_id]['speed'] = round(speed * 3.6, 1)
                        active_ambulances[amb_id]['status'] = 'en_route'
                        active_ambulances[amb_id]['progress'] = f"{route_index + 1}/{len(route)}"
                        active_ambulances[amb_id]['progress_percent'] = round(
                            (route_index + 1) / len(route) * 100, 1)

                        if active_ambulances[amb_id]['mode'] == 'green_corridor':
                            for sig in get_upcoming_traffic_lights(amb_id):
                                if sig['distance'] <= SIGNAL_ACTIVATION_DISTANCE:
                                    if set_green_corridor(sig['tl_id'], amb_id):
                                        active_green_corridors.add(sig['tl_id'])
                            check_and_release_passed_signals(amb_id)
                    except:
                        pass
                else:
                    if amb_id in active_ambulances:
                        if active_ambulances[amb_id].get('status') != 'arrived':
                            print(f"\n🏥 {amb_id} ARRIVED!")
                            spawn_time = active_ambulances[amb_id]['spawn_time']
                            arrival_time = time.time()
                            duration = arrival_time - spawn_time

                            active_ambulances[amb_id]['status'] = 'arrived'
                            active_ambulances[amb_id]['arrival_time'] = arrival_time
                            release_ambulance_signals(amb_id)

                            socketio.emit('ambulance_completed', {
                                'ambulance_id': amb_id,
                                'source': active_ambulances[amb_id]['source_name'],
                                'destination': active_ambulances[amb_id]['dest_name'],
                                'mode': active_ambulances[amb_id]['mode'],
                                'start_time': int(spawn_time * 1000),
                                'end_time': int(arrival_time * 1000),
                                'travel_time': round(duration, 2),
                                'route_length': active_ambulances[amb_id]['route_length']
                            })
                            print(f"  ✅ {int(duration//60)}m {int(duration%60)}s")

                        if time.time() - active_ambulances[amb_id].get('arrival_time', time.time()) > 10:
                            del active_ambulances[amb_id]

            # ================================================================
            # TRAFFIC LIGHTS
            # ================================================================

            tl_data = {}
            for tl_id in traci.trafficlight.getIDList()[:200]:
                try:
                    state = traci.trafficlight.getRedYellowGreenState(tl_id)
                    phase = traci.trafficlight.getPhase(tl_id)
                    approaches = build_approach_map(tl_id)
                    if not approaches:
                        continue
                    total = len(approaches)

                    for idx, approach in enumerate(approaches):
                        color, state_char = get_approach_color(state, approach['link_indices'])
                        signal_id = f"{tl_id}_a{idx}"
                        tl_data[signal_id] = {
                            'id': signal_id, 'cluster_id': tl_id,
                            'approach_index': idx, 'total_approaches': total,
                            'state': state_char, 'color': color, 'phase': phase,
                            'lat': approach['lat'], 'lon': approach['lon'],
                            'incoming_lane': approach['incoming_lane'],
                            'from_node': approach['from_node'],
                            'edges': approach['edges'][:3],
                            'num_links': len(approach['link_indices']),
                            'green_corridor_active': tl_id in active_green_corridors
                        }
                except:
                    continue

            if not approach_log_done and CACHED_TL_APPROACHES:
                counts = {}
                for approaches in CACHED_TL_APPROACHES.values():
                    n = len(approaches)
                    counts[n] = counts.get(n, 0) + 1
                print(f"\n  📊 Junction distribution:")
                for n in sorted(counts.keys()):
                    label = {1:"1-way",2:"2-way",3:"T-junction",4:"4-way",5:"5-way",6:"6-way"}.get(n,f"{n}-way")
                    print(f"     {label}: {counts[n]}")
                print(f"     Total signals: {len(tl_data)}")
                approach_log_done = True

            # ================================================================
            # VEHICLES
            # ================================================================

            vehicles_data = []
            for veh_id in vehicle_ids:
                try:
                    x, y = traci.vehicle.getPosition(veh_id)
                    lon, lat = traci.simulation.convertGeo(x, y)
                    speed = traci.vehicle.getSpeed(veh_id)
                    angle = traci.vehicle.getAngle(veh_id)
                    vc = traci.vehicle.getColor(veh_id)
                    vehicles_data.append({
                        'id': veh_id, 'lat': lat, 'lon': lon,
                        'speed': round(speed * 3.6, 1), 'angle': angle,
                        'is_ambulance': veh_id in active_ambulances,
                        'color': {'r': vc[0], 'g': vc[1], 'b': vc[2]}
                    })
                except:
                    continue

            ambulance_data = [{
                'id': k, 'source': v['source_name'],
                'destination': v['dest_name'], 'mode': v['mode'],
                'status': v.get('status', 'spawning'),
                'speed': v.get('speed', 0),
                'progress': v.get('progress', '0/0'),
                'progress_percent': v.get('progress_percent', 0)
            } for k, v in active_ambulances.items()]

            # ================================================================
            # EMIT
            # ================================================================

            socketio.emit('simulation_update', {
                'sim_time': sim_time, 'step': step_count,
                'vehicle_count': len(vehicles_data),
                'vehicles': vehicles_data,
                'traffic_lights': tl_data,
                'ambulances': ambulance_data,
                'active_green_corridors': len(active_green_corridors),
                'last_refresh': last_refresh_data,
            })

            if time.time() - last_log > 10:
                greens = sum(1 for s in tl_data.values() if s['color'] == 'green')
                reds = sum(1 for s in tl_data.values() if s['color'] == 'red')
                yellows = sum(1 for s in tl_data.values() if s['color'] == 'yellow')
                print(f"  ⏱️ {int(sim_time)}s | "
                      f"Veh: {len(vehicles_data)} | "
                      f"Amb: {len(active_ambulances)} | "
                      f"Signals: {len(tl_data)} "
                      f"(🟢{greens} 🔴{reds} 🟡{yellows}) | "
                      f"Corridors: {len(active_green_corridors)}")
                last_log = time.time()

        print("\n✅ Simulation completed")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        simulation_running = False
        for amb_id in list(ambulance_controlled_signals.keys()):
            release_ambulance_signals(amb_id)
        try:
            traci.close()
        except:
            pass
        socketio.emit('simulation_ended', {'message': 'Simulation completed'})

# ============================================================================
# REST API
# ============================================================================

@app.route('/')
def index():
    return jsonify({
        'name': 'Smart Green Corridor v3.4',
        'simulation_running': simulation_running,
        'active_ambulances': len(active_ambulances),
        'hospitals': len(HOSPITALS),
        'last_refresh': last_refresh_data,
    })

@app.route('/api/hospitals', methods=['GET'])
def get_hospitals():
    return jsonify(HOSPITALS)

@app.route('/api/traffic-flow', methods=['GET'])
def get_traffic_flow():
    return jsonify(TRAFFIC_FLOW)

@app.route('/api/edge-traffic', methods=['GET'])
def get_edge_traffic():
    return jsonify(EDGE_TRAFFIC)

@app.route('/api/incidents', methods=['GET'])
def get_incidents():
    return jsonify(TRAFFIC_INCIDENTS)

@app.route('/api/traffic-lights-ref', methods=['GET'])
def get_traffic_lights_ref():
    return jsonify(TRAFFIC_LIGHTS_REF)

# ============================================================================
# REFRESH TRAFFIC ENDPOINT
# ============================================================================

@app.route('/api/refresh-traffic', methods=['POST'])
def refresh_traffic():
    """
    Fetch real-time TomTom data, apply to simulation,
    and boost to 45% congestion if needed.
    """
    global last_refresh_data, TRAFFIC_FLOW

    if not simulation_running:
        return jsonify({'error': 'Simulation not running'}), 400

    print(f"\n{'='*60}")
    print(f"🔄 REFRESH TRAFFIC — Fetching real-time TomTom data...")
    print(f"{'='*60}")

    # Step 1: Fetch live traffic
    try:
        avg_congestion, flow_data, api_calls = fetch_live_traffic(sample_count=50)
    except Exception as e:
        print(f"  ❌ Fetch failed: {e}")

        # Fallback: use cached data
        if TRAFFIC_FLOW:
            print(f"  📦 Using cached data ({len(TRAFFIC_FLOW)} segments)")
            flow_data = TRAFFIC_FLOW
            avg_congestion = sum(s.get('congestion_ratio', 0) for s in flow_data) / len(flow_data)
            api_calls = 0
        else:
            return jsonify({'error': f'TomTom fetch failed: {e}'}), 500

    print(f"  📊 Real-time congestion: {avg_congestion:.1%}")
    print(f"  📡 Flow segments: {len(flow_data)}")
    print(f"  📞 API calls: {api_calls}")

    # Step 2: Apply real speeds to SUMO
    applied = apply_real_speeds_to_sumo(flow_data)
    print(f"  🚗 Applied speeds to {applied} edges")

    # Step 3: Check if we need to boost
    vehicles_injected = 0
    boosted = False

    if avg_congestion < MIN_CONGESTION_TARGET:
        print(f"\n  ⚠️ Congestion {avg_congestion:.1%} < target {MIN_CONGESTION_TARGET:.0%}")
        vehicles_injected = inject_vehicles_for_congestion(
            MIN_CONGESTION_TARGET, avg_congestion
        )
        boosted = True
    else:
        print(f"\n  ✅ Real congestion {avg_congestion:.1%} ≥ target {MIN_CONGESTION_TARGET:.0%}")
        print(f"     Using real traffic as-is — no boost needed")

    # Step 4: Update cache
    TRAFFIC_FLOW = flow_data

    # Save to disk
    try:
        with open(os.path.join(DATA_DIR, 'tomtom_traffic_flow.json'), 'w') as f:
            json.dump(flow_data, f, indent=2)
    except:
        pass

    # Step 5: Update state
    try:
        current_vehicles = traci.vehicle.getIDCount()
    except:
        current_vehicles = 0

    last_refresh_data = {
        'timestamp': time.time(),
        'timestamp_str': time.strftime('%H:%M:%S'),
        'real_congestion': avg_congestion,
        'target_congestion': MIN_CONGESTION_TARGET,
        'vehicles_injected': vehicles_injected,
        'flow_segments': len(flow_data),
        'api_calls': api_calls,
        'edges_updated': applied,
        'boosted': boosted,
        'total_vehicles': current_vehicles,
    }

    print(f"\n  📊 Refresh Summary:")
    print(f"     Real congestion: {avg_congestion:.1%}")
    print(f"     Boosted: {'Yes' if boosted else 'No'}")
    print(f"     Vehicles injected: {vehicles_injected}")
    print(f"     Edges updated: {applied}")
    print(f"     Total vehicles: {current_vehicles}")
    print(f"{'='*60}")

    # Notify frontend
    socketio.emit('traffic_refreshed', last_refresh_data)

    return jsonify({
        'success': True,
        **last_refresh_data
    })

# ============================================================================
# SPAWN AMBULANCE
# ============================================================================

@app.route('/api/spawn-ambulance', methods=['POST'])
def spawn_ambulance():
    global active_ambulances, ambulance_counter

    if not simulation_running:
        return jsonify({'error': 'Simulation not running'}), 400

    try:
        vc = traci.vehicle.getIDCount()
        if vc < MIN_VEHICLES:
            return jsonify({
                'error': 'Waiting for traffic',
                'current_vehicles': vc, 'required_vehicles': MIN_VEHICLES
            }), 400
    except:
        pass

    data = request.json
    src = data.get('source')
    dst = data.get('destination')
    mode = data.get('mode', 'normal')

    if src is None or dst is None:
        return jsonify({'error': 'Source and destination required'}), 400
    if src == dst:
        return jsonify({'error': 'Same source and destination'}), 400

    route_info = get_route(src, dst)
    if not route_info:
        return jsonify({'error': f'No route {src} → {dst}'}), 400

    route_edges = route_info['route_edges']
    ambulance_counter += 1
    amb_id = f"ambulance_{ambulance_counter}_{int(time.time())}"

    print(f"\n{'='*60}")
    print(f"🚑 {route_info['source_name']} → {route_info['dest_name']}")
    print(f"   Mode: {mode} | ID: {amb_id}")

    try:
        traci.vehicle.add(vehID=amb_id, routeID='', typeID='DEFAULT_VEHTYPE',
                          depart='now', departLane='best', departSpeed='max')
        traci.vehicle.setRoute(amb_id, route_edges)

        if mode == 'green_corridor':
            traci.vehicle.setColor(amb_id, (0, 255, 0, 255))
            traci.vehicle.setSpeedMode(amb_id, 32)
            traci.vehicle.setMaxSpeed(amb_id, 30)
            print(f"   🟢 GREEN CORRIDOR")
        else:
            traci.vehicle.setColor(amb_id, (255, 0, 0, 255))
            traci.vehicle.setMaxSpeed(amb_id, 25)
            print(f"   🔴 NORMAL")

        active_ambulances[amb_id] = {
            'source_idx': src, 'dest_idx': dst,
            'source_name': route_info['source_name'],
            'dest_name': route_info['dest_name'],
            'mode': mode, 'spawn_time': time.time(),
            'status': 'spawned', 'route': route_edges,
            'route_length': len(route_edges)
        }

        print(f"   ✅ Route: {len(route_edges)} edges")
        print(f"{'='*60}")

        socketio.emit('ambulance_spawned', {
            'ambulance_id': amb_id,
            'source': route_info['source_name'],
            'destination': route_info['dest_name'], 'mode': mode
        })

        return jsonify({
            'success': True, 'ambulance_id': amb_id,
            'source': route_info['source_name'],
            'destination': route_info['dest_name'],
            'mode': mode, 'route_length': len(route_edges),
        })
    except Exception as e:
        print(f"   ❌ {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/ambulances', methods=['GET'])
def get_ambulances():
    return jsonify([{
        'id': k, 'source': v['source_name'], 'destination': v['dest_name'],
        'mode': v['mode'], 'status': v.get('status', 'unknown'),
        'speed': v.get('speed', 0), 'progress': v.get('progress', '0/0'),
        'progress_percent': v.get('progress_percent', 0)
    } for k, v in active_ambulances.items()])

@app.route('/api/simulation/start', methods=['POST'])
def start_simulation():
    global simulation_running, simulation_thread
    if simulation_running:
        return jsonify({'error': 'Already running'}), 400
    simulation_thread = threading.Thread(target=run_sumo_simulation, daemon=True)
    simulation_thread.start()
    return jsonify({'message': 'Started'})

@app.route('/api/simulation/stop', methods=['POST'])
def stop_simulation():
    global simulation_running
    if not simulation_running:
        return jsonify({'error': 'Not running'}), 400
    simulation_running = False
    return jsonify({'message': 'Stopping'})

@app.route('/api/simulation/status', methods=['GET'])
def get_status():
    try:
        vc = traci.vehicle.getIDCount() if simulation_running else 0
    except:
        vc = 0
    return jsonify({
        'running': simulation_running, 'vehicle_count': vc,
        'ready_for_ambulances': vc >= MIN_VEHICLES,
        'min_vehicles_required': MIN_VEHICLES,
        'connected_clients': len(connected_clients),
        'active_ambulances': len(active_ambulances),
        'last_refresh': last_refresh_data,
    })

# ============================================================================
# WEBSOCKET
# ============================================================================

@socketio.on('connect')
def handle_connect():
    cid = request.sid
    connected_clients.add(cid)
    print(f"  ✅ Client: {cid[:8]}... ({len(connected_clients)} total)")
    emit('connection_response', {
        'status': 'connected', 'client_id': cid,
        'simulation_running': simulation_running,
        'last_refresh': last_refresh_data,
    })

@socketio.on('disconnect')
def handle_disconnect():
    connected_clients.discard(request.sid)

@socketio.on('request_simulation_start')
def handle_start():
    global simulation_running, simulation_thread
    if not simulation_running:
        simulation_thread = threading.Thread(target=run_sumo_simulation, daemon=True)
        simulation_thread.start()
        emit('simulation_started', {'message': 'Started'}, broadcast=True)

@socketio.on('request_simulation_stop')
def handle_stop():
    global simulation_running
    if simulation_running:
        simulation_running = False
        emit('simulation_stopped', {'message': 'Stopping'}, broadcast=True)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print(f"\n{'='*60}")
    print(f"🚀 SMART GREEN CORRIDOR v3.4")
    print(f"{'='*60}")
    print(f"  Hospitals:        {len(HOSPITALS)}")
    print(f"  Routes:           {ROUTE_DATA['assigned_routes']}")
    print(f"  Edge→Node:        {len(EDGE_FROM_NODE)}")
    print(f"  Traffic segments: {len(TRAFFIC_FLOW)}")
    print(f"  Min congestion:   {MIN_CONGESTION_TARGET:.0%}")
    print(f"\n  Backend:  http://localhost:5000")
    print(f"  Frontend: http://localhost:3000")
    print(f"\n🔄 Refresh Traffic:")
    print(f"   • Click 'Refresh Traffic' on frontend")
    print(f"   • Fetches real-time TomTom data (~50 API calls)")
    print(f"   • If congestion < {MIN_CONGESTION_TARGET:.0%} → injects vehicles")
    print(f"   • If congestion ≥ {MIN_CONGESTION_TARGET:.0%} → uses real data")
    print(f"   • Real speeds applied to SUMO edges live")
    print(f"\n⏹️  Ctrl+C to stop")
    print(f"{'='*60}\n")

    try:
        socketio.run(app, host='0.0.0.0', port=5000,
                     debug=False, use_reloader=False,
                     allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n⏹️ Stopped")
        simulation_running = False
        for a in list(ambulance_controlled_signals.keys()):
            release_ambulance_signals(a)
        try:
            traci.close()
        except:
            pass