import os
import random
import json
import requests
import traceback # Para ver el error exacto
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
        print("✅ Conexión a Supabase iniciada.")
except Exception as e:
    print(f"🔥 Error conectando Supabase: {e}")

# --- FUNCIONES BASE DE DATOS (ARREGLADAS) ---

def guardar_mensaje_seguro(chat_id, user_text, bot_text):
    if not supabase: return
    try:
        # FORZAMOS que el chat_id sea string
        chat_id_str = str(chat_id)
        
        data = {
            "user_id": chat_id_str,
            "user_input": user_text,
            "bot_response": bot_text,
            "platform": "whatsapp"
        }
        # Imprimimos lo que vamos a guardar para ver si hay error
        print(f"💾 Intentando guardar DB: {data}")
        
        supabase.table('messages').insert(data).execute()
        print("✅ Guardado Exitoso en DB.")
        
    except Exception as e:
        print(f"🔥 ERROR CRÍTICO GUARDANDO EN DB: {e}")
        traceback.print_exc() # Esto mostrará el error real en los logs

def obtener_historial_texto(chat_id):
    if not supabase: return ""
    try:
        chat_id_str = str(chat_id)
        response = supabase.table('messages').select('*').eq('user_id', chat_id_str).order('created_at', desc=True).limit(5).execute()
        
        historial = ""
        if response.data:
            for msg in reversed(response.data):
                val_user = msg.get('user_input') or "..."
                val_bot = msg.get('bot_response') or "..."
                historial += f"Usuario: {val_user}\nAsistente: {val_bot}\n---\n"
        return historial
    except Exception as e:
        print(f"⚠️ Error leyendo historial: {e}")
        return ""

def obtener_id_cliente(chat_id):
    try:
        chat_id_str = str(chat_id)
        resp = supabase.table('cliente').select("id").eq('telefono', chat_id_str).execute()
        if resp.data: return resp.data[0]['id']
    except: pass
    return None

# --- FUNCIONES DE LOGICA (Resumidas para asegurar funcionamiento) ---
# ... (Mantén tu lógica de gestionar_reserva aquí, asegúrate de usar str(chat_id)) ...

def gestionar_reserva(datos, chat_id):
    try:
        nombre = datos.get("nombre")
        fecha = datos.get("fecha_hora")
        servicio = datos.get("servicio", "General")
        chat_id_str = str(chat_id)
        
        cliente_id = obtener_id_cliente(chat_id_str)
        if not cliente_id:
            # Crear cliente si no existe
            nuevo = supabase.table('cliente').insert({"nombre": nombre, "telefono": chat_id_str}).execute()
            cliente_id = nuevo.data[0]['id']
            
        supabase.table('citas').insert({"cliente_id": cliente_id, "servicio": servicio, "fecha_hora": fecha}).execute()
        return f"✅ Reserva confirmada para {nombre} el {fecha}."
    except Exception as e:
        print(f"Error Reserva: {e}")
        return "Error agendando. Intenta de nuevo."

def enviar_whatsapp(destinatario, texto):
    try:
        url = f"https://graph.facebook.com/v17.0/{PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": destinatario, "type": "text", "text": {"body": texto}}
        requests.post(url, headers=headers, json=data)
    except: pass

def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS: return "Error API Key"
    try:
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        historial = obtener_historial_texto(chat_id)
        
        # PROMPT
        prompt = f"""
        {BUSINESS_CONTEXT}
        Hoy es: {datetime.now().strftime('%Y-%m-%d')}
        HISTORIAL:
        {historial}
        
        Si tienes datos para agendar (Nombre, Fecha, Hora), responde SOLO JSON:
        {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}
        """
        
        model = genai.GenerativeModel('models/gemini-2.5-flash', system_instruction=prompt)
        response = model.generate_content(user_text)
        texto = response.text.strip()
        
        if 'action": "reservar"' in texto:
            try:
                # Limpieza de JSON básica
                clean = texto.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean[clean.find('{'):clean.rfind('}')+1])
                return gestionar_reserva(data, chat_id)
            except: pass
            
        return texto
    except Exception as e: return f"Error IA: {e}"

# --- WEBHOOK ---
@app.route('/whatsapp', methods=['GET', 'POST'])
def whatsapp_webhook():
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN: return request.args.get('hub.challenge')
        return "Error Token", 403

    try:
        data = request.json
        if 'entry' in data:
            for entry in data['entry']:
                for change in entry['changes']:
                    if 'messages' in change['value']:
                        msg = change['value']['messages'][0]
                        sender = msg['from'] # ESTE ES EL ID (Whatsapp Number)
                        
                        if msg['type'] == 'text':
                            body = msg['text']['body']
                            resp = get_gemini_response(body, sender)
                            
                            # AQUÍ OCURRE EL GUARDADO
                            guardar_mensaje_seguro(sender, body, resp)
                            enviar_whatsapp(sender, resp)

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"🔥 Error Webhook: {e}")
        traceback.print_exc()
        return jsonify({"status": "error"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))