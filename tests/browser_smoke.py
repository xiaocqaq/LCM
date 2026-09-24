"""Browser acceptance on a loopback test instance; never navigates production.
Usage: MEM_CJK_SHIM=/path/to/shim.js python tests/browser_smoke.py OUTPUT_DIR
Requires an existing CDP browser at MEM_CDP_URL (default localhost:9333).
The login used here is tests/preview_auth_fixture.py, not a real user account.
"""
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
from websockets.sync.client import connect

BASE = 'http://127.0.0.1:8660'
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
with urllib.request.urlopen(os.environ.get('MEM_CDP_URL','http://127.0.0.1:9333')+'/json/version') as r:
    endpoint = json.load(r)['webSocketDebuggerUrl']
ws = connect(endpoint, open_timeout=10, max_size=30_000_000, legacy=True)
seq=0; session=None; target=None; events=[]; results={}

def call(method, **params):
    global seq
    seq += 1
    payload={'id':seq,'method':method,'params':params}
    if session: payload['sessionId']=session
    ws.send(json.dumps(payload))
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        msg=json.loads(ws.recv(timeout=35))
        if msg.get('id')==seq:
            if 'error' in msg: raise RuntimeError((method,msg['error']))
            return msg.get('result',{})
        if msg.get('method')=='Runtime.exceptionThrown': events.append(msg)
    raise TimeoutError(method)

def ev(code):
    r=call('Runtime.evaluate',expression=code,returnByValue=True,awaitPromise=True)
    if 'exceptionDetails' in r: raise AssertionError(r['exceptionDetails'])
    return r.get('result',{}).get('value')

