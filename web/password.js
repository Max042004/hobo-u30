'use strict';
(() => {
 const t=(zh,en)=>window.U30I18n?.language()==='en'?en:zh;
 let pending=false,lastNetwork=null,lastJob='';
 window.renderPasswordNetwork=(n,force=false)=>{
  lastNetwork=n;
  $('password-submit').disabled=pending||!n.available||n.busy||['connecting','changing_password'].includes(n.job?.state);
  const job=JSON.stringify(n.job);
  if(n.job?.kind==='password'&&(force||job!==lastJob))notice('password-notice',window.U30I18n?.translate(n.job.message)||n.job.message);
  lastJob=job;
 };
 $('password-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const password=$('password-new').value;
  if(!/^[\x20-\x7e]{8,63}$/.test(password)){notice('password-notice',t('新密碼須為 8～63 個英文字母、數字或符號','Use 8–63 English letters, digits or symbols.'));return;}
  if(password!==$('password-confirm').value){notice('password-notice',t('兩次輸入的新密碼不一致','The new passwords do not match.'));$('password-confirm').focus();return;}
  pending=true;$('password-submit').disabled=true;
  try{
   await api('/api/network/password','POST',{setup_key:$('password-current').value,new_password:password,confirm_password:$('password-confirm').value,confirmed:$('password-ack').checked});
   ['password-current','password-new','password-confirm'].forEach(id=>$(id).value='');$('password-ack').checked=false;
   notice('password-notice',t('密碼更新已排入。請等待完成；使用 Pi 熱點時，需以新密碼重新連線。','Password update queued. Wait for completion; reconnect to the Pi hotspot with the new password.'));
   await loadNetwork();
  }catch(error){notice('password-notice',window.U30I18n?.translate(error.message)||error.message);}
  finally{['password-current','password-new','password-confirm'].forEach(id=>$(id).value='');$('password-ack').checked=false;pending=false;if(lastNetwork)window.renderPasswordNetwork(lastNetwork);else $('password-submit').disabled=false;}
 });
 window.addEventListener('u30-language',()=>{if(lastNetwork)window.renderPasswordNetwork(lastNetwork,true);});
 if(window.u30NetworkState)window.renderPasswordNetwork(window.u30NetworkState);
})();
