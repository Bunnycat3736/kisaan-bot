import os
import re
import uuid
import threading
import requests
import asyncio
import edge_tts

from flask import Flask, request, Response, send_file
from dotenv import load_dotenv
from google import genai
from google.genai import types
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# ElevenLabs
from elevenlabs.client import ElevenLabs
from elevenlabs import save

load_dotenv()

# ---------- ENV ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

PUBLIC_BASE_URL = (
    os.getenv("RENDER_EXTERNAL_URL")
    or (
        f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
        if os.getenv("RENDER_EXTERNAL_HOSTNAME")
        else ""
    )
)

if not GEMINI_API_KEY:
    raise ValueError("Missing GEMINI_API_KEY")

if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise ValueError("Missing Twilio credentials")

# ---------- CLIENTS ----------
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else None

app = Flask(__name__)

# ---------- STORAGE ----------
memory = {}
VOICE_DIR = "/tmp/kisaan_voice"
os.makedirs(VOICE_DIR, exist_ok=True)

KISAAN_PROMPT = """
You are Kisaan Bot, a helpful farming assistant for Indian farmers.

Rules:
- Give simple, clear, practical answers.
- Use easy language (Hinglish is fine).
- Prefer low-cost, local solutions.
- Reply in the same language as the user.
"""

# ---------- SAFE ASYNC ----------
def run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

# ---------- TEXT HELPERS ----------
def clean_text(text):
    return (text or "").strip()

def make_speech_text(text):
    text = clean_text(text)
    text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.M)
    return text[:600]

# ---------- LANGUAGE DETECTION ----------
def detect_language(text):
    text = text.lower()

    if "मराठी" in text or "माझ्या" in text:
        return "mr"

    if re.search(r"[\u0900-\u097F]", text):
        return "hi"

    return "en"

# ---------- EDGE VOICE ----------
def get_edge_voice(lang):
    if lang == "mr":
        return "mr-IN-AarohiNeural"
    if lang == "hi":
        return "hi-IN-MadhurNeural"
    return "en-IN-PrabhatNeural"

async def generate_edge_tts(text, voice, filepath):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate="-10%")
    await communicate.save(filepath)

def synthesize_edge(text):
    filename = f"edge_{uuid.uuid4().hex[:10]}.mp3"
    filepath = os.path.join(VOICE_DIR, filename)

    lang = detect_language(text)
    voice = get_edge_voice(lang)

    run_async(generate_edge_tts(text, voice, filepath))

    return filepath

# ---------- ELEVENLABS ----------
def synthesize_elevenlabs(text):
    filename = f"el_{uuid.uuid4().hex[:10]}.mp3"
    filepath = os.path.join(VOICE_DIR, filename)

    audio = eleven_client.generate(
        text=text,
        voice="Rachel",
        model="eleven_multilingual_v2"
    )

    save(audio, filepath)
    return filepath

# ---------- HYBRID SWITCH ----------
def synthesize_voice(text):
    lang = detect_language(text)

    # English → ElevenLabs
    if lang == "en" and eleven_client:
        try:
            print("🔥 ElevenLabs")
            return synthesize_elevenlabs(text)
        except Exception as e:
            print("⚠️ Eleven failed:", e)

    # Fallback → Edge
    try:
        print("✅ Edge TTS")
        return synthesize_edge(text)
    except Exception as e:
        print("❌ Edge failed:", e)
        return None

# ---------- MEDIA ----------
def build_gemini_contents(full_prompt, user_msg, num_media):
    if num_media <= 0:
        return f"{full_prompt}\nUser: {user_msg}"

    try:
        media_url = request.form.get("MediaUrl0")
        content_type = request.form.get("MediaContentType0", "")

        if not media_url:
            return f"{full_prompt}\nUser: {user_msg}"

        r = requests.get(
            media_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=15
        )
        r.raise_for_status()

        media_bytes = r.content

        media_part = types.Part.from_bytes(
            data=media_bytes,
            mime_type=content_type
        )

        return [media_part, f"{full_prompt}\nUser message"]

    except Exception as e:
        print("⚠️ Media error:", e)
        return f"{full_prompt}\nUser: {user_msg}"

# ---------- VOICE SENDER ----------
def send_voice_note_async(to_phone, reply_text):
    try:
        if not PUBLIC_BASE_URL:
            print("❌ Missing PUBLIC URL")
            return

        speech_text = make_speech_text(reply_text)

        mp3_path = synthesize_voice(speech_text)

        if not mp3_path:
            print("❌ Voice generation failed")
            return

        filename = os.path.basename(mp3_path)
        voice_url = f"{PUBLIC_BASE_URL}/voice/{filename}"

        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_phone,
            body="🎤 Voice reply",
            media_url=[voice_url],
        )

        print("✅ Voice sent")

    except Exception as e:
        print(f"❌ Voice error: {e}")

# ---------- ROUTES ----------
@app.route("/")
def home():
    return "✅ Kisaan Bot Hybrid Voice LIVE 🌾"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From")
    user_msg = request.form.get("Body", "")
    num_media = int(request.form.get("NumMedia", 0))

    if phone not in memory:
        memory[phone] = []

    history = "\n".join(memory[phone][-5:])

    try:
        full_prompt = f"{KISAAN_PROMPT}\n{history}"

        contents = build_gemini_contents(full_prompt, user_msg, num_media)

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents
        )

        reply_text = clean_text(getattr(response, "text", ""))

        memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply_text[:100]}")

        threading.Thread(
            target=send_voice_note_async,
            args=(phone, reply_text),
            daemon=True
        ).start()

    except Exception as e:
        reply_text = "Error bhai, try again"
        print(e)

    twiml = MessagingResponse()
    twiml.message(reply_text[:1500])

    return Response(str(twiml), mimetype="application/xml")

@app.route("/voice/<filename>")
def serve_voice(filename):
    path = os.path.join(VOICE_DIR, filename)

    if not os.path.exists(path):
        return "Not found", 404

    return send_file(path, mimetype="audio/mpeg")

# ---------- RUN ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
