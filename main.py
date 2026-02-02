import os, random, requests, traceback, base64
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors, types
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE ENTORNO ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k] # Rotación de 3 llaves
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # Tu número para confirmar pagos
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

# --- COMUNICACIÓN ---
def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

def descargar_media(media_id):
    url = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    res = requests.get(url, headers=headers).json()
    image_url = res.get('url')
    if image_url:
        return requests.get(image_url, headers=headers).content
    return None

# --- IA CON REINTENTOS Y VISIÓN DE COMPROBANTES ---
def procesar_con_ia(instruccion, contenido_usuario, es_imagen=False):
    llaves = VALID_KEYS[:]
    random.shuffle(llaves) # Evitamos el error 429 de cuota agotada
    for key in llaves:
        try:
            client = genai.Client(api_key=key)
            if es_imagen:
                # La IA analiza el comprobante para extraer la referencia
                partes = [types.Part.from_bytes(data=contenido_usuario, mime_type="image/jpeg"), 
                          "Analiza este comprobante de pago móvil y dime SOLAMENTE el número de referencia."]
            else:
                partes = contenido_usuario

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion},
                contents=partes
            )
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): continue
            raise e
    return "Lo siento, estamos horneando demasiadas peticiones. Intenta en un minuto."

# --- LOGICA DE NEGOCIO (CITAS, PAGOS, CANCELAR) ---
def ejecutar_logica(id_num, accion, texto_ia="", nombre_actual="Cliente"):
    try:
        # Aseguramos que el cliente exista en la tabla 'cliente'
        supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": str(id_num)}).execute()

        if accion == "AGENDAR":
            # Extraer el detalle del pedido del texto de la IA si es posible
            detalle_final = texto_ia.replace("[ACCION:AGENDAR]", "").strip()[:200]
            # Insertamos con todas las columnas, asegurando que 'nombre' NO sea NULL
            supabase.table('citas').insert({
                "user_id": id_num, 
                "nombre": nombre_actual, # Corregido: ya no saldrá NULL
                "servicio": detalle_final, 
                "fecha_hora": datetime.now(venezuela_tz).isoformat(),
                "estado": "pendiente"
            }).execute()
            return "✅ ¡Pedido registrado en cocina!"

        elif accion == "PAGO":
            ref = ''.join(filter(str.isdigit, texto_ia))
            if len(ref) >= 4:
                supabase.table('pagos').insert({"user_id": id_num, "referencia": ref, "verificado": False}).execute()
                if ADMIN_PHONE:
                    enviar_meta(ADMIN_PHONE, f"🔔 *NUEVO PAGO*\nCliente: {nombre_actual}\nRef: {ref}\n\nResponde: *CONFIRMAR {ref}*")
                return "⏳ Referencia recibida. Te avisaré cuando el administrador la verifique."
            return "⚠️ No pude detectar una referencia válida."

    except Exception as e:
        print(f"🔥 Error en DB: {e}")
        return "✅ ¡Entendido! Ya procesé tu solicitud."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            
            # --- FILTRO DE FRESCURA (EVITA RESPUESTAS FANTASMA) ---
            if int(datetime.now(pytz.utc).timestamp()) - int(msg.get('timestamp', 0)) > 300:
                return jsonify({"status": "ignored"}), 200

            numero = msg['from']
            id_num = int(numero)
            texto_usuario = msg.get('text', {}).get('body', "").strip()

            # 1. Lógica de Administrador (Confirmar Pago)
            if ADMIN_PHONE and numero == ADMIN_PHONE and texto_usuario.upper().startswith("CONFIRMAR"):
                ref_v = texto_usuario.split()[-1]
                res_p = supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_v).execute()
                if res_p.data:
                    enviar_meta(str(res_p.data[0]['user_id']), "✅ *¡Pago verificado!* Tu pizza ya está lista. 🍕")
                    return jsonify({"status": "ok"}), 200

            # 2. Identidad y Memoria
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_actual = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"
            
            res_mem = supabase.table('messages').select('user_input, bot_response').eq('user_id', numero).order('created_at', desc=True).limit(6).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            # 3. Procesar Contenido
            es_imagen = 'image' in msg
            contenido = descargar_media(msg['image']['id']) if es_imagen else texto_usuario

            # REFUERZO DE INSTRUCCIÓN: Los tags son obligatorios
            instruccion = f"""{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nHistorial:\n{historial}
            REGLAS MANDATORIAS:
            - Si el cliente confirma un pedido o la hora de recogida, DEBES incluir el tag [ACCION:AGENDAR] al final.
            - Si envía una referencia de pago, usa [ACCION:PAGO].
            - Si no sabes su nombre, usa [ACCION:NOMBRE:Nombre].
            """
            respuesta_ia = procesar_con_ia(instruccion, contenido, es_imagen)

            # 4. Ejecutar Acciones
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_actual = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": numero}).execute()
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["AGENDAR", "PAGO"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica(id_num, tag, respuesta_ia, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero, "user_input": texto_usuario if not es_imagen else "Envió imagen", "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)