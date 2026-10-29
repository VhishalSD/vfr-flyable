'use strict';

const CATEGORY_COLORS={VFR:'#27b84f',MVFR:'#1976db',IFR:'#d92525',LIFR:'#a521a8',UNKNOWN:'#7b8790'};
const WIND_METER_MAX=45,WIND_METER_CENTER={x:211,y:80},WIND_METER_RADIUS=60;
const HISTORY_CHARTS=[
  {field:'temp',title:'Temperature (°C)',zeroBased:false,minimumSpan:4},
  {field:'visib',title:'Visibility (SM)',zeroBased:true,minimumSpan:2},
  {field:'wspd',title:'Wind speed (kt)',zeroBased:true,minimumSpan:5},
  {field:'altim',title:'QNH (hPa)',zeroBased:false,minimumSpan:4}
];
const SVG_NAMESPACE='http://www.w3.org/2000/svg';
const TAF_HOUR_MS=60*60*1000;
const TAF_PERMANENT_TYPES=new Set(['BASELINE','FM','BECMG']);
const TAF_OVERLAY_TYPES=new Set(['TEMPO','PROB','PROB_TEMPO']);
let detailAirport=null,detailAirportMetadata=null,detailMetar=null,detailTaf=null,tafDocument=null,tafLoadFailed=false,allAirportRunways=[],airportRunways=[],selectedRunway=null,activeDetailTab='metar',detailMetadataTimer=null,recentHistoryLoad=null,recentHistoryLoadedIcao=null;
let airportGeodataLoad=null,airportGeodata=null,airportTabLoad=null,airportTabMap=null,airportTabMapLayers=null,airportTabMapFitted=false;
let trendsLoad=null,trendsDocument=null;

async function loadAirportMetadata(){
  try{
    const response=await fetch('data/airport-metadata.json');if(!response.ok)return{};
    const document=await response.json();
    return document&&typeof document==='object'&&!Array.isArray(document)&&document.airports&&typeof document.airports==='object'&&!Array.isArray(document.airports)?document.airports:{};
  }catch{return{}}
}

function detailMetadataElements(){return{
  municipality:document.getElementById('detail-municipality'),iata:document.getElementById('detail-iata'),fir:document.getElementById('detail-fir'),elevationFt:document.getElementById('detail-elevation-ft'),elevationM:document.getElementById('detail-elevation-m'),localTime:document.getElementById('detail-local-time'),timezone:document.getElementById('detail-timezone'),sunrise:document.getElementById('detail-sunrise'),sunset:document.getElementById('detail-sunset'),daylightStatus:document.getElementById('detail-daylight-status'),solarNoon:document.getElementById('detail-solar-noon'),dayLength:document.getElementById('detail-day-length')
}}

function detailIsoTimestamp(value){
  if(typeof value!=='string')return null;
  const timestamp=value.trim();if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp))return null;
  const date=new Date(timestamp);return Number.isFinite(date.getTime())?date:null;
}

function detailLocalDate(timezone,now){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now).filter(part=>part.type!=='literal').map(part=>[part.type,part.value]));
  return`${parts.year}-${parts.month}-${parts.day}`;
}

function renderDetailMetadataTimes(now=new Date(Date.now())){
  renderAirportDynamicTimeFields(now);
  const elements=detailMetadataElements(),metadata=detailAirportMetadata,timezone=metadata?.timezone,daylight=metadata?.daylight;
  elements.localTime.textContent='—';elements.timezone.textContent='—';elements.sunrise.textContent='—';elements.sunset.textContent='—';elements.daylightStatus.textContent='—';
  if(typeof timezone!=='string'||!timezone.trim()||!Number.isFinite(now.getTime()))return false;
  try{
    const timezoneName=timezone.trim(),timeFormatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezoneName,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}),localDate=detailLocalDate(timezoneName,now);
    elements.localTime.textContent=timeFormatter.format(now);elements.timezone.textContent=timezoneName;
    if(!daylight||typeof daylight!=='object')return true;
    const currentDay=typeof daylight.date==='string'&&daylight.date===localDate,sunrise=currentDay?detailIsoTimestamp(daylight.sunriseUtc):null,sunset=currentDay?detailIsoTimestamp(daylight.sunsetUtc):null;
    if(sunrise)elements.sunrise.textContent=timeFormatter.format(sunrise);if(sunset)elements.sunset.textContent=timeFormatter.format(sunset);
    if(daylight.state==='polar_day'||daylight.state==='polarDay'){elements.daylightStatus.textContent='Polar day';return true}
    if(daylight.state==='polar_night'||daylight.state==='polarNight'){elements.daylightStatus.textContent='Polar night';return true}
    if(daylight.state!=='normal'||!currentDay||!sunrise||!sunset||sunrise>=sunset)return true;
    elements.daylightStatus.textContent=now>=sunrise&&now<sunset?'Day':'Night';return true;
  }catch{return false}
}

function stopDetailMetadataClock(){if(detailMetadataTimer!==null){window.clearInterval(detailMetadataTimer);detailMetadataTimer=null}}

function renderDetailDaylightSummary(now=new Date(Date.now())){
  const elements=detailMetadataElements(),metadata=detailAirportMetadata,timezone=metadata?.timezone,daylight=metadata?.daylight;
  elements.solarNoon.textContent='—';elements.dayLength.textContent='—';
  if(typeof timezone!=='string'||!timezone.trim()||!daylight||typeof daylight!=='object'||!Number.isFinite(now.getTime()))return;
  try{
    const timezoneName=timezone.trim(),localDate=detailLocalDate(timezoneName,now);if(typeof daylight.date!=='string'||daylight.date!==localDate)return;
    const formatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezoneName,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}),solarNoon=detailIsoTimestamp(daylight.solarNoonUtc);if(solarNoon)elements.solarNoon.textContent=formatter.format(solarNoon);
    const seconds=daylight.dayLengthSeconds;if(typeof seconds==='number'&&Number.isFinite(seconds)&&seconds>=0){const minutes=Math.round(seconds/60);elements.dayLength.textContent=`${Math.floor(minutes/60)} h ${minutes%60} min`}
  }catch{elements.solarNoon.textContent='—';elements.dayLength.textContent='—'}
}

function renderDetailAirportMetadata(){
  stopDetailMetadataClock();
  const elements=detailMetadataElements(),metadata=detailAirportMetadata,elevationFt=metadata?.elevationFt,elevationM=metadata?.elevationM;
  const fir=metadata?.fir;elements.municipality.textContent=metadata?.municipality??'—';elements.iata.textContent=metadata?.iata??'—';elements.fir.textContent=typeof fir==='string'&&fir.trim()?fir:'—';elements.elevationFt.textContent=Number.isFinite(elevationFt)?`${Math.round(elevationFt)} ft`:'—';elements.elevationM.textContent=Number.isFinite(elevationM)?`${elevationM.toFixed(1)} m`:'—';renderDetailDaylightSummary();
  if(renderDetailMetadataTimes())detailMetadataTimer=window.setInterval(()=>renderDetailMetadataTimes(),60*1000);
}

function airportSetText(id,value){const element=document.getElementById(id);if(element)element.textContent=value??'—'}
function airportValue(value){return typeof value==='string'&&value.trim()?value.trim():'—'}
function airportFiniteNumber(value){return value!==null&&value!==''&&Number.isFinite(Number(value))}
function airportHeading(value){if(!airportFiniteNumber(value))return'—';const number=Number(value);return`${Number.isInteger(number)?number:number.toFixed(1)}°`}
function airportBearing(value){return airportFiniteNumber(value)?`${String(Math.round(Number(value))).padStart(3,'0')}°`:'—'}
function airportTypeLabel(value){
  const labels={large_airport:'Large airport',medium_airport:'Medium airport',small_airport:'Small airport',heliport:'Heliport',seaplane_base:'Seaplane base',balloonport:'Balloonport',closed:'Closed',closed_airport:'Closed'};
  if(typeof value!=='string'||!value.trim())return'—';return labels[value]||value.replaceAll('_',' ').replace(/^./,letter=>letter.toUpperCase());
}

function decimalCoordinateToDms(value,kind){
  const number=Number(value),latitude=kind==='latitude',maximum=latitude?90:180;if(!Number.isFinite(number)||Math.abs(number)>maximum)return'—';
  const totalSeconds=Math.min(maximum*3600,Math.round(Math.abs(number)*3600)),degrees=Math.floor(totalSeconds/3600),remainder=totalSeconds%3600,minutes=Math.floor(remainder/60),seconds=remainder%60;
  const hemisphere=latitude?(number<0?'S':'N'):(number<0?'W':'E');return`${String(degrees).padStart(latitude?2:3,'0')}${String(minutes).padStart(2,'0')}${String(seconds).padStart(2,'0')}${hemisphere}`;
}

function detailUtcOffsetMinutes(timezone,date){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date).filter(part=>part.type!=='literal').map(part=>[part.type,part.value]));
  const localAsUtc=Date.UTC(Number(parts.year),Number(parts.month)-1,Number(parts.day),Number(parts.hour),Number(parts.minute),Number(parts.second)),moment=Math.trunc(date.getTime()/1000)*1000;return Math.round((localAsUtc-moment)/60000);
}

function detailDstActive(timezone,date){
  const year=Number(new Intl.DateTimeFormat('en',{timeZone:timezone,year:'numeric'}).format(date)),offsets=[];
  for(let month=0;month<12;month+=1)offsets.push(detailUtcOffsetMinutes(timezone,new Date(Date.UTC(year,month,15,12))));
  return detailUtcOffsetMinutes(timezone,date)>Math.min(...offsets);
}

function detailOffsetLabel(minutes){if(!Number.isFinite(minutes))return'—';const sign=minutes>=0?'+':'−',absolute=Math.abs(minutes);return`UTC${sign}${String(Math.floor(absolute/60)).padStart(2,'0')}:${String(absolute%60).padStart(2,'0')}`}

function currentAirportDaylight(now){
  const metadata=detailAirportMetadata,timezone=metadata?.timezone,daylight=metadata?.daylight;if(typeof timezone!=='string'||!timezone.trim()||!daylight||typeof daylight!=='object')return null;
  try{if(daylight.date!==detailLocalDate(timezone,now))return null;const formatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'});return{formatter,sunrise:detailIsoTimestamp(daylight.sunriseUtc),noon:detailIsoTimestamp(daylight.solarNoonUtc),sunset:detailIsoTimestamp(daylight.sunsetUtc),seconds:airportFiniteNumber(daylight.dayLengthSeconds)?Number(daylight.dayLengthSeconds):null,state:daylight.state}}catch{return null}
}

function renderAirportDynamicTimeFields(now=new Date(Date.now())){
  const timezone=detailAirportMetadata?.timezone;airportSetText('airport-info-local-time','—');airportSetText('airport-info-utc-offset','—');
  if(typeof timezone!=='string'||!timezone.trim()||!Number.isFinite(now.getTime()))return;
  try{
    const formatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezone,dateStyle:'medium',timeStyle:'short',hourCycle:'h23'}),offset=detailUtcOffsetMinutes(timezone,now),dst=detailDstActive(timezone,now),daylight=currentAirportDaylight(now);
    airportSetText('airport-info-local-time',formatter.format(now));airportSetText('airport-info-utc-offset',detailOffsetLabel(offset));
    if(document.getElementById('airport-description-time')){
      const daylightMinutes=daylight?.seconds!=null?Math.round(daylight.seconds/60):null;
      const sunlight=daylight&&daylight.sunrise&&daylight.sunset?` Vandaag komt de zon op om ${daylight.formatter.format(daylight.sunrise)} en gaat zij onder om ${daylight.formatter.format(daylight.sunset)}${daylight.noon?`; solar noon is om ${daylight.formatter.format(daylight.noon)}`:''}${daylightMinutes!=null?` en de daglengte is ${Math.floor(daylightMinutes/60)} h ${daylightMinutes%60} min`:''}.`:'';
      airportSetText('airport-description-time',`${detailAirport?.name||'Dit vliegveld'} gebruikt de timezone ${timezone} (${detailOffsetLabel(offset)}). Daylight saving time is ${dst?'actief':'niet actief'}.${sunlight}`);
    }
  }catch{airportSetText('airport-info-local-time','—');airportSetText('airport-info-utc-offset','—')}
}

