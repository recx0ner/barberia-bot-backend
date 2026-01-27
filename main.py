import os
import random
import requests # Necesario para responderle a Telegram
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- VARIABLES ---
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") # <--- NUEVA VARIABLE

# --- CONFIGURACIÓN ---
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- RUTAS ---

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "online", "bot": "Telegram Ready ✈️"})

# Ruta para Telegram (Webhook)
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        # 1. Recibir el mensaje de Telegram
        data = request.json
        
        # Verificamos si es un mensaje de texto real
        if "message" not in data or "text" not in data["message"]:
            return jsonify({"status": "ignored"}), 200

        chat_id = data["message"]["chat"]["id"]
        user_text = data["message"]["text"]

        # 2. Consultar a Gemini (Tu lógica actual)
        if not VALID_GEMINI_KEYS:
            bot_reply = "Error: El sistema está en mantenimiento (Sin API Keys)."
        else:
            selected_key = random.choice(VALID_GEMINI_KEYS)
            genai.configure(api_key=selected_key)
            model = genai.GenerativeModel('models/gemini-2.5-flash')
            response = model.generate_content(user_text)
            bot_reply = response.text

        # 3. Guardar en Supabase (Opcional)
        if supabase:
            try:
                supabase.table('messages').insert({
                    "user_input": user_text,
                    "bot_response": bot_reply,
                    "platform": "telegram" # Para saber que vino de ahí
                }).execute()
            except:
                pass

        # 4. ENVIAR RESPUESTA A TELEGRAM
        send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": bot_reply,
            "parse_mode": "Markdown" # Para que se vea bonito con negritas
        }
        requests.post(send_url, json=payload)

        return jsonify({"status": "sent"}), 200

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)