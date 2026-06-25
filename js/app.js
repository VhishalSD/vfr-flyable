// Initialize the Leaflet map and center it on Western Europe.
const map = L.map('map', { zoomControl: false }).setView([51.5, 5.0], 6);

// Create the standard OpenStreetMap raster tile layer.
const standardMapLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors'
});

// Add the default map layer to the Leaflet map.
standardMapLayer.addTo(map);

// The custom toolbar is used instead of the default Leaflet layer selector.

// Flight category colors following standard aviation convention.
const CATEGORY_COLORS = {
    VFR:  '#00b300',
    MVFR: '#0066ff',
    IFR:  '#cc0000',
    LIFR: '#990099'
};

// Short marker labels, closer to the visual style of metar-taf.com.
const CATEGORY_LABELS = {
    VFR: 'V',
    MVFR: 'M',
    IFR: 'I',
    LIFR: 'L'
};

// Store markers by ICAO code for the search function.
const markerIndex = {};
// Store airport metadata by ICAO code for filtering.
const airportIndex = {};
// Store calculated flight categories by ICAO code for category filtering.
const categoryIndex = {};

// Store airports for partial name-based search.
const airportSearchIndex = [];

// Remove existing markers and clear lookup indexes before weather data is loaded again.
function clearAirportData() {
    Object.values(markerIndex).forEach(marker => {
        marker.removeFrom(map);
    });

    Object.keys(markerIndex).forEach(icao => delete markerIndex[icao]);
    Object.keys(airportIndex).forEach(icao => delete airportIndex[icao]);
    Object.keys(categoryIndex).forEach(icao => delete categoryIndex[icao]);
    airportSearchIndex.length = 0;
}

// Return a colored div icon for a given flight category.
function createIcon(fltCat) {
    const color = CATEGORY_COLORS[fltCat] ?? '#999999';
    const label = CATEGORY_LABELS[fltCat] ?? '?';

    return L.divIcon({
        className: '',
        html: `<div class="marker" style="background:${color}">${label}</div>`,
        iconSize: [32, 32],
        iconAnchor: [16, 16]
    });
}

// Convert visibility in meters to statute miles for flight category calculation.
function metersToStatuteMiles(meters) {
    return Number((meters / 1609.344).toFixed(1));
}

// Parse useful weather values from a raw METAR string.
function parseMetar(rawMetar) {
    if (!rawMetar) {
        return {};
    }

    const windMatch = rawMetar.match(/\b(\d{3})(\d{2})(G\d{2})?KT\b/);
    const visibilityMetersMatch = rawMetar.match(/\b(\d{4})\b/);
    const ceilingMatch = rawMetar.match(/\b(?:BKN|OVC|VV)(\d{3})\b/);
    const qnhMatch = rawMetar.match(/\bQ(\d{4})\b/);
    const weatherMatch = rawMetar.match(/\b(-|\+)?(RA|SN|BR|FG|HZ|DZ|TS|SH|GR)\b/);

    const parsedMetar = {};

    if (windMatch) {
        parsedMetar.wdir = Number(windMatch[1]);
        parsedMetar.wspd = Number(windMatch[2]);

        if (windMatch[3]) {
            parsedMetar.wgst = Number(windMatch[3].replace('G', ''));
        }
    }

    if (visibilityMetersMatch) {
        const visibilityMeters = Number(visibilityMetersMatch[1]);
        parsedMetar.visib = metersToStatuteMiles(visibilityMeters).toString();
    }

    if (ceilingMatch) {
        parsedMetar.ceiling = Number(ceilingMatch[1]) * 100;
    }

    if (qnhMatch) {
        parsedMetar.altim = Number(qnhMatch[1]);
    }

    if (weatherMatch) {
        parsedMetar.wxString = weatherMatch[0];
    }

    return parsedMetar;
}

