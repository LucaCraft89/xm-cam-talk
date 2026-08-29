# XM Camera Talk 🔊

Two-way audio for cheap **XM / iCSee / Sofia** IP cameras (XM530, XMeye, the
"dual-lens 8MP" boxes, etc.) — **without the app**.

These cameras (sold under the **iCSee** / XMEye apps) have a speaker, but
their talk-back does **not** use ONVIF or an RTSP backchannel, so `go2rtc` / Frigate / the *advanced-camera-card* cannot
drive it. The audio rides the proprietary **DVRIP `OPTalk`** channel (Sofia,
TCP `34567`) — the same channel the **iCSee** app uses for its talk button. This project speaks that
protocol directly and exposes it as:

- a tiny **Docker bridge** with an HTTP + WebSocket API, and
- a **Home Assistant integration** (config-flow, one *notify* entity per camera).

Say something out any camera from an automation, a dashboard button, or hold a
button on your phone and talk live.

---

## Features

- 🗣️ **Text-to-speech to a camera** — `notify.<camera>` with a message; the
  bridge renders it (espeak-ng) and plays it out the speaker.
- 📢 **Play any audio** — `POST /play` with a WAV/MP3/OGG/… file, or `POST
  /play_url` with a link; ffmpeg transcodes to G.711 A-law 8 kHz automatically.
- 📡 **Broadcast** — target one camera, a comma list, or `all` (`cam` accepts
  `"all"`, `"cam2,cam3"`, or a JSON array). Plays to every camera at once.
- ⏹️ **Stop** — `POST`/`GET /stop?cam=` kills TTS or media mid-playback instantly.
- 🎙️ **Live hold-to-talk** — a minimal web page captures your browser mic and
  streams it to the speaker over WebSocket.
- 🏠 **Home Assistant** — UI config-flow, auto-discovers cameras from the
  bridge, a `notify` entity per camera, and an all-in-one Lovelace card (TTS +
  media + stop + broadcast + push-to-talk). HACS-installable.
- ❤️ **Self-healing** — a `/healthz` endpoint plus the optional `autoheal`
  service restart the bridge if it ever wedges (see `compose.example.yml`).
- 🔒 **Local & credential-free in source** — camera credentials live only in
  your environment, never in the image or this repo.

## How it works

### The problem