function renderAirportInfo(){
  const metadata=detailAirportMetadata||{},coordinates=metadata.sourceCoordinates||{},hasCoordinates=airportFiniteNumber(coordinates.latitude)&&airportFiniteNumber(coordinates.longitude),latitude=hasCoordinates?Number(coordinates.latitude):null,longitude=hasCoordinates?Number(coordinates.longitude):null,runwayNames=allAirportRunways.map(runwayLabel).filter(label=>label!=='—');
  airportSetText('airport-tab-title',`About ${metadata.name||detailAirport.name}`);airportSetText('airport-description-title',metadata.name||detailAirport.name);
  airportSetText('airport-info-icao',detailAirport.icao);airportSetText('airport-info-iata',airportValue(metadata.iata));airportSetText('airport-info-fir',airportValue(metadata.fir));airportSetText('airport-info-region',airportValue(metadata.regionName));airportSetText('airport-info-country',airportValue(metadata.countryName||detailAirport.country));airportSetText('airport-info-runways',runwayNames.length?runwayNames.join(', '):'—');airportSetText('airport-info-type',airportTypeLabel(metadata.type));
  airportSetText('airport-info-elevation',airportFiniteNumber(metadata.elevationFt)?`${Math.round(Number(metadata.elevationFt))} ft${airportFiniteNumber(metadata.elevationM)?` / ${Number(metadata.elevationM).toFixed(1)} m`:''}`:'—');
  airportSetText('airport-info-coordinates',Number.isFinite(latitude)&&Number.isFinite(longitude)?`${latitude.toFixed(6)}, ${longitude.toFixed(6)}`:'—');airportSetText('airport-info-dms',Number.isFinite(latitude)&&Number.isFinite(longitude)?`${decimalCoordinateToDms(latitude,'latitude')} ${decimalCoordinateToDms(longitude,'longitude')}`:'—');
  airportSetText('airport-info-variation',airportFiniteNumber(metadata.magneticVariationDeg)?`${Number(metadata.magneticVariationDeg).toFixed(1)}° ${metadata.magneticVariationDirection||''} (${metadata.magneticVariationYear||'—'}) · ${metadata.magneticVariationSource||'—'}`:'—');airportSetText('airport-info-timezone',airportValue(metadata.timezone));airportSetText('airport-info-source',Number.isInteger(metadata.ourAirportsId)?`OurAirports #${metadata.ourAirportsId} (${detailAirport.icao})`:'—');
  renderAirportDynamicTimeFields();
}

function appendAirportCell(row,text,tag='td'){const cell=document.createElement(tag);cell.textContent=text??'—';row.appendChild(cell);return cell}

function renderAirportRunwayDetails(){
  const body=document.getElementById('airport-runway-details');body.replaceChildren();
  if(!allAirportRunways.length){const row=document.createElement('tr'),cell=appendAirportCell(row,'Geen runwaydata beschikbaar.');cell.colSpan=11;body.appendChild(row);return}
  allAirportRunways.forEach(runway=>{
    const shared={length:airportFiniteNumber(runway.length_m)?`${Number(runway.length_m).toFixed(1)} m`:'—',width:airportFiniteNumber(runway.width_m)?`${Number(runway.width_m).toFixed(1)} m`:'—',surface:airportValue(runway.surface),status:runway.closed?'Closed':runway.lighted?'Open · lighted':'Open · unlighted'};
    [{prefix:'le',ident:runway.le_ident},{prefix:'he',ident:runway.he_ident}].forEach(end=>{
      const row=document.createElement('tr');appendAirportCell(row,airportValue(end.ident));appendAirportCell(row,airportHeading(runway[`${end.prefix}_heading_degT`]));appendAirportCell(row,airportHeading(runway[`${end.prefix}_heading_degM`]));appendAirportCell(row,shared.length);appendAirportCell(row,shared.width);appendAirportCell(row,shared.surface);appendAirportCell(row,airportFiniteNumber(runway[`${end.prefix}_latitude_deg`])?Number(runway[`${end.prefix}_latitude_deg`]).toFixed(7):'—');appendAirportCell(row,airportFiniteNumber(runway[`${end.prefix}_longitude_deg`])?Number(runway[`${end.prefix}_longitude_deg`]).toFixed(7):'—');appendAirportCell(row,airportFiniteNumber(runway[`${end.prefix}_elevation_ft`])?`${Math.round(Number(runway[`${end.prefix}_elevation_ft`]))} ft`:'—');appendAirportCell(row,airportFiniteNumber(runway[`${end.prefix}_displaced_threshold_ft`])?`${Math.round(Number(runway[`${end.prefix}_displaced_threshold_ft`]))} ft`:'—');appendAirportCell(row,shared.status);body.appendChild(row);
    });
  });
  const model=detailAirportMetadata?.magneticVariationSource||'het geconfigureerde World Magnetic Model';airportSetText('airport-runway-notice',`De airport kan gesloten zijn of voorafgaande toestemming vereisen. True headings en runway-endcoördinaten kunnen broninschattingen zijn. Magnetic headings zijn backendmatig afgeleid met ${model}.`);
}

function renderAirportDescription(){
  const metadata=detailAirportMetadata||{},coordinates=metadata.sourceCoordinates||{},hasCoordinates=airportFiniteNumber(coordinates.latitude)&&airportFiniteNumber(coordinates.longitude),latitude=hasCoordinates?Number(coordinates.latitude):null,longitude=hasCoordinates?Number(coordinates.longitude):null,nearby=airportGeodata?.nearby||[],runways=allAirportRunways.map(runwayLabel).filter(value=>value!=='—'),location=[metadata.municipality,metadata.regionName,metadata.countryName||detailAirport.country].filter(value=>typeof value==='string'&&value.trim()).join(', '),nearbyNames=nearby.slice(0,5).map(record=>record.name).filter(Boolean);
  const runwayText=runways.length===1?`één runway: ${runways[0]}`:runways.length?`${runways.length} runways: ${runways.join(', ')}`:'geen runwaydata';
  airportSetText('airport-description-main',`${metadata.name||detailAirport.name} is een ${airportTypeLabel(metadata.type).toLowerCase()}${location?` in ${location}`:''}.${Number.isFinite(latitude)&&Number.isFinite(longitude)?` De airport ligt op latitude ${latitude.toFixed(6)} en longitude ${longitude.toFixed(6)}.`:''} De backend bevat ${runwayText}.${nearbyNames.length?` Nabije locaties zijn onder andere ${nearbyNames.join(', ')}.`:''}`);
  airportSetText('airport-description-codes',`De ICAO-code is ${detailAirport.icao}${metadata.iata?` en de IATA-code is ${metadata.iata}`:''}.${metadata.fir?` De flight information region is ${metadata.fir}.`:''}${detailMetar?' Voor deze airport is een actuele lokale METAR beschikbaar.':''}`);renderAirportDynamicTimeFields();
}

function airportLocationIcon(type){if(type==='heliport')return'Ⓗ';if(type==='closed'||type==='closed_airport')return'●';if(type==='seaplane_base')return'≈';return'●'}
function navaidIcon(type){return type==='NDB'||type==='NDB-DME'?'◉':'⬡'}
function formatNavaidFrequency(record){if(!airportFiniteNumber(record.frequencyKhz))return'—';const frequency=Number(record.frequencyKhz);return frequency>=1000?`${(frequency/1000).toFixed(frequency%1000?3:1).replace(/0+$/,'').replace(/\.$/,'')} MHz`:`${frequency} kHz`}

function renderAirportNearby(records,error){
  const body=document.getElementById('airport-nearby-body'),status=document.getElementById('airport-nearby-status');body.replaceChildren();
  records.slice(0,10).forEach(record=>{const row=document.createElement('tr');appendAirportCell(row,airportLocationIcon(record.type));appendAirportCell(row,airportValue(record.name));appendAirportCell(row,airportValue(record.ident));appendAirportCell(row,airportFiniteNumber(record.distanceKm)?`${Number(record.distanceKm).toFixed(1)} km`:'—');appendAirportCell(row,airportBearing(record.bearingDeg));body.appendChild(row)});
  status.hidden=records.length>0;status.textContent=error?'Nearby-airportdata kon niet worden geladen.':records.length?'':`Geen nearby airports beschikbaar voor ${detailAirport.icao}.`;
}

function renderAirportNavaids(records,error){
  const body=document.getElementById('airport-navaid-body'),status=document.getElementById('airport-navaid-status');body.replaceChildren();
  records.slice(0,10).forEach(record=>{const row=document.createElement('tr');appendAirportCell(row,navaidIcon(record.type));appendAirportCell(row,airportValue(record.name));appendAirportCell(row,airportValue(record.ident));appendAirportCell(row,airportValue(record.type));appendAirportCell(row,formatNavaidFrequency(record));appendAirportCell(row,airportFiniteNumber(record.distanceKm)?`${Number(record.distanceKm).toFixed(1)} km`:'—');appendAirportCell(row,airportBearing(record.bearingDeg));body.appendChild(row)});
  status.hidden=records.length>0;status.textContent=error?'Navaid-data kon niet worden geladen.':records.length?'':`Geen navigational aids beschikbaar voor ${detailAirport.icao}.`;
}

async function fetchAirportIndexedDataset(url,listField){
  const response=await fetch(url);if(!response.ok)throw Error(`${url}: HTTP ${response.status}`);const documentData=await response.json();const airports=documentData&&typeof documentData==='object'&&!Array.isArray(documentData)&&documentData.airports&&typeof documentData.airports==='object'&&!Array.isArray(documentData.airports)?documentData.airports:null;if(!airports)throw Error(`${url}: airports-object ontbreekt`);const group=airports[detailAirport.icao];return group&&Array.isArray(group[listField])?group[listField]:[];
}

function loadAirportGeodata(){
  if(!airportGeodataLoad)airportGeodataLoad=Promise.allSettled([fetchAirportIndexedDataset('data/airport-nearby.json','nearby'),fetchAirportIndexedDataset('data/airport-navaids.json','navaids')]).then(results=>{airportGeodata={nearby:results[0].status==='fulfilled'?results[0].value:[],navaids:results[1].status==='fulfilled'?results[1].value:[],nearbyError:results[0].status==='rejected',navaidsError:results[1].status==='rejected'};if(results[0].status==='rejected')console.warn('Nearby-airportdata is niet beschikbaar.',results[0].reason);if(results[1].status==='rejected')console.warn('Navaid-data is niet beschikbaar.',results[1].reason);return airportGeodata});
  return airportGeodataLoad;
}

function airportMapLabel(text){const element=document.createElement('span');element.textContent=text;return element}
function airportMapCoordinates(record){if(!airportFiniteNumber(record.latitude)||!airportFiniteNumber(record.longitude))return null;return[Number(record.latitude),Number(record.longitude)]}

function initializeAirportMap(){
  if(airportTabMap)return true;const mapElement=document.getElementById('airport-tab-map'),status=document.getElementById('airport-map-status');if(!mapElement||typeof window.L!=='object'){status.textContent='De kaartbibliotheek kon niet worden geladen.';return false}
  const coordinates=detailAirportMetadata?.sourceCoordinates||{};if(!airportFiniteNumber(coordinates.latitude)||!airportFiniteNumber(coordinates.longitude)){status.textContent='Geen geldige airportcoördinaten beschikbaar.';return false}const latitude=Number(coordinates.latitude),longitude=Number(coordinates.longitude);
  airportTabMap=L.map(mapElement,{zoomControl:true,minZoom:2}).setView([latitude,longitude],13);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap contributors'}).addTo(airportTabMap);L.control.scale({imperial:true}).addTo(airportTabMap);airportTabMapLayers=L.layerGroup().addTo(airportTabMap);status.hidden=true;return true;
}

function renderAirportMap(){
  if(!initializeAirportMap())return;airportTabMapLayers.clearLayers();const coordinates=detailAirportMetadata.sourceCoordinates,center=[Number(coordinates.latitude),Number(coordinates.longitude)],fitPoints=[center];
  L.circleMarker(center,{radius:8,color:'#fff',weight:2,fillColor:'#27b84f',fillOpacity:1}).bindTooltip(airportMapLabel(`${detailAirport.icao} · ${detailAirportMetadata.name||detailAirport.name}`)).addTo(airportTabMapLayers);
  allAirportRunways.forEach(runway=>{const start=airportMapCoordinates({latitude:runway.le_latitude_deg,longitude:runway.le_longitude_deg}),end=airportMapCoordinates({latitude:runway.he_latitude_deg,longitude:runway.he_longitude_deg});if(!start||!end)return;fitPoints.push(start,end);L.polyline([start,end],{color:runway.closed?'#e35c65':'#202830',weight:6,opacity:.85,dashArray:runway.closed?'7 6':null}).bindTooltip(airportMapLabel(`Runway ${runwayLabel(runway)}${runway.closed?' · closed':''}`)).addTo(airportTabMapLayers)});
  (airportGeodata?.nearby||[]).forEach(record=>{const point=airportMapCoordinates(record);if(!point)return;L.circleMarker(point,{radius:5,color:'#fff',weight:1,fillColor:record.closed?'#68737c':'#3388ff',fillOpacity:.9}).bindTooltip(airportMapLabel(`${record.ident||'—'} · ${record.name||'Airport'}`)).addTo(airportTabMapLayers)});
  (airportGeodata?.navaids||[]).forEach(record=>{const point=airportMapCoordinates(record);if(!point)return;L.circleMarker(point,{radius:4,color:'#ffd166',weight:2,fillColor:'#075da2',fillOpacity:1}).bindTooltip(airportMapLabel(`${record.ident||'—'} · ${record.type||'Navaid'}`)).addTo(airportTabMapLayers)});
  if(!airportTabMapFitted&&fitPoints.length>1){airportTabMap.fitBounds(fitPoints,{padding:[28,28],maxZoom:14});airportTabMapFitted=true}requestAnimationFrame(()=>airportTabMap.invalidateSize());
}

