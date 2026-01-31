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

# --- 1. CONFIGURACIÓN ---
# Rotación de llaves para máxima estabilidad
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Conexión con Evolution API (QR)
EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "BarberiaBot")

# Inicializar Base de Datos
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- 2. PROMPT DE COMPORTAMIENTO ---
SYSTEM_PROMPT = f"""
Eres el asistente virtual de una barbería moderna. Tu tono es profesional y breve.
Hoy es: {datetime.now().strftime('%Y-%m-%d %H:%M')}

--- REGLAS DE RESERVAS ---
1. Si el usuario quiere RESERVAR y da Nombre, Fecha y Hora:
   Responde SOLO este JSON: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}

2. Si el usuario quiere CANCELAR una cita:
   Responde SOLO este JSON: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}

Si falta información, pídela amablemente.
"""

# --- 3. LÓGICA DE CEREBRO (GEMINI 3 FLASH) ---

def get_gemini_3_response(user_text, numero):
    try:
        # 1. Rotación de llaves aleatoria
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        
        # 2. Configuración del modelo Gemini 3 Flash
        model = genai.GenerativeModel(
            model_name='gemini-3-flash', # ¡Actualizado según tu captura!
            system_instruction=SYSTEM_PROMPT
        )
        
        # 3. Generar respuesta
        response = model.generate_content(user_text)
        txt = response.text.strip()
        
        # 4. Procesar si es una acción (JSON)
        if 'action":' in txt:
            try:
                clean = txt.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean[clean.find('{'):clean.rfind('}')+1])
                # Aquí llamarías a tus funciones de gestionar_reserva(data, numero)
                return f"✅ Entendido. Procesando tu {data.get('action')} para el {data.get('fecha_hora')}."
            except: pass
            
        return txt
    except Exception as e:
        print(f"❌ Error en Gemini 3: {e}")
        return "Lo siento, mi conexión con la red neuronal de Google falló. ¿Me repites?"

# --- 4. ENVÍO Y WEBHOOK ---

def enviar_whatsapp(numero, texto):
    try:
        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        headers = {"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"}
        payload = {"number": numero, "textMessage": {"text": texto}}
        requests.post(url, headers=headers, json=payload)
    except: pass

@app.route('/whatsapp', methods=['POST'])
def webhook():
    try:
        data = request.json
        # Ajuste: Evolution usa "messages.upsert" o "MESSAGES_UPSERT" según config
        event_type = data.get("type", "").lower() 
        if event_type != "messages.upsert": 
            return jsonify({"status": "ignored_event"}), 200
        
        msg_data = data.get('data', {})
        # Evitar que el bot se responda a sí mismo
        if msg_data.get('key', {}).get('fromMe'): 
            return jsonify({"status": "ok"}), 200
        
        numero = msg_data['key']['remoteJid'].split('@')[0]
        
        # Extraer texto de forma más robusta
        message_content = msg_data.get('message', {})
        texto = (message_content.get('conversation') or 
                 message_content.get('extendedTextMessage', {}).get('text') or "")

        if texto:
            print(f"📩 Procesando mensaje de {numero}: {texto}")
            # Sugerencia: Asegúrate que 'gemini-2.5-flash' es el ID correcto
            respuesta = get_gemini_3_response(texto, numero) 
            enviar_whatsapp(numero, respuesta)
            
            # Registro en Supabase
            try:
                supabase.table('messages').insert({
                    "user_id": numero, 
                    "user_input": texto, 
                    "bot_response": respuesta, 
                    "platform": "whatsapp"
                }).execute()
                print("✅ Guardado en Supabase")
            except Exception as e:
                print(f"🔥 Error Supabase: {e}")
            
        return jsonify({"status": "success"}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500