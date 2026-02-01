import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)

# --- CONFIGURACIÓN DE ZONA HORARIA ---
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN CENTRAL ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de Pizzas El Guaro.")

# --- 1. FUNCIÓN DE MEMORIA ---
def obtener_contexto(user_id):
    try:
        # Recuperamos historial para que el bot no olvide qué pidió el cliente
        res = supabase.table('messages').select('user_input, bot_response')\
            .eq('user_id', user_id).order('created_at', desc=True).limit(8).execute()
        
        historial = ""
        for msg in reversed(res.data):
            historial += f"Cliente: {msg['user_input']}\nBot: {msg['bot_response']}\n"
        return historial
    except:
        return ""

# --- 2. FUNCIÓN DE AGENDAMIENTO (La que faltaba) ---
def ejecutar_agendamiento(user_id, texto_pedido):
    try:
        # Inserta el pedido en tu tabla 'citas'
        # Nota: Asegúrate de que tu tabla en Supabase tenga estas columnas
        supabase.table('citas').insert({
            "user_id": user_id, 
            "detalle": texto_pedido,
            "estado": "pendiente"
        }).execute()
        print(f"📅 PEDIDO REGISTRADO: {user_id}")
        return True
    except Exception as e:
        print(f"🔥 Error al agendar: {e}")
        return False

# --- 3. WEBHOOK PRINCIPAL ---
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

            # Obtener hora de Venezuela para evitar desfases
            ahora_ve = datetime.now(venezuela_tz)
            fecha_hora_str = ahora_ve.strftime('%Y-%m-%d %H:%M:%S')

            memoria = obtener_contexto(numero)

            # Instrucción Maestra para la IA
            instruccion_maestra = f"""
            {BUSINESS_CONTEXT}
            Hora en Venezuela: {fecha_hora_str}
            
            HISTORIAL:
            {memoria}
            
            REGLAS DE ACCIÓN:
            - Si el cliente confirma un pedido o cita (ej: "quiero una pizza"), agrega al FINAL de tu respuesta: [ACCION:AGENDAR]
            """

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion_maestra},
                contents=texto_usuario
            )
            respuesta_ia = response.text.strip()

            # --- LÓGICA DE ACTIVACIÓN DE AGENDAMIENTO ---
            if "[ACCION:AGENDAR]" in respuesta_ia:
                exito = ejecutar_agendamiento(numero, texto_usuario)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip()
                if exito:
                    respuesta_ia += "\n\n✅ *Pedido anotado en nuestro sistema.*"

            # Enviar respuesta por Meta
            url_meta = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
            requests.post(url_meta, headers={"Authorization": f"Bearer {META_TOKEN}"}, 
                          json={"messaging_product": "whatsapp", "to": numero, "text": {"body": respuesta_ia}})

            # Guardar registro en Supabase
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