function renderAirportTabStatic(){
  renderAirportInfo();renderAirportRunwayDetails();renderAirportDescription();
  const nearbyBody=document.getElementById('airport-nearby-body'),nearbyStatus=document.getElementById('airport-nearby-status'),navaidBody=document.getElementById('airport-navaid-body'),navaidStatus=document.getElementById('airport-navaid-status');
  nearbyBody.replaceChildren();navaidBody.replaceChildren();nearbyStatus.hidden=false;navaidStatus.hidden=false;nearbyStatus.textContent='Nearby airports laden…';navaidStatus.textContent='Navigational aids laden…';
}
function ensureAirportTab(){
  if(airportTabLoad)return airportTabLoad;renderAirportTabStatic();renderAirportMap();airportTabLoad=loadAirportGeodata().then(data=>{renderAirportNearby(data.nearby,data.nearbyError);renderAirportNavaids(data.navaids,data.navaidsError);renderAirportDescription();renderAirportMap();return data});return airportTabLoad;
}

function requestedIcao(){
  const query=new URLSearchParams(location.search).get('icao');
  const pathMatch=location.pathname.match(/\/metar\/([A-Z]{4})\/?$/i);
  return(query||pathMatch?.[1]||'').trim().toUpperCase();
}

function variableWindRange(raw){
  const match=(raw||'').match(/(?:^|\s)(\d{3})V(\d{3})(?=\s|$)/);
  return match?[Number(match[1]),Number(match[2])]:null;
}

function weatherSymbol(m){const raw=m?.rawOb||'';
  if(/TS/.test(raw))return'⛈️';
  if(/RA|DZ|SH/.test(raw))return'🌧️';
  if(/FG|BR|HZ/.test(raw))return'🌫️';
  if(/OVC/.test(raw))return'☁️';
  if(/BKN|SCT/.test(raw))return'🌤️';
  return'☀️';
}

function weatherText(m){
  if(m.wxString)return m.wxString;
  if(/\bCAVOK\b/.test(m.rawOb||''))return'CAVOK';
  const covers=(m.clouds||[]).map(layer=>layer.cover).filter(Boolean);
  return covers.length?[...new Set(covers)].join(' · '):'—';
}

function signedAngleDifference(wind,heading){return((Number(wind)-Number(heading)+540)%360)-180}
function absoluteAngleDifference(wind,heading){return Math.abs(signedAngleDifference(wind,heading))}

function runwayEnds(runway){return[
  {heading:runway.le_heading_degT,ident:runway.le_ident,opposite:runway.he_ident},
  {heading:runway.he_heading_degT,ident:runway.he_ident,opposite:runway.le_ident}
].filter(end=>end.ident&&end.opposite&&end.heading!=null&&Number.isFinite(Number(end.heading))).map(end=>({...end,heading:Number(end.heading)}))}

function endForWind(runway){
  const ends=runwayEnds(runway);
  if(!ends.length)return null;
  if(detailMetar.wdir==null||!Number.isFinite(Number(detailMetar.wdir)))return ends[0];
  return ends.sort((a,b)=>absoluteAngleDifference(detailMetar.wdir,a.heading)-absoluteAngleDifference(detailMetar.wdir,b.heading)||a.ident.localeCompare(b.ident))[0];
}

function chooseInitialRunway(runways){
  return runways.slice().sort((a,b)=>{
    const aEnd=endForWind(a),bEnd=endForWind(b);
    const aDifference=aEnd&&detailMetar.wdir!=null?absoluteAngleDifference(detailMetar.wdir,aEnd.heading):Infinity;
    const bDifference=bEnd&&detailMetar.wdir!=null?absoluteAngleDifference(detailMetar.wdir,bEnd.heading):Infinity;
    return aDifference-bDifference||(b.length_ft||0)-(a.length_ft||0)||runwayLabel(a).localeCompare(runwayLabel(b));
  })[0]||null;
}

function runwayLabel(runway){return[runway.le_ident,runway.he_ident].filter(Boolean).join('/')||'—'}

function headingValue(value){
  if(value==null||!Number.isFinite(Number(value)))return'—';
  const number=Number(value);
  return Number.isInteger(number)?String(number).padStart(3,'0'):number.toFixed(1).padStart(5,'0');
}

function windComponents(runway){
  const end=endForWind(runway),speed=Number(detailMetar.wspd),wind=Number(detailMetar.wdir);
  if(!end||detailMetar.wdir==null||!Number.isFinite(wind)||!Number.isFinite(speed))return null;
  const angle=signedAngleDifference(wind,end.heading)*Math.PI/180;
  return{end,crosswind:speed*Math.sin(angle),headwind:speed*Math.cos(angle),percentage:Math.abs(Math.sin(angle))*100};
}

function relativeHumidity(temp,dewp){
  const temperature=Number(temp),dewpoint=Number(dewp);
  if(!Number.isFinite(temperature)||!Number.isFinite(dewpoint))return null;
  const saturation=Math.exp((17.625*temperature)/(243.04+temperature));
  const actual=Math.exp((17.625*dewpoint)/(243.04+dewpoint));
  return Math.round(Math.max(0,Math.min(100,100*actual/saturation)));
}

function windMeterPoint(value,radius){
  const speed=Math.max(0,Math.min(WIND_METER_MAX,Number(value)||0));
  const progress=speed/WIND_METER_MAX,scale=radius/250,angle=Math.PI/2+progress*Math.PI;
  return{x:WIND_METER_CENTER.x+Math.cos(angle)*WIND_METER_RADIUS*scale,y:WIND_METER_CENTER.y+Math.sin(angle)*WIND_METER_RADIUS*scale};
}

function renderWindSpeedMeter(){
  const svgNamespace='http://www.w3.org/2000/svg',ticks=document.getElementById('wind-speed-ticks');
  if(!ticks.childElementCount){
    for(let value=0;value<=WIND_METER_MAX;value+=2.5){
      const major=value%5===0,outer=windMeterPoint(value,250),inner=windMeterPoint(value,major?228:238);
      const line=document.createElementNS(svgNamespace,'line');line.setAttribute('x1',inner.x);line.setAttribute('y1',inner.y);line.setAttribute('x2',outer.x);line.setAttribute('y2',outer.y);if(!major)line.classList.add('minor');ticks.appendChild(line);
      if(major){
        const labelPoint=windMeterPoint(value,205),label=document.createElementNS(svgNamespace,'text');label.setAttribute('x',labelPoint.x);label.setAttribute('y',labelPoint.y);label.textContent=String(value);ticks.appendChild(label);
      }
    }
  }
  const speed=Number(detailMetar.wspd),hasSpeed=detailMetar.wspd!=null&&Number.isFinite(speed),needle=document.getElementById('wind-speed-needle');
  const needlePoint=windMeterPoint(hasSpeed?speed:0,215);needle.setAttribute('x2',needlePoint.x);needle.setAttribute('y2',needlePoint.y);needle.style.opacity=hasSpeed?'1':'.25';
  document.getElementById('wind-speed-value').textContent=hasSpeed?String(speed):'—';
  const gust=Number(detailMetar.wgst),hasGust=detailMetar.wgst!=null&&Number.isFinite(gust),gustLine=document.getElementById('wind-gust-indicator'),gustReading=document.getElementById('wind-gust-reading');
  gustLine.toggleAttribute('hidden',!hasGust);gustReading.hidden=!hasGust;
  if(hasGust){const gustPoint=windMeterPoint(gust,242);gustLine.setAttribute('x2',gustPoint.x);gustLine.setAttribute('y2',gustPoint.y);gustReading.textContent=`Gust ${gust} kt`}
  document.getElementById('wind-speed-description').textContent=hasGust?`Actuele wind ${speed} knots met windstoten tot ${gust} knots op een schaal tot ${WIND_METER_MAX} knots.`:`Actuele wind ${hasSpeed?speed:'onbekend'} knots op een schaal tot ${WIND_METER_MAX} knots.`;
}

function renderMetrics(){
  const cat=detailMetar.fltCat||'UNKNOWN';
  const category=document.getElementById('metric-category');
  category.textContent=cat;category.style.setProperty('--category-color',CATEGORY_COLORS[cat]||CATEGORY_COLORS.UNKNOWN);
  document.getElementById('metric-weather').textContent=`${weatherSymbol(detailMetar)} ${detailMetar.temp!=null?`${detailMetar.temp} °C`:'—'}`;
  document.getElementById('metric-weather-detail').textContent=weatherText(detailMetar);
  const variable=variableWindRange(detailMetar.rawOb);
  const direction=detailMetar.wdir!=null?`${String(detailMetar.wdir).padStart(3,'0')}°`:/\bVRB\d{2,3}(?:G\d{2,3})?KT\b/.test(detailMetar.rawOb||'')?'VRB':'—';
  const gust=detailMetar.wgst!=null?` G${detailMetar.wgst}`:'';
  document.getElementById('metric-wind').textContent=detailMetar.wspd!=null?`${detailMetar.wspd}${gust} kt`:'—';
  document.getElementById('metric-wind-detail').textContent=variable?`${direction} (${variable[0]}°–${variable[1]}°)`:direction;
  document.getElementById('metric-visibility').textContent=detailMetar.visib!=null?`${detailMetar.visib} SM`:'—';
  document.getElementById('metric-ceiling').textContent=detailMetar.ceiling!=null?`${Number(detailMetar.ceiling).toLocaleString('nl-NL')} ft`:/\b(?:CAVOK|NSC|SKC)\b/.test(detailMetar.rawOb||'')?'None':'—';
  document.getElementById('metric-qnh').textContent=detailMetar.altim!=null?`${detailMetar.altim} hPa`:'—';
}

function renderWindLayers(){
  const arrow=document.getElementById('detail-wind-arrow'),minimum=document.getElementById('detail-wind-min'),maximum=document.getElementById('detail-wind-max');
  const hasDirection=detailMetar.wdir!=null&&Number.isFinite(Number(detailMetar.wdir));
  arrow.hidden=!hasDirection;arrow.style.transform=hasDirection?`rotate(${Number(detailMetar.wdir)}deg)`:'';
  const variable=variableWindRange(detailMetar.rawOb);
  minimum.hidden=!variable;maximum.hidden=!variable;
  minimum.style.transform=variable?`rotate(${variable[0]}deg)`:'';maximum.style.transform=variable?`rotate(${variable[1]}deg)`:'';
  document.getElementById('compass-wind-label').textContent=detailMetar.wspd!=null?`${hasDirection?`${String(detailMetar.wdir).padStart(3,'0')}°`:'VRB'} · ${detailMetar.wspd}${detailMetar.wgst!=null?` G${detailMetar.wgst}`:''} kt`:'Wind —';
}

function setCompassOrientation(){
  const holder=document.getElementById('compass-holder'),heading=Number(holder.dataset.heading);
  holder.style.transform=holder.classList.contains('active')&&Number.isFinite(heading)?`rotate(${-heading}deg)`:'rotate(0deg)';
}

function renderSelectedComponents(){
  const components=selectedRunway?windComponents(selectedRunway):null;
  document.getElementById('headwind-component').textContent=components?`${Math.round(components.headwind)} kt`:'—';
  document.getElementById('crosswind-component').textContent=components?`${Math.round(Math.abs(components.crosswind))} kt`:'—';
}

function renderRunwayOverlay(){
  const layer=document.getElementById('detail-runway'),holder=document.getElementById('compass-holder'),end=selectedRunway?endForWind(selectedRunway):null;
  layer.toggleAttribute('hidden',!end);layer.style.transform=end?`rotate(${end.heading}deg)`:'';
  document.getElementById('detail-runway-primary').textContent=end?.ident||'—';document.getElementById('detail-runway-opposite').textContent=end?.opposite||'—';
  holder.dataset.heading=end?String(end.heading):'0';
  if(end)Object.assign(layer.dataset,{heading:end.heading,primary:end.ident,opposite:end.opposite});else Object.keys(layer.dataset).forEach(key=>delete layer.dataset[key]);
  setCompassOrientation();renderSelectedComponents();
}

