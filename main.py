import os
import random
import requests
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN E IDENTIDAD DEL BOT ---
# Aquí es donde defines la personalidad de tu Barbería
BARBER_PROMPT = """
Eres el asistente virtual de la "Barbería El Corte Arrechísimo" (o el nombre que prefieras).
Tu tono es: Amable, profesional y directo.
Tus objetivos son: Agendar citas, dar información de precios y resolver dudas.

LISTA DE PRECIOS Y SERVICIOS:
- Corte de Cabello: $10
- Arreglo de Barba: $5
- Combo Corte + Barba: $12
- Cejas: $3
- Mascarilla Negra: $4

REGLAS DE RESPUESTA:
1. Si te piden una cita, pregunta SIEMPRE: Nombre, Día y Hora preferida.
2. NO inventes horarios disponibles, solo di que verificarás la agenda.
3. Responde de forma breve (máximo 3 frases).
4. Si te preguntan algo fuera del tema barbería (ej: medicina, autos), di amablemente que solo sabes de cortes.
"""

# --- 2. VARIABLES DE ENTORNO ---
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# --- 3. CONFIGURACIÓN SUPABASE ---
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- 4. FUNCIÓN CEREBRO (GEMINI) ---
def get_gemini_response(user_text):
    if not VALID_GEMINI_KEYS:
        return "⚠️ Error: Sistema en mantenimiento."
    
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        # AQUÍ OCURRE LA MAGIA: Le pasamos la instrucción de sistema
        model = genai.GenerativeModel(
            'models/gemini-2.5-flash',
            system_instruction=BARBER_PROMPT 
        )
        
        response = model.generate_content(user_text)
        return response.text
    except Exception as e:
        return f"Error procesando mensaje: {str(e)}"

# --- 5. RUTAS ---
@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "online", "bot": "Barbería Activa 💈"})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            user_text = data["message"]["text"]
            
            # Obtenemos respuesta con personalidad
            response_text = get_gemini_response(user_text)

            # Guardar en BD
            if supabase:
                supabase.table('messages').insert({
                    "user_input": user_text, "bot_response": response_text, "platform": "telegram"
                }).execute()

            # Responder a Telegram
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": response_text, "parse_mode": "Markdown"
            })
        return jsonify({"status": "sent"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    incoming_msg = request.values.get('Body', '').lower()
    bot_reply = get_gemini_response(incoming_msg)

    # Guardar en BD
    if supabase:
        try:
            user_phone = request.values.get('From', '')
            supabase.table('messages').insert({
                "user_input": incoming_msg, "bot_response": bot_reply, "platform": "whatsapp", "user_id": user_phone 
            }).execute()
        except:
            pass

    resp = MessagingResponse()
    msg = resp.message()
    msg.body(bot_reply)
    return str(resp)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)