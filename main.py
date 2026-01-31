import os, random, json, requests, traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN ---
PORT = int(os.environ.get("PORT", 10000))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente de pizzería.")
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# Variables de Evolution
EVO_URL = os.environ.get("EVOLUTION_URL", "").strip().rstrip('/')
EVO_KEY = os.environ.get("EVOLUTION_APIKEY", "").strip()
EVO_INST = os.environ.get("EVOLUTION_INSTANCE", "").strip()

# --- 2. FUNCIÓN DE ENVÍO (Súper Reforzada) ---

def enviar_whatsapp(numero, texto):
    try:
        # CONSTRUCCIÓN SEGURA DE URL
        # Evolution v2 usa /message/sendText/{instance}
        url_send = f"{EVO_URL}/message/sendText/{EVO_INST}"
        
        headers = {
            "apikey": EVO_KEY, 
            "Content-Type": "application/json"
        }
        
        payload = {
            "number": numero, 
            "textMessage": {"text": texto}
        }
        
        print(f"🚀 INTENTANDO ENVÍO A: {url_send}")
        res = requests.post(url_send, headers=headers, json=payload, timeout=15)
        
        print(f"📤 STATUS EVOLUTION: {res.status_code}")
        
        # Si no es 200/201, imprimimos solo el inicio de la respuesta para no inundar el log
        if res.status_code not in [200, 201]:
            print(f"⚠️ ERROR DETALLADO (Primeros 100 caracteres): {res.text[:100]}")
            
        return res.status_code
    except Exception as e:
        print(f"🔥 ERROR CRÍTICO EN ENVÍO: {e}")
        return 500

# --- 3. WEBHOOK ---

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        challenge = request.args.get('hub.challenge')
        if challenge: return challenge, 200

    payload = request.json
    print("--- NUEVO PAYLOAD DETECTADO ---")
    
    numero = None
    texto_usuario = None

    try:
        # Detección de formato Meta (El que estás recibiendo según logs)
        if 'object' in payload and 'entry' in payload:
            print("📦 Formato: Meta Cloud")
            try:
                msg_data = payload['entry'][0]['changes'][0]['value']['messages'][0]
                numero = msg_data.get('from')
                texto_usuario = msg_data.get('text', {}).get('body')
            except: pass
        
        # Formato Evolution (Backup)
        if not numero:
            data_content = payload.get('data', payload)
            numero = data_content.get('key', {}).get('remoteJid', '').split('@')[0]
            texto_usuario = data_content.get('message', {}).get('conversation') or \
                            data_content.get('message', {}).get('extendedTextMessage', {}).get('text')

        if numero and texto_usuario:
            # 1. IA
            client = genai.Client(api_key=random.choice(VALID_KEYS))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': f"{BUSINESS_CONTEXT}\nHoy: {datetime.now()}"},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()
            
            # 2. ENVIAR (Aquí es donde fallaba)
            enviar_whatsapp(numero, respuesta_ia)
            
            # 3. SUPABASE (Ya funciona, lo mantenemos igual)
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario,
                "bot_response": respuesta_ia,
                "platform": "whatsapp"
            }).execute()
            print("✅ Supabase sincronizado.")
            
        return jsonify({"status": "success"}), 200
    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Bot Activo 🚀", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)