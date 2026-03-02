import React, { memo, useEffect, useMemo } from 'react';
import { MapContainer, TileLayer, Marker, Popup, CircleMarker, useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

const TOMTOM_API_KEY = 'mPtfLSFK8MXJHzOuvE6CJJKYn56Hcw2v';

delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png',
  iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png',
});

const hospitalIcon = new L.Icon({
  iconUrl: 'https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-2x-red.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png',
  iconSize: [25, 41], iconAnchor: [12, 41], popupAnchor: [1, -34], shadowSize: [41, 41]
});

const createAmbulanceIcon = (isGreen) => {
  const color = isGreen ? '#10b981' : '#ef4444';
  const pulse = isGreen ? '16,185,129' : '239,68,68';
  return L.divIcon({
    className: 'custom-ambulance-icon',
    html: `<div style="position:relative;width:40px;height:40px;">
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:40px;height:40px;background:rgba(${pulse},0.3);border-radius:50%;animation:ambulance-pulse 1.5s infinite;"></div>
      <svg style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);filter:drop-shadow(0 2px 4px rgba(0,0,0,0.3));" width="32" height="32" viewBox="0 0 24 24" fill="none">
        <rect x="3" y="10" width="18" height="8" rx="1" fill="${color}"/>
        <rect x="14" y="11" width="6" height="4" fill="#fff" opacity="0.8"/>
        <g transform="translate(7,12)"><rect x="1.5" y="0" width="1" height="4" fill="#fff"/><rect x="0" y="1.5" width="4" height="1" fill="#fff"/></g>
        <circle cx="7" cy="18" r="1.5" fill="#333"/><circle cx="17" cy="18" r="1.5" fill="#333"/>
        <rect x="8" y="8" width="8" height="1" rx="0.5" fill="${color}" opacity="0.9"><animate attributeName="opacity" values="0.9;0.3;0.9" dur="0.8s" repeatCount="indefinite"/></rect>
      </svg></div>`,
    iconSize: [40, 40], iconAnchor: [20, 20], popupAnchor: [0, -20]
  });
};

const MapController = ({ zoomToAmbulance }) => {
  const map = useMap();
  useEffect(() => {
    if (zoomToAmbulance?.lat && zoomToAmbulance?.lon)
      map.flyTo([zoomToAmbulance.lat, zoomToAmbulance.lon], 17, { duration: 1.5 });
  }, [zoomToAmbulance, map]);
  return null;
};

const HospitalMarkers = memo(({ hospitals }) => (
  <>{hospitals.map((h, i) => (
    <Marker key={`h-${h.id}-${i}`} position={[h.lat, h.lon]} icon={hospitalIcon}>
      <Popup>
        <div style={{minWidth:150}}>
          <strong>🏥 {h.name}</strong><br/>
          <small>ID: {i} | ({h.lat.toFixed(4)}, {h.lon.toFixed(4)})</small>
        </div>
      </Popup>
    </Marker>
  ))}</>
));

// ============================================================================
// TRAFFIC LIGHTS — CircleMarker (scales with zoom) + Backend colors only
// ============================================================================

const SIGNAL_COLORS = {
  red: '#ef4444',
  yellow: '#fbbf24',
  green: '#10b981',
  off: '#6b7280',
};

