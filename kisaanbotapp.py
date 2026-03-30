import os
import requests
import uuid
from flask import Flask, request, Response, send_file
from dotenv import load_dotenv
from google import genai
from twilio.twiml.messaging_response import MessagingResponse
from gtts import gTTS

load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

app = Flask(__name__)

# Memory: phone → last 5 messages
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
"""

@app.route("/", methods=["GET"])
def home():
    return "✅ Kisaan Bot Pro – Image + Voice Input + Voice Reply is LIVE! 🌾"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "unknown")
    user_msg = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", 0))

    print(f"📥 From: {phone} | Text: {user_msg} | Media: {num_media}")

    # Memory
    if phone not in memory:
        memory[phone] = []
    history = "\n".join(memory[phone][-5:])

    reply_text = "Sorry bhai, thoda issue ho gaya. Fir se photo/voice/text bhejo."

    try:
        full_prompt = f"{KISAAN_PROMPT}\n\nPrevious chat:\n{history}\n\n"

        if num_media > 0:
            media_url = request.form.get("MediaUrl0")
            content_type = request.form.get("MediaContentType0", "")
            auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
            r = requests.get(media_url, auth=auth)
            media_bytes = r.content

            if "image" in content_type:
                print("🖼️ Image received")
                full_prompt += "User sent a photo of crop/field. Analyse the image and give farming advice.\n"
                contents = [{"role": "user", "parts": [{"text": full_prompt}, {"inline_data": {"mime_type": content_type, "data": media_bytes}}]}]

            elif "audio" in content_type:
                print("🎤 Voice note received")
                full_prompt += "User sent a voice note. First transcribe it, then analyse the farming problem.\n"
                contents = [{"role": "user", "parts": [{"text": full_prompt}, {"inline_data": {"mime_type": content_type, "data": media_bytes}}]}]

            else:
                contents = [{"role": "user", "parts": [{"text": full_prompt}]}]
        else:
            contents = [{"role": "user", "parts": [{"text": full_prompt + f"User: {user_msg}"}]}]

        # Gemini call
        response = client.models.generate_content(model="gemini-2.5-flash", contents=contents)
        reply_text = response.text.strip()

        # Save memory
        if user_msg:
            memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply_text[:150]}...")

        print(f"🤖 AI Reply: {reply_text[:200]}...")

    except Exception as e:
        print(f"❌ Gemini Error: {e}")

    # ================== CREATE VOICE NOTE (FREE gTTS) ==================
    voice_url = None
    try:
        tts = gTTS(text=reply_text, lang='hi', slow=False)
        filename = f"voice_{uuid.uuid4().hex[:10]}.mp3"
        filepath = os.path.join("/tmp", filename)
        tts.save(filepath)

        # Render public URL
        domain = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        voice_url = f"https://{domain}/voice/{filename}"
    except Exception as e:
        print(f"❌ TTS Error: {e}")

    # ================== TWILIO RESPONSE ==================
    twiml = MessagingResponse()
    msg = twiml.message(reply_text[:1500])
    if voice_url:
        msg.media(voice_url)

    print("✅ Text + Voice Note sent to WhatsApp")
    return Response(str(twiml), mimetype="application/xml")


# Serve voice files
@app.route("/voice/<filename>")
def serve_voice(filename):
    try:
        return send_file(os.path.join("/tmp", filename), mimetype="audio/mpeg")
    except:
        return "File not found", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)