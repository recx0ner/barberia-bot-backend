import os, random, requests, traceback
from datetime import datetime
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN CENTRAL ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de Pizzas El Guaro.")

# --- FUNCIÓN DE MEMORIA (Extrae historial de Supabase) ---
def obtener_contexto(user_id):
    try:
        # Traemos los últimos 8 mensajes para que el bot no pierda el hilo
        res = supabase.table('messages').select('user_input, bot_response')\
            .eq('user_id', user_id).order('created_at', desc=True).limit(8).execute()
        
        historial = ""
        for msg in reversed(res.data):
            historial += f"Cliente: {msg['user_input']}\nBot: {msg['bot_response']}\n"
        return historial
    except:
        return ""

# --- FUNCIÓN DE ACCIÓN (Agendar en la tabla 'citas') ---
def ejecutar_agendamiento(user_id, texto):
    try:
        # Registramos el detalle en tu tabla de citas
        supabase.table('citas').insert({
            "user_id": user_id, 
            "detalle": f"Pedido/Cita detectada: {texto}"
        }).execute()
        return True
    except:
        return False

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

            # 1. Recuperamos lo que hablamos antes con este cliente
            memoria = obtener_contexto(numero)

            # 2. Preparamos a la IA con memoria e instrucciones de acción
            instruccion_maestra = f"""
            {BUSINESS_CONTEXT}
            Hora actual: {datetime.now().strftime('%H:%M')}
            
            HISTORIAL DE LA CHARLA:
            {memoria}
            
            REGLAS DE ORO:
            - Si el cliente confirma un pedido o cita, pon al final de tu respuesta: [ACCION:AGENDAR]
            - Si el cliente envía una referencia de pago móvil, pon al final: [ACCION:PAGO]
            """

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion_maestra},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()

            # 3. Procesar Acciones (Si la IA puso el "tag")
            info_adicional = ""
            if "[ACCION:AGENDAR]" in respuesta_ia:
                ejecutar_agendamiento(numero, texto_usuario)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip()
                info_adicional = " (📅 Agendado)"

            # 4. Enviar respuesta por el número oficial de Meta
            url_meta = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
            requests.post(url_meta, headers={"Authorization": f"Bearer {META_TOKEN}"}, 
                          json={"messaging_product": "whatsapp", "to": numero, "text": {"body": respuesta_ia}})

            # 5. Guardar en Supabase para la próxima vez
            supabase.table('messages').insert({
                "user_id": numero, 
                "user_input": texto_usuario, 
                "bot_response": respuesta_ia + info_adicional
            }).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)