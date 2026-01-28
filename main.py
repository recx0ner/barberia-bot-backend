import os
import random
import json
from datetime import datetime, timedelta
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. PROMPT ESTRICTO (JSON FIRST) ---
BARBER_PROMPT = """
Eres el asistente de la "Barbería El Corte Arrechísimo".
Hoy es: {current_date} (Hora Venezuela).

--- HISTORIAL RECIENTE ---
{chat_history}
--------------------------

REGLA DE ORO:
Si el cliente ya proporcionó NOMBRE, FECHA y HORA para una cita:
¡NO respondas con texto conversacional!
TU ÚNICA RESPUESTA DEBE SER EL OBJETO JSON.

ACCIONES JSON:
- RESERVAR: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}
- CANCELAR: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}
- REPROGRAMAR: {{"action": "reprogramar", "fecha_hora_vieja": "YYYY-MM-DD HH:MM", "fecha_hora_nueva": "YYYY-MM-DD HH:MM"}}

Si faltan datos, entonces sí responde amable y pide lo que falta.
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
ADMIN_ID = os.environ.get("ADMIN_ID")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Conexión Supabase: {e}")

# --- 3. FUNCIONES BD ---
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
            historial += f"Cliente: {msg.get('user_input','')}\nBarbero: {msg.get('bot_response','')}\n---\n"
        return historial
    except: return ""

def obtener_id_cliente(chat_id):
    try:
        resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
        if resp.data: return resp.data[0]['id']
    except: pass
    return None

def gestionar_reserva(datos, chat_id):
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "Corte")
        cliente_id = obtener_id_cliente(chat_id)
        
        if not cliente_id:
            nuevo = supabase.table('cliente').insert({"nombre": nombre, "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']
            
        supabase.table('citas').insert({"cliente_id": cliente_id, "servicio": servicio, "fecha_hora": fecha}).execute()
        return f"✅ ¡Listo {nombre}! Cita agendada para el {fecha}."
    except Exception as e: return f"Error reservando: {str(e)}"

def gestionar_cancelacion(datos, chat_id):
    try:
        fecha = datos.get("fecha_hora")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No encontré tu usuario."
        
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        cita_id = None
        for c in citas.data:
            if c['fecha_hora'].startswith(fecha):
                cita_id = c['id']
                break
        
        if cita_id:
            supabase.table('citas').delete().eq('id', cita_id).execute()
            return f"🗑️ Cita del {fecha} cancelada."
        return "⚠️ No encontré esa cita exacta."
    except Exception as e: return f"Error cancelando: {str(e)}"

def gestionar_reprogramacion(datos, chat_id):
    try:
        f_vieja = datos.get("fecha_hora_vieja")
        f_nueva = datos.get("fecha_hora_nueva")
        cliente_id = obtener_id_cliente(chat_id)
        
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        cita_id = None
        for c in citas.data:
            if c['fecha_hora'].startswith(f_vieja):
                cita_id = c['id']
                break

        if cita_id:
            supabase.table('citas').update({"fecha_hora": f_nueva}).eq('id', cita_id).execute()
            return f"🔄 Cita movida al {f_nueva}."
        return "⚠️ No encontré la cita original."
    except Exception as e: return f"Error reprogramando: {str(e)}"

# --- 4. CEREBRO IA ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "⚠️ Error: Sin API Keys."
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        historial_str = obtener_historial_texto(chat_id)
        fecha_dt = datetime.now() - timedelta(hours=4)
        fecha_hoy = fecha_dt.strftime("%Y-%m-%d %H:%M")
        
        prompt_final = BARBER_PROMPT.format(current_date=fecha_hoy, chat_history=historial_str)
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt_final)
        response = model.generate_content(user_text)
        texto = response.text.strip()

        if "{" in texto and '"action":' in texto:
            try:
                json_str = texto.replace("```json", "").replace("```", "").strip()
                datos = json.loads(json_str)
                accion = datos.get("action")
                if accion == "reservar": return gestionar_reserva(datos, chat_id)
                elif accion == "cancelar": return gestionar_cancelacion(datos, chat_id)
                elif accion == "reprogramar": return gestionar_reprogramacion(datos, chat_id)
            except: pass
        return texto
    except Exception as e: return f"Error técnico: {str(e)}"

# --- 5. RECORDATORIOS (CORREGIDO PARA BASE DE DATOS TIPO FECHA) ---
@app.route('/recordatorios', methods=['GET'])
def enviar_recordatorios():
    # 1. Seguridad
    secret = request.args.get('key')
    if secret != CRON_SECRET: return jsonify({"error": "Clave incorrecta"}), 403
    if not supabase: return jsonify({"error": "Sin conexión a DB"}), 500

    try:
        # 2. Calcular el rango de búsqueda (MAÑANA COMPLETO)
        # Hora Venezuela (-4)
        hoy = datetime.now() - timedelta(hours=4)
        manana = hoy + timedelta(days=1)
        
        # Definimos inicio y fin del día
        fecha_inicio = manana.strftime("%Y-%m-%d 00:00:00")
        fecha_fin = manana.strftime("%Y-%m-%d 23:59:59")
        fecha_simple = manana.strftime("%Y-%m-%d") # Solo para mostrar en el mensaje
        
        print(f"🔎 Buscando citas entre: {fecha_inicio} y {fecha_fin}")

        # 3. Consulta compatible con TIMESTAMP (Usamos gte y lte en lugar de ilike)
        # gte = Mayor o igual que...
        # lte = Menor o igual que...
        response = supabase.table('citas').select("*")\
            .gte('fecha_hora', fecha_inicio)\
            .lte('fecha_hora', fecha_fin)\
            .execute()
            
        citas = response.data

        if not citas: return jsonify({"status": "Sin citas", "fecha": fecha_simple}), 200

        # 4. Enviar Mensajes
        resumen_admin = f"📅 *AGENDA {fecha_simple}*\n"
        enviados = 0
        
        for cita in citas:
            try:
                if not cita.get('fecha_hora'): continue
                
                # Limpieza de fecha (Maneja formatos con T y sin T)
                fecha_raw = str(cita['fecha_hora']) # Aseguramos que sea string
                hora = fecha_raw.replace("T", " ").split(" ")[1][:5]
                
                cliente_resp = supabase.table('cliente').select("nombre, telefono").eq('id', cita['cliente_id']).execute()
                
                if cliente_resp.data:
                    cliente = cliente_resp.data[0]
                    nombre = cliente.get('nombre', 'Cliente')
                    chat_id = cliente.get('telefono')
                    servicio = cita.get('servicio', 'Cita')
                    
                    resumen_admin += f"⏰ {hora} - {nombre}\n"

                    if chat_id:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                            "chat_id": chat_id, 
                            "text": f"👋 Hola {nombre}, recuerda tu cita mañana a las {hora} ({servicio})."
                        })
                        enviados += 1
            except Exception as e:
                print(f"Error enviando uno: {e}")

        if ADMIN_ID:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": ADMIN_ID, "text": resumen_admin
            })

        return jsonify({"status": "Enviado", "cantidad": enviados}), 200

    except Exception as e: return jsonify({"error": str(e)}), 500
    
# --- 6. RUTAS PRINCIPALES ---
@app.route('/', methods=['GET'])
def home(): return jsonify({"status": "online", "features": ["Chat Vzla", "DB", "Recordatorios"]})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"]
            resp = get_gemini_response(user_text, chat_id)
            guardar_mensaje_seguro(chat_id, user_text, resp)
            
            # --- CORRECCIÓN CRÍTICA AQUÍ ---
            # Eliminamos "parse_mode" para que Telegram no rechace el mensaje
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, 
                "text": resp
                # "parse_mode": "Markdown" <--- BORRADO A PROPÓSITO
            })
        return jsonify({"status": "sent"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)