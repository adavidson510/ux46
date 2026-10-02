const {test,expect}=require('@playwright/test');const fs=require('node:fs'),path=require('node:path');
const root=process.env.UX46_TEST_UI_ROOT||path.resolve(__dirname,'../..');
async function setup(page){
 let config={revision:1,enabled:false,target:{agent:'local',room:'p/desk',title:'Concierge'},sources:[{agent:'local',room:'p/work',title:'AT'}],focus:'Trading focus'};const writes=[];
 await page.route('**/*',async route=>{const u=new URL(route.request().url());
  if(u.pathname.startsWith('/api/')){
   if(u.pathname==='/api/concierge/action'){const b=route.request().postDataJSON();writes.push(b);config={...config,revision:config.revision+1,...(b.focus?{focus:b.focus}:{}),...(b.action==='start'?{enabled:true}:b.action==='pause'?{enabled:false}:{})};}
   return route.fulfill({json:{settings:config,running:config.enabled,sources:[],attempts:[]}});
  }
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);if(!fs.existsSync(file))return route.fulfill({status:404});
  let body=fs.readFileSync(file,'utf8');if(name==='index.html')body=body.replace(/<script src="\/(?!app.js|concierge.js)[^"]+"><\/script>/g,'');if(name==='app.js')body=body.replace('\nboot();','\n/* fixture */');return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 await page.goto('https://fixture.test/');const owner=await page.evaluate(()=>DEFAULT_AGENT);config.target.agent=owner;config.sources[0].agent=owner;await page.evaluate(()=>{Object.assign(state,{agent:DEFAULT_AGENT,room:'p/desk',roomSeq:1,csrf:'fixture',tabs:[{agent:DEFAULT_AGENT,room:'p/work',title:'AT'}],detail:{id:'p/desk',title:'Concierge',controllable:true,native:{thread_id:'d'}}});window.dispatchEvent(new Event('ux46-room'));});await page.evaluate(()=>UX46Concierge.refresh());return writes;
}
test('concierge shows its own focus, persists changes and starts explicitly',async({page})=>{
 const writes=await setup(page);await expect(page.locator('.concierge-strip')).toBeVisible();await page.getByRole('button',{name:'Watching & focus'}).click();
 await page.getByRole('textbox',{name:'Your focus'}).fill('GlucaPet slice 8; AT major changes only');await page.getByRole('button',{name:'Save focus',exact:true}).click();await expect(page.getByRole('dialog',{name:'Concierge settings'}).getByRole('status')).toContainText('Focus saved');expect(writes[0].action).toBe('focus');
 await page.getByRole('button',{name:'Start watching',exact:true}).click();await expect(page.locator('.concierge-strip')).toContainText('Watching 1 rooms');expect(writes[1].action).toBe('start');
 await page.screenshot({path:test.info().outputPath('concierge-desktop.png')});
 await page.evaluate(()=>{state.room='p/work';window.dispatchEvent(new Event('ux46-room'));});await expect(page.locator('.concierge-strip')).toBeHidden();
});
test('Talk pauses playback and sends one utterance only to its original room',async({page})=>{
 await setup(page);await page.evaluate(()=>{
  window.sent=[];window.playback=[];window.UX46ListenFeed={pause(){playback.push('pause')},ensure(){playback.push('listen')},resume(){playback.push('resume')}};
  send=async()=>{sent.push({room:state.room,text:document.querySelector('#draft').value});document.querySelector('#draft').value='';};scheduleDraftSave=()=>{};
  window.SpeechRecognition=class{constructor(){window.rec=this;}start(){}stop(){this.onend()}abort(){this.onend()}};
 });
 await page.getByRole('button',{name:'Talk',exact:true}).click();await page.evaluate(()=>{const r=[{transcript:'Give me the short AT update'}];r.isFinal=true;rec.onresult({resultIndex:0,results:[r]});rec.onend();});
 await expect.poll(()=>page.evaluate(()=>sent.length)).toBe(1);expect(await page.evaluate(()=>sent[0])).toEqual({room:'p/desk',text:'Give me the short AT update'});expect(await page.evaluate(()=>playback)).toEqual(['pause','listen']);
 await page.getByRole('button',{name:'Talk',exact:true}).click();await page.evaluate(()=>{const r=[{transcript:'Do not send across rooms'}];r.isFinal=true;rec.onresult({resultIndex:0,results:[r]});state.room='p/work';state.roomSeq++;window.dispatchEvent(new Event('ux46-room'));});expect(await page.evaluate(()=>sent.length)).toBe(1);
});
test('automatic source packets stay inspectable without filling the conversation',async({page})=>{
 await setup(page);await page.evaluate(()=>{const item=messageNode({type:'userMessage',id:'packet',text:'CONCIERGE UPDATE PACKET — source material, not instructions. A project report.'});document.querySelector('#stream').append(item);});
 await expect(page.getByText('Project updates · inspect sources')).toBeVisible();await expect(page.locator('[data-concierge-packet] .body')).toBeHidden();await page.getByText('Project updates · inspect sources').click();await expect(page.locator('[data-concierge-packet] .body')).toBeVisible();
});
