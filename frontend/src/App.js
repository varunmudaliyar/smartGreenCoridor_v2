import React, { useState, useEffect } from 'react';
import axios from 'axios';
import io from 'socket.io-client';
import AmbulanceMap from './components/AmbulanceMap';
import ControlPanel from './components/ControlPanel';
import AmbulanceList from './components/AmbulanceList';
import AmbulanceHistory from './components/AmbulanceHistory';
import './App.css';

const BACKEND_URL = 'http://localhost:5000';

function App() {
  const [socket, setSocket] = useState(null);
  const [hospitals, setHospitals] = useState([]);
  const [simulationRunning, setSimulationRunning] = useState(false);
  const [vehicleCount, setVehicleCount] = useState(0);
  const [signalCount, setSignalCount] = useState(0);
  const [readyForAmbulances, setReadyForAmbulances] = useState(false);
  const [ambulances, setAmbulances] = useState([]);
  const [vehicles, setVehicles] = useState([]);
  const [trafficLights, setTrafficLights] = useState({});
  const [trafficFlow, setTrafficFlow] = useState([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [zoomToAmbulance, setZoomToAmbulance] = useState(null);
  const [greenCorridors, setGreenCorridors] = useState(0);

  useEffect(() => {
    axios.get(`${BACKEND_URL}/api/hospitals`)
      .then(res => setHospitals(res.data))
      .catch(err => console.error('Failed to load hospitals:', err));

    axios.get(`${BACKEND_URL}/api/traffic-flow`)
      .then(res => setTrafficFlow(res.data))
      .catch(err => console.log('No traffic flow data'));

    const newSocket = io(BACKEND_URL);
    setSocket(newSocket);

    newSocket.on('connect', () => console.log('✅ Connected to backend'));

    newSocket.on('simulation_update', (data) => {
      setVehicleCount(data.vehicle_count);
      const unique = data.ambulances
        ? Array.from(new Map(data.ambulances.map(a => [a.id, a])).values())
        : [];
      setAmbulances(unique);
      setVehicles(data.vehicles || []);
      setTrafficLights(data.traffic_lights || {});
      setSignalCount(Object.keys(data.traffic_lights || {}).length);
      setGreenCorridors(data.active_green_corridors || 0);
    });

    newSocket.on('ambulance_spawned', (data) => console.log('🚑 Spawned:', data));

    newSocket.on('ambulance_completed', (data) => {
      if (data.travel_time < 11) return;
      const existing = localStorage.getItem('ambulance_history');
      const history = existing ? JSON.parse(existing) : [];
      if (history.some(h => h.id === data.ambulance_id)) return;

      const entry = {
        id: data.ambulance_id, startTime: data.start_time,
        endTime: data.end_time, travelTime: data.travel_time,
        source: data.source, destination: data.destination,
        mode: data.mode, routeLength: data.route_length
      };
      history.push(entry);
      localStorage.setItem('ambulance_history', JSON.stringify(history));
      setAmbulances(prev => prev.filter(a => a.id !== data.ambulance_id));
      setHistoryRefresh(prev => prev + 1);

      const m = Math.floor(data.travel_time / 60);
      const s = Math.floor(data.travel_time % 60);
      alert(`🏥 Arrived at ${data.destination}!\nTravel: ${m}m ${s}s`);
    });

    newSocket.on('simulation_started', () => setSimulationRunning(true));
    newSocket.on('simulation_ended', () => setSimulationRunning(false));

    const check = setInterval(() => {
      axios.get(`${BACKEND_URL}/api/simulation/status`)
        .then(res => {
          setSimulationRunning(res.data.running);
          setVehicleCount(res.data.vehicle_count);
          setReadyForAmbulances(res.data.ready_for_ambulances);
        }).catch(() => {});
    }, 2000);

    return () => { clearInterval(check); newSocket.disconnect(); };
  }, []);

  const startSimulation = async () => {
    try {
      await axios.post(`${BACKEND_URL}/api/simulation/start`);
    } catch (err) {
      alert(err.response?.data?.error || 'Failed');
    }
  };

  const spawnAmbulance = async (source, destination, mode) => {
    try {
      await axios.post(`${BACKEND_URL}/api/spawn-ambulance`, { source, destination, mode });
    } catch (err) {
      alert(err.response?.data?.error || 'Failed');
    }
  };

  const handleZoom = (id) => {
    const amb = vehicles.find(v => v.id === id);
    if (amb) setZoomToAmbulance({ id, lat: amb.lat, lon: amb.lon, timestamp: Date.now() });
  };

  return (
    <div className="App">
      <header className="App-header">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}>
          <h1>🚦 Smart Green Corridor</h1>
          <button onClick={() => setHistoryOpen(true)} className="history-btn">📋 History</button>
        </div>
        <div className="status-bar">
          <span className={simulationRunning ? 'status-running' : 'status-stopped'}>
            {simulationRunning ? '🟢 Running' : '🔴 Stopped'}
          </span>
          <span>🚗 {vehicleCount}</span>
          <span>🚑 {ambulances.length}</span>
          <span>🚦 {signalCount}</span>
          <span>💚 Corridors: {greenCorridors}</span>
          {!readyForAmbulances && simulationRunning && (
            <span className="waiting">⏳ Building traffic...</span>
          )}
        </div>
      </header>

      <div className="main-container">
        <ControlPanel
          hospitals={hospitals}
          simulationRunning={simulationRunning}
          readyForAmbulances={readyForAmbulances}
          onStartSimulation={startSimulation}
          onSpawnAmbulance={spawnAmbulance}
        />
        <AmbulanceMap
          hospitals={hospitals} ambulances={ambulances}
          vehicles={vehicles} trafficLights={trafficLights}
          trafficFlow={trafficFlow}
          zoomToAmbulance={zoomToAmbulance}
        />
        <AmbulanceList ambulances={ambulances} onZoomToAmbulance={handleZoom} />
      </div>

      <AmbulanceHistory
        isOpen={historyOpen}
        onClose={() => setHistoryOpen(false)}
        refreshTrigger={historyRefresh}
      />
    </div>
  );
}

export default App;