// Combine existing METAR object fields with values parsed from the raw METAR text.
function normalizeMetar(metar) {
    if (!metar) {
        return null;
    }

    const parsedMetar = parseMetar(metar.rawOb);

    return {
        ...parsedMetar,
        ...metar
    };
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

// Explain which visibility or ceiling value caused the chosen flight category.
function getFlightCategoryReason(metar, fltCat) {
    if (!metar) {
        return 'No METAR data is available, so the category is unknown.';
    }

    const visibility = parseFloat(String(metar.visib).replace('+', ''));
    const ceiling = Number(metar.ceiling);

    if (Number.isNaN(visibility) || Number.isNaN(ceiling)) {
        return 'The category is based on the reported METAR flight category because visibility or ceiling data is missing.';
    }

    const visibilityReason = `visibility ${metar.visib} SM`;
    const ceilingReason = `ceiling ${ceiling} ft`;

    if (fltCat === 'LIFR') {
        const triggers = [];

        if (visibility < 1) {
            triggers.push(visibilityReason);
        }

        if (ceiling < 500) {
            triggers.push(ceilingReason);
        }

        return `LIFR because of ${triggers.join(' and ')}.`;
    }

    if (fltCat === 'IFR') {
        const triggers = [];

        if (visibility < 3) {
            triggers.push(visibilityReason);
        }

        if (ceiling < 1000) {
            triggers.push(ceilingReason);
        }

        return `IFR because of ${triggers.join(' and ')}.`;
    }

    if (fltCat === 'MVFR') {
        const triggers = [];

        if (visibility <= 5) {
            triggers.push(visibilityReason);
        }

        if (ceiling <= 3000) {
            triggers.push(ceilingReason);
        }

        return `MVFR because of ${triggers.join(' and ')}.`;
    }

    if (fltCat === 'VFR') {
        return `VFR because visibility ${metar.visib} SM and ceiling ${ceiling} ft are both within VFR limits.`;
    }

    return 'The flight category could not be explained with the available data.';
}

// Search for an airport by ICAO code or airport name and open its sidebar.
function searchAirport() {
    const searchInput = document.getElementById('airport-search');
    const searchValue = searchInput.value.trim();

    if (!searchValue) {
        return;
    }

    const icaoSearchValue = searchValue.toUpperCase();
    const nameSearchValue = searchValue.toLowerCase();
    const matchingAirport = airportSearchIndex.find(airport =>
        airport.name.toLowerCase().includes(nameSearchValue)
    );
    const marker = markerIndex[icaoSearchValue] ?? markerIndex[matchingAirport?.icao];

    if (!marker) {
        alert(`No airport found for: ${searchValue}`);
        return;
    }

    map.flyTo(marker.getLatLng(), 8);
    const foundIcao = markerIndex[icaoSearchValue] ? icaoSearchValue : matchingAirport.icao;
    const airport = airportIndex[foundIcao];
    openSidebar(airport, airport.metar, airport.taf);
}

// Connect the search input and button to the airport search function.
function setupAirportSearch() {
    const searchInput = document.getElementById('airport-search');
    const searchButton = document.getElementById('search-button');

    searchButton.addEventListener('click', searchAirport);

    searchInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            searchAirport();
        }
    });
}

// Update the category dropdown so it only shows categories available for the selected country.
function updateCategoryFilterOptions() {
    const countryFilter = document.getElementById('country-filter');
    const categoryFilter = document.getElementById('category-filter');
    const selectedCountry = countryFilter.value;
    const previousCategory = categoryFilter.value;
    const availableCategories = new Set();

    Object.keys(airportIndex).forEach(icao => {
        const airport = airportIndex[icao];
        const category = categoryIndex[icao];
        const countryMatches = selectedCountry === 'ALL' || airport.country === selectedCountry;

        if (countryMatches && category) {
            availableCategories.add(category);
        }
    });

    categoryFilter.innerHTML = '<option value="ALL">All categories</option>';

    Object.keys(CATEGORY_COLORS).forEach(category => {
        if (availableCategories.has(category)) {
            const option = document.createElement('option');
            option.value = category;
            option.textContent = category;
            categoryFilter.appendChild(option);
        }
    });

    if (previousCategory !== 'ALL' && availableCategories.has(previousCategory)) {
        categoryFilter.value = previousCategory;
    } else {
        categoryFilter.value = 'ALL';
    }
}

// Show or hide markers based on the selected country and flight category.
function applyMarkerFilters() {
    const countryFilter = document.getElementById('country-filter');
    const categoryFilter = document.getElementById('category-filter');
    const selectedCountry = countryFilter.value;
    const selectedCategory = categoryFilter.value;

    Object.keys(markerIndex).forEach(icao => {
        const marker = markerIndex[icao];
        const airport = airportIndex[icao];
        const category = categoryIndex[icao];

        const countryMatches = selectedCountry === 'ALL' || airport.country === selectedCountry;
        const categoryMatches = selectedCategory === 'ALL' || category === selectedCategory;
        const shouldShowMarker = countryMatches && categoryMatches;

        if (shouldShowMarker) {
            marker.addTo(map);
        } else {
            marker.removeFrom(map);
        }
    });
}

