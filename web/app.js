'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=value=>value?new Date(value).toLocaleString(window.U30I18n?.language()==='en'?'en-GB':'zh-TW',{timeZone:'Asia/Taipei',hour12:false}):'—';
const num=value=>value==null||!Number.isFinite(value)?'—':new Intl.NumberFormat('zh-TW',{maximumFractionDigits:3}).format(value);
const unit=value=>({'uE':'µmol/m²/s','*C':'°C'}[value]||value||'');
let channels=[],backups=[],lastHistory='',metadataVersion='',historyRequest=0,appliedFilters=null,deletePlan=null,trashOffset=0;
async function api(path,method='GET',body){const r=await fetch(path,{method,...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(path.startsWith('/api/data/')||path.startsWith('/api/studio/')?120000:30000)});const data=await r.json();if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data;}
function notice(id,message){$(id).hidden=!message;$(id).textContent=message||'';}
function renderStatus(state){
 window.u30LastState=state;window.dispatchEvent(new CustomEvent('u30-status',{detail:state}));
 if(typeof renderAlarms==='function')renderAlarms(state);
 const s=state.snapshot,online=state.usb_connected&&!state.stale,paused=state.sync.running,waiting=state.waiting_for_query;
 renderSamplingState(state);
 $('connection').className='pill '+(paused?'':online?'online':'offline');$('connection').textContent=paused?'下載記憶體中':online?(waiting?'等待下次採集':'正在採集'):'USB 未連接';
 notice('error',state.error?`U30 讀取未成功：${state.error}。裝置接回後會自動重試。`:null);
 renderCollectionState(state);
 if(window.renderNativeControl)window.renderNativeControl(state);
 if(!s)return;
 $('identity').textContent=`${s.product}　/　S/N ${s.serial}　/　韌體 ${s.firmware}, ${s.hardware_firmware}`;
 $('last-update').textContent=date(state.last_success_utc||s.observed_utc);
 $('cadence').textContent=(window.U30I18n?.language()==='en'?`Acquisition interval ${state.interval_seconds} s · Next ${date(state.next_query_utc)} GMT+8`:`全站採集間隔 ${state.interval_seconds} 秒 · 下次 ${date(state.next_query_utc)} GMT+8`);
 $('battery').textContent=online?s.battery_percent:'—';$('battery-bar').style.width=(online?s.battery_percent:0)+'%';
 $('sensor-count').textContent=online?s.sensors.length:'—';$('sensor-note').textContent=paused?'記憶體下載中，暫停更新':online?`已辨識 ${s.sensor_slots.length} 個感測槽位`:'等待裝置重新連接';
 $('memory').textContent=num(s.archive.used_percent);$('memory-note').textContent=s.archive.metadata_valid===false?(window.U30I18n?.language()==='en'?'Memory information unavailable':'記憶體資訊無法確認'):s.archive.pointer_valid===false?`${window.U30I18n?.language()==='en'?'Unknown':'無法確認'} / ${s.archive.memory_bytes/1024} KB`:`${Math.round(s.archive.data_end/1024)} / ${s.archive.memory_bytes/1024} KB`;
 $('logging').textContent=({0:'已停止',1:'記錄中',3:'等待觸發',5:'等待啟動'}[s.status_code]||'未知');
 $('interval').textContent=`U30 記錄間隔 ${num(s.archive.logging_interval_seconds)} 秒`;
 notice('notice',s.archive.metadata_valid===false?(window.U30I18n?.language()==='en'?'U30 memory information is unavailable. Live readings continue with an unverified deployment; the next metadata refresh will retry automatically.':'U30 記憶體資訊讀取失敗，即時量測仍持續保存，部署暫標示為未確認；下次資訊更新會自動重試。'):s.archive.pointer_valid===false?(window.U30I18n?.language()==='en'?`U30 memory position is invalid. Live sensor collection and full raw backup are available; historical bounds are unverified. Native logging: ${s.logging?'running':'stopped'}.`:`U30 記憶體位置回應異常，即時採集與完整原始備份可運作；舊歷史範圍無法確認，可透過原始備份流程重新設定採集頻率。U30 原生記錄：${s.logging?'記錄中':'已停止'}。`):s.archive.memory_full?`U30 記憶體已滿${s.logging?'':'，已停止記錄'}。Pi 依下方各通道的排程與容量設定保存即時量測。`:null);
 $('reading-time').textContent=online?date(s.observed_utc):'目前未取得新讀值';
 renderSensors(s,online,paused,waiting);
 const fields={'記錄器序號':s.serial,'韌體版本':`${s.firmware}, ${s.hardware_firmware}`,'部署編號':s.archive.deployment??'—','上次啟動':date(s.archive.launched_utc),'首筆歷史時間':date(s.archive.first_sample_utc),'U30 記錄間隔':num(s.archive.logging_interval_seconds)+' 秒','覆寫次數':s.archive.wrap_count??'—','環狀記錄':s.archive.wrap_enabled==null?'—':s.archive.wrap_enabled?'啟用':'關閉','感測器位圖槽位':s.sensor_slots.join(', ')||'無','電池原始回應':s.battery_response_hex};
 $('details').innerHTML=Object.entries(fields).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
 const sync=state.sync;$('sync').disabled=sync.running;
 notice('sync-state',sync.running?backupProgress(sync):sync.error?'備份失敗：'+sync.error:sync.result?`下載完成，已保存 ${sync.result.bytes/1024} KB 原始檔案。${sync.result.decoded?'歷史讀值已匯入；相同資料不會重複新增。':sync.result.error||'請查看備份資訊。'}`:null);
 const version=(sync.result?.id||'')+s.sensors.map(c=>c.key).join(',');
 if(version!==metadataVersion){metadataVersion=version;loadMetadata();}
}

