/* A dedicated native room receives a bounded feed. Sources remain independent. */
(() => {
 'use strict';
 if(CONTENT_POPOUT)return;
 const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 const btn=(text,fn)=>{const n=make('button',text,'ghost');n.type='button';n.onclick=fn;return n;};
 let data=null,loading=false,capture=null;
 const entry=btn('',()=>void open());entry.id='btnConcierge';entry.className='navbtn';entry.title='Your assistant across conversations';
 const mark=make('span',undefined,'concierge-mark'),label=make('span',undefined,'concierge-label'),name=make('span','Assistant');
 label.append(name,make('small','Your assistant'));entry.append(mark,label);
 document.querySelector('#btnConsole').before(entry);
 const strip=make('div',undefined,'concierge-strip');strip.hidden=true;
 const status=make('span'),settings=btn('Watching & focus',()=>void configure()),talk=document.querySelector('#btnListen');
 // Reuse the composer microphone; other rooms keep ordinary dictation.
 talk.addEventListener('click',e=>{if(!here())return;e.preventDefault();e.stopImmediatePropagation();void speak();},true);
 function markTalk(){
  if(!here()){markListening(!!state.listening);return;}
  talk.hidden=!(window.SpeechRecognition||window.webkitSpeechRecognition);
  const label=capture?'Done — send voice message':'Talk to your assistant';
  talk.title=label;talk.setAttribute('aria-pressed',String(!!capture));
  const name=talk.querySelector('.sr-only');if(name)name.textContent=label;
 }
 const cancelTalk=btn('Cancel',()=>cancelCapture()),retryTalk=btn('Continue talking',()=>startRecognition(capture));
 cancelTalk.hidden=true;retryTalk.hidden=true;
 const heard=make('span','','concierge-heard');heard.hidden=true;heard.setAttribute('role','status');
 strip.append(status,settings,cancelTalk,retryTalk,heard);document.querySelector('#composer').before(strip);
 const dialog=make('dialog',undefined,'concierge-dialog');dialog.setAttribute('aria-label','Assistant settings');document.body.append(dialog);
 async function refresh(){
  if(loading)return;loading=true;
  try{data=await api('/api/concierge/view',{absolute:true});paint();}catch(e){/* Optional gateway capability; never change the current conversation. */}finally{loading=false;}
 }
 function here(){const t=data?.settings?.target;return t&&t.agent===agentId()&&t.room===state.room;}
 function paint(){
  name.textContent=data?.settings?.name||'Assistant';
  const owner=data?.settings?.target?.agent||DEFAULT_AGENT;
  if(mark.dataset.owner!==owner){mark.dataset.owner=owner;mark.replaceChildren(agentAvatar({id:owner,name:name.textContent}),useIcon('i-mic','concierge-voice-mark'));}
  entry.classList.toggle('active',!!here());
  strip.hidden=!here();if(capture&&!sameRoom(capture))cancelCapture();markTalk();
  if(here())status.textContent=data.running?(data.settings.sources.length?'Watching '+data.settings.sources.length+' rooms':'Ready for your requests'):'Updates paused';
  foldPackets();
 }
 function foldPackets(){
  if(!here())return;
  for(const row of document.querySelectorAll('#stream .msg.human:not([data-concierge-packet])')){
   const body=row.querySelector('.body');if(!body?.textContent.startsWith('CONCIERGE UPDATE PACKET'))continue;
   row.dataset.conciergePacket='true';const details=make('details'),summary=make('summary','Project updates · inspect sources');details.append(summary);body.before(details);details.append(body);
   const who=row.querySelector('.who');if(who)who.textContent='Project updates';
  }
 }
 new MutationObserver(foldPackets).observe(document.querySelector('#stream'),{childList:true,subtree:true});
 async function open(){await refresh();const t=data?.settings?.target;if(!t)return configure();await switchAgent(t.agent,t.room);paint();}
 async function action(body){data=await api('/api/concierge/action',{absolute:true,method:'POST',body:{...body,base_revision:data.settings.revision}});paint();return data;}
 async function configure(){
  await refresh();if(!data?.settings){flash('Your assistant needs the updated workspace service.');return;}
  const cfg=data.settings;dialog.replaceChildren();
  const heading=make('h2',cfg.name||'Your assistant'),close=btn('Close',()=>dialog.close());
  const rename=make('input');rename.value=cfg.name||'Assistant';rename.maxLength=40;rename.setAttribute('aria-label','Assistant name');
  const saveName=btn('Save name',async()=>{try{await action({action:'identity',name:rename.value});heading.textContent=data.settings.name;note.textContent='Name saved.';}catch(e){note.textContent=e.message;}});
  const focus=make('textarea');focus.value=cfg.focus;focus.maxLength=1200;focus.setAttribute('aria-label','Your focus');
  const explanation=make('p','Everyday conversation stays here. Your assistant can contact any available room when you ask. The selection below controls background updates only. Checking for updates uses no model; speaking a summary does.');
  const pace=make('select');pace.setAttribute('aria-label','Update pace');
  for(const [value,title] of [['quiet','Quiet · finished replies, at most every 5 minutes'],['live','Live · progress too, at most every minute']]){const option=make('option',title);option.value=value;pace.append(option);}pace.value=cfg.update_mode||'quiet';
  const choices=make('div',undefined,'concierge-sources');const boxes=[];
  const dst=cfg.target||{agent:agentId(),room:state.room};
  const tabs=[...(cfg.sources||[]),...state.tabs];const seen=new Set();
  for(const t of tabs){const id=t.agent+'/'+t.room;if(seen.has(id)||id===dst.agent+'/'+dst.room)continue;seen.add(id);
   const label=make('label'),check=make('input');check.type='checkbox';check.checked=cfg.sources.some(s=>s.agent===t.agent&&s.room===t.room);
   label.append(check,document.createTextNode(t.title||t.name||t.room));choices.append(label);boxes.push({check,t});
  }
  const note=make('p','','muted');note.setAttribute('role','status');
  const save=btn('Save rooms & focus',async()=>{try{await action({action:'configure',target:dst,sources:boxes.filter(x=>x.check.checked).map(x=>({agent:x.t.agent,room:x.t.room})),focus:focus.value});note.textContent='Saved. Start watching when ready.';}catch(e){note.textContent=e.message;}});
  const start=btn(cfg.enabled&&data.running?'Pause updates':'Start watching',async()=>{try{await action({action:data.running?'pause':'start'});dialog.close();}catch(e){note.textContent=e.message;}});
  const saveFocus=btn('Save focus',async()=>{try{await action({action:'focus',focus:focus.value,update_mode:pace.value});note.textContent='Focus saved.';}catch(e){note.textContent=e.message;}});
  dialog.append(heading,close,rename,saveName,make('p',cfg.target?'Assistant conversation: '+cfg.target.title:'Use a dedicated, empty conversation for your assistant. The current conversation will receive its updates.'),explanation,pace,focus,saveFocus,make('h3','Conversations to watch'),choices,save,start,note);
  if(data.handoffs?.length){dialog.append(make('h3','Requests to rooms'));for(const r of data.handoffs)dialog.append(make('p',r.destination.title+' · '+({sent:'Sent · waiting for a reply',answered:'Room replied',unknown:'Delivery needs checking',failed:'Not sent',checking:'Checking',sending:'Sending'}[r.state]||r.state)));}
  for(const source of data.sources||[])if(source.error)dialog.append(make('p',source.title+': '+source.error,'muted'));
  if(cfg.notice)dialog.append(make('p',cfg.notice));
  const limit=make('p','Desktop alpha · Up to 30 automatic project updates/hour. Ask your assistant to check connected email or calendars separately. Background email/calendar alerts need their own setup. Use the composer microphone: tap to speak, tap again to finish and send. Pauses and corrections stay together; Cancel sends nothing.','muted');dialog.append(limit);
  if(!dialog.open)dialog.showModal();
 }
 function sameRoom(c){return c&&c.agent===agentId()&&c.room===state.room&&c.seq===state.roomSeq;}
 function captureText(c){return [c.saved,c.final,c.interim].filter(Boolean).join(' ').trim();}
 function showHeard(c){heard.textContent=captureText(c)||'Listening. Tap the microphone again to finish and send.';}
 function resetCapture(c){
  if(capture!==c)return;
  clearTimeout(c.timer);capture=null;window.UX46VoiceAwake?.set('assistant-mic',false);window.dispatchEvent(new CustomEvent('ux46-dictation',{detail:{active:false}}));markTalk();cancelTalk.hidden=true;retryTalk.hidden=true;heard.hidden=true;
 }
 function cancelCapture(){
  const c=capture;if(!c)return;resetCapture(c);c.r?.abort();window.UX46ListenFeed?.resume();
 }
 async function finishCapture(c){
  if(capture!==c)return;
  const text=captureText(c),valid=sameRoom(c);resetCapture(c);
  if(!valid||!text){window.UX46ListenFeed?.resume();return;}
  const draft=document.querySelector('#draft');
  if(draft.value!==c.draftBefore){
   // Do not overwrite text typed while the microphone was open.
   heard.hidden=false;heard.textContent='Spoken text was not sent: '+text;
   window.UX46ListenFeed?.resume();flash('Your typed draft is still here. Spoken text is shown above it.');return;
  }
  draft.value=[c.draftBefore,text].filter(Boolean).join(' ').trim();scheduleDraftSave();
  // Start playback readiness in this click path, without letting slow audio
  // preparation prevent the human message from being sent.
  Promise.resolve(window.UX46ListenFeed?.ensure()).catch(()=>{});
  await send();
 }
 function startRecognition(c){
  if(capture!==c||!sameRoom(c)||c.done||c.r)return;
  clearTimeout(c.timer);retryTalk.hidden=true;c.failed=false;
  const R=window.SpeechRecognition||window.webkitSpeechRecognition,r=new R();c.r=r;
  c.final='';c.interim='';r.continuous=true;r.interimResults=true;r.lang=document.documentElement.lang||'en-US';
  r.onresult=e=>{
   if(capture!==c||c.r!==r)return;
   const final=[],interim=[];
   for(let i=0;i<e.results.length;i++)(e.results[i].isFinal?final:interim).push(e.results[i][0].transcript);
   c.final=final.join(' ');c.interim=interim.join(' ');c.restarts=0;showHeard(c);
  };
  r.onerror=e=>{
   if(capture!==c||c.r!==r)return;
   if(e.error!=='no-speech'){c.failed=true;heard.textContent='Microphone stopped. Your words are kept; tap the microphone to send, or Continue talking.';}
  };
  r.onend=()=>{
   if(capture!==c||c.r!==r)return;
   c.r=null;c.saved=captureText(c);c.final='';c.interim='';
   if(c.done){void finishCapture(c);return;}
   if(c.failed||++c.restarts>3){retryTalk.hidden=false;return;}
   // Some browsers end recognition on silence despite continuous=true.
   // Silence never sends. Restart a bounded number of times, then offer retry.
   c.timer=setTimeout(()=>startRecognition(c),250);
  };
  try{r.start();}catch(e){c.r=null;c.failed=true;retryTalk.hidden=false;heard.textContent='Microphone unavailable. Tap the microphone to send what was heard, or Cancel.';}
 }
 async function speak(){
  if(capture){
   const c=capture;if(c.done)return;c.done=true;clearTimeout(c.timer);
   if(c.r){c.timer=setTimeout(()=>{const r=c.r;void finishCapture(c);r?.abort();},1200);c.r.stop();}
   else await finishCapture(c);
   return;
  }
  if(!here())return;
  if(!(window.SpeechRecognition||window.webkitSpeechRecognition)){flash('Voice input is unavailable in this browser. Use Dictate or type here.');return;}
  stopDictation();
  const c={agent:agentId(),room:state.room,seq:state.roomSeq,draftBefore:document.querySelector('#draft').value,saved:'',final:'',interim:'',restarts:0};capture=c;
  window.UX46ListenFeed?.pause();window.UX46VoiceAwake?.set('assistant-mic',true);window.dispatchEvent(new CustomEvent('ux46-dictation',{detail:{active:true}}));markTalk();cancelTalk.hidden=false;heard.hidden=false;showHeard(c);startRecognition(c);
 }
 window.addEventListener('ux46-room',()=>{paint();void refresh();});
 window.UX46Concierge={refresh,open,configure};
 setInterval(()=>{if(here()||dialog.open)void refresh();},15000);void refresh();
})();
