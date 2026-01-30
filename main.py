import os
import random
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN ---
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

META_TOKEN = os.environ.get("META_TOKEN")
PHONE_ID = os.environ.get("PHONE_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "barberia_token")
ADMIN_NUMBER = os.environ.get("ADMIN_NUMBER")

DEFAULT_CONTEXT = 'Eres el asistente de la "Barberia Estilazo". Tu objetivo es agendar citas.'
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", DEFAULT_CONTEXT)

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Crítico Supabase: {e}")

# --- 2. PROMPTS ---
SYSTEM_PROMPT_TEMPLATE = """
{business_context}
Hoy es: {current_date} (Hora Venezuela).

--- HISTORIAL RECIENTE ---
{chat_history}
--------------------------

REGLA DE ORO:
Si el cliente ya proporcionó NOMBRE, FECHA y HORA para una cita/pedido:
¡NO respondas con texto conversacional!
TU ÚNICA RESPUESTA DEBE SER EL OBJETO JSON.

ACCIONES JSON:
- RESERVAR: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}
- CANCELAR: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}
- REPROGRAMAR: {{"action": "reprogramar", "fecha_hora_vieja": "YYYY-MM-DD HH:MM", "fecha_hora_nueva": "YYYY-MM-DD HH:MM"}}

IMPORTANTE: Responde siempre en español latino y sé amable.
"""

CAJERO_PROMPT = """
Analiza esta imagen. Es un comprobante de pago móvil o transferencia bancaria de Venezuela.
Tu trabajo es extraer ÚNICAMENTE la siguiente información en formato JSON:
{
  "referencia": "El número de referencia o confirmación completo (ej: 0123456789)",
  "monto": "El monto pagado (ej: 150.00 Bs)",
  "banco": "El banco de origen (ej: Banesco, Venezuela, Mercantil)"
}
Si la imagen NO parece un comprobante de pago, responde con JSON: {"error": "no_es_pago"}
"""

# --- 3. FUNCIONES META API ---

def enviar_whatsapp(destinatario, texto):
    try:
        url = f"https://graph.facebook.com/v17.0/{PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": destinatario,
            "type": "text",
            "text": {"body": texto}
        }
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code != 200:
            print(f"❌ Error enviando WhatsApp: {resp.text}")
    except Exception as e:
        print(f"❌ Excepción enviando WhatsApp: {e}")

# --- 4. FUNCIONES DE BASE DE DATOS (AQUÍ ESTABA EL ERROR) ---

def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    if not supabase: return
    try:
        # Imprimimos en consola para depurar en Render
        print(f"💾 Guardando mensaje de {chat_id}...")
        
        data = {
            "user_id": str(chat_id), # Aseguramos que sea string
            "user_input": user_text, 
            "bot_response": bot_text, 
            "platform": "whatsapp_meta"
        }
        
        supabase.table('messages').insert(data).execute()
        print("✅ Mensaje guardado correctamente.")
        
    except Exception as e:
        print(f"🔥 ERROR GUARDANDO EN DB: {e}")

def obtener_historial_texto(chat_id):
    if not supabase: return ""
    try:
        # Buscamos historial convirtiendo el ID a string explícitamente
        response = supabase.table('messages').select('*').eq('user_id', str(chat_id)).order('created_at', desc=True).limit(5).execute()
        if not response.data: return ""
        historial = ""
        for msg in reversed(response.data):
            val_user = msg.get('user_input') or "[Imagen]"
            historial += f"Usuario: {val_user}\nAsistente: {msg.get('bot_response','')}\n---\n"
        return historial
    except Exception as e:
        print(f"⚠️ Error leyendo historial: {e}")
        return ""

def obtener_id_cliente(chat_id):
    try:
        resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
        if resp.data: 
            return resp.data[0]['id']
        else:
            return None
    except Exception as e:
        print(f"⚠️ Error buscando cliente: {e}")
        return None

# --- 5. LÓGICA DE NEGOCIO ---

