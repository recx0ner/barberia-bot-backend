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

# --- UTILIDADES DE BÚSQUEDA ---

def buscar_llave(data, llave):
    """Busca una llave específica en todo el JSON, sin importar la profundidad."""
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
        instruction = f"{BUSINESS_CONTEXT}\nFecha: {datetime.now().strftime('%Y-%m-%d')}"
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            config={'system_instruction': instruction},
            contents=user_text
        )
        return response.text.strip()
    except Exception as e:
        print(f"❌ Error Gemini: {e}")
        return "¡Ups! Mi horno mental falló. ¿Me repites?"

# --- WEBHOOK PRINCIPAL ---

@app.route('/whatsapp', methods=['POST'])
def webhook():
    payload = request.json
    print("--- NUEVO PAYLOAD DETECTADO ---")
    
    # Imprimimos las llaves principales para diagnóstico rápido
    print(f"Llaves en el paquete: {list(payload.keys())}")

    try:
        # 1. BÚSQUEDA AGRESIVA DEL REMOTEPID (Número)
        remote_jid = buscar_llave(payload, 'remoteJid')
        
        # 2. EVITAR AUTO-RESPUESTAS
        from_me = buscar_llave(payload, 'fromMe')
        if from_me is True:
            return jsonify({"status": "ignored_self"}), 200

        if not remote_jid:
            print("⚠️ No se encontró remoteJid. Puede que no sea un mensaje de texto.")
            return jsonify({"status": "no_jid_detected"}), 200

        numero = str(remote_jid).split('@')[0]

        # 3. BÚSQUEDA AGRESIVA DEL TEXTO
        texto_usuario = (
            buscar_llave(payload, 'conversation') or 
            buscar_llave(payload, 'text') or 
            buscar_llave(payload, 'displayText') or ""
        )

        if not texto_usuario:
            print(f"⚠️ Mensaje de {numero} sin texto legible.")
            return jsonify({"status": "no_text"}), 200

        print(f"📩 Procesando: '{texto_usuario}' de {numero}")

        # 4. GENERAR Y ENVIAR RESPUESTA
        respuesta_ia = get_gemini_response(texto_usuario)
        
        url_send = f"{EVO_URL}/message/sendText/{EVO_INST}"
        headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
        send_data = {"number": numero, "textMessage": {"text": respuesta_ia}}
        
        res = requests.post(url_send, headers=headers, json=send_data, timeout=12)
        print(f"📤 Evolution Status: {res.status_code}")

        # 5. GUARDADO EN SUPABASE
        try:
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario,
                "bot_response": respuesta_ia
            }).execute()
            print("✅ Supabase sincronizado.")
        except Exception as e:
            print(f"🔥 Error Supabase: {e}")

        return jsonify({"status": "success"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route('/')
def health(): return "Pizza & Barber Bot Online 🚀", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)