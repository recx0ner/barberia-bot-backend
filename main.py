import os
import random
import json
from datetime import datetime
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. PROMPT INTELIGENTE (JSON MODE) ---
# Le enseñamos a Gemini a detectar cuándo hay una reserva lista
BARBER_PROMPT = """
Eres el asistente de la "Barbería El Corte Arrechísimo". Hoy es: {current_date}.

TU OBJETIVO:
1. Si el usuario solo saluda o pregunta precios, responde amablemente como barbero.
2. Si el usuario quiere una cita pero faltan datos, pídeles: Nombre y Hora/Fecha.
3. ¡IMPORTANTE! Si el usuario YA te dio su Nombre y la Fecha/Hora:
   NO respondas con texto. Debes generar UNICAMENTE un objeto JSON con este formato exacto:
   
   {{"action": "reservar", "nombre": "NombreDelCliente", "fecha_hora": "YYYY-MM-DD HH:MM:SS", "servicio": "Corte"}}

   - Calcula la fecha exacta basada en "hoy" (ej: si dicen "mañana", suma 1 día).
   - El servicio por defecto es "Corte" si no especifican.
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

# --- 3. FUNCIÓN PARA GUARDAR EN BASE DE DATOS ---
def guardar_cita_en_supabase(datos_reserva, chat_id):
    if not supabase:
        return "Error: Base de datos no conectada."
    
    try:
        nombre = datos_reserva.get("nombre")
        fecha = datos_reserva.get("fecha_hora")
        servicio = datos_reserva.get("servicio", "Corte")
        telefono = str(chat_id) # Usamos el ID de Telegram como teléfono/ID único

        # PASO A: Verificar si el cliente ya existe (por teléfono/ID)
        response = supabase.table('cliente').select("*").eq('telefono', telefono).execute()
        
        cliente_id = None
        
        if response.data and len(response.data) > 0:
            # Cliente existe
            cliente_id = response.data[0]['id']
        else:
            # Cliente nuevo: Lo creamos
            nuevo_cliente = supabase.table('cliente').insert({
                "nombre": nombre,
                "telefono": telefono
            }).execute()
            cliente_id = nuevo_cliente.data[0]['id']

        # PASO B: Crear la cita vinculada al cliente
        supabase.table('citas').insert({
            "cliente_id": cliente_id,
            "servicio": servicio,
            "fecha_hora": fecha
        }).execute()

        return f"✅ ¡Listo {nombre}! Tu cita para el {fecha} ha sido registrada en el sistema."

    except Exception as e:
        print(f"Error DB: {e}")
        return "Hubo un error técnico guardando tu cita. Por favor intenta de nuevo."

# --- 4. CEREBRO IA ---
def get_gemini_response(user_text, chat_id):
    if not VALID_GEMINI_KEYS:
        return "⚠️ Error: Sistema en mantenimiento."
    
    try:
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        # Inyectamos la fecha de hoy para que sepa qué es "mañana"
        fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt_actualizado = BARBER_PROMPT.format(current_date=fecha_hoy)

        model = genai.GenerativeModel(
            'models/gemini-2.5-flash',
            system_instruction=prompt_actualizado
        )
        
        # Pedimos respuesta
        response = model.generate_content(user_text)
        respuesta_texto = response.text.strip()

        # --- DETECTOR DE JSON (MAGIA) ---
        # Verificamos si Gemini nos devolvió el JSON de reserva en vez de texto normal
        if "{" in respuesta_texto and '"action": "reservar"' in respuesta_texto:
            try:
                # Limpiamos el texto para obtener solo el JSON (a veces añade ```json ... ```)
                json_str = respuesta_texto.replace("```json", "").replace("```", "").strip()
                datos_reserva = json.loads(json_str)
                
                # ¡AQUÍ ES DONDE PYTHON GUARDA EN SUPABASE!
                mensaje_confirmacion = guardar_cita_en_supabase(datos_reserva, chat_id)
                return mensaje_confirmacion
            except Exception as e:
                print(f"Error parseando JSON: {e}")
                return "Entendí que quieres reservar, pero tuve un error procesando los datos."
        
        # Si no es JSON, es charla normal
        return respuesta_texto

    except Exception as e:
        return f"Tuve un problema técnico: {str(e)}"

# --- 5. RUTAS ---
@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "online", "mode": "DB Integration Active 🗄️"})

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.json
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            user_text = data["message"]["text"]
            
            # Pasamos el chat_id para poder registrar al usuario
            response_text = get_gemini_response(user_text, chat_id)

            # (Opcional) Guardar log del chat
            if supabase:
                try:
                    supabase.table('messages').insert({
                        "user_input": user_text, 
                        "bot_response": response_text, 
                        "platform": "telegram"
                    }).execute()
                except:
                    pass

            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": chat_id, 
                "text": response_text,
                "parse_mode": "Markdown"
            })
        return jsonify({"status": "sent"}), 200

    except Exception as e:
        print(f"Error Telegram: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)