def wait(code, seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if ev(code): return
        time.sleep(.2)
    raise AssertionError('wait failed: '+code)

def click(selector):
    ev(f'document.querySelector({json.dumps(selector)}).click()')

def snapshot():
    return ev("""(()=>({first:document.querySelector('aside .navitem')?.textContent.trim(),
      selected:[...document.querySelectorAll('aside .navitem.on')].map(e=>e.textContent.trim()),
      view:S.view,project:S.projFilter,cards:document.querySelectorAll('.pcard').length,
      docs:document.querySelectorAll('#lres .doc').length,stat:document.querySelector('#tstat').textContent,
      listVisible:!document.querySelector('#v-list').classList.contains('hidden'),
      overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
      tokenPresent:!!localStorage.getItem('mem_token')}))()""")

def shot(name, view='list'):
    ev('sessionStorage.setItem("smoke_view",'+json.dumps(view)+')')
    call('Page.reload',ignoreCache=True)
    # Obscura font registrations are invalidated by eval: no evaluate between reload and capture.
    time.sleep(6)
    r=call('Page.captureScreenshot',format='png',captureBeyondViewport=False)
    (OUT/(name+'.png')).write_bytes(base64.b64decode(r['data']))

try:
    target=call('Target.createTarget',url='about:blank')['targetId']
    session=call('Target.attachToTarget',targetId=target,flatten=True)['sessionId']
    call('Page.enable');call('Runtime.enable')
    shim=os.environ.get('MEM_CJK_SHIM')
    if shim:
        call('Page.addScriptToEvaluateOnNewDocument',source=Path(shim).read_text().replace('__FONT_URL__','http://127.0.0.1:9334/cjk.ttf'))
    call('Page.addScriptToEvaluateOnNewDocument',source="""
      const nativeFetch=window.fetch;
      window.fetch=async (...args)=>{await new Promise(r=>setTimeout(r,150));return nativeFetch(...args)};
      document.addEventListener('DOMContentLoaded',()=>{
        let n=0;const timer=setInterval(()=>{
          const v=sessionStorage.getItem('smoke_view');
          if(!v||++n>100){clearInterval(timer);return}
          if(document.querySelector('.pcard')&&!document.querySelector('#appview').classList.contains('hidden')){
            clearInterval(timer);
            if(v==='project') document.querySelector('.pcard').click();
            else if(v!=='list') document.querySelector('[data-v="'+v+'"]').click();
          }
        },100);
      });
    """)
    call('Emulation.setDeviceMetricsOverride',width=1440,height=1000,deviceScaleFactor=1,mobile=False)
    call('Page.navigate',url=BASE+'/?smoke=1');time.sleep(1)
    ev('localStorage.clear();sessionStorage.clear()');call('Page.reload',ignoreCache=True);time.sleep(1)
    assert ev('!document.querySelector("#login").classList.contains("hidden")')
    ev("document.querySelector('#u').value='preview';document.querySelector('#p').value='preview-only-test';document.querySelector('#login button.primary').click()")
    wait('document.querySelectorAll(".pcard").length>=3')
    results['cold_login']=snapshot()
    assert results['cold_login']['first']=='全部文档'
    assert results['cold_login']['listVisible'] and results['cold_login']['project'] is None
    click('.pcard'); wait('document.querySelectorAll("#lres .doc").length===50')
    results['project_page1']=snapshot()
    click('#lmore');wait('document.querySelectorAll("#lres .doc").length===100')
    results['project_page2']=snapshot()
    assert len(results['project_page2']['selected'])==1
    click('#lres .doc')
    wait('S.view==="edit" && !!S.doc && !!document.querySelector("#f-branch").value')
    before = ev('S.doc.content')
    # Real UI save against synthetic preview data, then restore original content.
    ev('document.querySelector("#f-content").value='+json.dumps('浏览器隔离保存验收\n\n'+before))
    click('#v-edit button[onclick="saveDoc()"]')
    wait('!!S.doc && S.doc.content.startsWith("浏览器隔离保存验收")')
    results['editor_save']=ev('({id:S.doc.id,saved:document.querySelector("#save-status").textContent,git:S.doc.persistenceStatus?.git?.ok})')
    assert results['editor_save']['git'] is True
    ev('document.querySelector("#f-content").value='+json.dumps(before))
    click('#v-edit button[onclick="saveDoc()"]')
    wait('S.doc.content==='+json.dumps(before))
    click('aside [data-v="list"]');wait('!!document.querySelector(".pgrid")')
    assert ev('S.projFilter===null')
    click('.pcard[data-proj=""]');wait('S.projFilter==="" && document.querySelectorAll("#lres .doc").length>0')
    results['unassigned']=snapshot()
    assert ev('S.docs.every(d=>!d.project)')
    click('aside [data-v="list"]');wait('!!document.querySelector(".pgrid")')
    results['back_to_all']=snapshot()
    # An injected HTTP 503 tests the front-end recovery path, not service uptime.
    ev("window.smokeFetch=window.fetch; window.smokeFailOnce=true; window.fetch=(u,o)=>{if(window.smokeFailOnce&&String(u).includes('/api/v1/projects')){window.smokeFailOnce=false;return Promise.resolve(new Response(JSON.stringify({detail:'隔离测试503'}),{status:503,headers:{'Content-Type':'application/json'}}))}return window.smokeFetch(u,o)}")
    click('#v-list button[aria-label="刷新"]');wait('!!document.querySelector("#lres [role=alert]")')
    results['simulated_503']=snapshot()
    assert results['simulated_503']['tokenPresent']
    click('#lres [role="alert"] button');wait('!!document.querySelector(".pgrid")')
    ev('window.fetch=window.smokeFetch')
    shot('desktop-folders')
    shot('desktop-project','project')
    # UI state and light/dark layout at small viewports.
    for width in (390,768):
        call('Emulation.setDeviceMetricsOverride',width=width,height=844,deviceScaleFactor=1,mobile=True)
        ev("sessionStorage.setItem('smoke_view','list')");call('Page.reload',ignoreCache=True)
        wait('!!document.querySelector(".pgrid")')
        state=snapshot(); results[f'viewport_{width}']=state
        assert state['overflow']==0, state
        if width==390:
            click('#nav-toggle');assert ev('document.querySelector("#nav-toggle").getAttribute("aria-expanded")==="true"')
            click('aside [data-v="search"]');assert ev('S.view==="search"')
            click('#nav-toggle');click('aside [data-v="list"]');wait('!!document.querySelector(".pgrid")')
            shot('mobile-folders')
            ev("localStorage.setItem('mem_theme','dark')")
            shot('mobile-dark')
    call('Emulation.setDeviceMetricsOverride',width=1440,height=1000,deviceScaleFactor=1,mobile=False)
    ev("localStorage.setItem('mem_theme','light')")
    results['runtime_errors']=events
    assert not events,events
    print(json.dumps(results,ensure_ascii=False,indent=2))
    (OUT/'browser-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
except Exception:
    try:
        results['failure_state'] = ev("({url:location.href,token:!!localStorage.getItem('mem_token'),stateToken:!!S.token,loginError:document.querySelector('#lerr').textContent,body:document.body.innerText.slice(0,600)})")
        results['runtime_errors'] = events
        (OUT/'browser-failure.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(results,ensure_ascii=False,indent=2))
    except Exception: pass
    raise
finally:
    try: call('Target.closeTarget',targetId=target)
    except Exception: pass
    ws.close()
