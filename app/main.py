from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("vota")
app = FastAPI(title="VOTA", description="WhatsApp voter-information assistant for Nigeria", version="1.0.0")
GRAPH_API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
CSV_PATH = Path(os.getenv("INEC_CSV_PATH", "data/polling_units.csv"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "VOTA"}


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> str:
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if hub_mode == "subscribe" and expected and hmac.compare_digest(hub_verify_token or "", expected):
        return hub_challenge or ""
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    body = await request.body()
    secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    for message, phone_number_id in extract_messages(payload):
        background_tasks.add_task(process_message, message, phone_number_id)
    return {"status": "received"}


def extract_messages(payload: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    found = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for message in value.get("messages", []):
                found.append((message, phone_number_id))
    return found


async def process_message(message: dict[str, Any], phone_number_id: str) -> None:
    token, sender = os.getenv("WHATSAPP_TOKEN", ""), message.get("from", "")
    if not token or not sender or not phone_number_id:
        log.error("Missing WhatsApp token, sender, or phone number ID")
        return
    try:
        text, language = await incoming_text(message, token)
        rows = find_polling_units(text)
        answer = await compose_reply(text, language, rows)
        await send_whatsapp_text(phone_number_id, sender, answer, token)
        audio_path = await synthesize(answer, language)
        if audio_path:
            try:
                media_id = await upload_audio(phone_number_id, audio_path, token)
                await send_whatsapp_audio(phone_number_id, sender, media_id, token)
            finally:
                audio_path.unlink(missing_ok=True)
    except Exception:
        log.exception("Failed to process WhatsApp message %s", message.get("id", "unknown"))


async def incoming_text(message: dict[str, Any], token: str) -> tuple[str, str]:
    if message.get("type") == "text":
        text = message.get("text", {}).get("body", "").strip()
        return text, detect_text_language(text)
    if message.get("type") == "audio":
        media_id = message.get("audio", {}).get("id")
        if not media_id:
            raise ValueError("Audio message has no media ID")
        path = await download_media(media_id, token)
        try:
            return await asyncio.to_thread(transcribe_audio, path)
        finally:
            path.unlink(missing_ok=True)
    return "", "english"


async def download_media(media_id: str, token: str) -> Path:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        metadata = await client.get(f"{GRAPH_BASE}/{media_id}", headers=headers)
        metadata.raise_for_status()
        media = await client.get(metadata.json()["url"], headers=headers)
        media.raise_for_status()
    mime = media.headers.get("content-type", "").split(";")[0]
    suffix = ".mp3" if "mpeg" in mime or "mp3" in mime else ".m4a" if "mp4" in mime else ".ogg"
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="vota-in-")
    os.close(fd)
    path = Path(name)
    path.write_bytes(media.content)
    return path


_whisper_model: Any = None


def transcribe_audio(path: Path) -> tuple[str, str]:
    global _whisper_model
    from faster_whisper import WhisperModel
    if _whisper_model is None:
        _whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
    segments, info = _whisper_model.transcribe(str(path), beam_size=3, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    code = (info.language or "en").lower()
    language = {"ha": "hausa", "ig": "igbo", "yo": "yoruba", "en": "english"}.get(code, "english")
    return text, "pidgin" if language == "english" and is_pidgin(text) else language


def is_pidgin(text: str) -> bool:
    words = set(re.findall(r"[a-z']+", text.lower()))
    return len(words & {"abeg", "dey", "wetin", "una", "na", "sef", "wahala", "abi", "chop", "sabi"}) >= 2


_TEXT_MARKERS = {
    "hausa": {"ina", "nake", "yaya", "lafiya", "yanzu", "don", "Allah", "ne"},
    "igbo": {"kedu", "anyị", "ndị", "ebe", "bụ", "ga", "ị", "nke"},
    "yoruba": {"ẹ", "ọ", "ṣ", "báwo", "jẹ́", "mo", "ẹ̀gbọ́n", "ní"},
}


def detect_text_language(text: str) -> str:
    folded = text.casefold()
    if is_pidgin(text):
        return "pidgin"
    # Yorùbá tone/dot characters are a useful high-confidence script clue.
    if re.search(r"[ẹọṣẸỌṢ]", text):
        return "yoruba"
    scores = {lang: sum(1 for marker in words if marker.casefold() in folded) for lang, words in _TEXT_MARKERS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "english"


def find_polling_units(query: str) -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        log.warning("INEC data file not found: %s", CSV_PATH)
        return []
    import csv
    words = [w for w in re.findall(r"[\w'-]+", query.casefold()) if len(w) > 2]
    if not words:
        return []
    try:
        with CSV_PATH.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        log.exception("Unable to read INEC CSV")
        return []
    required = {"state", "lga", "ward", "polling_unit_name", "code"}
    if not rows or not required.issubset(rows[0].keys()):
        return []
    scored: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        searchable = f"{row.get('state','')} {row.get('lga','')} {row.get('ward','')}".casefold()
        matches = sum(word in searchable for word in words)
        if matches:
            scored.append((matches, {key: (row.get(key) or "").strip() for key in required}))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    unique: dict[str, dict[str, str]] = {}
    for score, row in scored:
        if score == best:
            unique[row["code"] or "|".join(row[key] for key in ("state", "lga", "ward", "polling_unit_name"))] = row
    return list(unique.values())[:3]


SAFE_TIPS = {
    "english": "Please follow INEC staff instructions and keep your voter information private.",
    "pidgin": "Abeg follow INEC officials' instructions, and keep your voter details private.",
    "hausa": "Ka bi umarnin jami'an INEC, kuma ka kiyaye bayanan katin zaɓenka.",
    "igbo": "Soro ntuziaka ndị ọrụ INEC, ma chekwaa ozi kaadị ntuli aka gị.",
    "yoruba": "Tẹ̀lé ìtọ́nisọ́nà àwọn òṣìṣẹ́ INEC, kí o sì pa àlàyé olùdìbò rẹ mọ́.",
}


def format_grounded_reply(row: dict[str, str], language: str) -> str:
    name, ward = row["polling_unit_name"], row["ward"]
    tip = SAFE_TIPS[language]
    if language == "pidgin":
        return f"Your polling unit na {name}, for {ward} ward. {tip}"
    if language == "hausa":
        return f"Wurin kaɗa ƙuri'arka shi ne {name}, a Ward {ward}. {tip}"
    if language == "igbo":
        return f"Ebe ị ga-eme ntuli aka bụ {name}, na Ward {ward}. {tip}"
    if language == "yoruba":
        return f"Ibi ìdìbò rẹ ni {name}, ní Ward {ward}. {tip}"
    return f"Your polling unit is {name}, in Ward {ward}. {tip}"


async def compose_reply(query: str, language: str, rows: list[dict[str, str]]) -> str:
    lang = language if language in SAFE_TIPS else "english"
    if not rows:
        return "I never see am for my list, but check inec.gov.ng"
    row = rows[0]
    facts = {key: row[key] for key in ("polling_unit_name", "ward", "lga", "state")}
    fallback = format_grounded_reply(facts, lang)
    api_key = os.getenv("LLAMA_API_KEY", "")
    if not api_key:
        return fallback
    api_url = os.getenv("LLAMA_API_URL", "https://api.llama.com/v1/chat/completions")
    prompt = (
        f"Write one friendly, short voter-information reply in {lang}. Use only the verified location facts below; "
        "never add or change a location. Include the exact polling unit name and ward, plus the safety tip verbatim. "
        "Do not give voting advice or claim anything not in the facts.\n"
        f"Verified facts (JSON): {json.dumps(facts, ensure_ascii=False)}\n"
        f"Include this safety tip exactly as written: {SAFE_TIPS[lang]}"
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(api_url, headers={"Authorization": f"Bearer {api_key}"}, json={
                "model": os.getenv("LLAMA_MODEL", "Llama-4-Scout-17B-16E-Instruct"),
                "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 160,
            })
            response.raise_for_status()
            data = response.json()
            candidate: Any = data.get("completion_message", {}).get("content", "")
            if isinstance(candidate, list):
                candidate = " ".join(item.get("text", "") for item in candidate if isinstance(item, dict))
            if not candidate and data.get("choices"):
                candidate = data["choices"][0].get("message", {}).get("content", "")
            if isinstance(candidate, str):
                candidate = candidate.strip()
                # Require the exact authoritative unit and ward to survive generation.
                if (candidate and facts["polling_unit_name"].casefold() in candidate.casefold()
                        and facts["ward"].casefold() in candidate.casefold()
                        and SAFE_TIPS[lang].casefold() in candidate.casefold()):
                    return candidate[:1000]
    except Exception:
        log.exception("Llama API request failed; using grounded local template")
    return fallback


async def graph_post(path: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{GRAPH_BASE}/{path}", headers={"Authorization": f"Bearer {token}"}, json=payload)
        response.raise_for_status()
        return response.json()


async def send_whatsapp_text(phone_number_id: str, recipient: str, text: str, token: str) -> None:
    await graph_post(f"{phone_number_id}/messages", token, {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient,
        "type": "text", "text": {"preview_url": False, "body": text[:4000]},
    })


async def send_whatsapp_audio(phone_number_id: str, recipient: str, media_id: str, token: str) -> None:
    await graph_post(f"{phone_number_id}/messages", token, {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient,
        "type": "audio", "audio": {"id": media_id},
    })


async def upload_audio(phone_number_id: str, audio_path: Path, token: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        with audio_path.open("rb") as handle:
            response = await client.post(
                f"{GRAPH_BASE}/{phone_number_id}/media", headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp", "type": "audio/ogg; codecs=opus"},
                files={"file": ("vota-reply.ogg", handle, "audio/ogg")},
            )
        response.raise_for_status()
        return response.json()["id"]


async def synthesize(text: str, language: str) -> Path | None:
    try:
        voice_map = json.loads(os.getenv("PIPER_VOICE_MAP", "{}"))
    except json.JSONDecodeError:
        log.warning("PIPER_VOICE_MAP must be valid JSON")
        voice_map = {}
    voice_dir = Path(os.getenv("PIPER_VOICE_DIR", "voices"))
    voice = voice_map.get(language) or voice_map.get("english")
    if not voice:
        log.info("No Piper voice configured; text reply will still be sent")
        return None
    model_path = Path(voice)
    if not model_path.is_absolute():
        model_path = voice_dir / model_path
    if not model_path.exists():
        log.warning("Piper model missing: %s", model_path)
        return None
    fd, wav_name = tempfile.mkstemp(suffix=".wav", prefix="vota-out-")
    os.close(fd)
    wav_path = Path(wav_name)
    try:
        piper = await asyncio.create_subprocess_exec(
            "piper", "--model", str(model_path), "--output_file", str(wav_path),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(piper.communicate(text.encode("utf-8")), timeout=60)
        if piper.returncode != 0 or not wav_path.exists():
            log.error("Piper failed: %s", stderr.decode(errors="replace")[-1000:])
            wav_path.unlink(missing_ok=True)
            return None
        ogg_path = wav_path.with_suffix(".ogg")
        ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libopus", "-b:a", "32k", str(ogg_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await ffmpeg.communicate()
        wav_path.unlink(missing_ok=True)
        if ffmpeg.returncode != 0 or not ogg_path.exists():
            log.error("Audio conversion failed: %s", error.decode(errors="replace")[-1000:])
            ogg_path.unlink(missing_ok=True)
            return None
        return ogg_path
    except (FileNotFoundError, asyncio.TimeoutError):
        wav_path.unlink(missing_ok=True)
        log.exception("Piper or ffmpeg unavailable / timed out")
        return None
# --- Added for Render testing /search in 5 languages ---
@app.get("/search")
async def search(q: str):
    lang = detect_text_language(q)
    rows = find_polling_units(q)
    if not rows:
        return {"query": q, "language": lang, "result": "I never see am for my list, but check inec.gov.ng - Try Jalingo, Ikeja, Bodija, Amawbia, Gama", "rows": []}
    # Use first match
    row = rows[0]
    reply = format_grounded_reply(row, lang)
    return {"query": q, "language": lang, "reply": reply, "matches": rows}

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "VOTA is live! Try /health and /search?q=Jalingo"
# --- USSD FOR FEATURE PHONES (NO SMARTPHONE NEEDED) - FREE via Africa's Talking ---
# This endpoint makes VOTA work on *384*... codes for village phones
from fastapi import Form

@app.post("/ussd", response_class=PlainTextResponse)
async def ussd_callback(
    session_id: str = Form(None, alias="sessionId"),
    phone_number: str = Form(None, alias="phoneNumber"),
    text: str = Form("", alias="text")
):
    """
    Africa's Talking USSD format:
    text = user input, e.g. "" (first dial), "1", "1*Jalingo"
    We reply with CON (continue) or END (stop)
    """
    # Normalize
    user_input = (text or "").strip()
    parts = user_input.split("*") if user_input else []
    last = parts[-1].lower() if parts else ""

    # Step 1: Welcome
    if not user_input:
        return "CON VOTA - Your Vote, Your Language\nFind Polling Unit FREE\n1. Enter your area (e.g. Jalingo, Ikeja, Amawbia, Gama, Bodija)\n2. Hausa/Igbo/Yoruba/Pidgin/English\n\nEnter area:"

    # Step 2: User entered area
    query = last if last else user_input
    rows = find_polling_units(query)
    lang = detect_text_language(query)
    
    if not rows:
        return f"END I never see {query} for my list. Try Jalingo, Ikeja, Amawbia, Gama, Bodija. Check inec.gov.ng - VOTA"

    row = rows[0]
    reply = format_grounded_reply(row, lang)
    # USSD has 160 char limit, trim
    short_reply = reply[:150]
    return f"END {short_reply}\nVOTA - Voice Of The Area"

@app.get("/ussd")
async def ussd_info():
    return {
        "service": "VOTA USSD",
        "how_it_works": "Dial *384*12345# (Africa's Talking sandbox) -> Enter area -> Get polling unit",
        "test_web": "Use /search?q=Jalingo for web test",
        "free": "No smartphone needed, works on Nokia small phones, free via Africa's Talking sandbox",
        "languages": ["hausa","igbo","yoruba","pidgin","english"]
    }
    
