const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const html = readFileSync(require('node:path').join(__dirname,'../../app/static/index.html'),'utf8');
function element(dataset={}) {
  const classes=new Set();
  return {dataset, value:'', innerHTML:'', textContent:'', disabled:false, attributes:{}, handlers:{}, style:{},
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle(x,on){on ??= !classes.has(x); on?classes.add(x):classes.delete(x);}},
    setAttribute(k,v){this.attributes[k]=String(v);},removeAttribute(k){delete this.attributes[k];},getAttribute(k){return this.attributes[k];},
    addEventListener(k,f){this.handlers[k]=f;}, querySelector(){return null;},querySelectorAll(){return [];},focus(){this.focused=true;},remove(){},click(){this.clicked=true;}
  };
}
function harness(options={}){
  const nodes=new Map();
  const nav=['list','search','edit','bootstrap','trash','keys','sync','branch','mcp'].map(v=>element({v}));
  const projects=['','real','（未归项目）','__none__'].map(proj=>element({proj:encodeURIComponent(proj)}));
  const get=s=>{if(!nodes.has(s)) nodes.set(s,element());return nodes.get(s);};
  const storage=new Map(options.token?[['mem_token',options.token]]:[]);const timers=new Map();let clock=0;
  const ctx={console,URLSearchParams,encodeURIComponent,decodeURIComponent,Date,Promise,
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    location:{pathname:'/',origin:'http://test'},navigator:{},window:{matchMedia:()=>({matches:false})},
    document:{querySelector:get,querySelectorAll:s=>s==='.navitem'?[...nav,...projects]:[],documentElement:element(),body:{appendChild(){}},handlers:{},addEventListener(k,f){this.handlers[k]=f;},createElement:()=>({...element(),lastChild:element()})},
    setTimeout:(f)=>{timers.set(++clock,f);return clock;},clearTimeout:id=>timers.delete(id),
    fetch:options.fetch|| (async()=>{throw Error('unexpected fetch');}),confirm:()=>false,prompt:()=>null
  };
  vm.createContext(ctx);
  for(const m of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(m[1],ctx);
  ctx.state=vm.runInContext('S',ctx);
  return {ctx,get,nav,projects,timers,storage,run:s=>vm.runInContext(s,ctx)};
}
test('failed branch save cannot display a success toast',async()=>{
  const {ctx,get}=harness();const messages=[];
  ctx.toast=(msg,error)=>messages.push({msg,error});
  ctx.loadEditBranches=async()=>{};
  await ctx.branchSaveResult({ok:false,branch:'draft',error:'隔离模拟：提交失败'});
  assert.ok(messages.some(x=>x.error));
  assert.ok(messages.every(x=>!String(x.msg).includes('已写入')));
  assert.match(get('#brhint').textContent,/失败/);
});

test('overview and sidebar use full project aggregates, not a capped document page',async()=>{
  const {ctx,get}=harness();const urls=[];
  ctx.api=async url=>{urls.push(url);return {totalDocuments:307,projects:[{project:'real',count:305,latestTitle:'Newest'},{project:'',count:2,latestTitle:'Loose'}],libraries:['main','other']};};
  await ctx.loadLibs();await ctx.loadList();
  assert.ok(urls.every(u=>u.startsWith('/api/v1/projects')));
  assert.equal(ctx.state.docTotal,307);
  assert.match(get('#lres').innerHTML,/305 篇/);
  assert.match(get('#tstat').textContent,/307/);
  assert.match(get('#libs').innerHTML,/data-proj=""/);
});

test('unassigned pagination uses unassigned=true and server totals; literals stay literal',async()=>{
  const {ctx,get}=harness();const urls=[];
  ctx.api=async url=>{urls.push(url);return {total:73,items:[{id:urls.length,title:'Doc',type:'fact',tags:[]}]};};
  await ctx.filterProj('');
  let q=new URL('http://test'+urls[0]).searchParams;
  assert.equal(q.get('unassigned'),'true');assert.equal(q.get('limit'),'50');assert.equal(q.get('offset'),'0');
  assert.equal(q.has('project'),false);assert.match(get('#tstat').textContent,/73/);
  await ctx.loadList(undefined,undefined,true);
  q=new URL('http://test'+urls[1]).searchParams;assert.equal(q.get('offset'),'1');assert.equal(ctx.state.docs.length,2);
  await ctx.filterProj('（未归项目）');q=new URL('http://test'+urls[2]).searchParams;
  assert.equal(q.get('project'),'（未归项目）');assert.equal(q.has('unassigned'),false);
});

test('loading, empty, retry and stale-response protection render actual request states',async()=>{
  const {ctx,get}=harness();const pending=[];ctx.state.projFilter='real';
  ctx.api=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
  const old=ctx.loadList();assert.match(get('#lres').innerHTML,/加载/);
  const current=ctx.loadList();pending[1].resolve({total:0,items:[]});await current;
  assert.match(get('#lres').innerHTML,/没有文档/);
  pending[0].resolve({total:1,items:[{id:1,title:'Stale',tags:[]}]});await old;
  assert.doesNotMatch(get('#lres').innerHTML,/Stale/);
  const failed=ctx.loadList();pending[2].reject(Error('HTTP 503'));await failed;
  assert.match(get('#lres').innerHTML,/重试/);assert.match(get('#lres').innerHTML,/HTTP 503/);
});

test('title input debounces and invalidates an in-flight result immediately',async()=>{
  const {ctx,get,timers}=harness();ctx.state.projFilter='real';let resolve;
  ctx.api=()=>new Promise(r=>resolve=r);
  const old=ctx.loadList();get('#lq').value='new';
  assert.equal(typeof ctx.scheduleList,'function');ctx.scheduleList();ctx.scheduleList();
  assert.equal(timers.size,1);resolve({total:1,items:[{id:1,title:'Stale',tags:[]}]});await old;
  assert.doesNotMatch(get('#lres').innerHTML,/Stale/);
  let calls=0;ctx.loadList=async()=>{calls++;};for(const f of timers.values()) f();assert.equal(calls,1);
});

test('navigation cancels delayed input so it cannot run after leaving the view',async()=>{
  const {ctx,timers}=harness();assert.equal(typeof ctx.scheduleList,'function');
  ctx.scheduleList();await ctx.nav('search');assert.equal(timers.size,0);
});

test('boot retains credentials on 503 and defaults to the list on success',async()=>{
  const {ctx,get,storage}=harness();ctx.state.token='good';storage.set('mem_token','good');
  ctx.fetch=async()=>({status:503,ok:false,text:async()=>'{"detail":"temporarily unavailable"}'});
  await ctx.boot();assert.equal(ctx.state.token,'good');assert.equal(storage.get('mem_token'),'good');
  assert.match(get('#lerr').textContent,/temporarily unavailable/);
  let destination;ctx.api=async()=>({username:'tester'});ctx.loadLibs=async()=>{};ctx.renderMcp=async()=>{};ctx.nav=async v=>{destination=v;};
  await ctx.boot();assert.equal(destination,'list');
  ctx.fetch=async()=>({status:401,ok:false,text:async()=>''});
  // Reload the original API function, rather than testing the success stub.
  const original=harness();original.ctx.state.token='expired';original.ctx.fetch=ctx.fetch;
  await assert.rejects(original.ctx.api('/api/v1/auth/me'),/登录已过期/);assert.equal(original.ctx.state.token,'');
});

test('mobile drawer state and focus follow open, Escape, and navigation',async()=>{
  const {ctx,get}=harness();assert.equal(typeof ctx.toggleNav,'function');
  ctx.toggleNav(true);assert.equal(get('#nav-toggle').attributes['aria-expanded'],'true');
  assert.equal(get('#appview').classList.contains('nav-open'),true);
  let prevented=false;ctx.document.handlers.keydown({key:'Escape',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(get('#nav-toggle').focused,true);
  ctx.toggleNav(true);ctx.loadList=async()=>{};await ctx.nav('list');
  assert.equal(get('#appview').classList.contains('nav-open'),false);
});

test('real markup wires loading/pagination, keyboard targets, responsive drawer and debounce',()=>{
  const {ctx,get}=harness();
  for(const id of ['list-title','list-subtitle','lmore','lpage-error','nav-toggle','nav-shade','page-title']) assert.ok(html.includes(`id="${id}"`),id);
  assert.match(html,/oninput="[^\"]*scheduleList\(/);
  assert.match(html,/@media\s*\(max-width:\s*760px\)/);
  assert.match(html,/@media\s*\(hover:\s*none\)/);
  assert.match(html,/id="ltype"[^>]*onchange=/);
  ctx.renderFlat([{id:1,title:'Accessible',type:'fact',tags:[]}]);
  assert.match(get('#lres').innerHTML,/tabindex="0"/);assert.match(get('#lres').innerHTML,/role="button"/);
  assert.doesNotMatch(html,/<div class="navitem(?: on)?" data-v=/);
});

test('trash paginates using server total and purge confirms/refuses stale expected_count',async()=>{
  const {ctx,get}=harness();const calls=[],prompts=[];
  ctx.api=async(url,opt)=>{calls.push({url,opt});if(opt)return {purged:307,files:307};return {total:307,revision:'trash-v1:fixture',items:[{id:calls.length,title:'Trash',tags:[]}]};};
  await ctx.loadTrash();assert.match(get('#tpurgetxt').textContent,/307/);
  await ctx.loadTrash(true);let q=new URL('http://test'+calls[1].url).searchParams;
  assert.equal(q.get('offset'),'1');assert.equal(q.get('limit'),'50');
  ctx.confirm=m=>{prompts.push(m);return true;};ctx.prompt=()=> '清空';
  ctx.loadTrash=async()=>{};ctx.loadLibs=async()=>{};
  await ctx.purgeTrash();assert.match(prompts[0],/307/);
  const post=calls.find(c=>c.opt?.method==='POST');assert.ok(post);
  assert.equal(new URL('http://test'+post.url).searchParams.get('expected_count'),'307');
  assert.equal(new URL('http://test'+post.url).searchParams.get('expected_revision'),'trash-v1:fixture');
  assert.ok(calls.some(c=>c.url==='/api/v1/trash/snapshot'));
});

test('project deletion confirms uncapped unfiltered server count and uses real project value',async()=>{
  const {ctx,get}=harness();const calls=[],prompts=[];
  ctx.state.lastLib='other';get('#lq').value='filter';get('#ltype').value='fact';
  ctx.api=async(url,opt)=>{calls.push({url,opt});return opt?{deleted:307}:{total:307,items:[]};};
  ctx.confirm=m=>{prompts.push(m);return true;};ctx.loadList=async()=>{};ctx.loadLibs=async()=>{};
  await ctx.delProject('real',2);
  assert.match(prompts[0],/307/);
  const q=new URL('http://test'+calls[0].url).searchParams;
  assert.equal(q.get('project'),'real');assert.equal(q.has('q'),false);assert.equal(q.has('type'),false);assert.equal(q.has('library'),false);
  assert.ok(calls.some(c=>c.opt?.method==='DELETE'));
});

test('query-based bulk delete preserves every literal project name and unassigned',async()=>{
  for(const name of ['__none__','.','..','a/b','未归项目','']){
    const {ctx}=harness();const calls=[];ctx.confirm=()=>true;
    ctx.api=async(url,opt)=>{calls.push({url,opt});return {total:1,items:[],deleted:1};};
    ctx.loadList=async()=>{};ctx.loadLibs=async()=>{};
    await ctx.delProject(name);
    const mutation=calls.find(c=>c.opt?.method==='DELETE');assert.ok(mutation);
    const url=new URL('http://t'+mutation.url);assert.equal(url.pathname,'/api/v1/projects');
    assert.equal(url.searchParams.get(name===''?'unassigned':'project'),name===''?'true':name);
  }
});

test('structured API errors expose the useful message and retain status',async()=>{
  const {ctx}=harness();
  ctx.fetch=async()=>({ok:false,status:409,text:async()=>JSON.stringify({detail:{message:'回收站数量已变化，请刷新后重新确认',actualCount:302}})});
  await assert.rejects(ctx.api('/api/v1/trash/empty'),e=>e.status===409&&e.message.includes('数量已变化'));
});

test('trash failure provides retry and disables purge; old responses cannot overwrite new',async()=>{
  const {ctx,get}=harness();const pending=[];
  ctx.api=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
  const old=ctx.loadTrash();const fresh=ctx.loadTrash();pending[1].resolve({total:0,items:[]});await fresh;
  pending[0].resolve({total:99,items:[{id:1,title:'StaleTrash'}]});await old;
  assert.doesNotMatch(get('#tres').innerHTML,/StaleTrash/);
  const fail=ctx.loadTrash();pending[2].reject(Error('offline'));await fail;
  assert.match(get('#tres').innerHTML,/重试/);assert.equal(get('#tpurge').disabled,true);
});

test('save displays durable Git failure warning without reporting a Git commit',async()=>{
  const {ctx,get}=harness();const messages=[];ctx.toast=(m,err)=>messages.push({m,err});ctx.loadLibs=async()=>{};
  get('#f-title').value='Memory';get('#f-content').value='Body';get('#f-library').value='main';
  ctx.api=async()=>({id:10,persistenceStatus:{disk:'saved',database:'saved',ok:false,retryable:true,git:{ok:false,status:'failed',error:'disk full <danger>'}}});
  await ctx.saveDoc();assert.equal(ctx.state.doc.id,10);
  assert.match(get('#save-status').textContent,/Git.*失败/);assert.match(get('#save-status').textContent,/disk full/);
  assert.equal(get('#save-status').attributes.role,'alert');assert.ok(messages.some(m=>m.err));
  assert.ok(messages.every(m=>!m.m.includes('已提交')));
  ctx.api=async()=>({id:10,persistenceStatus:{disk:'saved',database:'saved',ok:true,git:{ok:true,status:'committed',commit:'abcd'}}});
  await ctx.saveDoc();assert.doesNotMatch(get('#save-status').textContent,/失败/);assert.match(get('#save-status').textContent,/abcd/);
});

test('project navigation moves focus to a meaningful heading',async()=>{
  const {ctx,get}=harness();ctx.loadList=async()=>{};
  await ctx.filterProj('real');assert.equal(get('#list-title').focused,true);
  await ctx.nav('search');assert.equal(get('#page-title').focused,true);
});

test('purge refuses snapshots without a membership revision',async()=>{
  const {ctx}=harness();let writes=0;const messages=[];
  ctx.api=async(url,opt)=>{if(opt)writes++;return {total:1};};
  ctx.confirm=()=>true;ctx.prompt=()=> '清空';ctx.toast=m=>messages.push(m);
  await ctx.purgeTrash();
  assert.equal(writes,0);
  assert.match(messages[0],/无法确认回收站版本/);
});

test('purge cancellation sends no mutation; 409 refreshes count without retrying deletion',async()=>{
  for(const answer of [null,'wrong','清空']){
    const {ctx}=harness();const calls=[],messages=[];let refreshed=0;
    ctx.api=async(url,opt)=>{calls.push({url,opt});if(opt)throw Error('HTTP 409: count changed');return {total:301,revision:'trash-v1:fixture',items:[]};};
    ctx.confirm=()=>true;ctx.prompt=()=>answer;ctx.toast=m=>messages.push(m);ctx.loadTrash=async()=>{refreshed++;};
    await ctx.purgeTrash();
    assert.equal(calls.filter(c=>c.opt).length,answer==='清空'?1:0);
    if(answer==='清空'){assert.equal(refreshed,1);assert.match(messages[0],/409/);}
    else assert.equal(refreshed,0);
  }
});

test('loading more failure preserves existing documents and retries same offset',async()=>{
  const {ctx,get}=harness();ctx.state.projFilter='real';let n=0;const urls=[];
  ctx.api=async url=>{urls.push(url);n++;if(n===2)throw Error('offline');return {total:2,items:[{id:n,title:n===1?'First':'Second',tags:[]}]};};
  await ctx.loadList();await ctx.loadList(undefined,undefined,true);
  assert.match(get('#lres').innerHTML,/First/);assert.match(get('#lpage-error').innerHTML,/重试/);
  await ctx.loadList(undefined,undefined,true);
  assert.equal(new URL('http://t'+urls[1]).searchParams.get('offset'),'1');assert.equal(urls[1],urls[2]);
  assert.equal(ctx.state.docs.length,2);assert.equal(get('#lmore').classList.contains('hidden'),true);
});

test('rendered project identifiers cannot collide or inject markup',()=>{
  const {ctx,get}=harness();
  const names=['','未归项目','（未归项目）','__none__','__proto__','a/b','x\"<script>'];
  ctx.renderGrid(names.map(project=>({project,count:1,latestTitle:'<img src=x onerror=bad>'})));
  const output=get('#lres').innerHTML;
  for(const name of names)assert.ok(output.includes(`data-proj="${encodeURIComponent(name)}"`));
  assert.doesNotMatch(output,/<script>|<img/);assert.match(output,/&lt;img/);
});

test('SVG symbols resolve and all literal queried element ids exist in markup',()=>{
  const ids=[...html.matchAll(/\bid="([\w-]+)"/g)].map(m=>m[1]);
  const set=new Set(ids);assert.equal(set.size,ids.length,'duplicate ids');
  for(const m of html.matchAll(/href="#(i-[\w-]+)"/g)) assert.ok(set.has(m[1]),m[1]);
  for(const m of html.matchAll(/\$\('#([\w-]+)'\)/g))assert.ok(set.has(m[1]),`missing #${m[1]}`);
});

test('ordinary script startup with a saved token boots into all-documents without injected boot()',async()=>{
  const urls=[];
  const {ctx,get}=harness({token:'test-session',fetch:async(url)=>{
    urls.push(url);
    let data;
    if(url==='/api/v1/auth/me') data={username:'tester'};
    else if(url==='/api/v1/keys') data=[];
    else if(url.startsWith('/api/v1/projects')) data={totalDocuments:301,projects:[{project:'real',count:301,latestTitle:'Live title'}],libraries:['main']};
    else throw Error('unexpected request '+url);
    return {ok:true,status:200,text:async()=>JSON.stringify(data)};
  }});
  await new Promise(setImmediate);
  assert.equal(ctx.state.view,'list');assert.equal(ctx.state.projFilter,null);
  assert.match(get('#lres').innerHTML,/Live title/);assert.match(get('#tstat').textContent,/301/);
  assert.ok(urls.every(url=>url.startsWith('/api/v1/')));
});

module.exports={harness,html};

test('all-documents navigation clears every filter and selects exactly one item',async()=>{
  const {ctx,get,nav,projects}=harness();
  ctx.state.projFilter='real';ctx.state.lastLib='other';get('#lq').value='old';get('#ltype').value='fact';
  let calls=0;ctx.loadList=async()=>{calls++;};
  await ctx.nav('list');
  assert.equal(ctx.state.projFilter,null);
  assert.equal(ctx.state.lastLib,undefined);
  assert.equal(get('#lq').value,'');assert.equal(get('#ltype').value,'');assert.equal(calls,1);
  assert.deepEqual([...nav,...projects].filter(n=>n.classList.contains('on')).map(n=>n.dataset.v),['list']);
});

test('unassigned and literal placeholder-named projects retain distinct values and highlighting',async()=>{
  const {ctx,nav,projects}=harness();ctx.loadList=async()=>{};
  for(const name of ['', '（未归项目）','__none__']){
    await ctx.filterProj(name);
    assert.equal(ctx.state.projFilter,name);
    assert.deepEqual([...nav,...projects].filter(n=>n.classList.contains('on')).map(n=>n.dataset.proj),[encodeURIComponent(name)]);
  }
});
