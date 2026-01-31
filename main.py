import os, random, json, requests, traceback, time
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
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# Variables limpias
EVO_URL = os.environ.get("EVOLUTION_URL", "").strip().rstrip('/')
EVO_KEY = os.environ.get("EVOLUTION_APIKEY", "").strip()
EVO_INST = os.environ.get("EVOLUTION_INSTANCE", "").strip()

def enviar_whatsapp(numero, texto):
    url_send = f"{EVO_URL}/message/sendText/{EVO_INST}"
    payload = {"number": numero, "textMessage": {"text": texto}}
    headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
    
    # Intentamos hasta 2 veces por si la API está despertando
    for intento in range(2):
        try:
            print(f"🚀 Intento {intento+1} enviando a: {url_send}")
            res = requests.post(url_send, headers=headers, json=payload, timeout=20)
            print(f"📤 STATUS EVOLUTION: {res.status_code}")
            
            if res.status_code in [200, 201]:
                return True
            
            if res.status_code == 502:
                print("⏳ La API está despertando (502), esperando 5 segundos...")
                time.sleep(5)
                continue
                
            print(f"⚠️ Error API: {res.text[:100]}")
            break
        except Exception as e:
            print(f"🔥 Error en el POST: {e}")
            time.sleep(2)
    return False

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        challenge = request.args.get('hub.challenge')
        if challenge: return challenge, 200

    payload = request.json
    print("--- NUEVO PAYLOAD ---")
    
    try:
        # Extracción para Meta Cloud (tu formato actual)
        try:
            msg_data = payload['entry'][0]['changes'][0]['value']['messages'][0]
            numero = msg_data.get('from')
            texto_usuario = msg_data.get('text', {}).get('body')
        except:
            numero, texto_usuario = None, None

        if numero and texto_usuario:
            # 1. IA
            client = genai.Client(api_key=random.choice(VALID_KEYS))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': BUSINESS_CONTEXT},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()
            
            # 2. ENVIAR (Con reintento)
            enviado = enviar_whatsapp(numero, respuesta_ia)
            
            # 3. SUPABASE (Siempre guardar)
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario,
                "bot_response": respuesta_ia
            }).execute()
            print(f"✅ Supabase OK. ¿Enviado a WA?: {enviado}")
            
        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Bot Online 🍕", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)