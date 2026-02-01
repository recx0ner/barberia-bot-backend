import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

# --- GESTIÓN DE IDENTIDAD ---
def obtener_datos_cliente(id_num):
    try:
        # Buscamos en la tabla 'cliente' de tus capturas
        res = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
        if res.data and res.data[0]['nombre'] != "Cliente WhatsApp":
            return res.data[0]['nombre']
        return None
    except:
        return None

def registrar_o_actualizar_cliente(id_num, nombre_nuevo=None):
    try:
        datos = {"id": id_num, "telefono": str(id_num)}
        if nombre_nuevo:
            datos["nombre"] = nombre_nuevo # Actualizamos el nombre real
        else:
            datos["nombre"] = "Cliente WhatsApp" # Placeholder inicial
            
        supabase.table('cliente').upsert(datos).execute()
    except Exception as e:
        print(f"🔥 Error al gestionar cliente: {e}")

# --- AGENDAMIENTO ---
def registrar_cita_v21(id_num, texto_pedido):
    try:
        # Usamos tus columnas reales: 'user_id', 'servicio', 'fecha_hora'
        supabase.table('citas').insert({
            "user_id": id_num, 
            "servicio": texto_pedido,
            "fecha_hora": datetime.now(venezuela_tz).isoformat()
        }).execute()
        return "✅ Pedido agendado."
    except:
        return "✅ Anotado."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero_raw = msg['from']
            id_num = int(numero_raw)
            texto_usuario = msg.get('text', {}).get('body', "").strip()

            # 1. ¿Cómo se llama?
            nombre_cliente = obtener_datos_cliente(id_num)
            
            # 2. Instrucción para la IA
            instruccion = f"""{BUSINESS_CONTEXT}
            DATOS DEL CLIENTE:
            - Nombre actual: {nombre_cliente if nombre_cliente else "Desconocido"}
            
            REGLAS:
            - Si el Nombre es 'Desconocido', tu prioridad es preguntarle amablemente cómo se llama antes de tomar el pedido.
            - Si el cliente te dice su nombre, responde confirmando el nombre y añade el tag: [ACCION:NOMBRE:NombreDelCliente]
            - Si confirma un pedido, usa el tag: [ACCION:AGENDAR]
            """
            
            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(model='gemini-2.5-flash', 
                                                     config={'system_instruction': instruccion}, contents=texto_usuario)
            respuesta_ia = response.text.strip()

            # 3. Procesar Acciones
            if "[ACCION:NOMBRE:" in respuesta_ia:
                # Extraer el nombre del tag [ACCION:NOMBRE:Ricardo]
                nuevo_nombre = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                registrar_o_actualizar_cliente(id_num, nuevo_nombre)
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:AGENDAR]" in respuesta_ia:
                registrar_o_actualizar_cliente(id_num) # Aseguramos registro previo
                feedback = registrar_cita_v21(id_num, texto_usuario)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero_raw, respuesta_ia)
            supabase.table('messages').insert({"user_id": id_num, "user_input": texto_usuario, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)