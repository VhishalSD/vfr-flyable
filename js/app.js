// Initialize the Leaflet map and center it on Western Europe.
const map = L.map('map').setView([51.5, 5.0], 6);

// Add the OpenStreetMap tile layer to the map.
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors'
}).addTo(map);