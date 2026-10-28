'use strict';
(() => {
 const t=(zh,en)=>window.U30I18n?.language()==='en'?en:zh;
 let state=null,dirty=false,pendingPlan=null,submitting=false;
 const notify=message=>notice('native-message',message);
 window.renderNativeControl=current=>{
  state=current;
  const s=current.snapshot,p=current.native_control||{},busy=!!current.deployment?.running||!!current.sync?.running;
  if(!dirty)$('native-auto').checked=p.enabled!==false;
  $('native-pause').disabled=submitting||busy||!current.device_state_fresh||![1,3,5].includes(s?.status_code);
  $('native-resume').disabled=submitting||busy||!current.device_state_fresh||s?.status_code!==0||s?.archive?.metadata_valid===false||s?.archive?.pointer_valid===false;
  $('native-policy-save').disabled=submitting||busy;
  $('native-auto').disabled=submitting||busy;
  $('native-policy-status').textContent=p.blocked?t('自動部署已暫停，請確認裝置後手動恢復。原因：','Automatic deployment is blocked. Check the device and resume manually. Reason: ')+(window.U30I18n?.translate(p.error||'')||''):p.enabled?t('自動納入新感測器已啟用；需連續兩次辨識一致。','Automatic sensor enrollment is enabled; two matching readings are required.'):t('自動納入新感測器已關閉。','Automatic sensor enrollment is disabled.');
 };
 $('native-auto').addEventListener('change',()=>dirty=true);
 async function send(action){
  if(!$('native-key').value){notify(t('請輸入 Pi 設定密碼。','Enter the Pi setup password.'));$('native-key').focus();return;}
  submitting=true;if(state)window.renderNativeControl(state);
  try{
   await api('/api/u30/control','POST',{action,serial:action==='resume'?pendingPlan?.serial:state?.snapshot?.serial,expected_deployment:pendingPlan?.deployment,expected_channels:pendingPlan?.channels,enabled:$('native-auto').checked,confirmed:true,setup_key:$('native-key').value});
   if(action==='policy')dirty=false;
   notify(action==='pause'?t('已排入暫停 U30 記錄；Pi 仍繼續採集。','U30 pause queued. Pi collection continues.'):action==='resume'?t('已排入完整備份與恢復記錄，請等待核對完成。','Backup and resume queued. Please wait for verification.'):t('自動部署設定已儲存。','Automatic enrollment setting saved.'));
   await tick();
  }catch(e){notify(window.U30I18n?.translate(e.message)||e.message);}finally{$('native-key').value='';submitting=false;if(state)window.renderNativeControl(state);}
 }
 $('native-pause').addEventListener('click',()=>send('pause'));
 $('native-policy-save').addEventListener('click',()=>send('policy'));
 $('native-resume').addEventListener('click',()=>{
  const s=state?.snapshot;if(!s)return;
  pendingPlan={serial:s.serial,deployment:s.archive.deployment,channels:s.sensors.map(c=>c.key).sort()};
  $('native-resume-summary').textContent=t(`U30 ${s.serial} · 目前 ${s.sensors.length} 個通道 · 每 ${state.interval_seconds} 秒 · ${s.archive.wrap_enabled?'循環覆寫':'滿額停止'}。先完整備份，再清除舊部署並立即開始；Pi 既有資料保留。`,`U30 ${s.serial} · ${s.sensors.length} channels · ${state.interval_seconds} s · ${s.archive.wrap_enabled?'Wrap':'Stop when full'}. A complete backup precedes clearing the old deployment and starting immediately. Existing Pi data is retained.`);
  $('native-resume-ack').checked=false;$('native-resume-error').hidden=true;$('native-dialog').showModal();
 });
 $('native-resume-cancel').addEventListener('click',()=>$('native-dialog').close());
 $('native-resume-confirm').addEventListener('submit',async e=>{
  e.preventDefault();
  if(pendingPlan?.serial!==state?.snapshot?.serial){notice('native-resume-error',t('裝置已變更，請重新預覽。','Device changed. Preview again.'));return;}
  if(!$('native-key').value){notice('native-resume-error',t('請先取消並輸入 Pi 設定密碼。','Cancel and enter the Pi setup password first.'));return;}
  $('native-dialog').close();await send('resume');
 });
 window.addEventListener('u30-language',()=>{if(state)window.renderNativeControl(state);});
 if(window.u30LastState)window.renderNativeControl(window.u30LastState);
})();
