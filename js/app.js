// Initialize the Leaflet map and center it on Western Europe.
const map = L.map('map').setView([51.5, 5.0], 6);

// Create the standard OpenStreetMap tile layer.
const standardMapLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors'
});

// Create an extra topographic map layer for terrain-focused viewing.
const topographicMapLayer = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors, SRTM | © OpenTopoMap'
});

// Add the default map layer to the map.
standardMapLayer.addTo(map);

// Add a Leaflet layer selector so users can switch between map styles.
L.control.layers({
    'OpenStreetMap': standardMapLayer,
    'Topographic map': topographicMapLayer
}).addTo(map);

// Flight category colors following standard aviation convention.
const CATEGORY_COLORS = {
    VFR:  '#00b300',
    MVFR: '#0066ff',
    IFR:  '#cc0000',
    LIFR: '#990099'
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

// Search for an airport by ICAO code or airport name and open its popup.
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
    marker.openPopup();
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

// Reset the search field, filters and map position to the default view.
function resetMapView() {
    const searchInput = document.getElementById('airport-search');
    const countryFilter = document.getElementById('country-filter');
    const categoryFilter = document.getElementById('category-filter');

    searchInput.value = '';
    countryFilter.value = 'ALL';
    categoryFilter.value = 'ALL';

    updateCategoryFilterOptions();
    applyMarkerFilters();
    map.flyTo([51.5, 5.0], 6);
    map.closePopup();
}

// Connect the reset button to the reset view function.
function setupResetView() {
    const resetViewButton = document.getElementById('reset-view-button');
    resetViewButton.addEventListener('click', resetMapView);
}

// Build clear popup HTML for airport, METAR and TAF information.
function createAirportPopupContent(airport, metar, taf, fltCat, categoryReason) {
    return `
        <div class="popup-content">
            <div class="popup-header">
                <strong>${airport.icao}</strong> — ${airport.name}<br>
                <span>${airport.country}</span>
            </div>

            <hr>

            <div class="popup-section">
                <strong>Flight category</strong><br>
                <span>${fltCat}</span><br>
                <small>${categoryReason}</small>
            </div>

            <div class="popup-section">
                <strong>Weather details</strong><br>
                Wind: ${metar?.wdir ?? '—'}° / ${metar?.wspd ?? '—'} kt<br>
                Visibility: ${metar?.visib ?? '—'} SM<br>
                Ceiling: ${metar?.ceiling ?? '—'} ft<br>
                QNH: ${metar?.altim ?? '—'} hPa
            </div>

            <div class="popup-section">
                <strong>METAR</strong><br>
                <small>${metar?.rawOb ?? 'No METAR data available'}</small>
            </div>

            <div class="popup-section">
                <strong>TAF</strong><br>
                <small>${taf?.rawTAF ?? 'No TAF data available'}</small>
            </div>
        </div>
    `;
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
                const metar = metarMap[airport.icao];
                const taf = tafMap[airport.icao];
                const fltCat = calculateFlightCategory(metar);
                const categoryReason = getFlightCategoryReason(metar, fltCat);
                const icon = createIcon(fltCat);
                const popupContent = createAirportPopupContent(airport, metar, taf, fltCat, categoryReason);

                const marker = L.marker([airport.lat, airport.lon], { icon })
                    .addTo(map)
                    .bindPopup(popupContent);

                markerIndex[airport.icao] = marker;
                airportIndex[airport.icao] = airport;
                categoryIndex[airport.icao] = fltCat;
                airportSearchIndex.push(airport);
            });

            updateCategoryFilterOptions();
            applyMarkerFilters();
            updateLastUpdatedTime();
        })
        .catch(error => {
            console.error('Could not load airport or weather data:', error);
        });
}

setupAirportSearch();
setupMarkerFilters();
setupWeatherRefresh();
setupResetView();
loadAirportWeatherData();
