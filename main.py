import os, random, requests, traceback
from datetime import datetime, timedelta
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE LLAVES Y ENTORNO ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k] # Rotación de 3 llaves

META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # Número para confirmaciones
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# --- FUNCIONES DE COMUNICACIÓN ---
def enviar_meta(to, text):
    # Envío oficial por el número del bot de Meta
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

# --- IA CON REINTENTOS (FAILOVER) ---
def generar_respuesta_ia(instruccion, texto_usuario):
    # Protege contra el error 429 de cuota agotada
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
            if "429" in str(e):
                print(f"⚠️ Llave agotada, probando otra llave de las 3 disponibles...")
                continue
            raise e
    return "Lo sentimos, estamos horneando demasiadas respuestas. Intenta de nuevo en unos minutos."

# --- LÓGICA DE NEGOCIO (CITAS, PAGOS Y CANCELACIÓN LÍMITE) ---
def ejecutar_logica_negocio(id_num, accion, texto="", nombre_actual="Cliente"):
    try:
        # Asegurar que el cliente existe en la tabla 'cliente'
        supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": str(id_num)}).execute()

        if accion == "AGENDAR":
            # Agendado con columnas: user_id, nombre, servicio, fecha_hora, estado
            supabase.table('citas').insert({
                "user_id": id_num, 
                "nombre": nombre_actual,
                "servicio": texto, 
                "fecha_hora": datetime.now(venezuela_tz).isoformat(),
                "estado": "pendiente"
            }).execute()
            return "✅ ¡Pedido agendado! Estamos manos a la masa. Recuerda que tienes 20 min para cancelar si lo necesitas."
        
        elif accion == "CANCELAR":
            # 1. Buscar el último pedido pendiente del cliente
            res = supabase.table('citas').select('id, fecha_hora')\
                .eq('user_id', id_num).eq('estado', 'pendiente')\
                .order('fecha_hora', desc=True).limit(1).execute()
            
            if not res.data:
                return "❌ No tienes ningún pedido pendiente que se pueda cancelar en este momento."
            
            # 2. Verificar límite de 20 minutos [NUEVA FUNCIÓN]
            cita = res.data[0]
            fecha_cita = datetime.fromisoformat(cita['fecha_hora'])
            ahora = datetime.now(venezuela_tz)
            
            # Cálculo de la diferencia en minutos
            diferencia = (ahora - fecha_cita).total_seconds() / 60
            
            if diferencia <= 20:
                supabase.table('citas').update({"estado": "cancelado"}).eq("id", cita['id']).execute()
                return f"🗑️ Tu pedido ha sido cancelado con éxito (han pasado {int(diferencia)} min)."
            else:
                return f"⛔ Lo sentimos, han pasado {int(diferencia)} minutos. Tu pizza ya está en el horno y no se puede cancelar. Debes proceder con el pago."

        elif accion == "PAGO":
            ref = ''.join(filter(str.isdigit, texto))
            if len(ref) < 6: return "⚠️ Referencia inválida, por favor envíala completa."
            
            # Registrar pago para verificación del admin
            supabase.table('pagos').insert({"user_id": id_num, "referencia": ref, "verificado": False}).execute()
            
            if ADMIN_PHONE:
                enviar_meta(ADMIN_PHONE, f"🔔 *PAGO RECIBIDO*\nCliente: {nombre_actual}\nRef: {ref}\n\nResponde: *CONFIRMAR {ref}*")
            return "⏳ Referencia recibida. Te avisaré cuando el administrador la verifique."

    except Exception as e:
        print(f"🔥 Error en DB: {str(e)}")
        return "✅ ¡Entendido! Ya procesé tu solicitud."

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
            id_num = int(numero_str) # int8
            texto = msg.get('text', {}).get('body', "").strip()
            ahora_ve = datetime.now(venezuela_tz).strftime('%Y-%m-%d %H:%M:%S') #

            # 1. LÓGICA DE ADMINISTRADOR
            if ADMIN_PHONE and numero_str == ADMIN_PHONE and texto.upper().startswith("CONFIRMAR"):
                ref_v = texto.split()[-1]
                res_p = supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_v).execute()
                if res_p.data:
                    c_id = res_p.data[0]['user_id']
                    enviar_meta(str(c_id), "✅ *¡Pago verificado!* Tu pedido ya está en el horno. 🍕🔥")
                    return jsonify({"status": "ok"}), 200

            # 2. IDENTIDAD Y MEMORIA
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_cli = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"
            
            res_mem = supabase.table('messages').select('user_input, bot_response')\
                .eq('user_id', numero_str).order('created_at', desc=True).limit(6).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            # 3. PROCESAMIENTO IA
            instruccion = f"""{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\nCliente: {nombre_cli}\nHistorial:\n{historial}
            REGLAS:
            - Prioridad: Si no sabes el nombre, pregúntalo.
            - Si te da el nombre, usa: [ACCION:NOMBRE:Nombre]
            - Si confirma pedido, usa: [ACCION:AGENDAR]
            - Si quiere cancelar, usa: [ACCION:CANCELAR] (Infórmale que tiene solo 20 min).
            - Si envía pago, usa: [ACCION:PAGO]
            """
            
            respuesta_ia = generar_respuesta_ia(instruccion, texto)

            # 4. PROCESAR ACCIONES
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nuevo_nom = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nuevo_nom, "telefono": numero_str}).execute()
                nombre_cli = nuevo_nom
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["AGENDAR", "CANCELAR", "PAGO"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto, nombre_cli)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            # 5. RESPUESTA Y LOG
            enviar_meta(numero_str, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero_str, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)