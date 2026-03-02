import React, { useState } from 'react';
import axios from 'axios';

const BACKEND_URL = 'http://localhost:5000';

function ControlPanel({ hospitals, simulationRunning, readyForAmbulances, onStartSimulation, onSpawnAmbulance }) {
  const [source, setSource] = useState('');
  const [destination, setDestination] = useState('');
  const [mode, setMode] = useState('green_corridor');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshResult, setRefreshResult] = useState(null);

  const handleSpawn = () => {
    if (source === '' || destination === '') return alert('Select both hospitals');
    if (source === destination) return alert('Source and destination must differ');
    onSpawnAmbulance(parseInt(source), parseInt(destination), mode);
  };

  const handleRefreshTraffic = async () => {
    setRefreshing(true);
    setRefreshResult(null);
    try {
      const res = await axios.post(`${BACKEND_URL}/api/refresh-traffic`);
      setRefreshResult(res.data);
      console.log('Traffic refreshed:', res.data);
    } catch (err) {
      const msg = err.response?.data?.error || 'Failed to refresh';
      alert(`❌ ${msg}`);
      setRefreshResult(null);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="control-panel">
      <h2>🎛️ Control Panel</h2>

      {!simulationRunning && (
        <button className="btn-start" onClick={onStartSimulation}>
          ▶ Start Simulation
        </button>
      )}

      {simulationRunning && (
        <>
          {/* ====== REFRESH TRAFFIC ====== */}
          <div className="refresh-section">
            <button
              className={`btn-refresh ${refreshing ? 'refreshing' : ''}`}
              onClick={handleRefreshTraffic}
              disabled={refreshing}
            >
              {refreshing ? '🔄 Fetching TomTom...' : '🔄 Refresh Traffic'}
            </button>

            {refreshResult && (
              <div className="refresh-result">
                <div className="refresh-stat">
                  <span className="refresh-label">Real Congestion</span>
                  <span className={`refresh-value ${
                    refreshResult.real_congestion >= 0.45 ? 'high' :
                    refreshResult.real_congestion >= 0.25 ? 'medium' : 'low'
                  }`}>
                    {(refreshResult.real_congestion * 100).toFixed(1)}%
                  </span>
                </div>

                {refreshResult.boosted && (
                  <div className="refresh-boost">
                    <span>📈 Boosted to 45%</span>
                    <span>+{refreshResult.vehicles_injected} vehicles</span>
                  </div>
                )}

                {!refreshResult.boosted && (
                  <div className="refresh-real">
                    ✅ Using real traffic
                  </div>
                )}

                <div className="refresh-details">
                  <small>
                    {refreshResult.flow_segments} segments |
                    {refreshResult.edges_updated} edges |
                    {refreshResult.api_calls} API calls |
                    {refreshResult.total_vehicles} vehicles
                  </small>
                </div>

                {refreshResult.timestamp_str && (
                  <div className="refresh-time">
                    Updated: {refreshResult.timestamp_str}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* ====== SPAWN AMBULANCE ====== */}
          <div className="spawn-section">
            <h3>🚑 Dispatch Ambulance</h3>

            <label>Source Hospital</label>
            <select value={source} onChange={e => setSource(e.target.value)}>
              <option value="">-- Select --</option>
              {hospitals.map((h, i) => (
                <option key={`src-${i}`} value={i}>{h.name}</option>
              ))}
            </select>

            <label>Destination Hospital</label>
            <select value={destination} onChange={e => setDestination(e.target.value)}>
              <option value="">-- Select --</option>
              {hospitals.map((h, i) => (
                <option key={`dst-${i}`} value={i}>{h.name}</option>
              ))}
            </select>

            <label>Mode</label>
            <div className="mode-selector">
              <button
                className={`mode-btn ${mode === 'normal' ? 'active-normal' : ''}`}
                onClick={() => setMode('normal')}
              >
                🔴 Normal
              </button>
              <button
                className={`mode-btn ${mode === 'green_corridor' ? 'active-green' : ''}`}
                onClick={() => setMode('green_corridor')}
              >
                🟢 Green Corridor
              </button>
            </div>

            <button
              className="btn-spawn"
              onClick={handleSpawn}
              disabled={!readyForAmbulances}
            >
              {readyForAmbulances ? '🚑 Dispatch Ambulance' : '⏳ Waiting for traffic...'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default ControlPanel;