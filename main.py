import os
import random
import json
from datetime import datetime
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. PROMPT (CEREBRO) ---
BARBER_PROMPT = """
Eres el asistente de la "Barbería El Corte Arrechísimo". Hoy es: {current_date}.
TU OBJETIVO: Gestionar citas y mantener una conversación fluida.

REGLAS CRÍTICAS:
1. Si el usuario pide CANCELAR o REPROGRAMAR y da la hora, ¡DEBES GENERAR EL JSON! No respondas con texto.
2. Formato de fechas siempre: YYYY-MM-DD HH:MM (Ej: 2026-01-28 10:30).
3. Si el usuario solo saluda, responde corto y amable.

ACCIONES JSON (Úsalas obligatoriamente si detectas la intención):
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

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- 3. FUNCIONES DE BASE DE DATOS ROBUSTAS ---

def obtener_id_cliente(chat_id):
    """Busca el ID del cliente por su chat_id de Telegram"""
    resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
    if resp.data: return resp.data[0]['id']
    return None

def encontrar_cita_flexible(cliente_id, fecha_objetivo_str):
    """
    Busca una cita que coincida en día y hora, ignorando segundos y zona horaria.
    Retorna el ID de la cita si la encuentra.
    """
    try:
        # 1. Convertimos la fecha que dio el usuario a objeto datetime
        fecha_obj = datetime.strptime(fecha_objetivo_str, "%Y-%m-%d %H:%M")
        
        # 2. Traemos TODAS las citas futuras del cliente
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        
        if not citas.data:
            return None

        # 3. Buscamos coincidencia manual en Python
        for cita in citas.data:
            # La fecha en DB viene como string ISO (ej: 2026-01-28T10:30:00+00:00)
            # Cortamos los primeros 16 caracteres para comparar "YYYY-MM-DDTHH:MM"
            fecha_db_str = cita['fecha_hora'][:16].replace("T", " ")
            
            if fecha_db_str == fecha_objetivo_str:
                return cita['id'] # ¡Encontramos la cita exacta!
                
        return None
    except Exception as e:
        print(f"Error buscando cita flexible: {e}")
        return None

def gestionar_reserva(datos, chat_id):
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "Corte")
        cliente_id = obtener_id_cliente(chat_id)
        
        if not cliente_id:
            # Crear cliente si no existe
            nuevo = supabase.table('cliente').insert({"nombre": nombre, "telefono": str(chat_id)}).execute()
            cliente_id = nuevo.data[0]['id']
            
        supabase.table('citas').insert({
            "cliente_id": cliente_id, 
            "servicio": servicio, 
            "fecha_hora": fecha
        }).execute()
        
        return f"✅ ¡Listo {nombre}! Te agendé para el {fecha}."
    except Exception as e: return f"Error reservando: {str(e)}"

def gestionar_cancelacion(datos, chat_id):
    try:
        fecha_usuario = datos.get("fecha_hora")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No te encuentro en el sistema."

        # BUSQUEDA INTELIGENTE
        cita_id = encontrar_cita_flexible(cliente_id, fecha_usuario)

        if cita_id:
            # Borramos por ID específico (infalible)
            supabase.table('citas').delete().eq('id', cita_id).execute()
            return f"🗑️ Cita del {fecha_usuario} cancelada correctamente."
        else:
            return f"⚠️ No encontré una cita EXACTAMENTE a las {fecha_usuario}. Verifica la hora."

    except Exception as e: return f"Error cancelando: {str(e)}"

def gestionar_reprogramacion(datos, chat_id):
    try:
        fecha_vieja = datos.get("fecha_hora_vieja")
        fecha_nueva = datos.get("fecha_hora_nueva")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No tienes citas para cambiar."

        # BUSQUEDA INTELIGENTE
        cita_id = encontrar_cita_flexible(cliente_id, fecha_vieja)

        if cita_id:
            supabase.table('citas').update({"fecha_hora": fecha_nueva}).eq('id', cita_id).execute()
            return f"🔄 Cita movida exitosamente al {fecha_nueva}."
        else:
            return "⚠️ No encontré la cita original para cambiarla."
            
    except Exception as e: return f"Error reprogramando: {str(e)}"

# --- 4. MEMORIA Y CHAT ---
def obtener_historial_chat(chat_id):
    if not supabase: return []
    try:
        response = supabase.table('messages').select('*').eq('user_id', str(chat_id)).order('created_at', desc=True).limit(6).execute()
        historial_gemini = []
        if response.data:
            for msg in reversed(response.data):
                if msg.get('user_input'): historial_gemini.append({"role": "user", "parts": [msg['user_input']]})
                if msg.get('bot_response'): historial_gemini.append({"role": "model", "parts": [msg['bot_response']]})
        return historial_gemini
    except: return []

def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "⚠️ Error: Sin API Keys."
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt_sistema = BARBER_PROMPT.format(current_date=fecha_hoy)

        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt_sistema)
        historial = obtener_historial_chat(chat_id)
        chat = model.start_chat(history=historial)
        
        response = chat.send_message(user_text)
        texto = response.text.strip()

        # DETECTOR DE JSON
        if "{" in texto and '"action":' in texto:
            try:
                json_str = texto.replace("```json", "").replace("```", "").strip()
                datos = json.loads(json_str)
                accion = datos.get("action")
                
                if accion == "reservar": return gestionar_reserva(datos, chat_id)
                elif accion == "cancelar": return gestionar_cancelacion(datos, chat_id)
                elif accion == "reprogramar": return gestionar_reprogramacion(datos, chat_id)
            except Exception as e: print(f"JSON Error: {e}")
        
        return texto

    except Exception as e: return f"Error técnico: {str(e)}"

# --- 5. RUTAS ---
@app.route('/', methods=['GET'])
def home(): return jsonify({"status": "online", "mode": "Smart Dates 📅"})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"]
            
            response_text = get_gemini_response(user_text, chat_id)

            if supabase:
                try:
                    supabase.table('messages').insert({
                        "user_id": chat_id,
                        "user_input": user_text, 
                        "bot_response": response_text, 
                        "platform": "telegram"
                    }).execute()
                except: pass

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": response_text, "parse_mode": "Markdown"
            })
        return jsonify({"status": "sent"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)