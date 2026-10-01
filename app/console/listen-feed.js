/* An opt-in reader for one fixed room. No prompts, shared workspace writes,
   or hidden background agent. Native history remains the source of speech. */
(() => {
  'use strict';
  if (CONTENT_POPOUT) return;
  const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const button=(label,fn)=>{const b=make('button',label,'ghost');b.type='button';b.onclick=fn;return b;};
  let feed=null,releaseLock=null,generation=0;
  const toggle=button('Listen',()=>feed&&feed.agent===agentId()&&feed.room===state.room?stop():void start());
  toggle.id='btnListenFeed';toggle.title='Read new replies aloud on this device';toggle.setAttribute('aria-pressed','false');
  toggle.prepend(replyIcon('listen'));document.querySelector('#btnModel').before(toggle);
  const bar=make('section',undefined,'listen-feed');bar.hidden=true;bar.setAttribute('aria-label','Live listening');
  const label=make('strong'),status=make('span','','listen-feed-status');status.setAttribute('role','status');
  const mute=button('Mute',()=>{if(!feed)return;feed.muted=!feed.muted;if(feed.muted)feed.audio.pause();else {clearSpeech();void pump(feed);}paint();});
  const retry=button('Play / retry',()=>{if(!feed)return;feed.error='';feed.muted=false;clearSpeech();void pump(feed);void poll();});retry.hidden=true;
  const skip=button('Skip response',()=>{if(!feed)return;const f=feed;f.audio.pause();f.audio.removeAttribute('src');f.audio.load();f.current=null;f.preparing=false;++f.speechGeneration;f.error='';void pump(f);paint();});skip.hidden=true;
  const volume=make('input');volume.type='range';volume.min='0';volume.max='1';volume.step='.05';volume.value='1';volume.setAttribute('aria-label','Listening volume');volume.oninput=()=>{if(feed)feed.audio.volume=Number(volume.value);};
  const words=make('details'),summary=make('summary','Response being read'),text=make('div','','listen-feed-text');words.append(summary,text);
  bar.append(label,mute,volume,retry,skip,button('Stop listening',stop),status,words);document.body.append(bar);
  function paint(){
    bar.hidden=!feed;toggle.disabled=!state.detail;
    const here=feed&&feed.agent===agentId()&&feed.room===state.room;
    toggle.textContent=here?'Listening':'Listen';toggle.prepend(replyIcon('listen'));toggle.setAttribute('aria-pressed',String(Boolean(here)));
    if(!feed)return;
    label.textContent='Listening · '+feed.title;mute.textContent=feed.muted?'Unmute':'Mute';mute.setAttribute('aria-pressed',String(feed.muted));
    status.textContent=feed.error||feed.connectionNote||((!feed.initialized?'Starting from the latest response…':feed.muted?'Muted':feed.preparing?'Preparing voice…':feed.current?'Reading':'Waiting for new replies')+(feed.queue.length?' · '+feed.queue.length+' waiting':'')+(feed.partial?' · '+feed.partial:''));
    retry.hidden=!feed.error;skip.hidden=!feed.current;text.textContent=feed.current?.text||feed.lastText||'New commentary and final answers will appear here. Tool activity is skipped.';
  }
  function stop(){
    ++generation;
    if(feed){feed.audio.pause();feed.audio.removeAttribute('src');feed.audio.load();feed.audio.remove();feed=null;}
    if(releaseLock){releaseLock();releaseLock=null;}paint();
  }
  async function start(){
    if(!state.detail)return;
    const target={agent:agentId(),room:state.room,title:state.detail.title||state.room,voice:state.voice.preferred};
    stop();const gen=generation;
    // One live reader per browser profile, including conversation satellites.
    if(!navigator.locks){flash('Live listening needs a browser with Web Locks support. One-off playback is still available.');return;}
    navigator.locks.request('ux46-live-listening',{ifAvailable:true},async lock=>{
      if(!lock){if(gen===generation)flash('Another UX46 window is listening on this browser. Stop it there first.');return;}
      if(gen!==generation)return;
      const held=new Promise(resolve=>{releaseLock=resolve;});
      clearSpeech();
      const audio=make('audio');audio.hidden=true;audio.preload='auto';audio.volume=Number(volume.value);document.body.append(audio);
      const f={...target,audio,seen:new Set(),queue:[],current:null,anchor:null,initialized:false,polling:false,preparing:false,muted:false,error:'',partial:'',speechGeneration:0};feed=f;
      audio.addEventListener('ended',()=>{if(feed!==f)return;f.lastText=f.current?.text||'';f.current=null;f.error='';void pump(f);paint();});
      audio.addEventListener('error',()=>{if(feed===f&&f.current){f.error='Audio could not load. Play / retry or skip this response.';paint();}});
      paint();await poll();await held;
    }).catch(e=>flash('Could not start listening: '+e.message));
  }
  async function pump(f){
    if(feed!==f||f.muted||f.preparing||f.error)return;
    if(!f.current){f.current=f.queue.shift();f.partial='';}
    if(!f.current){paint();return;}
    if(!f.current.url){
      f.preparing=true;const seq=++f.speechGeneration;paint();
      try{
        const speech=await api(agentPath(f.agent,'/api/room/'+encodeURI(f.room)+'/speak'),{absolute:true,method:'POST',body:{item_id:f.current.id,voice:f.voice||undefined}});
        if(feed!==f||seq!==f.speechGeneration)return;
        if(!/^\/api\/audio\/[a-zA-Z0-9_-]+\.wav$/.test(speech.audio_url||''))throw Error('Invalid audio response');
        f.current.url=agentPath(f.agent,speech.audio_url);f.audio.src=f.current.url;
        f.partial=speech.complete===false?'Partial reading: '+speech.spoken_chars+' of '+speech.total_chars+' characters':'';
      }catch(e){if(feed===f&&seq===f.speechGeneration){f.error='Could not prepare this response. '+e.message;paint();}return;}
      finally{if(feed===f&&seq===f.speechGeneration)f.preparing=false;}
    }
    if(feed!==f||f.muted||f.error)return;
    try{await f.audio.play();}catch(e){if(feed===f)f.error='Tap Play / retry to allow audio on this device.';}
    paint();
  }
  async function poll(){
    const f=feed;if(!f||f.polling||f.queue.length>=100)return;f.polling=true;
    try{
      const path=agentPath(f.agent,'/api/room/'+encodeURI(f.room));
      const room=await api(path,{absolute:true});if(feed!==f)return;if(room.id!==f.room)throw Error('Conversation could not be checked');
      let rows=[],cursor=null,page;
      for(let count=0;count<10;count++){
        page=await api(path+'/history?limit=40&direction=desc'+(cursor?'&cursor='+encodeURIComponent(cursor):''),{absolute:true});
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
      if(!f.error)void pump(f);paint();
    }catch(e){if(feed===f){f.connectionNote='Waiting for connection · '+e.message;paint();}}
    finally{f.polling=false;}
  }
  // Manual one-off playback pauses the feed so two voices never compete.
  window.UX46ListenFeed={poll,stop,pause(){if(feed){feed.muted=true;feed.audio.pause();paint();}}};
  window.addEventListener('ux46-room',paint);
  window.addEventListener('pagehide',stop);
  setInterval(()=>{paint();void poll();},5000);
  paint();
})();
