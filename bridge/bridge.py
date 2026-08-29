#!/usr/bin/env python3
"""XM DVRIP OPTalk bridge: push TTS/audio to camera speakers + live browser-mic talk.
Endpoints:
  POST /say      {"cam":"cam3"|"all"|["cam2","cam3"],"text":"...","voice":"it"}  -> TTS out speaker(s)
  POST /play?cam=cam3|all|cam2,cam3    body = audio bytes (wav/mp3/ogg/...)      -> out speaker(s)
  POST /play_url {"cam":...,"url":"https://.../x.mp3"}                            -> fetch + out speaker(s)
  POST /stop     {"cam":...}   (or GET /stop?cam=cam3|all)                        -> kill playback now
  GET  /mic /talk?cam=cam3                                                        -> web UI (talk + media)
  WS   /ws?cam=cam3   binary 16-bit LE 8k mono PCM frames                         -> speaker
  GET  /cams  /healthz  /xm_ptt.js
Multi-cam: cam may be "all"/"*", a comma list, or a JSON array. Playback is cancellable via /stop.
"""
import asyncio, hashlib, json, logging, os, struct, subprocess, threading, time
import aiohttp
from aiohttp import web, WSMsgType

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("xm-talk")

CHARS="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
def sofia_hash(p):
    m=hashlib.md5(p.encode()).digest()
    return "".join(CHARS[(m[i]+m[i+1])%62] for i in range(0,16,2))

# cam -> [ip, dvrip_user, dvrip_pass]. Set via CAMS env (JSON). No creds in source.
CAMS=json.loads(os.environ.get("CAMS","{}"))
TOKEN=os.environ.get("TALK_TOKEN","")  # if set, required for requests coming via a reverse proxy
MAX_URL_BYTES=int(os.environ.get("MAX_URL_BYTES", str(20*1024*1024)))  # /play_url fetch cap

# cam -> threading.Event. Set => the running playback thread for that cam stops at the next 40ms chunk.
# ponytail: one active playback per cam; a second play on the same cam replaces the stop slot.
PLAYING={}

def _authed(req):
    if not TOKEN:
        return True
    if "X-Forwarded-For" not in req.headers and "X-Real-IP" not in req.headers:
        return True  # direct LAN access, no proxy
    supplied = req.query.get("token") or req.headers.get("X-Talk-Token")
    return supplied == TOKEN

def _deny():
    return web.json_response({"ok": False, "err": "unauthorized"}, status=401)

def _resolve(spec):
    """cam spec -> list of known cam names. Accepts str ('all'/'*'/'cam2,cam3'), list, or None."""
    if spec is None: return []
    if isinstance(spec, (list, tuple)):
        names=[str(c).strip() for c in spec]
    elif str(spec).strip().lower() in ("all","*"):
        return list(CAMS)
    else:
        names=[c.strip() for c in str(spec).split(",")]
    return [c for c in names if c in CAMS]

def lin2alaw(s):
    SEG=[0x1F,0x3F,0x7F,0xFF,0x1FF,0x3FF,0x7FF,0xFFF]
    sign=0x80 if s>=0 else 0x00
    if s<0: s=-s-1
    if s>0x7FFF: s=0x7FFF
    if s<=0x1F: aval=s>>1
    else:
        seg=7
        for i,t in enumerate(SEG):
            if s<=t: seg=i; break
        aval=(seg<<4)|((s>>(seg+1))&0x0F) if seg>=1 else (seg<<4)|((s>>1)&0x0F)
    return (sign|aval)^0x55

def pcm16_to_alaw(pcm):  # bytes of s16le -> alaw bytes
    n=len(pcm)//2
    return bytes(lin2alaw(struct.unpack_from("<h",pcm,2*i)[0]) for i in range(n))

