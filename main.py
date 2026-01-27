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
TU OBJETIVO: Gestionar citas y mantener una conversación fluida y amable.

REGLAS:
1. Actúa como un barbero profesional que recuerda lo que el cliente le acaba de decir.
2. Si tienes todos los datos para una acción (Reservar/Cancelar/Reprogramar), genera SOLO el JSON.
3. Si falta información, pregúntala basándote en lo que ya te han dicho.

ACCIONES JSON (Solo úsalas cuando tengas confirmación de datos):
- RESERVAR: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}
- CANCELAR: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}
- REPROGRAMAR: {{"action": "reprogramar", "fecha_hora_vieja": "...", "fecha_hora_nueva": "..."}}
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

# --- 3. FUNCIONES DE BASE DE DATOS (GESTIÓN) ---
def obtener_id_cliente(chat_id):
    resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
    if resp.data: return resp.data[0]['id']
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
        return f"✅ ¡Listo {nombre}! Te agendé para el {fecha}."
    except Exception as e: return f"Error reservando: {str(e)}"

def gestionar_cancelacion(datos, chat_id):
    try:
        fecha = datos.get("fecha_hora")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No te encuentro en el sistema."
        resp = supabase.table('citas').delete().eq('cliente_id', cliente_id).eq('fecha_hora', fecha).execute()
        if resp.data: return f"🗑️ Cita del {fecha} cancelada."
        return "⚠️ No encontré una cita exacta a esa hora."
    except Exception as e: return f"Error cancelando: {str(e)}"

def gestionar_reprogramacion(datos, chat_id):
    try:
        fecha_vieja = datos.get("fecha_hora_vieja")
        fecha_nueva = datos.get("fecha_hora_nueva")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No tienes citas para cambiar."
        resp = supabase.table('citas').update({"fecha_hora": fecha_nueva}).eq('cliente_id', cliente_id).eq('fecha_hora', fecha_vieja).execute()
        if resp.data: return f"🔄 Cita movida al {fecha_nueva}."
        return "⚠️ No encontré la cita original."
    except Exception as e: return f"Error reprogramando: {str(e)}"

# --- 4. NUEVA FUNCIÓN: RECUPERAR MEMORIA ---
def obtener_historial_chat(chat_id):
    """Descarga los últimos 6 mensajes de este usuario para tener contexto"""
    if not supabase: return []
    try:
        # Buscamos mensajes donde 'user_id' sea igual al chat_id de Telegram
        response = supabase.table('messages').select('*')\
            .eq('user_id', str(chat_id))\
            .order('created_at', desc=True)\
            .limit(6).execute()
        
        historial_gemini = []
        if response.data:
            # Invertimos la lista para que vaya del más viejo al más nuevo
            for msg in reversed(response.data):
                # Formato que exige Gemini para el historial
                if msg.get('user_input'):
                    historial_gemini.append({"role": "user", "parts": [msg['user_input']]})
                if msg.get('bot_response'):
                    historial_gemini.append({"role": "model", "parts": [msg['bot_response']]})
        
        return historial_gemini
    except Exception as e:
        print(f"Error leyendo historial: {e}")
        return []

# --- 5. CEREBRO IA (CON MEMORIA) ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "⚠️ Error: Sin API Keys."
    
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        # Preparamos el prompt con la fecha
        fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt_sistema = BARBER_PROMPT.format(current_date=fecha_hoy)

        model = genai.GenerativeModel(
            'models/gemini-2.5-flash',
            system_instruction=prompt_sistema
        )
        
        # 1. RECUPERAMOS EL HISTORIAL DE SUPABASE
        historial = obtener_historial_chat(chat_id)
        
        # 2. INICIAMOS EL CHAT CON ESE CONTEXTO
        chat = model.start_chat(history=historial)
        
        # 3. ENVIAMOS EL MENSAJE NUEVO
        response = chat.send_message(user_text)
        texto = response.text.strip()

        # Procesar JSON si existe
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

# --- 6. RUTAS ---
@app.route('/', methods=['GET'])
def home(): return jsonify({"status": "online", "mode": "Memory Active 🧠"})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"]
            
            # Generar respuesta (con memoria)
            response_text = get_gemini_response(user_text, chat_id)

            # GUARDAR EN SUPABASE (IMPORTANTE: Guardamos el user_id para la memoria futura)
            if supabase:
                try:
                    supabase.table('messages').insert({
                        "user_id": chat_id, # <--- CLAVE PARA LA MEMORIA
                        "user_input": user_text, 
                        "bot_response": response_text, 
                        "platform": "telegram"
                    }).execute()
                except Exception as e: print(f"Error guardando mensaje: {e}")

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": response_text, "parse_mode": "Markdown"
            })
        return jsonify({"status": "sent"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)