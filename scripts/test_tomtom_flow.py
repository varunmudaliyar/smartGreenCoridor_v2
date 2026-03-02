#!/usr/bin/env python3
"""
Quick test: Can we reach TomTom Flow API?
"""

import requests

API_KEY = "mPtfLSFK8MXJHzOuvE6CJJKYn56Hcw2v"
FLOW_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"

# Test with a single point in Andheri
params = {
    "key": API_KEY,
    "point": "19.1170,72.8470",
    "unit": "KMPH",
    "thickness": 10,
}

print("🔍 Testing TomTom Flow API...")
print(f"   URL: {FLOW_URL}")
print(f"   Point: 19.1170, 72.8470 (Andheri)")
print(f"   Key: ...{API_KEY[-4:]}")
print()

try:
    print("   Sending request...")
    resp = requests.get(FLOW_URL, params=params, timeout=15)
    print(f"   Status: {resp.status_code}")
    print(f"   Headers: {dict(resp.headers)}")
    print()

    if resp.status_code == 200:
        data = resp.json()
        flow = data.get("flowSegmentData", {})
        print(f"   ✅ SUCCESS!")
        print(f"   Current Speed: {flow.get('currentSpeed', '?')} km/h")
        print(f"   Free Flow Speed: {flow.get('freeFlowSpeed', '?')} km/h")
        print(f"   Confidence: {flow.get('confidence', '?')}")
        print(f"   Road Closure: {flow.get('roadClosure', '?')}")
        coords = flow.get("coordinates", {}).get("coordinate", [])
        print(f"   Coordinates: {len(coords)} points")
    else:
        print(f"   ❌ FAILED!")
        print(f"   Response: {resp.text[:500]}")

except requests.exceptions.Timeout:
    print("   ❌ TIMEOUT — request took more than 15 seconds")
    print("   Check your internet connection")

except requests.exceptions.ConnectionError as e:
    print(f"   ❌ CONNECTION ERROR: {e}")
    print("   Check if you're behind a proxy/firewall")

except Exception as e:
    print(f"   ❌ ERROR: {type(e).__name__}: {e}")