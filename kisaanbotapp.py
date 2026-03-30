import os
import re
import uuid
import threading
import requests

from flask import Flask, request, Response, send_file
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.cloud import texttospeech
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

load_dotenv()

# ---------- ENV ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

PUBLIC_BASE_URL = (
    os.getenv("RENDER_EXTERNAL_URL")
    or (
        f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
        if os.getenv("RENDER_EXTERNAL_HOSTNAME")
        else ""
    )
)

if not GEMINI_API_KEY:
    raise ValueError("Missing GEMINI_API_KEY in .env")

if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise ValueError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env")

# Google Cloud TTS uses Application Default Credentials.
# On your laptop, run:
#   gcloud auth application-default login
# On Render, use a service account / ADC-compatible setup.
tts_client = texttospeech.TextToSpeechClient()

# ---------- CLIENTS ----------
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

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
- Always use this exact format:

1. Problem
2. Cause
3. Solution
4. Extra Tip

If user sends photo or voice note, analyze it properly and give farming advice.
Keep replies short enough for WhatsApp.
"""


# ---------- HELPERS ----------
def clean_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def make_speech_text(reply_text: str) -> str:
    # Make the spoken audio shorter and easier to hear.
    text = clean_text(reply_text)
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.M)
    return text[:700]


def infer_tts_language(user_msg: str, reply_text: str) -> str:
    """
    Simple practical voice selector:
    - Marathi request -> mr-IN
    - Hindi request / Devanagari with no Marathi hint -> hi-IN
    - English request -> en-US
    """
    combined = f"{user_msg} {reply_text}".strip().lower()

    if "marathi" in combined or "मराठी" in combined:
        return "mr-IN"

    if "english" in combined or "inglish" in combined:
        return "en-US"

    if re.search(r"[\u0900-\u097F]", combined):
        # If Devanagari is present, use Marathi voice if the message looks Marathi-ish.
        marathi_markers = [
            "माझ्या", "शेतात", "उपाय", "सांगा", "पिकांना", "काय", "कसं",
            "फवारणी", "बियाणे", "खत", "आहे", "कृपया", "मला"
        ]
        if any(word in combined for word in marathi_markers):
            return "mr-IN"
        return "hi-IN"

    return "en-US"


def tts_voice_name_for(language_code: str) -> texttospeech.VoiceSelectionParams:
    # Let Google choose the exact voice for that language.
    return texttospeech.VoiceSelectionParams(
        language_code=language_code,
        ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
    )


def synthesize_voice_mp3(text: str, language_code: str) -> str:
    """
    Returns local mp3 filepath.
    """
    filename = f"voice_{uuid.uuid4().hex[:12]}.mp3"
    filepath = os.path.join(VOICE_DIR, filename)

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = tts_voice_name_for(language_code)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )

    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )

    with open(filepath, "wb") as out:
        out.write(response.audio_content)

    return filepath


def build_gemini_contents(full_prompt: str, user_msg: str, num_media: int):
    """
    Returns:
      - text string for normal messages
      - list of parts for image/audio messages
    """
    if num_media <= 0:
        return f"{full_prompt}\nUser: {user_msg}"

    media_url = request.form.get("MediaUrl0")
    content_type = request.form.get("MediaContentType0", "").strip()

    if not media_url or not content_type:
        return f"{full_prompt}\nUser: {user_msg}"

    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    r = requests.get(media_url, auth=auth, timeout=20)
    r.raise_for_status()
    media_bytes = r.content

    media_part = types.Part.from_bytes(
        data=media_bytes,
        mime_type=content_type
    )

    if "image" in content_type:
        prompt = (
            f"{full_prompt}\n"
            "User sent a crop photo. Analyze the image and give farming advice."
        )
    elif "audio" in content_type:
        prompt = (
            f"{full_prompt}\n"
            "User sent a voice note. Understand it and answer the farming problem."
        )
    else:
        prompt = f"{full_prompt}\nUser: {user_msg}"

    return [media_part, prompt]


def send_voice_note_async(to_phone: str, user_msg: str, reply_text: str):
    """
    Sends a separate WhatsApp voice note after the text reply is already returned.
    """
    try:
        if not PUBLIC_BASE_URL:
            print("❌ Missing RENDER_EXTERNAL_URL / RENDER_EXTERNAL_HOSTNAME")
            return

        speech_text = make_speech_text(reply_text)
        lang = infer_tts_language(user_msg, reply_text)
        mp3_path = synthesize_voice_mp3(speech_text, lang)

        filename = os.path.basename(mp3_path)
        voice_url = f"{PUBLIC_BASE_URL}/voice/{filename}"

        # Twilio WhatsApp media message
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_phone,
            body="🎤 Kisaan Bot voice note",
            media_url=[voice_url],
        )

        print(f"✅ Voice note sent to {to_phone} using {lang}")

    except Exception as e:
        print(f"❌ Voice async error: {e}")


# ---------- ROUTES ----------
@app.route("/", methods=["GET"])
def home():
    return "✅ Kisaan Bot Pro – Text + Image + Voice Input + Voice Reply is LIVE! 🌾"


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "unknown")
    user_msg = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", 0))

    print(f"📥 From: {phone} | Text: {user_msg} | Media: {num_media}")

    if phone not in memory:
        memory[phone] = []

    history = "\n".join(memory[phone][-5:])
    reply_text = "Sorry bhai, thoda issue ho gaya. Fir se text/voice/photo bhejo."

    try:
        full_prompt = f"{KISAAN_PROMPT}\n\nPrevious chat:\n{history}\n"

        contents = build_gemini_contents(full_prompt, user_msg, num_media)

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents
        )

        reply_text = clean_text(getattr(response, "text", "") or "")
        if not reply_text:
            reply_text = "Sorry bhai, abhi response nahi bana. Fir se try karo."

        if user_msg:
            memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply_text[:150]}...")

        print(f"🤖 AI Reply: {reply_text[:200]}...")

        # Send voice note in the background, after text reply is already returned.
        threading.Thread(
            target=send_voice_note_async,
            args=(phone, user_msg, reply_text),
            daemon=True
        ).start()

    except Exception as e:
        print(f"❌ Gemini Error: {e}")

    twiml = MessagingResponse()
    twiml.message(reply_text[:1500])

    print("✅ Text reply sent to WhatsApp")
    return Response(str(twiml), mimetype="application/xml")


@app.route("/voice/<filename>", methods=["GET"])
def serve_voice(filename):
    path = os.path.join(VOICE_DIR, filename)
    if not os.path.exists(path):
        return "File not found", 404

    return send_file(path, mimetype="audio/mpeg", as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
