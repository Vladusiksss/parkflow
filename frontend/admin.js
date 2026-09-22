'use strict';
const $ = (id) => document.getElementById(id);
const names = {FREE:'Вільно', OCCUPIED:'Зайнято', RESERVED:'Бронь', UNKNOWN:'Невідомо', DISABLED:'Закрито'};
const kinds = {STANDARD:'Стандартне', EV:'Електромобіль', ACCESSIBLE:'Доступне'};
const money = (cents) => new Intl.NumberFormat('uk-UA',{maximumFractionDigits:2}).format(cents/100)+' грн';
const escapeHTML = (s) => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let adminKey='', refreshing=false, errorTimer;
function error(message){$('error').textContent=message;$('error').hidden=false;clearTimeout(errorTimer);errorTimer=setTimeout(()=>$('error').hidden=true,10000);}
function notice(message){$('notice').textContent=message;$('notice').hidden=false;setTimeout(()=>$('notice').hidden=true,6000);}
async function api(path, {method='GET',body,admin=false}={}){
  const headers={'X-ParkFlow':'1'};
  if(body!==undefined)headers['Content-Type']='application/json';
  if(admin)headers.Authorization='Bearer '+adminKey;
  const response=await fetch('/api'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Перевірте введені дані');
  return data;
}
$('admin-login').addEventListener('submit',async(e)=>{e.preventDefault();adminKey=$('admin-key').value.trim();try{await refreshAdmin();$('admin-key').value='';await loadCamera();}catch(err){adminKey='';error(err.message);}});
$('logout').addEventListener('click',()=>{adminKey='';clearCamera();$('admin-content').hidden=true;$('admin-content').querySelectorAll('#admin-reservations,#admin-sessions,#events').forEach(el=>el.replaceChildren());$('admin-login').hidden=false;});
async function refreshAdmin(){
  if(!adminKey||refreshing)return;
  const requestedKey=adminKey;
  refreshing=true;
  try{
    const data=await api('/admin',{admin:true});
    if(adminKey!==requestedKey)return;
    $('admin-login').hidden=true;$('admin-content').hidden=false;fillSpaceOptions(data.spaces);$('billed').textContent=money(data.billed);
    if(document.activeElement!==$('tariff-input'))$('tariff-input').value=data.parking.rate/100;
    const fresh=data.spaces.filter(s=>data.server_time-s.observed_at<=30).length;
    $('camera-state').textContent=data.demo?'Демонстрація':fresh?'Отримуємо дані':'Очікуємо AI';
    $('camera-detail').textContent=data.demo?'Демонстраційні стани місць. Камера налаштовується вище.':`Актуальні вимірювання: ${fresh} / ${data.spaces.length}`;
    const hist=data.history;
    $('history-chart').innerHTML=hist.length?`<svg viewBox="0 0 ${Math.max(hist.length*8,400)} 110" preserveAspectRatio="none" role="img" aria-label="Відсоток зайнятих місць">${hist.map((h,i)=>{const height=Math.round(h.occupied/h.total*100),width=Math.max(400/hist.length,8);return `<rect x="${i*width}" y="${105-height}" width="${width-2}" height="${Math.max(2,height)}" rx="2" fill="#a9ca76"><title>${new Date(h.minute*1000).toLocaleTimeString('uk-UA')} · ${height}%</title></rect>`;}).join('')}</svg>`:'<small>Збираємо перші вимірювання…</small>';
    $('admin-reservations').innerHTML=data.reservations.length?data.reservations.map(r=>`<div class="admin-row"><div><strong>${r.space_id}</strong> · ${escapeHTML(r.plate)}<small>До ${new Date(r.expires_at*1000).toLocaleTimeString('uk-UA')}</small></div><button class="secondary" data-entry="${r.id}">Підтвердити в’їзд</button></div>`).join(''):'<p>Активних бронювань немає.</p>';
    $('admin-sessions').innerHTML=data.sessions.length?data.sessions.map(s=>`<div class="admin-row"><div><strong>${s.space_id}</strong><small>${s.ended_at?'Нараховано '+money(s.amount)+' · не сплачено':'Триває · '+money(s.rate)+'/год'}</small></div>${s.ended_at?'':`<button class="secondary" data-exit="${s.id}">Підтвердити виїзд</button>`}</div>`).join(''):'<p>Паркувальних сесій ще немає.</p>';
    $('demo-hint').textContent=data.demo?'Демо: змініть зайнятість місця, щоб перевірити оновлення у водія.':'Закрийте місця, що тимчасово недоступні.';
    if(!document.activeElement.closest('#admin-spaces'))$('admin-spaces').innerHTML=data.spaces.map(s=>`<div class="admin-space"><strong>${s.id}</strong><small>${names[s.status]}</small>${data.demo?`<select data-demo="${s.id}" aria-label="Демо стан ${s.id}"><option value="">Змінити стан</option value="FREE">Вільно</option><option value="OCCUPIED">Зайнято</option><option value="UNKNOWN">Невідомо</option></select>`:''}<button data-disable="${s.id}" data-value="${s.status!=='DISABLED'}">${s.status==='DISABLED'?'Відкрити':'Закрити'}</button></div>`).join('');
    $('events').innerHTML=data.events.length?data.events.map(e=>`<div class="admin-row"><span>${escapeHTML(e.kind)} · ${escapeHTML(e.detail)}</span><small>${new Date(e.at*1000).toLocaleTimeString('uk-UA')}</small></div>`).join(''):'<p>Подій ще немає.</p>';
  }finally{refreshing=false;}
}
$('tariff-form').addEventListener('submit',async(e)=>{e.preventDefault();try{await api('/admin/tariff',{method:'PATCH',admin:true,body:{rate:Math.round(Number($('tariff-input').value)*100)}});await refreshAdmin();}catch(err){error(err.message);}});
$('admin-content').addEventListener('click',async(e)=>{
  const b=e.target.closest('button');if(!b)return;
  let path,method='POST',body;
  if(b.dataset.entry)path='/admin/entry/'+b.dataset.entry;
  if(b.dataset.exit)path='/admin/exit/'+b.dataset.exit;
  if(b.dataset.disable){path='/admin/spaces/'+b.dataset.disable;method='PATCH';body={disabled:b.dataset.value==='true'};}
  if(!path)return;b.disabled=true;
  try{await api(path,{method,body,admin:true});await refreshAdmin();}catch(err){error(err.message);}finally{b.disabled=false;}
});
$('admin-spaces').addEventListener('change',async(e)=>{if(!e.target.dataset.demo||!e.target.value)return;try{await api('/admin/demo/'+e.target.dataset.demo,{method:'POST',admin:true,body:{status:e.target.value}});e.target.blur();await refreshAdmin();}catch(err){error(err.message);}});

setInterval(()=>{if(adminKey)refreshAdmin().catch(e=>error(e.message));},4000);

const phases={STOPPED:'Зупинено',CONNECTING:'Підключення…',ONLINE:'Онлайн',RECONNECTING:'Немає кадру · повторюємо',FAILED:'Помилка процесу',AI_ERROR:'Помилка AI'};
let cameraInfo=null, polygons=[], points=[], lastFrame=null, previewBusy=false, cameraPolling=false;
let spacesFilled=false;
const canvas=$('camera-canvas'),ctx=canvas.getContext('2d');
function fillSpaceOptions(spaces){
  if(spacesFilled)return;
  $('polygon-space').innerHTML=spaces.map(s=>`<option value="${escapeHTML(s.id)}">${escapeHTML(s.id)} · ${s.floor} поверх</option>`).join('');
  spacesFilled=true;
}
function clearCamera(){
  cameraInfo=null;polygons=[];points=[];spacesFilled=false;
  if(lastFrame)lastFrame.close();lastFrame=null;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  $('camera-placeholder').hidden=false;$('camera-url').value='';
}
function showCamera(data,hydrate=false){
  cameraInfo=data;
  $('camera-phase').textContent=phases[data.phase]||data.phase;
  $('start-camera').disabled=data.running||!data.configured||!data.capabilities.opencv;
  $('stop-camera').disabled=!data.running&&data.phase!=='FAILED';
  $('save-camera').disabled=data.running;
  $('camera-url').disabled=data.running;
  $('camera-mode-note').textContent=data.demo?'Деморежим: перегляд і розмітка доступні. Для AI-зайнятості запустіть start-camera.cmd.':'Режим камери: AI оновлюватиме лише розмічені місця.';
  $('camera-dependencies').textContent=`OpenCV: ${data.capabilities.opencv?'готовий':'потрібен install-camera.cmd'} · YOLO: ${data.capabilities.yolo&&data.capabilities.model?'готовий':'потрібен install-vision.cmd для AI'}`;
  $('camera-ai').disabled=data.demo;
  $('camera-secret-hint').textContent=data.configured?'Адресу збережено. Залиште поле порожнім, щоб зберегти її. Зміна адреси очистить розмітку.':'Адреса зберігається лише на сервері. Спецсимволи пароля в URL потрібно percent-encode.';
  const ai={OFF:'вимкнено',LOADING:'завантаження моделі',READY:'готовий',RUNNING:'визначає зайнятість',ERROR:'помилка обробки'};
  $('camera-status-detail').textContent=data.phase==='RECONNECTING'?'Перевірте IP, шлях RTSP, логін, пароль і доступність камери з цього комп’ютера. Повторне підключення автоматичне.':data.phase==='FAILED'?'Процес завершився. Перевірте залежності та спробуйте підключити знову.':`AI: ${ai[data.ai_state]||data.ai_state}. ${data.ingestion_error?'Сервер відхилив застарілі вимірювання.':''}`;
  if(hydrate){
    $('camera-name').value=data.name;$('camera-transport').value=data.transport;
    $('camera-ai').checked=data.inference&&!data.demo;
    polygons=structuredClone(data.spaces);points=[];renderPolygons();drawFrame();
  }
  if(!data.running&&lastFrame)$('frame-time').textContent='Збережений кадр · камера зупинена';
}
async function loadCamera(hydrate=true){
  const key=adminKey;
  if(!key)return;
  const data=await api('/admin/camera',{admin:true});
  if(key!==adminKey)return;
  showCamera(data,hydrate);
}
function drawFrame(){
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(lastFrame)ctx.drawImage(lastFrame,0,0,canvas.width,canvas.height);
  function shape(vertices,color,label){
    if(!vertices.length)return;
    ctx.beginPath();vertices.forEach((p,i)=>{const x=p[0]*canvas.width,y=p[1]*canvas.height;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});
    if(vertices.length===4)ctx.closePath();
    ctx.strokeStyle=color;ctx.lineWidth=3;ctx.stroke();ctx.fillStyle=color+'33';ctx.fill();
    vertices.forEach(p=>{ctx.beginPath();ctx.arc(p[0]*canvas.width,p[1]*canvas.height,5,0,Math.PI*2);ctx.fillStyle=color;ctx.fill()});
    if(label){ctx.font='bold 18px Segoe UI';ctx.fillStyle='#102c25';ctx.fillRect(vertices[0][0]*canvas.width,vertices[0][1]*canvas.height-22,80,24);ctx.fillStyle=color;ctx.fillText(label,vertices[0][0]*canvas.width+4,vertices[0][1]*canvas.height-4);}
  }
  polygons.forEach(s=>shape(s.polygon,'#c7f36b',s.id));shape(points,'#7cceff','');
  $('points-count').textContent=`Позначено кутів: ${points.length} / 4`;
}
function renderPolygons(){
  $('polygon-list').innerHTML=polygons.map(s=>`<span class="polygon-tag">${escapeHTML(s.id)}<button type="button" data-remove-polygon="${escapeHTML(s.id)}" aria-label="Видалити розмітку ${escapeHTML(s.id)}">×</button></span>`).join('')||'<small>Місця ще не розмічені.</small>';
}
async function preview(){
  if(!adminKey||previewBusy||!cameraInfo?.running||cameraInfo.phase!=='ONLINE'||$('freeze-frame').checked)return;
  const key=adminKey;previewBusy=true;
  try{
    const response=await fetch('/api/admin/camera/frame',{headers:{Authorization:'Bearer '+key},cache:'no-store'});
    if(!response.ok)return;
    const bitmap=await createImageBitmap(await response.blob());
    if(key!==adminKey||$('freeze-frame').checked){bitmap.close();return;}
    if(lastFrame)lastFrame.close();lastFrame=bitmap;canvas.width=bitmap.width;canvas.height=bitmap.height;
    $('camera-placeholder').hidden=true;
    $('frame-time').textContent='Кадр: '+new Date(cameraInfo.frame_at*1000).toLocaleTimeString('uk-UA');drawFrame();
  }catch(e){$('frame-time').textContent='Кадр тимчасово недоступний';}finally{previewBusy=false;}
}
canvas.addEventListener('click',e=>{
  if(!lastFrame){error('Спочатку підключіть камеру й отримайте кадр');return;}
  if(points.length===4){error('Натисніть «Додати місце» або «Скинути кути»');return;}
  $('freeze-frame').checked=true;
  const rect=canvas.getBoundingClientRect();points.push([Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))]);
  $('frame-time').textContent='Зафіксований кадр для розмітки';drawFrame();
});
$('reset-points').addEventListener('click',()=>{points=[];drawFrame()});
$('add-polygon').addEventListener('click',()=>{
  if(points.length!==4){error('Позначте чотири кути місця');return;}
  const turns=points.map((a,i)=>{const b=points[(i+1)%4],c=points[(i+2)%4];return(b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])});
  if(!(turns.every(x=>x>0)||turns.every(x=>x<0))){error('Кути мають іти по периметру без перетинів');return;}
  const id=$('polygon-space').value;polygons=polygons.filter(s=>s.id!==id);polygons.push({id,polygon:points});points=[];renderPolygons();drawFrame();
});
$('polygon-list').addEventListener('click',e=>{const id=e.target.dataset.removePolygon;if(id){polygons=polygons.filter(s=>s.id!==id);renderPolygons();drawFrame()}});
async function cameraAction(button,action){
  if(button.disabled)return;
  button.disabled=true;
  try{await action();}catch(e){error(e.message);}finally{button.disabled=false;if(adminKey)loadCamera(false).catch(()=>{});}
}
$('camera-form').addEventListener('submit',e=>{e.preventDefault();cameraAction($('save-camera'),async()=>{
  const changed=Boolean($('camera-url').value.trim());
  const data=await api('/admin/camera',{method:'PUT',admin:true,body:{name:$('camera-name').value,rtsp_url:$('camera-url').value.trim()||null,transport:$('camera-transport').value,inference:$('camera-ai').checked}});
  if(!adminKey)return;
  $('camera-url').value='';showCamera(data,true);
  if(changed){if(lastFrame)lastFrame.close();lastFrame=null;ctx.clearRect(0,0,canvas.width,canvas.height);$('camera-placeholder').hidden=false;}
  notice('Налаштування камери збережено');
})});
$('start-camera').addEventListener('click',()=>cameraAction($('start-camera'),async()=>{
  const data=await api('/admin/camera/start',{method:'POST',admin:true});
  if(!adminKey)return;$('freeze-frame').checked=false;showCamera(data);notice('Підключаємося до RTSP-потоку…');
}));
$('stop-camera').addEventListener('click',()=>cameraAction($('stop-camera'),async()=>{
  const data=await api('/admin/camera/stop',{method:'POST',admin:true});if(!adminKey)return;$('freeze-frame').checked=true;showCamera(data);notice('Камеру зупинено');
}));
$('save-polygons').addEventListener('click',()=>cameraAction($('save-polygons'),async()=>{
  if(points.length)throw new Error('Спочатку додайте поточне місце або скиньте кути');
  const data=await api('/admin/camera/polygons',{method:'PUT',admin:true,body:{spaces:polygons}});
  if(!adminKey)return;$('freeze-frame').checked=true;showCamera(data,true);notice('Розмітку збережено. Можна знову підключити камеру.');
}));
setInterval(async()=>{
  if(!adminKey||cameraPolling)return;cameraPolling=true;
  try{await loadCamera(false);await preview();}catch(e){$('camera-phase').textContent='Сервер недоступний';}finally{cameraPolling=false;}
},1500);
