"""Chromium UI checks: provenance, manual updates, lazy video and layout; fixtures only."""
import argparse,base64,os,json,shutil,socket,signal,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cdp import CDP
parser=argparse.ArgumentParser();parser.add_argument('--html',default=str(ROOT/'admin.html'));parser.add_argument('--screenshots',type=Path);parser.add_argument('--probe-only',action='store_true');args=parser.parse_args()
html=Path(args.html).read_bytes()
VERSION=json.loads((ROOT/'version.json').read_text())['version']
EXTENSION_VERSION=json.loads((ROOT/'extension/manifest.json').read_text())['version']
popup_files={
 '/extension/popup.html': ((ROOT/'extension/popup.html').read_bytes(), 'text/html; charset=utf-8'),
 '/extension/popup.js': ((ROOT/'extension/popup.js').read_bytes(), 'text/javascript; charset=utf-8'),
 '/extension/bridge-site.js': ((ROOT/'extension/bridge-site.js').read_bytes(), 'text/javascript; charset=utf-8'),
}
now=int(time.time())
accounts=[{'id':'fixture'+str(i).zfill(5),'label':'Fixture-'+str(i),'enabled':True,'ok':True,'essential_ok':True,'cookie_count':12,
 'proxy':None,'note':'Lỗi tác vụ: 参考素材上传未确认 (0/1), 已停止生成','last_used':now-43200,'expires_at':now+172800,
 'quota':{'found':True,'plan':'Free plan','weekly_used_pct':29,'extra_left':'1B tokens left','checked_at':now-10000}} for i in range(8)]
