import os, requests, traceback, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

# Modelo actualizado a Gemini 2.5 Flash
MODELO_IA = "google/gemini-2.5-flash"

def get_db_connection():
    """Conexión a la base de datos Neon"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def enviar_meta(to, text):
    """Envía respuesta al cliente por WhatsApp"""
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def generar_respuesta_ia(instruccion, texto_usuario):
    """Llamada a OpenRouter usando Gemini 2.5 Flash"""
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://pizzaselguaro.com",
            "X-Title": "Pizzas El Guaro Bot",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": MODELO_IA,
            "messages": [
                {"role": "system", "content": instruccion},
                {"role": "user", "content": texto_usuario}
            ],
            "temperature": 0.7
        }
        
        response = requests.post(url, headers=headers, json=payload)
        data = response.json()
        
        if "choices" in data:
            return data['choices'][0]['message']['content'].strip()
        
        print(f"🔥 Error OpenRouter: {data}")
        return "Lo siento, tuve un problema al conectar con el horno mental. ¿Podrías repetir tu pedido?"
        
    except Exception as e:
        print(f"🔥 Excepción en IA: {e}")
        return "Tengo un problema técnico. Intenta de nuevo en unos segundos."

def ejecutar_logica_negocio(id_num, accion, texto_cliente="", nombre_actual="Cliente"):
    """Gestión de pedidos y cancelaciones en Neon"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", 
                    (id_num, nombre_actual, str(id_num)))
        
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')",
                        (id_num, nombre_actual, texto_cliente[:200], datetime.now(venezuela_tz)))
            res = "✅ ¡Listo! Tu pedido ya está en la fila de cocina."
            
        elif accion == "CANCELAR":
            cur.execute("SELECT id, fecha_hora FROM citas WHERE user_id = %s AND estado = 'pendiente' ORDER BY fecha_hora DESC LIMIT 1", (id_num,))
            cita = cur.fetchone()
            if cita:
                diff = (datetime.now(venezuela_tz) - cita['fecha_hora']).total_seconds() / 60
                if diff <= 20: # Límite de 20 minutos
                    cur.execute("UPDATE citas SET estado = 'cancelado' WHERE id = %s", (cita['id'],))
                    res = f"🗑️ Pedido cancelado correctamente."
                else:
                    res = "⛔ Ya pasaron 20 min y el pedido ya está en el horno, no se puede cancelar."
            else:
                res = "❌ No encontré pedidos pendientes para cancelar."
        
        conn.commit()
        return res
    except:
        conn.rollback()
        return "✅ Procesado."
    finally:
        cur.close()
        conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200
        
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            
            # Filtro de mensajes antiguos
            if int(datetime.now(pytz.utc).timestamp()) - int(msg.get('timestamp', 0)) > 300:
                return jsonify({"status": "old"}), 200

            numero = msg['from']
            id_num = int(numero)
            texto_cliente = msg.get('text', {}).get('body', "").strip()

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            bot_active = cur.fetchone()['value']
            
            # Control de Administrador (ON/OFF)
            if ADMIN_PHONE and numero == ADMIN_PHONE:
                if texto_cliente.upper() == "/BOT_OFF":
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "😴 Bot apagado. Estás en control manual.")
                    return jsonify({"status": "ok"}), 200
                elif texto_cliente.upper() == "/BOT_ON":
                    cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "🤖 Bot reactivado.")
                    return jsonify({"status": "ok"}), 200

            if bot_active == 'false':
                return jsonify({"status": "off"}), 200

            # Recuperar Identidad y Memoria
            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone()
            nombre_actual = cli['nombre'] if cli and cli['nombre'].lower() != "nombre" else "Desconocido"
            
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            instruccion = f"""{BUSINESS_CONTEXT}
            Cliente: {nombre_actual}
            Historial:\n{historial}
            ETIQUETAS REQUERIDAS: [ACCION:NOMBRE:NombreReal], [ACCION:AGENDAR], [ACCION:CANCELAR].
            """
            respuesta_ia = generar_respuesta_ia(instruccion, texto_cliente)

            # Limpiar etiquetas del mensaje para el cliente
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_nuevo = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                if nombre_nuevo.lower() != "nombre":
                    nombre_actual = nombre_nuevo
                    cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_actual, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["CANCELAR", "AGENDAR"]:
                tag_full = f"[ACCION:{tag}]"
                if tag_full in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto_cliente, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(tag_full, "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_cliente, respuesta_ia))
            conn.commit()
            cur.close()
            conn.close()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)