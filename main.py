import os, random, requests, traceback, base64
from datetime import datetime, timedelta
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors, types
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") #
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

# --- FUNCIONES DE COMUNICACIÓN ---
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
    if image_url: return requests.get(image_url, headers=headers).content
    return None

# --- IA CON REINTENTOS (ANTI-BLOQUEO 429) ---
def procesar_con_ia(instruccion, contenido_usuario, es_imagen=False):
    llaves = VALID_KEYS[:]
    random.shuffle(llaves) #
    for key in llaves:
        try:
            client = genai.Client(api_key=key)
            if es_imagen:
                partes = [types.Part.from_bytes(data=contenido_usuario, mime_type="image/jpeg"), 
                          "Extrae la referencia de este pago móvil."]
            else:
                partes = contenido_usuario

            response = client.models.generate_content(model='gemini-2.5-flash', config={'system_instruction': instruccion}, contents=partes)
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): continue
            raise e
    return "Sistemas saturados, intenta en un momento."

# --- LÓGICA DE NEGOCIO CORREGIDA ---
def ejecutar_logica_blindada(id_num, accion, texto_cliente="", nombre_actual="Cliente"):
    try:
        # Asegurar cliente
        supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": str(id_num)}).execute()

        if accion == "AGENDAR":
            # Guardamos el pedido REAL del cliente, no lo que dice el bot
            supabase.table('citas').insert({
                "user_id": id_num, 
                "nombre": nombre_actual, # Ya no será NULL
                "servicio": texto_cliente[:200], 
                "fecha_hora": datetime.now(venezuela_tz).isoformat(),
                "estado": "pendiente"
            }).execute()
            return "✅ ¡Pedido agendado en cocina!"

        elif accion == "CANCELAR":
            # Buscamos la última cita PENDIENTE real
            res = supabase.table('citas').select('id, fecha_hora').eq('user_id', id_num).eq('estado', 'pendiente').order('fecha_hora', desc=True).limit(1).execute()
            
            if res.data:
                fecha_cita = datetime.fromisoformat(res.data[0]['fecha_hora'])
                minutos_pasados = (datetime.now(venezuela_tz) - fecha_cita).total_seconds() / 60
                
                if minutos_pasados <= 20: # Límite de 20 min solicitado
                    supabase.table('citas').update({"estado": "cancelado"}).eq("id", res.data[0]['id']).execute()
                    return f"🗑️ Pedido cancelado con éxito (hace {int(minutos_pasados)} min)."
                return f"⛔ No se puede cancelar: han pasado {int(minutos_pasados)} min y la pizza ya está en el horno."
            return "❌ No tienes pedidos pendientes para cancelar."

        elif accion == "PAGO":
            ref = ''.join(filter(str.isdigit, texto_cliente))
            if len(ref) >= 4:
                supabase.table('pagos').insert({"user_id": id_num, "referencia": ref, "verificado": False}).execute()
                if ADMIN_PHONE:
                    enviar_meta(ADMIN_PHONE, f"🔔 *NUEVO PAGO*\nCliente: {nombre_actual}\nRef: {ref}\n\nResponde: *CONFIRMAR {ref}*")
                return "⏳ Referencia recibida. El administrador verificará el pago."
            return "⚠️ Referencia no detectada."

    except Exception as e:
        print(f"🔥 Error DB: {e}")
        return "✅ Procesado."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            
            # Filtro de frescura
            if int(datetime.now(pytz.utc).timestamp()) - int(msg.get('timestamp', 0)) > 300:
                return jsonify({"status": "old"}), 200

            numero = msg['from']
            id_num = int(numero)
            texto_usuario = msg.get('text', {}).get('body', "").strip()

            # Lógica de Admin (Confirmar)
            if ADMIN_PHONE and numero == ADMIN_PHONE and texto_usuario.upper().startswith("CONFIRMAR"):
                ref_v = texto_usuario.split()[-1]
                res_p = supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_v).execute()
                if res_p.data:
                    enviar_meta(str(res_p.data[0]['user_id']), "✅ *¡Pago verificado!* Tu pizza está lista. 🍕")
                    return jsonify({"status": "ok"}), 200

            # Identidad y Memoria
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_actual = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"
            res_mem = supabase.table('messages').select('user_input, bot_response').eq('user_id', numero).order('created_at', desc=True).limit(6).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            es_imagen = 'image' in msg
            contenido = descargar_media(msg['image']['id']) if es_imagen else texto_usuario

            instruccion = f"""{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nHistorial:\n{historial}
            REGLAS:
            - Solo usa UN tag por respuesta. Prioridad: CANCELAR > PAGO > AGENDAR.
            - Si el cliente quiere cancelar, usa SOLO [ACCION:CANCELAR].
            - Si confirma pedido, usa SOLO [ACCION:AGENDAR].
            """
            respuesta_ia = procesar_con_ia(instruccion, contenido, es_imagen)

            # --- JERARQUÍA DE ACCIONES (Evita el error de image_fc50d2.png) ---
            accion_realizada = False
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_actual = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": numero}).execute()
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["CANCELAR", "PAGO", "AGENDAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia and not accion_realizada:
                    # Pasamos texto_usuario para que el servicio sea la pizza y no el texto del bot
                    feedback = ejecutar_logica_blindada(id_num, tag, texto_usuario, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"
                    accion_realizada = True

            enviar_meta(numero, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero, "user_input": texto_usuario if not es_imagen else "Imagen", "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)