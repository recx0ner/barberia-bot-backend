import os
import random
import json
from datetime import datetime, timedelta
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. PROMPT (CEREBRO) ---
BARBER_PROMPT = """
Eres el asistente de la "Barbería El Corte Arrechísimo". Hoy es: {current_date}.
--- HISTORIAL ---
{chat_history}
-----------------
TU OBJETIVO: Responder usando el historial.
ACCIONES JSON:
- RESERVAR: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}
- CANCELAR: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}
- REPROGRAMAR: {{"action": "reprogramar", "fecha_hora_vieja": "YYYY-MM-DD HH:MM", "fecha_hora_nueva": "YYYY-MM-DD HH:MM"}}
"""

# --- 2. CONFIGURACIÓN ---
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CRON_SECRET = os.environ.get("CRON_SECRET", "mi_clave_secreta_barberia")
ADMIN_ID = os.environ.get("ADMIN_ID") # <--- NUEVA VARIABLE DEL JEFE

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- 3. FUNCIONES DE AYUDA ---
def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    if not supabase: return
    try:
        supabase.table('messages').insert({
            "user_id": str(chat_id), "user_input": user_text, "bot_response": bot_text, "platform": "telegram"
        }).execute()
    except: pass

def obtener_historial_texto(chat_id):
    if not supabase: return "Sin historial."
    try:
        response = supabase.table('messages').select('*').eq('user_id', str(chat_id)).order('created_at', desc=True).limit(5).execute()
        if not response.data: return "Sin mensajes previos."
        historial = ""
        for msg in reversed(response.data):
            historial += f"Cliente: {msg.get('user_input','')}\nBarbero: {msg.get('bot_response','')}\n---\n"
        return historial
    except: return "Error historial."

def obtener_id_cliente(chat_id):
    try:
        resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
        if resp.data: return resp.data[0]['id']
    except: pass
    return None

def encontrar_cita_flexible(cliente_id, fecha_str):
    try:
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        for c in citas.data:
            if c['fecha_hora'][:16].replace("T", " ") == fecha_str: return c['id']
    except: pass
    return None

def gestionar_reserva(datos, chat_id):
    try:
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": datos.get("nombre"), "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']
        supabase.table('citas').insert({"cliente_id": cliente_id, "servicio": datos.get("servicio", "Corte"), "fecha_hora": datos.get("fecha_hora")}).execute()
        return f"✅ Listo {datos.get('nombre')}, cita agendada para el {datos.get('fecha_hora')}."
    except Exception as e: return f"Error: {e}"

def gestionar_cancelacion(datos, chat_id):
    try:
        cliente_id = obtener_id_cliente(chat_id)
        cita_id = encontrar_cita_flexible(cliente_id, datos.get("fecha_hora"))
        if cita_id:
            supabase.table('citas').delete().eq('id', cita_id).execute()
            return "🗑️ Cita cancelada."
        return "⚠️ No encontré esa cita."
    except: return "Error cancelando."

def gestionar_reprogramacion(datos, chat_id):
    try:
        cliente_id = obtener_id_cliente(chat_id)
        cita_id = encontrar_cita_flexible(cliente_id, datos.get("fecha_hora_vieja"))
        if cita_id:
            supabase.table('citas').update({"fecha_hora": datos.get("fecha_hora_nueva")}).eq('id', cita_id).execute()
            return f"🔄 Cita movida al {datos.get('fecha_hora_nueva')}."
        return "⚠️ No encontré la cita original."
    except: return "Error reprogramando."

# --- 4. CEREBRO IA ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "Error: Sin API Keys."
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        historial = obtener_historial_texto(chat_id)
        fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = BARBER_PROMPT.format(current_date=fecha_hoy, chat_history=historial)
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt)
        resp = model.generate_content(user_text)
        texto = resp.text.strip()

        if "{" in texto and '"action":' in texto:
            try:
                datos = json.loads(texto.replace("```json", "").replace("```", "").strip())
                acc = datos.get("action")
                if acc == "reservar": return gestionar_reserva(datos, chat_id)
                elif acc == "cancelar": return gestionar_cancelacion(datos, chat_id)
                elif acc == "reprogramar": return gestionar_reprogramacion(datos, chat_id)
            except: pass
        return texto
    except Exception as e: return f"Error: {e}"