// Connect the filter dropdowns to the marker filter function.
function setupMarkerFilters() {
    const countryFilter = document.getElementById('country-filter');
    const categoryFilter = document.getElementById('category-filter');

    countryFilter.addEventListener('change', () => {
        updateCategoryFilterOptions();
        applyMarkerFilters();
    });

    categoryFilter.addEventListener('change', applyMarkerFilters);

    updateCategoryFilterOptions();
    applyMarkerFilters();
}

// Connect the refresh button to reload the local weather test data.
function setupWeatherRefresh() {
    const refreshWeatherButton = document.getElementById('refresh-weather-button');

    refreshWeatherButton.addEventListener('click', () => {
        loadAirportWeatherData();
    });
}

// Show the latest time at which the weather data was loaded.
function updateLastUpdatedTime() {
    const lastUpdatedElement = document.getElementById('last-updated');
    const now = new Date();
    const formattedTime = now.toLocaleTimeString('en-GB', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });

    lastUpdatedElement.textContent = `Last updated: ${formattedTime}`;
}

// Show a short status message when the optional status element exists.
function updateDataStatus(message, isError = false) {
    const dataStatusElement = document.getElementById('data-status');

    if (!dataStatusElement) {
        return;
    }

    dataStatusElement.textContent = message;
    dataStatusElement.classList.toggle('error', isError);
}

// Reset the search field, filters, sidebar and map position to the default view.
function resetMapView() {
    const searchInput = document.getElementById('airport-search');
    const countryFilter = document.getElementById('country-filter');
    const categoryFilter = document.getElementById('category-filter');

    searchInput.value = '';
    countryFilter.value = 'ALL';
    categoryFilter.value = 'ALL';

    updateCategoryFilterOptions();
    applyMarkerFilters();
    closeSidebar();
    map.flyTo([51.5, 5.0], 6);
}

// Connect the reset button to the reset view function.
function setupResetView() {
    const resetViewButton = document.getElementById('reset-view-button');
    resetViewButton.addEventListener('click', resetMapView);
}

// Draw a simple wind compass inside the right airport sidebar.
function drawCompass(canvas, windDirection, windSpeed) {
    if (!canvas) {
        return;
    }

    const context = canvas.getContext('2d');
    const centerX = canvas.width / 2;
    const centerY = canvas.height / 2;
    const radius = centerX - 16;

    context.clearRect(0, 0, canvas.width, canvas.height);

    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.fillStyle = 'rgba(0,0,0,0.25)';
    context.fill();
    context.strokeStyle = 'rgba(255,255,255,0.15)';
    context.lineWidth = 1;
    context.stroke();

    const cardinalDirections = [
        { label: 'N', angle: 0 },
        { label: 'E', angle: 90 },
        { label: 'S', angle: 180 },
        { label: 'W', angle: 270 }
    ];

    context.fillStyle = 'rgba(255,255,255,0.55)';
    context.font = '11px sans-serif';
    context.textAlign = 'center';
    context.textBaseline = 'middle';

    cardinalDirections.forEach(direction => {
        const radians = (direction.angle - 90) * Math.PI / 180;
        const x = centerX + (radius - 13) * Math.cos(radians);
        const y = centerY + (radius - 13) * Math.sin(radians);
        context.fillText(direction.label, x, y);
    });

    for (let degrees = 0; degrees < 360; degrees += 30) {
        const radians = (degrees - 90) * Math.PI / 180;
        const innerRadius = radius - 22;
        const outerRadius = radius - 14;

        context.beginPath();
        context.moveTo(centerX + innerRadius * Math.cos(radians), centerY + innerRadius * Math.sin(radians));
        context.lineTo(centerX + outerRadius * Math.cos(radians), centerY + outerRadius * Math.sin(radians));
        context.strokeStyle = 'rgba(255,255,255,0.25)';
        context.lineWidth = 1;
        context.stroke();
    }

    if (windDirection == null || windSpeed == null) {
        return;
    }

    const windRadians = (windDirection - 90) * Math.PI / 180;
    const arrowRadius = radius - 28;
    const arrowTipX = centerX + arrowRadius * Math.cos(windRadians);
    const arrowTipY = centerY + arrowRadius * Math.sin(windRadians);
    const arrowBaseX = centerX - arrowRadius * Math.cos(windRadians);
    const arrowBaseY = centerY - arrowRadius * Math.sin(windRadians);

    context.beginPath();
    context.moveTo(arrowBaseX, arrowBaseY);
    context.lineTo(arrowTipX, arrowTipY);
    context.strokeStyle = 'white';
    context.lineWidth = 2;
    context.stroke();

    const headSize = 9;
    context.beginPath();
    context.moveTo(arrowTipX, arrowTipY);
    context.lineTo(
        arrowTipX - headSize * Math.cos(windRadians - 0.4),
        arrowTipY - headSize * Math.sin(windRadians - 0.4)
    );
    context.lineTo(
        arrowTipX - headSize * Math.cos(windRadians + 0.4),
        arrowTipY - headSize * Math.sin(windRadians + 0.4)
    );
    context.closePath();
    context.fillStyle = 'white';
    context.fill();
}