These cameras expose ONVIF and RTSP, but **neither carries a working audio
back-channel** — a `DESCRIBE` with `Require: www.onvif.org/ver20/backchannel`
is ignored, and the RTSP audio track is `recvonly` (the camera's mic only). So
`go2rtc` / Frigate / the advanced-camera-card cannot talk *to* the camera. The
iCSee/XMEye app can, because it uses the vendor's own protocol.

### The protocol (DVRIP `OPTalk`)

Two-way audio rides **DVRIP / Sofia** on **TCP 34567** — the same channel the
app uses. Reverse-engineered and confirmed against an **iCSee** camera, model
**`XM530V200_X6C-WEQ_8M`** (XM530 SoC, 8 MP dual-lens):

```
login (per-device iCSee user/pass; 'admin' is rejected)
  → Claim   msgid 1434  {"Name":"OPTalk","OPTalk":{"Action":"Claim",
                          "AudioFormat":{"EncodeType":"G711_ALAW",
                          "SampleRate":8,"SampleBit":8,"BitRate":128}}}   → Ret 100
  → Start   msgid 1430  (same JSON, Action:"Start")   ← camera now opens a
                          DUPLEX channel and streams its own mic back
  → audio   msgid 1430  frames:  00 00 01 fa 0e 02 <uint16-LE len> + G711A
                          320-byte payloads (40 ms) paced in real time
  → Stop    msgid 1434  Action:"Stop"
```

`msgid 1436` returns `Ret 102` (unsupported) — the data channel is **1430**,
not 1436. Because the channel is duplex (the camera echoes its mic while you
talk), the bridge can even **self-test** the speaker: play a tone, then detect
that same tone coming back on the camera's microphone.

### The pieces

```
                         ┌──────────────────── this repo ───────────────────┐
HA notify.<cam> ─────────┐
(server-side, TTS)       │
                         ├─► POST /say       (espeak-ng → G711A)  ─┐
POST /play, /play_url ───┤   POST /play(_url) (ffmpeg → G711A)      │  cam=all
                         │   POST /stop       (cancel mid-play)     ├─► DVRIP OPTalk ─► 🔊 camera(s)
xm-ptt-card (browser) ───┘   WS /ws (browser mic, s16le 8k)        ┘   (TCP 34567)      speaker
   live push-to-talk         └──────────── bridge (aiohttp, Docker) ───────────┘
```

1. **bridge** (`/bridge`, Docker) — speaks OPTalk to the cameras and exposes a
   small HTTP + WebSocket API. `espeak-ng` renders TTS, `ffmpeg` transcodes any
   audio to G.711 A-law 8 kHz.
2. **Home Assistant integration** (`/custom_components/xm_cam_talk`) — a config
   flow that discovers cameras from the bridge and creates one
   `notify.<camera>` entity each. `notify.send_message` → `POST /say`. This runs
   **server-side**, so TTS works from anywhere HA does, without exposing the
   bridge.
3. **push-to-talk card**
   ([xm-ptt-card](https://github.com/LucaCraft89/xm-ptt-card)) — a Lovelace card
   that captures the **browser microphone** and streams 8 kHz PCM over a
   WebSocket to `/ws`. It must run in HA's own origin (a custom card, **not** an
   iframe — browsers block the mic in a cross-origin iframe), and needs the
   bridge reachable over **HTTPS** from the browser.

### Two audio paths

- **Text-to-speech** — HA → bridge → camera, entirely server-side. No mic, no
  bridge exposure needed; works over the internet through HA.
- **Live push-to-talk** — browser mic → bridge → camera. Needs the bridge on
  **HTTPS** (secure context + not mixed-content) and reachable from the browser.
  Behind Cloudflare a plain WebSocket is fine (unlike WebRTC media). If HA is on
  your LAN and the bridge host resolves to a public IP, add a split-horizon DNS
  entry so LAN clients reach the reverse proxy directly (avoids NAT-hairpin).

### Security

The bridge has no login. Set **`TALK_TOKEN`** and it requires `?token=` for any
request that arrives through a reverse proxy (detected via `X-Forwarded-For`),
while direct LAN calls stay open so Home Assistant keeps working. Keep it on
your LAN or behind an access list otherwise — anyone who can reach it can make
your cameras talk.

## 1. Run the bridge

```bash
cd bridge
cp compose.example.yml compose.yml     # then edit CAMS with your cameras
docker compose up -d --build
```

`CAMS` is a JSON map of `name -> [ip, dvrip_user, dvrip_pass]`. Use the
**per-device iCSee username/password**, not `admin` (admin is rejected by the
firmware). Example:

> **Where to find the per-device username/password:** in the **iCSee** app, open
> the camera's **Settings → Device Information → Device Login**. Tap the
> **closed-eye icon** next to the username and the password to reveal them.
> (These are per-device credentials — `admin` is rejected by the firmware.)

```yaml
environment:
  CAMS: '{"front":["192.168.1.50","user1","pass1"],"yard":["192.168.1.51","user2","pass2"]}'
```

Test it:

```bash
curl localhost:8090/cams
curl -X POST localhost:8090/say -H 'content-type: application/json' \
     -d '{"cam":"front","text":"hello there","voice":"en"}'
```

Prebuilt image (from CI): `ghcr.io/lucacraft89/xm-cam-talk:latest`.

### Max the speaker volume (optional)

The camera's own output volume defaults to ~50%. Raise it once over DVRIP
(`fVideo.Volume`, 0-100) with any XM tool, or the app, for louder talk-down.

## 2. Home Assistant integration

**HACS** → Custom repositories → add this repo (type *Integration*) → install →
restart HA. Or copy `custom_components/xm_cam_talk` into your `config/`.

Then **Settings → Devices & Services → Add Integration → "XM Camera Talk"**,
enter the bridge URL (e.g. `http://192.168.1.186:8090`). You get one
`notify.<camera>` entity per camera.

```yaml
# automation example
- action: notify.send_message
  target:
    entity_id: notify.front
  data:
    message: "The gate is open"
```

Pair it with HA's own TTS by posting the generated audio to `/play` if you
prefer a nicer voice than espeak-ng.

## 3. Live push-to-talk

Open `http://<bridge>:8090/talk` for the full UI: a **camera dropdown**, a
**text-to-speech** box, and a **hold-to-talk** button.

⚠️ Browsers only allow microphone access over **HTTPS** (or `localhost`). To
use push-to-talk from a phone, put the bridge behind a reverse proxy with a
certificate (Nginx Proxy Manager / NPMplus / Caddy / Traefik) and open the
`https://…/talk` URL.

## 4. Securing internet access

The bridge has no login. If you expose it, set **`TALK_TOKEN`** (see
`compose.example.yml`). Requests arriving through a reverse proxy then require
`?token=<TALK_TOKEN>` (or an `X-Talk-Token` header); direct LAN requests stay
open so Home Assistant keeps working locally. Use the token in your URLs, e.g.
`https://talk.example.com/talk?token=…`.

## 5. Home Assistant dashboard

For **text-to-speech**, use the native `notify` entities (they call the bridge
server-side over your LAN, so they work from anywhere the HA app does — no need
to expose the bridge at all):

**a) Two helpers** (create as UI helpers, or in `configuration.yaml`):

```yaml
input_select:
  xm_talk_camera:
    name: XM Talk Camera
    options: [cam2, cam3, cam4]   # your camera names, as configured on the bridge
    icon: mdi:cctv

input_text:
  xm_talk_message:
    name: XM Talk Message
    max: 255
    icon: mdi:message-text
```

**b) A script** (`scripts.yaml` → `script.xm_talk_speak`) that speaks the typed
message out the selected camera. The integration names its entities
`notify.xm_talk_<camera>_<camera>`, hence the doubled template:

