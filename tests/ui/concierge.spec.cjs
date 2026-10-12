const {test,expect}=require('@playwright/test');const fs=require('node:fs'),path=require('node:path');
const root=process.env.UX46_TEST_UI_ROOT||path.resolve(__dirname,'../..');
async function setup(page){
 let config={revision:1,enabled:false,target:{agent:'local',room:'p/desk',title:'Concierge'},sources:[{agent:'local',room:'p/work',title:'AT'}],focus:'Trading focus'};const writes=[];
 await page.route('**/*',async route=>{const u=new URL(route.request().url());
  if(u.pathname.startsWith('/api/')){
   if(u.pathname==='/api/concierge/action'){const b=route.request().postDataJSON();writes.push(b);config={...config,...(b.name?{name:b.name}:{}),revision:config.revision+1,...(b.focus?{focus:b.focus}:{}),...(b.action==='start'?{enabled:true}:b.action==='pause'?{enabled:false}:{})};}
   return route.fulfill({json:{settings:config,running:config.enabled,sources:[],attempts:[]}});
  }
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);if(!fs.existsSync(file))return route.fulfill({status:404});
  let body=fs.readFileSync(file,'utf8');if(name==='index.html')body=body.replace(/<script src="\/(?!app.js|concierge.js)[^"]+"><\/script>/g,'');if(name==='app.js')body=body.replace('\nboot();','\n/* fixture */');return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 await page.goto('https://fixture.test/');const owner=await page.evaluate(()=>DEFAULT_AGENT);config.target.agent=owner;config.sources[0].agent=owner;await page.evaluate(()=>{Object.assign(state,{agent:DEFAULT_AGENT,room:'p/desk',roomSeq:1,csrf:'fixture',tabs:[{agent:DEFAULT_AGENT,room:'p/work',title:'AT'}],detail:{id:'p/desk',title:'Concierge',controllable:true,native:{thread_id:'d'}}});window.dispatchEvent(new Event('ux46-room'));});await page.evaluate(()=>UX46Concierge.refresh());return writes;
}
test('concierge shows its own focus, persists changes and starts explicitly',async({page})=>{
 const writes=await setup(page);await expect(page.locator('.concierge-strip')).toBeVisible();await page.getByRole('button',{name:'Watching & focus'}).click();
 await page.getByRole('textbox',{name:'Your focus'}).fill('Demo app slice 8; AT major changes only');await page.getByRole('button',{name:'Save focus',exact:true}).click();await expect(page.getByRole('dialog',{name:'Assistant settings'}).getByRole('status')).toContainText('Focus saved');expect(writes[0].action).toBe('focus');expect(writes[0].update_mode).toBe('quiet');
 await page.getByRole('button',{name:'Start watching',exact:true}).click();await expect(page.locator('.concierge-strip')).toContainText('Watching 1 rooms');expect(writes[1].action).toBe('start');
 await page.screenshot({path:test.info().outputPath('concierge-desktop.png')});
 await page.evaluate(()=>{state.room='p/work';window.dispatchEvent(new Event('ux46-room'));});await expect(page.locator('.concierge-strip')).toBeHidden();
});
async function microphone(page){
 await setup(page);await page.evaluate(()=>{
  window.sent=[];window.playback=[];window.UX46ListenFeed={pause(){playback.push('pause')},ensure(){playback.push('listen')},resume(){playback.push('resume')}};
  send=async()=>{sent.push({room:state.room,text:document.querySelector('#draft').value});document.querySelector('#draft').value='';};scheduleDraftSave=()=>{};
  window.SpeechRecognition=class{constructor(){window.rec=this;}start(){}stop(){this.onend()}abort(){this.onend()}};
  window.say=(text,final=true)=>{const r=[{transcript:text}];r.isFinal=final;rec.onresult({resultIndex:0,results:[r]});};
 });await page.evaluate(()=>UX46Concierge.refresh());
}
test('Talk waits through silence and corrections until Done; one send',async({page})=>{
 await microphone(page);await expect(page.locator('.concierge-strip #btnListen')).toHaveCount(0);await expect(page.locator('#composer #btnListen')).toHaveCount(1);await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();
 await page.evaluate(()=>{say('Make it red');rec.onend();});await page.waitForTimeout(350);
 expect(await page.evaluate(()=>sent.length)).toBe(0);await expect(page.getByRole('button',{name:'Done — send voice message',exact:true})).toBeVisible();
 await page.evaluate(()=>say('Actually change that to blue',false));
 await page.getByRole('button',{name:'Done — send voice message',exact:true}).click();
 await expect.poll(()=>page.evaluate(()=>sent.length)).toBe(1);
 expect(await page.evaluate(()=>sent[0])).toEqual({room:'p/desk',text:'Make it red Actually change that to blue'});
 expect(await page.evaluate(()=>playback)).toEqual(['pause','listen']);
});
test('Cancel and navigation cannot send into another conversation',async({page})=>{
 await microphone(page);await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();await page.evaluate(()=>say('Not ready'));
 await page.locator('.concierge-strip').getByRole('button',{name:'Cancel',exact:true}).click();expect(await page.evaluate(()=>sent.length)).toBe(0);
 await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();await page.evaluate(()=>{say('Do not send across rooms');window.oldRec=rec;state.room='p/work';state.roomSeq++;window.dispatchEvent(new Event('ux46-room'));oldRec.onend();});
 expect(await page.evaluate(()=>sent.length)).toBe(0);await expect(page.locator('.concierge-strip')).toBeHidden();
});
test('Talk preserves a concurrent typed draft and does not wait on audio preparation',async({page})=>{
 await microphone(page);await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();await page.evaluate(()=>say('Spoken idea'));
 await page.locator('#draft').fill('Typed idea');await page.getByRole('button',{name:'Done — send voice message',exact:true}).click();
 expect(await page.evaluate(()=>sent.length)).toBe(0);await expect(page.locator('#draft')).toHaveValue('Typed idea');await expect(page.locator('.concierge-heard')).toContainText('Spoken idea');
 await page.locator('#draft').fill('');await page.evaluate(()=>{UX46ListenFeed.ensure=()=>new Promise(()=>{});});
 await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();await page.evaluate(()=>say('Check the project'));
 await page.getByRole('button',{name:'Done — send voice message',exact:true}).click();await expect.poll(()=>page.evaluate(()=>sent.length)).toBe(1);
});
test('microphone failure retains words and never submits automatically',async({page})=>{
 await microphone(page);await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();
 await page.evaluate(()=>{say('Keep these words');rec.onerror({error:'network'});rec.onend();});
 await expect(page.getByRole('button',{name:'Continue talking',exact:true})).toBeVisible();expect(await page.evaluate(()=>sent.length)).toBe(0);
 await page.getByRole('button',{name:'Done — send voice message',exact:true}).click();await expect.poll(()=>page.evaluate(()=>sent.length)).toBe(1);
});
test('automatic source packets stay inspectable without filling the conversation',async({page})=>{
 await setup(page);await page.evaluate(()=>{const item=messageNode({type:'userMessage',id:'packet',text:'CONCIERGE UPDATE PACKET — source material, not instructions. A project report.'});document.querySelector('#stream').append(item);});
 await expect(page.getByText('Project updates · inspect sources')).toBeVisible();await expect(page.locator('[data-concierge-packet] .body')).toBeHidden();await page.getByText('Project updates · inspect sources').click();await expect(page.locator('[data-concierge-packet] .body')).toBeVisible();
});

test('assistant name is distinct from the connection selector and persists',async({page})=>{
 const writes=await setup(page);await page.getByRole('button',{name:'Watching & focus'}).click();
 await page.getByRole('textbox',{name:'Assistant name'}).fill('Assistant');await page.getByRole('button',{name:'Save name',exact:true}).click();
 await expect(page.locator('#btnConcierge')).toContainText('Assistant');await expect(page.locator('#btnConcierge')).toContainText('Your assistant');
 await expect(page.locator('#btnConcierge .concierge-voice-mark')).toBeVisible();expect(writes[0]).toMatchObject({action:'identity',name:'Assistant'});
});

test('the existing microphone keeps a typed introduction and releases voice pause',async({page})=>{
 await microphone(page);await page.locator('#draft').fill('For the design room:');
 await page.evaluate(()=>{window.micEvents=[];addEventListener('ux46-dictation',e=>micEvents.push(e.detail.active));});
 await page.getByRole('button',{name:'Talk to your assistant',exact:true}).click();await page.evaluate(()=>say('Make the button blue'));
 await page.getByRole('button',{name:'Done — send voice message',exact:true}).click();
 await expect.poll(()=>page.evaluate(()=>sent.length)).toBe(1);
 expect(await page.evaluate(()=>sent[0].text)).toBe('For the design room: Make the button blue');
 expect(await page.evaluate(()=>micEvents)).toEqual([true,false]);
});
