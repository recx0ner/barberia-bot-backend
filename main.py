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

# Credenciales META
META_TOKEN = os.environ.get("META_TOKEN")
PHONE_ID = os.environ.get("PHONE_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "barberia_token")
ADMIN_NUMBER = os.environ.get("ADMIN_NUMBER") # TU número para aprobar pagos

# Contexto SaaS (Si no hay variable, usa Barbería por defecto)
DEFAULT_CONTEXT = 'Eres el asistente de la "Barberia Estilazo". Tu objetivo es agendar citas.'
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", DEFAULT_CONTEXT)

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

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

Si faltan datos, responde amable y pide lo que falta según tu rol.
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

# --- 3. FUNCIONES AUXILIARES (META API) ---

def enviar_whatsapp(destinatario, texto):
    """Envía mensaje usando la Graph API de Meta"""
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
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        print(f"Error enviando WhatsApp: {e}")

def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    if not supabase: return
    try:
        supabase.table('messages').insert({
            "user_id": str(chat_id), "user_input": user_text, "bot_response": bot_text, "platform": "whatsapp_meta"
        }).execute()
    except: pass

def obtener_historial_texto(chat_id):
    if not supabase: return ""
    try:
        response = supabase.table('messages').select('*').eq('user_id', str(chat_id)).order('created_at', desc=True).limit(5).execute()
        if not response.data: return ""
        historial = ""
        for msg in reversed(response.data):
            val_user = msg.get('user_input') or "[Imagen]"
            historial += f"Usuario: {val_user}\nAsistente: {msg.get('bot_response','')}\n---\n"
        return historial
    except: return ""

def obtener_id_cliente(chat_id):
    try:
        resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
        if resp.data: return resp.data[0]['id']
    except: pass
    return None

# --- 4. GESTIÓN DE CITAS/PEDIDOS ---
def gestionar_reserva(datos, chat_id):
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "General")
        cliente_id = obtener_id_cliente(chat_id)
        
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": nombre, "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']
            
        supabase.table('citas').insert({"cliente_id": cliente_id, "servicio": servicio, "fecha_hora": fecha}).execute()
        return f"✅ ¡Listo {nombre}! Confirmado para el {fecha} ({servicio})."
    except Exception as e: return f"Error reservando: {str(e)}"

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

# --- 5. GESTIÓN DE PAGOS ---
def gestionar_aprobacion_pago(referencia):
    try:
        resp = supabase.table('pagos').select("*").eq('referencia', referencia).execute()
        if not resp.data: return "⚠️ Referencia no encontrada."
        
        pago = resp.data[0]
        cliente_id = pago['cliente_id']
        supabase.table('pagos').update({"estado": "aprobado"}).eq('referencia', referencia).execute()
        
        resp_c = supabase.table('cliente').select("telefono").eq('id', cliente_id).execute()
        if resp_c.data:
            enviar_whatsapp(resp_c.data[0]['telefono'], f"✅ Pago *{referencia}* APROBADO. Tu reserva está firme.")
            
        return f"👍 Pago {referencia} aprobado."
    except Exception as e: return f"Error: {str(e)}"

def procesar_imagen_meta(image_id, chat_id):
    try:
        # 1. Obtener URL de Meta
        url_get = f"https://graph.facebook.com/v17.0/{image_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        resp_url = requests.get(url_get, headers=headers).json()
        media_url = resp_url.get("url")
        
        if not media_url: return "Error obteniendo imagen."
        
        # 2. Descargar Bytes (Requiere Auth Header)
        img_bytes = requests.get(media_url, headers=headers).content
        
        # 3. Gemini Vision
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        model = genai.GenerativeModel('models/gemini-2.5-flash')
        response = model.generate_content([CAJERO_PROMPT, {"mime_type": "image/jpeg", "data": img_bytes}])
        
        data = json.loads(response.text.replace("```json", "").replace("```", "").strip())
        if data.get("error"): return "❌ No parece un comprobante válido."
        
        # 4. Guardar BD
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": "Cliente Pagador", "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']

        supabase.table('pagos').insert({
            "cliente_id": cliente_id, "referencia": data.get("referencia"), "monto": data.get("monto"), "banco": data.get("banco"), "estado": "pendiente"
        }).execute()
        
        # 5. Avisar al JEFE
        if ADMIN_NUMBER:
            msg_admin = f"💰 *NUEVO PAGO*\nRef: {data.get('referencia')}\nMonto: {data.get('monto')}\nBanco: {data.get('banco')}\n\nResponde: /ok {data.get('referencia')}"
            enviar_whatsapp(ADMIN_NUMBER, msg_admin)
        
        return f"⏳ Recibido. Ref: *{data.get('referencia')}*. Esperando aprobación..."
    except Exception as e:
        print(f"Error Vision: {e}")
        return "⚠️ Error procesando imagen."

# --- 6. CEREBRO IA ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "⚠️ Error: Sin API Keys."
    try:
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        historial_str = obtener_historial_texto(chat_id)
        fecha_ve = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
        
        # Prompt con Contexto de Negocio
        prompt_final = SYSTEM_PROMPT_TEMPLATE.format(
            business_context=BUSINESS_CONTEXT, 
            current_date=fecha_ve, 
            chat_history=historial_str
        )
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt_final)
        response = model.generate_content(user_text)
        texto = response.text.strip()

        if "{" in texto and '"action":' in texto:
            try:
                datos = json.loads(texto.replace("```json", "").replace("```", "").strip())
                act = datos.get("action")
                if act == "reservar": return gestionar_reserva(datos, chat_id)
                elif act == "cancelar": return gestionar_cancelacion(datos, chat_id)
                elif act == "reprogramar": return gestionar_reprogramacion(datos, chat_id)
            except: pass
        return texto
    except Exception as e: return f"Error técnico: {str(e)}"

# --- 7. WEBHOOK ---
@app.route('/whatsapp', methods=['GET', 'POST'])
def whatsapp_webhook():
    # VERIFICACION
    if request.method == 'GET':
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token == VERIFY_TOKEN: return challenge, 200
        return "Token inválido", 403

    # MENSAJES
    try:
        data = request.json
        entry = data['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        
        if 'messages' in value:
            message = value['messages'][0]
            sender = message['from']
            msg_type = message['type']
            respuesta_bot = ""

            # CASO A: ADMIN (Tu número)
            if sender == ADMIN_NUMBER and msg_type == 'text':
                body = message['text']['body']
                if body.lower().startswith("/ok"):
                    ref = body.split(" ")[1] if len(body.split(" ")) > 1 else ""
                    respuesta_bot = gestionar_aprobacion_pago(ref)
                else:
                    # Admin hablando normal con el bot
                    respuesta_bot = get_gemini_response(body, sender)

            # CASO B: CLIENTE (Imagen Pago)
            elif msg_type == 'image':
                image_id = message['image']['id']
                respuesta_bot = procesar_imagen_meta(image_id, sender)

            # CASO C: CLIENTE (Texto)
            elif msg_type == 'text':
                body = message['text']['body']
                respuesta_bot = get_gemini_response(body, sender)
                guardar_mensaje_seguro(sender, body, respuesta_bot)

            if respuesta_bot:
                enviar_whatsapp(sender, respuesta_bot)

        return jsonify({"status": "received"}), 200

    except Exception as e:
        return jsonify({"status": "error", "desc": str(e)}), 200

@app.route('/', methods=['GET'])
def home(): 
    return jsonify({"status": "online", "platform": "Meta", "business": BUSINESS_CONTEXT[:30]})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)