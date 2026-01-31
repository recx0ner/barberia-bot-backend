import os
import random
import json
import requests
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- CONFIGURACIÓN DE VARIABLES ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Datos de Evolution API (QR)
EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "BarberiaBot")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- LÓGICA DE IA ---
def get_gemini_response(user_text, chat_id):
    try:
        # Rotación de llaves
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        
        # Usamos 2.0 Flash Experimental (es la versión real actual)
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        # Prompt simplificado para asegurar respuesta
        prompt = f"Eres el asistente de una barbería. Responde de forma amable y breve. Cliente: {chat_id}"
        
        response = model.generate_content([prompt, user_text])
        return response.text.strip()
    except Exception as e:
        return f"Error IA: {str(e)}"

# --- ENVÍO DE MENSAJE (VÍA EVOLUTION QR) ---
def enviar_whatsapp_evolution(numero, texto):
    try:
        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        headers = {"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"}
        payload = {
            "number": numero,
            "textMessage": {"text": texto}
        }
        requests.post(url, headers=headers, json=payload)
    except: pass

# --- WEBHOOK COMPATIBLE CON EVOLUTION ---
@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    try:
        data = request.json
        # Verificamos que sea un mensaje de Evolution
        if data.get("type") == "MESSAGES_UPSERT":
            msg = data['data']
            if msg.get('key', {}).get('fromMe'): return jsonify({"status": "ignored"}), 200
            
            sender = msg['key']['remoteJid'].split('@')[0]
            
            # Extraer texto del mensaje
            body = (msg.get('message', {}).get('conversation') or 
                    msg.get('message', {}).get('extendedTextMessage', {}).get('text') or "")
            
            if body:
                print(f"📩 Mensaje recibido de {sender}: {body}")
                
                # 1. Obtener respuesta de la IA
                resp_text = get_gemini_response(body, sender)
                
                # 2. Guardar en Supabase (TABLA MESSAGES)
                supabase.table('messages').insert({
                    "user_id": sender, 
                    "user_input": body, 
                    "bot_response": resp_text, 
                    "platform": "whatsapp"
                }).execute()
                
                # 3. Enviar respuesta por WhatsApp
                enviar_whatsapp_evolution(sender, resp_text)
                
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        traceback.print_exc()
        return "Error", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))