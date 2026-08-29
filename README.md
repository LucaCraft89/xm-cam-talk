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
- 📢 **Play a WAV/any audio** — `POST /play` with an audio file (ffmpeg
  transcodes to G.711 A-law 8 kHz automatically).
- 🎙️ **Live hold-to-talk** — a minimal web page captures your browser mic and
  streams it to the speaker over WebSocket.
- 🏠 **Home Assistant** — UI config-flow, auto-discovers cameras from the
  bridge, a `notify` entity per camera. HACS-installable.
- 🔒 **Local & credential-free in source** — camera credentials live only in
  your environment, never in the image or this repo.

## How it works

```
HA notify / POST /say ─┐
POST /play (wav)      ─┤→  bridge  ──DVRIP OPTalk (TCP 34567)──►  camera speaker
browser mic → WS /ws ─┘   (aiohttp)     Claim 1434 → Start/data 1430
                                        frames: 00 00 01 fa 0e 02 <u16 len> + G711A
```

Developed and confirmed against an **iCSee** camera — model
**`XM530V200_X6C-WEQ_8M`** (XM530 SoC, 8 MP dual-lens) — the kind managed
by the iCSee / XMEye mobile app. The talk channel is duplex —
the camera streams its own mic back while you talk, which the bridge also uses
to **self-test** the speaker (it can detect its own tone coming back).

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
title: Push-to-Talk
bridge: talk.example.com
token: YOUR_TALK_TOKEN
cameras: [cam2, cam3, cam4]     # or a single: camera: cam3
```

First use prompts for microphone permission — grant it for your HA URL. HA must
be served over **HTTPS** (secure context for the mic). If HA sets a strict
`Content-Security-Policy`, allow the bridge host in `script-src` / `connect-src`
(default HA sets none).

### Complete dashboard section

The whole **Camera Talk** section — TTS controls, a Speak button, the live
push-to-talk card, and a full-screen fallback button — as one `sections`-view
grid (this is exactly the layout the setup above produces):

```yaml
type: grid
cards:
  - type: heading
    heading: Camera Talk
    icon: mdi:bullhorn
  - type: entities
    entities:
      - entity: input_select.xm_talk_camera
        name: Camera
      - entity: input_text.xm_talk_message
        name: Message
  - type: button
    name: Speak
    icon: mdi:volume-high
    tap_action:
      action: perform-action
      perform_action: script.xm_talk_speak
  # live push-to-talk — needs the xm-ptt-card plugin installed
  - type: custom:xm-ptt-card
    title: Push-to-Talk
    bridge: talk.example.com
    token: YOUR_TALK_TOKEN
    cameras: [cam2, cam3, cam4]
  # fallback: opens the bridge's own /talk page full-screen (mic needs top-level)
  - type: button
    name: Push-to-Talk (full screen)
    icon: mdi:microphone
    tap_action:
      action: url
      url_path: https://talk.example.com/talk?token=YOUR_TALK_TOKEN
```

## API

| Method | Path | Body | Effect |
|-------:|------|------|--------|
| `POST` | `/say` | `{"cam","text","voice"}` | TTS → speaker |
| `POST` | `/play?cam=` | audio bytes (any ffmpeg format) | audio → speaker |
| `GET`  | `/talk` | — | combined UI: camera dropdown + TTS + push-to-talk |
| `GET`  | `/mic?cam=` | — | single-camera live-talk page |
| `WS`   | `/ws?cam=` | binary s16le 8 kHz mono | mic → speaker |
| `GET`  | `/cams` | — | list configured cameras |
| `GET`  | `/healthz` | — | health check |

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

## License

MIT — see [LICENSE](LICENSE). Not affiliated with XM, iCSee, or Home Assistant.
