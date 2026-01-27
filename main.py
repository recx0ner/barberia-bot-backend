import os
import random
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. PERSONALIDAD DEL BARBERO ---
BARBER_PROMPT = """
Eres el asistente virtual de la "Barbería El Corte Arrechísimo".
Tu tono es: Amable, profesional y con estilo.
Tus objetivos son: Agendar citas y dar precios.

LISTA DE PRECIOS:
- Corte Caballero: $10
- Barba: $5
- Combo (Corte + Barba): $12
- Cejas: $3

REGLAS:
1. Si piden cita, pregunta: Nombre y Hora preferida.
2. NO inventes horarios, solo di que verificarás la disponibilidad.
3. Respuestas cortas (máximo 3 líneas) para chat rápido.
4. Si preguntan de otros temas, recuérdales amablemente que eres un barbero.
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

# --- 3. CONEXIÓN BASE DE DATOS ---
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- 4. CEREBRO IA (CON PERSONALIDAD) ---
def get_gemini_response(user_text):
    if not VALID_GEMINI_KEYS:
        return "⚠️ Error: El sistema está en mantenimiento."
    
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        # Usamos el modelo 2.5 Flash CON instrucciones de sistema
        model = genai.GenerativeModel(
            'models/gemini-2.5-flash',
            system_instruction=BARBER_PROMPT
        )
        
        response = model.generate_content(user_text)
        return response.text
    except Exception as e:
        return f"Tuve un problema técnico: {str(e)}"

# --- 5. RUTAS ---

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "online", "bot": "Barbería Telegram Activa 💈"})

# Ruta exclusiva para Telegram
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        # Verificamos que sea un mensaje de texto válido
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            user_text = data["message"]["text"]
            
            # Obtener respuesta del Barbero
            response_text = get_gemini_response(user_text)

            # Guardar historial en Supabase
            if supabase:
                try:
                    supabase.table('messages').insert({
                        "user_input": user_text, 
                        "bot_response": response_text, 
                        "platform": "telegram"
                    }).execute()
                except Exception as db_e:
                    print(f"Error guardando DB: {db_e}")

            # Enviar respuesta a Telegram
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, 
                "text": response_text, 
                "parse_mode": "Markdown"
            })
        
        return jsonify({"status": "sent"}), 200

    except Exception as e:
        print(f"Error Telegram: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)