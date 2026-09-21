const {test,expect}=require('@playwright/test');
const fs=require('node:fs');const path=require('node:path');const root=path.resolve(__dirname,'../..');
const empty={sources:[],routes:[],experiments:[],results:[],metrics:{interested:0,awaiting_review:0,helped:0,failed:0,unknown:0},coverage:'Reported outcomes'};
async function fixture(page,work=empty,mail={}){
 const writes=[];
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());if(u.origin!=='https://fixture.test')return route.abort();
  if(u.pathname.startsWith('/api/')){
   if(route.request().method()==='POST')writes.push(route.request().postDataJSON());
   if(u.pathname==='/api/work/view')return route.fulfill({json:work});
   if(u.pathname==='/api/email/assistant')return route.fulfill({json:mail});
   return route.fulfill({json:{}});
  }
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);
  if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
  let body=fs.readFileSync(file);if(name==='index.html')body=body.toString().replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g,'');
  if(name==='app.js')body=body.toString().replace('\nboot();','\n/* synthetic */');
  return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 await page.goto('https://fixture.test/');
 await page.evaluate(()=>{Object.assign(__atlas.state,{agent:'local',room:'demo/room',csrf:'fixture'});__atlas.state.rooms.set('demo/room',{id:'demo/room',title:'Demo'});window.__ux46modules={email:true};});
 return writes;
}
test('room result pairs artifact, changes and check limits; other room clears it',async({page})=>{
 const data={...empty,results:[{id:'r',version:1,title:'Timer buttons',summary:'Larger touch targets',artifact_version:'v1',reporter:'Fixture',url:'https://example.com/timer',changed:'Only timer buttons',checked:'Narrow layout',unchecked:'Physical phone',evidence:'Fixture receipt',article:''}]};
 await fixture(page,data);await page.addScriptTag({url:'/work.js'});
 await page.evaluate(()=>{document.querySelector('#dock').hidden=false;document.querySelector('#panelBoard').hidden=false;});
 await expect(page.getByRole('link',{name:'Open current result'})).toHaveAttribute('href','https://example.com/timer');
 await page.getByText('What changed and what was checked').click();await expect(page.locator('#roomWork')).toContainText('Physical phone');
 await page.route('**/api/work/view?**',r=>r.fulfill({json:empty}));
 await page.evaluate(()=>{__atlas.state.room='demo/other';window.dispatchEvent(new Event('ux46-room'));});
 await expect(page.locator('#roomWork')).not.toContainText('Timer buttons');
});
test('email brief and draft review are separate; edits disable Send',async({page})=>{
 const d={id:'d1',account:'first',from:'owner@example.com',thread:'t1',to:'person@example.net',cc:'',subject:'Re: Question',body:'Prepared reply',note:'Check before sending',incoming_attachments:[],state:'draft',revision:1};
 const mail={configured:true,settings:{enabled:true,hour:5,minute:0,timezone:'America/Los_Angeles',revision:1},preferences:{text:'',revision:0},filing_policy:{enabled:false,archive_routine:false,mark_read:false},briefs:[{id:'b1',at:Date.now()/1000,summary:'One email needs your reply.',items:[{subject:'Question',summary:'A reply is needed.',gmail_url:'https://mail.google.com/',draft_id:'d1'}],coverage:'Email only',coverage_complete:true,seen:false}],drafts:[d],unread:1};
 const writes=await fixture(page,empty,mail);
 await page.evaluate(()=>{document.querySelector('#viewEmail').hidden=false;});
 await page.addScriptTag({url:'/workspace.js'});
 await expect(page.locator('#emailAssistant')).toContainText('05:00');
 await page.getByRole('button',{name:'Review prepared reply'}).click();
 await expect(page.getByRole('button',{name:'Send this reply'})).toBeDisabled();
 await page.route('**/api/email/assistant-action',async route=>{const args=route.request().postDataJSON();writes.push(args);await route.fulfill({json:{...d,body:args.body,revision:2,review_hash:'exact-review'}});});
 await page.getByLabel('Reply',{exact:true}).fill('My edited reply');await page.getByRole('button',{name:'Save and review'}).click();
 await expect(page.getByRole('button',{name:'Send this reply'})).toBeEnabled();
 await page.getByLabel('Reply',{exact:true}).fill('Another edit');await expect(page.getByRole('button',{name:'Send this reply'})).toBeDisabled();
 expect(writes.some(x=>x.action==='send')).toBe(false);await page.screenshot({path:test.info().outputPath('email-draft-review.png')});
});
test('dismissed Signals can be restored and never imply a tested outcome',async({page})=>{
 let s={id:'tell-post1',version:1,title:'Try clearer controls',kind:'tell',disposition:'active',interest:false};const all={...empty,sources:[s]};
 await fixture(page,all);await page.addScriptTag({url:'/work.js'});
 await page.route('**/api/work/action',async route=>{const a=route.request().postDataJSON();if(a.action==='dismiss')s={...s,version:s.version+1,disposition:'dismissed'};if(a.action==='restore')s={...s,version:s.version+1,disposition:'active'};all.sources=[s];await route.fulfill({json:s});});
 await page.evaluate(()=>{document.querySelector('#viewTell').hidden=false;const article=document.createElement('article');article.id='syntheticSignal';document.querySelector('#tellBody').append(article);return __work.signal({id:'post1',board:'working-better',title:'Try clearer controls',human_body:'Try it once',sources:[]},article);});
 await expect(page.locator('#syntheticSignal')).toContainText('not yet tested');await page.locator('#syntheticSignal').getByRole('button',{name:'Dismiss',exact:true}).click();await expect(page.locator('#syntheticSignal')).toBeHidden();
 await page.getByText('Saved, dismissed and tried',{exact:true}).click();await page.getByRole('button',{name:'Restore',exact:true}).click();await expect(page.locator('#syntheticSignal')).toBeVisible();
});
