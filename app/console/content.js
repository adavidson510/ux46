/* Saved content is room-scoped; a popout is a view, never a native session. */
(() => {
  'use strict';
  const n=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
  const btn=(text,fn)=>{const b=n('button',text,'ghost');b.type='button';b.onclick=()=>Promise.resolve(fn()).catch(e=>notice(e.message));return b;};
  const key=c=>[c.agent,c.room,c.id||''].join('|');
  const scope=()=>({agent:agentId(),room:state.room});
  const pop=CONTENT_POPOUT;
  let host,menu,body,note,current=null,serial=0,listing='',loading=false,lastList=0;
  let windowId;
  try{windowId=sessionStorage.getItem('ux46.content.window')||crypto.randomUUID();sessionStorage.setItem('ux46.content.window',windowId);}catch(e){windowId=crypto.randomUUID();}
  const draftKey=c=>'ux46.content.draft.'+windowId+'.'+key(c);
  const notice=text=>{if(note)note.textContent=text;};
  async function call(path,args,write=false){
    const url='/api/content/'+path+(write?'':'?'+new URLSearchParams(args));
    const r=await fetch(url,{method:write?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:write?{'Content-Type':'application/json','X-Atlas-CSRF':state.csrf}:{},body:write?JSON.stringify(args):undefined});
    const data=await r.json();if(!r.ok){const e=Error(data.message||'Content unavailable');e.status=r.status;throw e;}return data;
  }
  function saveLocal(){
    if(!current?.editor)return true;
    try{if(current.dirty)localStorage.setItem(draftKey(current),JSON.stringify({title:current.title.value,text:current.editor.value,revision:current.revision}));else localStorage.removeItem(draftKey(current));return true;}
    catch(e){notice('Unsaved · browser recovery unavailable. Save or download your text before leaving.');return false;}
  }
  function download(text,title){const u=URL.createObjectURL(new Blob([text],{type:'text/markdown'}));const a=n('a');a.href=u;a.download=title+'.md';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
  function popUrl(c){const u=new URL(location.origin+'/');u.search=new URLSearchParams({content:c.id,agent:c.agent,room:c.room});return u;}
  function popout(c){
    // noopener may return null even when successful: always provide a regular
    // link fallback, and never repeatedly retry or reuse another editor window.
    window.open(popUrl(c),'_blank','popup,width=1050,height=850,noopener');
    notice('Opened a content window. If your browser blocked it, use Open in a new tab below.');
    const a=n('a','Open in a new tab');a.href=popUrl(c);a.target='_blank';a.rel='noopener';note.append(' ',a);
  }
  function mount(){
    if(host)return;
    host=n('section',undefined,'content-workspace');host.id='contentWorkspace';host.setAttribute('aria-label','Saved Canvas content');
    const heading=n('div',undefined,'content-toolbar');heading.append(n('h3',pop?'Content':'Documents & charts'));
    if(!pop)heading.append(btn('New scratchpad',create),btn('Refresh content',()=>list(true)),btn('Back to overview',()=>{
      if(!leave())return;host.hidden=true;
    }));
    menu=n('div',undefined,'content-menu');note=n('p','','content-status');note.setAttribute('role','status');body=n('div',undefined,'content-body');
    host.append(heading,menu,note,body);
    if(pop){document.body.append(host);}else {
      // Saved documents are an explicit view, not permanent chrome above the overview.
      host.hidden=true;
      document.querySelector('#panelBoard').append(host);
    }
  }
  async function list(force=false){
    mount();const c=scope(),identity=key(c);
    if(!c.room)return;
    if(loading||(!force&&listing===identity&&Date.now()-lastList<15000))return;
    loading=true;
    try{
      const d=await call('view',c);if(key(scope())!==identity)return;listing=identity;lastList=Date.now();menu.replaceChildren();
      for(const item of d.items)menu.append(btn(item.title,()=>open({...c,id:item.id})));
      if(!d.items.length)menu.append(n('p','No saved documents in this conversation.'));
    }catch(e){notice(e.message);}finally{loading=false;}
  }
  function leave(){
    saveLocal();
    if(current?.dirty&&!window.confirm('This document has unsaved text. Keep a browser recovery copy and leave this editor?'))return false;
    return true;
  }
  async function open(c){
    mount();if(current&&key(current)===key(c)){if(!pop){openPanel('board');host.hidden=false;}return;}if(current&&!leave())return;
    if(!pop){openPanel('board');}
    const seq=++serial;notice('Opening…');
    const d=await call('view',c);if(seq!==serial||(!pop&&(scope().agent!==c.agent||scope().room!==c.room)))return;
    render(d);if(!pop){host.hidden=false;host.scrollIntoView({block:'nearest'});}
  }
  function render(d){
    const c={...d,baselineTitle:d.title,dirty:false,saving:false};current=c;body.replaceChildren();
    if(pop){document.title=d.title+' · UX46';host.querySelector('h3').textContent=d.title;}
    const origin=n('p',d.agent+' · '+d.room,'content-origin');body.append(origin);
    const toolbar=n('div',undefined,'content-toolbar');body.append(toolbar);
    const source=n('a','Open source conversation');source.href='/?'+new URLSearchParams({agent:d.agent,room:d.room});source.target='_blank';source.rel='noopener';
    if(!pop)toolbar.append(btn('Expand Canvas',()=>{dockSize.expanded=true;applyDockSize();}));
    toolbar.append(btn('Pop out',()=>popout(c)),source);
    if(d.kind==='chart'){
      if(!pop)body.append(n('h2',d.title));
      body.append(n('p','Snapshot · '+d.payload.source+' · Saved '+new Date(d.at*1000).toLocaleString()),canvasChart(d.payload.chart));
      notice('Saved chart · revision '+d.revision);return;
    }
    c.title=n('input');c.title.value=d.title;c.title.maxLength=160;c.title.setAttribute('aria-label','Document title');
    c.editor=n('textarea');c.editor.value=d.payload.text;c.editor.maxLength=50000;c.editor.setAttribute('aria-label','Markdown source');c.editor.spellcheck=true;
    c.preview=n('article',undefined,'md content-preview');
    const tabs=n('div',undefined,'content-toolbar');
    tabs.append(btn('Edit Markdown',()=>{c.editor.hidden=false;c.preview.hidden=true;}),btn('Preview',()=>{c.preview.replaceChildren(markdownFragment(c.editor.value));c.editor.hidden=true;c.preview.hidden=false;}));
    c.preview.hidden=true;
    const row=n('div',undefined,'content-toolbar');c.save=btn('Save',()=>save(c));row.append(c.save,btn('Download my text',()=>download(c.editor.value,c.title.value)));
    const hist=n('select');hist.setAttribute('aria-label','Saved revisions');hist.append(new Option('Revision history',''));
    for(const h of d.history)hist.append(new Option('Revision '+h.revision+' · '+new Date(h.at*1000).toLocaleString(),h.revision));
    row.append(hist,btn('Load revision into editor',async()=>{
      if(!hist.value)return;if(c.dirty&&!confirm('Replace the editor text with this saved revision? Download your current text first if you want to keep both.'))return;
      const oldText=c.editor.value,oldTitle=c.title.value;
      const r=await call('view',{agent:c.agent,room:c.room,id:c.id,revision:hist.value});if(current!==c)return;
      if(c.editor.value!==oldText||c.title.value!==oldTitle){notice('You edited while that revision loaded. Your text is kept; load again when ready.');return;}
      c.editor.value=r.payload.text;c.title.value=r.title;changed();notice('Revision '+r.revision+' loaded as unsaved text. Save to make a new revision.');
    }));
    function changed(){c.dirty=c.editor.value!==c.payload.text||c.title.value!==c.baselineTitle;if(!saveLocal())return;notice(c.dirty?'Unsaved · the agent can read only the last saved revision':'Saved · revision '+c.revision);}
    c.editor.oninput=changed;c.title.oninput=changed;
    body.append(c.title,tabs,c.editor,c.preview,row);notice('Saved · revision '+d.revision);
    try{
      const recovered=JSON.parse(localStorage.getItem(draftKey(c))||'null');
      if(recovered){c.editor.value=recovered.text;c.title.value=recovered.title;c.revision=recovered.revision;c.dirty=true;notice('Recovered unsaved text from this window. Review and Save; newer saves will be checked.');}
      // Recovery from a closed window is offered explicitly; never imported over edits.
      const other=Object.keys(localStorage).filter(k=>k.startsWith('ux46.content.draft.')&&k.endsWith('.'+key(c))&&k!==draftKey(c));
      for(const k of other){const r=JSON.parse(localStorage.getItem(k));row.append(btn('Recover another window’s draft',()=>{if(c.dirty&&!confirm('Replace this editor with the recovered text?'))return;c.editor.value=r.text;c.title.value=r.title;c.revision=r.revision;c.dirty=true;saveLocal();notice('Recovered text · Save checks for a newer revision.');}));}
    }catch(e){/* malformed local cache does not replace server content */}
  }
  async function save(c){
    if(c!==current||c.saving)return;c.saving=true;c.save.disabled=true;
    const text=c.editor.value,title=c.title.value,revision=c.revision;notice('Saving…');
    try{
      const r=await call('action',{action:'save',agent:c.agent,room:c.room,id:c.id,base_revision:revision,title,payload:{text},reporter:'User'},true);
      if(current!==c)return;c.revision=r.revision;c.payload=r.payload;c.baselineTitle=r.title;c.at=r.at;
      if(c.editor.value===text&&c.title.value===title){c.dirty=false;saveLocal();render(r);}else{c.dirty=true;saveLocal();notice('Saved revision '+r.revision+' · newer typing is still unsaved');}
      listing='';if(!pop)void list(true);
    }catch(e){
      saveLocal();notice(e.status===409?'A newer save exists. Your text is kept. Compare it below before saving.':'Save failed. Your text is kept; retry Save or download it.');
      if(e.status===409){
        const fresh=await call('view',{agent:c.agent,room:c.room,id:c.id});if(current!==c)return;
        body.querySelector('.content-conflict')?.remove();const box=n('section',undefined,'content-conflict');const latest=n('pre',fresh.payload.text);box.append(n('h4','Saved revision '+fresh.revision),latest,btn('Keep my text and use this revision as the base',()=>{c.revision=fresh.revision;saveLocal();notice('Your text is still unsaved. Save will check for any further changes.');box.remove();}),btn('Use saved text',()=>{if(confirm('Discard this editor text and use the saved revision?')){c.dirty=false;saveLocal();render(fresh);}}));body.append(box);
      }
    }finally{c.saving=false;c.save.disabled=false;}
  }
  async function create(){
    if(!leave())return;const c=scope();if(!c.room)return;const title=prompt('Name this scratchpad','Untitled scratchpad');if(!title)return;
    const d=await call('action',{...c,action:'create',kind:'markdown',title,payload:{text:''},reporter:'User'},true);if(key(scope())!==key(c))return;listing='';await list(true);if(key(scope())===key(c))render(d);
  }
  async function poll(){
    if(document.hidden)return;
    if(!pop){
      if(current&&key(scope())!==key({agent:current.agent,room:current.room})){saveLocal();current=null;++serial;body?.replaceChildren();listing='';host.hidden=true;notice('');menu.replaceChildren();}
      if(state.ui.dock==='board')await list();
    }
    const c=current;if(!c||c.dirty||c.saving)return;
    try{const d=await call('view',{agent:c.agent,room:c.room,id:c.id});if(current===c&&!c.dirty&&!c.saving&&d.revision!==c.revision)render(d);}catch(e){notice('Saved view · updates unavailable');}
  }
  function card(d){
    if(!d||!/^[a-zA-Z0-9_-]{8,80}$/.test(d.id||''))return null;
    const c={...scope(),id:d.id};if(d.agent&&d.agent!==c.agent||d.room&&d.room!==c.room)return null;
    const row=n('div',undefined,'content-card');row.append(n('strong',String(d.title||'Saved content').slice(0,160)),btn('Open in Canvas',()=>open(c)),btn('Open popout',()=>{mount();popout(c);}));return row;
  }
  window.UX46Content={open,popout,card,refresh:()=>list(true),poll};
  window.addEventListener('beforeunload',e=>{if(current?.dirty){saveLocal();e.preventDefault();e.returnValue='Unsaved document';}});
  window.addEventListener('ux46-room',()=>void poll());
  window.addEventListener('focus',()=>void poll());setInterval(()=>void poll(),3000);
  if(pop){
    mount();const c={id:satelliteParams.get('content'),agent:satelliteParams.get('agent'),room:satelliteParams.get('room')};
    fetch('/api/bootstrap',{credentials:'same-origin',cache:'no-store'}).then(r=>{if(!r.ok)throw Error('Sign in to open this content');return r.json();}).then(d=>{state.csrf=d.csrf;return open(c);}).catch(e=>notice(e.message));
  }else mount();
})();
