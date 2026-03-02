import React, { useState, useEffect } from 'react';

function AmbulanceHistory({ isOpen, onClose, refreshTrigger }) {
  const [history, setHistory] = useState([]);

  useEffect(() => {
    const stored = localStorage.getItem('ambulance_history');
    if (stored) setHistory(JSON.parse(stored).reverse());
  }, [isOpen, refreshTrigger]);

  const clearHistory = () => {
    localStorage.removeItem('ambulance_history');
    setHistory([]);
  };

  if (!isOpen) return null;

  return (
    <div className="history-overlay">
      <div className="history-panel">
        <div className="history-header">
          <h2>📋 Trip History</h2>
          <div>
            <button onClick={clearHistory} className="btn-clear">🗑️ Clear</button>
            <button onClick={onClose} className="btn-close">✕</button>
          </div>
        </div>

        {history.length === 0 ? (
          <div className="history-empty">No trips recorded yet</div>
        ) : (
          <div className="history-list">
            {history.map((entry, idx) => {
              const m = Math.floor(entry.travelTime / 60);
              const s = Math.floor(entry.travelTime % 60);
              return (
                <div key={idx} className={`history-card ${entry.mode}`}>
                  <div className="history-mode">
                    {entry.mode === 'green_corridor' ? '🟢 Green Corridor' : '🔴 Normal'}
                  </div>
                  <div className="history-route">
                    {entry.source} → {entry.destination}
                  </div>
                  <div className="history-time">
                    ⏱️ {m}m {s}s | 📍 {entry.routeLength} edges
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default AmbulanceHistory;