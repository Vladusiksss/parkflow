'use strict';
const $ = (id) => document.getElementById(id);
const names = {FREE:'Вільно', OCCUPIED:'Зайнято', RESERVED:'Бронь', UNKNOWN:'Невідомо', DISABLED:'Закрито'};
const kinds = {STANDARD:'Стандартне', EV:'Електромобіль', ACCESSIBLE:'Доступне'};
const money = (cents) => new Intl.NumberFormat('uk-UA',{maximumFractionDigits:2}).format(cents/100)+' грн';
const escapeHTML = (s) => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state, mine={reservations:[],sessions:[]}, selected=null, floor=1, kind='ALL', skew=0;
let lastMap='', socket, errorTimer, refreshing=false;
function error(message){$('error').textContent=message;$('error').hidden=false;clearTimeout(errorTimer);errorTimer=setTimeout(()=>$('error').hidden=true,9000);}
async function api(path, {method='GET',body}={}){
  const headers={'X-ParkFlow':'1'};
  if(body!==undefined)headers['Content-Type']='application/json';
  const response=await fetch('/api'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Перевірте введені дані');
  return data;
}
function render(){
  if(!state)return;
  const {parking,counts,spaces}=state;
  $('free-count').textContent=counts.FREE;
  $('occupancy').textContent=Math.round(counts.OCCUPIED/spaces.length*100)+'%';
  $('rate').textContent=money(parking.rate);
  $('parking-name').textContent=parking.name;
  $('address').textContent=parking.address;
  $('mode-label').textContent=state.demo?'ДЕМО · СИНТЕТИЧНІ ДАНІ':'ДАНІ КАМЕРИ';
  $('directions').href=`https://www.google.com/maps/dir/?api=1&destination=${parking.lat},${parking.lng}`;
  const shown=spaces.filter(s=>s.floor===floor);
  const signature=JSON.stringify([shown.map(s=>[s.id,s.status]),selected,kind]);
  if(signature!==lastMap){
    lastMap=signature;
    $('space-map').innerHTML=['A','B','C'].map((row,i)=>`${i?'<div class="lane" aria-hidden="true">→ → →</div>':''}<div class="spot-row">${shown.filter(s=>s.id.includes('-'+row)).map(s=>`<button class="spot ${s.status} ${selected===s.id?'selected':''} ${kind!=='ALL'&&kind!==s.kind?'dimmed':''}" data-space="${s.id}" aria-label="${s.id}, ${kinds[s.kind]}, ${names[s.status]}" aria-pressed="${selected===s.id}"><span class="spot-symbol" aria-hidden="true">${s.kind==='EV'?'ϟ':s.kind==='ACCESSIBLE'?'♿':s.status==='OCCUPIED'?'▰':s.status==='RESERVED'?'◷':'·'}</span>${s.id.split('-')[1]}</button>`).join('')}</div>`).join('');
  }
  const s=spaces.find(s=>s.id===selected);
  if(s){
    $('selection').innerHTML=`<h2>Місце ${s.id}</h2><div class="choice-meta"><span>Тип</span>${kinds[s.kind]}</div><div class="choice-meta"><span>До входу</span>${s.distance} м</div><div class="choice-meta"><span>Стан</span>${names[s.status]}</div>`;
    $('booking-form').hidden=s.status!=='FREE'||!!activeBooking()||!!activeSession();
  }else{$('booking-form').hidden=true;}
  renderMine();
}
function activeBooking(){return mine.reservations.find(r=>r.state==='ACTIVE'&&r.expires_at*1000>Date.now()+skew);}
function activeSession(){return mine.sessions.find(s=>!s.ended_at);}
function renderMine(){
  const booking=activeBooking(),session=activeSession(),box=$('my-booking');
  box.hidden=!booking&&!session;
  if(session){box.innerHTML=`<div class="eyebrow">МОЯ МАШИНА</div><h2>${session.space_id}</h2><p>${session.space_id[0]} поверх · паркування триває</p><p>Початок: ${new Date(session.started_at*1000).toLocaleTimeString('uk-UA')}</p><small>Тариф сесії: ${money(session.rate)}/год</small>`;return;}
  if(booking){const seconds=Math.max(0,Math.ceil((booking.expires_at*1000-Date.now()-skew)/1000));box.innerHTML=`<div class="eyebrow">ВАШЕ МІСЦЕ ЧЕКАЄ</div><h2>${booking.space_id}</h2><p>${escapeHTML(booking.plate)} · ${booking.space_id[0]} поверх</p><div class="timer">${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}</div><button class="secondary" id="cancel-booking">Скасувати бронювання</button>`;}
}
async function refreshMine(){mine=await api('/me');render();}
function connect(){
  socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
  socket.onmessage=(event)=>{state=JSON.parse(event.data);skew=state.server_time*1000-Date.now();render();};
  socket.onopen=()=>{$('connection').textContent='● Оновлення наживо';$('connection').classList.remove('offline');};
  socket.onclose=()=>{$('connection').textContent='○ Немає зв’язку · повторюємо';$('connection').classList.add('offline');if(state){state.spaces=state.spaces.map(s=>({...s,status:'UNKNOWN'}));state.counts={...state.counts,FREE:0,OCCUPIED:0,UNKNOWN:state.spaces.length};render();}setTimeout(connect,3000);};
  socket.onerror=()=>socket.close();
}
$('space-map').addEventListener('click',(e)=>{const b=e.target.closest('[data-space]');if(b){selected=b.dataset.space;render();}});
document.querySelectorAll('[data-floor]').forEach(b=>b.addEventListener('click',()=>{floor=Number(b.dataset.floor);document.querySelectorAll('[data-floor]').forEach(x=>x.classList.toggle('active',x===b));render();}));
$('kind').addEventListener('change',(e)=>{kind=e.target.value;render();});
$('recommend').addEventListener('click',()=>{
  if(!state)return;
  const candidates=state.spaces.filter(s=>s.status==='FREE'&&(kind==='ALL'?s.kind==='STANDARD':s.kind===kind));
  const best=candidates.sort((a,b)=>(a.distance+a.floor*10)-(b.distance+b.floor*10))[0];
  if(!best){error('Немає вільних місць обраного типу');return;}
  selected=best.id;floor=best.floor;document.querySelectorAll('[data-floor]').forEach(x=>x.classList.toggle('active',Number(x.dataset.floor)===floor));render();$('plate').focus();
});
$('booking-form').addEventListener('submit',async(e)=>{e.preventDefault();$('reserve-btn').disabled=true;try{await api('/reservations',{method:'POST',body:{space_id:selected,plate:$('plate').value}});await refreshMine();}catch(err){error(err.message);}finally{$('reserve-btn').disabled=false;}});
$('my-booking').addEventListener('click',async(e)=>{if(e.target.id==='cancel-booking'){const booking=activeBooking();if(!booking)return;try{await api('/reservations/'+booking.id,{method:'DELETE'});await refreshMine();}catch(err){error(err.message);}}});
setInterval(renderMine,1000);
setInterval(()=>{refreshMine().catch(()=>{});},4000);
(async()=>{try{await api('/guest',{method:'POST'});state=await api('/parkings/1');await refreshMine();connect();}catch(err){error('Не вдалося підключитися: '+err.message);$('connection').textContent='○ Сервер недоступний';}})();
