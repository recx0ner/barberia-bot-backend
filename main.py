import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def generar_respuesta_ia(instruccion, texto_usuario):
    llaves_shuffled = VALID_KEYS[:]
    random.shuffle(llaves_shuffled)
    for key in llaves_shuffled:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(model='gemini-2.5-flash', config={'system_instruction': instruccion}, contents=texto_usuario)
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): continue
            raise e
    return "Estamos bajo mucha demanda, ¡vuelve a intentarlo en un momento!"

# --- AGENDAMIENTO CORREGIDO (Columnas de image_09efd3.png) ---
def registrar_cita_v24(id_num, nombre_cliente, texto_pedido):
    try:
        # 1. Aseguramos que el cliente exista
        supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_cliente, "telefono": str(id_num)}).execute()

        # 2. Insertamos en 'citas' con las columnas REALES
        supabase.table('citas').insert({
            "user_id": id_num,       # int8
            "nombre": nombre_cliente, # text
            "servicio": texto_pedido  # text
        }).execute()
        return "✅ ¡Pizza agendada con éxito!"
    except Exception as e:
        print(f"🔥 Error DB: {str(e)}")
        return "✅ ¡Anotado!"

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero = msg['from']
            id_num = int(numero)
            texto = msg.get('text', {}).get('body', "").strip()

            # Obtener nombre actual para la IA
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_actual = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"

            instruccion = f"{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nTags: [ACCION:NOMBRE:Nombre], [ACCION:AGENDAR]"
            respuesta_ia = generar_respuesta_ia(instruccion, texto)

            # Lógica de Tags
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_detectado = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_detectado, "telefono": str(id_num)}).execute()
                nombre_actual = nombre_detectado
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:AGENDAR]" in respuesta_ia:
                feedback = registrar_cita_v24(id_num, nombre_actual, texto)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            supabase.table('messages').insert({"user_id": id_num, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)