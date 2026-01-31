import os
import random
import json
import requests
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN ---
PORT = int(os.environ.get("PORT", 10000))
# Leemos el contexto que configuraste en la imagen (pizzas, precios, tono)
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente virtual servicial.")

GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_GEMINI_KEYS = [k for k in GEMINI_KEYS if k]

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "ComercioBot")

# --- 2. CEREBRO CON CONTEXTO DINÁMICO ---

def get_gemini_response(user_text):
    try:
        client = genai.Client(api_key=random.choice(VALID_GEMINI_KEYS))
        
        # Mezclamos tus metas generales con el contexto específico de Render
        instruction = f"""
        {BUSINESS_CONTEXT}

        REGLAS ADICIONALES:
        - Fecha/Hora actual: {datetime.now().strftime('%Y-%m-%d %H:%M')}.
        - Si el usuario quiere hacer un pedido o cita, extrae los datos.
        - Si detectas una intención clara de compra/reserva, responde con este JSON:
          {{"action": "procesar", "item": "...", "cantidad": "...", "detalles": "..."}}
        - Si no es una transacción, mantén el tono divertido y servicial que definiste.
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            config={'system_instruction': instruction},
            contents=user_text
        )
        return response.text.strip()
    except Exception as e:
        print(f"❌ Error Gemini: {e}")
        return "¡Ups! Mi horno mental se sobrecalentó. ¿Me repites eso? 🍕"

# --- 3. PROCESAMIENTO DE WHATSAPP ---

@app.route('/whatsapp', methods=['POST'])
def webhook():
    try:
        data = request.json
        if data.get("event") != "messages.upsert":
            return jsonify({"status": "ignored"}), 200

        msg_data = data.get('data', {})
        if msg_data.get('key', {}).get('fromMe'):
            return jsonify({"status": "ok"}), 200

        numero = msg_data['key']['remoteJid'].split('@')[0]
        msg_body = msg_data.get('message', {})
        texto_usuario = (msg_body.get('conversation') or 
                         msg_body.get('extendedTextMessage', {}).get('text') or "")

        if texto_usuario:
            respuesta_ia = get_gemini_response(texto_usuario)
            
            # Si la respuesta es un JSON de acción, lo manejamos (o enviamos confirmación)
            if '{"action":' in respuesta_ia:
                texto_para_enviar = "¡Excelente elección! Estoy tomando nota de tu pedido/cita ahora mismo... 📝"
            else:
                texto_para_enviar = respuesta_ia

            enviar_whatsapp(numero, texto_para_enviar)
            
            # Registro en Supabase para que no se pierda nada
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario, 
                "bot_response": texto_para_enviar
            }).execute()

        return jsonify({"status": "success"}), 200
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Error interno"}), 500

def enviar_whatsapp(numero, texto):
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"}
    payload = {"number": numero, "textMessage": {"text": texto}}
    requests.post(url, headers=headers, json=payload, timeout=10)

@app.route('/')
def health(): return "Bot con Contexto Activo 🚀", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)