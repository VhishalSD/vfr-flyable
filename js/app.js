'use strict';

const COLORS={VFR:'#25b84b',MVFR:'#1976db',IFR:'#d92525',LIFR:'#a521a8'};

const SHORT={VFR:'V',MVFR:'M',IFR:'I',LIFR:'L',UNKNOWN:'?'};

const map=L.map('map',{zoomControl:false,worldCopyJump:true,minZoom:2}).setView([52.3,4.8],6);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap contributors'}).addTo(map);

const codeLayer=L.layerGroup().addTo(map),weatherLayer=L.layerGroup(),rainLayer=L.layerGroup();

let airports=[],nearbyDataset=[],runwaysByAirport={},mode='code',rainActive=false,current=null,routeMode=false,pointerMode=false,routePoints=[],routeLine=null,locationRequest=0,localTimeTimer=null;

async function loadAirportMetadata(){
try{const response=await fetch('data/airport-metadata.json');
if(!response.ok)return{};
const document=await response.json();
return document&&typeof document==='object'&&!Array.isArray(document)&&document.airports&&typeof document.airports==='object'&&!Array.isArray(document.airports)?document.airports:{}}
catch{return{}}}

async function loadTafData(){
try{const response=await fetch('data/taf-live.json');
if(!response.ok)return{};
const document=await response.json();
return document&&typeof document==='object'&&!Array.isArray(document)&&document.airports&&typeof document.airports==='object'&&!Array.isArray(document.airports)?document.airports:{}}
catch{return{}}}

function stopLocalTimeClock(){if(localTimeTimer!==null){window.clearInterval(localTimeTimer);localTimeTimer=null}}
function startLocalTimeClock(timezone,metadata){
stopLocalTimeClock();
const timeElement=document.getElementById('panel-local-time'),timezoneElement=document.getElementById('panel-timezone'),daylightStatusElement=document.getElementById('panel-daylight-status');
timeElement.textContent='—';timezoneElement.textContent='—';daylightStatusElement.textContent='—';
if(typeof timezone!=='string'||!timezone.trim())return;
try{const timezoneName=timezone.trim(),formatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezoneName,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}),update=()=>{const now=new Date(Date.now());timeElement.textContent=formatter.format(now);renderDaylightStatus(metadata,now)};
update();timezoneElement.textContent=timezoneName;localTimeTimer=window.setInterval(update,60*1000)}catch{timeElement.textContent='—';timezoneElement.textContent='—';daylightStatusElement.textContent='—'}}

function renderDaylightStatus(metadata,now=new Date(Date.now())){
const statusElement=document.getElementById('panel-daylight-status');
statusElement.textContent='—';
const timezone=metadata?.timezone,daylight=metadata?.daylight;
if(typeof timezone!=='string'||!timezone.trim()||!daylight||typeof daylight!=='object'||!Number.isFinite(now.getTime()))return;
try{const timezoneName=timezone.trim(),dateParts=Object.fromEntries(new Intl.DateTimeFormat('en',{timeZone:timezoneName,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now).filter(part=>part.type!=='literal').map(part=>[part.type,part.value])),localDate=`${dateParts.year}-${dateParts.month}-${dateParts.day}`;
if(daylight.state==='polar_day'||daylight.state==='polarDay'){statusElement.textContent='Polar day';return}
if(daylight.state==='polar_night'||daylight.state==='polarNight'){statusElement.textContent='Polar night';return}
if(daylight.state!=='normal'||typeof daylight.date!=='string'||daylight.date!==localDate)return;
const parseEvent=value=>{if(typeof value!=='string')return null;const timestamp=value.trim();if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp))return null;const event=new Date(timestamp);return Number.isFinite(event.getTime())?event:null},sunrise=parseEvent(daylight.sunriseUtc),sunset=parseEvent(daylight.sunsetUtc);
if(!sunrise||!sunset||sunrise>=sunset)return;
statusElement.textContent=now>=sunrise&&now<sunset?'Day':'Night'}catch{statusElement.textContent='—'}}

