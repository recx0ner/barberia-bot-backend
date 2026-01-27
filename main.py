import os
import random  # Importamos random para elegir la clave al azar
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

# Inicializamos la aplicación Flask
app = Flask(__name__)

# --- 1. CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
# Cargamos las 3 API Keys. Asegúrate de ponerlas en las variables de entorno de Render.
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]

# Filtramos la lista para quitar valores vacíos (por si alguna no está configurada)
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# --- 2. CONFIGURACIÓN DE SUPABASE ---
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Error conectando a Supabase: {e}")

# --- 3. RUTAS DEL SERVIDOR ---

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "online",
        "message": "El chatbot está activo y rotando API Keys."
    })

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        user_message = data.get('message')

        if not user_message:
            return jsonify({"error": "No enviaste ningún mensaje"}), 400

        # --- Lógica de Rotación de Keys ---
        if not VALID_GEMINI_KEYS:
            return jsonify({"error": "No hay API Keys de Gemini configuradas"}), 500
            
        # Elegimos una clave al azar de la lista
        selected_key = random.choice(VALID_GEMINI_KEYS)
        
        # Configuramos Gemini con la clave elegida para ESTA petición
        genai.configure(api_key=selected_key)
        model = genai.GenerativeModel('gemini-pro')

        # --- Generar respuesta ---
        response = model.generate_content(user_message)
        bot_reply = response.text

        # --- Guardar en Supabase (Opcional) ---
        if supabase:
            try:
                supabase.table('messages').insert({
                    "user_input": user_message,
                    "bot_response": bot_reply
                }).execute()
            except Exception as db_error:
                print(f"Error guardando en BD: {db_error}")

        return jsonify({
            "response": bot_reply
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 4. ARRANQUE DEL SERVIDOR ---
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)