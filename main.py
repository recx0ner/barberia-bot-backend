import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors # Necesario para el manejo de cuotas
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE LLAVES Y ENTORNO ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k] # Rotación de 3 llaves

META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # Tu número para confirmar pagos
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# --- FUNCIONES DE COMUNICACIÓN ---
def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

# --- IA CON SISTEMA DE REINTENTOS (FAILOVER) ---
def generar_respuesta_ia(instruccion, texto_usuario):
    llaves_shuffled = VALID_KEYS[:]
    random.shuffle(llaves_shuffled) # Desordenamos para no agotar siempre la misma

    for key in llaves_shuffled:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion},
                contents=texto_usuario
            )
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): # Si una llave está agotada, salta a la siguiente
                print(f"⚠️ Llave agotada, probando otra...")
                continue
            raise e
    return "Lo siento, mis sistemas están saturados por ahora. Por favor, intenta en unos minutos."

# --- GESTIÓN DE BASE DE DATOS BLINDADA ---
def registrar_cita_y_cliente(id_num, texto_pedido):
    try:
        # 1. Registro previo de cliente para evitar Error 23503
        supabase.table('cliente').upsert({"id": id_num, "telefono": str(id_num)}).execute()

        # 2. Inserción en tabla citas usando tus columnas reales
        supabase.table('citas').insert({
            "user_id": id_num, 
            "servicio": texto_pedido, # Lo que pidió el cliente
            "fecha_hora": datetime.now(venezuela_tz).isoformat(),
            "estado": "pendiente"
        }).execute()
        return "✅ Tu pedido ha sido agendado."
    except Exception as e:
        print(f"🔥 Error DB: {str(e)}") # Ocultamos el error técnico al cliente
        return "✅ ¡Entendido! Ya tomé nota de tu solicitud."

# --- WEBHOOK PRINCIPAL ---
@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200

    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero_raw = msg['from']
            id_num = int(numero_raw) # Conversión obligatoria a int8
            texto = msg.get('text', {}).get('body', "").strip()
            ahora_ve = datetime.now(venezuela_tz).strftime('%Y-%m-%d %H:%M:%S')

            # 1. Memoria Histórica
            res_mem = supabase.table('messages').select('user_input, bot_response')\
                .eq('user_id', id_num).order('created_at', desc=True).limit(5).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            # 2. Datos del Cliente (Identidad)
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_cli = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"

            # 3. Procesamiento de IA con Reintentos
            instruccion = f"""{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\nCliente: {nombre_cli}\nHistorial:\n{historial}
            REGLAS:
            - Si no sabes su nombre, pregunta amablemente.
            - Si te da su nombre, usa: [ACCION:NOMBRE:Nombre]
            - Si confirma pedido, usa: [ACCION:AGENDAR]
            """
            
            respuesta_ia = generar_respuesta_ia(instruccion, texto)

            # 4. Lógica de Acciones
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nuevo_nombre = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nuevo_nombre, "telefono": str(id_num)}).execute()
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:AGENDAR]" in respuesta_ia:
                feedback = registrar_cita_y_cliente(id_num, texto)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip() + f"\n\n{feedback}"

            # 5. Envío y Registro de mensaje
            enviar_meta(numero_raw, respuesta_ia)
            supabase.table('messages').insert({
                "user_id": id_num, 
                "user_input": texto, 
                "bot_response": respuesta_ia
            }).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)