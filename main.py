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
CRON_SECRET = os.environ.get("CRON_SECRET", "mi_clave_secreta")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID") # Tu ID de Telegram para aprobar pagos

# --- 2. PERSONALIDAD DEL NEGOCIO (SaaS) ---
# Si cambias esta variable en Render, el bot cambia de oficio mágicamente.
DEFAULT_CONTEXT = 'Eres el asistente de la "Barberia Estilazo". Tu objetivo es agendar citas para cortes de cabello.'
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", DEFAULT_CONTEXT)

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- 3. PROMPTS DINÁMICOS ---
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

# --- 4. FUNCIONES AUXILIARES (TELEGRAM) ---

def enviar_telegram(chat_id, texto, parse_mode=None):
    try:
        payload = {"chat_id": chat_id, "text": texto}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload)
    except Exception as e:
        print(f"Error enviando Telegram: {e}")

def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    if not supabase: return
    try:
        supabase.table('messages').insert({
            "user_id": str(chat_id), "user_input": user_text, "bot_response": bot_text, "platform": "telegram"
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

# --- 5. GESTIÓN DE AGENDAMIENTO (GENÉRICO) ---
def gestionar_reserva(datos, chat_id):
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "Servicio General")
        cliente_id = obtener_id_cliente(chat_id)
        
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": nombre, "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']
            
        supabase.table('citas').insert({"cliente_id": cliente_id, "servicio": servicio, "fecha_hora": fecha}).execute()
        return f"✅ ¡Listo {nombre}! Agendado para el {fecha} ({servicio})."
    except Exception as e: return f"Error reservando: {str(e)}"

def gestionar_cancelacion(datos, chat_id):
    try:
        fecha = datos.get("fecha_hora")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No encontré tu usuario."
        
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        cita_id = None
        for c in citas.data:
            fecha_db = str(c['fecha_hora']).replace("T", " ")
            if fecha_db.startswith(fecha):
                cita_id = c['id']
                break
        
        if cita_id:
            supabase.table('citas').delete().eq('id', cita_id).execute()
            return f"🗑️ Reserva del {fecha} cancelada."
        return "⚠️ No encontré esa reserva exacta."
    except Exception as e: return f"Error cancelando: {str(e)}"

def gestionar_reprogramacion(datos, chat_id):
    try:
        f_vieja = datos.get("fecha_hora_vieja")
        f_nueva = datos.get("fecha_hora_nueva")
        cliente_id = obtener_id_cliente(chat_id)
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        cita_id = None
        for c in citas.data:
            fecha_db = str(c['fecha_hora']).replace("T", " ")
            if fecha_db.startswith(f_vieja):
                cita_id = c['id']
                break
        if cita_id:
            supabase.table('citas').update({"fecha_hora": f_nueva}).eq('id', cita_id).execute()
            return f"🔄 Movido al {f_nueva}."
        return "⚠️ No encontré la reserva original."
    except Exception as e: return f"Error reprogramando: {str(e)}"

# --- 6. GESTIÓN DE PAGOS Y VISIÓN (TELEGRAM) ---
def gestionar_aprobacion_pago(referencia):
    try:
        resp = supabase.table('pagos').select("*").eq('referencia', referencia).execute()
        if not resp.data: return "⚠️ No encontré esa referencia."
        
        pago = resp.data[0]
        cliente_id = pago['cliente_id']
        supabase.table('pagos').update({"estado": "aprobado"}).eq('referencia', referencia).execute()
        
        # Notificar Cliente
        resp_cliente = supabase.table('cliente').select("telefono, nombre").eq('id', cliente_id).execute()
        if resp_cliente.data:
            cliente = resp_cliente.data[0]
            chat_id_cliente = cliente['telefono']
            enviar_telegram(chat_id_cliente, f"✅ ¡Pago *{referencia}* aprobado! Tu cita está 100% confirmada.", parse_mode="Markdown")
            
        return f"👍 Pago {referencia} aprobado."
    except Exception as e: return f"Error: {str(e)}"

def procesar_imagen_telegram(file_id, chat_id):
    try:
        # 1. Obtener URL de Telegram
        url_info = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        resp_info = requests.get(url_info).json()
        if not resp_info.get("ok"): return "Error obteniendo imagen."
        
        file_path = resp_info["result"]["file_path"]
        url_descarga = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        
        # 2. Descargar
        img_bytes = requests.get(url_descarga).content
        
        # 3. Gemini Vision
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        model = genai.GenerativeModel('models/gemini-2.5-flash')
        response = model.generate_content([CAJERO_PROMPT, {"mime_type": "image/jpeg", "data": img_bytes}])
        
        datos = json.loads(response.text.replace("```json", "").replace("```", "").strip())
        
        if datos.get("error"): return "❌ No parece un comprobante válido."
            
        referencia = datos.get("referencia", "Desconocida")
        monto = datos.get("monto", "N/A")
        banco = datos.get("banco", "Desconocido")
        
        # 4. Guardar BD
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": "Cliente Pagador", "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']

        supabase.table('pagos').insert({
            "cliente_id": cliente_id, "referencia": referencia, "monto": monto, "banco": banco, "estado": "pendiente"
        }).execute()
        
        # 5. Avisar al JEFE (ADMIN_ID)
        if ADMIN_ID:
            msg_admin = f"💰 *NUEVO PAGO*\nBanco: {banco}\nMonto: {monto}\nRef: `{referencia}`\n\nResponde: /ok {referencia}"
            enviar_telegram(ADMIN_ID, msg_admin, parse_mode="Markdown")
        
        return f"⏳ Recibido. Ref: *{referencia}*. Esperando aprobación..."
    except Exception as e:
        print(f"Error Vision: {e}")
        return "⚠️ Error leyendo imagen."

# --- 7. CEREBRO IA ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "⚠️ Error: Sin API Keys."
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        historial_str = obtener_historial_texto(chat_id)
        fecha_dt = datetime.now() - timedelta(hours=4)
        fecha_hoy = fecha_dt.strftime("%Y-%m-%d %H:%M")
        
        # INYECCIÓN DEL CONTEXTO DEL NEGOCIO
        prompt_final = SYSTEM_PROMPT_TEMPLATE.format(
            business_context=BUSINESS_CONTEXT, 
            current_date=fecha_hoy, 
            chat_history=historial_str
        )
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt_final)
        response = model.generate_content(user_text)
        texto = response.text.strip()

        if "{" in texto and '"action":' in texto:
            try:
                datos = json.loads(texto.replace("```json", "").replace("```", "").strip())
                accion = datos.get("action")
                if accion == "reservar": return gestionar_reserva(datos, chat_id)
                elif accion == "cancelar": return gestionar_cancelacion(datos, chat_id)
                elif accion == "reprogramar": return gestionar_reprogramacion(datos, chat_id)
            except: pass
        return texto
    except Exception as e: return f"Error técnico: {str(e)}"

