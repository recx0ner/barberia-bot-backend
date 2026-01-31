import os
import random
import json
import requests
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
# IMPORTANTE: Usamos la nueva librería recomendada en tus logs
from google import genai 
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "BarberiaBot")

# --- 2. LÓGICA DE IA (NUEVO SDK 2026) ---

def get_gemini_response(user_text, numero):
    try:
        # Inicializamos el cliente con una llave aleatoria
        client = genai.Client(api_key=random.choice(VALID_GEMINI_KEYS))
        
        system_prompt = f"Eres el asistente de una barbería. Hoy es {datetime.now().strftime('%Y-%m-%d')}. Responde breve."
        
        # Nueva sintaxis de Google GenAI
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            config={'system_instruction': system_prompt},
            contents=user_text
        )
        
        return response.text.strip()
    except Exception as e:
        print(f"❌ Error en Gemini: {e}")
        return "Lo siento, tuve un pequeño problema técnico. ¿Me repites eso?"

# --- 3. WEBHOOK Y ENVÍO ---

def enviar_whatsapp(numero, texto):
    try:
        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        headers = {"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"}
        payload = {"number": numero, "textMessage": {"text": texto}}
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"🔥 Error enviando WA: {e}")

@app.route('/whatsapp', methods=['POST'])
def webhook():
    data = request.json
    # Evolution API envía eventos en minúsculas usualmente
    event = data.get("event", "").lower()
    
    if event != "messages.upsert":
        return jsonify({"status": "ignored"}), 200

    msg_data = data.get('data', {})
    if msg_data.get('key', {}).get('fromMe'):
        return jsonify({"status": "ok"}), 200

    numero = msg_data['key']['remoteJid'].split('@')[0]
    message_content = msg_data.get('message', {})
    
    # Extracción robusta de texto
    texto = (message_content.get('conversation') or 
             message_content.get('extendedTextMessage', {}).get('text') or "")

    if texto:
        respuesta = get_gemini_response(texto, numero)
        enviar_whatsapp(numero, respuesta)
        
        # Registro en Supabase
        try:
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto, 
                "bot_response": respuesta
            }).execute()
        except Exception as e:
            print(f"🔥 Error Supabase: {e}")

    return jsonify({"status": "success"}), 200

# --- 4. ESTO ES LO QUE LE FALTA A TU CÓDIGO PARA RENDER ---
if __name__ == "__main__":
    # Render asigna un puerto dinámicamente en la variable PORT
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)