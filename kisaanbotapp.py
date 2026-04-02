import os
import re
import uuid
import threading
import tempfile
import requests

from flask import Flask, request, Response, send_file
from dotenv import load_dotenv
from google import genai
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from sarvamai import SarvamAI
from sarvamai.play import save

load_dotenv()

# -------------------- ENV --------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3")
SARVAM_STT_MODE = os.getenv("SARVAM_STT_MODE", "codemix")
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")

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

if not SARVAM_API_KEY:
    raise ValueError("Missing SARVAM_API_KEY in .env")

if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise ValueError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env")

# -------------------- CLIENTS --------------------
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
sarvam_client = SarvamAI(api_subscription_key=SARVAM_API_KEY)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = Flask(__name__)

# -------------------- STORAGE --------------------
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
- If the user writes in Hindi or Marathi, reply in native script, not Romanized letters.
- Keep answers short and WhatsApp-friendly.
- If a crop photo is sent, analyze it carefully and give practical farm advice.
- If the user sends a voice note, understand it properly and answer the farming question.

Use this format when it fits:
1. Problem
2. Cause
3. Solution
4. Extra Tip
"""

# -------------------- HELPERS --------------------
def clean_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_for_whatsapp(text: str, limit: int = 1500) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def make_speech_text(reply_text: str) -> str:
    # Keep voice notes shorter and easier to hear.
    text = clean_text(reply_text)
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text)
    return text[:600]


def infer_tts_language_code(user_msg: str, reply_text: str, language_hint: str | None = None) -> str:
    """
    Sarvam TTS language codes used here:
    - hi-IN
    - mr-IN
    - en-IN
    """
    if language_hint in {"hi-IN", "mr-IN", "en-IN"}:
        return language_hint

    combined = f"{user_msg} {reply_text}".lower()

    marathi_markers = [
        "माझ्या", "शेतात", "उपाय", "सांगा", "पिकांना", "काय", "कसं",
        "फवारणी", "बियाणे", "खत", "आहे", "कृपया", "मला"
    ]

    if "मराठी" in combined or any(word in combined for word in marathi_markers):
        return "mr-IN"

    if re.search(r"[\u0900-\u097F]", combined):
        return "hi-IN"

    return "en-IN"


def download_twilio_media(media_url: str) -> tuple[bytes, str]:
    """
    Returns (bytes, content_type).
    """
    r = requests.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=20
    )
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "")


def transcribe_voice_note(media_bytes: bytes, content_type: str, filename_suffix: str) -> tuple[str, str | None]:
    """
    Sarvam Saaras v3 voice-note transcription.
    Returns (transcript, detected_language_code).
    """
    temp_in = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=filename_suffix, dir=VOICE_DIR) as tmp:
            tmp.write(media_bytes)
            temp_in = tmp.name

        with open(temp_in, "rb") as f:
            response = sarvam_client.speech_to_text.transcribe(
                file=f,
                model=SARVAM_STT_MODEL,
                mode=SARVAM_STT_MODE,
            )

        transcript = clean_text(getattr(response, "transcript", "") or "")
        language_code = getattr(response, "language_code", None)
        return transcript, language_code

    finally:
        if temp_in and os.path.exists(temp_in):
            try:
                os.remove(temp_in)
            except OSError:
                pass


def synthesize_voice_wav(reply_text: str, user_msg: str, language_hint: str | None = None) -> str | None:
    """
    Sarvam Bulbul v3 TTS -> returns local WAV filepath.
    """
    speech_text = make_speech_text(reply_text)
    if not speech_text:
        return None

    language_code = infer_tts_language_code(user_msg, reply_text, language_hint)

    temp_out = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav", dir=VOICE_DIR) as tmp:
            temp_out = tmp.name

        audio = sarvam_client.text_to_speech.convert(
            text=speech_text,
            model=SARVAM_TTS_MODEL,
            target_language_code=language_code,
        )

        save(audio, temp_out)
        return temp_out

    except Exception:
        if temp_out and os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except OSError:
                pass
        return None


def gemini_reply(full_prompt: str, user_msg: str, image_bytes: bytes | None = None, image_mime: str | None = None) -> str:
    """
    Gemini handles chat + image understanding.
    """
    if image_bytes and image_mime and image_mime.startswith("image/"):
        user_prompt = (
            f"{full_prompt}\n\n"
            f"User question/caption: {user_msg or 'Analyze this crop photo and give practical farming advice.'}"
        )
        contents = [
            {
                "role": "user",
                "parts": [
                    {"text": user_prompt},
                    {
                        "inline_data": {
                            "mime_type": image_mime,
                            "data": image_bytes,
                        }
                    },
                ],
            }
        ]
    else:
        contents = f"{full_prompt}\n\nUser: {user_msg}"

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
    )
    return clean_text(getattr(response, "text", "") or "")


def send_voice_note_async(to_phone: str, reply_text: str, user_msg: str, language_hint: str | None = None):
    """
    Sends a separate WhatsApp voice message after the text reply is already returned.
    """
    try:
        if not PUBLIC_BASE_URL:
            print("❌ Missing RENDER_EXTERNAL_URL / RENDER_EXTERNAL_HOSTNAME")
            return

        wav_path = synthesize_voice_wav(reply_text, user_msg, language_hint)
        if not wav_path:
            print("⚠️ Voice generation failed; skipping voice note.")
            return

        filename = os.path.basename(wav_path)
        voice_url = f"{PUBLIC_BASE_URL}/voice/{filename}"

        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_phone,
            body="🎤 Voice reply",
            media_url=[voice_url],
        )

        print(f"✅ Voice note sent to {to_phone}")

    except Exception as e:
        print(f"❌ Voice async error: {e}")


def is_audio_content(content_type: str) -> bool:
    return (content_type or "").startswith("audio/")


def is_image_content(content_type: str) -> bool:
    return (content_type or "").startswith("image/")


# -------------------- ROUTES --------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Kisaan Bot Hybrid LIVE 🌾"


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "unknown")
    user_msg = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", 0))
    media_url = request.form.get("MediaUrl0", "")
    content_type = request.form.get("MediaContentType0", "")

    print(f"📥 From: {phone} | Text: {user_msg} | Media: {num_media} | Type: {content_type}")

    if phone not in memory:
        memory[phone] = []

    history = "\n".join(memory[phone][-5:])
    reply_text = "Sorry bhai, thoda issue ho gaya. Fir se text, voice note, ya photo bhejo."
    voice_hint = None

    try:
        # 1) Voice note input -> Sarvam STT -> Gemini brain
        if num_media > 0 and is_audio_content(content_type):
            if not media_url:
                raise ValueError("Audio media URL missing")

            media_bytes, downloaded_type = download_twilio_media(media_url)

            suffix = ".ogg"
            if "mp3" in content_type or "mpeg" in content_type:
                suffix = ".mp3"
            elif "wav" in content_type:
                suffix = ".wav"
            elif "m4a" in content_type or "mp4" in content_type:
                suffix = ".m4a"

            transcript, stt_language = transcribe_voice_note(media_bytes, downloaded_type or content_type, suffix)
            if not transcript:
                raise ValueError("Could not transcribe the voice note")

            user_msg = transcript
            voice_hint = stt_language
            print(f"🎤 Transcript: {user_msg} | Lang: {voice_hint}")

        # 2) Image input -> Gemini multimodal
        if num_media > 0 and is_image_content(content_type):
            if not media_url:
                raise ValueError("Image media URL missing")

            image_bytes, image_type = download_twilio_media(media_url)

            full_prompt = f"{KISAAN_PROMPT}\n\nPrevious chat:\n{history}\n"
            reply_text = gemini_reply(
                full_prompt=full_prompt,
                user_msg=user_msg,
                image_bytes=image_bytes,
                image_mime=image_type or content_type,
            )

        # 3) Normal text / transcription text -> Gemini text
        else:
            full_prompt = f"{KISAAN_PROMPT}\n\nPrevious chat:\n{history}\n"
            if not user_msg:
                user_msg = "Hello"

            reply_text = gemini_reply(
                full_prompt=full_prompt,
                user_msg=user_msg,
            )

        reply_text = reply_text or "Sorry bhai, abhi response nahi bana. Fir se try karo."

        # Save memory
        if user_msg:
            memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply_text[:150]}...")

        print(f"🤖 AI Reply: {reply_text[:200]}...")

        # Voice reply comes AFTER text reply
        threading.Thread(
            target=send_voice_note_async,
            args=(phone, reply_text, user_msg, voice_hint),
            daemon=True,
        ).start()

    except Exception as e:
        print(f"❌ Bot Error: {e}")
        reply_text = "Error bhai, try again"

    twiml = MessagingResponse()
    twiml.message(truncate_for_whatsapp(reply_text))

    print("✅ Text reply sent to WhatsApp")
    return Response(str(twiml), mimetype="application/xml")


@app.route("/voice/<filename>", methods=["GET"])
def serve_voice(filename):
    path = os.path.join(VOICE_DIR, filename)
    if not os.path.exists(path):
        return "File not found", 404

    return send_file(path, mimetype="audio/wav", as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
