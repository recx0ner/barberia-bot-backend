import os, random, json, requests, traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN ---
PORT = int(os.environ.get("PORT", 10000))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente divertido de una pizzería.")
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE")

def get_gemini_response(user_text):
    try:
        client = genai.Client(api_key=random.choice(VALID_KEYS))
        instruction = f"{BUSINESS_CONTEXT}\nFecha actual: {datetime.now().strftime('%Y-%m-%d')}"
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            config={'system_instruction': instruction},
            contents=user_text
        )
        return response.text.strip()
    except Exception as e:
        return f"Error en IA: {str(e)}"

@app.route('/whatsapp', methods=['POST'])
def webhook():
    try:
        data = request.json
        # ESTO ES CLAVE: Ver que está llegando realmente
        print(f"DEBUG: Datos recibidos -> {json.dumps(data)}")

        # Intentamos obtener el mensaje sin importar la estructura exacta
        msg_data = data.get('data', {})
        if msg_data.get('key', {}).get('fromMe'):
            return jsonify({"status": "ignoring_self"}), 200

        numero = msg_data.get('key', {}).get('remoteJid', '').split('@')[0]
        msg_body = msg_data.get('message', {})
        texto_usuario = (msg_body.get('conversation') or 
                         msg_body.get('extendedTextMessage', {}).get('text') or "")

        if texto_usuario and numero:
            print(f"📩 Procesando: {texto_usuario} de {numero}")
            
            respuesta_ia = get_gemini_response(texto_usuario)
            
            # Enviar a WhatsApp
            url_send = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
            headers = {"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"}
            payload = {"number": numero, "textMessage": {"text": respuesta_ia}}
            res = requests.post(url_send, headers=headers, json=payload)
            print(f"📤 Status Envío WA: {res.status_code}")

            # Guardar en Supabase
            try:
                # IMPORTANTE: Asegúrate que estas columnas existan en tu tabla 'messages'
                supabase.table('messages').insert({
                    "user_id": numero, 
                    "user_input": texto_usuario,
                    "bot_response": respuesta_ia
                }).execute()
                print("✅ Guardado en Supabase")
            except Exception as e:
                print(f"🔥 Error Supabase: {e}")

        return jsonify({"status": "success"}), 200
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "check logs"}), 500

@app.route('/')
def health(): return "Pizza Bot Online 🍕", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)