function renderDaylightTimes(metadata,now=new Date()){
const sunriseElement=document.getElementById('panel-sunrise'),sunsetElement=document.getElementById('panel-sunset'),solarNoonElement=document.getElementById('panel-solar-noon'),dayLengthElement=document.getElementById('panel-day-length');
sunriseElement.textContent='—';sunsetElement.textContent='—';solarNoonElement.textContent='—';dayLengthElement.textContent='—';
const timezone=metadata?.timezone,daylight=metadata?.daylight;
if(typeof timezone!=='string'||!timezone.trim()||!daylight||typeof daylight!=='object'||typeof daylight.date!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(daylight.date))return;
try{const timezoneName=timezone.trim(),dateParts=Object.fromEntries(new Intl.DateTimeFormat('en',{timeZone:timezoneName,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now).filter(part=>part.type!=='literal').map(part=>[part.type,part.value])),localDate=`${dateParts.year}-${dateParts.month}-${dateParts.day}`;
if(daylight.date!==localDate)return;
const formatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezoneName,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}),formatEvent=value=>{if(typeof value!=='string')return'—';const timestamp=value.trim();if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp))return'—';const event=new Date(timestamp);return Number.isFinite(event.getTime())?formatter.format(event):'—'};
sunriseElement.textContent=formatEvent(daylight.sunriseUtc);sunsetElement.textContent=formatEvent(daylight.sunsetUtc);solarNoonElement.textContent=formatEvent(daylight.solarNoonUtc);
const seconds=daylight.dayLengthSeconds;if(typeof seconds==='number'&&Number.isFinite(seconds)&&seconds>=0){const minutes=Math.round(seconds/60);dayLengthElement.textContent=`${Math.floor(minutes/60)} h ${minutes%60} min`}}catch{sunriseElement.textContent='—';sunsetElement.textContent='—';solarNoonElement.textContent='—';dayLengthElement.textContent='—'}}


function symbol(m){const raw=m?.rawOb||'';
if(/TS/.test(raw))return'⛈️';
if(/RA|DZ|SH/.test(raw))return'🌧️';
if(/FG|BR|HZ/.test(raw))return'🌫️';
if(/OVC/.test(raw))return'☁️';
if(/BKN|SCT/.test(raw))return'🌤️';
return'☀️'}
function category(m){if(!m)return'UNKNOWN';
if(m.fltCat)return m.fltCat;
const v=parseFloat(m?.visib),c=Number(m?.ceiling);
if(v<1||c<500)return'LIFR';
if(v<3||c<1000)return'IFR';
if(v<=5||c<=3000)return'MVFR';
return'VFR'}
function variableWindRange(raw){const match=(raw||'').match(/(?:^|\s)(\d{3})V(\d{3})(?=\s|$)/);
return match?[Number(match[1]),Number(match[2])]:null}
function angularDifference(a,b){return Math.abs(((a-b+540)%360)-180)}
function runwayForWind(icao,wdir){const wind=Number(wdir);
if(!Number.isFinite(wind))return null;
return(runwaysByAirport[icao]||[]).filter(runway=>!runway.closed&&runway.le_ident&&runway.he_ident).flatMap(runway=>[
  {heading:runway.le_heading_degT,primary:runway.le_ident,opposite:runway.he_ident,length:runway.length_ft||0},
  {heading:runway.he_heading_degT,primary:runway.he_ident,opposite:runway.le_ident,length:runway.length_ft||0}
]).filter(end=>end.heading!=null&&Number.isFinite(Number(end.heading))).map(end=>({...end,heading:Number(end.heading)})).map(end=>({...end,difference:angularDifference(end.heading,wind)})).sort((a,b)=>a.difference-b.difference||b.length-a.length||a.primary.localeCompare(b.primary))[0]||null}
function codeIcon(cat){return L.divIcon({className:'',html:`<span class="code-icon" style="--marker-color:${COLORS[cat]||'#7b8790'}">${SHORT[cat]}</span>`,iconSize:[26,26],iconAnchor:[13,13]})}
function weatherIcon(m){return L.divIcon({className:'',html:`<span class="weather-icon">${symbol(m)}</span>`,iconSize:[29,29],iconAnchor:[15,15]})}
function popup(a){return `<div class="airport-popup"><b>${a.icao}</b><br>${a.name}<br><small>${a.country}</small></div>`}

