import os
from flask import Flask, request, Response
from dotenv import load_dotenv
from google import genai                     # Naya SDK
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

# ====================== GEMINI SETUP ======================
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

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
    return "✅ Kisaan Bot is running 24/7 on Render! (Fixed New SDK)"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_msg = request.form.get("Body", "").strip()
    print(f"📥 Incoming message: {user_msg}")

    if not user_msg:
        reply = "Bhai, kya problem hai? Farming related batao 🌾"
    else:
        try:
            full_prompt = f"{KISAAN_PROMPT}\n\nUser: {user_msg}"
            
            # FIXED: Correct model + safe way
            response = client.models.generate_content(
                model="gemini-2.0-flash",          # ← Yeh ab sabse reliable hai
                contents=full_prompt
            )
            reply = response.text.strip()
            
            print(f"🤖 AI Reply (first 200 chars): {reply[:200]}...")
            
        except Exception as e:
            print(f"❌ Gemini Error: {e}")      # ← Yeh Render logs mein dikhega
            reply = "Sorry bhai, thoda issue ho gaya. Fir se batao."

    twiml = MessagingResponse()
    twiml.message(reply[:1500])
    print("✅ TwiML sent back to Twilio")
    
    return Response(str(twiml), mimetype="application/xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)