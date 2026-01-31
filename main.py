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

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
EVO_URL = os.environ.get("EVOLUTION_URL")
EVO_KEY = os.environ.get("EVOLUTION_APIKEY")
EVO_INST = os.environ.get("EVOLUTION_INSTANCE")

# --- FUNCIONES DE APOYO ---

def buscar_llave(data, llave):
    if isinstance(data, dict):
        if llave in data: return data[llave]
        for v in data.values():
            res = buscar_llave(v, llave)
            if res: return res
    elif isinstance(data, list):
        for item in data:
            res = buscar_llave(item, llave)
            if res: return res
    return None

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

# --- WEBHOOK ---

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    # Soporte para Verificación de Webhook de Meta (por si acaso)
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode and token:
            return challenge, 200

    payload = request.json
    print("--- NUEVO PAYLOAD DETECTADO ---")
    
    numero = None
    texto_usuario = None

    try:
        # 1. CASO A: Formato Meta Cloud API (Tus logs actuales)
        if 'object' in payload and 'entry' in payload:
            print("Detectado formato Meta Cloud API")
            try:
                entry = payload['entry'][0]
                changes = entry.get('changes', [{}])[0]
                value = changes.get('value', {})
                if 'messages' in value:
                    msg = value['messages'][0]
                    numero = msg.get('from')
                    texto_usuario = msg.get('text', {}).get('body') or msg.get('button', {}).get('text')
            except: pass

        # 2. CASO B: Formato Evolution API o Búsqueda Genérica
        if not numero:
            remote_jid = buscar_llave(payload, 'remoteJid')
            if remote_jid:
                numero = str(remote_jid).split('@')[0]
                texto_usuario = (buscar_llave(payload, 'conversation') or 
                                 buscar_llave(payload, 'text') or 
                                 buscar_llave(payload, 'displayText'))

        # 3. PROCESAMIENTO SI HAY DATOS
        if numero and texto_usuario:
            # Evitar auto-respuestas
            if buscar_llave(payload, 'fromMe') is True:
                return jsonify({"status": "ignored_self"}), 200

            print(f"📩 Mensaje de {numero}: {texto_usuario}")
            respuesta_ia = get_gemini_response(texto_usuario)

            # Enviar vía Evolution API
            url_send = f"{EVO_URL}/message/sendText/{EVO_INST}"
            headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
            send_data = {"number": numero, "textMessage": {"text": respuesta_ia}}
            
            res = requests.post(url_send, headers=headers, json=send_data, timeout=10)
            print(f"📤 Evolution Status: {res.status_code}")

            # Guardar en Supabase
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
            print("⚠️ El paquete no contenía un mensaje de texto procesable.")

        return jsonify({"status": "success"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Bot Multi-Formato Activo 🚀", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)