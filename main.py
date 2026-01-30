import os
import random
import json
import requests
import traceback
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- CONFIGURACIÓN ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
META_TOKEN = os.environ.get("META_TOKEN")
PHONE_ID = os.environ.get("PHONE_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "barberia_token")
ADMIN_NUMBER = os.environ.get("ADMIN_NUMBER")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres un asistente.")

supabase = None
try:
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase Conectado.")
except: print("🔥 Error Supabase")

# --- PROMPTS (MÁS ESTRICTO) ---
SYSTEM_PROMPT = f"""
{BUSINESS_CONTEXT}
Hoy es: {datetime.now().strftime('%Y-%m-%d')}

--- REGLAS ABSOLUTAS DE COMPORTAMIENTO ---
1. Si el usuario quiere RESERVAR y da los datos (Nombre, Fecha, Hora):
   RESPONDE SOLO ESTE JSON: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}

2. Si el usuario quiere CANCELAR una cita/pedido existente:
   ¡NO HABLES! ¡NO ACTÚES EL PERSONAJE!
   RESPONDE SOLO ESTE JSON: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}

3. Si el usuario quiere REPROGRAMAR (Cambiar hora):
   RESPONDE SOLO ESTE JSON: {{"action": "reprogramar", "fecha_hora_vieja": "YYYY-MM-DD HH:MM", "fecha_hora_nueva": "YYYY-MM-DD HH:MM"}}

Si falta información, pregunta amablemente usando tu personalidad. Pero si tienes la acción clara, SOLO JSON.
"""

CAJERO_PROMPT = """
Analiza este comprobante de pago móvil de Venezuela.
Extrae ÚNICAMENTE JSON:
{
  "referencia": "Solo los últimos 4-6 dígitos o el número completo visible",
  "monto": "El monto exacto",
  "banco": "Banco de origen"
}
Si no es pago, responde: {"error": "no_es_pago"}
"""

# --- FUNCIONES DE BASE DE DATOS MEJORADAS ---

def obtener_id_cliente(chat_id):
    try:
        r = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
        if r.data: return r.data[0]['id']
    except: pass
    return None

def gestionar_reserva(datos, chat_id):
    print(f"⚡ ACCIÓN DETECTADA: RESERVAR {datos}")
    try:
        nombre, fecha = datos.get("nombre"), datos.get("fecha_hora")
        cid = obtener_id_cliente(chat_id)
        if not cid:
            n = supabase.table('cliente').insert({"nombre": nombre, "telefono": str(chat_id)}).execute()
            cid = n.data[0]['id']
        
        # Guardamos estado 'confirmado' por defecto
        supabase.table('citas').insert({
            "cliente_id": cid, 
            "servicio": datos.get("servicio"), 
            "fecha_hora": fecha,
            "estado": "confirmado"
        }).execute()
        
        return f"✅ Reserva confirmada para {nombre} el {fecha}."
    except Exception as e: 
        print(f"Error Reserva: {e}")
        return "Error técnico agendando."

def gestionar_cancelacion(datos, chat_id):
    print(f"⚡ ACCIÓN DETECTADA: CANCELAR {datos}")
    try:
        # Formato esperado YYYY-MM-DD HH:MM
        fecha_input = datos.get("fecha_hora")
        
        cid = obtener_id_cliente(chat_id)
        if not cid: return "⚠️ No encontré tu usuario en el sistema."

        # Buscamos citas confirmadas de este cliente
        citas = supabase.table('citas').select("*").eq('cliente_id', cid).eq('estado', 'confirmado').execute()
        
        cita_a_cancelar = None
        
        # Lógica de búsqueda flexible de fecha
        for c in citas.data:
            # Convertimos la fecha de la BD a string limpio "YYYY-MM-DD HH:MM"
            fecha_db = str(c['fecha_hora']).replace("T", " ")[:16] # Tomamos solo hasta el minuto
            
            if fecha_input in fecha_db: # Si "2026-01-30 15:30" está dentro del string de la DB
                cita_a_cancelar = c['id']
                break
        
        if cita_a_cancelar:
            # EN LUGAR DE BORRAR, ACTUALIZAMOS A 'cancelado' (Mejor práctica)
            supabase.table('citas').update({"estado": "cancelado"}).eq('id', cita_a_cancelar).execute()
            print(f"✅ Cita ID {cita_a_cancelar} cancelada en BD")
            return f"🚫 Listo. He cancelado tu reserva del {fecha_input}."
        
        print("⚠️ No se encontró coincidencia de fecha.")
        return "⚠️ No encontré una reserva activa en esa fecha exacta. Por favor verifica la hora."

    except Exception as e: 
        print(f"Error Cancelando: {e}")
        return f"Error técnico: {str(e)}"

# --- RESTO DE FUNCIONES (Meta, Vision, etc) ---

def enviar_whatsapp(destinatario, texto):
    try:
        url = f"https://graph.facebook.com/v17.0/{PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": destinatario, "type": "text", "text": {"body": texto}}
        requests.post(url, headers=headers, json=data)
    except: pass

