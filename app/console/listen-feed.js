/* An opt-in reader for one fixed room. No prompts, shared workspace writes,
   or hidden background agent. Native history remains the source of speech. */
(() => {
  'use strict';
  if (CONTENT_POPOUT) return;
  const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const button=(label,fn)=>{const b=make('button',label,'ghost');b.type='button';b.onclick=fn;return b;};
  const satellite=typeof LISTEN_WINDOW!=='undefined' && LISTEN_WINDOW;
  let feed=null,releaseLock=null,generation=0,popup=null,handoff='',detached=false,returning=false,detachedTarget=null;
  const phone=window.matchMedia('(max-width:899px)');
  let controlsOpen=false;
  const parent=satellite?window.opener:null;
  const channel=new URLSearchParams(location.search).get('handoff');
  const toggle=button('Listen',()=>{if(detached&&popup&&!popup.closed){popup.focus();return;}if(feed&&phone.matches){showControls(!controlsOpen);return;}if(feed&&feed.agent===agentId()&&feed.room===state.room){words.open=!words.open;return;}void start();});
  toggle.id='btnListenFeed';toggle.title='Read new replies aloud on this device';toggle.setAttribute('aria-pressed','false');
  toggle.prepend(replyIcon('listen'));
  const dock=make('div',undefined,'listen-dock');document.querySelector('#btnModel').before(dock);dock.append(toggle);
  const bar=make('section',undefined,'listen-feed');bar.hidden=true;bar.setAttribute('aria-label','Live listening');
  bar.id='listenFeedControls';
  toggle.setAttribute('aria-controls',bar.id);
  toggle.setAttribute('aria-expanded','false');
  const close=button('×',()=>{showControls(false);toggle.focus();});
  close.classList.add('listen-close');close.setAttribute('aria-label','Hide listening controls');
  function showControls(open){controlsOpen=open;bar.classList.toggle('controls-open',open);toggle.setAttribute('aria-expanded',String(open));}
  document.addEventListener('pointerdown',event=>{if(phone.matches&&controlsOpen&&!dock.contains(event.target))showControls(false);});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&controlsOpen){showControls(false);toggle.focus();}});
  phone.addEventListener('change',()=>showControls(false));
  const label=make('strong'),status=make('span','','listen-feed-status');status.setAttribute('role','status');
  const mute=button('Mute',()=>{if(!feed)return;feed.muted=!feed.muted;if(feed.muted)feed.audio.pause();else {clearSpeech();void pump(feed);}paint();});
  const retry=button('Play / retry',()=>{if(!feed)return;feed.error='';feed.retries=0;feed.muted=false;clearSpeech();void pump(feed);void poll();});retry.hidden=true;
  const skip=button('Skip response',()=>{if(!feed)return;const f=feed;cancelReading(f);f.audio.pause();f.audio.removeAttribute('src');f.audio.load();f.current=null;f.error='';void pump(f);paint();});skip.hidden=true;
  if(!satellite){for(const [control,name,glyph] of [[retry,'Play / retry','▶'],[skip,'Skip response','⏭']]){control.textContent=glyph;control.setAttribute('aria-label',name);control.title=name;}}
  const volume=make('input');volume.type='range';volume.min='0';volume.max='1';volume.step='.05';volume.value='1';volume.setAttribute('aria-label','Listening volume');volume.oninput=()=>{if(feed)feed.audio.volume=Number(volume.value);};
  const words=make('details'),summary=make('summary','Response being read'),text=make('div','','listen-feed-text');words.append(summary,text);
  const pop=button(satellite?'Dock back':'↗',()=>satellite?dockBack():popOut());
  pop.classList.add('listen-popout');
  pop.title=pop.getAttribute('aria-label')|| (satellite?'Dock back':'Pop out listening');pop.setAttribute('aria-label',satellite?'Dock back':'Pop out listening');
  const end=button('■',()=>{stop();if(satellite)window.close();});end.title='Stop listening';end.setAttribute('aria-label','Stop listening');
  const live=button('Resume live',()=>{if(feed)resumeLive(feed);});live.hidden=true;
  bar.append(label,close,mute,volume,retry,skip,end,pop,live,status,words);dock.append(bar);
  if(satellite){words.open=true;document.body.append(bar);dock.hidden=true;}
  function snapshot(){
    if(!feed)return null;
    const {agent,room,title,voice,queue,current,anchor,initialized,muted,error,partial,lastText}=feed;
    return {localAgent:DEFAULT_AGENT,agent,room,title,voice,queue,current,anchor,initialized,muted,error,partial,lastText,replay:feed.replay?{...feed.replay,loading:false}:null,seen:[...feed.seen],volume:feed.audio.volume,position:feed.audio.currentTime};
  }
  function popOut(){
    if(!feed)return;
    if(popup&&!popup.closed){popup.focus();return;}
    handoff=crypto.randomUUID();
    const url=new URL('/listen.html',location.origin);url.searchParams.set('handoff',handoff);
    popup=window.open(url.toString(),'_blank','popup,width=560,height=460');
    if(!popup){flash('Allow pop-ups for UX46 to open the listening window. Playback is still here.');return;}
    // Keep playing here until the independent window has loaded its own CSRF.
  }
  function dockBack(){
    if(!parent||parent.closed){flash('The original workspace is closed. This window can keep listening.');return;}
    const saved=snapshot();if(!saved)return;
    returning=true;stop(true);parent.postMessage({type:'ux46-listen-return',channel,saved},location.origin);
    window.close();
  }
  window.addEventListener('message',event=>{
    if(event.origin!==location.origin)return;
    const message=event.data||{};
    if(!satellite&&event.source===popup&&message.channel===handoff){
      if(message.type==='ux46-listen-ready'&&feed){
        const saved=snapshot();stop(true);detached=true;detachedTarget={agent:saved.agent,room:saved.room};paint();
        popup.postMessage({type:'ux46-listen-transfer',channel:handoff,saved},location.origin);
      } else if(message.type==='ux46-listen-return'&&message.saved){
        detached=false;popup=null;void start(message.saved);
      } else if(message.type==='ux46-listen-stopped'){detached=false;popup=null;paint();}
    } else if(satellite&&event.source===parent&&message.channel===channel&&message.type==='ux46-listen-replay'){
      if(feed&&message.agent===feed.agent&&message.room===feed.room)void replayFrom(feed,message.itemId,message.offset);
    } else if(satellite&&event.source===parent&&message.channel===channel&&['ux46-listen-pause','ux46-listen-resume'].includes(message.type)){
      if(message.type==='ux46-listen-resume')window.UX46ListenFeed.resume();else window.UX46ListenFeed.pause();
    } else if(satellite&&event.source===parent&&message.channel===channel&&message.type==='ux46-listen-transfer'){
      DEFAULT_AGENT=message.saved.localAgent||'local';
      document.querySelector('#listenWindowNotice').textContent='';
      document.title='Listening · '+message.saved.title;void start(message.saved);
    }
  });
  function paint(){
    document.documentElement.classList.toggle('listen-word-mode',Boolean(feed||detached));
    bar.hidden=!feed;toggle.disabled=!state.detail&&!detached;
    if(detached&&popup?.closed){detached=false;popup=null;}
    pop.disabled=Boolean(feed?.replay?.loading)||(satellite&&(!parent||parent.closed));
    const here=feed&&feed.agent===agentId()&&feed.room===state.room;
    toggle.textContent=detached?'Listening ↗':feed&&phone.matches?(feed.error?'Listening !':'Listening'):here?'Listening':'Listen';toggle.prepend(replyIcon('listen'));toggle.setAttribute('aria-pressed',String(Boolean(here||detached||feed&&phone.matches)));
    if(!feed)return;
    label.textContent=(feed.replay?'Replaying · ':'Listening · ')+feed.title;mute.textContent=feed.muted?'Unmute':'Mute';mute.setAttribute('aria-pressed',String(feed.muted));
    live.hidden=!feed.replay;
    status.textContent=feed.error||feed.connectionNote||((feed.replay?.loading?'Loading earlier replies…':!feed.initialized?'Starting from the latest response…':feed.muted?'Muted':feed.preparing?'Preparing voice…':feed.current?(feed.replay?'Replaying':'Reading'):'Waiting for new replies')+(feed.queue.length?' · '+feed.queue.length+(feed.replay?' live waiting':' waiting'):'')+(feed.partial?' · '+feed.partial:''));
    retry.hidden=!feed.error||Boolean(feed.replay?.failed);skip.hidden=!feed.current;text.textContent=feed.current?.text||feed.lastText||'New commentary and final answers will appear here. Tool activity is skipped.';
    toggle.title=feed.title+' · '+status.textContent+' · Double-click a reply word to read from there';
  }
  function stop(transferring=false){
    ++generation;
    showControls(false);
    if(satellite&&!transferring&&parent&&!parent.closed)parent.postMessage({type:"ux46-listen-stopped",channel},location.origin);
    if(feed){cancelReading(feed);feed.historyRequest?.abort();feed.replayRequest?.abort();feed.audio.pause();feed.audio.removeAttribute('src');feed.audio.load();feed.audio.remove();feed=null;}
    if(releaseLock){releaseLock();releaseLock=null;}paint();
  }
  async function start(saved=null){
    if(!saved&&!state.detail)return;
    const target=saved?{agent:saved.agent,room:saved.room,title:saved.title,voice:saved.voice}:{agent:agentId(),room:state.room,title:state.detail.title||state.room,voice:state.voice.preferred};
    stop(true);const gen=generation;
    // One live reader per browser profile, including conversation satellites.
    if(!navigator.locks){flash('Live listening needs a browser with Web Locks support. One-off playback is still available.');return;}
    navigator.locks.request('ux46-live-listening',{ifAvailable:true},async lock=>{
      if(!lock){if(gen===generation)flash('Another UX46 window is listening on this browser. Stop it there first.');return;}
      if(gen!==generation)return;
      const held=new Promise(resolve=>{releaseLock=resolve;});
      clearSpeech();
      const audio=make('audio');audio.hidden=true;audio.preload='auto';audio.volume=Number(volume.value);document.body.append(audio);
      const f={...target,audio,seen:new Set(),queue:[],current:null,anchor:null,initialized:false,polling:false,preparing:false,muted:false,error:'',partial:'',speechGeneration:0,lastProgress:Date.now(),position:0};feed=f;
      if(saved){
        Object.assign(f,{queue:saved.queue,current:saved.current,anchor:saved.anchor,initialized:saved.initialized,muted:saved.muted,error:saved.error,partial:saved.partial,lastText:saved.lastText,replay:saved.replay||null,seen:new Set(saved.seen)});
        audio.volume=saved.volume;volume.value=String(saved.volume);
        if(f.current?.url){
          audio.src=f.current.url;
          audio.addEventListener('loadedmetadata',()=>{if(feed===f&&Number.isFinite(saved.position))audio.currentTime=Math.min(saved.position,Number.isFinite(audio.duration)?audio.duration:saved.position);},{once:true});
        }
      }
      audio.addEventListener('ended',()=>{if(feed!==f)return;cancelReading(f);f.lastText=f.current?.text||'';
        if(f.replay&&f.current){f.seen.add(f.current.id);f.queue=f.queue.filter(item=>item.id!==f.current.id);if(f.replay.liveCurrent?.item?.id===f.current.id)f.replay.liveCurrent=null;}
        f.current=null;f.error='';void pump(f);paint();});
      audio.addEventListener('timeupdate',()=>{if(feed===f&&audio.currentTime!==f.position){f.position=audio.currentTime;f.lastProgress=Date.now();}});
      audio.addEventListener('error',()=>{if(feed===f&&f.current&&!f.error)recover(f,'Audio could not load.');});
      paint();if(saved)void pump(f);await poll();await held;
    }).catch(e=>flash('Could not start listening: '+e.message));
  }
  function cancelReading(f){
    ++f.speechGeneration;f.speechRequest?.abort();f.preparing=false;f.starting=false;
  }
  // Retry brief interruptions, but never loop indefinitely or silently discard
  // a response. Late failures from a skipped/stopped clip cannot block the next.
  function recover(f,message,automatic=true){
    cancelReading(f);f.audio.pause();f.reload=Boolean(f.current?.url);
    f.retries=(f.retries||0)+1;
    f.retryAt=automatic&&f.retries<=2?Date.now()+(f.retries===1?2000:8000):0;
    f.error=message+(f.retryAt?' Retrying automatically…':' Play / retry or skip this response.');paint();
  }
  async function request(f,key,path,options,timeout){
    const controller=new AbortController();f[key]=controller;
    const timer=setTimeout(()=>controller.abort(),timeout);
    try{return await api(path,{...options,signal:controller.signal});}
    finally{clearTimeout(timer);if(f[key]===controller)f[key]=null;}
  }
  async function pump(f){
    if(feed!==f||f.muted||f.preparing||f.replay?.loading||f.replay?.failed)return;
    if(f.error){if(!f.retryAt||Date.now()<f.retryAt)return;f.error='';f.retryAt=0;}
    if(!f.current){f.current=(f.replay?f.replay.queue:f.queue).shift();f.partial='';f.retries=0;f.reload=false;f.position=0;f.lastProgress=Date.now();}
    if(!f.current&&f.replay){resumeLive(f);return;}
    if(!f.current){paint();return;}
    if(f.starting||f.current.url&&!f.audio.paused){
      if(Date.now()-f.lastProgress>15000)recover(f,'Audio stopped progressing.');
      return;
    }
    const current=f.current,seq=++f.speechGeneration;
    if(!f.current.url){
      f.preparing=true;paint();
      try{
        const speech=await request(f,'speechRequest',agentPath(f.agent,'/api/room/'+String(f.room).split('/').map(encodeURIComponent).join('/')+'/speak'),{absolute:true,method:'POST',body:{item_id:current.id,voice:f.voice||undefined,start_offset:current.startOffset}},120000);
        if(feed!==f||seq!==f.speechGeneration)return;
        if(!/^\/api\/audio\/[a-zA-Z0-9_-]+\.wav$/.test(speech.audio_url||''))throw Error('Invalid audio response');
        f.current.url=agentPath(f.agent,speech.audio_url);f.audio.src=f.current.url;
        f.partial=speech.complete===false?'Partial reading: '+speech.spoken_chars+' of '+speech.total_chars+' characters':'';
      }catch(e){if(feed===f&&seq===f.speechGeneration)recover(f,'Could not prepare this response. '+(e.name==='AbortError'?'Voice preparation timed out.':e.message),!e.status||e.status>=500||e.status===408||e.status===429||e.code==='item_unknown');return;}
      finally{if(feed===f&&seq===f.speechGeneration)f.preparing=false;}
    }
    if(feed!==f||f.muted||f.error)return;
    if(f.reload){
      f.reload=false;const position=f.position||0;
      f.audio.addEventListener('loadedmetadata',()=>{if(feed===f&&f.current===current&&seq===f.speechGeneration)f.audio.currentTime=Math.min(position,Number.isFinite(f.audio.duration)?f.audio.duration:position);},{once:true});
      f.audio.src=current.url;f.audio.load();
    }
    f.starting=true;f.lastProgress=Date.now();
    try{await f.audio.play();}
    catch(e){if(feed===f&&seq===f.speechGeneration&&!f.muted){
      if(e.name==='NotAllowedError'){f.error='Tap Play / retry to allow audio on this device.';f.retryAt=0;}
      else recover(f,'Audio was interrupted.');
    }}finally{if(seq===f.speechGeneration)f.starting=false;}
    paint();
  }
  function resumeLive(f){
    if(feed!==f||!f.replay)return;
    const saved=f.replay.liveCurrent;f.replayRequest?.abort();cancelReading(f);f.audio.pause();
    f.replay=null;f.current=saved?.item||null;f.position=saved?.position||0;f.reload=Boolean(f.current?.url);
    f.error='';f.partial='';f.retries=0;f.muted=false;void pump(f);paint();
  }
  const readable=item=>item.type==='agentMessage'&&(!item.phase||['commentary','final_answer'].includes(item.phase))&&typeof item.text==='string'&&item.text.trim();
  async function replayFrom(f,itemId,offset){
    if(feed!==f||!Number.isInteger(offset)||offset<0)return;
    const saved=f.replay?f.replay.liveCurrent:(f.current?{item:f.current,position:f.audio.currentTime}:null);
    f.replayRequest?.abort();cancelReading(f);f.audio.pause();
    const replay={liveCurrent:saved,queue:[],loading:true};f.replay=replay;f.current=null;f.error='';f.muted=false;paint();
    const path=agentPath(f.agent,'/api/room/'+String(f.room).split('/').map(encodeURIComponent).join('/'));
    try{
      const room=await request(f,'replayRequest',path,{absolute:true},15000);
      if(feed!==f||f.replay!==replay)return;
      if(room.id!==f.room)throw Error('This conversation could not be checked.');
      let rows=[],cursor=null,found=false;const ids=new Set();
      for(let count=0;count<20;count++){
        const page=await request(f,'replayRequest',path+'/history?limit=200&direction=desc'+(cursor?'&cursor='+encodeURIComponent(cursor):''),{absolute:true},15000);
        if(feed!==f||f.replay!==replay)return;
        if(page.unavailable||!Array.isArray(page.items))throw Error('History could not be checked.');
        for(const item of page.items){if(item.id&&!ids.has(item.id)){ids.add(item.id);rows.push(item);}if(item.id===itemId){found=true;break;}}
        if(found||page.complete||!page.next_cursor||page.next_cursor===cursor)break;
        cursor=page.next_cursor;
      }
      if(!found)throw Error('That reply is outside the available replay history.');
      const newest=rows[0];
      replay.queue=rows.reverse().filter(item=>readable(item)&&!(item===newest&&commandTurnRunning(room)&&(!item.turn_id||!room.native?.active_turn||item.turn_id===room.native.active_turn))).map(item=>({id:item.id,text:item.text}));
      const first=replay.queue[0];
      if(!first||first.id!==itemId)throw Error('Wait until this reply finishes, then choose a word.');
      if(offset>=Array.from(first.text.replace(/\r\n?/g,'\n')).length)throw Error('The reply changed; choose the word again.');
      first.startOffset=offset;first.text=Array.from(first.text.replace(/\r\n?/g,'\n')).slice(offset).join('');
      replay.loading=false;void pump(f);paint();
    }catch(error){if(feed===f&&f.replay===replay){replay.loading=false;replay.failed=true;f.error=error.message+' Choose another word or Resume live.';paint();}}
  }
  // Match rendered text back to the existing native Markdown in reading order.
  // Presentation controls are excluded. Never invent text or guess an offset
  // when the displayed fragment cannot be matched to its source.
  function wordPosition(body,node,offset,source){
    if(node?.nodeType!==Node.TEXT_NODE||!body.contains(node))return null;
    const skip='.reply-actions,.mdcode-head,.mdviz,button,summary';
    const walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);let cursor=0,part;
    const normalized=source.replace(/\r\n?/g,'\n');
    while((part=walker.nextNode())){
      if(part.parentElement.closest(skip)||!part.data)continue;
      const at=normalized.indexOf(part.data,cursor);
      if(at<0)return null;
      if(part===node){
        let start=Math.min(offset,part.length-1);
        const word=/[\p{L}\p{N}_'’\-]/u;
        if(!word.test(part.data[start]||'')&&start>0&&word.test(part.data[start-1]))start--;
        while(start>0&&word.test(part.data[start-1]))start--;
        if(!word.test(part.data[start]||''))return null;
        return Array.from(normalized.slice(0,at+start)).length;
      }
      cursor=at+part.length;
    }
    return null;
  }
  function readAtPoint(event){
    const target=feed||detachedTarget;
    if((!feed&&!detached)||!target||target.agent!==agentId()||target.room!==state.room)return;
    const body=event.target.closest?.('.msg.agent > .body');
    if(!body||event.target.closest('button,a,summary,.reply-actions,.mdcode-head,.mdviz'))return;
    const item=state.items.find(item=>item.id===body.closest('[data-mid]').dataset.mid);
    if(!item||!readable(item))return;
    const caret=document.caretPositionFromPoint?.(event.clientX,event.clientY);
    const range=caret?null:document.caretRangeFromPoint?.(event.clientX,event.clientY);
    const offset=wordPosition(body,caret?.offsetNode||range?.startContainer,caret?.offset??range?.startOffset,item.text);
    if(offset===null){flash('Could not match that word. Try a word in the reply’s text.');return;}
    event.preventDefault();clearSpeech();
    if(feed)void replayFrom(feed,item.id,offset);
    else if(popup&&!popup.closed)popup.postMessage({type:'ux46-listen-replay',channel:handoff,agent:target.agent,room:target.room,itemId:item.id,offset},location.origin);
  }
  let touch=null,lastTouchRead=0;
  document.addEventListener('dblclick',event=>{if(Date.now()-lastTouchRead>700)readAtPoint(event);});
  document.addEventListener('pointerup',event=>{
    if(event.pointerType!=='touch')return;
    const now=Date.now();
    if(touch&&now-touch.at<400&&Math.hypot(event.clientX-touch.x,event.clientY-touch.y)<20){lastTouchRead=now;touch=null;readAtPoint(event);}
    else touch={at:now,x:event.clientX,y:event.clientY};
  });
  async function poll(){
    const f=feed;if(!f||f.polling||f.queue.length>=100)return;f.polling=true;
    try{
      const path=agentPath(f.agent,'/api/room/'+String(f.room).split('/').map(encodeURIComponent).join('/'));
      const room=await request(f,'historyRequest',path,{absolute:true},15000);if(feed!==f)return;if(room.id!==f.room)throw Error('Conversation could not be checked');
      let rows=[],cursor=null,page;
      for(let count=0;count<10;count++){
        page=await request(f,'historyRequest',path+'/history?limit=40&direction=desc'+(cursor?'&cursor='+encodeURIComponent(cursor):''),{absolute:true},15000);
        if(feed!==f)return;
        if(page.unavailable||!Array.isArray(page.items))throw Error('History could not be checked');
        rows.push(...page.items);
        if(!f.initialized||!f.anchor||rows.some(i=>i.id===f.anchor)||page.complete)break;
        if(!page.next_cursor||page.next_cursor===cursor||count===9)throw Error('Listening fell behind. Stop and start to listen from now.');
        cursor=page.next_cursor;
      }
      f.connectionNote='';
      const newest=rows[0]?.id||f.anchor;
      if(!f.initialized){
        // Existing completed messages are a baseline, not an audible backlog.
        // Keep an in-progress newest message eligible once it finishes.
        for(let i=0;i<rows.length;i++)if(i>0||!commandTurnRunning(room))f.seen.add(rows[i].id);
        f.initialized=true;f.anchor=newest;paint();return;
      }
      const selected=rows.slice().reverse();
      for(let i=0;i<selected.length;i++){
        const item=selected[i];if(!item.id||f.seen.has(item.id))continue;
        const eligible=item.type==='agentMessage'&&(!item.phase||['commentary','final_answer'].includes(item.phase))&&typeof item.text==='string'&&item.text.trim();
        if(!eligible){f.seen.add(item.id);continue;}
        // A later native item or a finished turn establishes completion. Never
        // guess completion from a pause in token streaming and lose the suffix.
        if(i===selected.length-1&&commandTurnRunning(room)&&(!item.turn_id||!room.native?.active_turn||item.turn_id===room.native.active_turn))continue;
        if(f.queue.length>=100)throw Error('Listening has 100 replies waiting. Stop and start to catch up to now.');
        f.seen.add(item.id);f.queue.push({id:item.id,text:item.text});
      }
      f.anchor=newest;
      void pump(f);paint();
    }catch(e){if(feed===f){f.connectionNote='Waiting for connection · '+e.message;paint();}}
    finally{f.polling=false;}
  }
  // Manual one-off playback pauses the feed so two voices never compete.
  window.UX46ListenFeed={poll,stop,start, ensure(){if(feed&&feed.agent===agentId()&&feed.room===state.room||detached){this.resume();return;}return start();}, resume(){if(feed){feed.muted=false;feed.error='';void pump(feed);paint();}else if(detached&&popup&&!popup.closed)popup.postMessage({type:'ux46-listen-resume',channel:handoff},location.origin);}, pause(){if(feed){feed.muted=true;feed.audio.pause();paint();}else if(detached&&popup&&!popup.closed)popup.postMessage({type:'ux46-listen-pause',channel:handoff},location.origin);}};
  window.addEventListener('ux46-room',paint);
  window.addEventListener('pagehide',()=>{
    if(satellite&&!returning&&feed&&parent&&!parent.closed){
      const saved=snapshot();stop(true);parent.postMessage({type:'ux46-listen-return',channel,saved},location.origin);
    } else stop(true);
  });
  if(satellite){
    (async()=>{
      try{
        if(!parent||parent.closed||!channel)throw Error('Open this player using Pop out listening in UX46.');
        const boot=await api('/api/bootstrap');state.csrf=boot.csrf;
        DEFAULT_AGENT=boot.local_agent?.id||boot.local_agent||boot.agent?.id||'local';
        parent.postMessage({type:'ux46-listen-ready',channel},location.origin);
        setTimeout(()=>{if(!feed)flash('No listening feed is attached. Open Pop out listening in UX46.');},10000);
      }catch(error){flash(error.message);}
    })();
  }
  // Audio recovery is independent of history availability. A slow history read
  // must not prevent already queued responses from continuing.
  function tick(){if(feed)void pump(feed);paint();void poll();}
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')tick();});
  setInterval(tick,5000);
  paint();
})();
