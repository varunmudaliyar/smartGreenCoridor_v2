import React from 'react';

function AmbulanceList({ ambulances, onZoomToAmbulance }) {
  if (!ambulances || ambulances.length === 0) return null;

  return (
    <div className="ambulance-list">
      <h3>🚑 Active Ambulances</h3>
      {ambulances.map(amb => (
        <div key={amb.id} className={`amb-card ${amb.mode === 'green_corridor' ? 'green' : 'normal'}`}>
          <div className="amb-header">
            <span className="amb-mode">
              {amb.mode === 'green_corridor' ? '🟢' : '🔴'}
            </span>
            <span className="amb-status">{amb.status}</span>
          </div>
          <div className="amb-route">
            {amb.source} → {amb.destination}
          </div>
          <div className="amb-info">
            <span>🏎️ {amb.speed} km/h</span>
            <span>📍 {amb.progress}</span>
          </div>
          <div className="amb-progress-bar">
            <div
              className="amb-progress-fill"
              style={{ width: `${amb.progress_percent || 0}%` }}
            />
          </div>
          <button className="btn-zoom" onClick={() => onZoomToAmbulance(amb.id)}>
            🔍 Track
          </button>
        </div>
      ))}
    </div>
  );
}

export default AmbulanceList;