function formatCrosswind(value){
  const rounded=Math.round(Math.abs(value));
  if(!rounded)return'0 kt';
  return`${value>0?'←':'→'} ${rounded} kt`;
}

function appendRunwayLabel(root,runway,end){
  root.classList.add('runway-cell');
  [runway.le_ident,'/',runway.he_ident].forEach((part,index)=>{
    if(part==='/'){root.append('/');return}
    const span=document.createElement('span');span.textContent=part;span.classList.toggle('active',part===end?.ident);root.appendChild(span);
  });
}

function renderRunwayTable(){
  const body=document.getElementById('runway-table');body.innerHTML='';
  if(!airportRunways.length){
    const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=5;cell.textContent='Geen open runway met bruikbare heading beschikbaar.';row.appendChild(cell);body.appendChild(row);return;
  }
  airportRunways.forEach(runway=>{
    const components=windComponents(runway),row=document.createElement('tr');row.classList.toggle('selected',runway===selectedRunway);
    const idCell=document.createElement('td');appendRunwayLabel(idCell,runway,components?.end||endForWind(runway));row.appendChild(idCell);
    const values=[`${headingValue(runway.le_heading_degT)}°–${headingValue(runway.he_heading_degT)}°`,components?formatCrosswind(components.crosswind):'—',components?`${Math.round(components.headwind)} kt`:'—',components?`${Math.round(components.percentage)}%`:'—'];
    values.forEach(value=>{const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell)});body.appendChild(row);
  });
}

function renderRunwayInputs(){
  const root=document.getElementById('runway-inputs');root.innerHTML='';
  airportRunways.forEach(runway=>{
    const button=document.createElement('button');button.type='button';button.className='runway-input';button.classList.toggle('selected',runway===selectedRunway);
    appendRunwayLabel(button,runway,endForWind(runway));button.setAttribute('aria-pressed',String(runway===selectedRunway));button.onclick=()=>selectRunway(runway);root.appendChild(button);
  });
  document.getElementById('change-runway').disabled=airportRunways.length<2;
}

function selectRunway(runway){selectedRunway=runway;renderRunwayOverlay();renderRunwayTable();renderRunwayInputs()}

function renderCeilingVisibility(){
  const raw=detailMetar.rawOb||'',ceiling=Number(detailMetar.ceiling),hasCeiling=detailMetar.ceiling!=null&&Number.isFinite(ceiling),cavok=/\bCAVOK\b/.test(raw);
  const current=document.getElementById('ceiling-current');
  if(hasCeiling){
    const clamped=Math.max(0,Math.min(5000,ceiling));current.textContent=`${ceiling.toLocaleString('nl-NL')} ft`;current.style.top=`${7+(1-clamped/5000)*86}%`;
  }else{current.textContent=cavok?'CAVOK':/\b(?:NSC|SKC)\b/.test(raw)?'No ceiling':'Ceiling —';current.style.top='50%'}
  document.getElementById('visibility-current').textContent=detailMetar.visib!=null?`Visibility ${detailMetar.visib} SM`:'Visibility —';
}

function renderAirportNarrative(){
  const runwayNames=airportRunways.map(runwayLabel);
  const runwayText=runwayNames.length===1?`Het vliegveld heeft één open runway met bruikbare heading: ${runwayNames[0]}.`:runwayNames.length>1?`Het vliegveld heeft ${runwayNames.length} open runways met bruikbare headings: ${runwayNames.join(', ')}.`:'Voor dit vliegveld is geen open runway met een bruikbare heading beschikbaar.';
  document.getElementById('airport-description').textContent=`${detailAirport.name} ligt in ${detailAirport.country} op ${Number(detailAirport.lat).toFixed(5)}, ${Number(detailAirport.lon).toFixed(5)}. De ICAO-code is ${detailAirport.icao}. ${runwayText}`;

  const direction=detailMetar.wdir!=null&&Number.isFinite(Number(detailMetar.wdir))?`${String(detailMetar.wdir).padStart(3,'0')}°`:/\bVRB\d{2,3}(?:G\d{2,3})?KT\b/.test(detailMetar.rawOb||'')?'variabel':'niet beschikbaar';
  const speed=detailMetar.wspd!=null?`${detailMetar.wspd} kt`:'niet beschikbaar';
  const gust=detailMetar.wgst!=null?` met windstoten tot ${detailMetar.wgst} kt`:'';
  const variable=variableWindRange(detailMetar.rawOb);
  document.getElementById('observation-wind').textContent=`De windrichting is ${direction} met een snelheid van ${speed}${gust}${variable?`; de gemelde variabele sector loopt van ${variable[0]}° tot ${variable[1]}°`:''}.`;

  const raw=detailMetar.rawOb||'',conditions=[];
  if(/\bCAVOK\b/.test(raw))conditions.push('De METAR meldt CAVOK');
  else{
    conditions.push(detailMetar.visib!=null?`Het zicht is ${detailMetar.visib} SM`:'Het zicht is niet beschikbaar');
    if(detailMetar.ceiling!=null)conditions.push(`de ceiling is ${Number(detailMetar.ceiling).toLocaleString('nl-NL')} ft`);
    else if(/\b(?:NSC|SKC|NCD)\b/.test(raw))conditions.push('er is geen ceiling gemeld');
    else if((detailMetar.clouds||[]).length)conditions.push('er is geen BKN-, OVC- of VV-ceiling gemeld');
    else conditions.push('de ceiling is niet beschikbaar');
  }
  if(detailMetar.wxString)conditions.push(`de actuele weercode is ${detailMetar.wxString}`);
  document.getElementById('observation-conditions').textContent=`${conditions.join(', ')}.`;

  const humidity=relativeHumidity(detailMetar.temp,detailMetar.dewp),temperatureParts=[];
  temperatureParts.push(detailMetar.temp!=null?`De temperatuur is ${detailMetar.temp} °C`:'De temperatuur is niet beschikbaar');
  temperatureParts.push(detailMetar.dewp!=null?`het dauwpunt is ${detailMetar.dewp} °C`:'het dauwpunt is niet beschikbaar');
  if(humidity!=null)temperatureParts.push(`de berekende relatieve luchtvochtigheid is ${humidity}%`);
  document.getElementById('observation-temperature').textContent=`${temperatureParts.join(', ')}.`;
  document.getElementById('observation-pressure').textContent=detailMetar.altim!=null?`De luchtdruk op zeeniveau is ${detailMetar.altim} hPa (QNH).`:'QNH is niet beschikbaar.';

  document.getElementById('weather-temperature').textContent=detailMetar.temp!=null?`${detailMetar.temp} °C`:'Niet beschikbaar';
  document.getElementById('weather-dewpoint').textContent=detailMetar.dewp!=null?`${detailMetar.dewp} °C`:'Niet beschikbaar';
  document.getElementById('weather-humidity').textContent=humidity!=null?`${humidity}%`:'Niet berekend';
}

function renderMetarDaylightContent(){
  const visual=document.querySelector('.metar-daylight-visual'),status=document.getElementById('metar-daylight-status'),summary=document.getElementById('metar-daylight-summary'),sun=document.getElementById('metar-daylight-sun'),daylight=detailAirportMetadata?.daylight,timezone=detailAirportMetadata?.timezone;
  const setUnavailable=message=>{summary.textContent=message;visual.classList.add('unavailable');status.hidden=false;status.textContent=message;['metar-daylight-sunrise','metar-daylight-noon','metar-daylight-sunset'].forEach(id=>airportSetText(id,'—'))};
  document.getElementById('metar-experiences-copy').textContent=`Deze projectpagina visualiseert beschikbare luchtvaartweer- en luchthavengegevens voor ${detailAirport.name} (${detailAirport.icao}).`;
  document.getElementById('metar-project-summary-copy').textContent=`Deze weergave combineert de actuele beschikbare METAR voor ${detailAirport.icao} met luchthaven- en baangegevens uit het project.`;
  visual.classList.remove('unavailable');status.hidden=true;status.textContent='';
  if(!daylight||typeof daylight!=='object'||typeof timezone!=='string'||!timezone.trim()){setUnavailable(`Voor ${detailAirport.icao} is geen daylightmetadata beschikbaar.`);return}
  if(daylight.state==='polar_day'||daylight.state==='polarDay'){setUnavailable(`De daylightmetadata voor ${detailAirport.icao} geeft een pooldag aan.`);return}
  if(daylight.state==='polar_night'||daylight.state==='polarNight'){setUnavailable(`De daylightmetadata voor ${detailAirport.icao} geeft een poolnacht aan.`);return}
  const sunrise=detailIsoTimestamp(daylight.sunriseUtc),noon=detailIsoTimestamp(daylight.solarNoonUtc),sunset=detailIsoTimestamp(daylight.sunsetUtc);
  if(!sunrise||!noon||!sunset||sunrise>=noon||noon>=sunset){setUnavailable(`De daylightmetadata voor ${detailAirport.icao} bevat geen volledige dagperiode.`);return}
  try{
    const timeFormatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}),dateFormatter=new Intl.DateTimeFormat('nl-NL',{timeZone:timezone,day:'numeric',month:'long',year:'numeric'}),sunriseText=timeFormatter.format(sunrise),noonText=timeFormatter.format(noon),sunsetText=timeFormatter.format(sunset),durationSeconds=airportFiniteNumber(daylight.dayLengthSeconds)?Number(daylight.dayLengthSeconds):null,durationMinutes=durationSeconds==null?null:Math.round(durationSeconds/60),durationText=durationMinutes==null?'':` De daglengte is ${Math.floor(durationMinutes/60)} uur en ${durationMinutes%60} minuten.`;
    summary.textContent=`Voor ${dateFormatter.format(sunrise)} komt de zon bij ${detailAirport.name} op om ${sunriseText}, valt solar noon om ${noonText} en gaat de zon onder om ${sunsetText}.${durationText} De daylightstatus voor deze datum is normaal.`;
    airportSetText('metar-daylight-sunrise',sunriseText);airportSetText('metar-daylight-noon',noonText);airportSetText('metar-daylight-sunset',sunsetText);
    const progress=Math.max(0,Math.min(1,(noon.getTime()-sunrise.getTime())/(sunset.getTime()-sunrise.getTime()))),x=52+456*progress,y=126-78*Math.sin(Math.PI*progress);sun.setAttribute('transform',`translate(${x.toFixed(2)} ${y.toFixed(2)})`);
    document.getElementById('metar-daylight-chart-description').textContent=`Sunrise ${sunriseText}, solar noon ${noonText} en sunset ${sunsetText} voor ${detailAirport.name} op ${dateFormatter.format(sunrise)}.`;
  }catch{setUnavailable(`De daylightmetadata voor ${detailAirport.icao} kon niet in de lokale timezone worden weergegeven.`)}
}

function svgElement(name,attributes={},text=''){
  const element=document.createElementNS(SVG_NAMESPACE,name);
  Object.entries(attributes).forEach(([key,value])=>element.setAttribute(key,String(value)));
  if(text)element.textContent=text;
  return element;
}

function niceHistoryStep(value){
  const power=10**Math.floor(Math.log10(Math.max(value,Number.EPSILON))),fraction=value/power;
  return(fraction<=1?1:fraction<=2?2:fraction<=5?5:10)*power;
}

function historyYDomain(values,{zeroBased,minimumSpan}){
  let minimum=Math.min(...values),maximum=Math.max(...values);
  if(zeroBased){minimum=0;maximum=Math.max(maximum,minimumSpan)}
  else if(minimum===maximum){minimum-=minimumSpan/2;maximum+=minimumSpan/2}
  else{const padding=(maximum-minimum)*.08;minimum-=padding;maximum+=padding}
  const step=niceHistoryStep((maximum-minimum)/4),lower=zeroBased?0:Math.floor(minimum/step)*step,upper=Math.ceil(maximum/step)*step;
  const ticks=[];for(let value=lower;value<=upper+step*.01;value+=step)ticks.push(Number(value.toFixed(6)));
  return{lower,upper:upper===lower?lower+step:upper,step,ticks};
}

function formatHistoryAxis(value,step){
  return Math.abs(step)<1?Number(value.toFixed(1)).toLocaleString('nl-NL'):Math.round(value).toLocaleString('nl-NL');
}