const TrafficLightSignals = ({ trafficLightStates }) => {
  // No grouping, no frontend color logic.
  // Backend already sends 1 signal per approach with correct SUMO color.
  const signals = useMemo(() => {
    return Object.values(trafficLightStates || {});
  }, [trafficLightStates]);

  return (
    <>
      {signals.map(signal => {
        // Use backend color directly — no override
        const fillColor = SIGNAL_COLORS[signal.color] || SIGNAL_COLORS.off;
        const isCorridor = signal.green_corridor_active;

        return (
          <CircleMarker
            key={`${signal.id}-${signal.color}-${signal.phase}`}
            center={[signal.lat, signal.lon]}
            radius={7}
            fillColor={fillColor}
            color={isCorridor ? '#10b981' : '#ffffff'}
            weight={isCorridor ? 3 : 2}
            fillOpacity={0.95}
            opacity={0.9}
          >
            <Popup>
              <div style={{ minWidth: 200, fontFamily: 'Segoe UI, sans-serif' }}>
                <strong style={{ fontSize: 13 }}>🚦 {signal.cluster_id}</strong>
                <div style={{ fontSize: 10, color: '#888', marginTop: 2 }}>
                  Phase {signal.phase} | Approach {signal.approach_index + 1} of {signal.total_approaches}
                </div>
                <hr style={{ margin: '6px 0', border: '1px solid #eee' }} />

                <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '6px 0' }}>
                  <div style={{
                    width: 18, height: 18, borderRadius: '50%',
                    background: fillColor,
                    border: '2px solid white',
                    boxShadow: `0 0 6px ${fillColor}`,
                  }}></div>
                  <span style={{
                    fontWeight: 700, fontSize: 14,
                    textTransform: 'uppercase',
                    color: fillColor,
                  }}>
                    {signal.color}
                  </span>
                </div>

                <div style={{ fontSize: 11, color: '#666', marginTop: 4 }}>
                  <strong>Edge:</strong> {signal.incoming_edge}<br/>
                  <strong>Lane:</strong> <code style={{fontSize:10}}>{signal.incoming_lane}</code><br/>
                  <strong>State char:</strong> <code>{signal.state}</code>
                </div>

                {isCorridor && (
                  <div style={{
                    marginTop: 8, padding: 6, background: '#d1fae5',
                    borderRadius: 4, textAlign: 'center', fontSize: 11,
                    fontWeight: 700, color: '#065f46',
                  }}>
                    🚑 GREEN CORRIDOR ACTIVE
                  </div>
                )}
              </div>
            </Popup>
          </CircleMarker>
        );
      })}
    </>
  );
};

// ============================================================================
// VEHICLES
// ============================================================================

const VehicleMarkers = memo(({ vehicles }) => (
  <>{vehicles.slice(0, 500).map(v => {
    const isAmb = v.is_ambulance;
    const isGreen = isAmb && v.color?.g > 200;

    if (isAmb) return (
      <Marker key={v.id} position={[v.lat, v.lon]} icon={createAmbulanceIcon(isGreen)}>
        <Popup>
          <div style={{minWidth:160}}>
            <h4 style={{margin:0,color:isGreen?'#10b981':'#ef4444'}}>🚑 AMBULANCE</h4>
            <p style={{fontSize:11,margin:'4px 0'}}><strong>ID:</strong> {v.id}</p>
            <p style={{fontSize:11,margin:'4px 0'}}><strong>Speed:</strong> {v.speed} km/h</p>
            <p style={{
              fontSize:10, padding:4,
              background: isGreen ? '#d1fae5' : '#ffebee',
              borderRadius:3,
              color: isGreen ? '#065f46' : '#c62828',
              fontWeight:700, textAlign:'center'
            }}>
              {isGreen ? '✓ GREEN CORRIDOR' : '⚠ EMERGENCY'}
            </p>
          </div>
        </Popup>
      </Marker>
    );

    return (
      <CircleMarker key={v.id} center={[v.lat, v.lon]} radius={4}
        fillColor="#3b82f6" color="white" weight={1} fillOpacity={0.7}>
        <Popup><small>🚗 {v.id} | {v.speed} km/h</small></Popup>
      </CircleMarker>
    );
  })}</>
));

// ============================================================================
// MAP
// ============================================================================

function AmbulanceMap({ hospitals, ambulances, vehicles, trafficLights, trafficFlow, zoomToAmbulance }) {
  return (
    <div className="map-container">
      <MapContainer center={[19.1170, 72.8470]} zoom={14}
        style={{ height: '100%', width: '100%' }} preferCanvas={true}>

        <TileLayer
          url={`https://api.tomtom.com/map/1/tile/basic/night/{z}/{x}/{y}.png?key=${TOMTOM_API_KEY}`}
          attribution='&copy; <a href="https://www.tomtom.com">TomTom</a>'
          maxZoom={20}
        />

        <MapController zoomToAmbulance={zoomToAmbulance} />
        <TrafficLightSignals trafficLightStates={trafficLights} />
        <HospitalMarkers hospitals={hospitals} />
        <VehicleMarkers vehicles={vehicles} />
      </MapContainer>

      <div className="map-signal-count">
        🚦 {Object.keys(trafficLights || {}).length} signals
      </div>
    </div>
  );
}

export default memo(AmbulanceMap);