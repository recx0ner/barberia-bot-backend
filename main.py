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

def descargar_imagen_meta(media_id):
    url = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    res = requests.get(url, headers=headers).json()
    image_url = res.get('url')
    if image_url:
        img_res = requests.get(image_url, headers=headers)
        return img_res.content
    return None

# --- IA CON REINTENTOS Y SOPORTE MULTIMODAL ---
def procesar_con_ia(instruccion, contenido_usuario, es_imagen=False):
    llaves = VALID_KEYS[:]
    random.shuffle(llaves)
    for key in llaves:
        try:
            client = genai.Client(api_key=key)
            if es_imagen:
                # Si el usuario mandó foto, la IA la analiza para buscar la referencia
                contenido = [types.Part.from_bytes(data=contenido_usuario, mime_type="image/jpeg"), "Analiza este comprobante y extrae la referencia de pago."]
            else:
                contenido = contenido_usuario

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion},
                contents=contenido
            )
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): continue
            raise e
    return "Estamos bajo mucha demanda, ¡intenta de nuevo en un momento!"

# --- LÓGICA DE NEGOCIO ---
def ejecutar_accion(id_num, accion, texto="", nombre_actual="Cliente"):
    try:
        # Aseguramos existencia del cliente
        supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": str(id_num)}).execute()

        if accion == "AGENDAR":
            # Guardamos con el nombre real para que la celda no quede vacía
            supabase.table('citas').insert({
                "user_id": id_num, "nombre": nombre_actual, "servicio": texto,
                "fecha_hora": datetime.now(venezuela_tz).isoformat(), "estado": "pendiente"
            }).execute()
            return "✅ ¡Pedido agendado! Tienes 20 min para cancelar si lo necesitas."

        elif accion == "CANCELAR":
            res = supabase.table('citas').select('id, fecha_hora').eq('user_id', id_num).eq('estado', 'pendiente').order('fecha_hora', desc=True).limit(1).execute()
            if res.data:
                fecha_cita = datetime.fromisoformat(res.data[0]['fecha_hora'])
                if (datetime.now(venezuela_tz) - fecha_cita).total_seconds() / 60 <= 20:
                    supabase.table('citas').update({"estado": "cancelado"}).eq("id", res.data[0]['id']).execute()
                    return "🗑️ Tu pedido ha sido cancelado con éxito."
                return "⛔ Ya pasaron más de 20 min. Tu pizza ya está en preparación y no se puede cancelar."
            return "❌ No encontré pedidos pendientes para cancelar."

        elif accion == "PAGO":
            ref = ''.join(filter(str.isdigit, texto))
            if len(ref) >= 4:
                supabase.table('pagos').insert({"user_id": id_num, "referencia": ref, "verificado": False}).execute()
                if ADMIN_PHONE:
                    enviar_meta(ADMIN_PHONE, f"🔔 *NUEVO PAGO*\nCliente: {nombre_actual}\nRef: {ref}\n\nResponde: *CONFIRMAR {ref}*")
                return "⏳ Referencia recibida. El administrador la verificará pronto."
            return "⚠️ No pude detectar una referencia válida en el comprobante."

    except Exception as e:
        print(f"🔥 Error DB: {e}")
        return "✅ ¡Entendido! Ya procesé tu solicitud."

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
            
            # 1. Lógica de Admin
            texto_admin = msg.get('text', {}).get('body', "").strip()
            if ADMIN_PHONE and numero == ADMIN_PHONE and texto_admin.upper().startswith("CONFIRMAR"):
                ref_v = texto_admin.split()[-1]
                res_p = supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_v).execute()
                if res_p.data:
                    enviar_meta(str(res_p.data[0]['user_id']), "✅ *¡Pago verificado!* Tu pizza ya está lista. 🍕")
                    return jsonify({"status": "ok"}), 200

            # 2. Identidad y Memoria
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_actual = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"
            
            # 3. Manejo de contenido (Texto o Imagen)
            es_imagen = 'image' in msg
            contenido = descargar_imagen_meta(msg['image']['id']) if es_imagen else texto_admin

            instruccion = f"{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nTags: [ACCION:AGENDAR], [ACCION:NOMBRE:Nombre], [ACCION:PAGO], [ACCION:CANCELAR]"
            respuesta_ia = procesar_con_ia(instruccion, contenido, es_imagen)

            # 4. Tags
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_actual = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nombre_actual, "telefono": numero}).execute()
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["AGENDAR", "PAGO", "CANCELAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_accion(id_num, tag, respuesta_ia if tag == "PAGO" else texto_admin, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero, "user_input": texto_admin if not es_imagen else "Envió imagen", "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)