function renderSensors(s,online,paused,waiting=false){
 const english=window.U30I18n?.language()==='en',list=s.sensors,container=$('sensors');
 if(!online||paused||!list.length){container.dataset.structure='';container.innerHTML=`<div class="empty">${paused?'正在下載記憶體，即時量測暫停。下載結束後會自動恢復。':online?'目前沒有偵測到感測通道，請確認 Smart Sensor 的連接。':'等待 U30 連接後取得新讀值。'}</div>`;return;}
 const signature=JSON.stringify(list.map(c=>[c.key,c.label,c.measurement,c.model,c.serial,c.instrument,c.units]))+english;
 if(container.dataset.structure!==signature){container.dataset.structure=signature;container.innerHTML=list.map(c=>`<article class="sensor" data-sensor-key="${esc(c.key)}"><div class="sensor-top"><h3>${esc(c.label||c.measurement)}</h3><span class="pill"></span></div><div class="sensor-value"></div><div class="sensor-unit">${esc(unit(c.units))} · ${esc(c.measurement)}</div><div class="sensor-meta"><span>${esc(c.model)}<br>S/N ${esc(c.serial)}</span><span>${english?'Slot':'槽位'} ${esc(c.instrument)}<br><span class="sensor-raw"></span></span></div><button class="sensor-inspect quiet" data-inspect-channel="${esc(c.key)}">${english?'Explore channel':'查看此通道資料'} <span aria-hidden="true">↗</span></button></article>`).join('');}
 for(const c of list){const card=[...container.children].find(n=>n.dataset.sensorKey===c.key);card.querySelector('.sensor-value').textContent=num(c.value);const pill=card.querySelector('.pill');pill.className='pill '+(c.quality==='ok'?'online':'offline');pill.textContent=c.quality==='ok'?(waiting?(english?'Last reading':'上次讀值'):(english?'Connected':'已連接')):(english?'Invalid reading':'讀值異常');card.querySelector('.sensor-raw').textContent=(english?'Raw value ':'原始值 ')+(c.raw??'—');}
}