function rebuildMarkers(){codeLayer.clearLayers();
weatherLayer.clearLayers();
const visibleAirports=airports.filter(a=>map.getBounds().contains([a.lat,a.lon]));
const metarAirports=visibleAirports.filter(a=>a.metar);
const minimumDistance=32,minimumDistanceSquared=minimumDistance*minimumDistance;
const selectedPoints=[];
const selectedAirports=metarAirports.slice().sort((a,b)=>a.icao<b.icao?-1:a.icao>b.icao?1:0).filter(a=>{
  const point=map.latLngToContainerPoint([a.lat,a.lon]);
  if(selectedPoints.some(chosen=>{const dx=point.x-chosen.x,dy=point.y-chosen.y;return dx*dx+dy*dy<minimumDistanceSquared}))return false;
  selectedPoints.push(point);return true;
});
Object.assign(map.getContainer().dataset,{markerCandidates:metarAirports.length,markerShown:selectedAirports.length,markerHidden:metarAirports.length-selectedAirports.length});
selectedAirports.forEach(a=>{const cat=category(a.metar);
const attach=marker=>marker.bindPopup(popup(a)).on('click',event=>{
  if(routeMode){
    if(event.originalEvent)L.DomEvent.stopPropagation(event.originalEvent);
    marker.closePopup();map.closePopup();addRouteAirport(a);return;
  }
  if(pointerMode){
    if(event.originalEvent)L.DomEvent.stopPropagation(event.originalEvent);
    marker.closePopup();map.closePopup();showLocation(L.latLng(a.lat,a.lon));return;
  }
  selectAirport(a);marker.openPopup();
});
L.marker([a.lat,a.lon],{icon:codeIcon(cat)}).addTo(codeLayer);
attach(codeLayer.getLayers().at(-1));
L.marker([a.lat,a.lon],{icon:weatherIcon(a.metar)}).addTo(weatherLayer);
attach(weatherLayer.getLayers().at(-1))})}
function setMode(next){mode=next;
const code=next==='code';
document.getElementById('layer-code').classList.toggle('active',code);
document.getElementById('layer-weather').classList.toggle('active',!code);
document.getElementById('layer-code').setAttribute('aria-pressed',code);
document.getElementById('layer-weather').setAttribute('aria-pressed',!code);
if(code){if(map.hasLayer(weatherLayer))map.removeLayer(weatherLayer);
if(!map.hasLayer(codeLayer))map.addLayer(codeLayer)}else{if(map.hasLayer(codeLayer))map.removeLayer(codeLayer);
if(!map.hasLayer(weatherLayer))map.addLayer(weatherLayer)}}
function buildRain(){if(rainLayer.getLayers().length)return;
[
[[58,-13],[55,-7],[52,-4],[50,-8],[53,-15]],[[53,2],[51,7],[48,9],[47,3],[50,0]],[[45,-3],[43,3],[39,2],[40,-5]],[[8,-70],[3,-64],[-4,-61],[-1,-70]]
].forEach((p,i)=>L.polygon(p,{color:i%2?'#6f4ad1':'#2589df',weight:1,fillColor:i%2?'#7751d5':'#32b8dd',fillOpacity:.32,interactive:false}).addTo(rainLayer))}
function toggleRain(){rainActive=!rainActive;
const b=document.getElementById('layer-rain');
b.classList.toggle('active',rainActive);
b.setAttribute('aria-pressed',rainActive);
document.getElementById('rain-label').classList.toggle('show',rainActive);
if(rainActive){buildRain();
if(!map.hasLayer(rainLayer))map.addLayer(rainLayer)}else if(map.hasLayer(rainLayer))map.removeLayer(rainLayer)}

