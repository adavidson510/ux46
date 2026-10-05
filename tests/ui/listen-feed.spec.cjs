const {test,expect}=require('@playwright/test');
const fs=require('node:fs'),path=require('node:path');
const root=process.env.UX46_TEST_UI_ROOT||path.resolve(__dirname,'../..');
const message=(id,text,phase='commentary',type='agentMessage')=>({id,text,phase,type,turn_id:'turn-1'});
async function fixture(context){
 let items=[message('old','Earlier answer','final_answer')],active=null,fail=false;const calls=[];
 const wav=Buffer.alloc(44+8000*2*30);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
 await context.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());if(u.origin!=='https://fixture.test')return route.abort();
  if(u.pathname.startsWith('/api/')){
   calls.push({path:u.pathname,method:req.method(),body:req.method()==='POST'?req.postDataJSON():null});
   if(u.pathname.endsWith('/speak')){if(fail)return route.fulfill({status:503,json:{message:'Voice unavailable'}});return route.fulfill({json:{audio_url:'/api/audio/test.wav',complete:true}});}
   if(u.pathname==='/api/audio/test.wav'){
    const range=req.headers()['range'];const match=range&&/^bytes=(\d+)-(\d*)$/.exec(range);
    if(match){const start=Number(match[1]),end=match[2]?Math.min(Number(match[2]),wav.length-1):wav.length-1;
      return route.fulfill({status:206,body:wav.subarray(start,end+1),contentType:'audio/wav',headers:{'Accept-Ranges':'bytes','Content-Range':`bytes ${start}-${end}/${wav.length}`}});}
    return route.fulfill({body:wav,contentType:'audio/wav',headers:{'Accept-Ranges':'bytes'}});
   }
   if(u.pathname.endsWith('/history'))return route.fulfill({json:{items:items.slice().reverse(),complete:true}});
   if(u.pathname==='/api/room/example/at')return route.fulfill({json:{id:'example/at',controllable:true,native:{thread_id:'thread-1',active_turn:active}}});
   return route.fulfill({json:{}});
  }
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);if(!fs.existsSync(file))return route.fulfill({status:404});let body=fs.readFileSync(file,'utf8');
  if(name==='index.html')body=body.replace(/<script src="\/(?!app.js|listen-feed.js)[^"]+"><\/script>/g,'');
  if(name==='app.js')body=body.replace('\nboot();','\n/* fixture boot */');
  return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 async function open(page){await page.goto('https://fixture.test/');await page.evaluate(()=>{Object.assign(state,{agent:DEFAULT_AGENT,room:'example/at',csrf:'fixture',detail:{id:'example/at',title:'AT feed',controllable:true,native:{thread_id:'thread-1',active_turn:null}},voice:{enabled:true,preferred:''}});applyShell();window.dispatchEvent(new Event('ux46-room'));});}
 async function start(page){await page.getByRole('button',{name:'Listen',exact:true}).click();await expect(page.locator('.listen-feed-status')).toHaveText('Waiting for new replies');}
 return {open,start,calls,set:(rows,turn=null)=>{items=rows;active=turn;},fail:v=>{fail=v;},writes:()=>calls.filter(c=>c.method==='POST'),wav};
}
test('live reading skips history and tools, waits for complete text, queues in order and stays pinned',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);expect(f.writes()).toHaveLength(0);
 f.set([message('old','Earlier answer','final_answer'),message('a','Work is underway'),message('t','shell secrets','', 'commandExecution'),message('b','Still typ','final_answer')],'turn-1');
 await page.evaluate(()=>UX46ListenFeed.poll());await expect.poll(()=>f.writes().length).toBe(1);expect(f.writes()[0].body.item_id).toBe('a');
 await page.evaluate(()=>{state.room='other/project';state.agent='other';renderStream();});
 f.set([message('old','Earlier answer','final_answer'),message('a','Work is underway'),message('t','shell secrets','','commandExecution'),message('b','Finished answer','final_answer')]);
 await page.evaluate(()=>UX46ListenFeed.poll());await expect(page.locator('.listen-feed-status')).toContainText('1 waiting');
 await page.evaluate(()=>{const a=document.querySelector('audio');a.pause();a.dispatchEvent(new Event('ended'));});await expect.poll(()=>f.writes().length).toBe(2);expect(f.writes()[1].body.item_id).toBe('b');expect(f.writes().every(x=>x.path==='/api/room/example/at/speak')).toBe(true);
 await page.evaluate(()=>UX46ListenFeed.poll());expect(f.writes()).toHaveLength(2);
 await page.getByRole('button',{name:'Stop listening',exact:true}).click();await expect(page.locator('.listen-feed')).toBeHidden();await expect(page.locator('audio')).toHaveCount(0);
});
test('mute holds playback, local one-off pauses feed, retry keeps the failed item, and controls fit a phone',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);f.fail(true);f.set([message('old','Earlier answer'),message('a','New response')]);
 await page.evaluate(()=>UX46ListenFeed.poll());await expect(page.locator('.listen-feed-status')).toContainText('Could not prepare');
 f.fail(false);await page.getByRole('button',{name:'Play / retry',exact:true}).click();await expect.poll(()=>f.writes().length).toBe(2);
 await page.getByRole('button',{name:'Mute',exact:true}).click();expect(await page.locator('audio').evaluate(a=>a.paused)).toBe(true);
 await page.getByRole('button',{name:'Unmute',exact:true}).click();await page.evaluate(()=>UX46ListenFeed.pause());await expect(page.getByRole('button',{name:'Unmute',exact:true})).toBeVisible();
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>applyShell());await expect(page.locator('.listen-feed')).toBeHidden();await page.locator('#btnListenFeed').click();const box=await page.locator('.listen-feed').boundingBox();expect(box.x).toBeGreaterThanOrEqual(0);expect(box.x+box.width).toBeLessThanOrEqual(390);
 await page.screenshot({path:test.info().outputPath('listen-feed-phone.png')});await page.getByRole('button',{name:'Stop listening'}).click();
});
test('only one window listens and stopping during synthesis discards late audio',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);
 const other=await context.newPage();await f.open(other);await other.getByRole('button',{name:'Listen',exact:true}).click();await expect(other.locator('.listen-feed')).toBeHidden();
 let release;const held=new Promise(r=>release=r);let started;const requested=new Promise(r=>started=r);
 await page.route('**/speak',async route=>{started();await held;await route.fulfill({json:{audio_url:'/api/audio/test.wav'}});});
 f.set([message('old','Earlier'),message('new','Latest')]);await page.evaluate(()=>{void UX46ListenFeed.poll();});await requested;
 await page.getByRole('button',{name:'Stop listening'}).click();release();await expect(page.locator('audio')).toHaveCount(0);await expect(page.locator('.listen-feed')).toBeHidden();await other.close();
});
test('browser autoplay refusal offers a direct play button without skipping the response',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);
 await page.evaluate(()=>{window.originalPlay=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=()=>Promise.reject(Error('autoplay blocked'));});
 f.set([message('old','Earlier'),message('new','Read this')]);await page.evaluate(()=>UX46ListenFeed.poll());await expect(page.getByRole('button',{name:'Play / retry',exact:true})).toBeVisible();expect(f.writes()).toHaveLength(1);
 await page.evaluate(()=>{HTMLMediaElement.prototype.play=window.originalPlay;});await page.getByRole('button',{name:'Play / retry',exact:true}).click();await expect(page.locator('.listen-feed-status')).toHaveText('Reading');expect(f.writes()).toHaveLength(1);await page.getByRole('button',{name:'Stop listening'}).click();
});
test('desktop player hands its reply, queue and position to a real window and docks back',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);
 f.set([message('old','Earlier'),message('a','First complete reply'),message('b','Second complete reply')]);
 await page.evaluate(()=>UX46ListenFeed.poll());await expect.poll(()=>f.writes().length).toBe(1);
 await expect.poll(()=>page.locator('audio').evaluate(a=>a.readyState)).toBeGreaterThan(0);
 await page.locator('audio').evaluate(a=>{a.pause();a.currentTime=7;});
 await expect.poll(()=>page.locator('audio').evaluate(a=>a.currentTime)).toBeGreaterThanOrEqual(7);
 const popupWait=page.waitForEvent('popup');await page.getByRole('button',{name:'Pop out listening'}).click();const pop=await popupWait;
 await expect(pop.locator('.listen-feed')).toBeVisible();await expect(page.locator('audio')).toHaveCount(0);
 await expect(pop.locator('.listen-feed-text')).toContainText('First complete reply');
 await expect.poll(()=>pop.locator('audio').evaluate(a=>a.currentTime)).toBeGreaterThanOrEqual(7);
 expect(f.writes()).toHaveLength(1);
 await pop.getByRole('button',{name:'Mute',exact:true}).click();
 await pop.screenshot({path:test.info().outputPath('listen-window.png')});
 await pop.getByRole('button',{name:'Dock back',exact:true}).click();
 await expect(page.locator('audio')).toHaveCount(1);await expect(page.getByRole('button',{name:'Unmute',exact:true})).toBeVisible();
 await expect(page.locator('.listen-feed-status')).toContainText('1 waiting');
 await page.getByRole('button',{name:'Unmute',exact:true}).click();
 await page.locator('audio').evaluate(a=>{a.pause();a.dispatchEvent(new Event('ended'));});
 await expect.poll(()=>f.writes().length).toBe(2);expect(f.writes()[1].body.item_id).toBe('b');
 const dock=await page.locator('.listen-dock').boundingBox(),bar=await page.locator('.listen-feed').boundingBox();
 expect(Math.abs(dock.y-bar.y)).toBeLessThan(15);
 await page.screenshot({path:test.info().outputPath('listen-docked.png')});
 await page.getByRole('button',{name:'Stop listening'}).click();
});
test('satellite keeps reading after main workspace closes, without attaching or sending',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);
 const wait=page.waitForEvent('popup');await page.getByRole('button',{name:'Pop out listening'}).click();const pop=await wait;
 await expect(pop.locator('.listen-feed')).toBeVisible();await page.close();
 f.set([message('old','Earlier'),message('new','Independent reply')]);
 await pop.evaluate(()=>UX46ListenFeed.poll());await expect.poll(()=>f.writes().length,{timeout:10000}).toBe(1);
 await expect(pop.locator('.listen-feed-text')).toHaveText('Independent reply');
 expect(f.writes().every(c=>c.path.endsWith('/speak'))).toBe(true);
 await pop.getByRole('button',{name:'Stop listening'}).click();await pop.close();
});
test('blocked popup keeps playback here and closing a satellite returns its feed',async({context,page})=>{
 const f=await fixture(context);await f.open(page);await f.start(page);
 await page.evaluate(()=>{window.realOpen=window.open;window.open=()=>null;});
 await page.getByRole('button',{name:'Pop out listening'}).click();await expect(page.locator('audio')).toHaveCount(1);
 await page.evaluate(()=>{window.open=window.realOpen;});
 const wait=page.waitForEvent('popup');await page.getByRole('button',{name:'Pop out listening'}).click();const pop=await wait;
 await expect(pop.locator('.listen-feed')).toBeVisible();await expect(page.locator('audio')).toHaveCount(0);
 await pop.close();await expect(page.locator('audio')).toHaveCount(1);
 await page.getByRole('button',{name:'Stop listening'}).click();
});

