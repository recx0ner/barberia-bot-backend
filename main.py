import os, random, requests, traceback
from datetime import datetime
import pytz # <--- Librería para zonas horarias
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN DE ZONA HORARIA ---
# Definimos la zona horaria de Venezuela (UTC-4)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN CENTRAL ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de Pizzas El Guaro.")

# --- LÓGICA DE MEMORIA ---
def obtener_contexto(user_id):
    try:
        res = supabase.table('messages').select('user_input, bot_response')\
            .eq('user_id', user_id).order('created_at', desc=True).limit(8).execute()
        
        historial = ""
        for msg in reversed(res.data):
            historial += f"Cliente: {msg['user_input']}\nBot: {msg['bot_response']}\n"
        return historial
    except:
        return ""

# --- WEBHOOK ---
@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200

    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero = msg['from']
            texto_usuario = msg.get('text', {}).get('body', "")

            # 1. OBTENER HORA REAL DE VENEZUELA
            ahora_ve = datetime.now(venezuela_tz)
            fecha_hora_str = ahora_ve.strftime('%Y-%m-%d %H:%M:%S')

            memoria = obtener_contexto(numero)

            # 2. Instrucción con la hora corregida
            instruccion_maestra = f"""
            {BUSINESS_CONTEXT}
            
            UBICACIÓN Y TIEMPO:
            - Estás en Venezuela.
            - Fecha y Hora actual: {fecha_hora_str} (No uses la hora del sistema UTC)
            
            HISTORIAL DE LA CHARLA:
            {memoria}
            
            REGLAS:
            - Si el cliente confirma pedido, usa: [ACCION:AGENDAR]
            """

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion_maestra},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()

            # 3. Enviar respuesta por Meta
            url_meta = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
            requests.post(url_meta, headers={"Authorization": f"Bearer {META_TOKEN}"}, 
                          json={"messaging_product": "whatsapp", "to": numero, "text": {"body": respuesta_ia}})

            # 4. Guardar en Supabase
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario, 
                "bot_response": respuesta_ia
            }).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)