events=[]
network=[]
server_state={'mode':'update','videoPolls':0,'connections':{
 '/admin/connections/chatgpt':dict(mode='browser',image_model='gpt-image-1.5',official_configured=True,external_configured=False,profiles=[]),
 '/admin/connections/gemini-web':dict(mode='browser',image_model='gemini-2.5-flash-image',official_configured=True,external_configured=False,profiles=[]),
}}
feature_errors=[]
def check(value,message):
 if not value:feature_errors.append(message)

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  path=urlsplit(self.path).path
  status_code=200
  network.append({'method':'GET','path':path})
  if path in ['/','/admin']:data=html;kind='text/html; charset=utf-8'
  elif path in popup_files:data,kind=popup_files[path]
  elif path=='/admin/accounts':data={'accounts':accounts,'stats':{'total':8,'healthy':8}};kind='application/json'
  elif path=='/admin/apikey':data={'api_key':'fixture-only'};kind='application/json'
  elif path=='/admin/web-providers':data={'configured':False,'profile_connected':False,'ready':[],'local_setup':True};kind='application/json'
  elif path in server_state['connections']:data=server_state['connections'][path];kind='application/json'
  elif path in ['/admin/update/check','/admin/repo/status']:
   known=server_state['mode']=='update'
   data={'repo_url':'https://github.com/czg86389-hub/muse2api','repo_ref':'main','local_version':'1.6.10','remote_version':VERSION if known else '', 'local_sha':'aaaaaaa','remote_sha':'bbbbbbb' if known else '', 'up_to_date':False if known else None,'has_update':known,'check_error':'' if known else 'fixture remote unavailable','managed_deployment':False,'highlights':['fixture update'],'highlights_en':['fixture update'],'highlights_vi':['Bản thử tiếng Việt'],'recent_commits':[]};kind='application/json'
  elif path=='/admin/media':
   data={'count':2,'media':[{'name':name,'kind':'video','url':'/v1/media/'+name,'thumbnail_url':'/v1/media/'+name+'/thumbnail','bytes':4000,'mtime':now}for name in ['clip-one.mp4','clip-two.mp4']]};kind='application/json'
  elif path=='/admin/tasks':
   data={'tasks':[{'id':'fixture-task','kind':'video','prompt':'fixture task','status':'failed','error':'参考素材上传未确认（0/1），已停止生成','created_at':now-7,'account':'fixture00000','queue_elapsed':2.0,'elapsed':5.0,'total_elapsed':7.0,'attempt':1,
                   'error_code':'MUSE_REFERENCE_UPLOAD_UNCONFIRMED','error_stage':'before_send',
                   'reference_count':1,'confirmed_reference_count':0}]};kind='application/json'
  elif path=='/v1/videos/fixture-video':
   server_state['videoPolls'] += 1
   if server_state['videoPolls'] == 1:
    status_code=502
    data={'error':{'message':'temporary fixture transport error'}}
   else:
    data={'status':'failed','error':'fixture only'}
   kind='application/json'
  elif path.endswith('/thumbnail'):data=thumbnail_bytes;kind='image/jpeg'
  elif path.endswith('.mp4'):data=video_bytes;kind='video/mp4'
  else:data={'browser_running':True,'media_count':2,'status':'ok','generation_queue':{'queue_timeout':3600},'config':{'image_timeout':240,'video_timeout':600}};kind='application/json'
  body=data if isinstance(data,bytes) else json.dumps(data).encode()
  self.send_response(status_code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def do_PUT(self):
  self.do_POST()
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
  path=urlsplit(self.path).path
  events.append({'method':'POST','path':path,'body':body})
  if path=='/admin/web-providers/connect-profile':
   reply={'configured':True,'profile_connected':True,'ready':[],'local_setup':True}
  elif path in server_state['connections']:
   reply=server_state['connections'][path]
   reply.update(body)
  elif path.endswith('/test') and path[:-5] in server_state['connections']:
   reply={'ok':True}
  elif path=='/v1/videos':
   reply={'id':'fixture-video','status':'queued'}
  else:
   reply={'ok':True,'proxy_reachable':True,'session_ok':True,'exit_ip':'198.51.100.7','workspace_status':'assigned','tested_saved_proxy':False}
  data=json.dumps(reply).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
with tempfile.TemporaryDirectory(prefix='muse-layout-',dir=Path.home()) as home:
 ffmpeg=shutil.which('ffmpeg')
 assert ffmpeg, 'Install FFmpeg for media UI integration checks'
 clip=Path(home)/'fixture.mp4';thumb=Path(home)/'fixture.jpg'
 subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10','-t','1','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-y',str(clip)],check=True)
 subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(clip),'-frames:v','1','-threads','1','-y',str(thumb)],check=True)
 video_bytes=clip.read_bytes();thumbnail_bytes=thumb.read_bytes()
 log=open(Path(home)/'chrome.log','wb')
 proc=subprocess.Popen([shutil.which('chromium') or shutil.which('google-chrome'),'--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-allow-origins=*',
 '--remote-debugging-port='+str(port),'--user-data-dir='+str(Path(home)/'browser'),'about:blank'],stdout=log,stderr=log,start_new_session=True)
 page=None
 try:
  import requests
  session=requests.Session();session.trust_env=False
  for _ in range(150):
   try:
    tab=next(x for x in session.get(f'http://127.0.0.1:{port}/json/list',timeout=1).json() if x.get('type')=='page')
    page=CDP(tab['webSocketDebuggerUrl'],timeout=15);break
   except (requests.RequestException,StopIteration):time.sleep(.1)
  assert page
  page.send('Page.enable');page.send('Runtime.enable')
  page.send('Page.addScriptToEvaluateOnNewDocument',{'source':"localStorage.setItem('muse2api_lang',localStorage.getItem('muse2api_lang')||'zh');window.__confirms=[];window.confirm=(m)=>{window.__confirms.push(m);return false};window.__errors=[];window.addEventListener('error',e=>__errors.push(e.message));"})
  page.send('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/?key=fixture-only'})
  def wait(expr):
   end=time.monotonic()+15
   while time.monotonic()<end:
    if page.js(expr):return
    time.sleep(.1)
   raise AssertionError('Condition failed: '+expr)
  wait("document.querySelectorAll('#poolBody tr').length===8")
  for tab, provider, scope in [('openai','chatgpt','chatgpt'), ('gemini','gemini-web','gemini')]:
   page.js(f"goTab('{tab}')")
   node=f"$('tab-{tab}')"
   wait(f"{node}.querySelector('[name=image_model]').value.length > 0")
   assert page.js(f"{node}.querySelector('[name=mode]').value === 'browser' && {node}.querySelector('[data-public-url]').value.endsWith('/{scope}/v1')")
   page.js(f"""{{const mode={node}.querySelector('[name=mode]'); mode.value='official';
      mode.dispatchEvent(new Event('change')); {node}.querySelector('button[type=submit]').click();}}""")
   wait(f"{node}.querySelector('[data-connection-status]').textContent.includes('Đã lưu')")
   page.js(f"{node}.querySelector('[data-test-official]').click()")
   wait(f"{node}.querySelector('[data-connection-status]').textContent.includes('đã xác nhận')")
   endpoint='/admin/connections/'+provider
   assert [event['path'] for event in events[-2:]]==[endpoint,endpoint+'/test']
   page.send('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':False})
   assert page.js("document.documentElement.scrollWidth === document.documentElement.clientWidth")
   page.send('Emulation.clearDeviceMetricsOverride')
  wait("$('tab-gemini').querySelector('[data-provider-status]').textContent.includes('Chưa kết nối')")
  assert page.js("$('tab-gemini').querySelector('[data-web-extension]').getClientRects().length && !$('tab-gemini').querySelector('[data-web-setup]').open && !$('extensionUpgradeNotice').getClientRects().length")
  page.js("$('tab-gemini').querySelector('[data-web-connect]').click()")
  wait("$('tab-gemini').querySelector('[data-provider-status]').textContent.includes('Đã kết nối Chrome')")
  assert any(item['path']=='/admin/web-providers/connect-profile' for item in events)
  assert page.js("$('tab-gemini').querySelector('[data-provider-test]').disabled")
  page.js("goTab('play');$('vidDur').value='125';$('vidBtn').click()")
  wait("$('vidRes').textContent.includes('fixture only') && !$('vidBtn').disabled")
  assert events[-1]['path']=='/v1/videos' and events[-1]['body']['duration']==125
  assert server_state['videoPolls']==2
  page.js("$('vidDur').value='0';$('vidBtn').click()")
  assert page.js("!$('vidBtn').disabled && $('vidDur').validity.rangeUnderflow")
  assert sum(item['path']=='/v1/videos' for item in events)==1
  page.js("document.querySelector('[data-t=pool]').click()")
  events.clear()
  measure="""(()=>{const m=document.querySelector('main'),w=document.querySelector('#poolBody').closest('.tblwrap'),n=document.querySelector('#poolBody td.ellip');return {viewport:innerWidth,mainWidth:m.getBoundingClientRect().width,bodyWidth:document.documentElement.scrollWidth,tableWidth:w.clientWidth,tableScroll:w.scrollWidth,noteDisplay:getComputedStyle(n).display,noteWhiteSpace:getComputedStyle(n).whiteSpace,buttons:document.querySelectorAll('#poolBody tr:first-child button').length}})()"""
  for lang in (['zh'] if args.probe_only else ['zh','en','vi']):
   if lang!='zh':
    page.js(f"localStorage.setItem('muse2api_lang',{json.dumps(lang)});location.reload()")
    wait(f"document.documentElement.lang==={json.dumps(lang)} && document.querySelectorAll('#poolBody tr').length===8")
   network.clear()
   server_state['mode']='update'
   page.js("loadRepoStatus(true)",await_promise=True)
   if lang=='vi':
    check(page.js("$('updBannerMsg').textContent.includes('Bản thử tiếng Việt') && $('repoCommitsBox').textContent.includes('Bản thử tiếng Việt')"), 'vi: release notes are not localized')
    check(page.js("""(async () => {
      const fetchNormally = window.fetch;
      let finishOlder;
      window.fetch = (path, options) => path === '/admin/update/check'
        ? new Promise(resolve => { finishOlder = resolve; }) : fetchNormally(path, options);
      try {
        const old = loadRepoStatus(false);
        await loadRepoStatus(true);
        finishOlder(new Response(JSON.stringify({..._lastRepoStatus, local_version:'old fixture'})));
        await old;
        return $('repoBadge').textContent.includes('1.6.10');
      } finally { window.fetch = fetchNormally; }
    })()""",await_promise=True) is True,'vi: late automatic update check changed the manual result')
   check('1.6.10' in page.js("$('repoBadge').textContent"),lang+': installed version was replaced by remote')
   check(page.js("$('updateBanner').classList.contains('show')"),lang+': update notification banner is hidden')
   check(not any(x['path']=='/admin/update/upgrade' for x in events),lang+': update check posted an upgrade')
   page.js("window.__confirms=[];upgradeNow()")
   wait("window.__confirms.length===1")
   if lang=='en':check('Update and restart' in page.js("window.__confirms[0]"),'en: manual confirmation is not localized')
   if lang=='vi':check('Cập nhật và khởi động lại' in page.js("window.__confirms[0]"),'vi: manual confirmation is not localized')
   check(not any(x['path']=='/admin/update/upgrade' for x in events),lang+': cancelled confirmation still upgraded')
   page.js("goTab('media')")
   wait("document.querySelectorAll('#mediaGrid .tile').length===2")
   check(page.js("document.querySelectorAll('#mediaGrid video').length===0"),lang+': video elements preload in media list')
   check(not any(x['path'].endswith('.mp4') for x in network),lang+': listing downloaded original video')
   page.js("document.querySelector('#mediaGrid [data-play-video]').scrollIntoView({block:'center'})")
   rect=page.js("(()=>{const r=document.querySelector('#mediaGrid [data-play-video]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
   page.send('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','clickCount':1,**rect})
   page.send('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','clickCount':1,**rect})
   wait("document.querySelectorAll('#mediaPlayer video').length===1")
   page.send('Runtime.evaluate',{'expression':"document.querySelector('#mediaPlayer video').play().catch(()=>{})",'userGesture':True,'awaitPromise':True})
   end=time.monotonic()+4
   while not any(x['path']=='/v1/media/clip-one.mp4' for x in network)and time.monotonic()<end:time.sleep(.1)
   check(any(x['path']=='/v1/media/clip-one.mp4' for x in network),lang+': selected video was not requested')
   check(not any(x['path']=='/v1/media/clip-two.mp4' for x in network),lang+': unselected video was requested')
   page.js("closeMediaPreview()")
   check(page.js("document.querySelectorAll('#mediaPlayer video').length===0"),lang+': preview player not released')
   page.js("goTab('tasks')")
   wait("document.querySelector('#taskBody').textContent.includes('fixture-task')")
   text=page.js("document.querySelector('#tab-tasks tbody').innerText")
   check('Fixture-0' in text,lang+': task account is not visible')
   page.js("document.querySelector('#taskBody summary').click()")
   message = {'vi': 'Muse chưa xác nhận tệp tham chiếu (0/1)',
              'en': 'Muse did not confirm the reference files (0/1)',
              'zh': '参考素材上传未确认（0/1）'}[lang]
   wait(f"document.querySelector('#taskBody details[open]').innerText.includes({json.dumps(message)})")
   diagnostic=page.js("document.querySelector('#taskBody details[open]').innerText")
   check('MUSE_REFERENCE_UPLOAD_UNCONFIRMED' not in diagnostic, lang+': task code visible without opening advanced settings')
   page.js("document.querySelector('#taskBody [data-task-diagnostic] summary').click()")
   check(page.js("document.querySelector('#taskBody [data-task-diagnostic]').innerText.includes('MUSE_REFERENCE_UPLOAD_UNCONFIRMED')"),
         lang+': task code is missing in advanced settings')
   if lang=='vi':
    check(all(label in diagnostic for label in ('Dừng trước khi gửi','Tệp tham chiếu API đã nhận 1','Tệp thấy trong ô nhập 0')),
          'vi: task stage or reference counts are missing')
   page.js("loadTasks()",await_promise=True)
   check(page.js("Boolean(document.querySelector('#taskBody details[open]'))"),lang+': error details collapsed on refresh')
   for word in {'en': ['Queue','Generate','Total'], 'vi': ['Chờ','Tạo','Tổng'], 'zh': ['排队','生成','总计']}[lang]:check(word in text,lang+': task duration missing '+word)
   server_state['mode']='unknown'
   page.js("loadRepoStatus(true)",await_promise=True)
   badge=page.js("$('repoBadge').textContent")
   check('已是最新'not in badge and'Up to date'not in badge,lang+': unknown remote was reported current')
   if lang=='en':check('unconfirmed'in badge.lower(),'en: unknown state is not localized')
   server_state['mode']='update'
   page.js("loadRepoStatus(true);goTab('pool')")
   if lang == 'vi':
    wait("document.querySelector('#poolBody td.ellip')?.title.includes('Lỗi tác vụ: Muse chưa xác nhận tệp tham chiếu (0/1)')")
   print('UI_FEATURE_PROBE',lang,json.dumps({'errors':feature_errors,'original_video_requests':[x['path']for x in network if x['path'].endswith('.mp4')]}),flush=True)
   for width in ([1920] if args.probe_only else [2560,1920,1536,1440,1366,1280,768,390,320]):
    page.send('Emulation.setDeviceMetricsOverride',{'width':width,'height':1080,'deviceScaleFactor':1,'mobile':False})
    time.sleep(.15);m=page.js(measure);print(json.dumps({'lang':lang,**m}),flush=True)
    assert m['mainWidth']>=width-24,'Page does not fill the viewport'
    assert m['bodyWidth']<=width+1,'Page overflows the viewport'
    if width>=1280:assert m['tableScroll']<=m['tableWidth']+1,'Desktop account controls need horizontal scrolling'
    assert m['noteDisplay']=='table-cell' and m['noteWhiteSpace']=='normal','Notes must retain table-cell layout and wrap'
    assert m['buttons']==8
    if width==1920 and not args.probe_only and args.screenshots:
     page.js("document.querySelector('#poolBody').scrollIntoView({block:'end'})")
     image=page.send('Page.captureScreenshot',{'format':'png'})
     args.screenshots.mkdir(parents=True,exist_ok=True)
     (args.screenshots/('layout-'+lang+'.png')).write_bytes(base64.b64decode(image['result']['data']))
   if not args.probe_only:
    page.send('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':False})
    page.js("openProxy('fixture00000');$('proxyMode').value='proxy';updateProxyMode();$('proxyServer').value='socks5://draft:draft-secret@127.0.0.1:1081';$('proxyServer').dispatchEvent(new Event('input',{bubbles:true}))")
    assert page.js("!$('proxyTest').disabled && !document.querySelector('.proxy-auth').open")
    page.js("$('proxyTest').click()");wait("$('proxyResult').innerText.includes('198.51.100.7') && !$('proxyTest').disabled")
    assert events[-1]['body']['proxy']['server']=='socks5://draft:draft-secret@127.0.0.1:1081'
    assert page.js("!accounts[0].proxy && $('proxyDialog').getBoundingClientRect().right<=innerWidth")
    assert all(e['path'].endswith('/proxy/test') for e in events)
    page.js("$('proxyDialog').close()")
    wait("$('proxyServer').value==='' && $('proxyPassword').value===''")
  if not args.probe_only:
   assert not page.js('window.__errors')
   print('PASS: full-width layout, desktop controls, contained mobile scrolling, Chinese/English/Vietnamese and test-without-saving')
   print('FEATURE_ERRORS',json.dumps(feature_errors),flush=True)
   assert not feature_errors,feature_errors

   page.send('Page.addScriptToEvaluateOnNewDocument', {'source': f"""
    if (location.pathname === '/extension/popup.html') {{
      localStorage.setItem('muse2api_lang', new URLSearchParams(location.search).get('lang'));
      Object.assign(window.chrome, {{
        runtime: {{getManifest: () => ({{version: {json.dumps(EXTENSION_VERSION)}}})}},
        storage: {{local: {{get: async () => ({{}})}}}},
        tabs: {{query: async () => []}},
      }});
    }}
   """})
   for lang in ('vi','en','zh'):
    page.send('Emulation.setDeviceMetricsOverride',{'width':216,'height':550,'deviceScaleFactor':1,'mobile':False})
    page.send('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/extension/popup.html?lang={lang}'})
    wait(f"new URLSearchParams(location.search).get('lang')==='{lang}' && "
         f"document.getElementById('extensionVersion')?.textContent==='v{EXTENSION_VERSION}'")
    assert page.js("document.body.getBoundingClientRect().width === 380 && document.documentElement.scrollWidth === 380"), lang
    page.send('Emulation.setDeviceMetricsOverride',{'width':380,'height':550,'deviceScaleFactor':1,'mobile':False})
    assert page.js("""(() => {
     const heading = document.querySelector('h1');
     const range = document.createRange();
     range.selectNodeContents(heading);
     const buttons = [...document.querySelectorAll('.provider-buttons button')].map(b => b.getBoundingClientRect());
     return document.documentElement.scrollWidth === 380 && range.getClientRects().length === 1
      && $('langSel').getBoundingClientRect().right < innerWidth
      && buttons.length === 3 && buttons.every((b, i) => b.top === buttons[0].top && (!i || b.left > buttons[i-1].right))
      && $('directSteps').checkVisibility() === false;
    })()""") is True, lang
    page.js("$('checkConnection').click()")
    wait("document.getElementById('log').classList.contains('show')")
    assert page.js("document.documentElement.scrollWidth === 380")
    if lang == 'vi' and args.screenshots:
     image=page.send('Page.captureScreenshot',{'format':'png'})
     (args.screenshots/'extension-popup-vi.png').write_bytes(base64.b64decode(image['result']['data']))

   page.js("""new Promise(resolve => {
    const script = document.createElement('script');
    script.src = '/extension/bridge-site.js';
    script.onload = resolve;
    document.head.append(script);
   })""",await_promise=True)
   page.js("""document.body.innerHTML = '<button aria-label="Google Account">Profile</button>' +
    '<div contenteditable="true">Unrelated editor</div>' +
    '<rich-textarea><div contenteditable="true" class="ql-editor">Composer</div></rich-textarea>'""")
   verify="bridgeSiteAction({...bridgeSites['gemini-web'], origin:location.origin}, 'verify')"
   assert page.js(verify,await_promise=True)=={'signedIn':True,'composerReady':True}
   page.js("document.querySelector('rich-textarea').setAttribute('aria-readonly','true')")
   assert page.js(verify,await_promise=True)=={'signedIn':True,'composerReady':False}
   page.js("""document.body.innerHTML = '<button data-testid="profile-menu-trigger">Profile</button>' +
    '<form id="other"><textarea>Keep my draft</textarea></form>' +
    '<form id="composer"><textarea id="new-editor"></textarea><button id="send" data-testid="send-button" type="button" disabled>Send</button></form>';
    $('new-editor').oninput = () => { $('send').disabled = false; };
    window.fixtureSends = 0;
    $('send').onclick = () => { window.fixtureSends++; };
    window.chatSite = {...bridgeSites.chatgpt, origin:location.origin, path:location.pathname};
   """)
   verify="bridgeSiteAction(chatSite, 'verify')"
   assert page.js(verify,await_promise=True)=={'signedIn':True,'composerReady':True}
   assert page.js("bridgeSiteAction(chatSite, 'prepare', 'Fixture prompt')",await_promise=True)=={'ready':False}
   assert page.js("bridgeSiteAction(chatSite, 'prepare', 'Fixture prompt')",await_promise=True)=={'ready':True}
   assert page.js("bridgeSiteAction(chatSite, 'send', 'Fixture prompt')",await_promise=True)=={'sent':True}
   assert page.js("window.fixtureSends === 1 && $('other').querySelector('textarea').value === 'Keep my draft'")
   page.js("$('new-editor').readOnly = true")
   assert page.js(verify,await_promise=True)=={'signedIn':True,'composerReady':False}
   page.js("$('new-editor').readOnly = false; $('composer').append(document.createElement('textarea'))")
   assert page.js(verify,await_promise=True)=={'signedIn':True,'composerReady':False}
   page.js("""$('composer').querySelectorAll('textarea').forEach(editor => editor.remove());
    $('composer').insertAdjacentHTML('afterbegin', '<div contenteditable="plaintext-only">Composer</div>');""")
   assert page.js(verify,await_promise=True)=={'signedIn':True,'composerReady':True}
   assert not page.js('window.__errors')
   print('PASS: popup renders in three languages and detects the scoped Gemini and ChatGPT editors')
 finally:
  if page:
   try:page.send('Browser.close')
   except Exception:pass
   page.close()
  try:os.killpg(proc.pid,signal.SIGTERM)
  except ProcessLookupError:pass
  try:proc.wait(timeout=8)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
  try:os.killpg(proc.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  time.sleep(.2);log.close();server.shutdown();server.server_close()