# --- 5. RUTA MAESTRA: RECORDATORIOS + REPORTE ADMIN ---
@app.route('/recordatorios', methods=['GET'])
def enviar_recordatorios():
    secret = request.args.get('key')
    if secret != CRON_SECRET:
        return jsonify({"error": "Acceso denegado"}), 403

    try:
        # Calcular MAÑANA
        hoy = datetime.now()
        manana = hoy + timedelta(days=1)
        fecha_busqueda = manana.strftime("%Y-%m-%d")

        # Buscar citas
        response_citas = supabase.table('citas').select("*").ilike('fecha_hora', f"{fecha_busqueda}%").execute()
        citas = response_citas.data

        if not citas:
            # Avisar al admin que tiene el día libre
            if ADMIN_ID:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                    "chat_id": ADMIN_ID,
                    "text": f"🏖️ ¡Jefe! No hay citas programadas para mañana ({fecha_busqueda}). Tómate un descanso.",
                    "parse_mode": "Markdown"
                })
            return jsonify({"status": "Día libre", "fecha": fecha_busqueda})

        # Preparamos el REPORTE PARA EL JEFE
        resumen_admin = f"📅 *REPORTE PARA MAÑANA ({fecha_busqueda})*\n"
        resumen_admin += f"📊 Total Citas: {len(citas)}\n\n"
        resumen_admin += "📋 *Agenda del día:*\n"

        enviados_clientes = 0

        # Ordenamos las citas por hora para que el reporte salga ordenado
        citas_ordenadas = sorted(citas, key=lambda x: x['fecha_hora'])

        for cita in citas_ordenadas:
            try:
                cliente_id = cita['cliente_id']
                hora_cita = cita['fecha_hora'].split(" ")[1]
                servicio = cita.get('servicio', 'Cita')

                # Datos del cliente
                resp_cliente = supabase.table('cliente').select("nombre, telefono").eq('id', cliente_id).execute()
                
                if resp_cliente.data:
                    cliente = resp_cliente.data[0]
                    nombre = cliente['nombre']
                    chat_id = cliente['telefono']

                    # 1. Agregar línea al reporte del jefe
                    resumen_admin += f"⏰ *{hora_cita}* - {nombre} ({servicio})\n"

                    # 2. Enviar recordatorio al cliente
                    mensaje_cliente = (
                        f"👋 ¡Hola {nombre}!\n"
                        f"💈 Recordatorio: Tu cita es mañana a las *{hora_cita}*.\n"
                        f"✂️ Servicio: {servicio}\n"
                        f"¡Nos vemos!"
                    )
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                        "chat_id": chat_id, "text": mensaje_cliente, "parse_mode": "Markdown"
                    })
                    enviados_clientes += 1
            except Exception as e:
                print(f"Error procesando cita: {e}")

        # ENVIAR REPORTE FINAL AL ADMIN
        if ADMIN_ID:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": ADMIN_ID,
                "text": resumen_admin,
                "parse_mode": "Markdown"
            })

        return jsonify({
            "status": "Proceso finalizado",
            "fecha": fecha_busqueda,
            "reporte_enviado": True
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 6. RUTAS PRINCIPALES ---
@app.route('/', methods=['GET'])
def home(): return jsonify({"status": "online", "features": ["Chat", "DB", "Memoria", "Recordatorios", "Reporte Admin"]})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"]
            resp = get_gemini_response(user_text, chat_id)
            guardar_mensaje_seguro(chat_id, user_text, resp)
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": resp, "parse_mode": "Markdown"})
        return jsonify({"status": "sent"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)