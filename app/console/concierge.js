/* A dedicated native room receives a bounded feed. Sources remain independent. */
(() => {
 'use strict';
 if(CONTENT_POPOUT)return;
 const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 const btn=(text,fn)=>{const n=make('button',text,'ghost');n.type='button';n.onclick=fn;return n;};
 let data=null,loading=false,recognition=null;
 const entry=btn('',()=>void open());entry.id='btnConcierge';entry.className='navbtn';entry.title='Your assistant across conversations';
 const mark=make('span',undefined,'concierge-mark'),label=make('span',undefined,'concierge-label'),name=make('span','Assistant');
 label.append(name,make('small','Your assistant'));entry.append(mark,label);
 document.querySelector('#btnConsole').before(entry);
 const strip=make('div',undefined,'concierge-strip');strip.hidden=true;
 const status=make('span'),settings=btn('Watching & focus',()=>void configure()),talk=btn('Talk',()=>void speak());
 strip.append(status,settings,talk);document.querySelector('#composer').before(strip);
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
  strip.hidden=!here();if(!here()&&recognition){recognition.abort();recognition=null;}
  if(here())status.textContent=data.running?'Watching '+data.settings.sources.length+' rooms':'Updates paused';
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
  const explanation=make('p','Everyday conversation stays here. Ask explicitly when you want another room to do work. Checking for updates uses no model; speaking a summary does.');
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
  const limit=make('p','Desktop alpha · Up to 30 automatic project updates/hour. Ask your assistant to check connected email or calendars separately. Background email/calendar alerts need their own setup. Talk sends one utterance; ordinary Dictate lets you edit before sending.','muted');dialog.append(limit);
  if(!dialog.open)dialog.showModal();
 }
 async function speak(){
  if(recognition){recognition.stop();return;}
  if(!here())return;
  const R=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!R){flash('Voice input is unavailable in this browser. Use Dictate or type here.');return;}
  const draft=document.querySelector('#draft');if(draft.value.trim()){flash('Send or clear your draft before using Talk.');return;}
  const token={agent:agentId(),room:state.room,seq:state.roomSeq};let text='',failed=false;
  const r=new R();recognition=r;r.continuous=false;r.interimResults=false;r.lang=document.documentElement.lang||'en-US';
  window.UX46ListenFeed?.pause();talk.textContent='Stop talking';
  r.onresult=e=>{for(let i=e.resultIndex;i<e.results.length;i++)if(e.results[i].isFinal)text+=e.results[i][0].transcript+' ';};
  r.onerror=()=>{failed=true;flash('Could not hear that. Try Talk again or use Dictate.');};
  r.onend=async()=>{
   recognition=null;talk.textContent='Talk';
   if(token.agent!==agentId()||token.room!==state.room||token.seq!==state.roomSeq)return;
   if(!failed&&text.trim()){
    if(draft.value.trim()){flash('Your draft changed. Spoken text was not sent.');return;}
    draft.value=text.trim();scheduleDraftSave();
    await window.UX46ListenFeed?.ensure();await send();
   }else window.UX46ListenFeed?.resume();
  };
  try{r.start();}catch(e){recognition=null;talk.textContent='Talk';window.UX46ListenFeed?.resume();flash('Microphone unavailable. Use Dictate or type here.');}
 }
 window.addEventListener('ux46-room',()=>{paint();void refresh();});
 window.UX46Concierge={refresh,open,configure};
 setInterval(()=>{if(here()||dialog.open)void refresh();},15000);void refresh();
})();
