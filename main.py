import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE LLAVES Y ENTORNO ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k] # Rotación de las 3 llaves

META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # Tu número para confirmar pagos
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# --- FUNCIONES DE COMUNICACIÓN ---
def enviar_meta(to, text):
    # Función para enviar mensajes a través del número oficial del bot
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

# --- IA CON FAILOVER (ROTACIÓN ANTIBLOQUEO) ---
def generar_respuesta_ia(instruccion, texto_usuario):
    # Implementa el uso de las 3 llaves para evitar el error 429
    llaves_shuffled = VALID_KEYS[:]
    random.shuffle(llaves_shuffled) 

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
            if "429" in str(e): # Si la cuota está agotada, salta a la siguiente
                print(f"⚠️ Llave agotada, reintentando con otra...")
                continue
            raise e
    return "Nuestros sistemas están algo saturados, por favor intenta en unos minutos."

# --- LÓGICA DE NEGOCIO (CITAS Y PAGOS) ---
def ejecutar_logica_negocio(id_num, accion, texto="", nombre_actual="Cliente"):
    try:
        # Aseguramos que el cliente exista para evitar Error 23503
        supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": str(id_num)}).execute()

        if accion == "AGENDAR":
            # Agendado en las columnas: user_id, nombre, servicio, fecha_hora, estado
            supabase.table('citas').insert({
                "user_id": id_num, 
                "nombre": nombre_actual,
                "servicio": texto, 
                "fecha_hora": datetime.now(venezuela_tz).isoformat(),
                "estado": "pendiente"
            }).execute()
            return "✅ ¡Pizza agendada! Ya estamos manos a la masa."
        
        elif accion == "PAGO":
            # Extrae la referencia de pago del texto
            ref = ''.join(filter(str.isdigit, texto))
            if len(ref) < 6: return "⚠️ Referencia inválida, por favor envíala completa."
            
            # Registra el pago en estado no verificado
            supabase.table('pagos').insert({"user_id": id_num, "referencia": ref, "verificado": False}).execute()
            
            # Notifica al administrador para su validación
            if ADMIN_PHONE:
                enviar_meta(ADMIN_PHONE, f"🔔 *PAGO RECIBIDO*\nCliente: {nombre_actual}\nRef: {ref}\n\nResponde: *CONFIRMAR {ref}*")
            return "⏳ Referencia recibida. Te avisaré cuando el administrador la verifique."

    except Exception as e:
        print(f"🔥 Error en DB: {str(e)}") # Ocultamos errores técnicos al cliente
        return "✅ ¡Entendido! Ya tomé nota de tu solicitud."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200

    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero_str = msg['from']
            id_num = int(numero_str) # Conversión a int8 para compatibilidad
            texto = msg.get('text', {}).get('body', "").strip()
            ahora_ve = datetime.now(venezuela_tz).strftime('%Y-%m-%d %H:%M:%S') #

            # 1. LÓGICA DE ADMINISTRADOR (VERIFICACIÓN DE PAGOS)
            if ADMIN_PHONE and numero_str == ADMIN_PHONE and texto.upper().startswith("CONFIRMAR"):
                ref_confirmar = texto.split()[-1]
                res_pago = supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_confirmar).execute()
                if res_pago.data:
                    cliente_id = res_pago.data[0]['user_id']
                    enviar_meta(str(cliente_id), "✅ *¡Pago verificado!* Tu pedido ya está en el horno. 🍕🔥")
                    return jsonify({"status": "pago_confirmado"}), 200

            # 2. IDENTIDAD Y MEMORIA
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_cli = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"
            
            res_mem = supabase.table('messages').select('user_input, bot_response')\
                .eq('user_id', numero_str).order('created_at', desc=True).limit(6).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            # 3. PROCESAMIENTO DE IA
            instruccion = f"""{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\nCliente: {nombre_cli}\nHistorial:\n{historial}
            REGLAS:
            - Si no sabes el nombre del cliente, pregúntalo antes de agendar.
            - Si el cliente te dice su nombre, usa: [ACCION:NOMBRE:Nombre]
            - Si confirma pedido, usa: [ACCION:AGENDAR]
            - Si envía comprobante/referencia de pago, usa: [ACCION:PAGO]
            """
            
            respuesta_ia = generar_respuesta_ia(instruccion, texto)

            # 4. PROCESAR ACCIONES DE NEGOCIO
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nuevo_nom = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nuevo_nom, "telefono": numero_str}).execute()
                nombre_cli = nuevo_nom
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["AGENDAR", "PAGO"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto, nombre_cli)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            # 5. RESPUESTA FINAL Y REGISTRO EN MEMORIA
            enviar_meta(numero_str, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero_str, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)