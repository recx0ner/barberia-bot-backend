import os, random, requests, traceback
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN ---
# Meta Cloud API
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")

# Evolution API
EVO_URL = os.environ.get("EVOLUTION_URL", "").strip().rstrip('/')
EVO_KEY = os.environ.get("EVOLUTION_APIKEY")
EVO_INST = os.environ.get("EVOLUTION_INSTANCE")

# IA y Base de Datos
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente de pizzería.")
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# --- FUNCIONES DE ENVÍO ---

def enviar_respuesta(canal, numero, texto):
    if canal == "META":
        url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": texto}}
        res = requests.post(url, headers=headers, json=payload)
        print(f"📤 Respuesta enviada por META (Status: {res.status_code})")
    
    elif canal == "EVOLUTION":
        url = f"{EVO_URL}/message/sendText/{EVO_INST}"
        headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
        payload = {"number": numero, "text": texto}
        res = requests.post(url, headers=headers, json=payload)
        print(f"📤 Respuesta enviada por EVOLUTION (Status: {res.status_code})")

# --- WEBHOOK HÍBRIDO ---

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    # Verificación para Meta
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200

    payload = request.json
    canal = None
    numero = None
    texto_usuario = None

    try:
        # 1. Detectar si el mensaje viene de META
        if 'object' in payload and 'entry' in payload:
            canal = "META"
            msg_data = payload['entry'][0]['changes'][0]['value'].get('messages', [{}])[0]
            numero = msg_data.get('from')
            texto_usuario = msg_data.get('text', {}).get('body')

        # 2. Detectar si viene de EVOLUTION (Si no es Meta)
        elif not canal:
            canal = "EVOLUTION"
            data = payload.get('data', payload)
            if data.get('key', {}).get('fromMe') is True: return jsonify({"status": "ignored"}), 200
            numero = data.get('key', {}).get('remoteJid', '').split('@')[0]
            texto_usuario = data.get('message', {}).get('conversation') or \
                            data.get('message', {}).get('extendedTextMessage', {}).get('text')

        # 3. Procesar y Responder
        if numero and texto_usuario:
            print(f"📩 Mensaje recibido por {canal} de {numero}: {texto_usuario}")
            
            # Generar IA
            client = genai.Client(api_key=random.choice(VALID_KEYS))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': BUSINESS_CONTEXT},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()

            # Enviar por el mismo canal de entrada
            enviar_respuesta(canal, numero, respuesta_ia)

            # Guardar en Supabase (Centralizado)
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario,
                "bot_response": respuesta_ia,
                "platform": canal
            }).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))