# --- 8. WEBHOOK TELEGRAM ---
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data:
            chat_id = str(data["message"]["chat"]["id"])
            
            # A. FOTO (Pago)
            if "photo" in data["message"]:
                file_id = data["message"]["photo"][-1]["file_id"]
                resp = procesar_imagen_telegram(file_id, chat_id)
                enviar_telegram(chat_id, resp, parse_mode="Markdown")

            # B. TEXTO (Chat o Comandos)
            elif "text" in data["message"]:
                user_text = data["message"]["text"]
                
                # COMANDO JEFE (/ok)
                if user_text.startswith("/ok ") and str(chat_id) == str(ADMIN_ID):
                    referencia = user_text.split(" ")[1]
                    resp = gestionar_aprobacion_pago(referencia)
                    enviar_telegram(chat_id, resp)
                
                # CHAT NORMAL
                else:
                    resp = get_gemini_response(user_text, chat_id)
                    guardar_mensaje_seguro(chat_id, user_text, resp)
                    enviar_telegram(chat_id, resp)

        return jsonify({"status": "sent"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/recordatorios', methods=['GET'])
def enviar_recordatorios():
    secret = request.args.get('key')
    if secret != CRON_SECRET: return jsonify({"error": "Clave incorrecta"}), 403
    if not supabase: return jsonify({"error": "Sin conexión a DB"}), 500

    try:
        hoy = datetime.now() - timedelta(hours=4)
        manana = hoy + timedelta(days=1)
        fecha_inicio = manana.strftime("%Y-%m-%d 00:00:00")
        fecha_fin = manana.strftime("%Y-%m-%d 23:59:59")
        
        response = supabase.table('citas').select("*").gte('fecha_hora', fecha_inicio).lte('fecha_hora', fecha_fin).execute()
        citas = response.data

        if not citas: return jsonify({"status": "Sin citas"}), 200

        enviados = 0
        resumen_admin = f"📅 *AGENDA {manana.strftime('%Y-%m-%d')}*\n"

        for cita in citas:
            try:
                cliente_resp = supabase.table('cliente').select("nombre, telefono").eq('id', cita['cliente_id']).execute()
                if cliente_resp.data:
                    cliente = cliente_resp.data[0]
                    chat_id = cliente.get('telefono')
                    nombre = cliente.get('nombre')
                    hora = str(cita['fecha_hora']).replace("T", " ").split(" ")[1][:5]
                    
                    resumen_admin += f"⏰ {hora} - {nombre}\n"
                    
                    if chat_id:
                        enviar_telegram(chat_id, f"👋 Hola {nombre}, recuerda tu cita mañana a las {hora}.")
                        enviados += 1
            except: pass
        
        if ADMIN_ID:
            enviar_telegram(ADMIN_ID, resumen_admin)

        return jsonify({"status": "Enviado", "cantidad": enviados}), 200

    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/', methods=['GET'])
def home(): 
    return jsonify({"status": "online", "business": BUSINESS_CONTEXT[:50] + "..."})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)