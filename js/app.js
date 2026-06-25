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

// Calculate the flight category from visibility and ceiling.
function calculateFlightCategory(metar) {
    if (!metar) {
        return 'UNKNOWN';
    }

    const visibility = parseFloat(String(metar.visib).replace('+', ''));
    const ceiling = Number(metar.ceiling);

    if (Number.isNaN(visibility) || Number.isNaN(ceiling)) {
        return metar.fltCat ?? 'UNKNOWN';
    }

    // The lowest condition wins: poor visibility or low ceiling makes the category worse.
    if (visibility < 1 || ceiling < 500) {
        return 'LIFR';
    }

    if (visibility < 3 || ceiling < 1000) {
        return 'IFR';
    }

    if (visibility <= 5 || ceiling <= 3000) {
        return 'MVFR';
    }

    return 'VFR';
}

// Load airport, METAR and TAF data, then combine and place markers.
Promise.all([
    fetch('data/airports.json').then(r => r.json()),
    fetch('data/metar-test.json').then(r => r.json()),
    fetch('data/taf-test.json').then(r => r.json())
])
    .then(([airports, metarList, tafList]) => {
        // Build a lookup map: icaoId -> metar object.
        const metarMap = {};
        metarList.forEach(metar => {
            metarMap[metar.icaoId] = metar;
        });

        // Build a lookup map: icaoId -> taf object.
        const tafMap = {};
        tafList.forEach(taf => {
            tafMap[taf.icaoId] = taf;
        });

        airports.forEach(airport => {
            const metar = metarMap[airport.icao];
            const taf = tafMap[airport.icao];
            const fltCat = calculateFlightCategory(metar);
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
                    <small>${metar?.rawOb ?? 'No METAR data available'}</small><br><br>
                    <strong>TAF:</strong><br>
                    <small>${taf?.rawTAF ?? 'No TAF data available'}</small>
                `);
        });
    })
    .catch(error => {
        console.error('Kon data niet laden:', error);
    });