async function loadMetadata(){
 try{[channels,backups]=await Promise.all([api('/api/channels'),api('/api/backups')]);
  const selected=$('channel').value;
  $('channel').innerHTML=channels.map(c=>`<option value="${esc(c.key)}">${esc(c.label||c.measurement)} · ${esc(c.serial)} · ${esc(unit(c.units))}</option>`).join('');
  if(channels.some(c=>c.key===selected))$('channel').value=selected;
  if(!$('channel').value&&channels.length)$('channel').value=channels[0].key;
  $('backups').innerHTML=backups.length?backups.map(b=>`<div class="backup-row"><div><strong>${esc(date(b.created_utc))} · ${b.bytes/1024} KB</strong><p>${b.decoded?`${b.channels} 個通道 · ${num(b.rows)} 筆讀值`:'原始備份已保留；尚未解碼'}<br>${b.first_utc?esc(date(b.first_utc)+' — '+date(b.last_utc)):(esc(b.error||''))}<br>SHA-256 ${esc(b.sha256.slice(0,16))}…</p></div><a href="/api/backups/${esc(b.id)}.bin">下載原始檔 ↗</a></div>`).join(''):'<p class="muted">尚無完整備份</p>';
  await loadHistory();
 }catch(e){notice('error','資料載入失敗：'+e.message);}
}
function query(all=false){const p=new URLSearchParams({source:$('source').value});if(!all&&$('channel').value)p.set('channel',$('channel').value);if($('start').value)p.set('start',$('start').value+'+08:00');if($('end').value)p.set('end',$('end').value+'+08:00');if(!all&&$('value-op').value){p.set('value_op',$('value-op').value);p.set('value',$('value-threshold').value);if($('value-op').value==='between')p.set('value_max',$('value-max').value);}return p;}
async function loadHistory(apply=true){
 if(apply&&!$('filters').reportValidity())return;
 const request=++historyRequest;
 try{const p=apply||!appliedFilters?query():new URLSearchParams(appliedFilters);const channel=channels.find(c=>c.key===p.get('channel'));
 const result=await api('/api/history?'+p);if(request!==historyRequest)return;appliedFilters=p.toString();notice('history-error',null);
 $('export').href='/api/export.csv?'+p;const all=new URLSearchParams(p);['channel','value_op','value','value_max'].forEach(k=>all.delete(k));$('export-all').href='/api/export.csv?'+all;$('export-all').hidden=!!p.get('value_op');
 $('chart-title').textContent=channel?`${channel.label||channel.measurement} / ${channel.serial} (${unit(channel.units)})`:'尚無感測通道';$('filter-unit').textContent=unit(channel?.units);
 $('chart-info').textContent=`${num(result.total)} 筆符合 · 顯示 ${num(result.rows.length)} 點`;
 draw(result.rows,channel);$('coverage').textContent=result.rows.length?date(result.rows[0].timestamp_utc)+' — '+date(result.rows.at(-1).timestamp_utc):'此範圍沒有資料';
 const quality={ok:'正常',missing:'缺值',eof:'無有效值',unsupported_conversion:'換算尚未支援',missing_or_unsupported:'缺值或換算尚未支援',error:'讀取失敗'};
 $('history-rows').innerHTML=result.rows.slice(-20).reverse().map(r=>`<tr><td>${esc(date(r.timestamp_utc))}</td><td>${num(r.value)} ${esc(unit(channel?.units))}</td><td>${esc(r.raw??'—')}</td><td>${esc(formatQuality(r.quality,quality))}</td></tr>`).join('');lastHistory=Date.now();
 $('matching-intervals').hidden=!p.get('value_op');$('interval-title').textContent=`符合條件的時間範圍 · 共 ${num(result.interval_count)} 段${result.interval_count>200?'（顯示最近 200 段）':''}`;
 $('interval-rows').innerHTML=result.intervals.map(r=>`<tr><td>${esc(date(r.start_utc))}</td><td>${esc(date(r.end_utc))}</td><td>${num(r.samples)}</td><td>${num(r.min_value)}～${num(r.max_value)} ${esc(unit(channel?.units))}</td></tr>`).join('')||'<tr><td colspan="4">沒有符合條件的時間範圍</td></tr>';
 }catch(e){if(request===historyRequest){notice('history-error',e.message);}}
}
function draw(rows,channel){
 const finite=rows.filter(r=>r.value!=null&&Number.isFinite(r.value));
 if(!finite.length){$('chart').innerHTML='<div class="empty">此日期範圍沒有有效讀值。可切換資料來源或選擇「全部時間」。</div>';return;}
 const W=1000,H=270,L=65,R=20,T=20,B=35;let ymin=Math.min(...finite.map(r=>r.value)),ymax=Math.max(...finite.map(r=>r.value));const pad=(ymax-ymin)*.08||Math.max(1,Math.abs(ymax)*.05);ymin-=pad;ymax+=pad;
 const tmin=new Date(rows[0].timestamp_utc).getTime(),tmax=new Date(rows.at(-1).timestamp_utc).getTime();const x=r=>L+(new Date(r.timestamp_utc).getTime()-tmin)/(tmax-tmin||1)*(W-L-R),y=v=>T+(ymax-v)/(ymax-ymin)*(H-T-B);let d='',pen=false;
 let segment=null;for(const r of rows){if(r.value==null){pen=false;continue;}if(r.segment!==segment)pen=false;d+=`${pen?'L':'M'}${x(r).toFixed(2)},${y(r.value).toFixed(2)} `;pen=true;segment=r.segment;}
 let grid='';for(let i=0;i<5;i++){const v=ymin+(ymax-ymin)*i/4,py=y(v);grid+=`<line x1="${L}" y1="${py}" x2="${W-R}" y2="${py}" stroke="#e6ede7"/><text x="${L-10}" y="${py+4}" text-anchor="end">${num(v)}</text>`;}
 const labels=[0,.25,.5,.75,1].map(k=>{const value=new Date(tmin+k*(tmax-tmin));const label=value.toLocaleDateString('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit'})+(tmax-tmin<172800000?' '+value.toLocaleTimeString('zh-TW',{timeZone:'Asia/Taipei',hour:'2-digit',minute:'2-digit',hour12:false}):'');return `<text x="${L+k*(W-L-R)}" y="${H-9}" text-anchor="middle">${esc(label)}</text>`;}).join('');
 const segmentCounts=new Map();finite.forEach(r=>segmentCounts.set(r.segment,(segmentCounts.get(r.segment)||0)+1));const dots=finite.filter(r=>segmentCounts.get(r.segment)===1).map(r=>`<circle cx="${x(r)}" cy="${y(r.value)}" r="3" fill="#317f65"/>`).join('');
 const hits=finite.map((r,i)=>`<circle class="chart-point" data-point="${i}" cx="${x(r)}" cy="${y(r.value)}" r="7" fill="transparent" tabindex="0" role="button" aria-label="${esc(date(r.timestamp_utc)+' GMT+8，'+String(r.value)+' '+unit(channel?.units))}"><title>${esc(date(r.timestamp_utc)+' GMT+8 / '+String(r.value)+' '+unit(channel?.units))}</title></circle>`).join('');
 $('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="歷史量測折線圖">${grid}${labels}<path d="${d}" stroke="#317f65" fill="none" stroke-width="1.7" vector-effect="non-scaling-stroke"/>${dots}<line class="chart-cursor" y1="${T}" y2="${H-B}" hidden/><circle class="chart-marker" r="4" fill="#174c41" hidden/>${hits}</svg><div class="chart-tooltip" role="status" hidden></div>`;
 const svg=$('chart').querySelector('svg'),tooltip=$('chart').querySelector('.chart-tooltip'),cursor=svg.querySelector('.chart-cursor'),marker=svg.querySelector('.chart-marker');
 let pinned=false,lastPoint=null;
 function reveal(index){const r=finite[index];if(!r)return;lastPoint=index;tooltip.innerHTML=`<strong>${esc(channel?.label||channel?.measurement||r.channel)}</strong><span>${esc(date(r.timestamp_utc))} GMT+8</span><b>${esc(String(r.value))} <small>${esc(unit(channel?.units))}</small></b><span>原始值 ${esc(r.raw??'—')}</span>`;tooltip.hidden=false;cursor.removeAttribute('hidden');marker.removeAttribute('hidden');cursor.setAttribute('x1',x(r));cursor.setAttribute('x2',x(r));marker.setAttribute('cx',x(r));marker.setAttribute('cy',y(r.value));const chartBox=$('chart').getBoundingClientRect(),svgBox=svg.getBoundingClientRect(),scale=Math.min(svgBox.width/W,svgBox.height/H),left=svgBox.left+(svgBox.width-W*scale)/2+x(r)*scale-chartBox.left,top=svgBox.top+(svgBox.height-H*scale)/2+y(r.value)*scale-chartBox.top;tooltip.style.left=Math.max(6,Math.min(chartBox.width-tooltip.offsetWidth-6,left+12))+'px';tooltip.style.top=Math.max(4,Math.min(chartBox.height-tooltip.offsetHeight-4,top-tooltip.offsetHeight-12))+'px';}
 function nearest(event){const target=event.target.closest?.('[data-point]');if(target)return Number(target.dataset.point);const box=svg.getBoundingClientRect(),scale=Math.min(box.width/W,box.height/H),px=(event.clientX-box.left-(box.width-W*scale)/2)/scale,py=(event.clientY-box.top-(box.height-H*scale)/2)/scale;if(px<L-10||px>W-R+10||py<T-15||py>H-B+15)return null;let chosen=0,distance=Infinity;finite.forEach((r,i)=>{const score=Math.abs(x(r)-px);if(score<distance){distance=score;chosen=i;}});return chosen;}
 function hide(){tooltip.hidden=true;cursor.setAttribute('hidden','');marker.setAttribute('hidden','');}
 svg.addEventListener('pointermove',event=>{if(!pinned&&event.pointerType!=='touch'){const index=nearest(event);if(index!==null)reveal(index);else hide();}});
 svg.addEventListener('pointerleave',()=>{if(!pinned)hide();});
 svg.addEventListener('focusin',event=>{if(event.target.dataset.point!=null)reveal(Number(event.target.dataset.point));});
 svg.addEventListener('focusout',()=>{if(!pinned)hide();});
 svg.addEventListener('click',event=>{const index=nearest(event);if(index!==null){pinned=!(pinned&&lastPoint===index);if(pinned)reveal(index);else hide();}});
 svg.addEventListener('keydown',event=>{if(event.key==='Escape'){pinned=false;hide();}else if((event.key==='Enter'||event.key===' ')&&event.target.dataset.point!=null){event.preventDefault();pinned=!pinned;if(pinned)reveal(Number(event.target.dataset.point));else hide();}});
}
$('filters').addEventListener('submit',e=>{e.preventDefault();loadHistory();});$('source').addEventListener('change',loadHistory);$('channel').addEventListener('change',loadHistory);
document.querySelectorAll('[data-days]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-days]').forEach(b=>b.classList.remove('active'));button.classList.add('active');const days=button.dataset.days;if(days==='all'){$('start').value='';$('end').value='';}else{const end=new Date(),start=new Date(end.getTime()-Number(days)*86400000);const local=d=>new Date(d.getTime()+8*3600000).toISOString().slice(0,19);$('start').value=local(start);$('end').value=local(end);}loadHistory();}));
$('refresh').addEventListener('click',async()=>{try{await api('/api/refresh','POST');$('refresh').textContent='已排入讀取';setTimeout(()=>$('refresh').textContent='重新讀取',2000);}catch(e){notice('error',e.message);}});
$('sync').addEventListener('click',async()=>{try{if($('readout-mode').value==='stop'){if(!$('readout-ack').checked||!$('readout-key').value)throw new Error('請先輸入 Pi 設定密碼並確認停止記錄');await api('/api/readout','POST',{mode:'stop',confirmed:true,setup_key:$('readout-key').value});}else await api('/api/sync','POST');$('sync').disabled=true;await tick();}catch(e){notice('sync-state',e.message);}finally{$('readout-key').value='';$('readout-ack').checked=false;}});
let ticking=false;
async function tick(){if(ticking)return;ticking=true;try{renderStatus(await api('/api/status'));if(new URLSearchParams(appliedFilters||'').get('source')==='live'&&Date.now()-lastHistory>5000&&!$('chart').closest('[hidden]')&&!$('chart').querySelector('.chart-tooltip:not([hidden])'))await loadHistory(false);}catch(e){$('connection').textContent='伺服器未連線';$('connection').className='pill offline';notice('error','無法連線至樹莓派：'+e.message);}finally{ticking=false;}}

