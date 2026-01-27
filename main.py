import os
import random
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

# Inicializamos Flask
app = Flask(__name__)

# --- 1. CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# --- 2. CONEXIÓN A SUPABASE ---
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Error Supabase: {e}")

# --- 3. RUTAS ---

@app.route('/', methods=['GET'])
def home():
    """Verificación de estado del bot"""
    return jsonify({
        "status": "online",
        "message": "Barbería Bot activo con Gemini 2.5 Flash 🚀"
    })

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        user_message = data.get('message')

        if not user_message:
            return jsonify({"error": "No se recibió ningún mensaje"}), 400

        if not VALID_GEMINI_KEYS:
            return jsonify({"error": "No hay API Keys configuradas en el servidor"}), 500
        
        # Selección aleatoria de Key para balancear la cuota
        selected_key = random.choice(VALID_GEMINI_KEYS)
        genai.configure(api_key=selected_key)
        
        # --- MODELO DETECTADO POR LISTMODELS ---
        model = genai.GenerativeModel('models/gemini-2.5-flash')
        
        response = model.generate_content(user_message)
        bot_reply = response.text

        # Guardado automático en Supabase
        if supabase:
            try:
                supabase.table('messages').insert({
                    "user_input": user_message,
                    "bot_response": bot_reply
                }).execute()
            except Exception as db_error:
                print(f"⚠️ Error al guardar en la base de datos: {db_error}")

        return jsonify({"response": bot_reply})

    except Exception as e:
        print(f"❌ Error en la petición: {str(e)}")
        return jsonify({"error": str(e)}), 500

# --- 4. ARRANQUE DEL SERVIDOR ---
if __name__ == '__main__':
    # Configuración específica para el puerto de Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)