import os
import re
import uuid
import threading
import requests
import base64

from flask import Flask, request, Response, send_file
from dotenv import load_dotenv
from google import genai
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from sarvamai import SarvamAI

load_dotenv()

# ====================== ENVIRONMENT VARIABLES ======================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

PUBLIC_BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or (
    f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
    if os.getenv("RENDER_EXTERNAL_HOSTNAME") else ""
)

# ====================== CLIENTS ======================
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
sarvam_client = SarvamAI(api_subscription_key=os.getenv("SARVAM_API_KEY"))
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = Flask(__name__)

# Memory
memory = {}
VOICE_DIR = "/tmp/kisaan_voice"
os.makedirs(VOICE_DIR, exist_ok=True)

KISAAN_PROMPT = """
You are Kisaan Bot, a helpful farming assistant for Indian farmers.
Rules:
- Give simple, clear, practical answers
- Use easy language (Hinglish is fine)
- Prefer low-cost, local solutions
- Reply in same language as user
- Always use this exact format:

1. Problem
2. Cause
3. Solution
4. Extra Tip

If user sends photo or voice note, analyse it properly and give farming advice.
Keep replies short and clear for WhatsApp.
"""

# ====================== HELPERS ======================
def clean_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def make_speech_text(reply_text: str) -> str:
    text = clean_text(reply_text)
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.M)
    return text[:700]   # Sarvam handles decent length

# ====================== BACKGROUND VOICE NOTE ======================
def send_voice_note_async(to_phone: str, reply_text: str):
    try:
        if not PUBLIC_BASE_URL:
            print("❌ Missing RENDER_EXTERNAL_HOSTNAME")
            return

        speech_text = make_speech_text(reply_text)

        # Sarvam AI Voice Generation
        audio_response = sarvam_client.text_to_speech.convert(
            text=speech_text,
            model="bulbul:v3",
            target_language_code="hi-IN",
            speaker="shubh"          # Change to "ritu", "priya", "aditya" if you want
        )

        combined_audio = "".join(audio_response.audios)
        audio_bytes = base64.b64decode(combined_audio)

        filename = f"voice_{uuid.uuid4().hex[:12]}.wav"
        filepath = os.path.join(VOICE_DIR, filename)

        with open(filepath, "wb") as f:
            f.write(audio_bytes)

        voice_url = f"{PUBLIC_BASE_URL}/voice/{filename}"

        # Send voice note as separate WhatsApp media message
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_phone,
            body="🎤 Kisaan Bot voice note",
            media_url=[voice_url]
        )

        print(f"✅ Sarvam Voice Note sent to {to_phone}")

    except Exception as e:
        print(f"❌ Voice Note Error: {e}")


# ====================== ROUTES ======================
@app.route("/", methods=["GET"])
def home():
    return "✅ Kisaan Bot Pro – Image + Voice Input + Sarvam AI Voice Reply is LIVE! 🌾🔊"

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

        if num_media > 0:
            media_url = request.form.get("MediaUrl0")
            content_type = request.form.get("MediaContentType0", "")
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            r = requests.get(media_url, auth=auth, timeout=20)
            r.raise_for_status()
            media_bytes = r.content

            if "image" in content_type:
                full_prompt += "\nUser sent a crop photo. Analyse the image and give farming advice."
                contents = [{"role": "user", "parts": [{"text": full_prompt}, {"inline_data": {"mime_type": content_type, "data": media_bytes}}]}]
            elif "audio" in content_type:
                full_prompt += "\nUser sent a voice note. Understand it and answer the farming problem."
                contents = [{"role": "user", "parts": [{"text": full_prompt}, {"inline_data": {"mime_type": content_type, "data": media_bytes}}]}]
            else:
                contents = [{"role": "user", "parts": [{"text": full_prompt}]}]
        else:
            contents = [{"role": "user", "parts": [{"text": full_prompt + f"\nUser: {user_msg}"}]}]

        response = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=contents)
        reply_text = clean_text(getattr(response, "text", "") or "")

        if user_msg:
            memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply_text[:150]}...")

        print(f"🤖 AI Reply: {reply_text[:200]}...")

        # Start voice note in background
        threading.Thread(target=send_voice_note_async, args=(phone, reply_text), daemon=True).start()

    except Exception as e:
        print(f"❌ Gemini Error: {e}")

    # Fast text reply
    twiml = MessagingResponse()
    twiml.message(reply_text[:1500])

    print("✅ Text reply sent to WhatsApp")
    return Response(str(twiml), mimetype="application/xml")


@app.route("/voice/<filename>")
def serve_voice(filename):
    path = os.path.join(VOICE_DIR, filename)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, mimetype="audio/wav")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