function configureValue(){const op=$('value-op').value;$('value-threshold').disabled=!op;$('value-threshold').required=!!op;$('value-max-label').hidden=op!=='between';$('value-max').required=op==='between';$('value-max').disabled=op!=='between';$('delete-scope').options[1].disabled=!!op;if(op)$('delete-scope').value='selected';}
$('value-op').addEventListener('change',configureValue);
$('clear-value').addEventListener('click',()=>{$('value-op').value='';$('value-threshold').value='';$('value-max').value='';configureValue();loadHistory();});
function filterObject(p){return Object.fromEntries([...p].map(([key,value])=>[key,[value]]));}
function planSummary(plan){const q=plan.query,source=q.source[0]==='live'?'樹莓派即時採集':'U30 歷史記錄（Pi 已匯入）';const names={gt:'大於',gte:'大於等於',lt:'小於',lte:'小於等於',between:'介於'};const value=q.value_op?`${names[q.value_op[0]]} ${q.value?.[0]}${q.value_max?'～'+q.value_max[0]:''}`:'不限數值';return `<p><strong>${esc(source)} · ${num(plan.count)} 筆</strong></p><p>${esc(date(q.start[0]))} ～ ${esc(date(q.end[0]))}</p><p>數值條件：${esc(value)}</p><ul>${plan.channels.map(c=>{const channel=channels.find(x=>x.key===c.channel);return `<li>${esc(channel?.label||channel?.measurement||c.channel)} / ${esc(channel?.serial||'')}：${num(c.count)} 筆</li>`;}).join('')}</ul>`;}
$('delete-preview').addEventListener('click',async()=>{if(!$('filters').reportValidity())return;if(!$('start').value||!$('end').value){notice('data-notice','請先填寫上方的起始與結束時間（台北）。');return;}$('delete-preview').disabled=true;notice('data-notice','正在核對刪除範圍與筆數…');try{const p=query();if($('delete-scope').value==='all'){if(p.get('value_op'))throw new Error('全部通道不可套用單一單位的數值條件');p.delete('channel');}deletePlan=await api('/api/data/delete-preview','POST',{filters:filterObject(p)});notice('data-notice',null);if(!deletePlan.count){notice('data-notice','此範圍沒有可刪除的資料。');return;}$('delete-summary').innerHTML=planSummary(deletePlan);$('delete-key').value='';$('delete-ack').checked=false;$('delete-submit').textContent=`移至回收區（${num(deletePlan.count)} 筆）`;notice('delete-error',null);$('delete-dialog').showModal();}catch(e){notice('data-notice',e.message);}finally{$('delete-preview').disabled=false;}});
$('delete-cancel').addEventListener('click',()=>$('delete-dialog').close());
$('delete-dialog').addEventListener('close',()=>{deletePlan=null;$('delete-key').value='';});
$('delete-confirm').addEventListener('submit',async e=>{e.preventDefault();if(!deletePlan)return;$('delete-submit').disabled=true;try{const result=await api('/api/data/trash','POST',{token:deletePlan.token,setup_key:$('delete-key').value,confirmed:$('delete-ack').checked});$('delete-dialog').close();notice('data-notice',`已將 ${num(result.count)} 筆資料移至回收區，可在下方復原。`);await loadMetadata();await loadTrash();}catch(e){notice('delete-error',e.message);}finally{$('delete-key').value='';$('delete-submit').disabled=false;}});
async function loadTrash(){try{const listing=await api('/api/trash?offset='+trashOffset),batches=listing.batches;$('trash-prev').disabled=trashOffset===0;$('trash-next').disabled=trashOffset+30>=listing.total;$('trash-page').textContent=listing.total?`${trashOffset+1}～${Math.min(trashOffset+30,listing.total)} / ${listing.total} 批`:'0 批';$('trash-list').innerHTML=batches.length?batches.map(b=>`<div class="trash-row"><div><strong>${esc(date(b.deleted_utc))} · ${num(b.count)} 筆</strong><p>${esc(b.query.source[0]==='live'?'樹莓派即時採集':'Pi 匯入的 U30 歷史')} · ${esc(date(b.query.start[0]))} ～ ${esc(date(b.query.end[0]))}</p></div>${b.restored_utc?'<span class="muted">已復原</span>':`<button class="quiet restore-batch" data-batch="${esc(b.id)}">復原這批資料</button>`}</div>`).join(''):'<p class="muted">目前沒有刪除記錄</p>';$('trash-list').querySelectorAll('.restore-batch').forEach(button=>button.addEventListener('click',async()=>{if(!$('restore-key').value){notice('data-notice','請先輸入回收區上方的 Pi 設定密碼。');$('restore-key').focus();return;}button.disabled=true;try{const result=await api('/api/data/restore','POST',{batch:button.dataset.batch,setup_key:$('restore-key').value});notice('data-notice',`已復原 ${num(result.restored)} 筆資料。`);await loadMetadata();await loadTrash();}catch(e){notice('data-notice',e.message);button.disabled=false;}finally{$('restore-key').value='';}}));}catch(e){notice('data-notice',e.message);}}
$('trash-section').addEventListener('toggle',()=>{if($('trash-section').open)loadTrash();});
async function loadNetwork(){
 try{const n=await api('/api/network');window.u30NetworkState=n;if(window.renderPasswordNetwork)window.renderPasswordNetwork(n);$('wifi-submit').disabled=!n.available||n.busy||['connecting','changing_password'].includes(n.job?.state);
 if(!n.available){$('network-mode').textContent='設定服務未啟用';notice('network-notice',n.error);return;}
 $('hostname-url').textContent=n.hostname_url;$('hostname-url').href=n.hostname_url;$('setup-ssid').textContent=n.setup_ssid;
 $('network-mode').textContent=({setup:'設定熱點已開啟',wifi:'Wi-Fi 已連線',ethernet:'網路線已連線',offline:'尚未連線',starting:'啟動中',connecting:'正在連接 Wi-Fi'}[n.mode]||n.mode);
 const names={ethernet:'網路線',wifi:'家用 Wi-Fi',setup:'Pi 設定熱點'};
 $('network-interfaces').innerHTML=n.interfaces.length?n.interfaces.map(i=>`<li><span>${names[i.kind]||esc(i.name)}</span><a href="${esc(i.url)}">${esc(i.ip)}:8080</a></li>`).join(''):'<li>等待取得 IP</li>';
 notice('network-notice',n.error||(n.job?.kind==='password'?null:n.job?.message));if(n.setup_active&&location.hostname==='10.42.0.1')$('wifi-settings').open=true;
 }catch(e){notice('network-notice','目前無法查詢網路狀態。若正在切換 Wi-Fi，請稍候再重新開啟網頁。');}
}
$('wifi-form').addEventListener('submit',async e=>{e.preventDefault();$('wifi-submit').disabled=true;try{
 await api('/api/network/connect','POST',{ssid:$('wifi-ssid').value,password:$('wifi-password').value,setup_key:$('setup-key').value});
 $('wifi-password').value='';$('setup-key').value='';notice('network-notice','已開始連線。設定熱點將暫時中斷；約 30～60 秒後，請切回家用網路並開啟上方固定網址。失敗時 Pi 會恢復設定熱點。');
 }catch(e){notice('network-notice',e.message);$('wifi-submit').disabled=false;}});