// Open the right sidebar with detailed airport weather information.
function openSidebar(airport, metar, taf) {
    if (!airport) {
        return;
    }

    const fltCat = calculateFlightCategory(metar);
    const color = CATEGORY_COLORS[fltCat] ?? '#999999';
    const sidebar = document.getElementById('sidebar');
    const legend = document.querySelector('.legend');

    document.getElementById('sidebar-icao').textContent = airport.icao;
    document.getElementById('sidebar-name').textContent = airport.name;
    document.getElementById('sidebar-location').textContent = airport.country;

    const categoryBadge = document.getElementById('sidebar-category-badge');
    categoryBadge.textContent = fltCat;
    categoryBadge.style.background = color;

    document.getElementById('sidebar-visib').textContent = metar ? `${metar.visib} SM` : '—';
    document.getElementById('sidebar-wxstring').textContent = metar?.wxString ?? 'None';
    document.getElementById('sidebar-temp').textContent = metar?.temp != null ? `${metar.temp} °C` : '—';
    document.getElementById('sidebar-wind-label').textContent = metar ? `${metar.wdir ?? '—'}° ${metar.wspd ?? '—'} kt` : '—';
    document.getElementById('sidebar-raw-metar').textContent = `${metar?.rawOb ?? 'No METAR available'}\n\n${taf?.rawTAF ?? 'No TAF available'}`;

    drawCompass(document.getElementById('wind-compass'), metar?.wdir ?? null, metar?.wspd ?? null);

    sidebar.classList.add('open');
    document.getElementById('map').style.right = '375px';

    if (legend) {
        legend.style.right = '391px';
    }

    map.invalidateSize();
}

// Close the right airport sidebar and restore the map width.
function closeSidebar() {
    const sidebar = document.getElementById('sidebar');
    const legend = document.querySelector('.legend');

    if (sidebar) {
        sidebar.classList.remove('open');
    }

    document.getElementById('map').style.right = '0';

    if (legend) {
        legend.style.right = '16px';
    }

    map.invalidateSize();
}

// Connect the custom toolbar buttons to map and UI actions.
function setupToolbarControls() {
    const filterPanelButton = document.getElementById('layer-category-filter');
    const zoomInButton = document.getElementById('layer-zoom-in');
    const zoomOutButton = document.getElementById('layer-zoom-out');
    const sidebarCloseButton = document.getElementById('sidebar-close');
    const filterPanel = document.getElementById('filter-panel');

    if (filterPanelButton && filterPanel) {
        filterPanelButton.addEventListener('click', () => {
            filterPanel.classList.toggle('open');
        });
    }

    if (zoomInButton) {
        zoomInButton.addEventListener('click', () => map.zoomIn());
    }

    if (zoomOutButton) {
        zoomOutButton.addEventListener('click', () => map.zoomOut());
    }

    if (sidebarCloseButton) {
        sidebarCloseButton.addEventListener('click', closeSidebar);
    }
}

// Load airport, METAR and TAF data, then combine and place markers.
function loadAirportWeatherData() {
    clearAirportData();

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
                const metar = normalizeMetar(metarMap[airport.icao]);
                const taf = tafMap[airport.icao];
                const fltCat = calculateFlightCategory(metar);
                const icon = createIcon(fltCat);

                airport.metar = metar;
                airport.taf = taf;

                const marker = L.marker([airport.lat, airport.lon], { icon })
                    .addTo(map);

                marker.on('click', () => {
                    openSidebar(airport, metar, taf);
                });

                markerIndex[airport.icao] = marker;
                airportIndex[airport.icao] = airport;
                categoryIndex[airport.icao] = fltCat;
                airportSearchIndex.push(airport);
            });

            updateCategoryFilterOptions();
            applyMarkerFilters();
            updateLastUpdatedTime();
            updateDataStatus('');
        })
        .catch(error => {
            console.error('Could not load airport or weather data:', error);
            updateDataStatus('Could not load airport or weather data.', true);
        });
}

setupAirportSearch();
setupMarkerFilters();
setupWeatherRefresh();
setupResetView();
setupToolbarControls();
loadAirportWeatherData();
