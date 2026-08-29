#!/usr/bin/env python3
"""XM DVRIP OPTalk bridge: push TTS/WAV to camera speakers + live browser-mic talk.
Endpoints:
  POST /say   {"cam":"cam3","text":"...","voice":"it"}     -> TTS out speaker
  POST /play?cam=cam3   body = WAV bytes                    -> WAV out speaker
  GET  /mic?cam=cam3                                        -> mic web page (live talk)
  WS   /ws?cam=cam3     binary 16-bit LE 8k mono PCM frames -> speaker
  GET  /cams  /healthz
"""
import asyncio, hashlib, json, os, struct, subprocess, time
from aiohttp import web, WSMsgType

CHARS="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
def sofia_hash(p):
    m=hashlib.md5(p.encode()).digest()
    return "".join(CHARS[(m[i]+m[i+1])%62] for i in range(0,16,2))

# cam -> (ip, user, pass). Override via CAMS env (JSON) if needed.
# cam -> [ip, dvrip_user, dvrip_pass]. Set via CAMS env (JSON). No creds in source.
CAMS=json.loads(os.environ.get("CAMS","{}"))

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
    def send_alaw(self, alaw, realtime=True):
        CH=320; t0=time.time()
        self.sock.setblocking(False)
        for n,i in enumerate(range(0,len(alaw),CH)):
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

def _wav_to_alaw(wav):
    p=subprocess.run(["ffmpeg","-hide_banner","-v","error","-i","pipe:0","-ar","8000","-ac","1","-f","alaw","pipe:1"],
                     input=wav,capture_output=True,check=True)
    return p.stdout

def _push(cam, alaw):
    ip,u,pw=CAMS[cam]
    t=OPTalk(ip,u,pw)
    try:
        r=t.login()
        if not t.session: return {"ok":False,"err":"login failed","ret":r}
        t.start(); t.send_alaw(alaw); t.stop()
        return {"ok":True,"cam":cam,"ms":int(len(alaw)/8.0)}
    finally:
        t.close()

async def say(req):
    d=await req.json()
    cam=d.get("cam"); 
    if cam not in CAMS: return web.json_response({"ok":False,"err":"unknown cam"},status=400)
    alaw=await asyncio.to_thread(_tts_to_alaw, d.get("text",""), d.get("voice","it"))
    return web.json_response(await asyncio.to_thread(_push, cam, alaw))

async def play(req):
    cam=req.query.get("cam")
    if cam not in CAMS: return web.json_response({"ok":False,"err":"unknown cam"},status=400)
    wav=await req.read()
    alaw=await asyncio.to_thread(_wav_to_alaw, wav)
    return web.json_response(await asyncio.to_thread(_push, cam, alaw))

async def ws_handler(req):
    cam=req.query.get("cam")
    if cam not in CAMS: return web.Response(status=400,text="unknown cam")
    ws=web.WebSocketResponse(); await ws.prepare(req)
    ip,u,pw=CAMS[cam]
    t=await asyncio.to_thread(lambda:(_mk(ip,u,pw)))
    if not t: 
        await ws.send_str('{"err":"login"}'); await ws.close(); return ws
    loop=asyncio.get_event_loop()
    try:
        async for msg in ws:
            if msg.type==WSMsgType.BINARY:
                alaw=pcm16_to_alaw(msg.data)  # browser sends s16le 8k
                await asyncio.to_thread(t.send_alaw, alaw, False)
            elif msg.type==WSMsgType.TEXT and msg.data=="ping":
                await ws.send_str("pong")
    finally:
        await asyncio.to_thread(lambda:(t.stop(),t.close()))
    return ws

def _mk(ip,u,pw):
    t=OPTalk(ip,u,pw); t.login()
    if not t.session: t.close(); return None
    t.start(); return t

MIC_HTML="""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Talk %CAM%</title><style>body{background:#111;color:#eee;font:16px system-ui;text-align:center;padding:2em}
button{font-size:1.4em;padding:1em 2em;border-radius:1em;border:0;background:#c33;color:#fff}
button.on{background:#3a3}#s{margin-top:1em;color:#9ad}</style>
<h2>%CAM%</h2><button id=b>Hold to Talk</button><div id=s>idle</div>
<script>
let ctx,ws,node,stream,on=false;const cam="%CAM%";
async function start(){
 on=true;b.className="on";b.textContent="Talking…";s.textContent="connecting";
 stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
 ctx=new AudioContext();const src=ctx.createMediaStreamSource(stream);
 const proto=location.protocol==="https:"?"wss":"ws";
 ws=new WebSocket(proto+"://"+location.host+"/ws?cam="+cam);ws.binaryType="arraybuffer";
 ws.onopen=()=>s.textContent="live @"+ctx.sampleRate+"Hz";
 node=ctx.createScriptProcessor(2048,1,1);
 node.onaudioprocess=e=>{if(!on||ws.readyState!=1)return;
  const inp=e.inputBuffer.getChannelData(0),r=ctx.sampleRate/8000;
  const n=Math.floor(inp.length/r),out=new Int16Array(n);
  for(let i=0;i<n;i++){let v=inp[Math.floor(i*r)];v=Math.max(-1,Math.min(1,v));out[i]=v<0?v*32768:v*32767;}
  ws.send(out.buffer);};
 src.connect(node);node.connect(ctx.destination);}
function stop(){on=false;b.className="";b.textContent="Hold to Talk";s.textContent="idle";
 try{node.disconnect();stream.getTracks().forEach(t=>t.stop());ws.close();ctx.close();}catch(e){}}
b.onpointerdown=start;b.onpointerup=stop;b.onpointerleave=()=>{if(on)stop()};
</script>"""

async def mic(req):
    cam=req.query.get("cam","cam3")
    return web.Response(text=MIC_HTML.replace("%CAM%",cam),content_type="text/html")

async def cams(req): return web.json_response({"cams":list(CAMS.keys())})
async def health(req): return web.json_response({"ok":True})

app=web.Application(client_max_size=20*1024*1024)
app.add_routes([web.post("/say",say),web.post("/play",play),web.get("/ws",ws_handler),
                web.get("/mic",mic),web.get("/cams",cams),web.get("/healthz",health)])
if __name__=="__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT","8090")))
