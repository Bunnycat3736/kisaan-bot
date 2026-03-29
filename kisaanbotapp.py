import os
from flask import Flask, request, Response
from dotenv import load_dotenv
import google.generativeai as genai
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-pro")   # yahi best chal raha hai abhi

app = Flask(__name__)

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
"""

@app.route("/", methods=["GET"])
def home():
    return "✅ Kisaan Bot is running 24/7 on Render!"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_msg = request.form.get("Body", "").strip()
    print(f"📥 Incoming: {user_msg}")

    if not user_msg:
        reply = "Bhai, kya problem hai? Farming related batao 🌾"
    else:
        try:
            full_prompt = f"{KISAAN_PROMPT}\n\nUser: {user_msg}"
            response = model.generate_content(full_prompt)
            reply = response.text.strip()
        except Exception as e:
            print(f"❌ Error: {e}")
            reply = "Sorry bhai, thoda issue ho gaya. Fir se batao."

    twiml = MessagingResponse()
    twiml.message(reply[:1500])
    print("✅ Reply sent to WhatsApp")
    return Response(str(twiml), mimetype="application/xml")


# ================== RENDER KE LIYE YE IMPORTANT HAI ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)