---
title: VOTA - Nigerian Voter WhatsApp Bot
description: WhatsApp voter information with local Whisper transcription and Piper speech
sdk: docker
app_port: 7860
---

# VOTA

VOTA is a Python FastAPI webhook service for the WhatsApp Cloud API. It accepts text and voice notes, transcribes voice locally with `faster-whisper` (Whisper `small` by default), looks up polling units in a local INEC CSV, and returns text plus a Piper-generated voice note when a Piper voice is configured.

Polling-unit facts are sourced only from `data/polling_units.csv`. If there is no match, VOTA does not guess; it replies: “I never see am for my list, but check inec.gov.ng”. The optional Meta Llama API is used only to phrase a short reply from the selected CSV facts. Without an API key, the app uses local templates. The Meta API is an external service and may have its own access, usage, or pricing terms; it is not required for the basic local-template mode.

## 1. Prepare the INEC data

Replace `data/polling_units.csv` with a CSV you are authorized to use. Keep the header exactly as follows:

```csv
state,lga,ward,polling_unit_name,code
```

Use UTF-8 encoding. The repository intentionally includes a header-only CSV, not fabricated or incomplete polling-unit data. Keyword search checks state, LGA, and ward fields against words in the incoming message and returns up to three equally best matches; the reply uses one verified match.

## 2. Run locally

Requires Python 3.11+, `ffmpeg`, and Piper's `piper` executable (installed through `piper-tts`). Whisper model files are downloaded by CTranslate2 on first voice message; initial download and CPU inference can take time.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit values; do not commit secrets
export $(grep -v '^#' .env | xargs)  # optional convenience; JSON values may require manual export
uvicorn app.main:app --host 0.0.0.0 --port 7860
```

For production, set environment variables through the host's secret manager rather than sourcing a `.env` file. Check `http://localhost:7860/health` for service health.

## 3. Configure WhatsApp Cloud API

1. In Meta for Developers, create or select a Meta app and add the WhatsApp product. Complete the business/phone-number setup and obtain a permanent or long-lived access token with the required WhatsApp messaging permissions. Set `WHATSAPP_TOKEN` to that token and `WHATSAPP_APP_SECRET` to the Meta app secret. Never place these secrets in source control.
2. Deploy this app to a public HTTPS URL. In the WhatsApp product's webhook settings, set the callback URL to `https://YOUR-SPACE.hf.space/webhook` and set a private verify token of your choice in both Meta's form and the Space secret `WHATSAPP_VERIFY_TOKEN`.
3. Subscribe the webhook to the `messages` field. Meta calls `GET /webhook` to verify the callback. The app validates `X-Hub-Signature-256` when `WHATSAPP_APP_SECRET` is set.
4. Add a test recipient and send a text and a voice note to the WhatsApp test number. The service acknowledges the webhook immediately, then handles the message in a background task. Configure the WhatsApp number's allowed recipients and messaging templates according to Meta's current rules.
5. Keep `WHATSAPP_GRAPH_API_VERSION` aligned with a Graph API version enabled for your Meta app. The default is `v21.0`; change it if Meta retires that version.

## 4. Deploy to Hugging Face Spaces

1. Create a new Space and select **Docker** as the SDK.
2. Upload or push this repository, including your real `data/polling_units.csv` and the Dockerfile.
3. In Space **Settings → Variables and secrets**, set `WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, and `WHATSAPP_APP_SECRET`. Add `LLAMA_API_KEY` only if you have valid access and want the optional external phrasing service.
4. Add Piper voice model files and their matching `.onnx.json` config files to the Space or a mounted persistent storage location. Set `PIPER_VOICE_DIR` and `PIPER_VOICE_MAP` as appropriate. The app uses the requested-language voice when configured, otherwise the configured English voice. When no matching English Piper voice is installed, text still works and audio is omitted.
5. Set `WHISPER_MODEL=small`. CPU-only Spaces may have substantial first-start and inference latency; consider a smaller Whisper model if latency or memory is a problem. The model is open source, but downloads and runtime compute consume Space resources.
6. Once the Space is running, use its `https://...hf.space` URL as the WhatsApp webhook callback.

### Piper language voices

Piper only speaks languages for which you provide a compatible voice model. Voice availability depends on the model catalog and file license. For example, with a compatible English voice in `/app/voices/en_US-lessac-medium.onnx` and its `.onnx.json` file, set:

```text
PIPER_VOICE_MAP={"english":"en_US-lessac-medium.onnx"}
```

Add keys `hausa`, `igbo`, `yoruba`, and `pidgin` only when you have suitable voice models. Unsupported languages fall back to the configured English voice. This fallback concerns audio only; the text reply still follows the app's language selection where available. The sample setup does not download or bundle voice assets.

## Configuration

See `.env.example` for all environment variables. `LLAMA_API_URL` and `LLAMA_MODEL` are configurable because access and API formats can change. Llama is optional and never supplies polling-unit facts. If a generated answer omits the verified polling-unit name or ward, the local grounded template is used instead.

## Important limitations

- A header-only CSV returns no polling-unit matches. Load the authoritative current dataset before enabling live voter queries.
- Whisper's built-in language detection does not reliably distinguish Nigerian Pidgin from English, and language availability varies by model. The app uses simple Pidgin word heuristics and supports Whisper language tags for Hausa, Igbo, and Yoruba when the selected model recognizes them; verify quality with representative local recordings.
- Basic text language detection is deliberately conservative. It reliably flags a few Pidgin markers and otherwise defaults to English; voice notes use Whisper's detected language. For production-quality written Hausa, Igbo, and Yoruba detection, add and test a local language-identification model.
- Sending the text response and the audio response is two separate WhatsApp messages. If Piper, ffmpeg, or a voice file is unavailable, the text message still sends.
- This app provides polling-unit lookup, not official election guidance. Confirm the CSV against INEC's latest data and direct users to INEC for authoritative updates.

## License

The application code is released under the MIT License (add a `LICENSE` file if redistributing). Whisper/faster-whisper, Piper, their model weights, and Meta services have separate licenses and terms; review those before deployment.
