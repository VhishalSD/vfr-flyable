// Initialize the Leaflet map and center it on Western Europe.
const map = L.map('map').setView([51.5, 5.0], 6);

// Add the OpenStreetMap tile layer to the map.
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors'
}).addTo(map);

// Flight category colors following standard aviation convention.
const CATEGORY_COLORS = {
    VFR:  '#00b300',
    MVFR: '#0066ff',
    IFR:  '#cc0000',
    LIFR: '#990099'
};

// Return a colored div icon for a given flight category.
function createIcon(fltCat) {
    const color = CATEGORY_COLORS[fltCat] ?? '#999999';
    const label = fltCat ?? '?';

    return L.divIcon({
        className: '',
        html: `<div class="marker" style="background:${color}">${label}</div>`,
        iconSize: [48, 24],
        iconAnchor: [24, 12]
    });
}

// Load airport data and METAR data, then combine and place markers.
Promise.all([
    fetch('data/airports.json').then(r => r.json()),
    fetch('data/metar-test.json').then(r => r.json())
])
    .then(([airports, metarList]) => {
        // Build a lookup map: icaoId -> metar object.
        const metarMap = {};
        metarList.forEach(metar => {
            metarMap[metar.icaoId] = metar;
        });

        airports.forEach(airport => {
            const metar = metarMap[airport.icao];
            const fltCat = metar?.fltCat ?? 'UNKNOWN';
            const icon = createIcon(fltCat);

            L.marker([airport.lat, airport.lon], { icon })
                .addTo(map)
                .bindPopup(`
                    <strong>${airport.icao}</strong><br>
                    ${airport.name}<br>
                    ${airport.country}<br><br>
                    <strong>Category:</strong> ${fltCat}<br>
                    <strong>Wind:</strong> ${metar?.wdir ?? '—'}° / ${metar?.wspd ?? '—'} kt<br>
                    <strong>Visibility:</strong> ${metar?.visib ?? '—'} SM<br>
                    <strong>Ceiling:</strong> ${metar?.ceiling ?? '—'} ft<br>
                    <strong>QNH:</strong> ${metar?.altim ?? '—'} hPa<br><br>
                    <strong>METAR:</strong><br>
                    <small>${metar?.rawOb ?? 'No METAR data available'}</small>
                `);
        });
    })
    .catch(error => {
        console.error('Kon data niet laden:', error);
    });
