const {test,expect}=require('@playwright/test');
const fs=require('node:fs'),path=require('node:path');
const root=process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname,'../..');
async function fixture(page){
 const records=new Map(),calls=[];let refuse=false;
 await page.addInitScript(()=>{
  navigator.mediaDevices.getUserMedia=async()=>{
   const ac=new AudioContext(),osc=ac.createOscillator(),target=ac.createMediaStreamDestination();
   osc.connect(target);osc.start();window.syntheticAudio={ac,osc};return target.stream;
  };
 });
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());if(u.origin!=='https://fixture.test')return route.abort();
  if(u.pathname.startsWith('/api/')){
   calls.push({path:u.pathname,method:route.request().method()});
   if(u.pathname==='/api/recordings/action'){
    if(refuse)return route.fulfill({status:503,json:{message:'Synthetic network interruption'}});
    const a=route.request().postDataJSON();let r=records.get(a.id);
    if(a.action==='start'){if(!r){r={id:a.id,title:a.title,mime:a.mime,metadata:a.metadata,created:Date.now()/1000,next_seq:0,version:1,status:'recording',bytes:0,parts:[],segments:[],duration:0};records.set(a.id,r);}}
    if(a.action==='chunk'){expect(a.seq).toBe(r.next_seq);r.parts.push(a.data);r.next_seq++;r.bytes+=Buffer.from(a.data,'base64').length;}
    if(a.action==='finish'){expect(a.count).toBe(r.next_seq);r.status='saved';r.interrupted=a.interrupted;r.duration=a.duration;}
    if(a.action==='metadata'){r.metadata=a.metadata;r.version++;}
    if(a.action==='rename'){r.title=a.title;r.version++;}
    return route.fulfill({json:r});
   }
   if(u.pathname==='/api/recordings/view')return route.fulfill({json:u.searchParams.has('id')?records.get(u.searchParams.get('id')):{recordings:[...records.values()],transcription_available:false,projects:[{id:'project',name:'Project'}]}});
   return route.fulfill({json:{}});
  }
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);
  if(!fs.existsSync(file))return route.fulfill({status:404});
  let body=fs.readFileSync(file,'utf8');
  if(name==='index.html')body=body.replace(/<script src="\/(?!app.js|recordings.js)[^"]+"><\/script>/g,'');
  if(name==='app.js')body=body.replace('\nboot();','\n/* fixture boot */');
  return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 async function seed(){await page.waitForFunction(()=>Boolean(window.UX46Recordings));await page.evaluate(()=>{state.csrf='fixture';state.room='project/first';document.querySelector('#draft').disabled=false;});}
 await page.goto('https://fixture.test/');await seed();
 return{records,calls,setRefuse:v=>{refuse=v;},seed};
}
test('recording survives using the composer and changing room; stop stores audio without an agent call',async({page})=>{
 const f=await fixture(page);
 await page.getByRole('button',{name:'Record audio',exact:true}).click();
 await expect(page.getByRole('dialog')).toBeVisible();
 expect(f.records.size).toBe(0); // Opening the panel cannot start the microphone.
 await page.getByRole('button',{name:'Record microphone'}).click();
 await expect(page.locator('#recordLive')).toBeVisible();
 await page.locator('#draft').fill('Keep working while this records');
 await page.evaluate(()=>{stopDictation();state.room='project/second';});
 await page.waitForTimeout(2200);
 await expect(page.locator('#draft')).toHaveValue('Keep working while this records');
 await page.locator('#recordStop').click();
 await expect(page.locator('#recordLive')).not.toBeVisible();
 await expect.poll(()=>[...f.records.values()][0]?.status).toBe('saved');
 expect([...f.records.values()][0].bytes).toBeGreaterThan(0);
 expect(f.calls.filter(r=>r.method==='POST').every(r=>r.path==='/api/recordings/action')).toBe(true);
 await page.getByRole('button',{name:'Saved recordings',exact:true}).click();
 await expect(page.locator('#recordingList')).toContainText('Audio saved');
 await page.screenshot({path:test.info().outputPath('recordings-alpha.png')});
});
test('reload recovers locally saved audio after a network failure and marks interruption',async({page})=>{
 const f=await fixture(page);f.setRefuse(true);
 await page.getByRole('button',{name:'Record audio',exact:true}).click();
 await page.getByRole('button',{name:'Record microphone'}).click();
 await expect(page.locator('#recordLive')).toBeVisible();await page.waitForTimeout(2500);
 page.on('dialog',d=>d.accept());await page.reload();await f.seed();
 f.setRefuse(false);await page.evaluate(()=>UX46Recordings.sync());
 await expect.poll(()=>[...f.records.values()][0]?.status).toBe('saved');
 expect([...f.records.values()][0].interrupted).toBe(true);expect([...f.records.values()][0].bytes).toBeGreaterThan(0);
 await expect(page.locator('#recordLive')).not.toBeVisible();
});

test('filing and a summary request preserve human control',async({page})=>{
 const f=await fixture(page);
 f.records.set('recording-fixture-0001',{id:'recording-fixture-0001',title:'Unfiled meeting',metadata:{},created:Date.now()/1000,version:1,status:'ready',bytes:10,duration:12,transcript:'We discussed the warranty.',segments:[{start:0,text:'We discussed the warranty.'}]});
 await page.getByRole('button',{name:'Saved recordings',exact:true}).click();
 await page.getByRole('button',{name:'Unfiled meeting',exact:true}).click();
 await page.getByText('Project, tags and summary',{exact:true}).click();
 await page.getByLabel('Recording project',{exact:true}).selectOption('project');
 await page.getByLabel('Tags',{exact:true}).fill('warranty');
 await page.getByLabel('Summary for Vault search',{exact:true}).fill('Discussed replacement parts.');
 await page.getByRole('button',{name:'Save filing and metadata'}).click();
 await expect.poll(()=>f.records.get('recording-fixture-0001').metadata.project).toBe('project');
 await page.getByRole('button',{name:'Draft a summary request'}).click();
 await expect(page.locator('#draft')).toHaveValue(/summarize this recording/);
 expect(f.calls.filter(r=>r.method==='POST').every(r=>r.path==='/api/recordings/action')).toBe(true);
});