class OPTalk:
    """One talk session to a cam. Blocking socket; run in executor."""
    def __init__(self, ip, user, pw):
        import socket
        self.sock=socket.create_connection((ip,34567),6)
        self.session=0; self.seq=0; self.user=user; self.pw=pw
    def _pkt(self,msgid,body):
        self.sock.sendall(struct.pack("<BB2xII2xHI",0xFF,0,self.session,self.seq,msgid,len(body))+body); self.seq+=1
    def _recvn(self,n):
        b=b""
        while len(b)<n:
            c=self.sock.recv(n-len(b))
            if not c: break
            b+=c
        return b
    def _reply(self):
        h=self._recvn(20)
        if len(h)<20: return None
        ln=struct.unpack("<I",h[16:20])[0]; d=self._recvn(ln)
        try: return json.loads(d.rstrip(b"\x00\x0a").decode("utf-8","replace"))
        except: return {"_raw":d[:80].hex()}
    def login(self):
        self._pkt(1000, json.dumps({"EncryptType":"MD5","LoginType":"DVRIP-Web","UserName":self.user,"PassWord":sofia_hash(self.pw)},separators=(",",":")).encode()+b"\x0a\x00")
        r=self._reply()
        if r and "SessionID" in r: self.session=int(r["SessionID"],16)
        return r
    def start(self):
        sid="0x%08X"%self.session
        fmt={"BitRate":128,"EncodeType":"G711_ALAW","SampleBit":8,"SampleRate":8}
        self._pkt(1434, json.dumps({"Name":"OPTalk","SessionID":sid,"OPTalk":{"Action":"Claim","AudioFormat":fmt}},separators=(",",":")).encode()+b"\x0a\x00")
        self._reply()
        self._pkt(1430, json.dumps({"Name":"OPTalk","SessionID":sid,"OPTalk":{"Action":"Start","AudioFormat":fmt}},separators=(",",":")).encode()+b"\x0a\x00")
        self.sock.setblocking(False); self._drain(); self.sock.setblocking(True)
        self.fmt=fmt
    def _drain(self):
        try:
            while True:
                if not self.sock.recv(65536): break
        except (BlockingIOError,OSError): pass
    def send_alaw(self, alaw, realtime=True, stop=None):
        CH=320; t0=time.time()
        self.sock.setblocking(False)
        for n,i in enumerate(range(0,len(alaw),CH)):
            if stop is not None and stop.is_set(): break
            ch=alaw[i:i+CH]
            self._pkt(1430, b"\x00\x00\x01\xfa"+bytes([0x0e,0x02])+struct.pack("<H",len(ch))+ch)
            self._drain()
            if realtime:
                dt=t0+(n+1)*0.04-time.time()
                if dt>0: time.sleep(dt)
        self.sock.setblocking(True)
    def stop(self):
        sid="0x%08X"%self.session
        try:
            self._pkt(1434, json.dumps({"Name":"OPTalk","SessionID":sid,"OPTalk":{"Action":"Stop","AudioFormat":self.fmt}},separators=(",",":")).encode()+b"\x0a\x00")
        except Exception: pass
    def close(self):
        try: self.sock.close()
        except Exception: pass

def _tts_to_alaw(text, voice):
    # espeak-ng wav -> ffmpeg -> 8k mono alaw
    p1=subprocess.run(["espeak-ng","-v",voice,"--stdout",text],capture_output=True,check=True)
    p2=subprocess.run(["ffmpeg","-hide_banner","-v","error","-i","pipe:0","-ar","8000","-ac","1","-f","alaw","pipe:1"],
                      input=p1.stdout,capture_output=True,check=True)
    return p2.stdout

def _audio_to_alaw(data):
    # any ffmpeg-decodable audio (wav/mp3/ogg/aac/...) -> 8k mono alaw
    p=subprocess.run(["ffmpeg","-hide_banner","-v","error","-i","pipe:0","-ar","8000","-ac","1","-f","alaw","pipe:1"],
                     input=data,capture_output=True,check=True)
    return p.stdout

def _push(cam, alaw):
    ip,u,pw=CAMS[cam]
    ev=threading.Event(); PLAYING[cam]=ev
    log.info("push cam=%s ip=%s bytes=%d (%.1fs)", cam, ip, len(alaw), len(alaw)/8000.0)
    t=OPTalk(ip,u,pw)
    try:
        r=t.login()
        if not t.session:
            log.warning("push cam=%s LOGIN FAILED ret=%s", cam, r)
            return {"ok":False,"cam":cam,"err":"login failed","ret":r}
        t.start(); t.send_alaw(alaw, stop=ev); t.stop()
        stopped=ev.is_set()
        log.info("push cam=%s %s", cam, "STOPPED" if stopped else "OK")
        return {"ok":True,"cam":cam,"ms":int(len(alaw)/8.0),"stopped":stopped}
    except Exception as e:
        log.exception("push cam=%s ERROR: %s", cam, e)
        return {"ok":False,"cam":cam,"err":str(e)}
    finally:
        if PLAYING.get(cam) is ev: PLAYING.pop(cam,None)
        t.close()

async def _push_many(cams, alaw):
    """Play the same alaw to several cams concurrently. Returns per-cam results."""
    results=await asyncio.gather(*[asyncio.to_thread(_push, c, alaw) for c in cams])
    return {"ok":all(r.get("ok") for r in results),"cams":cams,"results":results}

