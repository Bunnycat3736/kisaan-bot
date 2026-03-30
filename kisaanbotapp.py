import os
import requests
from flask import Flask, request, Response
from dotenv import load_dotenv
from google import genai
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

# ====================== GEMINI SETUP ======================
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

app = Flask(__name__)

# Simple memory: phone_number → list of last 5 messages
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
    return "✅ Kisaan Bot Pro – Image + Voice + Memory is LIVE! 🌾"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "unknown")
    user_msg = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", 0))

    print(f"📥 From: {phone} | Text: {user_msg} | Media: {num_media}")

    # Load memory for this user
    if phone not in memory:
        memory[phone] = []
    history = "\n".join(memory[phone][-5:])   # last 5 messages

    reply = "Sorry bhai, thoda issue ho gaya. Fir se batao."

    try:
        # Base prompt with history
        full_prompt = f"{KISAAN_PROMPT}\n\nPrevious chat:\n{history}\n\n"

        if num_media > 0:
            # ---------- MEDIA RECEIVED ----------
            media_url = request.form.get("MediaUrl0")
            content_type = request.form.get("MediaContentType0", "")

            # Download from Twilio
            auth = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
            r = requests.get(media_url, auth=auth)
            media_bytes = r.content

            if "image" in content_type:
                print("🖼️ Image received")
                full_prompt += "User sent a photo of crop/field. Analyse the image and give farming advice.\n"
                contents = [
                    {
                        "role": "user",
                        "parts": [
                            {"text": full_prompt},
                            {"inline_data": {"mime_type": content_type, "data": media_bytes}}
                        ]
                    }
                ]

            elif "audio" in content_type:
                print("🎤 Voice note received")
                full_prompt += "User sent a voice note. First transcribe it, then analyse the farming problem.\n"
                contents = [
                    {
                        "role": "user",
                        "parts": [
                            {"text": full_prompt},
                            {"inline_data": {"mime_type": content_type, "data": media_bytes}}
                        ]
                    }
                ]

            else:
                contents = [{"role": "user", "parts": [{"text": full_prompt + "\nUser sent unknown media."}]}]

        else:
            # ---------- NORMAL TEXT ----------
            contents = [{"role": "user", "parts": [{"text": full_prompt + f"User: {user_msg}"}]}]

        # Call Gemini
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents
        )
        reply = response.text.strip()

        # Save to memory
        if user_msg:
            memory[phone].append(f"User: {user_msg}")
        memory[phone].append(f"Bot: {reply[:150]}...")

        print(f"🤖 AI Reply sent (first 200 chars): {reply[:200]}...")

    except Exception as e:
        print(f"❌ Gemini Error: {e}")
        reply = "Sorry bhai, thoda issue ho gaya. Fir se photo/voice/text bhejo."

    # Twilio response
    twiml = MessagingResponse()
    twiml.message(reply[:1500])
    print("✅ TwiML sent back to Twilio")

    return Response(str(twiml), mimetype="application/xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)