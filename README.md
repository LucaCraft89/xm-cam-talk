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

## 3. Live hold-to-talk

Open `http://<bridge>:8090/mic?cam=front` and hold the button.

⚠️ Browsers only allow microphone access over **HTTPS** (or `localhost`). To
use it from a phone, put the bridge behind a reverse proxy with a certificate
(Nginx Proxy Manager / NPMplus / Caddy / Traefik) and open the `https://…/mic`
URL. Embed it in a dashboard with a *Webpage* card.

## API

| Method | Path | Body | Effect |
|-------:|------|------|--------|
| `POST` | `/say` | `{"cam","text","voice"}` | TTS → speaker |
| `POST` | `/play?cam=` | audio bytes (any ffmpeg format) | audio → speaker |
| `GET`  | `/mic?cam=` | — | live-talk web page |
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
