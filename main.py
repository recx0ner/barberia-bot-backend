import os
import random
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

# Inicializamos la aplicación Flask
app = Flask(__name__)

# --- 1. CONFIGURACIÓN DE VARIABLES DE ENTORNO ---

# Cargamos las 3 API Keys de Gemini
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]

# Filtramos para usar solo las que tengan valor (por si alguna falta)
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

# Cargamos las credenciales de Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# --- 2. CONEXIÓN A SUPABASE ---
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Conectado a Supabase correctamente.")
    except Exception as e:
        print(f"⚠️ Error conectando a Supabase: {e}")

# --- 3. RUTAS DEL SERVIDOR ---

@app.route('/', methods=['GET'])
def home():
    """Ruta para verificar que el bot está vivo"""
    return jsonify({
        "status": "online",
        "message": "Barbería Bot activo y funcionando con Gemini 1.5 Flash ⚡"
    })

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        user_message = data.get('message')

        if not user_message:
            return jsonify({"error": "No enviaste ningún mensaje"}), 400

        # --- A. Selección de API Key ---
        if not VALID_GEMINI_KEYS:
            return jsonify({"error": "Error interno: No hay API Keys configuradas"}), 500
        
        # Elegimos una clave al azar para repartir la carga
        selected_key = random.choice(VALID_GEMINI_KEYS)
        
        # Configuramos la librería con esa clave específica
        genai.configure(api_key=selected_key)
        
        # --- B. Generación de Respuesta (CORRECCIÓN AQUÍ) ---
        # Usamos 'gemini-1.5-flash' que es el modelo actual recomendado
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        response = model.generate_content(user_message)
        bot_reply = response.text

        # --- C. Guardado en Historial (Supabase) ---
        if supabase:
            try:
                # Intenta guardar en la tabla 'messages'. 
                # Si tu tabla se llama diferente, cambia el nombre aquí.
                supabase.table('messages').insert({
                    "user_input": user_message,
                    "bot_response": bot_reply
                }).execute()
            except Exception as db_error:
                # Solo imprimimos el error en los logs de Render, no detenemos al bot
                print(f"⚠️ Error guardando en BD: {db_error}")

        return jsonify({
            "response": bot_reply
        })

    except Exception as e:
        print(f"❌ Error crítico: {str(e)}")
        return jsonify({"error": str(e)}), 500

# --- 4. ARRANQUE DEL SERVIDOR ---
if __name__ == '__main__':
    # Render asigna el puerto en la variable 'PORT'
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)