function selectAirport(a){
if(routeMode||pointerMode)setToolMode(null);
stopLocalTimeClock();
document.body.classList.remove('panel-closed');
document.getElementById('airport-panel').classList.remove('closed');
document.getElementById('airport-panel').setAttribute('aria-hidden','false');
window.setTimeout(()=>map.invalidateSize(),260);
current=a;
const m=a.metar||{},cat=category(m);
document.getElementById('panel-icao').textContent=a.icao;
document.getElementById('panel-name').textContent=a.name;
const municipality=typeof a.metadata?.municipality==='string'&&a.metadata.municipality.trim()?a.metadata.municipality.trim():null;
document.getElementById('panel-location').textContent=[municipality,a.country].filter(Boolean).join(', ')||'—';
const fir=a.metadata?.fir;document.getElementById('panel-fir').textContent=typeof fir==='string'&&fir.trim()?fir:'—';
const badge=document.getElementById('panel-category');
badge.textContent=cat;
badge.style.background=COLORS[cat];
document.getElementById('panel-visibility').textContent=m.visib!=null?`${m.visib} SM`:'—';
const noCeiling=Array.isArray(m.clouds)&&m.clouds.some(layer=>['CAVOK','CLR','SKC','NSC','NCD','FEW','SCT'].includes(String(layer?.cover||'').toUpperCase()));
document.getElementById('panel-ceiling').textContent=m.ceiling!=null?`${Number(m.ceiling).toLocaleString('nl-NL')} ft`:noCeiling?'None':'—';
document.getElementById('panel-symbol').textContent=symbol(m);
document.getElementById('panel-temp').textContent=m.temp!=null?`${m.temp} °C`:'—';
document.getElementById('panel-wind').textContent=`${m.wdir??'—'}° · ${m.wspd??'—'} kt`;
const windArrow=document.getElementById('wind-arrow'),windMin=document.getElementById('wind-min'),windMax=document.getElementById('wind-max');
const hasWindDirection=m.wdir!=null&&Number.isFinite(Number(m.wdir));
windArrow.hidden=!hasWindDirection;
if(hasWindDirection)windArrow.style.transform=`rotate(${Number(m.wdir)}deg)`;
const variableRange=variableWindRange(m.rawOb);
windMin.hidden=!variableRange;windMax.hidden=!variableRange;
if(variableRange){windMin.style.transform=`rotate(${variableRange[0]}deg)`;windMax.style.transform=`rotate(${variableRange[1]}deg)`}
const runwayLayer=document.getElementById('compass-runway'),selectedRunway=runwayForWind(a.icao,m.wdir);
runwayLayer.toggleAttribute('hidden',!selectedRunway);
runwayLayer.style.transform=selectedRunway?`rotate(${selectedRunway.heading}deg)`:'';
document.getElementById('runway-primary').textContent=selectedRunway?.primary||'—';
document.getElementById('runway-opposite').textContent=selectedRunway?.opposite||'—';
if(selectedRunway)Object.assign(runwayLayer.dataset,{airport:a.icao,primary:selectedRunway.primary,opposite:selectedRunway.opposite,heading:selectedRunway.heading,windDifference:selectedRunway.difference});
else Object.keys(runwayLayer.dataset).forEach(key=>delete runwayLayer.dataset[key]);
document.getElementById('panel-raw').textContent=m.rawOb||'Geen METAR beschikbaar';
document.querySelector('.drawer-weather').href=`metar/index.html?icao=${encodeURIComponent(a.icao)}`;
document.getElementById('airport-panel').classList.add('open')}
function renderFavorites(){const root=document.getElementById('favorites');
root.innerHTML='';
['EGLL','EHAM','EHRD','EBOS','LFPG'].forEach(id=>{const a=airports.find(x=>x.icao===id);
if(!a)return;
const b=document.createElement('button');
b.innerHTML=`<span class="fav-v">${SHORT[category(a.metar)]}</span>${id}`;
b.onclick=()=>{selectAirport(a);
map.flyTo([a.lat,a.lon],6)};
root.appendChild(b)})}

async function loadData(){try{const metadataRequest=loadAirportMetadata(),tafRequest=loadTafData();
const [ap,ms,nearby,runways,metadataByIcao,tafByIcao]=await Promise.all([...['data/airports-full.json','data/metar-live.json','data/nearby-airports.json','data/runways.json'].map(u=>fetch(u).then(r=>{if(!r.ok)throw Error(u);
return r.json()})),metadataRequest,tafRequest]);
const mm=Object.fromEntries(ms.map(x=>[x.icaoId,x]));
airports=ap.map(sourceAirport=>{const airport={...sourceAirport,metar:mm[sourceAirport.icao],taf:tafByIcao[sourceAirport.icao]||null};airport.metadata=metadataByIcao[airport.icao]||null;return airport});
nearbyDataset=nearby;
runwaysByAirport=runways.reduce((groups,runway)=>{(groups[runway.airport_ident]??=[]).push(runway);return groups},{});
rebuildMarkers();
renderFavorites()}catch(e){console.error('Lokale weerdata kon niet worden geladen.',e)}}
function search(){const q=document.getElementById('airport-search').value.trim().toLowerCase();
if(!q)return;
const a=airports.find(x=>x.icao.toLowerCase()===q)||airports.find(x=>`${x.name} ${x.country}`.toLowerCase().includes(q));
if(a){selectAirport(a);
map.flyTo([a.lat,a.lon],7)}else alert(`Geen vliegveld gevonden voor: ${q}`)}
document.getElementById('search-form').addEventListener('submit',e=>{e.preventDefault();
search()});