loadMetadata();tick();loadNetwork();setInterval(tick,1000);setInterval(loadNetwork,10000);

$('trash-prev').addEventListener('click',()=>{trashOffset=Math.max(0,trashOffset-30);loadTrash();});
$('trash-next').addEventListener('click',()=>{trashOffset+=30;loadTrash();});
['start','end'].forEach(id=>$(id).addEventListener('input',()=>{document.querySelectorAll('[data-days]').forEach(b=>b.classList.toggle('active',b.dataset.days==='all'&&!$('start').value&&!$('end').value));}));

let collectionState=null,collectionSignature='',u30DefaultsLoaded=false,u30Plan=null;
function formatQuality(value,names){if(value?.startsWith('statistics_n=')){const n=/statistics_n=(\d+)/.exec(value)?.[1],expected=/expected=(\d+)/.exec(value)?.[1];return `統計 ${n} / ${expected} 筆${n!==expected?'（有漏樣）':''}`;}return names[value?.split(';')[0]]||value;}
function configureScheduleForm(prefix){const delayed=$(prefix+'-start').value==='delayed';$(prefix+'-date-label').hidden=!delayed;$(prefix+'-date').required=delayed;if(prefix==='collect'){const stats=[...document.querySelectorAll('[name="pi-stat"]:checked')].length>0;$('collect-aggregate-label').hidden=!stats;$('collect-aggregate').required=stats;$('statistics-help').hidden=!stats;}}
function fillCollectionForm(){const entry=collectionState?.channels[$('collect-channel').value],c=entry?.config;$('collect-aggregate').value=c?.aggregate_count??(c?.statistics.length?Math.max(1,c.interval/c.sample_interval):1);$('collect-start').value=c?.start_mode??'immediate';$('collect-enabled').checked=c?.enabled??true;$('collect-date').value=c?.start_at?new Date(new Date(c.start_at).getTime()+28800000).toISOString().slice(0,19):'';document.querySelectorAll('[name="pi-stat"]').forEach(input=>input.checked=!!c?.statistics.includes(input.value));configureScheduleForm('collect');renderCollectionRuntime();}
function renderCollectionRuntime(){const entry=collectionState?.channels[$('collect-channel').value],c=entry?.config,r=entry?.runtime;$('collect-button').disabled=!(c?.enabled&&c.start_mode==='button'&&r?.start==null);$('collect-runtime').textContent=!entry?'尚未另設排程，維持一般即時存檔':!c.enabled?'此通道已停用存檔':r.start==null?'等待網頁按鈕啟動':`開始：${date(new Date(r.start*1000).toISOString())}${r.n?' · 本視窗已取樣 '+r.n+' 筆':''}`;}
function renderCollectionState(state){if(state.pi_collection){collectionState=state.pi_collection;const storage=collectionState.storage;$('storage-status').textContent=`已存 ${num(storage.rows)} 筆即時資料 · ${storage.max_rows?'上限 '+num(storage.max_rows)+' 筆':'未設筆數上限'} · ${storage.halted?'容量已滿，Pi 存檔停止；調整容量設定可恢復':storage.mode==='wrap'?'循環覆寫模式':'滿額停止模式'}`;renderCollectionRuntime();}const s=state.snapshot;if(s){const signature=s.sensors.map(c=>c.key).join('|');if(signature!==collectionSignature){collectionSignature=signature;const selected=$('collect-channel').value;$('collect-channel').innerHTML=s.sensors.map(c=>`<option value="${esc(c.key)}">${esc(c.label||c.measurement)} · ${esc(c.serial)} · ${esc(unit(c.units))}</option>`).join('');if(s.sensors.some(c=>c.key===selected))$('collect-channel').value=selected;fillCollectionForm();}if(!u30DefaultsLoaded&&s.archive.metadata_valid!==false){u30DefaultsLoaded=true;$('u30-name').value=s.name||'U30 Station';$('u30-interval').value=s.archive.logging_interval_seconds;$('u30-memory').value=s.archive.wrap_enabled?'wrap':'stop';configureScheduleForm('u30');}$('u30-current').textContent=s.archive.metadata_valid===false?(window.U30I18n?.language()==='en'?'Deployment information unavailable; live collection continues':'部署資訊尚未確認；Pi 即時採集持續中'):`目前全站：${state.interval_seconds} 秒 / ${s.archive.wrap_enabled?'循環':'滿額停止'} / 部署 ${s.archive.deployment}`;}$('u30-preview').disabled=!!state.deployment?.running||state.sync.running||state.snapshot?.archive.metadata_valid===false;if(state.deployment?.running||state.deployment?.error||state.deployment?.result)notice('u30-notice',state.deployment.phase+(state.deployment.error?'：'+state.deployment.error:''));}
async function loadCollection(){try{collectionState=await api('/api/collection');$('storage-limit').value=collectionState.storage.max_rows;$('storage-mode').value=collectionState.storage.mode;configureStorage();if(!$('collect-channel').options.length){const physical=(await api('/api/channels')).filter(c=>c.kind!=='statistic'&&c.kind!=='battery');if(!$('collect-channel').options.length)$('collect-channel').innerHTML=physical.map(c=>`<option value="${esc(c.key)}">${esc(c.label||c.measurement)} · ${esc(c.serial)}</option>`).join('');}fillCollectionForm();}catch(e){notice('collect-notice',e.message);}}
function configureStorage(){const wrap=$('storage-mode').value==='wrap';$('storage-ack-label').hidden=!wrap;$('storage-ack').required=wrap;}
$('storage-mode').addEventListener('change',configureStorage);
$('collect-channel').addEventListener('change',fillCollectionForm);
['collect','u30'].forEach(prefix=>$(prefix+'-start').addEventListener('change',()=>configureScheduleForm(prefix)));
document.querySelectorAll('[name="pi-stat"]').forEach(input=>input.addEventListener('change',()=>configureScheduleForm('collect')));
$('collection-form').addEventListener('submit',async e=>{e.preventDefault();$('collect-save').disabled=true;try{await api('/api/collection/settings','POST',{channel:$('collect-channel').value,enabled:$('collect-enabled').checked,aggregate_count:Number($('collect-aggregate').value),statistics:[...document.querySelectorAll('[name="pi-stat"]:checked')].map(x=>x.value),start_mode:$('collect-start').value,start_at:$('collect-date').value+'+08:00',setup_key:$('collect-key').value});notice('collect-notice','通道設定已儲存；重新設定會從新的視窗開始統計。');await loadCollection();await loadMetadata();}catch(e){notice('collect-notice',e.message);}finally{$('collect-key').value='';$('collect-save').disabled=false;}});
$('collect-button').addEventListener('click',async()=>{if(!$('collect-key').value){notice('collect-notice','請輸入 Pi 設定密碼後再啟動此通道。');$('collect-key').focus();return;}try{await api('/api/collection/start','POST',{channel:$('collect-channel').value,setup_key:$('collect-key').value});notice('collect-notice','此通道已啟動。');await loadCollection();}catch(e){notice('collect-notice',e.message);}finally{$('collect-key').value='';}});
$('storage-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/collection/settings','POST',{target:'storage',max_rows:Number($('storage-limit').value),mode:$('storage-mode').value,overwrite_confirmed:$('storage-ack').checked,setup_key:$('storage-key').value});notice('storage-notice','Pi 容量設定已儲存。');await loadCollection();}catch(e){notice('storage-notice',e.message);}finally{$('storage-key').value='';$('storage-ack').checked=false;}});
$('u30-form').addEventListener('submit',async e=>{
 e.preventDefault();
 try{
  u30Plan=await api('/api/u30/preview','POST',{configuration:{name:$('u30-name').value,interval:Number($('u30-interval').value),start_mode:$('u30-start').value,start_at:$('u30-date').value+'+08:00',memory_mode:$('u30-memory').value,sample_interval:0}});
  const c=u30Plan.configuration,modes={immediate:'立即開始',aligned:'整點間隔開始',button:'U30 機身按鈕',delayed:'指定時間 '+date(c.start_at)};
  $('u30-summary').innerHTML=`<p><strong>U30 ${esc(u30Plan.serial)} · 原部署 ${esc(u30Plan.current_deployment)}</strong></p><p>${esc(c.name)} / 全站每 ${c.interval} 秒採集一次 / ${esc(modes[c.start_mode])}<br>${c.memory_mode==='wrap'?'循環覆寫':'滿了停止'} / U30、Pi 同步頻率<br>目前 ${u30Plan.channels.length} 個感測通道</p><p class="notice">${esc(u30Plan.warning)}<br>完整備份期間會暫停 Pi 即時讀值；備份不完整或核對失敗時，不會寫入裝置。</p>`;
  $('u30-apply').disabled=u30Plan.requires_stop_first;
  if(u30Plan.requires_stop_first)$('u30-summary').innerHTML+='<p class="notice">目前 U30 正在記錄或等待啟動。請先取消此預覽，在下方選擇「停止記錄，再下載」，完成後重新預覽，以保留最後一筆舊部署資料。</p>';
  $('u30-raw-ack-label').hidden=!u30Plan.raw_backup_required;
  $('u30-raw-ack').required=!!u30Plan.raw_backup_required;
  $('u30-raw-ack').checked=false;
  $('u30-key').value='';$('u30-ack').checked=false;
  notice('u30-error',null);$('u30-dialog').showModal();
 }catch(e){notice('u30-notice',e.message);}
});
$('u30-cancel').addEventListener('click',()=>$('u30-dialog').close());
$('u30-dialog').addEventListener('close',()=>{u30Plan=null;$('u30-key').value='';});
$('u30-confirm').addEventListener('submit',async e=>{e.preventDefault();if(!u30Plan)return;$('u30-apply').disabled=true;try{await api('/api/u30/apply','POST',{token:u30Plan.token,serial:u30Plan.serial,setup_key:$('u30-key').value,confirmed:$('u30-ack').checked,raw_recovery_confirmed:$('u30-raw-ack').checked});$('u30-dialog').close();notice('u30-notice','已排入全站設定：備份、U30 寫入及核對成功後，才會更新 Pi 的採集頻率。請等待完成。');}catch(e){notice('u30-error',e.message);}finally{$('u30-key').value='';$('u30-apply').disabled=false;}});
$('readout-mode').addEventListener('change',()=>{const stop=$('readout-mode').value==='stop';$('readout-key-label').hidden=!stop;$('readout-ack-label').hidden=!stop;});
loadCollection();