def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    if not supabase: return
    try:
        supabase.table('messages').insert({
            "user_id": str(chat_id), "user_input": user_text, "bot_response": bot_text, "platform": "whatsapp"
        }).execute()
    except: pass

def obtener_historial_texto(chat_id):
    if not supabase: return ""
    try:
        res = supabase.table('messages').select('*').eq('user_id', str(chat_id)).order('created_at', desc=True).limit(5).execute()
        hist = ""
        for m in reversed(res.data): hist += f"U: {m.get('user_input')}\nA: {m.get('bot_response')}\n"
        return hist
    except: return ""

def gestionar_aprobacion_pago(referencia):
    try:
        r = supabase.table('pagos').select("*").eq('referencia', referencia).execute()
        if not r.data: return "⚠️ No encontré esa referencia."
        supabase.table('pagos').update({"estado": "aprobado"}).eq('referencia', referencia).execute()
        cid = r.data[0]['cliente_id']
        c = supabase.table('cliente').select("telefono").eq('id', cid).execute()
        if c.data: enviar_whatsapp(c.data[0]['telefono'], f"✅ Pago *{referencia}* CONFIRMADO.")
        return f"👍 Aprobado {referencia}"
    except: return "Error aprobando"

def procesar_imagen_meta(image_id, chat_id):
    try:
        url_get = f"https://graph.facebook.com/v17.0/{image_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        resp_url = requests.get(url_get, headers=headers).json()
        media_url = resp_url.get("url")
        if not media_url: return "Error imagen."
        img_bytes = requests.get(media_url, headers=headers).content
        
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        model = genai.GenerativeModel('models/gemini-2.5-flash')
        response = model.generate_content([CAJERO_PROMPT, {"mime_type": "image/jpeg", "data": img_bytes}])
        
        clean = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean[clean.find('{'):clean.rfind('}')+1])
        if data.get("error"): return "❌ No es un pago válido."

        cid = obtener_id_cliente(chat_id)
        if not cid:
            n = supabase.table('cliente').insert({"nombre": "Cliente Pago", "telefono": str(chat_id)}).execute()
            cid = n.data[0]['id']

        supabase.table('pagos').insert({
            "cliente_id": cid, "referencia": data.get("referencia"), "monto": data.get("monto"), "banco": data.get("banco"), "estado": "pendiente"
        }).execute()
        
        if ADMIN_NUMBER:
            enviar_whatsapp(ADMIN_NUMBER, f"💰 *PAGO NUEVO*\nRef: {data.get('referencia')}\nMonto: {data.get('monto')}\n/ok {data.get('referencia')}")

        return f"🔍 Recibido Ref: *{data.get('referencia')}*. Verificando..."
    except: return "⚠️ Error procesando imagen."

def get_gemini_response(user_text, chat_id):
    try:
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        # AÑADIMOS EL DÍA ACTUAL AL PROMPT PARA QUE SEPA CUÁNDO ES "HOY" O "MAÑANA"
        fecha_actual = datetime.now().strftime('%Y-%m-%d %H:%M')
        prompt = SYSTEM_PROMPT.replace("{current_date}", fecha_actual) + f"\nHISTORIAL:\n{obtener_historial_texto(chat_id)}"
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt)
        resp = model.generate_content(user_text)
        txt = resp.text.strip()
        
        print(f"🤖 RAW GEMINI: {txt}") # LOG PARA VER SI ALUCINA

        if 'action":' in txt:
            try:
                clean = txt.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean[clean.find('{'):clean.rfind('}')+1])
                act = data.get("action")
                
                if act == "reservar": return gestionar_reserva(data, chat_id)
                elif act == "cancelar": return gestionar_cancelacion(data, chat_id)
                # elif act == "reprogramar": return gestionar_reprogramacion(data, chat_id)
            except Exception as e:
                print(f"Error JSON: {e}")
        return txt
    except Exception as e: return f"Error IA: {e}"

# --- WEBHOOK ---
@app.route('/whatsapp', methods=['GET', 'POST'])
def whatsapp_webhook():
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN: return request.args.get('hub.challenge')
        return "Error", 403

    try:
        data = request.json
        if 'entry' in data:
            for entry in data['entry']:
                for change in entry['changes']:
                    if 'messages' in change['value']:
                        msg = change['value']['messages'][0]
                        sender = msg['from']
                        msg_type = msg['type']
                        
                        resp = ""
                        if msg_type == 'text':
                            body = msg['text']['body']
                            # COMANDO ADMIN
                            if sender == ADMIN_NUMBER and body.startswith("/ok"):
                                resp = gestionar_aprobacion_pago(body.split(" ")[1])
                            else:
                                resp = get_gemini_response(body, sender)
                                guardar_mensaje_seguro(sender, body, resp)
                        
                        elif msg_type == 'image':
                            resp = procesar_imagen_meta(msg['image']['id'], sender)

                        if resp: enviar_whatsapp(sender, resp)

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        traceback.print_exc()
        return "Error", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))