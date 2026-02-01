import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE LLAVES (ROTACIÓN) ---
# Extraemos las 3 llaves del panel de Render
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
# Filtramos solo las que no estén vacías
VALID_KEYS = [k for k in GEMINI_KEYS if k]

# --- RESTO DE CONFIGURACIÓN ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

# --- GESTIÓN DE BASE DE DATOS ---
def registrar_o_actualizar_cliente(id_num, nombre_nuevo=None):
    try:
        datos = {"id": id_num, "telefono": str(id_num)}
        if nombre_nuevo: datos["nombre"] = nombre_nuevo
        supabase.table('cliente').upsert(datos).execute()
    except: pass

def registrar_cita_v22(id_num, texto_pedido):
    try:
        supabase.table('citas').insert({
            "user_id": id_num, 
            "servicio": texto_pedido,
            "fecha_hora": datetime.now(venezuela_tz).isoformat()
        }).execute()
        return "✅ Pedido agendado."
    except: return "✅ Anotado."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try: entry = payload['entry'][0]['changes'][0]['value']
    except: return jsonify({"status": "no_data"}), 200

    if 'messages' in entry:
        msg = entry['messages'][0]
        numero_raw = msg['from']
        id_num = int(numero_raw)
        texto_usuario = msg.get('text', {}).get('body', "").strip()

        # 1. ¿Cómo se llama? (Consulta a Supabase)
        res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
        nombre_cliente = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] != "Cliente WhatsApp" else "Desconocido"
        
        # 2. IA CON ROTACIÓN DE LLAVES 🔄
        # Elegimos una llave al azar de las 3 disponibles
        api_key_elegida = random.choice(VALID_KEYS)
        client = genai.Client(api_key=api_key_elegida)
        
        instruccion = f"""{BUSINESS_CONTEXT}
        Cliente: {nombre_cliente}
        - Si es 'Desconocido', pregunta el nombre.
        - Si te da su nombre, usa el tag: [ACCION:NOMBRE:Nombre]
        - Si confirma pedido, usa el tag: [ACCION:AGENDAR]
        """
        
        response = client.models.generate_content(model='gemini-2.5-flash', config={'system_instruction': instruccion}, contents=texto_usuario)
        respuesta_ia = response.text.strip()

        # 3. PROCESAR ACCIONES
        if "[ACCION:NOMBRE:" in respuesta_ia:
            nuevo_nombre = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
            registrar_o_actualizar_cliente(id_num, nuevo_nombre)
            respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

        if "[ACCION:AGENDAR]" in respuesta_ia:
            registrar_o_actualizar_cliente(id_num)
            feedback = registrar_cita_v22(id_num, texto_usuario)
            respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip() + f"\n\n{feedback}"

        enviar_meta(numero_raw, respuesta_ia)
        supabase.table('messages').insert({"user_id": id_num, "user_input": texto_usuario, "bot_response": respuesta_ia}).execute()

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)