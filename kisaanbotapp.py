import os
import requests
import uuid
import threading
import base64
from flask import Flask, request, Response, send_file
from dotenv import load_dotenv
from google import genai
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from sarvamai import SarvamAI

load_dotenv()

# ====================== CLIENTS ======================
gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
sarvam_client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
twilio_client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

app = Flask(__name__)

# Memory: phone_number → last 5 messages
memory = {}

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

@app.route("/", methods=["GET"])
def home():
    return "✅ Kisaan Bot Pro – Image + Voice Input + Sarvam AI Voice Reply is LIVE & STABLE! 🌾🔊"

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
        full_prompt = f"{KISAAN_PROMPT}\n\nPrevious chat:\n{history}\n\n"

        if num_media > 0:
            media_url = request.form.get("MediaUrl0")
            content_type = request.form.get("MediaContentType0", "")
            auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
            r = requests.get(media_url, auth=auth, timeout=20)
            r.raise_for_status()
            media_bytes = r.content

            if "image" in content_type:
                print("🖼️ Image received")
                full_prompt += "User sent a photo of crop/field. Analyse the image and give farming advice.\n"
                contents = [{"role": "user", "parts": [{"text": full_prompt}, {"inline_data": {"mime_type": content_type, "data": media_bytes}}]}]
            elif "audio" in content_type:
                print("🎤 Voice note received")
                full_prompt += "User sent a voice note. Transcribe it and analyse the farming problem.\n"
                contents = [{"role": "user", "parts": [{"text": full_prompt}, {"inline_data": {"mime_type": content_type, "data": media_bytes}}]}]
            else:
                contents = [{"role": "user", "parts": [{"text": full_prompt}]}]
        else:
            contents = [{"role": "user", "parts": [{"text": full_prompt + f"User: {user_msg}"}]}]

        response = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=contents)
        reply_text = (response.text or "").strip()

        if user_msg:
            memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply_text[:150]}...")

        print(f"🤖 AI Reply: {reply_text[:200]}...")

    except Exception as e:
        print(f"❌ Gemini Error: {e}")

    # Fast text reply
    twiml = MessagingResponse()
    twiml.message(reply_text[:1500])

    # Background voice note
    if reply_text and "Sorry bhai" not in reply_text:
        threading.Thread(target=send_voice_note, args=(phone, reply_text), daemon=True).start()

    print("✅ Text reply sent to WhatsApp")
    return Response(str(twiml), mimetype="application/xml")


# ====================== BACKGROUND SARVAM VOICE NOTE ======================
def send_voice_note(phone, text):
    try:
        audio_response = sarvam_client.text_to_speech.convert(
            text=text,
            model="bulbul:v3",
            target_language_code="hi-IN",
            speaker="shubh"
        )

        combined_audio = "".join(audio_response.audios)
        audio_bytes = base64.b64decode(combined_audio)

        filename = f"voice_{uuid.uuid4().hex[:12]}.wav"
        filepath = os.path.join("/tmp", filename)

        with open(filepath, "wb") as f:
            f.write(audio_bytes)

        domain = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        voice_url = f"https://{domain}/voice/{filename}"

        twilio_client.messages.create(
            from_=f"whatsapp:{os.environ['TWILIO_WHATSAPP_NUMBER']}",
            to=phone,
            media_url=[voice_url]
        )

        print(f"🔊 Sarvam Voice Note sent to {phone}")

    except Exception as e:
        print(f"❌ Voice Note Error: {e}")


# Serve voice files
@app.route("/voice/<filename>")
def serve_voice(filename):
    try:
        return send_file(os.path.join("/tmp", filename), mimetype="audio/wav")
    except:
        return "File not found", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