async def say(req):
    if not _authed(req):
        log.warning("say DENIED (missing/bad token) from %s", req.headers.get("X-Forwarded-For", req.remote))
        return _deny()
    d=await req.json()
    cams=_resolve(d.get("cams") or d.get("cam"))
    log.info("say cams=%s chars=%d voice=%s", cams, len(d.get("text","")), d.get("voice"))
    if not cams: return web.json_response({"ok":False,"err":"no known cam"},status=400)
    alaw=await asyncio.to_thread(_tts_to_alaw, d.get("text",""), d.get("voice","it"))
    return web.json_response(await _push_many(cams, alaw))

async def play(req):
    if not _authed(req):
        log.warning("play DENIED (missing/bad token) from %s", req.headers.get("X-Forwarded-For", req.remote))
        return _deny()
    cams=_resolve(req.query.get("cams") or req.query.get("cam"))
    log.info("play cams=%s", cams)
    if not cams: return web.json_response({"ok":False,"err":"no known cam"},status=400)
    data=await req.read()
    if not data: return web.json_response({"ok":False,"err":"empty body"},status=400)
    try:
        alaw=await asyncio.to_thread(_audio_to_alaw, data)
    except subprocess.CalledProcessError as e:
        log.warning("play decode failed: %s", e.stderr[:200] if e.stderr else e)
        return web.json_response({"ok":False,"err":"could not decode audio"},status=400)
    return web.json_response(await _push_many(cams, alaw))