```yaml
xm_talk_speak:
  alias: XM Talk Speak
  icon: mdi:bullhorn
  mode: queued
  max: 5
  sequence:
    - action: notify.send_message
      target:
        entity_id: >-
          notify.xm_talk_{{ states('input_select.xm_talk_camera') }}_{{ states('input_select.xm_talk_camera') }}
      data:
        message: "{{ states('input_text.xm_talk_message') }}"
```

**c) A dashboard section** — see the complete YAML at the end of this section.

Avoid embedding `/talk` in an **iframe** card: many reverse proxies send
`X-Frame-Options: SAMEORIGIN` (blocking the embed), and browsers block the
microphone inside a cross-origin iframe anyway.

### Push-to-talk *inside* Home Assistant (custom card)

Push-to-talk can run natively in the dashboard — no iframe, no external page —
via a companion custom card that runs in HA's own origin, so the microphone
works like on any HA page.

**Recommended — install via HACS:** add the
**[xm-ptt-card](https://github.com/LucaCraft89/xm-ptt-card)** repo as a HACS
*Dashboard* custom repository and install it. HACS serves it same-origin
(`/hacsfiles/…`), so it loads identically on LAN and over the internet.

**Manual alternative:** the bridge also serves the same card at `/xm_ptt.js`.
Register it as a Lovelace **resource** (Settings → Dashboards → ⋮ → Resources):
URL `https://talk.example.com/xm_ptt.js`, type **JavaScript Module**. (Note:
loading it from the bridge host means it must be reachable from the browser,
which the HACS install avoids.)

Then add the card (`bridge` is your talk-bridge host, `token` only if you set
`TALK_TOKEN`):

```yaml
type: custom:xm-ptt-card
title: Camera Talk
bridge: talk.example.com
token: YOUR_TALK_TOKEN
cameras: [cam2, cam3, cam4]     # or a single: camera: cam3
tts: true                       # show the text-to-speech box + Speak button
media: true                     # show URL / file play buttons
stop: true                      # show a Stop button (on by default when tts/media)
# talk: true                    # push-to-talk (default true; set false to hide)
# voice: en                     # espeak-ng voice for tts
```

The one card does everything: a camera dropdown (with an **All cameras** option
when you list more than one), a TTS box, MP3/WAV play from a URL or an uploaded
file, a Stop button, and hold-to-talk. It runs in HA's own origin and calls the
bridge directly, so it works locally **and** over the internet.

| Option | Default | What it does |
|--------|---------|--------------|
| `bridge` | — (required) | talk-bridge host, e.g. `talk.example.com` |
| `cameras` / `camera` | — (required) | list of cameras, or one camera |
| `token` | — | your `TALK_TOKEN` (only if the bridge is proxied) |
| `title` | `Camera Talk` | card header |
| `talk` | `true` | push-to-talk button |
| `tts` | `false` | text-to-speech box + Speak |
| `media` | `false` | play from URL + play file |
| `stop` | `tts \|\| media` | Stop button |
| `voice` | `en` | espeak-ng voice for TTS |

First use prompts for microphone permission — grant it for your HA URL. HA must
be served over **HTTPS** (secure context for the mic). If HA sets a strict
`Content-Security-Policy`, allow the bridge host in `script-src` / `connect-src`
(default HA sets none).

Prefer native HA helpers over the card? A `notify.<camera>` entity is created
per camera, so `notify.send_message` (or a `script`) can speak to one camera
without the card. The card is the way to get media/stop/broadcast in the UI.

## API

`cam` may be a single name, a comma list, `all` / `*`, or a JSON array. Use
`cams` interchangeably with `cam`.

| Method | Path | Body | Effect |
|-------:|------|------|--------|
| `POST` | `/say` | `{"cam","text","voice"}` | TTS → speaker(s) |
| `POST` | `/play?cam=` | audio bytes (WAV/MP3/OGG/…) | audio → speaker(s) |
| `POST` | `/play_url` | `{"cam","url"}` | fetch a link → speaker(s) |
| `POST`/`GET` | `/stop?cam=` | — | kill playback now (defaults to all if no cam) |
| `GET`  | `/talk` | — | combined UI: dropdown + TTS + media + stop + push-to-talk |
| `GET`  | `/mic?cam=` | — | alias of `/talk` |
| `GET`  | `/xm_ptt.js` | — | the Lovelace card module |
| `WS`   | `/ws?cam=` | binary s16le 8 kHz mono | mic → speaker |
| `GET`  | `/cams` | — | configured cameras + what's currently playing |
| `GET`  | `/healthz` | — | health check (used by the container healthcheck) |

Multi-cam responses return per-camera results:
`{"ok":true,"cams":[...],"results":[{"ok":true,"cam":"cam3","stopped":false}, …]}`.

## Security notes

- The bridge has **no authentication** — keep it on your LAN. If you expose the
  `/mic` page over HTTPS, put an access list / auth in front of it at the proxy.
- Anyone who can reach the bridge can make your cameras talk. Treat it like any
  other local admin surface.
- Camera credentials are read from the `CAMS` env only. Don't commit your
  `compose.yml` (it's git-ignored here).

## Compatibility

Works with **XM / iCSee / XMEye** cameras exposing DVRIP on port `34567` and
reporting `PreviewFunction.Talk: true`. Tested model:
`XM530V200_X6C-WEQ_8M` (8 MP dual-lens, iCSee app). Yoosee / Hicam and other non-DVRIP firmwares are
**not** supported (no local talk protocol).

## Logging & debugging

Everything is verbose so you (and the debugger) can see what happens.

**Home Assistant integration** — turn on debug logs:

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.xm_cam_talk: debug
```

Then **Settings → System → Logs**. You'll see the bridge probe on setup, each
`Speaking on <cam> via <url>`, the bridge's reply, and clear errors (HTTP
status + body) if a call fails.

**Bridge (server)** — it logs every request:

```bash
docker logs -f talk-bridge
```

Lines like `say cam=cam3 chars=12`, `push cam=cam3 ip=… OK (1164ms)`,
`ws OPEN cam=cam3 from …`, `ws CLOSE … (3.2s, 51200 pcm bytes)`, and
`… DENIED (missing/bad token)` for rejected requests. Set `LOG_LEVEL: DEBUG`
in the compose `environment:` for more detail.

**Push-to-talk card** — open the browser dev-tools **Console**; every step is
logged with a `[xm-ptt]` prefix (mic granted, ws connecting/open/close with the
close code, seconds streamed), and the card's status line always shows the
current state or the reason it failed.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with XM, iCSee, or Home Assistant.
