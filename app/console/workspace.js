/* Workspace views use deterministic projections. Opening them never starts an agent. */
"use strict";
(() => {
  const mail = {account:"",category:"",project:"",lane:"needs",selected:null,data:null,gen:0,rules:false};
  const knowledge = {query:"",gen:0,project:"",kind:"",origin:"",subject:"",mode:"themes",lane:"all",selected:null,tab:"overview",cache:new Map(),snapshot:null,detailGen:0};
  const $w = id => document.getElementById(id);
  const n = (tag,attrs={},children=[]) => el(tag,attrs,children);
  const button = (label,click,cls="ws-button") => n("button",{type:"button",class:cls,text:label,on:{click}});
  const stamp = x => x ? new Date(x*1000).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : "Not checked yet";
  const title = (k,v) => n("div",{class:"ws-metric"},[n("strong",{text:String(v)}),n("span",{text:k})]);
  const error = (host,e) => host.replaceChildren(n("p",{class:"ws-error",role:"alert",text:e.message || "Could not load this view."}));
  async function call(path,body) {
    const r=await fetch(path,{method:body?"POST":"GET",credentials:"same-origin",cache:"no-store",
      headers:body?{"Content-Type":"application/json","X-Atlas-CSRF":window.__atlas?.state.csrf||""}:{},
      body:body?JSON.stringify(body):undefined});
    const data=await r.json();if(!r.ok)throw Error(data.message||data.error||"Workspace unavailable");return data;
  }
  const field=(label,input)=>n("label",{class:"ws-field"},[n("span",{text:label}),input]);
  const input=(label,value="")=>n("input",{"aria-label":label,value,maxlength:500});
  const select=(label,options,value)=> {
    const x=n("select",{"aria-label":label},options.map(([id,text])=>n("option",{value:id,text})));
    x.value=value;return x;
  };
  function header(label,subtitle) {
    return n("header",{class:"ws-header"},[n("div",{},[n("p",{class:"ws-eyebrow",text:"YOUR WORKSPACE"}),
      n("h1",{text:label}),n("p",{class:"ws-sub",text:subtitle})])]);
  }
  async function loadEmail(background=false) {
    const gen=++mail.gen;
    try {
      const data=await call("/api/email/view?"+new URLSearchParams({account:mail.account,category:mail.category,project:mail.project,lane:mail.lane}));
      if(gen!==mail.gen)return;
      mail.data=data;
      const count=$w("emailCount");if(count&&!mail.account){count.textContent=data.counts.needs;count.hidden=!data.counts.needs;}
      const active=document.activeElement;
      const focusLabel=background&&$w('emailBody').contains(active)?active.getAttribute('aria-label'):null;
      const scroll=$w('viewEmail').scrollTop,listScroll=$w('emailBody').querySelector('.mail-list')?.scrollTop||0;
      renderEmail();
      if(background){
        $w('viewEmail').scrollTop=scroll;
        const list=$w('emailBody').querySelector('.mail-list');if(list)list.scrollTop=listScroll;
        if(focusLabel)Array.from($w('emailBody').querySelectorAll('button[aria-label]')).find(x=>x.getAttribute('aria-label')===focusLabel)?.focus({preventScroll:true});
      }
    } catch(e) {if(gen===mail.gen)error($w("emailBody"),e);}
  }
  function renderEmail() {
    const data=mail.data,host=$w("emailBody");if(!data)return;
    const head=header("Email", data.preview?"Preview · fictional mail and sessions":"A little less inbox. A clear place for what needs you.");
    const account=select("Email account",[["","All accounts"],...data.accounts.map(a=>[a.id,a.email])],mail.account);
    account.addEventListener("change",()=>{mail.account=account.value;mail.selected=null;void loadEmail();});
    head.appendChild(n("div",{class:"ws-actions"},[account,button(mail.rules?"Back to email":"Your rules",()=>{mail.rules=!mail.rules;renderEmail();})]));
    host.replaceChildren(head);
    const coverage=n("div",{class:"mail-coverage"},data.accounts.map(a=>n("span",{class:"account-chip",title:a.email+" · "+stamp(a.last_success)+(a.error?" · "+a.error:"")},[
      n("i",{class:"status-light "+(a.status==="error"?"status-blocked":a.status==="healthy"&&!a.stale?"status-running":"status-unknown")}),
      n("span",{text:a.email.split("@")[1]+" · "+(a.error==='CapacityError'?'index full':a.status==='error'?'check failed':a.stale?"check overdue":a.status)})])));
    host.appendChild(coverage);
    if(!data.accounts.length){host.appendChild(n("div",{class:"ws-empty"},[n("h2",{text:"Bring your inboxes together"}),
      n("p",{text:"Set up accounts with Local. Mail access stays with your email agent; other project agents do not inherit it."})]));return;}
    if(mail.rules){renderRules(host,data);return;}
    host.appendChild(n("div",{class:"ws-metrics"},[title("Threads need you",data.counts.needs),title("Reviewed here",data.counts.reviewed),title("Awaiting a reply",data.counts.awaiting)]));
    const lanes=n("div",{class:"ws-tabs",role:"tablist","aria-label":"Email views"},[["needs","Needs you"],["all","All indexed mail"],["awaiting","Awaiting replies"]].map(([id,label])=>{
      const b=button(label,()=>{mail.lane=id;mail.selected=null;void loadEmail();});b.setAttribute("role","tab");b.setAttribute("aria-selected",String(mail.lane===id));return b;
    }));
    if(mail.category)lanes.appendChild(button("Clear "+mail.category+" filter",()=>{mail.category="";void loadEmail();}));
    if(mail.project)lanes.appendChild(button("Clear "+mail.project+" filter",()=>{mail.project="";void loadEmail();}));
    host.appendChild(lanes);
    const list=n("div",{class:"mail-list","aria-label":"Email threads"});
    if(!data.items.length)list.appendChild(n("div",{class:"ws-empty"},[n("h2",{text:data.coverage_complete?"Nothing here needs you":"Still gathering the picture"}),
      n("p",{text:data.coverage_complete?"Within your selected accounts and filters.":"Coverage is incomplete. This is not an all-clear for your inboxes."})]));
    for(const item of data.items){
      const selected=mail.selected===item.account+"/"+item.thread;
      list.appendChild(n("button",{type:"button",class:"mail-row"+(selected?" selected":""),
        "aria-label":item.subject+" · "+item.account_email,on:{click:()=>{mail.selected=item.account+"/"+item.thread;renderEmail();}}},[
        n("span",{class:"mail-need "+(item.need||"quiet"),text:item.active?item.need:item.reviewed?"reviewed":item.awaiting?"waiting":"quiet"}),
        n("span",{class:"mail-row-main"},[n("strong",{text:item.subject}),n("span",{text:(item.project||item.account_email)+" · "+stamp(item.stamp)})]),
        n("span",{class:"mail-arrow",text:"↗","aria-hidden":"true"})]));
    }
    if(data.more)list.appendChild(n("p",{class:"ws-sub",text:"Showing the first 100 threads. Narrow the account or category filter."}));
    const detail=n("aside",{class:"mail-detail","aria-label":"Selected email"});
    const chosen=data.items.find(i=>i.account+"/"+i.thread===mail.selected);
    if(chosen)renderMailDetail(detail,chosen);
    else detail.appendChild(n("div",{class:"mail-detail-empty"},[n("span",{text:"✉","aria-hidden":"true"}),n("p",{text:"Choose a thread to see why it surfaced."})]));
    host.appendChild(n("div",{class:"mail-grid"},[list,detail]));
    const mix=n("section",{class:"mail-mix"},[n("h2",{text:"Incoming mix"}),n("p",{class:"ws-sub",text:data.period+" · "+data.counts.received+" messages"})]);
    const bar=n("div",{class:"mix-bar","aria-label":"Incoming mail categories"});
    const legend=n("div",{class:"mix-legend"});
    for(const [cat,count] of Object.entries(data.mix)){
      if(!count)continue;const pct=Math.round(100*count/Math.max(1,data.counts.received));
      const open=()=>{mail.category=cat;mail.lane="all";mail.selected=null;void loadEmail();};
      const b=button("",open,"mix-segment cat-"+cat);b.style.flexGrow=String(count);b.title=cat+": "+count+" messages";b.setAttribute("aria-label",b.title);bar.appendChild(b);
      legend.appendChild(button(cat+" "+pct+"%",open,"mix-key cat-"+cat));
    }
    mix.append(bar,legend);host.appendChild(mix);
    const breakdown=(label,entries,total,onSelect)=>{
      const chart=n("section",{class:"mail-breakdown"},[n("h2",{text:label})]);
      const sorted=entries.sort((a,b)=>b[2]-a[2]||a[1].localeCompare(b[1]));
      for(const [key,label,count] of sorted.slice(0,6)){
        const pct=Math.round(100*count/Math.max(1,total));
        const row=button("",()=>onSelect(key),"breakdown-row");
        row.setAttribute("aria-label",label+": "+count+" messages, "+pct+"%");
        const fill=n("span",{class:"breakdown-fill"});fill.style.width=pct+"%";
        row.append(n("span",{class:"breakdown-label",text:label}),n("span",{class:"breakdown-track"},[fill]),n("span",{text:count+" · "+pct+"%"}));chart.appendChild(row);
      }
      if(sorted.length>6)chart.appendChild(n("p",{class:"ws-sub",text:"Top 6 of "+sorted.length+" · "+sorted.slice(6).reduce((sum,x)=>sum+x[2],0)+" other messages"}));
      if(!sorted.length)chart.appendChild(n("p",{class:"ws-sub",text:"No messages in this period yet."}));
      return chart;
    };
    const accountNames=Object.fromEntries(data.accounts.map(a=>[a.id,a.email]));
    host.appendChild(n("div",{class:"mail-charts"},[
      breakdown("By email address",Object.entries(data.account_mix||{}).map(([id,count])=>[id,accountNames[id]||id,count]),data.counts.received,id=>{mail.account=id;mail.selected=null;mail.lane="all";void loadEmail();}),
      breakdown("By project",Object.entries(data.project_mix||{}).map(([id,count])=>[id,id,count]),data.counts.received,id=>{mail.project=id;mail.selected=null;mail.lane="all";void loadEmail();})
    ]));
    if(data.project_mix?.Unmapped)host.appendChild(n("p",{class:"ws-sub",text:"Project counts follow your address rules. Unmapped mail stays visible until we connect its alias."}));
    const subs=n("details",{class:"mail-subscriptions"},[n("summary",{text:"Subscriptions · "+(data.mix.subscriptions||0)+" messages"}),n("p",{class:"ws-sub",text:"Subscription and billing messages by sender. Active plans, renewals and spending are not yet verified."})]);
    for(const s of data.subscriptions||[])subs.appendChild(n("div",{class:"subscription-row"},[n("span",{text:s.sender_domain}),n("strong",{text:String(s.messages)})]));
    subs.appendChild(button("Show subscription mail",()=>{mail.category="subscriptions";mail.lane="all";mail.selected=null;void loadEmail();}));host.appendChild(subs);
    host.appendChild(n("footer",{class:"ws-foot",text:data.checker+" · Categorization is local; no email is sent, archived or marked read."}));
    if(!data.coverage_complete)host.appendChild(n("p",{class:"ws-warning",text:"Some accounts are still syncing or need a connection check. Counts cover indexed mail only."}));
  }
  function renderMailDetail(host,item) {
    host.append(n("p",{class:"ws-eyebrow",text:item.account_email}),n("h2",{text:item.subject}),
      n("p",{class:"mail-sender",text:item.sender}),n("p",{class:"ws-sub",text:"To: "+item.recipient}),
      n("p",{class:"mail-why",text:item.reason}),n("p",{class:"mail-snippet",text:item.snippet||"No preview text available. Open the original in Gmail."}));
    for(const warning of item.warnings)host.appendChild(n("p",{class:"ws-warning",text:warning}));
    host.appendChild(n("a",{class:"ws-primary",href:item.gmail_url,target:"_blank",rel:"noopener noreferrer",text:"Open in Gmail ↗"}));
    const note=n("p",{class:"ws-sub",role:"status"});
    const act=async action=>{try{await call("/api/email/attention",{account:item.account,thread:item.thread,revision:item.revision,
      base_version:item.ack_revision,action});await loadEmail();}catch(e){note.textContent=e.message;}};
    host.appendChild(n("div",{class:"ws-actions"},[
      button(item.reviewed||item.snoozed||item.awaiting?"Reopen":"Mark reviewed",()=>act(item.reviewed||item.snoozed||item.awaiting?"reopen":"reviewed")),
      button("Snooze 1 day",()=>act("snooze")),button("Awaiting reply",()=>act("awaiting"))]));
    host.append(note,n("p",{class:"ws-sub",text:"These controls organize UX46 attention. New incoming mail brings the thread back. Gmail may ask you to choose or sign in to the named account."}));
  }
  function renderRules(host,data) {
    host.appendChild(n("p",{class:"ws-sub",text:"Your exact-address rules. Keep-visible rules win over quiet rules. Sender warnings remain visible."}));
    for(const r of data.rules){
      host.appendChild(n("div",{class:"rule-row"},[n("div",{},[n("strong",{text:r.value}),n("p",{class:"ws-sub",text:r.field+" · "+r.need+" · "+r.category+(r.project?" · "+r.project:"")})]),
        button(r.enabled?"Pause":"Enable",async()=>{try{await call("/api/email/rule",{base_revision:r.revision,rule:{...r,enabled:!r.enabled}});await loadEmail();}catch(e){error(host,e);}})]));
    }
    const address=input("Exact email address");address.type="email";address.required=true;
    const project=input("Project (optional)");
    const scope=select("Rule account",[["*","All accounts"],...data.accounts.map(a=>[a.id,a.email])],"*");
    const match=select("Match",[["recipient","Sent to alias"],["sender","Sent from address"]],"recipient");
    const need=select("Attention",[["read","Always show: read"],["reply","Always show: reply"],["decide","Always show: decide"],["quiet","Keep in quieter mail"]],"read");
    const cat=select("Category",["legal","receipts","subscriptions","customers","security","infrastructure","newsletters","other"].map(x=>[x,x]),"legal");
    const status=n("p",{role:"status",class:"ws-sub"});
    const form=n("form",{class:"ws-form"},[n("h2",{text:"Add a rule"}),field("Address",address),field("Match",match),field("Account",scope),field("Attention",need),field("Category",cat),field("Project",project),n("button",{type:"submit",class:"ws-primary",text:"Save rule"}),status]);
    form.addEventListener("submit",async e=>{e.preventDefault();try{await call("/api/email/rule",{base_revision:0,rule:{id:crypto.randomUUID(),field:match.value,value:address.value,
      account:scope.value,need:need.value,category:cat.value,project:project.value,enabled:true,source:"User in UX46"}});await loadEmail();}catch(ex){status.textContent=ex.message;}});
    host.appendChild(form);
  }
  const originNames={'human-direction':'Human direction','agent-discovery':'Agent discovery','joint-discovery':'Discovered together','inference':'Inferred connection'};
  const pretty=value=>String(value||'').replace(/[-_]/g,' ').replace(/^./,x=>x.toUpperCase());
  function wsIcon(id){const x=document.createElementNS('http://www.w3.org/2000/svg','svg');x.setAttribute('viewBox','0 0 20 20');x.setAttribute('class','ic');x.setAttribute('aria-hidden','true');const use=document.createElementNS(x.namespaceURI,'use');use.setAttribute('href','#'+id);x.append(use);return x;}
  async function loadKnowledge() {
    const gen=++knowledge.gen,host=$w('constellationBody');knowledge.detailGen++;
    try{
      const [catalog,health,review,data]=await Promise.all([call('/api/constellation/catalog'),call('/api/constellation/health'),call('/api/constellation/review'),
        knowledge.query?call('/api/constellation/lookup?'+new URLSearchParams({query:knowledge.query})):Promise.resolve(null)]);
      if(gen!==knowledge.gen)return;
      knowledge.snapshot={catalog,health,review,data,at:Date.now()};knowledge.cache.clear();
      for(const r of data?.items||[])knowledge.cache.set(r.id,r);
      renderKnowledge();
    }catch(e){if(gen===knowledge.gen)error(host,e);}
  }
  function knowledgeRows(){
    const s=knowledge.snapshot;if(!s)return [];
    let rows=knowledge.query?s.data?.items||[]:s.catalog.items;
    if(knowledge.lane==='review')rows=s.review.items.map(r=>({...s.catalog.items.find(c=>c.id===r.id),...r}));
    return rows.filter(r=>(!knowledge.project||(r.projects||[]).includes(knowledge.project))&&(!knowledge.subject||(r.subjects||[]).includes(knowledge.subject))&&(!knowledge.kind||r.kind===knowledge.kind)&&(!knowledge.origin||r.origin===knowledge.origin));
  }
  function renderKnowledge(){
    const host=$w('constellationBody'),s=knowledge.snapshot;if(!s)return;
    host.classList.add('stellar-surface');host.replaceChildren();
    const shell=n('div',{class:'constellation-shell'+(knowledge.selected?' with-detail':'')}),main=n('main',{class:'stellar-main'});
    const head=header('Constellation','Where useful connections across your work become visible.');
    head.append(button('Refresh',()=>void loadKnowledge()));main.append(head);
    const search=input('Ask about patterns, lessons or opportunities across your work',knowledge.query);search.placeholder='Ask about patterns, lessons, blockers, or opportunities across your work…';
    const submit=n('button',{type:'submit',class:'ws-primary',text:'Find connections →'});
    const form=n('form',{class:'constellation-search'},[wsIcon('i-spark'),search,submit]);
    form.addEventListener('submit',async e=>{e.preventDefault();knowledge.query=search.value.trim();knowledge.lane='all';knowledge.selected=null;await loadKnowledge();});main.append(form);
    const projectNames=[...new Set(s.catalog.items.flatMap(r=>r.projects||[]))].sort();
    const kinds=[...new Set(s.catalog.items.map(r=>r.kind).filter(Boolean))].sort();
    const filters=n('div',{class:'stellar-filters'});
    for(const [key,label,options] of [['project','All projects',projectNames.map(x=>[x,pretty(x)])],['kind','All lesson types',kinds.map(x=>[x,pretty(x)])],['origin','All origins',Object.entries(originNames)]]){
      const pick=select(label,[['',label],...options],knowledge[key]);pick.addEventListener('change',()=>{knowledge[key]=pick.value;renderKnowledge();});filters.append(pick);
    }
    if(knowledge.subject)filters.append(n('span',{class:'stellar-filter-tag',text:pretty(knowledge.subject)}));
    filters.append(button('Clear filters',()=>{Object.assign(knowledge,{project:'',kind:'',origin:'',subject:'',lane:'all'});renderKnowledge();},'stellar-text-button'));main.append(filters);
    const subjects=new Set(s.catalog.items.flatMap(r=>r.subjects||[]));
    const metrics=n('div',{class:'stellar-metrics'});
    for(const [icon,value,label,lane] of [['i-spark',s.health.records,'Captured lessons','all'],['i-refresh',s.health.applied_outcomes,'Applied outcomes','outcomes'],['i-bell',s.review.items.length+(s.review.more?'+':''),'Needs review','review'],['i-projects',projectNames.length+(s.catalog.more?'+':''),'Indexed projects','all']]){
      const tile=button('',()=>{knowledge.lane=lane;knowledge.project='';knowledge.kind='';knowledge.origin='';knowledge.subject='';renderKnowledge();$w('stellarResults')?.scrollIntoView({block:'nearest'});},'stellar-metric');
      tile.append(wsIcon(icon),n('div',{},[n('strong',{text:String(value)}),n('span',{text:label})]));metrics.append(tile);
    }
    main.append(metrics,knowledgeMap(s.catalog.items));
    const tabs=n('div',{class:'stellar-result-tools'}),choices=n('div',{class:'ws-tabs','aria-label':'Insight views'});
    for(const [value,label] of [['all','All insights'],['review','Needs review'],['outcomes','Applied outcomes']]){
      const btn=button(label,()=>{knowledge.lane=value;renderKnowledge();});btn.setAttribute('aria-pressed',String(knowledge.lane===value));choices.append(btn);
    }
    tabs.append(choices,n('span',{class:'ws-sub',text:knowledge.query?'Top matching lessons':'Browse the shared index'}));main.append(tabs);
    const results=n('section',{id:'stellarResults','aria-label':'Constellation insights',class:'stellar-cards'});
    if(knowledge.lane==='outcomes'){
      const reports=(s.health.feedback_items||[]).filter(f=>['helped','failed'].includes(f.verdict));
      for(const f of reports){const card=n('article',{class:'stellar-card'},[n('span',{class:'stellar-kind',text:f.verdict==='helped'?'Applied · helped':'Applied · did not help'}),n('h2',{text:f.reason}),n('p',{class:'ws-sub',text:'Reported by '+f.principal+' · '+stamp(f.at)}),n('p',{class:'ws-sub',text:'Evidence: '+(f.evidence||'No outcome reference supplied')})]);
        card.append(button('Open applied lesson →',()=>void openKnowledge(f.lesson_id,f.lesson_revision)));results.append(card);}
      if(!reports.length)results.append(n('p',{class:'ws-empty',text:'No applied outcomes in the latest feedback window.'}));
      if(s.health.applied_outcomes>reports.length)results.append(n('p',{class:'ws-sub',text:'Showing '+reports.length+' outcomes from the latest 20 feedback records.'}));
    }else{
      const rows=knowledgeRows();
      for(const r of rows){const full=knowledge.cache.get(r.id)||r,flag=s.review.items.find(x=>x.id===r.id);const card=n('article',{class:'stellar-card'+(knowledge.selected?.id===r.id?' selected':'')});
        card.append(n('div',{class:'knowledge-tags'},[n('span',{class:'stellar-kind',text:pretty(r.kind||'Lesson')}),...(flag?[n('span',{class:'stellar-review-tag',text:'Needs review'})]:[])]));
        const open=button(r.claim,()=>void openKnowledge(r.id),'stellar-card-title');card.append(n('h2',{},[open]));
        if(full.rationale)card.append(n('p',{class:'stellar-card-summary',text:full.rationale}));
        card.append(n('p',{class:'ws-sub',text:originNames[r.origin]||'Source attribution in details'}));
        const chips=n('div',{class:'stellar-chips'});for(const project of r.projects||[])chips.append(button(pretty(project),()=>{knowledge.project=project;renderKnowledge();},'stellar-project-chip'));
        card.append(chips,n('div',{class:'stellar-card-footer'},[n('span',{text:(r.subjects||[]).map(pretty).join(' · ')}),button('View →',()=>void openKnowledge(r.id),'stellar-text-button')]));results.append(card);
      }
      if(!rows.length)results.append(n('div',{class:'ws-empty'},[n('h2',{text:'No matching insights'}),n('p',{text:'Try a broader search or clear the filters. Review holds remain available under Needs review.'})]));
    }
    main.append(results);
    if(s.catalog.more&&!knowledge.query&&knowledge.lane==='all')main.append(button('Load more from the index',async e=>{
      const target=e.currentTarget;target.disabled=true;const gen=knowledge.gen;
      try{const next=await call('/api/constellation/catalog?'+new URLSearchParams({after:s.catalog.after}));if(gen!==knowledge.gen)return;s.catalog={...next,items:[...s.catalog.items,...next.items]};renderKnowledge();}
      catch(ex){target.disabled=false;target.textContent=ex.message;}
    }));
    const activity=n('details',{class:'stellar-activity'},[n('summary',{text:'Learning activity & retrieval details'})]);
    for(const row of s.health.adoption||[])activity.append(n('p',{class:'ws-sub',text:row.principal+' · '+row.agent_discoveries+' discoveries · '+row.briefs_in_window+' recalls · '+row.reported_applications+' reported applications'}));
    activity.append(n('p',{class:'ws-sub',text:'Activity covers bounded recent observations. Test recalls can appear here; installation does not establish adoption.'}));
    if(s.data?.measurement)activity.append(n('p',{class:'ws-sub',text:s.data.measurement.returned_bytes+' bytes returned · '+s.data.measurement.elapsed_ms+' ms · up to 5 lessons per search'}));
    main.append(activity,n('footer',{class:'ws-foot',text:s.catalog.items.length+' indexed lessons loaded · '+subjects.size+' topics · updated '+new Date(s.at).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})+' · No model calls to render this view. Token savings remain unmeasured.'}));
    shell.append(main);if(knowledge.selected)shell.append(knowledgeDrawer());host.append(shell);
  }
  function knowledgeMap(records){
    const section=n('section',{class:'stellar-map','aria-label':'Connections across indexed lessons'});
    const top=n('div',{class:'stellar-map-head'},[n('div',{},[n('h2',{text:'Emerging themes'}),n('p',{class:'ws-sub',text:'Select a node to see its lessons. Lines mean shared lesson tags, not proven outcomes.'})])]);
    const toggles=n('div',{class:'stellar-map-toggle'});for(const mode of ['themes','projects']){const b=button(pretty(mode),()=>{knowledge.mode=mode;renderKnowledge();});b.setAttribute('aria-pressed',String(knowledge.mode===mode));toggles.append(b);}top.append(toggles);section.append(top);
    const key=knowledge.mode==='projects'?'projects':'subjects',group=new Map();
    for(const r of records)for(const term of new Set(r[key]||[])){if(!group.has(term))group.set(term,[]);group.get(term).push(r.id);}
    const nodes=[...group].sort((a,b)=>b[1].length-a[1].length||a[0].localeCompare(b[0])).slice(0,8).map(([name,ids],i,all)=>({name,ids,x:((i%4)+.5)*100/Math.min(4,i<4?all.length:all.length-4),y:all.length===1?50:all.length<=4?(i%2?60:40):i<4?25:75}));
    const plot=n('div',{class:'stellar-plot'}),svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 1000 260');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');
    const edges=[];nodes.forEach((a,i)=>nodes.slice(i+1).forEach(b=>{const shared=a.ids.filter(id=>b.ids.includes(id));if(shared.length)edges.push({a,b,count:shared.length});}));
    edges.sort((a,b)=>b.count-a.count);for(const {a,b,count} of edges.slice(0,20)){const line=document.createElementNS(svg.namespaceURI,'line');for(const [name,value] of Object.entries({x1:a.x*10,y1:a.y*2.6,x2:b.x*10,y2:b.y*2.6,'stroke-width':Math.min(3,.7+count*.3)}))line.setAttribute(name,value);svg.append(line);}plot.append(svg);
    nodes.forEach((node,i)=>{const target=knowledge.mode==='projects'?'project':'subject',active=knowledge[target]===node.name;const b=button('',()=>{knowledge[target]=active?'':node.name;knowledge.lane='all';renderKnowledge();},'stellar-node');
      b.style.left=node.x+'%';b.style.top=node.y+'%';b.style.setProperty('--node-color',['#ac86fa','#6dadf5','#eb93ca','#62d6a3','#e5be55','#98a2f9','#79c9d5','#d7a376'][i]);b.setAttribute('aria-pressed',String(active));b.append(n('span',{class:'stellar-orb'}),n('strong',{text:pretty(node.name)}),n('small',{text:node.ids.length+' lesson'+(node.ids.length===1?'':'s')}));plot.append(b);});
    if(!nodes.length)plot.append(n('p',{class:'ws-empty',text:'No '+knowledge.mode+' indexed yet.'}));section.append(plot);
    if(group.size>nodes.length)section.append(n('p',{class:'ws-sub',text:'Showing the 8 most represented '+knowledge.mode+' in the loaded index.'}));return section;
  }
  async function openKnowledge(id,revision){
    const gen=++knowledge.detailGen;knowledge.selected={id,revision,loading:true};knowledge.tab='overview';renderKnowledge();
    try{const r=revision?await call('/api/constellation/get?'+new URLSearchParams({id,revision})):knowledge.cache.get(id)||await call('/api/constellation/get?'+new URLSearchParams({id}));
      if(gen!==knowledge.detailGen)return;if(!revision)knowledge.cache.set(id,r);knowledge.selected={id,revision,record:r};renderKnowledge();
      const drawer=$w('stellarDrawer');drawer?.focus({preventScroll:true});if(window.innerWidth<1000)drawer?.scrollIntoView({block:'start'});
    }catch(e){if(gen===knowledge.detailGen){knowledge.selected={id,error:e.message};renderKnowledge();}}
  }
  function knowledgeDrawer(){
    const chosen=knowledge.selected,r=chosen.record;
    const drawer=n('aside',{id:'stellarDrawer',class:'stellar-drawer',tabindex:'-1','aria-label':'Insight details'});
    const close=button('×',()=>{knowledge.detailGen++;knowledge.selected=null;renderKnowledge();},'stellar-close');close.setAttribute('aria-label','Close insight details');drawer.append(close);
    drawer.addEventListener('keydown',e=>{if(e.key==='Escape')close.click();});
    if(!r){drawer.append(n('p',{role:'status',text:chosen.error||'Loading insight…'}));return drawer;}
    drawer.append(n('span',{class:'stellar-kind',text:pretty(r.kind)}),n('h2',{text:r.claim}),n('p',{class:'ws-sub',text:(originNames[r.origin]||'Origin not recorded')+' · '+r.owner}));
    if(r.current_revision)drawer.append(n('p',{class:'ws-warning',text:'Applied revision '+r.revision+' · current revision '+r.current_revision}));
    const tabs=n('div',{class:'stellar-detail-tabs'});for(const [tab,label] of [['overview','Overview'],['evidence','Evidence ('+r.sources.length+')'],['projects','Projects ('+r.projects.length+')'],['related','Related ('+r.links.length+')']]){const b=button(label,()=>{knowledge.tab=tab;renderKnowledge();});b.setAttribute('aria-pressed',String(knowledge.tab===tab));tabs.append(b);}drawer.append(tabs);
    const body=n('div',{class:'stellar-detail-body'});
    if(knowledge.tab==='overview'){
      body.append(n('h3',{text:'Why this matters'}),n('p',{text:r.rationale}),n('h3',{text:'When it helps'}),n('p',{text:r.applies}),n('p',{class:'ws-sub',text:'Limits: '+r.limits}));
      for(const warning of r.warnings||[])body.append(n('p',{class:'ws-warning',text:warning}));
      if(r.learning&&Object.values(r.learning).some(Boolean)){body.append(n('h3',{text:'Put it to work'}));for(const [key,label] of [['trigger','When'],['action','Try'],['check','Check']])if(r.learning[key])body.append(n('p',{class:'stellar-next-step'},[n('strong',{text:label+' '}),n('span',{text:r.learning[key]})]));}
      if(r.outcome){body.append(n('h3',{text:'Reported outcomes'}),n('p',{text:r.outcome.helped+' helped · '+r.outcome.failed+' did not help · '+r.outcome.used+' awaiting outcome'}));
        for(const report of r.outcome.reports||[])body.append(n('p',{class:'ws-sub',text:report.by+' reported '+report.verdict+': '+report.reason+(report.evidence?' · '+report.evidence:'')}));
        for(const correction of r.outcome.corrections||[])body.append(n('p',{class:'ws-sub',text:'Suggested by '+correction.by+': '+correction.suggestion}));}
      body.append(button('Read the evidence →',()=>{knowledge.tab='evidence';renderKnowledge();}));
    }else if(knowledge.tab==='evidence'){
      body.append(n('h3',{text:'Source evidence'}));for(const source of r.sources){const box=n('section',{class:'stellar-evidence'},[n('strong',{text:source.room}),n('p',{class:'ws-sub',text:'Source revision: '+source.revision})]);
        box.append(button('Read source',async e=>{const btn=e.currentTarget;btn.disabled=true;try{const data=await call('/api/constellation/source',{refs:[{id:r.id,revision:r.revision,source_id:source.id,...(r.current_revision?{historical:true}:{})}]});const found=data.sources[0];box.append(n('blockquote',{text:found.excerpt||'Excerpt unavailable'}),n('p',{class:'ws-sub',text:'Location: '+found.locator}));btn.remove();}catch(ex){btn.disabled=false;btn.textContent=ex.message;}}));body.append(box);}
    }else if(knowledge.tab==='projects'){
      body.append(n('h3',{text:'Related projects'}));for(const project of r.projects)body.append(button(pretty(project)+' →',()=>{knowledge.project=project;knowledge.subject='';knowledge.lane='all';knowledge.selected=null;renderKnowledge();},'stellar-related'));
    }else{
      body.append(n('h3',{text:'Recorded connections'}));if(!r.links.length)body.append(n('p',{class:'ws-sub',text:'No explicit lesson connections recorded yet.'}));
      for(const link of r.links){const row=n('section',{class:'stellar-evidence'},[button((link.title||link.target)+' →',()=>void openKnowledge(link.target),'stellar-related'),n('p',{class:'ws-sub',text:pretty(link.type)+' · '+link.state}),n('p',{text:link.reason})]);body.append(row);}
    }
    const feedback=knowledgeCard(r).querySelector('.knowledge-feedback');body.append(feedback,n('p',{class:'ws-sub',text:'Lesson revision '+r.revision+' · '+pretty(r.evidence)+' · '+pretty(r.state)}));drawer.append(body);return drawer;
  }
  function knowledgeCard(r) {
    const card=n("article",{class:"knowledge-card"});
    card.append(n("div",{class:"knowledge-tags"},[n("span",{text:r.kind}),n("span",{text:r.evidence}),n("span",{text:r.state})]),n("h2",{text:r.claim}),
      n("p",{class:"knowledge-scope",text:r.projects.join(" · ")}),n("p",{class:"ws-sub",text:"Captured by "+r.owner+" · "+stamp(r.created_at)}));
    const origins={'human-direction':'Human direction','agent-discovery':'Agent discovery','joint-discovery':'Discovered together','inference':'Inferred connection'};
    if(origins[r.origin])card.appendChild(n('p',{class:'ws-eyebrow',text:origins[r.origin]}));
    if(r.outcome)card.appendChild(n('p',{class:'ws-sub',text:r.outcome.helped+' helped · '+r.outcome.failed+' did not help · '+r.outcome.used+' awaiting outcome'+(r.outcome.held_for_review?' · Held for review':'')}));
    const details=n("details",{},[n("summary",{text:"When it helps & evidence"}),n("p",{text:r.rationale}),
      n("p",{text:"Applies: "+r.applies}),n("p",{class:"ws-sub",text:"Limits: "+r.limits})]);
    for(const [key,label] of [['trigger','When'],['action','Try'],['check','Check']])if(r.learning?.[key])details.appendChild(n('p',{text:label+': '+r.learning[key]}));
    for(const warning of r.warnings||[])details.appendChild(n('p',{class:'ws-warning',text:warning}));
    for(const report of r.outcome?.reports||[])details.appendChild(n('p',{text:report.by+' reported '+report.verdict+': '+report.reason+(report.evidence?' · '+report.evidence:'')}));
    for(const correction of r.outcome?.corrections||[])details.appendChild(n('p',{text:'Suggested by '+correction.by+': '+correction.suggestion}));
    const sourceHost=n("div");
    for(const source of r.sources)details.appendChild(button("Read source · "+source.room,async()=>{
      try{const data=await call("/api/constellation/source",{refs:[{id:r.id,revision:r.revision,source_id:source.id,...(r.current_revision?{historical:true}:{})}]});
        const s=data.sources[0];sourceHost.replaceChildren(n("blockquote",{text:s.excerpt||"Excerpt unavailable. Source locator: "+s.locator}),n("p",{class:"ws-sub",text:"Source revision: "+s.revision}));
      }catch(e){error(sourceHost,e);}
    }));
    details.appendChild(sourceHost);card.appendChild(details);
    for(const link of r.links)card.appendChild(button("↗ "+link.type+" · "+link.title,async()=>{
      try{card.replaceWith(knowledgeCard(await call("/api/constellation/get?"+new URLSearchParams({id:link.target}))));}catch(e){error(sourceHost,e);}
    },"knowledge-link"));
    const feedback=n("details",{class:"knowledge-feedback"},[n("summary",{text:"Did this help?"})]);
    const verdict=select("Usefulness",[["interest","Interesting, not tried"],["helped","Applied and helped"],["failed","Applied, did not help"],["used","Applied, awaiting outcome"],["not-applicable","Not relevant here"],["unknown","Outcome unknown"]],"interest");
    const reason=input("What happened?");reason.required=true;
    const evidence=input("Outcome reference (optional)");
    const suggestion=input('Suggested correction (optional)');suggestion.maxLength=500;
    const note=n("p",{role:"status",class:"ws-sub"});
    const f=n("form",{},[field("Outcome",verdict),field("What happened?",reason),field("Evidence",evidence),field('Correction',suggestion),n("button",{type:"submit",class:"ws-button",text:"Save feedback"}),note]);
    const key=crypto.randomUUID();
    f.addEventListener("submit",async e=>{e.preventDefault();try{await call("/api/constellation/feedback",{key,id:r.id,revision:r.revision,use_id:key,
      verdict:verdict.value,reason:reason.value,evidence:evidence.value,suggestion:suggestion.value});note.textContent="Saved. Applied outcomes inform future ranking and review; interest stays separate.";f.querySelector('button').disabled=true;}catch(ex){note.textContent=ex.message;}});
    feedback.appendChild(f);card.appendChild(feedback);return card;
  }
  async function loadSchedule() {
    const host=$w('scheduleBody');
    try {
      const data=await call('/api/schedule/view');
      host.replaceChildren(header('Scheduled','Reminders and recurring work, in one place.'));
      host.appendChild(n('div',{class:'ws-metrics'},[title('Needs review',data.due),title('Registered tasks',data.items.length),title('Model calls per check',0)]));
      if(data.checker_stale)host.appendChild(n('p',{class:'ws-warning',text:'The checker is overdue. Date reminders still appear here; use counts may be behind.'}));
      for(const task of data.items) {
        const reminder=task.kind==='reminder',closed=['complete','cancelled'].includes(task.state);
        const status=task.state==='due'?'Review due':task.stale?'Status overdue':task.state==='observed'?'Observed':task.state;
        const card=n('article',{class:'schedule-card'+(task.state==='due'?' due':'')});
        card.append(n('div',{class:'schedule-top'},[n('i',{class:'status-light '+(task.state==='due'||task.state==='needs-check'?'status-blocked':closed||task.state==='observed'?'status-running':'status-unknown')}),
          n('h2',{text:task.title}),n('span',{class:'schedule-status',text:status})]));
        card.appendChild(n('p',{class:'schedule-when',text:reminder?`${task.progress||0} / ${task.uses} further uses · or ${new Date(task.deadline*1000).toLocaleString([], {dateStyle:'medium',timeStyle:'short',timeZone:task.timezone})} (${task.timezone})`:task.schedule}));
        if(reminder){const progress=n('progress',{max:task.uses,value:Math.min(task.progress||0,task.uses),'aria-label':'Applied uses toward review'});card.appendChild(progress);}
        const detail=n('details',{},[n('summary',{text:'Details'}),n('p',{text:task.detail||task.observation||''}),n('p',{class:'ws-sub',text:'Owner: '+task.owner})]);
        if(task.observation)detail.appendChild(n('p',{text:task.observation}));
        if(task.last_success)detail.appendChild(n('p',{class:'ws-sub',text:'Last evidence: '+stamp(task.last_success)}));
        if(task.counter_error)detail.appendChild(n('p',{class:'ws-warning',text:task.counter_error}));
        if(reminder){
          detail.appendChild(n('p',{class:'ws-sub',text:'Counted: distinct applied-use reports, helped or failed. Interest and repeated feedback on the same use do not count.'}));
          const note=n('p',{role:'status'});
          const act=async action=>{try{await call('/api/schedule/action',{id:task.id,revision:task.revision,action});await loadSchedule();await scheduleBadge();}catch(e){note.textContent=e.message;}};
          detail.appendChild(n('div',{class:'ws-actions'},closed?[button('Reopen',()=>act('reopen'))]:[button('Mark reviewed',()=>act('complete')),button('Cancel reminder',()=>act('cancel'))]));detail.appendChild(note);
        }
        card.appendChild(detail);host.appendChild(card);
      }
      host.appendChild(n('footer',{class:'ws-foot',text:data.coverage+' · Last check: '+stamp(data.checked_at)}));
    }catch(e){error(host,e);}
  }
  let usageGeneration=0;
  async function loadUsage(day='') {
    const gen=++usageGeneration,host=$w('usageBody');
    host.replaceChildren(header('Usage','Where recorded model work is going.'));
    host.appendChild(n('div',{class:'ws-actions'},[n('span',{class:'ws-sub',text:'All recorded usage'}),button('Refresh view',()=>void loadUsage())]));
    const loading=n('p',{class:'ws-sub',text:'Reading connected agents…'});host.appendChild(loading);
    let collection;
    try {collection=await call('/api/usage-report/view');}catch(e){if(gen===usageGeneration)error(host,e);return;}
    if(gen!==usageGeneration)return;loading.remove();
    const responses=collection.responses||[];
    host.appendChild(n('p',{class:'ws-sub',text:'Last collection: '+stamp(collection.at)+' · '+(collection.coverage||'')}));
    if(!collection.at||Date.now()/1000-collection.at>900)host.appendChild(n('p',{class:'ws-warning',text:'Usage collection is overdue or has not run. These are the last saved observations.'}));
    const report=usageReport(responses);
    host.appendChild(n('div',{class:'ws-metrics'},[title('Observed tokens',compact(report.threads?report.total:null)),title('Measured conversations',report.threads),title('Dollar billing','Unknown')]));
    host.appendChild(n('p',{class:'ws-sub',text:'Input + output tokens. Cached input and reasoning are subsets, counted once. Only indexed conversations are included; totals may be partial.'}));
    const coverage=n('div',{class:'mail-coverage'},responses.map(r=>n('span',{class:'account-chip'},[
      n('i',{class:'status-light '+(r.error?'status-unknown':'status-running')}),n('span',{text:r.agent.label+' · '+(r.error||r.data.native_threads.length+' indexed')})])));host.appendChild(coverage);
    if(report.unknownThreads)host.appendChild(n('p',{class:'ws-warning',text:report.unknownThreads+' indexed conversations have no token measurements yet.'}));
    if((collection.rooms||[]).some(r=>r.pending))host.appendChild(n('p',{class:'ws-sub',text:'Some native receipts are still indexing. This report covers the portion read so far.'}));
    const grid=n('div',{class:'usage-projects'});
    for(const project of report.projects){
      const card=n('article',{class:'schedule-card'}),pct=report.total?Math.round(project.total/report.total*100):0;
      card.append(n('div',{class:'schedule-top'},[n('h2',{text:project.name}),n('strong',{text:compact(project.total)+' · '+pct+'%'})]));
      const fill=n('span',{class:'breakdown-fill'});fill.style.width=pct+'%';card.appendChild(n('div',{class:'breakdown-track'},[fill]));
      card.appendChild(n('p',{class:'ws-sub',text:project.threads.length+' conversations · '+project.agents.join(', ')}));
      const details=n('details',{},[n('summary',{text:'What drove the usage?'}),n('p',{text:'Input '+compact(project.input)+' · output '+compact(project.output)+' · cached input '+compact(project.cached)})]);
      if(project.input>0&&project.cached!==null)details.appendChild(n('p',{text:Math.round(project.cached/project.input*100)+'% of observed input was cached.'}));
      details.appendChild(n('p',{class:'ws-sub',text:'Volume and cache reuse are observations. Rework, quality and savings require linked outcomes; they are not inferred from token counts.'}));
      for(const thread of project.threads.slice(0,12)) {
        const alias=thread.aliases?.[0];
        const row=button((alias?.room_id?.split('/').slice(1).join('/')||'Native conversation')+' · '+compact(thread.total)+(thread.usage?.latest_model?' · latest '+thread.usage.latest_model:''),async()=>{
          if(!alias)return;await window.__atlas.switchAgent(alias.agent||thread.agent);await window.__atlas.selectRoom(alias.room_id);window.__atlas.showView('console');
        },'usage-thread');details.appendChild(row);
      }
      if(project.threads.length>12)details.appendChild(n('p',{class:'ws-sub',text:'Top 12 conversations shown.'}));
      card.appendChild(details);grid.appendChild(card);
    }
    host.appendChild(grid);
    if(!report.threads)host.appendChild(n('p',{class:'ws-empty',text:'No conversations indexed yet. The deterministic collector refreshes registered desktop conversations every five minutes.'}));
    host.appendChild(n('footer',{class:'ws-foot',text:'Every agent’s coverage is shown above. Missing data is not zero. Shared native conversations across projects appear once under Shared / unattributed. Goal meters and Tell receipts are not added to these totals.'}));
  }
  const compact=x=>x===null?'Unknown':new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:1}).format(x);
  function usageReport(responses) {
    const seen=new Map();
    for(const {agent,data} of responses)for(const thread of data?.native_threads||[]) {
      const key=(agent.runtime||'codex')+':'+thread.thread_id;
      if(!seen.has(key))seen.set(key,{...thread,agent:agent.id,agents:[agent.label],aliases:[...(thread.aliases||[])]});
      else {const old=seen.get(key);old.aliases.push(...thread.aliases||[]);old.agents.push(agent.label);}
    }
    const groups=new Map();let total=0,unknownThreads=0;
    for(const thread of seen.values()) {
      if(!Number.isFinite(thread.usage?.input_tokens)&&!Number.isFinite(thread.usage?.output_tokens)){unknownThreads++;continue;}
      const names=[...new Set(thread.aliases.map(a=>a.project).filter(Boolean))];
      const name=names.length===1?names[0]:'Shared / unattributed';
      if(!groups.has(name))groups.set(name,{name,total:0,input:null,output:null,cached:null,threads:[],agents:[]});
      const p=groups.get(name),u=thread.usage||{};thread.total=(u.input_tokens||0)+(u.output_tokens||0);
      p.total+=thread.total;total+=thread.total;p.threads.push(thread);p.agents.push(...thread.agents);
      for(const [key,field] of [['input','input_tokens'],['output','output_tokens'],['cached','cached_input_tokens']])if(Number.isFinite(u[field]))p[key]=(p[key]||0)+u[field];
    }
    for(const p of groups.values()){p.agents=[...new Set(p.agents)];p.threads.sort((a,b)=>b.total-a.total);}
    return {total,threads:seen.size-unknownThreads,unknownThreads,projects:[...groups.values()].sort((a,b)=>b.total-a.total)};
  }
  async function scheduleBadge() {
    try {
      const data=await call('/api/schedule/view'),badge=$w('scheduleCount');badge.textContent=data.due;badge.hidden=!data.due;
      let notice=$w('scheduleNotice');
      if(data.due&&!notice){notice=button('',()=>window.__atlas.showView('schedule'),'schedule-notice');notice.id='scheduleNotice';notice.setAttribute('role','status');document.body.appendChild(notice);}
      if(notice){notice.hidden=!data.due;notice.textContent=data.due+' scheduled review'+(data.due===1?'':'s')+' due · Open';}
      $w('btnSchedule').title=data.checker_stale?'Scheduled tasks · checker overdue':'Scheduled tasks';
    }catch(e){$w('btnSchedule').title='Scheduled tasks · unavailable';}
  }
  window.__workspace={open:kind=>({email:loadEmail,constellation:loadKnowledge,schedule:loadSchedule,usage:loadUsage}[kind]||loadKnowledge)(),usageReport};
  async function emailBadge() {
    if (!window.__ux46modules?.email) return;
    try {const data=await call('/api/email/status'),badge=$w('emailCount');
      badge.textContent=data.counts.needs;badge.hidden=!data.counts.needs;
      $w('btnEmail').title=data.coverage_complete?'Email':'Email · coverage incomplete';
    }catch(e){$w('btnEmail').title='Email · not connected';}
  }
  void emailBadge();
  void scheduleBadge();
  // A cheap private projection read while visible; no provider query/model from the browser.
  setInterval(()=>{if(document.visibilityState!=="visible")return;
    void emailBadge();
    void scheduleBadge();
    if(!$w("viewEmail").hidden&&!mail.rules&&!document.activeElement?.matches('input,select,textarea'))void loadEmail(true);
  },60000);
})();
