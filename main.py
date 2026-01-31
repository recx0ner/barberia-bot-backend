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

@app.route('/whatsapp', methods=['POST'])
def webhook():
    payload = request.json
    print(f"--- NUEVO PAYLOAD DETECTADO ---")
    
    try:
        # 1. BUSCADOR DINÁMICO DE DATOS (No importa dónde los esconda Evolution)
        # Intentamos obtenerlo de 'data' o de la raíz directamente
        content = payload.get('data') if payload.get('data') else payload
        
        # Evitar respondernos a nosotros mismos
        if content.get('key', {}).get('fromMe') is True:
            return jsonify({"status": "ignored_self"}), 200

        # Extraer el número (remoteJid)
        remote_jid = content.get('key', {}).get('remoteJid', '')
        if not remote_jid:
            print("⚠️ No se encontró remoteJid en el payload.")
            return jsonify({"status": "no_jid"}), 200
        
        numero = remote_jid.split('@')[0]

        # Extraer el texto (buscamos en todas las ubicaciones posibles)
        msg_obj = content.get('message', {})
        texto_usuario = (
            msg_obj.get('conversation') or 
            msg_obj.get('extendedTextMessage', {}).get('text') or 
            content.get('text') or ""
        )

        if not texto_usuario:
            print("⚠️ No se pudo extraer texto del mensaje.")
            return jsonify({"status": "no_text"}), 200

        print(f"📩 Mensaje de {numero}: {texto_usuario}")

        # 2. GENERAR RESPUESTA
        respuesta_ia = get_gemini_response(texto_usuario)

        # 3. ENVIAR A WHATSAPP
        url_send = f"{EVO_URL}/message/sendText/{EVO_INST}"
        headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
        send_payload = {"number": numero, "textMessage": {"text": respuesta_ia}}
        
        res = requests.post(url_send, headers=headers, json=send_payload, timeout=10)
        print(f"📤 Evolution Status: {res.status_code}")

        # 4. GUARDAR EN SUPABASE
        try:
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario,
                "bot_response": respuesta_ia
            }).execute()
            print("✅ Supabase actualizado.")
        except Exception as e:
            print(f"🔥 Error Supabase: {e}")

        return jsonify({"status": "success"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Bot Online 🍕", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)