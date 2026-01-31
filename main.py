import os
import random
import json
import requests
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. CONFIGURACIÓN ---
# Rotación de llaves Gemini
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Evolution API (Tu conexión QR)
EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "BarberiaBot")

# Contexto
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de una barbería.")
ADMIN_NUMBER = os.environ.get("ADMIN_NUMBER") # Opcional: para avisar al dueño

# Conexión DB
supabase = None
try:
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase Conectado.")
except: print("🔥 Error conectando Supabase")

# --- 2. PROMPTS INTELIGENTES ---
# Usamos la fecha real para que el bot no se pierda
SYSTEM_PROMPT = f"""
{BUSINESS_CONTEXT}
Hoy es: {datetime.now().strftime('%Y-%m-%d %H:%M')}

--- REGLAS DE ACCIÓN (JSON) ---
Tu objetivo es detectar la intención del usuario.
1. Si quiere RESERVAR y da los datos (Nombre, Fecha, Hora):
   RESPONDE SOLO ESTE JSON: {{"action": "reservar", "nombre": "...", "fecha_hora": "YYYY-MM-DD HH:MM", "servicio": "..."}}

2. Si quiere CANCELAR una cita:
   RESPONDE SOLO ESTE JSON: {{"action": "cancelar", "fecha_hora": "YYYY-MM-DD HH:MM"}}

3. Si solo está conversando, responde normal (texto).
"""

# --- 3. FUNCIONES DE GESTIÓN (Lógica Avanzada) ---

def enviar_mensaje_whatsapp(numero, texto):
    """Envía mensaje usando Evolution API (QR)"""
    try:
        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        headers = {"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"}
        payload = {
            "number": numero,
            "options": {"delay": 1200, "presence": "composing"},
            "textMessage": {"text": texto}
        }
        requests.post(url, json=payload, headers=headers)
    except Exception as e:
        print(f"❌ Error enviando WhatsApp: {e}")

def obtener_id_cliente(telefono):
    """Busca o crea el cliente en Supabase"""
    try:
        # Limpiamos el teléfono para buscar solo números
        tel_limpio = telefono.replace("@s.whatsapp.net", "").replace("+", "")
        r = supabase.table('cliente').select("id").eq('telefono', tel_limpio).execute()
        if r.data: 
            return r.data[0]['id']
        else:
            # Si no existe, lo creamos
            n = supabase.table('cliente').insert({"nombre": "Usuario WhatsApp", "telefono": tel_limpio}).execute()
            return n.data[0]['id']
    except: return None

def gestionar_reserva(datos, telefono):
    print(f"⚡ RESERVA DETECTADA: {datos}")
    try:
        cid = obtener_id_cliente(telefono)
        if not cid: return "Error identificando cliente."
        
        # Insertamos cita
        supabase.table('citas').insert({
            "cliente_id": cid, 
            "servicio": datos.get("servicio", "Corte General"), 
            "fecha_hora": datos.get("fecha_hora"),
            "estado": "confirmado"
        }).execute()
        
        return f"✅ ¡Listo! Reserva confirmada para el {datos.get('fecha_hora')}."
    except Exception as e:
        print(f"Error DB: {e}")
        return "Tuve un error técnico agendando la cita. Intenta de nuevo."

def gestionar_cancelacion(datos, telefono):
    print(f"⚡ CANCELACIÓN DETECTADA: {datos}")
    try:
        cid = obtener_id_cliente(telefono)
        if not cid: return "No encontré tu usuario."

        # Buscamos citas del cliente
        citas = supabase.table('citas').select("*").eq('cliente_id', cid).eq('estado', 'confirmado').execute()
        
        fecha_input = datos.get("fecha_hora", "")[:10] # Comparamos solo la fecha YYYY-MM-DD
        
        for c in citas.data:
            if fecha_input in str(c['fecha_hora']):
                supabase.table('citas').update({"estado": "cancelado"}).eq('id', c['id']).execute()
                return f"🚫 Cita del {c['fecha_hora']} cancelada exitosamente."
        
        return "⚠️ No encontré una cita confirmada en esa fecha para cancelar."
    except Exception as e:
        return "Error técnico cancelando."

def guardar_historial(telefono, input_user, response_bot):
    try:
        supabase.table('messages').insert({
            "user_input": input_user,
            "bot_response": response_bot,
            "platform": "whatsapp",
            "created_at": "now()"
        }).execute()
    except: pass

# --- 4. CEREBRO GEMINI (CON MODELO CORREGIDO) ---
def get_gemini_response(user_text, telefono):
    try:
        if not VALID_GEMINI_KEYS: return "Error: Sin API Keys configuradas."
        
        # Rotación de llaves
        genai.configure(api_key=random.choice(VALID_GEMINI_KEYS))
        
        # CONFIGURACIÓN DEL MODELO CORREGIDA (2.0 Experimental)
        # Si falla 2.0, puedes cambiar a 'gemini-1.5-flash'
        model = genai.GenerativeModel(
            model_name='gemini-2.0-flash-exp', 
            system_instruction=SYSTEM_PROMPT
        )
        
        resp = model.generate_content(user_text)
        bot_text = resp.text.strip()
        
        # Detección de JSON (Acciones)
        if 'action":' in bot_text:
            try:
                # Limpieza de JSON por si Gemini añade markdown
                clean = bot_text.replace("```json", "").replace("```", "").strip()
                start = clean.find('{')
                end = clean.rfind('}') + 1
                json_str = clean[start:end]
                
                data = json.loads(json_str)
                accion = data.get("action")
                
                if accion == "reservar":
                    return gestionar_reserva(data, telefono)
                elif accion == "cancelar":
                    return gestionar_cancelacion(data, telefono)
            except Exception as e:
                print(f"Error parseando JSON: {e}")
                # Si falla el JSON, devolvemos el texto tal cual
                return bot_text

        return bot_text
    except Exception as e:
        return f"Estoy en mantenimiento breve (Error IA): {str(e)}"

# --- 5. WEBHOOK (COMPATIBLE CON EVOLUTION) ---
@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    try:
        data = request.get_json()
        
        # 1. Validación Evolution
        if data.get("type") != "MESSAGES_UPSERT":
            return jsonify({"status": "ignored"}), 200

        message_data = data.get("data", {})
        key = message_data.get("key", {})
        
        # 2. Evitar bucle (no responderse a sí mismo)
        if key.get("fromMe", False):
            return jsonify({"status": "ignored"}), 200

        # 3. Extraer datos
        remote_jid = key.get("remoteJid", "")
        numero = remote_jid.replace("@s.whatsapp.net", "")
        
        message_content = message_data.get("message", {})
        texto_usuario = (
            message_content.get("conversation") or 
            message_content.get("extendedTextMessage", {}).get("text") or
            ""
        )

        if not texto_usuario:
            return jsonify({"status": "ignored"}), 200

        print(f"📩 Mensaje de {numero}: {texto_usuario}")

        # 4. Procesar
        respuesta = get_gemini_response(texto_usuario, numero)
        
        # 5. Responder y Guardar
        enviar_mensaje_whatsapp(numero, respuesta)
        guardar_historial(numero, texto_usuario, respuesta)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)