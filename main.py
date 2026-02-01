import os, random, requests, traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de Pizzas El Guaro.")

# --- LÓGICA DE MEMORIA ---
def obtener_memoria(user_id):
    try:
        # Buscamos los últimos 6 mensajes para dar contexto
        res = supabase.table('messages').select('user_input, bot_response')\
            .eq('user_id', user_id).order('created_at', desc=True).limit(6).execute()
        
        historial = ""
        for msg in reversed(res.data):
            historial += f"Cliente: {msg['user_input']}\nBot: {msg['bot_response']}\n"
        return historial
    except:
        return ""

# --- LÓGICA DE AGENDAMIENTO ---
def registrar_cita(user_id, detalle):
    # Inserta en la tabla 'citas' que vimos en tu Supabase
    supabase.table('citas').insert({"user_id": user_id, "detalle": detalle}).execute()
    print(f"📅 Cita agendada para {user_id}")

# --- WEBHOOK ACTUALIZADO ---
@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200

    payload = request.json
    try:
        # 1. Extracción de datos de Meta
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero = msg['from']
            texto_usuario = msg.get('text', {}).get('body', "")

            # 2. Recuperar Memoria
            contexto_previo = obtener_memoria(numero)

            # 3. Instrucción Maestra para Gemini
            instruccion = f"""
            {BUSINESS_CONTEXT}
            Fecha/Hora actual: {datetime.now().strftime('%Y-%m-%d %H:%M')}
            
            HISTORIAL RECIENTE:
            {contexto_previo}
            
            REGLAS DE ACCIÓN:
            - Si el cliente confirma un pedido o cita, finaliza tu respuesta con el tag: [ACCION:AGENDAR]
            - Si el cliente envía una referencia de pago móvil, finaliza con: [ACCION:PAGO]
            """

            # 4. Generar Respuesta con IA
            client = genai.Client(api_key=random.choice([os.environ.get("GEMINI_API_KEY_1")]))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()

            # 5. Ejecutar Acciones si la IA lo ordena
            if "[ACCION:AGENDAR]" in respuesta_ia:
                registrar_cita(numero, texto_usuario)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "✅ ¡Pedido registrado en sistema!")

            # 6. Responder por Meta
            url_meta = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
            requests.post(url_meta, headers={"Authorization": f"Bearer {META_TOKEN}"}, 
                          json={"messaging_product": "whatsapp", "to": numero, "text": {"body": respuesta_ia}})

            # 7. Guardar nuevo mensaje en Supabase
            supabase.table('messages').insert({
                "user_id": numero, "user_input": texto_usuario, "bot_response": respuesta_ia
            }).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)