async def play_url(req):
    if not _authed(req):
        log.warning("play_url DENIED from %s", req.headers.get("X-Forwarded-For", req.remote))
        return _deny()
    d=await req.json()
    cams=_resolve(d.get("cams") or d.get("cam"))
    url=(d.get("url") or "").strip()
    log.info("play_url cams=%s url=%s", cams, url)
    if not cams: return web.json_response({"ok":False,"err":"no known cam"},status=400)
    if not url.startswith(("http://","https://")):
        return web.json_response({"ok":False,"err":"url must be http(s)"},status=400)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status!=200:
                    return web.json_response({"ok":False,"err":"fetch %d"%r.status},status=400)
                data=await r.content.read(MAX_URL_BYTES+1)
        if len(data)>MAX_URL_BYTES:
            return web.json_response({"ok":False,"err":"file too big (>%dMB)"%(MAX_URL_BYTES//1048576)},status=400)
    except Exception as e:
        log.warning("play_url fetch error: %s", e)
        return web.json_response({"ok":False,"err":"fetch failed: %s"%e},status=400)
    try:
        alaw=await asyncio.to_thread(_audio_to_alaw, data)
    except subprocess.CalledProcessError:
        return web.json_response({"ok":False,"err":"could not decode audio"},status=400)
    return web.json_response(await _push_many(cams, alaw))

async def stop(req):
    if not _authed(req):
        return _deny()
    spec = req.query.get("cams") or req.query.get("cam")
    if spec is None and req.can_read_body:
        try: spec=(await req.json()).get("cams") or (await req.json()).get("cam")
        except Exception: spec=None
    cams=_resolve(spec) or list(CAMS)  # no spec => stop everything
    stopped=[]
    for c in cams:
        ev=PLAYING.get(c)
        if ev: ev.set(); stopped.append(c)
    log.info("stop requested=%s signalled=%s", cams, stopped)
    return web.json_response({"ok":True,"stopped":stopped})

async def ws_handler(req):
    src=req.headers.get("X-Forwarded-For", req.remote)
    if not _authed(req):
        log.warning("ws DENIED (missing/bad token) from %s", src)
        return web.Response(status=401,text="unauthorized")
    cams=_resolve(req.query.get("cam"))   # supports "all"/list -> live broadcast
    if not cams:
        log.warning("ws unknown cam=%r from %s", req.query.get("cam"), src)
        return web.Response(status=400,text="unknown cam")
    ws=web.WebSocketResponse(); await ws.prepare(req)
    log.info("ws OPEN cams=%s from %s -> opening OPTalk", cams, src)
    def _open_all():
        out=[]
        for c in cams:
            ip,u,pw=CAMS[c]; t=_mk(ip,u,pw)
            if t: out.append((c,t))
            else: log.warning("ws cam=%s OPTalk login FAILED", c)
        return out
    sessions=await asyncio.to_thread(_open_all)
    if not sessions:
        await ws.send_str('{"err":"login"}'); await ws.close(); return ws
    def _send_all(alaw):
        for c,t in sessions: t.send_alaw(alaw, False)
    def _close_all():
        for c,t in sessions:
            t.stop(); t.close()
    total=0; t0=time.time(); live=[c for c,_ in sessions]
    try:
        async for msg in ws:
            if msg.type==WSMsgType.BINARY:
                total+=len(msg.data)
                alaw=pcm16_to_alaw(msg.data)  # browser sends s16le 8k
                await asyncio.to_thread(_send_all, alaw)
            elif msg.type==WSMsgType.TEXT and msg.data=="ping":
                await ws.send_str("pong")
            elif msg.type==WSMsgType.ERROR:
                log.warning("ws cams=%s error: %s", live, ws.exception())
    except Exception as e:
        log.exception("ws cams=%s stream error: %s", live, e)
    finally:
        await asyncio.to_thread(_close_all)
        log.info("ws CLOSE cams=%s from %s (%.1fs, %d pcm bytes)", live, src, time.time()-t0, total)
    return ws

def _mk(ip,u,pw):
    t=OPTalk(ip,u,pw); t.login()
    if not t.session: t.close(); return None
    t.start(); return t

TALK_HTML = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Camera Talk</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0b1a0f;color:#d7ffe6;font:16px/1.4 system-ui,sans-serif;padding:16px;max-width:560px;margin:0 auto}
h1{font-size:1.1rem;color:#00ff6a;margin:.2em 0 .8em;font-weight:600}
label{display:block;font-size:.8rem;color:#7fce9e;margin:.9em 0 .3em}
select,textarea,input{width:100%;padding:.6em .7em;border-radius:.6em;border:1px solid #1e4d33;background:#0f2417;color:#d7ffe6;font:inherit}
textarea{resize:vertical;min-height:3.2em}
.row{display:flex;gap:.6em}.row>*{flex:1}
button{border:0;border-radius:.7em;padding:.85em 1em;font:600 1rem system-ui;color:#04140b;cursor:pointer}
#speak,#playurl,#playfile{background:#00ff6a;width:100%;margin-top:.6em}
#stop{background:#ff5252;color:#fff;width:100%;margin-top:.6em}
#ptt{background:#ff5252;color:#fff;width:100%;margin-top:.9em;user-select:none;touch-action:none}
#ptt.on{background:#00c853;color:#04140b}
#log{margin-top:1em;font-size:.8rem;color:#6fbf8f;min-height:1.2em}
hr{border:0;border-top:1px solid #1e4d33;margin:1.4em 0}
small{color:#5a9a72}
</style></head><body>
<h1>&#128266; Camera Talk</h1>
<label>Camera</label><select id=cam></select>
<hr>
<label>Text to speech</label>
<textarea id=txt placeholder="Type a message to play out the camera speaker..."></textarea>
<div class=row style="margin-top:.5em">
  <div style="flex:0 0 90px"><label style="margin:0">Voice</label><input id=voice value="en" style="text-align:center"></div>
  <div style="align-self:flex-end"><button id=speak>&#128227; Speak</button></div>
</div>
<hr>
<label>Play audio (MP3 / WAV / OGG)</label>
<input id=url placeholder="https://example.com/sound.mp3">
<button id=playurl>&#9654;&#65039; Play from URL</button>
<input id=file type=file accept="audio/*" style="margin-top:.6em">
<button id=playfile>&#9654;&#65039; Play file</button>
<button id=stop>&#9209;&#65039; Stop playback</button>
<small>Stop cuts TTS or media on the selected camera(s) instantly.</small>
<hr>
<label>Push to talk (hold)</label>
<button id=ptt>&#127908; Hold to Talk</button>
<div id=log>ready</div>
<script>
const $=id=>document.getElementById(id);
const base=location.origin;
const TOKEN=new URLSearchParams(location.search).get("token")||"";
const tq=TOKEN?("?token="+encodeURIComponent(TOKEN)):"";
const tqamp=TOKEN?("&token="+encodeURIComponent(TOKEN)):"";
async function loadCams(){
 try{const r=await fetch(base+"/cams");const d=await r.json();
  const opts=(d.cams.length>1?['<option value="all">&#128226; All cameras</option>']:[]).concat(d.cams.map(c=>`<option>${c}</option>`));
  $("cam").innerHTML=opts.join("");
  const q=new URLSearchParams(location.search).get("cam");
  if(q&&d.cams.includes(q))$("cam").value=q;}
 catch(e){$("log").textContent="cannot reach bridge";}
}
loadCams();
if(!window.isSecureContext||!navigator.mediaDevices){$("log").textContent="Open via https:// for push-to-talk (mic needs a secure context).";}
$("speak").onclick=async()=>{
 const cam=$("cam").value,text=$("txt").value.trim();if(!text)return;
 $("log").textContent="speaking on "+cam+"…";$("speak").disabled=true;
 try{const r=await fetch(base+"/say"+tq,{method:"POST",headers:{"content-type":"application/json"},
   body:JSON.stringify({cam,text,voice:$("voice").value||"en"})});
  const d=await r.json();$("log").textContent=d.ok?("played on "+cam):("error: "+JSON.stringify(d));}
 catch(e){$("log").textContent="error: "+e;}finally{$("speak").disabled=false;}
};
$("playurl").onclick=async()=>{
 const cam=$("cam").value,url=$("url").value.trim();if(!url)return;
 $("log").textContent="playing URL on "+cam+"…";$("playurl").disabled=true;
 try{const r=await fetch(base+"/play_url"+tq,{method:"POST",headers:{"content-type":"application/json"},
   body:JSON.stringify({cam,url})});
  const d=await r.json();$("log").textContent=d.ok?("played on "+cam):("error: "+JSON.stringify(d));}
 catch(e){$("log").textContent="error: "+e;}finally{$("playurl").disabled=false;}
};
$("playfile").onclick=async()=>{
 const cam=$("cam").value,f=$("file").files[0];if(!f)return;
 $("log").textContent="uploading + playing on "+cam+"…";$("playfile").disabled=true;
 try{const r=await fetch(base+"/play?cam="+encodeURIComponent(cam)+tqamp,{method:"POST",body:f});
  const d=await r.json();$("log").textContent=d.ok?("played on "+cam):("error: "+JSON.stringify(d));}
 catch(e){$("log").textContent="error: "+e;}finally{$("playfile").disabled=false;}
};
$("stop").onclick=async()=>{
 const cam=$("cam").value;
 try{const r=await fetch(base+"/stop?cam="+encodeURIComponent(cam)+tqamp);
  const d=await r.json();$("log").textContent="stopped: "+(d.stopped.join(", ")||"(nothing playing)");}
 catch(e){$("log").textContent="error: "+e;}
};
// PTT
let ctx,ws,node,stream,on=false;
async function pttStart(){
 if(on)return;on=true;const cam=$("cam").value;  // "all" broadcasts live to every cam
 $("ptt").className="on";$("ptt").innerHTML="&#128308; Talking…";$("log").textContent="connecting…";
 try{
  stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
  ctx=new (window.AudioContext||window.webkitAudioContext)();
  const src=ctx.createMediaStreamSource(stream);
  const proto=location.protocol==="https:"?"wss":"ws";
  ws=new WebSocket(proto+"://"+location.host+"/ws?cam="+encodeURIComponent(cam)+tqamp);
  ws.binaryType="arraybuffer";
  ws.onopen=()=>$("log").textContent="live @"+ctx.sampleRate+"Hz → "+cam;
  ws.onerror=()=>$("log").textContent="ws error";
  node=ctx.createScriptProcessor(2048,1,1);
  node.onaudioprocess=e=>{if(!on||!ws||ws.readyState!=1)return;
   const inp=e.inputBuffer.getChannelData(0),r=ctx.sampleRate/8000;
   const n=Math.floor(inp.length/r),out=new Int16Array(n);
   for(let i=0;i<n;i++){let v=inp[Math.floor(i*r)];v=Math.max(-1,Math.min(1,v));out[i]=v<0?v*32768:v*32767;}
   ws.send(out.buffer);};
  src.connect(node);node.connect(ctx.destination);
 }catch(e){
  let m="mic error: "+(e.name||e);
  if(!window.isSecureContext||!navigator.mediaDevices) m="Not a secure context — open the https:// address in a real browser (not an in-app view).";
  else if(e.name==="NotAllowedError") m="Microphone blocked. Tap the 🔒 lock in the address bar → Microphone → Allow, reload, then hold again. On iOS: Settings → Safari → Microphone → Allow.";
  else if(e.name==="NotFoundError") m="No microphone found on this device.";
  $("log").textContent=m;on=false;$("ptt").className="";$("ptt").innerHTML="&#127908; Hold to Talk";}
}
function pttStop(){if(!on)return;on=false;$("ptt").className="";$("ptt").innerHTML="&#127908; Hold to Talk";$("log").textContent="idle";
 try{node.disconnect();stream.getTracks().forEach(t=>t.stop());ws.close();ctx.close();}catch(e){}}
const b=$("ptt");
b.addEventListener("pointerdown",e=>{e.preventDefault();pttStart();});
b.addEventListener("pointerup",e=>{e.preventDefault();pttStop();});
b.addEventListener("pointercancel",pttStop);
b.addEventListener("pointerleave",()=>{if(on)pttStop();});
</script></body></html>"""

async def talk_page(req):
    if not _authed(req): return _deny()
    return web.Response(text=TALK_HTML, content_type="text/html")

PTT_CARD_JS = r"""
// XM Camera Talk custom Lovelace card — runs in HA's origin so mic + fetch work (no iframe).
// PTT (hold-to-talk) + optional TTS, MP3/WAV play (URL or file), broadcast, and Stop.
// Verbose: status shown to the user + console.debug("[xm-ptt] ...") for debugging.
// Backward compatible: a PTT-only config (bridge + cameras) behaves exactly as before.
class XmPttCard extends HTMLElement {
  setConfig(cfg){
    if(!cfg.bridge) throw new Error("xm-ptt-card: 'bridge' (host) is required");
    if(!cfg.cameras && !cfg.camera) throw new Error("xm-ptt-card: set 'cameras: [..]' or 'camera: ..'");
    this._cfg=cfg; this._on=false; this._render();
  }
  getCardSize(){ return 3; }
  _log(m){ try{ console.debug("%c[xm-ptt]","color:#0a8;font-weight:bold",m); }catch(e){} }
  _host(){ return this._cfg.bridge.replace(/^wss?:\/\//,"").replace(/^https?:\/\//,"").replace(/\/$/,""); }
  _https(){ return "https://"+this._host(); }
  _tq(sep){ return this._cfg.token?(sep+"token="+encodeURIComponent(this._cfg.token)):""; }
  _render(){
    const c=this._cfg;
    const cams=c.cameras||[c.camera];
    const talk=c.talk!==false;                 // PTT on by default
    const tts=!!c.tts, media=!!c.media;
    const stop=(c.stop!==undefined)?!!c.stop:(tts||media); // stop auto-on with tts/media
    const multi=cams.length>1;
    const wantAll=multi&&(tts||media||stop);   // "all" only affects say/play/stop
    if(!this.shadowRoot) this.attachShadow({mode:"open"});
    const root=this.shadowRoot;
    const inp="padding:.6em;border-radius:.6em;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);font:inherit;width:100%;box-sizing:border-box";
    const btn="border:0;border-radius:.7em;padding:.9em;font:600 1rem system-ui;color:#fff;cursor:pointer;width:100%";
    root.innerHTML=`
      <ha-card header="${c.title||"Camera Talk"}">
        <div style="padding:16px;display:flex;flex-direction:column;gap:10px">
          ${(multi)?`<select id=cam style="${inp}">
             ${wantAll?`<option value="all">🔊 All cameras</option>`:``}
             ${cams.map(x=>`<option>${x}</option>`).join("")}</select>`:``}
          ${tts?`<textarea id=txt placeholder="Type a message to speak…" style="${inp};min-height:3em;resize:vertical"></textarea>
             <button id=speak style="${btn};background:#2e7d32">📢 Speak</button>`:``}
          ${media?`<input id=url placeholder="https://…/sound.mp3" style="${inp}">
             <button id=playurl style="${btn};background:#2e7d32">▶️ Play from URL</button>
             <input id=file type=file accept="audio/*" style="${inp}">
             <button id=playfile style="${btn};background:#2e7d32">▶️ Play file</button>`:``}
          ${stop?`<button id=stop style="${btn};background:#b71c1c">⏹️ Stop</button>`:``}
          ${talk?`<button id=ptt style="${btn};background:#c0392b;user-select:none;touch-action:none">🎙️ Hold to Talk${(!multi)?` — ${cams[0]}`:``}</button>`:``}
          <div id=log style="font-size:.85rem;color:var(--secondary-text-color);min-height:1.2em;word-break:break-word">ready</div>
        </div>
      </ha-card>`;
    const $=id=>root.getElementById(id);
    this._els={ptt:$("ptt"),log:$("log"),cam:$("cam")};
    const camOf=()=> this._els.cam? this._els.cam.value : cams[0];
    // TTS
    if(tts) $("speak").addEventListener("click",()=>this._post("/say",{cam:camOf(),text:$("txt").value.trim(),voice:c.voice||"en"},"speak",()=>$("txt").value.trim()));
    // media URL
    if(media){
      $("playurl").addEventListener("click",()=>this._post("/play_url",{cam:camOf(),url:$("url").value.trim()},"play",()=>$("url").value.trim()));
      $("playfile").addEventListener("click",()=>this._playFile(camOf(),$("file").files[0]));
    }
    // stop
    if(stop) $("stop").addEventListener("click",()=>this._stop(camOf()));
    // PTT
    if(talk){
      if(!window.isSecureContext) this._els.log.textContent="⚠️ Open Home Assistant over https:// — the mic needs a secure context.";
      const b=this._els.ptt;
      b.addEventListener("pointerdown",e=>{e.preventDefault();this._start(camOf());});
      b.addEventListener("pointerup",e=>{e.preventDefault();this._end("idle");});
      b.addEventListener("pointercancel",()=>this._end("idle"));
      b.addEventListener("pointerleave",()=>{if(this._on)this._end("idle");});
    }
  }
  async _post(path,body,verb,guard){
    if(guard&&!guard()) return;
    const cam=body.cam, L=this._els.log;
    L.textContent=verb+"ing on "+cam+"…"; this._log(verb+" "+path+" cam="+cam);
    try{
      const r=await fetch(this._https()+path+this._tq("?"),{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json();
      L.textContent=d.ok?("✅ played on "+cam):("⛔ "+(d.err||JSON.stringify(d)));
      this._log(verb+" result "+JSON.stringify(d));
    }catch(e){ L.textContent="⛔ "+verb+" failed: "+e; this._log(verb+" error "+e); }
  }
  async _playFile(cam,f){
    if(!f) return; const L=this._els.log;
    L.textContent="uploading + playing on "+cam+"…"; this._log("play file "+f.name+" cam="+cam);
    try{
      const r=await fetch(this._https()+"/play?cam="+encodeURIComponent(cam)+this._tq("&"),{method:"POST",body:f});
      const d=await r.json(); L.textContent=d.ok?("✅ played on "+cam):("⛔ "+(d.err||JSON.stringify(d)));
    }catch(e){ L.textContent="⛔ play failed: "+e; }
  }
  async _stop(cam){
    const L=this._els.log; this._log("stop cam="+cam);
    try{
      const r=await fetch(this._https()+"/stop?cam="+encodeURIComponent(cam)+this._tq("&"));
      const d=await r.json(); L.textContent="⏹️ stopped: "+((d.stopped||[]).join(", ")||"(nothing playing)");
    }catch(e){ L.textContent="⛔ stop failed: "+e; }
  }
  async _start(cam){
    if(this._on) return; this._on=true; this._opened=false;
    const L=this._els.log,B=this._els.ptt,host=this._host();
    B.style.background="#e67e22"; B.textContent="⏳ starting…";
    L.textContent="requesting microphone…"; this._log("start cam="+cam+" host="+host);
    try{
      this._stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
      this._log("microphone granted");
    }catch(err){
      const nm=(err&&err.name)||err; this._log("mic error: "+nm);
      let m="mic error: "+nm;
      if(nm==="NotAllowedError") m="🔒 Microphone blocked. Allow it for this site (address-bar lock → Microphone → Allow), then hold again.";
      else if(nm==="NotFoundError") m="No microphone found on this device.";
      else if(!window.isSecureContext||!navigator.mediaDevices) m="⚠️ Not a secure context — open HA over https://.";
      L.textContent=m; this._end(); return;
    }
    const url="wss://"+host+"/ws?cam="+encodeURIComponent(cam)+this._tq("&");
    L.textContent="connecting to "+host+" …"; this._log("ws connecting to "+host+(this._cfg.token?" (with token)":" (no token)"));
    try{ this._ws=new WebSocket(url); }
    catch(e){ this._log("ws ctor failed: "+e); L.textContent="⛔ Bad bridge address: "+host; this._end(); return; }
    this._ws.binaryType="arraybuffer";
    this._timer=setTimeout(()=>{
      if(this._opened) return;
      this._log("ws OPEN timeout (8s) — bridge unreachable");
      L.textContent="⚠️ No response from the bridge ("+host+"). On home Wi-Fi this is usually DNS/hairpin — see the card README. On mobile data it should connect.";
      this._end();
    },8000);
    this._ws.onopen=()=>{ this._opened=true; clearTimeout(this._timer); this._log("ws OPEN → streaming");
      B.style.background="#27ae60"; B.textContent="🔴 Talking…"; L.textContent="🔴 live → "+cam+" — talk now"; this._pipe(); };
    this._ws.onerror=()=>{ this._log("ws error event"); };
    this._ws.onclose=(e)=>{ clearTimeout(this._timer); this._log("ws CLOSE code="+e.code+" reason="+(e.reason||"(none)")+" opened="+this._opened);
      if(!this._opened){
        let m="⚠️ Connection failed (code "+e.code+").";
        if(e.code===1006) m="⚠️ Can't reach the bridge ("+host+") — network/hairpin. On Wi-Fi add a local DNS rewrite for this host (see README).";
        else if(e.code===1008||e.code===4401||e.code===4403) m="⛔ Unauthorized — check the card 'token' matches the bridge TALK_TOKEN.";
        L.textContent=m;
      }
      if(this._on) this._end();
    };
  }
  _pipe(){
    this._ctx=new (window.AudioContext||window.webkitAudioContext)();
    if(this._ctx.state==="suspended"){ this._ctx.resume().catch(()=>{}); }
    const src=this._ctx.createMediaStreamSource(this._stream);
    this._node=this._ctx.createScriptProcessor(2048,1,1);
    this._sent=0;
    this._node.onaudioprocess=e=>{
      if(!this._on||!this._ws||this._ws.readyState!=1) return;
      const inp=e.inputBuffer.getChannelData(0),r=this._ctx.sampleRate/8000;
      const n=Math.floor(inp.length/r),out=new Int16Array(n);
      for(let i=0;i<n;i++){let v=inp[Math.floor(i*r)];v=Math.max(-1,Math.min(1,v));out[i]=v<0?v*32768:v*32767;}
      this._ws.send(out.buffer);
      const before=this._sent; this._sent+=n;
      if(Math.floor(this._sent/8000)>Math.floor(before/8000)) this._log("streamed "+Math.floor(this._sent/8000)+"s");
    };
    src.connect(this._node); this._node.connect(this._ctx.destination);
    this._log("audio pipeline @"+this._ctx.sampleRate+"Hz");
  }
  _end(msg){
    const wasOn=this._on; this._on=false;
    try{this._timer&&clearTimeout(this._timer);}catch(e){}
    try{this._node&&this._node.disconnect();}catch(e){}
    try{this._stream&&this._stream.getTracks().forEach(t=>t.stop());}catch(e){}
    try{this._ws&&this._ws.close();}catch(e){}
    try{this._ctx&&this._ctx.close();}catch(e){}
    if(this._els.ptt){
      const cams=this._cfg.cameras||[this._cfg.camera];
      this._els.ptt.style.background="#c0392b";
      this._els.ptt.textContent="🎙️ Hold to Talk"+((cams.length===1)?" — "+cams[0]:"");
    }
    if(msg&&this._els.log) this._els.log.textContent=msg;
    if(wasOn) this._log("ended"+(this._sent?(" ("+Math.floor((this._sent||0)/8000)+"s sent)"):""));
    this._sent=0;
  }
}
if(!customElements.get("xm-ptt-card")) customElements.define("xm-ptt-card", XmPttCard);
window.customCards=window.customCards||[];
window.customCards.push({type:"xm-ptt-card",name:"XM Camera Talk",description:"Talk, TTS, media playback + push-to-talk to XM/iCSee cameras via the talk bridge"});
"""

async def ptt_card(req):
    return web.Response(text=PTT_CARD_JS, content_type="application/javascript",
                        headers={"Cache-Control":"no-cache"})

async def cams(req): return web.json_response({"cams":list(CAMS.keys()),"playing":list(PLAYING.keys())})
async def health(req): return web.json_response({"ok":True,"cams":list(CAMS.keys()),"playing":list(PLAYING.keys())})

app=web.Application(client_max_size=20*1024*1024)
app.add_routes([web.post("/say",say),web.post("/play",play),web.post("/play_url",play_url),
                web.get("/stop",stop),web.post("/stop",stop),
                web.get("/ws",ws_handler),
                web.get("/talk",talk_page),web.get("/xm_ptt.js",ptt_card),web.get("/mic",talk_page),
                web.get("/cams",cams),web.get("/healthz",health)])
if __name__=="__main__":
    port=int(os.environ.get("PORT","8090"))
    log.info("xm-talk bridge starting on :%d — cams=%s token=%s",
             port, list(CAMS.keys()), "on" if TOKEN else "off")
    web.run_app(app, host="0.0.0.0", port=port, access_log=log)