test('phone listening stays docked while reading and typing; controls dismiss without stopping audio',async({context,page})=>{
 await page.setViewportSize({width:390,height:844});
 const f=await fixture(context);await f.open(page);await f.start(page);
 f.set([message('old','Earlier'),message('a','A complete progress update')]);
 await page.evaluate(()=>UX46ListenFeed.poll());
 await expect(page.locator('.listen-feed-status')).toHaveText('Reading');
 await expect(page.locator('.listen-feed')).toBeHidden();
 await page.evaluate(()=>{window.readingAudio=document.querySelector('audio');});
 await page.locator('#draft').fill('Keep working while I listen');
 await page.screenshot({path:test.info().outputPath('phone-listening-docked.png')});
 await page.locator('#btnListenFeed').click();
 await expect(page.locator('.listen-feed')).toBeVisible();
 await expect(page.getByRole('button',{name:'Pop out listening'})).toBeHidden();
 await expect(page.locator('#btnListenFeed')).toHaveAttribute('aria-expanded','true');
 await page.screenshot({path:test.info().outputPath('phone-listening-expanded.png')});
 await page.getByRole('button',{name:'Hide listening controls'}).click();
 await expect(page.locator('.listen-feed')).toBeHidden();
 await page.locator('#btnListenFeed').click();
 await page.locator('#draft').click();
 await expect(page.locator('.listen-feed')).toBeHidden();
 expect(await page.evaluate(()=>document.querySelector('audio')===window.readingAudio&&!window.readingAudio.paused)).toBe(true);
 await expect(page.locator('#draft')).toHaveValue('Keep working while I listen');
 await page.evaluate(()=>{state.room='other/room';window.dispatchEvent(new Event('ux46-room'));});
 await page.locator('#btnListenFeed').click();
 await expect(page.locator('.listen-feed strong')).toHaveText('Listening · AT feed');
 await page.getByRole('button',{name:'Stop listening',exact:true}).click();
 await expect(page.locator('audio')).toHaveCount(0);
 expect(f.writes()).toHaveLength(1);
});
