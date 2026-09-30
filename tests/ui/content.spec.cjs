const {test,expect}=require('@playwright/test');
const fs=require('node:fs'),path=require('node:path');
const root=process.env.UX46_TEST_UI_ROOT||path.resolve(__dirname,'../..');
async function fixture(context){
 let saved={id:'article-test-01',agent:'keel',room:'example/article',kind:'markdown',revision:1,title:'Draft 2',payload:{text:'# Original\n\nA sentence to edit.'},at:1,history:[{revision:1,at:1}]};
 const chart={id:'chart-test-01',agent:'keel',room:'example/article',kind:'chart',revision:1,title:'Comparison fixture',payload:{chart:{type:'bar',labels:['Monday','Tuesday'],values:[1,3],unit:'points'},source:'Synthetic fixture',snapshot:true},at:1,history:[]};
 let fail=false;const calls=[];
 await context.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());if(u.origin!=='https://fixture.test')return route.abort();
  if(u.pathname.startsWith('/api/')){
   calls.push({path:u.pathname,method:req.method()});
   if(u.pathname==='/api/bootstrap')return route.fulfill({json:{csrf:'fixture'}});
   if(u.pathname==='/api/content/view'){
    const id=u.searchParams.get('id');return route.fulfill({json:id?(id===chart.id?chart:saved):{items:[saved,chart]}});
   }
   if(u.pathname==='/api/content/action'){
    if(fail)return route.fulfill({status:503,json:{message:'Synthetic unavailable'}});
    const a=req.postDataJSON();if(a.base_revision!==saved.revision)return route.fulfill({status:409,json:{message:'Newer revision'}});
    saved={...saved,title:a.title,payload:a.payload,revision:saved.revision+1};saved.history=[{revision:saved.revision,at:2},...saved.history];return route.fulfill({json:saved});
   }
   return route.fulfill({json:{}});
  }
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);
  if(!fs.existsSync(file))return route.fulfill({status:404});let body=fs.readFileSync(file,'utf8');
  if(name==='index.html')body=body.replace(/<script src="\/(?!app.js|content.js|workspace.js|recordings.js)[^"]+"><\/script>/g,'');
  if(name==='app.js')body=body.replace('\nboot();','\n/* fixture boot */');
  return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 async function main(page){await page.goto('https://fixture.test/');await page.evaluate(()=>{state.csrf='fixture';state.agent='keel';state.room='example/article';state.agents=[{id:'keel',kind:'local'}];state.ui.dock='board';applyShell();});await page.evaluate(()=>UX46Content.open({agent:'keel',room:'example/article',id:'article-test-01'}));}
 return {main,calls,saved:()=>saved,fail:v=>{fail=v;},chart};
}
test('Canvas save persists; popout shares identity and never opens a native session',async({context,page})=>{
 const f=await fixture(context);await f.main(page);
 await page.getByLabel('Markdown source',{exact:true}).fill('# Human edit\n\nThe saved sentence.');await page.getByRole('button',{name:'Save',exact:true}).click();
 await expect(page.locator('.content-status')).toHaveText('Saved · revision 2');expect(f.saved().payload.text).toContain('Human edit');
 const opened=context.waitForEvent('page');await page.getByRole('button',{name:'Pop out',exact:true}).click();const popup=await opened;
 await expect(popup.getByLabel('Markdown source',{exact:true})).toHaveValue(/Human edit/);
 await page.evaluate(()=>{state.room='other/room';return UX46Content.poll();});
 await expect(popup.getByLabel('Markdown source',{exact:true})).toHaveValue(/Human edit/);
 await popup.reload();await expect(popup.getByLabel('Markdown source',{exact:true})).toHaveValue(/Human edit/);
 expect(f.calls.filter(c=>c.method==='POST').every(c=>c.path==='/api/content/action')).toBe(true);
 expect(f.calls.some(c=>c.path.includes('/attach')||c.path.includes('/history')||c.path.includes('/submit'))).toBe(false);
 await popup.screenshot({path:test.info().outputPath('scratchpad-popout.png')});
});
test('conflicting and failed saves retain text; dirty polling never moves the editor',async({context,page})=>{
 const f=await fixture(context);await f.main(page);
 const second=await context.newPage();await second.goto('https://fixture.test/?content=article-test-01&agent=keel&room=example%2Farticle');
 await expect(second.getByLabel('Markdown source',{exact:true})).toBeVisible();
 await second.getByLabel('Markdown source',{exact:true}).fill('Second window unsaved');
 await second.evaluate(()=>UX46Content.open({agent:'keel',room:'example/article',id:'article-test-01'}));
 await expect(second.getByLabel('Markdown source',{exact:true})).toHaveValue('Second window unsaved');
 await page.getByLabel('Markdown source',{exact:true}).fill('First window saved');await page.getByRole('button',{name:'Save',exact:true}).click();
 await expect(page.locator('.content-status')).toHaveText('Saved · revision 2');
 await second.evaluate(()=>UX46Content.poll());await expect(second.getByLabel('Markdown source',{exact:true})).toHaveValue('Second window unsaved');
 await second.getByRole('button',{name:'Save',exact:true}).click();await expect(second.locator('.content-conflict')).toContainText('First window saved');
 await expect(second.getByLabel('Markdown source',{exact:true})).toHaveValue('Second window unsaved');
 await second.getByRole('button',{name:'Keep my text and use this revision as the base'}).click();f.fail(true);
 await second.getByRole('button',{name:'Save',exact:true}).click();await expect(second.locator('.content-status')).toContainText('Save failed');
 await expect(second.getByLabel('Markdown source',{exact:true})).toHaveValue('Second window unsaved');
 second.on('dialog',d=>d.accept());await second.reload();await expect(second.getByLabel('Markdown source',{exact:true})).toHaveValue('Second window unsaved');
 f.fail(false);await second.getByRole('button',{name:'Save',exact:true}).click();await expect(second.locator('.content-status')).toHaveText('Saved · revision 3');
 await page.evaluate(()=>UX46Content.poll());await expect(page.getByLabel('Markdown source',{exact:true})).toHaveValue('Second window unsaved');
});
test('chart uses the same popout; blocked popup fallback and mobile remain usable',async({context,page})=>{
 await fixture(context);await page.setViewportSize({width:390,height:844});
 await page.goto('https://fixture.test/?content=chart-test-01&agent=keel&room=example%2Farticle');
 await expect(page.getByRole('heading',{name:'Comparison fixture'})).toBeVisible();await expect(page.locator('.content-body')).toContainText('Snapshot · Synthetic fixture');
 await expect(page.getByRole('img',{name:/Monday 1 points/})).toBeVisible();
 await expect(page.getByRole('button',{name:'Save',exact:true})).toHaveCount(0);
 await page.evaluate(()=>{window.open=()=>null;});await page.getByRole('button',{name:'Pop out',exact:true}).click();
 await expect(page.getByRole('link',{name:'Open in a new tab'})).toHaveAttribute('href',/content=chart-test-01/);
 await expect(page.getByRole('link',{name:'Open source conversation'})).toBeVisible();
 await page.screenshot({path:test.info().outputPath('chart-popout-phone.png')});
});
test('Markdown HTML is displayed as text, without script execution',async({context,page})=>{
 await fixture(context);await page.goto('https://fixture.test/?content=article-test-01&agent=keel&room=example%2Farticle');
 await page.getByLabel('Markdown source',{exact:true}).fill('<img src=x onerror="window.bad=true"><script>window.bad=true</script>');
 await page.getByRole('button',{name:'Preview',exact:true}).click();expect(await page.evaluate(()=>window.bad)).toBeUndefined();await expect(page.locator('.content-preview img')).toHaveCount(0);
});

test('saved content descriptors open the owned item and reject a foreign room',async({context,page})=>{
 const f=await fixture(context);await f.main(page);
 await page.evaluate(()=>{const host=document.createElement('div');host.id='fixtureCard';host.append(markdownFragment('```ux46-content\n{"id":"article-test-01","title":"Existing draft"}\n```'));document.body.append(host);});
 await expect(page.locator('#fixtureCard').getByRole('button',{name:'Open in Canvas'})).toBeVisible();
 await page.locator('#fixtureCard').getByRole('button',{name:'Open in Canvas'}).click();await expect(page.getByLabel('Markdown source',{exact:true})).toHaveValue(/Original/);
 expect(await page.evaluate(()=>UX46Content.card({id:'article-test-01',room:'other/room'}))).toBeNull();
});