def gestionar_reserva(datos, chat_id):
    print(f"⚡ Intentando reservar para: {datos}")
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "General")
        cliente_id = obtener_id_cliente(chat_id)
        
        if not cliente_id:
            print("👤 Creando cliente nuevo...")
            nuevo = supabase.table('cliente').insert({"nombre": nombre, "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']
            
        supabase.table('citas').insert({"cliente_id": cliente_id, "servicio": servicio, "fecha_hora": fecha}).execute()
        return f"✅ ¡Listo {nombre}! Reserva confirmada para el {fecha} ({servicio})."
    except Exception as e: 
        print(f"🔥 Error en gestionar_reserva: {e}")
        return "Hubo un error técnico agendando tu cita. Por favor intenta de nuevo."

def gestionar_cancelacion(datos, chat_id):
    try:
        fecha = datos.get("fecha_hora")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No encontrado."
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        cita_id = None
        for c in citas.data:
            if str(c['fecha_hora']).replace("T", " ").startswith(fecha):
                cita_id = c['id']; break
        if cita_id:
            supabase.table('citas').delete().eq('id', cita_id).execute()
            return f"🗑️ Cancelada reserva del {fecha}."
        return "⚠️ No encontré esa reserva."
    except Exception as e: return f"Error: {str(e)}"

def gestionar_reprogramacion(datos, chat_id):
    try:
        f_vieja = datos.get("fecha_hora_vieja")
        f_nueva = datos.get("fecha_hora_nueva")
        cliente_id = obtener_id_cliente(chat_id)
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        cita_id = None
        for c in citas.data:
            if str(c['fecha_hora']).replace("T", " ").startswith(f_vieja):
                cita_id = c['id']; break
        if cita_id:
            supabase.table('citas').update({"fecha_hora": f_nueva}).eq('id', cita_id).execute()
            return f"🔄 Movido al {f_nueva}."
        return "⚠️ No encontré la original."
    except Exception as e: return f"Error: {str(e)}"

# --- 6. PAGOS ---
def gestionar_aprobacion_pago(referencia):
    try:
        resp = supabase.table('pagos').select("*").eq('referencia', referencia).execute()
        if not resp.data: return "⚠️ Referencia no encontrada."
        pago = resp.data[0]
        cliente_id = pago['cliente_id']
        supabase.table('pagos').update({"estado": "aprobado"}).eq('referencia', referencia).execute()
        resp_c = supabase.table('cliente').select("telefono").eq('id', cliente_id).execute()
        if resp_c.data:
            enviar_whatsapp(resp_c.data[0]['telefono'], f"✅ Pago *{referencia}* APROBADO.")
        return f"👍 Pago {referencia} aprobado."
    except Exception as e: return f"Error: {str(e)}"

def procesar_imagen_meta(image_id, chat_id):
    try:
        url_get = f"https://graph.facebook.com/v17.0/{image_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        resp_url = requests.get(url_get, headers=headers).json()
        media_url = resp_url.get("url")
        if not media_url: return "Error obteniendo imagen."
        
        img_bytes = requests.get(media_url, headers=headers).content
        
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        model = genai.GenerativeModel('models/gemini-2.5-flash')
        response = model.generate_content([CAJERO_PROMPT, {"mime_type": "image/jpeg", "data": img_bytes}])
        
        data = json.loads(response.text.replace("```json", "").replace("```", "").strip())
        if data.get("error"): return "❌ No parece un comprobante válido."
        
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": "Cliente Pagador", "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']

        supabase.table('pagos').insert({
            "cliente_id": cliente_id, "referencia": data.get("referencia"), "monto": data.get("monto"), "banco": data.get("banco"), "estado": "pendiente"
        }).execute()
        
        if ADMIN_NUMBER:
            msg_admin = f"💰 *NUEVO PAGO*\nRef: {data.get('referencia')}\nMonto: {data.get('monto')}\n\nResponde: /ok {data.get('referencia')}"
            enviar_whatsapp(ADMIN_NUMBER, msg_admin)
        
        return f"⏳ Recibido. Ref: *{data.get('referencia')}*. Esperando aprobación..."
    except Exception as e:
        print(f"Error Vision: {e}")
        return "⚠️ Error procesando imagen."

# --- 7. CEREBRO IA ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "⚠️ Error: Sin API Keys."
    try:
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        historial_str = obtener_historial_texto(chat_id)
        fecha_ve = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
        
        prompt_final = SYSTEM_PROMPT_TEMPLATE.format(
            business_context=BUSINESS_CONTEXT, 
            current_date=fecha_ve, 
            chat_history=historial_str
        )
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt_final)
        response = model.generate_content(user_text)
        texto = response.text.strip()
        print(f"🤖 Gemini dice: {texto}")

        # Intentar ejecutar acción JSON
        if "{" in texto and '"action":' in texto:
            try:
                clean_json = texto.replace("```json", "").replace("```", "").strip()
                # A veces gemini pone texto antes del json, buscamos el primer { y ultimo }
                start = clean_json.find('{')
                end = clean_json.rfind('}') + 1
                if start != -1 and end != -1:
                    clean_json = clean_json[start:end]
                
                datos = json.loads(clean_json)
                act = datos.get("action")
                if act == "reservar": return gestionar_reserva(datos, chat_id)
                elif act == "cancelar": return gestionar_cancelacion(datos, chat_id)
                elif act == "reprogramar": return gestionar_reprogramacion(datos, chat_id)
            except Exception as e:
                print(f"⚠️ Error procesando JSON: {e}")
        
        return texto
    except Exception as e: return f"Error técnico: {str(e)}"

# --- 8. WEBHOOK ---
@app.route('/whatsapp', methods=['GET', 'POST'])
def whatsapp_webhook():
    if request.method == 'GET':
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token == VERIFY_TOKEN: return challenge, 200
        return "Token inválido", 403

    try:
        data = request.json
        # Filtrar solo mensajes (Ignorar estados como 'read', 'delivered')
        if 'entry' in data and len(data['entry']) > 0:
            changes = data['entry'][0]['changes'][0]
            if 'value' in changes and 'messages' in changes['value']:
                message = changes['value']['messages'][0]
                sender = message['from']
                msg_type = message['type']
                
                print(f"📩 Mensaje recibido de {sender} tipo {msg_type}")
                respuesta_bot = ""

                # CASO A: ADMIN (/ok)
                if sender == ADMIN_NUMBER and msg_type == 'text':
                    body = message['text']['body']
                    if body.lower().startswith("/ok"):
                        ref = body.split(" ")[1] if len(body.split(" ")) > 1 else ""
                        respuesta_bot = gestionar_aprobacion_pago(ref)
                    else:
                        respuesta_bot = get_gemini_response(body, sender)

                # CASO B: IMAGEN
                elif msg_type == 'image':
                    image_id = message['image']['id']
                    respuesta_bot = procesar_imagen_meta(image_id, sender)

                # CASO C: TEXTO USUARIO
                elif msg_type == 'text':
                    body = message['text']['body']
                    respuesta_bot = get_gemini_response(body, sender)
                    # Guardamos aquí y si falla, lo veremos en los logs
                    guardar_mensaje_seguro(sender, body, respuesta_bot)

                if respuesta_bot:
                    enviar_whatsapp(sender, respuesta_bot)

        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"🔥 Error en Webhook: {e}")
        return jsonify({"status": "error", "desc": str(e)}), 200

@app.route('/', methods=['GET'])
def home(): 
    return jsonify({"status": "online", "platform": "Meta Official"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)