function renderSamplingState(state){
 const en=window.U30I18n?.language()==='en',s=state.sampling;
 $('sampling-status').textContent=en?`Acquisition interval: ${state.interval_seconds} seconds · Last successful read: ${date(state.last_success_utc)} · Next acquisition: ${date(state.next_query_utc)} GMT+8`:`採集間隔：${state.interval_seconds} 秒 · 上次成功讀取：${date(state.last_success_utc)} · 下次採集：${date(state.next_query_utc)} GMT+8`;
 const a=state.snapshot?.archive;
 const warning=!s?.configured?(en?'The existing U30 interval has not been confirmed. Refresh the device before applying unified settings.':'尚未確認既有 U30 間隔，請先重新讀取再套用全站設定。'):!s.matches_u30?(en?'The U30 settings do not match, or cannot be verified. Refresh and apply the unified settings before relying on this acquisition schedule.':'U30 設定不一致或無法確認；請重新讀取並套用全站設定後，再確認採集排程。'):state.snapshot&&!state.snapshot.logging?(en?'Native U30 logging is not running. Pi queries follow this interval, but U30 offline logging is not active.':'U30 本機尚未記錄。Pi 依此間隔查詢，但 U30 離線獨立記錄尚未啟動。'):null;
 notice('sampling-warning',warning);
}

function backupProgress(sync){const en=window.U30I18n?.language()==='en',pct=Math.round(sync.bytes/sync.total*100),two=sync.total>524288,pass=sync.bytes>=524288?2:1;return en?`Backing up and verifying U30 memory: ${pct}%${two?` (pass ${pass}/2; 512 KB per pass)`:``}. Live readings resume when finished.`:`正在備份與核對 U30 記憶體：${pct}%${two?`（第 ${pass}/2 遍；每遍 512 KB）`:``}，即時量測會在完成後恢復。`;}
