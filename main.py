import os, random, json, requests, traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN ---
PORT = int(os.environ.get("PORT", 10000))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente divertido de una pizzería.")

# Llaves de Gemini y Supabase
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# Configuración de Evolution API
EVO_URL = os.environ.get("EVOLUTION_URL")
EVO_KEY = os.environ.get("EVOLUTION_APIKEY")
EVO_INST = os.environ.get("EVOLUTION_INSTANCE")

# --- 2. LÓGICA DE IA ---

def get_gemini_response(user_text):
    try:
        client = genai.Client(api_key=random.choice(VALID_KEYS))
        instruction = f"{BUSINESS_CONTEXT}\nFecha: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            config={'system_instruction': instruction},
            contents=user_text
        )
        return response.text.strip()
    except Exception as e:
        print(f"❌ Error Gemini: {e}")
        return "Lo siento, tuve un pequeño problema técnico. ¿Me repites?"

# --- 3. FUNCIÓN DE ENVÍO (CORREGIDA PARA EVITAR EL 502) ---

def enviar_whatsapp(numero, texto):
    try:
        # AUTO-LIMPIEZA: Quitamos espacios y barras al final de la URL
        base_url = EVO_URL.strip().rstrip('/')
        url_send = f"{base_url}/message/sendText/{EVO_INST}"
        
        headers = {
            "apikey": EVO_KEY, 
            "Content-Type": "application/json"
        }
        
        payload = {
            "number": numero, 
            "textMessage": {"text": texto}
        }
        
        print(f"DEBUG: Intentando enviar a -> {url_send}")
        res = requests.post(url_send, headers=headers, json=payload, timeout=15)
        
        print(f"📤 Evolution Status: {res.status_code}")
        if res.status_code not in [200, 201]:
            print(f"⚠️ Error detallado de Evolution: {res.text}")
            
        return res.status_code
    except Exception as e:
        print(f"🔥 Error crítico en el envío: {e}")
        return 500

# --- 4. WEBHOOK UNIVERSAL ---

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    # Soporte para Verificación de Meta (Webhooks)
    if request.method == 'GET':
        challenge = request.args.get('hub.challenge')
        if challenge: return challenge, 200

    payload = request.json
    print("--- NUEVO PAYLOAD DETECTADO ---")
    
    numero = None
    texto_usuario = None

    try:
        # Detectar formato Meta Cloud API (Tus logs actuales)
        if 'object' in payload and 'entry' in payload:
            print("📦 Formato detectado: Meta Cloud API")
            try:
                entry = payload['entry'][0]
                changes = entry.get('changes', [{}])[0]
                value = changes.get('value', {})
                if 'messages' in value:
                    msg = value['messages'][0]
                    numero = msg.get('from')
                    texto_usuario = msg.get('text', {}).get('body')
            except: pass

        # Detectar formato Evolution API (Backup)
        if not numero:
            print("📦 Formato detectado: Evolution API / Genérico")
            data_content = payload.get('data', payload)
            numero = data_content.get('key', {}).get('remoteJid', '').split('@')[0]
            msg_obj = data_content.get('message', {})
            texto_usuario = msg_obj.get('conversation') or msg_obj.get('extendedTextMessage', {}).get('text')

        # Procesar el mensaje
        if numero and texto_usuario:
            print(f"📩 Mensaje de {numero}: {texto_usuario}")
            
            # 1. Obtener respuesta de IA
            respuesta_ia = get_gemini_response(texto_usuario)
            
            # 2. Enviar a WhatsApp
            enviar_whatsapp(numero, respuesta_ia)
            
            # 3. Guardar en Supabase (Ya funcionando)
            try:
                supabase.table('messages').insert({
                    "user_id": numero, 
                    "user_input": texto_usuario,
                    "bot_response": respuesta_ia
                }).execute()
                print("✅ Supabase sincronizado.")
            except Exception as e:
                print(f"🔥 Error Supabase: {e}")
        else:
            print("⚠️ No se pudo extraer número o texto del paquete.")

        return jsonify({"status": "success"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Bot Multi-Canal Activo 🚀", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)