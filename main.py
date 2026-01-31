import os, random, json, requests, traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN ---
PORT = int(os.environ.get("PORT", 10000))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente de pizzería.")
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]

# Conexiones
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
EVO_URL = os.environ.get("EVOLUTION_URL")
EVO_KEY = os.environ.get("EVOLUTION_APIKEY")
EVO_INST = os.environ.get("EVOLUTION_INSTANCE")

def get_gemini_response(user_text):
    try:
        client = genai.Client(api_key=random.choice(VALID_KEYS))
        instruction = f"{BUSINESS_CONTEXT}\nFecha: {datetime.now().strftime('%Y-%m-%d')}"
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            config={'system_instruction': instruction},
            contents=user_text
        )
        return response.text.strip()
    except Exception as e:
        print(f"❌ Error en Gemini: {e}")
        return "Lo siento, tuve un problema técnico."

@app.route('/whatsapp', methods=['POST'])
def webhook():
    # 1. CAPTURA TOTAL: Esto aparecerá sí o sí en tus logs
    data = request.json
    print(f"--- NUEVO MENSAJE RECIBIDO ---")
    print(json.dumps(data, indent=2)) 

    try:
        # 2. EXTRAER DATOS (Versión ultra-compatible)
        msg_data = data.get('data', {})
        if not msg_data: 
            print("⚠️ El JSON no tiene la llave 'data'. Revisa la config de Evolution.")
            return jsonify({"status": "no_data"}), 200

        # Evitar bucles
        if msg_data.get('key', {}).get('fromMe'):
            return jsonify({"status": "skipped_self"}), 200

        # Extraer número y texto
        numero = msg_data.get('key', {}).get('remoteJid', '').split('@')[0]
        msg_body = msg_data.get('message', {})
        texto_usuario = (msg_body.get('conversation') or 
                         msg_body.get('extendedTextMessage', {}).get('text') or "")

        if texto_usuario:
            print(f"📩 Procesando mensaje: '{texto_usuario}' de {numero}")
            
            respuesta = get_gemini_response(texto_usuario)
            
            # 3. INTENTO DE ENVÍO
            url_send = f"{EVO_URL}/message/sendText/{EVO_INST}"
            headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
            payload = {"number": numero, "textMessage": {"text": respuesta}}
            
            res = requests.post(url_send, headers=headers, json=payload, timeout=15)
            print(f"📤 Respuesta Evolution: {res.status_code} - {res.text}")

            # 4. GUARDADO EN SUPABASE
            try:
                supabase.table('messages').insert({
                    "user_id": numero, 
                    "user_input": texto_usuario,
                    "bot_response": respuesta
                }).execute()
                print("✅ Guardado en Supabase con éxito.")
            except Exception as e:
                print(f"🔥 Error guardando en Supabase: {e}")

        return jsonify({"status": "processed"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Servidor Activo 🍕", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)