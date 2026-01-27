import os
import random
import json
from datetime import datetime
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. PROMPT CON MEMORIA EXPLÍCITA ---
BARBER_PROMPT = """
Eres el asistente de la "Barbería El Corte Arrechísimo".
Hoy es: {current_date}.

--- HISTORIAL RECIENTE ---
{chat_history}
--------------------------

TU OBJETIVO: Responder usando el historial para entender el contexto.
- Si el cliente dice solo una hora (ej: "10:30"), mira arriba para saber si quiere reservar o cancelar.
- Si detectas una intención CLARA y COMPLETA, genera SOLO el JSON.

ACCIONES JSON (Solo cuando tengas todos los datos):
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
        print(f"⚠️ Error Conexión Supabase: {e}")

# --- 3. FUNCIONES BD ---

def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    """Intenta guardar el mensaje. Si la tabla no existe, no rompe el bot."""
    if not supabase: return
    try:
        supabase.table('messages').insert({
            "user_id": str(chat_id),
            "user_input": user_text,
            "bot_response": bot_text,
            "platform": "telegram"
        }).execute()
    except Exception as e:
        print(f"⚠️ NO SE PUDO GUARDAR EN HISTORIAL: {e}")
        print("💡 CONSEJO: Ejecuta el script SQL en Supabase para crear la tabla 'messages'.")

def obtener_historial_texto(chat_id):
    if not supabase: return "Sin conexión a DB."
    try:
        response = supabase.table('messages').select('*')\
            .eq('user_id', str(chat_id))\
            .order('created_at', desc=True)\
            .limit(5).execute()
        
        if not response.data: return "No hay mensajes anteriores."
        
        texto_historial = ""
        for msg in reversed(response.data):
            u = msg.get('user_input', '')
            b = msg.get('bot_response', '')
            texto_historial += f"Cliente: {u}\nBarbero (Tú): {b}\n---\n"
        return texto_historial
    except Exception as e:
        print(f"⚠️ Error leyendo historial: {e}")
        return "No se pudo recuperar el contexto."

# --- Funciones de Gestión (Reservas/Cancelación) ---
def obtener_id_cliente(chat_id):
    try:
        resp = supabase.table('cliente').select("id").eq('telefono', str(chat_id)).execute()
        if resp.data: return resp.data[0]['id']
    except: pass
    return None

def encontrar_cita_flexible(cliente_id, fecha_objetivo_str):
    try:
        citas = supabase.table('citas').select("*").eq('cliente_id', cliente_id).execute()
        if not citas.data: return None
        for cita in citas.data:
            fecha_db_str = cita['fecha_hora'][:16].replace("T", " ")
            if fecha_db_str == fecha_objetivo_str: return cita['id']
        return None
    except: return None

def gestionar_reserva(datos, chat_id):
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "Corte")
        cliente_id = obtener_id_cliente(chat_id)
        
        if not cliente_id:
            # Crear cliente
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
        
        cita_id = encontrar_cita_flexible(cliente_id, fecha)
        if cita_id:
            supabase.table('citas').delete().eq('id', cita_id).execute()
            return f"🗑️ Cita del {fecha} cancelada."
        return f"⚠️ No encontré una cita exacta a las {fecha}."
    except Exception as e: return f"Error cancelando: {str(e)}"

def gestionar_reprogramacion(datos, chat_id):
    try:
        f_vieja = datos.get("fecha_hora_vieja")
        f_nueva = datos.get("fecha_hora_nueva")
        cliente_id = obtener_id_cliente(chat_id)
        if not cliente_id: return "No encontré tu usuario."

        cita_id = encontrar_cita_flexible(cliente_id, f_vieja)
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
        fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M")
        
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

# --- 5. RUTAS ---
@app.route('/', methods=['GET'])
def home(): return jsonify({"status": "online", "mode": "Safe DB Mode 🛡️"})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"]
            
            response_text = get_gemini_response(user_text, chat_id)

            # Usamos la función segura para guardar
            guardar_mensaje_seguro(chat_id, user_text, response_text)

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": response_text, "parse_mode": "Markdown"
            })
        return jsonify({"status": "sent"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)