import os
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

# Inicializamos la aplicación Flask
app = Flask(__name__)

# --- 1. CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
# Render inyectará estos valores automáticamente si los configuraste en el panel
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# --- 2. CONFIGURACIÓN DE SERVICIOS ---

# Configuración de Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-pro')
else:
    print("⚠️ Error: No se encontró la GEMINI_API_KEY")

# Configuración de Supabase
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Error conectando a Supabase: {e}")

# --- 3. RUTAS DEL SERVIDOR ---

@app.route('/', methods=['GET'])
def home():
    """Ruta básica para verificar que el servidor está vivo (Ping)"""
    return jsonify({
        "status": "online",
        "message": "El chatbot de atención al cliente está activo."
    })

@app.route('/chat', methods=['POST'])
def chat():
    """Ruta principal donde se reciben los mensajes"""
    try:
        data = request.json
        user_message = data.get('message')

        if not user_message:
            return jsonify({"error": "No enviaste ningún mensaje"}), 400

        # --- Lógica del Bot ---
        
        # A. Generar respuesta con Gemini
        # (Aquí puedes agregar un 'prompt del sistema' si quieres darle personalidad)
        response = model.generate_content(user_message)
        bot_reply = response.text

        # B. Guardar historial en Supabase (Opcional pero recomendado)
        if supabase:
            try:
                # Asegúrate de tener una tabla llamada 'messages' o cambia el nombre aquí
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

# --- 4. ARRANQUE DEL SERVIDOR (CRÍTICO PARA RENDER) ---
if __name__ == '__main__':
    # Render asigna un puerto dinámico en la variable de entorno 'PORT'.
    # Si no lo encuentra (ej. en tu PC), usa el 5000.
    port = int(os.environ.get("PORT", 5000))
    
    # host='0.0.0.0' hace que el servidor sea accesible públicamente en la nube.
    app.run(host='0.0.0.0', port=port)