function historyWindow(history,observations){
  let start=Date.parse(history.windowStart),end=Date.parse(history.windowEnd);
  const times=observations.map(observation=>Date.parse(observation.obsTime)).filter(Number.isFinite);
  if(!Number.isFinite(end))end=Math.max(...times);
  if(!Number.isFinite(start)||start>=end)start=end-24*60*60*1000;
  return{start,end};
}

function setHistoryUnavailable(message='Geen historische data beschikbaar'){
  const root=document.getElementById('history-charts');root.dataset.observationCount='0';root.setAttribute('aria-busy','false');
  root.querySelectorAll('.history-chart').forEach(chart=>{
    chart.classList.remove('rendered');chart.dataset.pointCount='0';chart.dataset.segmentCount='0';
    chart.querySelector('svg').replaceChildren();chart.querySelector('.history-chart-status').textContent=message;
  });
}

function renderHistoryChart(history,observations,config){
  const svg=document.getElementById(`history-${config.field}`),chart=svg.closest('.history-chart');svg.replaceChildren();
  const points=observations.map(observation=>({time:Date.parse(observation.obsTime),value:observation[config.field]}));
  const values=points.filter(point=>Number.isFinite(point.time)&&point.value!=null&&Number.isFinite(Number(point.value))).map(point=>Number(point.value));
  chart.dataset.pointCount=String(values.length);
  if(!values.length){chart.classList.remove('rendered');chart.dataset.segmentCount='0';chart.querySelector('.history-chart-status').textContent='Geen historische data beschikbaar';return}

  const plot={left:42,right:354,top:8,bottom:90},window=historyWindow(history,observations),domain=historyYDomain(values,config);
  const x=time=>plot.left+(time-window.start)/(window.end-window.start)*(plot.right-plot.left);
  const y=value=>plot.bottom-(value-domain.lower)/(domain.upper-domain.lower)*(plot.bottom-plot.top);
  svg.append(svgElement('title',{},`${config.title} voor ${detailAirport.icao}`));
  svg.append(svgElement('desc',{},`${values.length} echte historische METAR-waarnemingen binnen het lokale 24-uursvenster.`));
  domain.ticks.forEach(tick=>{
    const tickY=y(tick);svg.append(svgElement('path',{class:'history-chart-grid',d:`M${plot.left} ${tickY}H${plot.right}`}));
    svg.append(svgElement('text',{class:'history-chart-label',x:plot.left-6,y:tickY+4,'text-anchor':'end'},formatHistoryAxis(tick,domain.step)));
  });
  [window.start,(window.start+window.end)/2,window.end].forEach(time=>svg.append(svgElement('path',{class:'history-chart-grid',d:`M${x(time)} ${plot.top}V${plot.bottom}`})));
  svg.append(svgElement('path',{class:'history-chart-axis',d:`M${plot.left} ${plot.top}V${plot.bottom}H${plot.right}`}));

  let path='',drawing=false,segments=0;
  points.forEach(point=>{
    const valid=Number.isFinite(point.time)&&point.value!=null&&Number.isFinite(Number(point.value));
    if(!valid){drawing=false;return}
    path+=`${drawing?'L':'M'}${x(point.time).toFixed(2)} ${y(Number(point.value)).toFixed(2)} `;
    if(!drawing)segments+=1;drawing=true;
  });
  svg.append(svgElement('path',{class:'history-chart-line',d:path.trim()}));
  const timeFormatter=new Intl.DateTimeFormat('nl-NL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  svg.append(svgElement('text',{class:'history-chart-time',x:plot.left,y:106},timeFormatter.format(new Date(window.start))));
  svg.append(svgElement('text',{class:'history-chart-time',x:plot.right,y:106,'text-anchor':'end'},timeFormatter.format(new Date(window.end))));
  svg.append(svgElement('text',{class:'history-chart-title',x:plot.right,y:125},config.title));
  chart.dataset.segmentCount=String(segments);chart.classList.add('rendered');
}

async function loadHistory(icao){
  const root=document.getElementById('history-charts');root.setAttribute('aria-busy','true');
  try{
    const response=await fetch('data/metar-history.json');if(!response.ok)throw Error(`HTTP ${response.status}`);
    const history=await response.json(),station=history?.airports?.[icao],source=station?.observations;
    if(!Array.isArray(source)||!source.length){setHistoryUnavailable();return}
    const observations=source.slice().filter(observation=>Number.isFinite(Date.parse(observation?.obsTime))).sort((a,b)=>Date.parse(a.obsTime)-Date.parse(b.obsTime));
    if(!observations.length){setHistoryUnavailable();return}
    root.dataset.observationCount=String(observations.length);
    HISTORY_CHARTS.forEach(config=>renderHistoryChart(history,observations,config));
    root.setAttribute('aria-busy','false');
  }catch(error){console.warn('Historische METAR-data kon niet worden geladen.',error);setHistoryUnavailable()}
}

function recentHistoryLocalParts(value,timezone){
  const date=new Date(value);if(!Number.isFinite(date.getTime()))return null;
  try{
    const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(date).filter(part=>part.type!=='literal').map(part=>[part.type,part.value]));
    if(!parts.year||!parts.month||!parts.day||!parts.hour||!parts.minute)return null;
    return{date:`${parts.year}-${parts.month}-${parts.day}`,time:`${parts.hour}:${parts.minute}`};
  }catch{return null}
}

function recentHistoryDateLabel(localDate){
  const date=new Date(`${localDate}T12:00:00Z`);if(!Number.isFinite(date.getTime()))return localDate;
  try{return new Intl.DateTimeFormat('nl-NL',{timeZone:'UTC',weekday:'long',day:'2-digit',month:'long',year:'numeric'}).format(date)}catch{return localDate}
}

function recentHistoryNumber(value,suffix,{digits=0}={}){
  const number=Number(value);return value!=null&&Number.isFinite(number)?`${number.toLocaleString('nl-NL',{minimumFractionDigits:digits,maximumFractionDigits:digits})}${suffix}`:'—';
}

function recentHistoryVisibility(observation){
  const source=typeof observation?.sourceVisibility==='string'?observation.sourceVisibility.trim().toUpperCase():'';
  if(source==='9999'||source==='CAVOK')return'10 km+';
  if(/^\d{4}$/.test(source)){
    const metres=Number(source);return metres>=1000?`${(metres/1000).toLocaleString('nl-NL',{maximumFractionDigits:1})} km`:`${metres.toLocaleString('nl-NL')} m`;
  }
  const value=Number(observation?.visib);if(observation?.visib==null||!Number.isFinite(value))return'—';
  const prefix=observation.visibilityQualifier==='lessThan'?'<':'';
  const suffix=observation.visibilityQualifier==='greaterThan'?'+':'';
  return`${prefix}${value.toLocaleString('nl-NL',{maximumFractionDigits:2})}${suffix} SM`;
}

function recentHistoryCell(row,value,className=''){
  const cell=document.createElement('td');if(className)cell.className=className;cell.textContent=value;row.appendChild(cell);return cell;
}

function recentHistoryCategoryCell(row,category){
  const cell=document.createElement('td'),codes={VFR:'V',MVFR:'M',IFR:'I',LIFR:'L'},code=codes[category];
  if(code){const badge=document.createElement('span');badge.className=`recent-history-category category-${code.toLowerCase()}`;badge.textContent=code;badge.title=category;cell.appendChild(badge)}else cell.textContent='—';
  row.appendChild(cell);
}

function recentHistoryWeatherCell(row,weather){
  const cell=document.createElement('td'),value=document.createElement('span');value.className='recent-history-weather';value.textContent=typeof weather==='string'&&weather.trim()?weather:'—';cell.appendChild(value);row.appendChild(cell);
}

function recentHistoryWindCell(row,observation){
  const cell=document.createElement('td'),wrap=document.createElement('span');wrap.className='recent-history-wind';
  const value=observation?.windDirection,number=Number(value);
  if(value==='VRB')wrap.textContent='VRB';
  else if(value!=null&&Number.isFinite(number)){
    const svg=document.createElementNS(SVG_NAMESPACE,'svg');svg.classList.add('recent-history-wind-arrow');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.style.transform=`rotate(${number}deg)`;
    const path=document.createElementNS(SVG_NAMESPACE,'path');path.setAttribute('d','M12 2V22M12 2 7 9M12 2 17 9');svg.appendChild(path);wrap.appendChild(svg);
    const degrees=document.createElement('span');degrees.textContent=`${String(Math.round(number)).padStart(3,'0')}°`;wrap.appendChild(degrees);
  }else wrap.textContent='—';
  const variable=observation?.variableWind;
  if(variable&&Number.isFinite(Number(variable.from))&&Number.isFinite(Number(variable.to))){const range=document.createElement('small');range.textContent=`${String(Math.round(Number(variable.from))).padStart(3,'0')}°–${String(Math.round(Number(variable.to))).padStart(3,'0')}°`;wrap.appendChild(range)}
  cell.appendChild(wrap);row.appendChild(cell);
}

function recentHistoryCeiling(observation){
  if(observation?.ceilingFt!=null&&Number.isFinite(Number(observation.ceilingFt)))return recentHistoryNumber(observation.ceilingFt,' ft');
  if(observation?.verticalVisibilityFt!=null&&Number.isFinite(Number(observation.verticalVisibilityFt)))return`VV ${recentHistoryNumber(observation.verticalVisibilityFt,' ft')}`;
  return'—';
}

function recentHistorySpeed(observation){
  const speed=recentHistoryNumber(observation?.windSpeed,' kt');
  if(observation?.windGust==null||!Number.isFinite(Number(observation.windGust)))return speed;
  return`${speed} / G${recentHistoryNumber(observation.windGust,' kt')}`;
}

function recentHistoryRawCell(row,observation){
  const cell=document.createElement('td');cell.className='recent-history-raw';
  const type=typeof observation?.metarType==='string'?observation.metarType.trim().toUpperCase():'',rawText=typeof observation?.rawOb==='string'&&observation.rawOb.trim()?observation.rawOb.trim():'—';
  if(type&&rawText!=='—'&&!rawText.toUpperCase().startsWith(`${type} `)){const label=document.createElement('span');label.className='recent-history-report-type';label.textContent=type;cell.appendChild(label)}
  const raw=document.createElement('code');raw.textContent=rawText;cell.appendChild(raw);row.appendChild(cell);
}

function renderRecentHistoryUnavailable(message){
  document.getElementById('recent-history-loading').hidden=true;document.getElementById('recent-history-content').hidden=true;
  const unavailable=document.getElementById('recent-history-unavailable');unavailable.hidden=false;document.getElementById('recent-history-unavailable-message').textContent=message;
  document.getElementById('history-panel').dataset.observationCount='0';
}

function renderRecentHistory(documentData,icao){
  const timezone=typeof documentData?.timezone==='string'?documentData.timezone.trim():'',source=Array.isArray(documentData?.observations)?documentData.observations:[];
  if(!timezone)throw Error('Timezone ontbreekt');
  try{new Intl.DateTimeFormat('nl-NL',{timeZone:timezone}).format(new Date())}catch{throw Error('Timezone is ongeldig')}
  if(typeof documentData?.icao!=='string'||documentData.icao.toUpperCase()!==icao)throw Error('ICAO in historybestand komt niet overeen');

  const observations=source.map(observation=>({observation,time:Date.parse(observation?.obsTime),local:recentHistoryLocalParts(observation?.obsTime,timezone)})).filter(item=>Number.isFinite(item.time)&&item.local).sort((a,b)=>b.time-a.time);
  if(!observations.length){renderRecentHistoryUnavailable(`Voor ${icao} zijn geen recente observaties beschikbaar.`);return}

  const groups=new Map();observations.forEach(item=>{if(!groups.has(item.local.date))groups.set(item.local.date,[]);groups.get(item.local.date).push(item)});
  const groupsRoot=document.getElementById('recent-history-groups');groupsRoot.replaceChildren();
  groups.forEach((items,localDate)=>{
    const section=document.createElement('section');section.className='recent-history-day';
    const heading=document.createElement('h2');heading.textContent=recentHistoryDateLabel(localDate);section.appendChild(heading);
    const scroll=document.createElement('div');scroll.className='table-scroll';
    const table=document.createElement('table');table.className='recent-history-table';
    const thead=document.createElement('thead'),headRow=document.createElement('tr');
    ['Time','Code','Weather','Temp.','Visibility','Ceiling','Wind direction','Wind speed / gust','METAR/SPECI'].forEach(label=>{const th=document.createElement('th');th.scope='col';th.textContent=label;headRow.appendChild(th)});
    thead.appendChild(headRow);table.appendChild(thead);
    const tbody=document.createElement('tbody');
    items.forEach(({observation,local})=>{
      const row=document.createElement('tr');
      recentHistoryCell(row,local.time);recentHistoryCategoryCell(row,observation.flightCategory);recentHistoryWeatherCell(row,observation.weatherRaw);recentHistoryCell(row,recentHistoryNumber(observation.temp,' °C'));recentHistoryCell(row,recentHistoryVisibility(observation));recentHistoryCell(row,recentHistoryCeiling(observation));recentHistoryWindCell(row,observation);recentHistoryCell(row,recentHistorySpeed(observation));recentHistoryRawCell(row,observation);tbody.appendChild(row);
    });
    table.appendChild(tbody);scroll.appendChild(table);section.appendChild(scroll);groupsRoot.appendChild(section);
  });
  document.getElementById('recent-history-summary').textContent=`${observations.length} observaties · ${timezone}`;
  document.getElementById('recent-history-loading').hidden=true;document.getElementById('recent-history-unavailable').hidden=true;document.getElementById('recent-history-content').hidden=false;document.getElementById('history-panel').dataset.observationCount=String(observations.length);
}