document.getElementById('layer-code').onclick=()=>setMode('code');
document.getElementById('layer-weather').onclick=()=>setMode('weather');
document.getElementById('layer-rain').onclick=toggleRain;
document.getElementById('zoom-in').onclick=()=>map.zoomIn();
document.getElementById('zoom-out').onclick=()=>map.zoomOut();
document.getElementById('locate-btn').onclick=()=>navigator.geolocation?.getCurrentPosition(p=>map.flyTo([p.coords.latitude,p.coords.longitude],8),()=>map.flyTo([52.1,5.2],6));
document.getElementById('panel-close').onclick=()=>{
  current=null;
  stopLocalTimeClock();
  map.closePopup();
  document.getElementById('airport-panel').classList.remove('open');
  document.getElementById('airport-panel').classList.add('closed');
  document.getElementById('airport-panel').setAttribute('aria-hidden','true');
  document.body.classList.add('panel-closed');
  window.setTimeout(()=>map.invalidateSize(),260);
}
function bearing(a,b){const r=Math.PI/180,p1=a.lat*r,p2=b.lat*r,d=(b.lng-a.lng)*r;return(Math.atan2(Math.sin(d)*Math.cos(p2),Math.cos(p1)*Math.sin(p2)-Math.sin(p1)*Math.cos(p2)*Math.cos(d))/r+360)%360}
function renderRoute(){
  const root=document.getElementById('route-stops');root.innerHTML='';let total=0;
  routePoints.forEach((stop,index)=>{
    if(index){const meters=stop.latlng.distanceTo(routePoints[index-1].latlng);total+=meters;const segment=document.createElement('div');segment.className='route-segment';segment.textContent=`${bearing(routePoints[index-1].latlng,stop.latlng).toFixed(0)}° · ${(meters/1852).toFixed(1)} NM`;root.appendChild(segment)}
    const card=document.createElement('div');card.className='stop-card';card.innerHTML=`<span class="stop-index">${index+1}</span><div><b>${stop.airport?.icao||`Punt ${index+1}`}</b><small>${stop.airport?.name||`${stop.latlng.lat.toFixed(4)}°, ${stop.latlng.lng.toFixed(4)}°`}</small></div><button class="stop-remove" aria-label="Stop verwijderen">×</button>`;card.querySelector('button').onclick=()=>{routePoints.splice(index,1);updateRoute()};root.appendChild(card)
  });
  const summary=document.getElementById('route-summary');summary.hidden=routePoints.length<2;
  document.getElementById('route-total-nm').textContent=`${(total/1852).toFixed(1)} NM`;
  document.getElementById('route-total-km').textContent=`${(total/1000).toFixed(1)} km`;
  document.getElementById('route-total-mi').textContent=`${(total/1609.344).toFixed(1)} mijl`;
}
function updateRoute(){if(routeLine){map.removeLayer(routeLine);routeLine=null}if(routePoints.length>1)routeLine=L.polyline(routePoints.map(s=>s.latlng),{color:'#e33',weight:3}).addTo(map);renderRoute()}
function clearRoute(){routePoints=[];updateRoute()}
function addRouteAirport(airport){routePoints.push({latlng:L.latLng(airport.lat,airport.lon),airport});updateRoute()}
async function showLocation(latlng){
  const request=++locationRequest;
  const nearby=(nearbyDataset.length?nearbyDataset:airports.map(a=>({id:a.icao,...a}))).map(a=>({a,distance:latlng.distanceTo(L.latLng(a.lat,a.lon))})).sort((a,b)=>a.distance-b.distance);
  const nearest=nearby[0];
  const panel=document.getElementById('location-panel');panel.classList.add('open');panel.setAttribute('aria-hidden','false');document.body.classList.add('location-open');
  document.getElementById('location-name').textContent='Locatie laden…';
  document.getElementById('location-coordinates').textContent=`${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}`;
  const list=document.getElementById('nearby-airports');list.innerHTML='';nearby.slice(0,5).forEach(item=>{const row=document.createElement('div');row.className='nearby-airport';row.innerHTML=`<div><b>${item.a.id||item.a.icao||'—'}</b><small>${item.a.name}</small></div><span>${Math.round(item.distance/1852)} NM</span>`;list.appendChild(row)});
  const firByCountry={NL:'Amsterdam FIR (EHAA)',BE:'Brussels FIR (EBBU)',FR:'Paris FIR (LFFF)',DE:'Langen FIR (EDGG)',GB:'London FIR (EGTT)',ES:'Madrid FIR (LECM)',IT:'Milano FIR (LIMM)',US:'Lokale Amerikaanse FIR',CA:'Canadian Domestic FIR'};
  let place=nearest?`Nabij ${nearest.a.name}`:`${latlng.lat.toFixed(3)}, ${latlng.lng.toFixed(3)}`,region=nearest?.a.country||'Niet beschikbaar',country=nearest?.a.country||'Niet beschikbaar',countryCode='';
  try{
    const response=await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${encodeURIComponent(latlng.lat)}&lon=${encodeURIComponent(latlng.lng)}&zoom=10&addressdetails=1`,{headers:{Accept:'application/json'}});
    if(!response.ok)throw Error(`reverse geocoding ${response.status}`);
    const data=await response.json();if(request!==locationRequest||!pointerMode)return;
    const a=data.address||{};place=a.city||a.town||a.village||a.municipality||a.hamlet||a.county||data.name||data.display_name?.split(',')[0]||place;
    region=a.state||a.province||a.region||a.county||region;country=a.country||country;countryCode=(a.country_code||'').toUpperCase();
  }catch(error){if(request!==locationRequest||!pointerMode)return;console.info('Reverse-geocoding niet bereikbaar; lokale locatiebenadering gebruikt.',error)}
  document.getElementById('location-name').textContent=place;
  document.getElementById('location-region').textContent=region;
  document.getElementById('location-country').textContent=country;
  document.getElementById('location-fir').textContent=firByCountry[countryCode]||`${country} FIR · lokale benadering`;
  window.setTimeout(()=>map.invalidateSize(),260);
}
function setToolMode(next){
  routeMode=next==='route';pointerMode=next==='pointer';
  const routeButton=document.getElementById('route-btn'),pointerButton=document.getElementById('pointer-btn');
  routeButton.classList.toggle('active',routeMode);pointerButton.classList.toggle('active',pointerMode);
  routeButton.setAttribute('aria-pressed',String(routeMode));pointerButton.setAttribute('aria-pressed',String(pointerMode));
  map.getContainer().style.cursor=(routeMode||pointerMode)?'crosshair':'';
  const panel=document.getElementById('route-panel');panel.classList.toggle('open',routeMode);panel.setAttribute('aria-hidden',String(!routeMode));document.body.classList.toggle('route-open',routeMode);
  const locationPanel=document.getElementById('location-panel');
  if(!pointerMode){locationPanel.classList.remove('open');locationPanel.setAttribute('aria-hidden','true');document.body.classList.remove('location-open')}
  if(routeMode||pointerMode){current=null;stopLocalTimeClock();map.closePopup();document.getElementById('airport-panel').classList.add('closed');document.getElementById('airport-panel').setAttribute('aria-hidden','true');document.body.classList.add('panel-closed')}
  if(!routeMode)clearRoute();
  window.setTimeout(()=>map.invalidateSize(),260);
}
document.getElementById('route-btn').onclick=()=>setToolMode(routeMode?null:'route');
document.getElementById('pointer-btn').onclick=()=>setToolMode(pointerMode?null:'pointer');
document.getElementById('route-close').onclick=()=>setToolMode(null);
document.getElementById('route-reset').onclick=clearRoute;
document.getElementById('location-close').onclick=()=>setToolMode(null);

map.on('moveend',rebuildMarkers);
map.on('click',e=>{
  if(routeMode){
    routePoints.push({latlng:e.latlng,airport:null});updateRoute();
    return;
  }
  if(pointerMode){
    showLocation(e.latlng);
  }
});

loadData();