async function loadRecentHistory(icao){
  if(recentHistoryLoadedIcao===icao)return;
  if(recentHistoryLoad)return recentHistoryLoad;
  document.getElementById('recent-history-loading').hidden=false;document.getElementById('recent-history-unavailable').hidden=true;document.getElementById('recent-history-content').hidden=true;
  recentHistoryLoad=(async()=>{
    try{
      const response=await fetch(`data/metar-recent/${encodeURIComponent(icao)}.json`);if(!response.ok)throw Error(`HTTP ${response.status}`);
      const documentData=await response.json();renderRecentHistory(documentData,icao);recentHistoryLoadedIcao=icao;
    }catch(error){console.warn(`Recente METAR-history voor ${icao} kon niet worden geladen.`,error);renderRecentHistoryUnavailable(`Voor ${detailAirport?.name||icao} (${icao}) kon geen recente METAR-history worden geladen.`)}
    finally{recentHistoryLoad=null}
  })();
  return recentHistoryLoad;
}

function tafDate(value){
  const date=new Date(value);
  return Number.isFinite(date.getTime())?date:null;
}

function formatTafDateTime(value,{weekday=false}={}){
  const date=tafDate(value);if(!date)return'—';
  const options={timeZone:'UTC',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'};
  if(weekday)options.weekday='short';
  return`${new Intl.DateTimeFormat('nl-NL',options).format(date)} UTC`;
}

function formatTafHour(date){
  return new Intl.DateTimeFormat('nl-NL',{timeZone:'UTC',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(date);
}

function formatTafDay(date){
  return new Intl.DateTimeFormat('nl-NL',{timeZone:'UTC',weekday:'short',day:'2-digit',month:'2-digit'}).format(date);
}

function tafGroupLabel(group){
  const probability=Number.isFinite(Number(group?.probability))?Number(group.probability):null;
  if(group?.type==='PROB_TEMPO')return`${probability!=null?`PROB${probability}`:'PROB'} TEMPO`;
  if(group?.type==='PROB')return probability!=null?`PROB${probability}`:'PROB';
  return group?.type||group?.changeIndicator||'BASELINE';
}

function tafVisibility(group){
  if(group?.visibilitySm==null||!Number.isFinite(Number(group.visibilitySm)))return null;
  const value=Number(group.visibilitySm),number=Number.isInteger(value)?String(value):String(Number(value.toFixed(2)));
  if(group.visibilityQualifier==='greaterThan')return`${number}+ SM`;
  if(group.visibilityQualifier==='lessThan')return`<${number} SM`;
  return`${number} SM`;
}

function tafClouds(group){
  const layers=Array.isArray(group?.cloudLayers)?group.cloudLayers:[];
  return layers.map(layer=>{
    if(!layer?.cover)return null;
    const base=layer.baseFtAgl!=null&&Number.isFinite(Number(layer.baseFtAgl))?` ${Number(layer.baseFtAgl).toLocaleString('nl-NL')} ft`:'';
    return`${layer.cover}${base}`;
  }).filter(Boolean).join(' · ')||null;
}

function tafWeather(group){return group?.weatherRaw||tafClouds(group)}

function tafCeiling(group){
  return group?.ceilingFt!=null&&Number.isFinite(Number(group.ceilingFt))?`${Number(group.ceilingFt).toLocaleString('nl-NL')} ft`:null;
}

function tafWindDirection(group){
  if(group?.windDirection==='VRB')return'VRB';
  return group?.windDirection!=null&&Number.isFinite(Number(group.windDirection))?`${String(Number(group.windDirection)).padStart(3,'0')}°`:null;
}

function tafWindSpeed(group){return group?.windSpeed!=null&&Number.isFinite(Number(group.windSpeed))?`${group.windSpeed} kt`:null}
function tafWindGust(group){return group?.windGust!=null&&Number.isFinite(Number(group.windGust))?`${group.windGust} kt`:null}

function tafHourSlots(station){
  const start=tafDate(station?.validFrom),end=tafDate(station?.validTo);if(!start||!end||start>=end)return[];
  const slots=[];
  for(let cursor=start.getTime();cursor<end.getTime()&&slots.length<72;cursor+=TAF_HOUR_MS){
    slots.push({start:new Date(cursor),end:new Date(Math.min(cursor+TAF_HOUR_MS,end.getTime()))});
  }
  return slots;
}

function tafGroupStart(group){
  const value=group?.type==='BECMG'?(group.becomingCompleteAt||group.validFrom):group?.validFrom;
  return tafDate(value)?.getTime()??NaN;
}

function intervalsOverlap(startA,endA,startB,endB){return startA<endB&&endA>startB}

function tafPrevailingGroup(groups,slot){
  const candidates=groups.filter(group=>TAF_PERMANENT_TYPES.has(group.type)&&Number.isFinite(tafGroupStart(group))&&tafGroupStart(group)<=slot.start.getTime()).sort((a,b)=>tafGroupStart(a)-tafGroupStart(b)||a.sequence-b.sequence);
  return candidates.at(-1)||groups.find(group=>group.type==='BASELINE')||null;
}

function tafApplicableOverlays(groups,slot){
  return groups.filter(group=>{
    let start=tafDate(group.validFrom)?.getTime(),end=tafDate(group.validTo)?.getTime();
    if(group.type==='BECMG')end=tafDate(group.becomingCompleteAt)?.getTime()??Math.min((start??0)+TAF_HOUR_MS,end??Infinity);
    if(!TAF_OVERLAY_TYPES.has(group.type)&&group.type!=='BECMG')return false;
    return Number.isFinite(start)&&Number.isFinite(end)&&intervalsOverlap(start,end,slot.start.getTime(),slot.end.getTime());
  }).sort((a,b)=>a.sequence-b.sequence);
}

function tafTemperatureAtSlot(groups,slot){
  const values=[];
  groups.forEach(group=>(group.temperatureExtremes||[]).forEach(item=>{
    const time=tafDate(item?.validTime)?.getTime();
    if(!Number.isFinite(time)||time<slot.start.getTime()||time>=slot.end.getTime()||item.temperatureC==null)return;
    const kind=item.kind?`${item.kind} `:'';values.push(`${kind}${item.temperatureC} °C`);
  }));
  return[...new Set(values)].join(' · ')||null;
}

function appendTafBadge(root,group,extraClass=''){
  const badge=document.createElement('span');badge.className=`taf-group-badge ${extraClass}`.trim();badge.textContent=tafGroupLabel(group);badge.dataset.groupType=group.type;badge.dataset.groupSequence=String(group.sequence);root.appendChild(badge);
}

function appendTafLayeredValue(cell,prevailing,overlays,valueForGroup){
  const primary=valueForGroup(prevailing);
  if(primary){const value=document.createElement('span');value.className='taf-primary-value';value.textContent=primary;cell.appendChild(value)}
  overlays.forEach(group=>{
    const text=valueForGroup(group);if(!text)return;
    const layer=document.createElement('span');layer.className='taf-overlay-value';appendTafBadge(layer,group);layer.append(document.createTextNode(text));cell.appendChild(layer);
  });
  if(!cell.childElementCount)cell.textContent='—';
}

function appendTafCode(cell,prevailing,overlays){
  const category=prevailing?.flightCategory;
  if(category&&category!=='UNKNOWN'){
    const badge=document.createElement('span');badge.className='taf-category';badge.textContent=category;badge.style.backgroundColor=CATEGORY_COLORS[category]||CATEGORY_COLORS.UNKNOWN;cell.appendChild(badge);
  }
  if(prevailing&&prevailing.type!=='BASELINE')appendTafBadge(cell,prevailing,'taf-prevailing-badge');
  overlays.forEach(group=>appendTafBadge(cell,group));
  if(!cell.childElementCount)cell.textContent='—';
}

function renderTafTimeline(station){
  const body=document.getElementById('taf-timeline-body'),groups=station.forecastGroups.slice().sort((a,b)=>a.sequence-b.sequence),slots=tafHourSlots(station);body.replaceChildren();
  document.getElementById('taf-timeline').dataset.slotCount=String(slots.length);
  const rows=[
    {label:'Time',key:'time'},
    {label:'Code',key:'code'},
    {label:'Weather',key:'weather',value:tafWeather},
    {label:'Visibility',key:'visibility',value:tafVisibility},
    {label:'Ceiling',key:'ceiling',value:tafCeiling},
    {label:'Wind',key:'wind',value:tafWindDirection},
    {label:'Speed',key:'speed',value:tafWindSpeed},
    {label:'Gusts',key:'gusts',value:tafWindGust},
    {label:'Temp/Sun',key:'temperature'}
  ];
  rows.forEach(row=>{
    const tr=document.createElement('tr');tr.dataset.tafRow=row.key;
    const heading=document.createElement('th');heading.scope='row';heading.textContent=row.label;tr.appendChild(heading);
    slots.forEach(slot=>{
      const cell=document.createElement('td');cell.dataset.slotStart=slot.start.toISOString();
      const prevailing=tafPrevailingGroup(groups,slot),overlays=tafApplicableOverlays(groups,slot);
      if(row.key==='time'){
        const day=document.createElement('span');day.className='taf-slot-day';day.textContent=formatTafDay(slot.start);
        const hour=document.createElement('strong');hour.textContent=formatTafHour(slot.start);cell.append(day,hour);
      }else if(row.key==='code')appendTafCode(cell,prevailing,overlays);
      else if(row.key==='temperature')cell.textContent=tafTemperatureAtSlot(groups,slot)||'—';
      else appendTafLayeredValue(cell,prevailing,overlays,row.value);
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
}

function tafGroupSummaryValues(group){
  const values=[],windDirection=tafWindDirection(group),windSpeed=tafWindSpeed(group),gust=tafWindGust(group),visibility=tafVisibility(group),clouds=tafClouds(group);
  if(windDirection||windSpeed)values.push(`Wind: ${[windDirection,windSpeed].filter(Boolean).join(' · ')}${gust?` · gusts ${gust}`:''}`);
  if(visibility)values.push(`Visibility: ${visibility}`);
  if(group.weatherRaw)values.push(`Weather: ${group.weatherRaw}`);
  if(clouds)values.push(`Clouds: ${clouds}`);
  if(tafCeiling(group))values.push(`Ceiling: ${tafCeiling(group)}`);
  (group.temperatureExtremes||[]).forEach(item=>{
    if(item?.temperatureC==null)return;
    values.push(`${item.kind||'Temperature'}: ${item.temperatureC} °C${item.validTime?` at ${formatTafDateTime(item.validTime)}`:''}`);
  });
  return values;
}

function renderTafForecast(station){
  const root=document.getElementById('taf-forecast-list');root.replaceChildren();
  station.forecastGroups.slice().sort((a,b)=>a.sequence-b.sequence).forEach(group=>{
    const item=document.createElement('li');item.dataset.groupSequence=String(group.sequence);item.dataset.groupType=group.type;
    const heading=document.createElement('h3');heading.textContent=`${tafGroupLabel(group)} · ${formatTafDateTime(group.validFrom)} – ${formatTafDateTime(group.validTo)}`;item.appendChild(heading);
    if(group.type==='BECMG'&&group.becomingCompleteAt){const transition=document.createElement('p');transition.textContent=`Transition complete: ${formatTafDateTime(group.becomingCompleteAt)}`;item.appendChild(transition)}
    const values=tafGroupSummaryValues(group);
    if(values.length){const list=document.createElement('ul');values.forEach(text=>{const child=document.createElement('li');child.textContent=text;list.appendChild(child)});item.appendChild(list)}
    root.appendChild(item);
  });
}

function renderTafAirportDescription(){
  const parts=[`${detailAirport.name} (${detailAirport.icao}) ligt in ${detailAirport.country}.`];
  const latitude=Number(detailAirport.lat),longitude=Number(detailAirport.lon);
  if(Number.isFinite(latitude)&&Number.isFinite(longitude))parts.push(`Coördinaten: ${latitude.toFixed(5)}, ${longitude.toFixed(5)}.`);
  document.getElementById('taf-airport-description').textContent=parts.join(' ');
  document.getElementById('taf-airport-title').textContent=`TAF ${detailAirport.name}`;
}

function renderTaf(){
  document.getElementById('taf-title').textContent=`TAF ${detailAirport.icao} · ${detailAirport.name}`;
  document.getElementById('taf-station-icao').textContent=detailAirport.icao;
  const unavailable=document.getElementById('taf-unavailable'),available=document.getElementById('taf-available'),meta=document.getElementById('taf-meta'),panel=document.getElementById('taf-panel');
  panel.dataset.forecastGroupCount=String(detailTaf?.forecastGroups?.length||0);
  if(!detailTaf){
    meta.hidden=true;available.hidden=true;unavailable.hidden=false;
    document.getElementById('taf-unavailable-message').textContent=tafLoadFailed?'De lokale TAF-dataset kon niet worden geladen. De overige detailpagina blijft beschikbaar.':`Geen actuele TAF beschikbaar voor ${detailAirport.name} (${detailAirport.icao}).`;
    return;
  }
  unavailable.hidden=true;available.hidden=false;meta.hidden=false;
  document.getElementById('taf-validity').textContent=`${formatTafDateTime(detailTaf.validFrom)} – ${formatTafDateTime(detailTaf.validTo)}`;
  document.getElementById('taf-issue-time').textContent=formatTafDateTime(detailTaf.issueTime);
  document.getElementById('raw-taf').textContent=detailTaf.rawTAF;
  renderTafTimeline(detailTaf);renderTafForecast(detailTaf);renderTafAirportDescription();
}

const TREND_MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const TREND_METRICS=[
  {key:'vfrPercent',title:'VFR conditions',unit:'%',scale:'percent',group:'flight-rules',fallback:'Visibility ≥ 8 km and ceiling ≥ 3,000 ft.'},
  {key:'mvfrOrBetterPercent',title:'Marginal VFR conditions',unit:'%',scale:'percent',group:'flight-rules',fallback:'Visibility ≥ 5 km and ceiling ≥ 1,000 ft.'},
  {key:'maxTemperatureC',title:'Maximum temperatures',unit:'°C',scale:'temperature',group:'temperature',heatmap:true,fallback:'Average of daily maximum temperatures.'},
  {key:'minTemperatureC',title:'Minimum temperatures',unit:'°C',scale:'temperature',group:'temperature',fallback:'Average of daily minimum temperatures.'},
  {key:'relativeHumidityPercent',title:'Relative humidity',unit:'%',scale:'percent',group:'humidity',fallback:'Calculated from temperature and dew point.'},
  {key:'windSpeedKt',title:'Wind speed',unit:'kt',scale:'wind',group:'wind',fallback:'Average sustained wind speed; gusts excluded.'}
];
const TREND_SERIES_COLORS={year:'#fff',average:'#73c8ff'};

function trendFinite(value){return typeof value==='number'&&Number.isFinite(value)}
function trendValue(value,unit){return trendFinite(value)?`${Number(value).toFixed(1)} ${unit}`:'—'}
function trendMetricValue(documentData,year,month,key){const value=documentData.years?.[year]?.[String(month)]?.[key];return trendFinite(value)?value:null}
function trendAverageValue(documentData,month,key){const value=documentData.averages?.[String(month)]?.[key];return trendFinite(value)?value:null}
function trendMonthPartial(documentData,year,month){return documentData.years?.[year]?.[String(month)]?.partialMonth===true}

function validateTrendsDocument(documentData,icao){
  if(!documentData||typeof documentData!=='object'||Array.isArray(documentData)||documentData.schemaVersion!==1||documentData.icao!==icao||typeof documentData.timezone!=='string'||!documentData.years||typeof documentData.years!=='object'||Array.isArray(documentData.years))return false;
  const years=Object.keys(documentData.years);if(!years.length||years.some(year=>!/^\d{4}$/.test(year)||!documentData.years[year]||typeof documentData.years[year]!=='object'||Array.isArray(documentData.years[year])))return false;
  return years.every(year=>Object.entries(documentData.years[year]).every(([month,values])=>/^\d{1,2}$/.test(month)&&Number(month)>=1&&Number(month)<=12&&values&&typeof values==='object'&&!Array.isArray(values)));
}

function trendYearRange(years){
  const sorted=years.map(Number).filter(Number.isFinite).sort((a,b)=>a-b);if(!sorted.length)return'';return sorted.length===1?String(sorted[0]):`${sorted[0]}–${sorted.at(-1)}`;
}

function trendCoverageText(documentData,years){
  const first=detailIsoTimestamp(documentData.coverage?.firstObservation),last=detailIsoTimestamp(documentData.coverage?.lastObservation),timezone=documentData.timezone;
  let range='';
  try{if(first&&last)range=`Data coverage: ${new Intl.DateTimeFormat('en-GB',{timeZone:timezone,day:'numeric',month:'short',year:'numeric'}).format(first)} – ${new Intl.DateTimeFormat('en-GB',{timeZone:timezone,day:'numeric',month:'short',year:'numeric'}).format(last)}`}catch{range=''}
  const latestYear=years[0],months=latestYear?Object.keys(documentData.years[latestYear]||{}).map(Number).filter(Number.isFinite).sort((a,b)=>b-a):[],latestMonth=months[0],quality=latestMonth?documentData.years[latestYear][String(latestMonth)]?.quality:null;
  const localDays=quality&&Number.isInteger(quality.completeLocalDays)&&Number.isInteger(quality.availableLocalDays)?`${quality.completeLocalDays} complete local days out of ${quality.availableLocalDays} available in ${TREND_MONTHS[latestMonth-1]} ${latestYear}`:'';
  return[range,localDays].filter(Boolean).join(' · ');
}

function appendTrendCell(row,text,tag='td'){
  const cell=document.createElement(tag);cell.textContent=text;row.appendChild(cell);return cell;
}

function trendHeatColor(value,range){
  if(!trendFinite(value)||!range)return'';const span=range.max-range.min,position=span>0?(value-range.min)/span:.5,t=Math.max(0,Math.min(1,position));
  const stops=[{p:0,c:[44,74,120]},{p:.55,c:[217,138,43]},{p:1,c:[224,72,62]}];let lower=stops[0],upper=stops.at(-1);
  for(let index=0;index<stops.length-1;index+=1){if(t>=stops[index].p&&t<=stops[index+1].p){lower=stops[index];upper=stops[index+1];break}}
  const local=(t-lower.p)/(upper.p-lower.p||1),rgb=lower.c.map((channel,index)=>Math.round(channel+(upper.c[index]-channel)*local));return`rgb(${rgb.join(',')})`;
}

function appendTrendValueCell(row,value,unit,partial,heatRange=null){
  const cell=document.createElement('td');cell.textContent=trendFinite(value)?Number(value).toFixed(1):'—';
  if(trendFinite(value)){cell.classList.add('trend-value-cell');cell.setAttribute('aria-label',`${Number(value).toFixed(1)} ${unit}${partial?', provisional':''}`);if(heatRange){cell.classList.add('trend-temperature-heat');cell.style.backgroundColor=trendHeatColor(value,heatRange)}if(partial){const marker=document.createElement('sup');marker.className='trend-provisional-marker';marker.textContent='*';marker.title='Provisional: month is incomplete.';cell.appendChild(marker)}}
  row.appendChild(cell);return cell;
}

function renderTrendTable(documentData,metric,years){
  const scroll=document.createElement('div');scroll.className='trend-table-scroll';const table=document.createElement('table');table.className='trend-table';table.setAttribute('aria-label',`${metric.title} per month`);
  const heatValues=metric.heatmap?[...years.flatMap(year=>Array.from({length:12},(_,index)=>trendMetricValue(documentData,year,index+1,metric.key))),...Array.from({length:12},(_,index)=>trendAverageValue(documentData,index+1,metric.key))].filter(trendFinite):[],heatRange=heatValues.length?{min:Math.min(...heatValues),max:Math.max(...heatValues)}:null;
  const head=document.createElement('thead'),headRow=document.createElement('tr');appendTrendCell(headRow,'Year','th').scope='col';TREND_MONTHS.forEach(month=>{const cell=appendTrendCell(headRow,month,'th');cell.scope='col'});head.appendChild(headRow);table.appendChild(head);
  const body=document.createElement('tbody'),averageRow=document.createElement('tr'),hasAverage=Array.from({length:12},(_,index)=>trendAverageValue(documentData,index+1,metric.key)).some(trendFinite);averageRow.className=`trend-average-row${hasAverage?' has-values':''}`;const averageLabel=appendTrendCell(averageRow,'Average','th');averageLabel.scope='row';for(let month=1;month<=12;month+=1)appendTrendValueCell(averageRow,trendAverageValue(documentData,month,metric.key),metric.unit,false,heatRange);body.appendChild(averageRow);
  years.forEach((year,index)=>{const row=document.createElement('tr');if(index===0)row.className='trend-latest-year-row';const label=appendTrendCell(row,year,'th');label.scope='row';for(let month=1;month<=12;month+=1)appendTrendValueCell(row,trendMetricValue(documentData,year,month,metric.key),metric.unit,trendMonthPartial(documentData,year,month),heatRange);body.appendChild(row)});
  table.appendChild(body);scroll.appendChild(table);return scroll;
}

function trendNiceStep(value){
  if(!Number.isFinite(value)||value<=0)return 1;const power=10**Math.floor(Math.log10(value)),fraction=value/power;return(fraction<=1?1:fraction<=2?2:fraction<=5?5:10)*power;
}

function trendScale(metric,values){
  if(metric.scale==='percent')return{min:0,max:100,ticks:[0,25,50,75,100]};
  if(metric.scale==='wind'){const observed=Math.max(0,...values),step=trendNiceStep(Math.max(1,observed)/4),maximum=Math.max(step*2,Math.ceil(observed/step)*step);return{min:0,max:maximum,ticks:Array.from({length:Math.round(maximum/step)+1},(_,index)=>index*step)}}
  let minimum=Math.min(...values),maximum=Math.max(...values);if(!values.length){minimum=0;maximum=10}else if(minimum===maximum){minimum-=2;maximum+=2}else{const padding=(maximum-minimum)*.12;minimum-=padding;maximum+=padding}const step=trendNiceStep((maximum-minimum)/4),niceMin=Math.floor(minimum/step)*step,niceMax=Math.ceil(maximum/step)*step;return{min:niceMin,max:niceMax,ticks:Array.from({length:Math.round((niceMax-niceMin)/step)+1},(_,index)=>niceMin+index*step)};
}

function trendSvgElement(name,attributes={}){const element=document.createElementNS(SVG_NAMESPACE,name);Object.entries(attributes).forEach(([key,value])=>element.setAttribute(key,String(value)));return element}

function renderTrendChart(documentData,metric,years){
  const figure=document.createElement('figure');figure.className='trend-chart-figure';const svg=trendSvgElement('svg',{viewBox:'0 0 600 260',role:'img','aria-label':`${metric.title} monthly chart`});
  const latestYear=years[0],yearValues=Array.from({length:12},(_,index)=>trendMetricValue(documentData,latestYear,index+1,metric.key)),averageValues=Array.from({length:12},(_,index)=>trendAverageValue(documentData,index+1,metric.key)),allValues=[...yearValues,...averageValues].filter(trendFinite),scale=trendScale(metric,allValues),left=48,right=584,top=18,bottom=222,width=right-left,height=bottom-top,x=index=>left+index*(width/11),y=value=>bottom-(value-scale.min)/(scale.max-scale.min)*height;
  scale.ticks.forEach(tick=>{svg.appendChild(trendSvgElement('line',{x1:left,y1:y(tick),x2:right,y2:y(tick),class:'trend-chart-grid'}));const label=trendSvgElement('text',{x:left-8,y:y(tick)+3,class:'trend-chart-y-label'});label.textContent=Number(tick).toFixed(Number.isInteger(tick)?0:1);svg.appendChild(label)});
  TREND_MONTHS.forEach((month,index)=>{const label=trendSvgElement('text',{x:x(index),y:244,class:'trend-chart-x-label'});label.textContent=month;svg.appendChild(label)});svg.appendChild(trendSvgElement('line',{x1:left,y1:bottom,x2:right,y2:bottom,class:'trend-chart-axis'}));
  const series=[{label:'Average',values:averageValues,color:TREND_SERIES_COLORS.average,average:true},{label:latestYear,values:yearValues,color:TREND_SERIES_COLORS.year,average:false}].filter(series=>series.values.some(trendFinite));
  series.forEach(seriesItem=>{
    let segment=[];const flush=()=>{if(segment.length>1){const path=trendSvgElement('path',{d:segment.map((point,index)=>`${index?'L':'M'} ${point[0]} ${point[1]}`).join(' '),class:`trend-chart-series${seriesItem.average?' average':''}`,stroke:seriesItem.color});svg.appendChild(path)}segment=[]};
    seriesItem.values.forEach((value,index)=>{if(trendFinite(value))segment.push([x(index),y(value)]);else flush()});flush();
    seriesItem.values.forEach((value,index)=>{if(!trendFinite(value))return;const partial=!seriesItem.average&&trendMonthPartial(documentData,latestYear,index+1),group=trendSvgElement('g',{class:`trend-chart-point${partial?' provisional':''}`,tabindex:'0',role:'img','aria-label':`${TREND_MONTHS[index]} ${seriesItem.label}: ${Number(value).toFixed(1)} ${metric.unit}${partial?', provisional':''}`}),circle=trendSvgElement('circle',{cx:x(index),cy:y(value),r:4.2,fill:seriesItem.color}),title=trendSvgElement('title');title.textContent=`${TREND_MONTHS[index]} ${seriesItem.label}: ${Number(value).toFixed(1)} ${metric.unit}${partial?' · provisional':''}`;group.append(circle,title);const tooltip=trendSvgElement('g',{class:'trend-chart-tooltip'}),tooltipX=Math.max(62,Math.min(470,x(index)-65)),tooltipY=Math.max(8,y(value)-38);tooltip.appendChild(trendSvgElement('rect',{x:tooltipX,y:tooltipY,width:130,height:25,rx:4}));const tooltipText=trendSvgElement('text',{x:tooltipX+65,y:tooltipY+16});tooltipText.textContent=`${TREND_MONTHS[index]} ${seriesItem.label}: ${Number(value).toFixed(1)} ${metric.unit}${partial?' *':''}`;tooltip.appendChild(tooltipText);group.appendChild(tooltip);svg.appendChild(group)});
  });
  const legend=document.createElement('figcaption');legend.className='trend-chart-legend';series.forEach(seriesItem=>{const item=document.createElement('span'),swatch=document.createElement('i');swatch.style.backgroundColor=seriesItem.color;item.appendChild(swatch);item.append(document.createTextNode(seriesItem.label));legend.appendChild(item)});if(!series.length){const empty=document.createElement('p');empty.className='trend-chart-empty';empty.textContent='No values available for this metric.';figure.append(svg,empty);return figure}figure.append(svg,legend);return figure;
}

function renderTrends(documentData){
  const years=Object.keys(documentData.years).sort((a,b)=>Number(b)-Number(a)),range=trendYearRange(years),content=document.getElementById('trends-content');content.replaceChildren();
  document.getElementById('trends-title').textContent=`Trends ${detailAirport.name}${range?` ${range}`:''}`;document.getElementById('trends-intro').textContent='These statistics are derived from available historical METAR observations. Only recorded data is shown; incomplete months are provisional.';
  const coverage=document.getElementById('trends-coverage'),coverageText=trendCoverageText(documentData,years);coverage.textContent=coverageText;coverage.hidden=!coverageText;
  const navigation=document.createElement('nav');navigation.className='trends-jump-nav';navigation.setAttribute('aria-label','Trend categories');[['flight-rules','Flight rules'],['temperature','Temperature'],['humidity','Humidity'],['wind','Wind']].forEach(([group,label])=>{const button=document.createElement('button');button.type='button';button.textContent=label;button.addEventListener('click',()=>document.getElementById(`trend-${TREND_METRICS.find(metric=>metric.group===group).key}`)?.scrollIntoView({behavior:'smooth',block:'start'}));navigation.appendChild(button)});content.appendChild(navigation);
  TREND_METRICS.forEach(metric=>{const section=document.createElement('section');section.className='trend-section';section.id=`trend-${metric.key}`;const header=document.createElement('header');header.className='trend-section-header';const heading=document.createElement('h2');heading.textContent=metric.title;const unit=document.createElement('span');unit.className='trend-section-unit';unit.textContent=metric.unit;header.append(heading,unit);const layout=document.createElement('div');layout.className='trend-section-layout';layout.append(renderTrendTable(documentData,metric,years));const visual=document.createElement('div');visual.className='trend-visual';visual.appendChild(renderTrendChart(documentData,metric,years));const explanation=document.createElement('p');explanation.className='trend-explanation';explanation.textContent=documentData.definitions?.[metric.key]?.definition||metric.fallback;visual.appendChild(explanation);layout.appendChild(visual);section.append(header,layout);content.appendChild(section)});
  if([...content.querySelectorAll('.trend-provisional-marker')].length){const note=document.createElement('p');note.className='trends-provisional-note';note.textContent='* Provisional: month is incomplete.';content.appendChild(note)}
  document.getElementById('trends-loading').hidden=true;document.getElementById('trends-unavailable').hidden=true;content.hidden=false;
}

function showTrendsUnavailable(message){
  document.getElementById('trends-loading').hidden=true;document.getElementById('trends-content').hidden=true;const unavailable=document.getElementById('trends-unavailable');unavailable.hidden=false;document.getElementById('trends-unavailable-message').textContent=message;
}

function loadTrends(){
  if(trendsLoad)return trendsLoad;document.getElementById('trends-loading').hidden=false;document.getElementById('trends-unavailable').hidden=true;
  trendsLoad=fetch(`data/metar-trends/${encodeURIComponent(detailAirport.icao)}.json`).then(response=>{if(!response.ok)throw Error(`HTTP ${response.status}`);return response.json()}).then(documentData=>{if(!validateTrendsDocument(documentData,detailAirport.icao))throw Error('ongeldig Trends-schema');trendsDocument=documentData;renderTrends(documentData);return documentData}).catch(error=>{console.warn(`Trenddata is niet beschikbaar voor ${detailAirport.icao}.`,error);showTrendsUnavailable(`No trend data available for ${detailAirport.name} (${detailAirport.icao}) yet.`);return null});return trendsLoad;
}

const DETAIL_TAB_TARGETS={metar:'metar-content',taf:'taf-panel',history:'history-panel',airport:'airport-panel',trends:'trends-panel'};
const DETAIL_TAB_HASHES={metar:'#metar-content',taf:'#taf-panel',history:'#history-panel',airport:'#airport-content',trends:'#trends'};

function detailTabFromHash(){
  return Object.entries(DETAIL_TAB_HASHES).find(([,hash])=>hash===location.hash)?.[0]||'metar';
}

function activateDetailTab(tabName,{updateHash=true,scroll=true}={}){
  const name=DETAIL_TAB_TARGETS[tabName]?tabName:'metar';activeDetailTab=name;
  document.getElementById('metar-panel').hidden=name!=='metar';document.getElementById('taf-panel').hidden=name!=='taf';document.getElementById('history-panel').hidden=name!=='history';document.getElementById('airport-panel').hidden=name!=='airport';document.getElementById('trends-panel').hidden=name!=='trends';
  document.querySelector('.detail-airport-metadata').hidden=name==='metar'||name==='history'||name==='airport'||name==='trends';
  document.querySelectorAll('[data-detail-tab]').forEach(tab=>{const active=tab.dataset.detailTab===name;tab.classList.toggle('active',active);if(active)tab.setAttribute('aria-current','page');else tab.removeAttribute('aria-current')});
  if(detailAirport){document.getElementById('breadcrumb-icao').textContent=`${name.toUpperCase()} ${detailAirport.icao}`;document.title=`${name.toUpperCase()} ${detailAirport.icao} · ${detailAirport.name} · METAR & TAF`}
  if(updateHash){history.replaceState(history.state,'',`${location.pathname}${location.search}${DETAIL_TAB_HASHES[name]}`)}
  if(name==='history'&&detailAirport)void loadRecentHistory(detailAirport.icao);
  if(name==='airport'&&detailAirport){void ensureAirportTab();requestAnimationFrame(()=>airportTabMap?.invalidateSize())}
  if(name==='trends'&&detailAirport)void loadTrends();
  if(scroll&&name!=='taf')requestAnimationFrame(()=>document.getElementById(DETAIL_TAB_TARGETS[name])?.scrollIntoView({block:'start'}));
}

function setupTabNavigation(){
  document.querySelectorAll('[data-detail-tab]').forEach(tab=>tab.addEventListener('click',event=>{event.preventDefault();activateDetailTab(tab.dataset.detailTab)}));
  window.addEventListener('hashchange',()=>activateDetailTab(detailTabFromHash(),{updateHash:false}));
}

function renderAirport(){
  const title=`METAR ${detailAirport.icao} · ${detailAirport.name}`;
  document.title=`${title} · METAR & TAF`;document.getElementById('detail-title').textContent=title;
  document.getElementById('header-airport-name').textContent=detailAirport.name;document.getElementById('header-icao').textContent=detailAirport.icao;document.getElementById('header-country').textContent=detailAirport.country;
  document.getElementById('breadcrumb-country').textContent=detailAirport.country;document.getElementById('breadcrumb-icao').textContent=`METAR ${detailAirport.icao}`;document.getElementById('station-icao').textContent=detailAirport.icao;
  document.getElementById('recent-history-title').textContent=`METAR history ${detailAirport.icao} · ${detailAirport.name}`;
  document.getElementById('raw-metar').textContent=detailMetar.rawOb;document.getElementById('airport-title').textContent=`METAR ${detailAirport.name} - ${detailAirport.icao}`;
  renderDetailAirportMetadata();renderMetrics();renderWindLayers();renderWindSpeedMeter();renderCeilingVisibility();selectedRunway=chooseInitialRunway(airportRunways);renderRunwayOverlay();renderRunwayTable();renderRunwayInputs();renderAirportNarrative();renderMetarDaylightContent();
}

function showError(message){document.getElementById('detail-main').hidden=true;document.getElementById('detail-error-message').textContent=message;document.getElementById('detail-error').hidden=false}

async function loadDetail(){
  const icao=requestedIcao(),initialTab=detailTabFromHash();if(!/^[A-Z]{4}$/.test(icao)){showError('De ICAO-code in de URL ontbreekt of is ongeldig.');return}
  try{
    const tafRequest=fetch('data/taf-live.json').then(response=>{if(!response.ok)throw Error(`HTTP ${response.status}`);return response.json()}).catch(()=>{tafLoadFailed=true;return null});
    const metadataRequest=loadAirportMetadata();
    const [airports,metars,runways,taf,metadataByIcao]=await Promise.all([...['data/airports-full.json','data/metar-live.json','data/runways.json'].map(url=>fetch(url).then(response=>{if(!response.ok)throw Error(url);return response.json()})),tafRequest,metadataRequest]);
    tafDocument=taf;
    detailAirport=airports.find(airport=>airport.icao===icao)||null;detailMetar=metars.find(metar=>metar.icaoId===icao)||null;
    detailAirportMetadata=metadataByIcao[icao]||null;
    if(!detailAirport){showError(`Airport ${icao} staat niet in airports-full.json.`);return}if(!detailMetar){showError(`Voor ${icao} staat geen actuele METAR in metar-live.json.`);return}
    detailTaf=tafDocument?.airports?.[icao]||null;
    allAirportRunways=runways.filter(runway=>runway.airport_ident===icao).sort((a,b)=>(b.length_ft||0)-(a.length_ft||0)||runwayLabel(a).localeCompare(runwayLabel(b)));airportRunways=allAirportRunways.filter(runway=>!runway.closed&&runway.le_ident&&runway.he_ident&&runwayEnds(runway).length);
    renderAirport();renderTaf();document.getElementById('detail-main').hidden=false;activateDetailTab(initialTab,{scroll:initialTab!=='metar'});void loadHistory(icao);
  }catch(error){console.error('De lokale METAR-detaildata kon niet worden geladen.',error);showError('De lokale detaildata kon niet worden geladen.')}
}

document.getElementById('change-runway').addEventListener('click',()=>{
  if(airportRunways.length<2)return;
  const index=airportRunways.indexOf(selectedRunway);selectRunway(airportRunways[(index+1)%airportRunways.length]);
});
document.getElementById('compass-holder').addEventListener('click',event=>{event.currentTarget.classList.toggle('active');setCompassOrientation()});
setupTabNavigation();
loadDetail();
