import os, random, requests, traceback, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN CENTRAL ---
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def get_db_connection():
    # Conexión corregida para psycopg2-binary
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def generar_respuesta_ia(instruccion, texto_usuario):
    llaves = VALID_KEYS[:]
    random.shuffle(llaves) # Evita el error 429
    for key in llaves:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.5-flash', 
                config={'system_instruction': instruccion}, 
                contents=texto_usuario
            )
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): continue
            raise e
    return "Lo siento, estamos horneando demasiadas peticiones."

def ejecutar_logica(id_num, accion, texto="", nombre="Cliente"):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Sincronización de identidad
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre, str(id_num)))
        
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')",
                        (id_num, nombre, texto[:200], datetime.now(venezuela_tz)))
            res = "✅ ¡Pedido agendado en cocina!"
            
        elif accion == "CANCELAR":
            # Buscamos la última orden pendiente del usuario para cancelarla
            cur.execute("SELECT id, fecha_hora FROM citas WHERE user_id = %s AND estado = 'pendiente' ORDER BY fecha_hora DESC LIMIT 1", (id_num,))
            cita = cur.fetchone()
            if cita:
                diff = (datetime.now(venezuela_tz) - cita['fecha_hora']).total_seconds() / 60
                if diff <= 20: # Límite de 20 minutos
                    cur.execute("UPDATE citas SET estado = 'cancelado' WHERE id = %s", (cita['id'],))
                    res = f"🗑️ Pedido cancelado con éxito (hace {int(diff)} min)."
                else:
                    res = "⛔ Lo siento, ya pasaron más de 20 min y no se puede cancelar."
            else:
                res = "❌ No encontré ningún pedido pendiente para cancelar."
        
        conn.commit()
        return res
    except Exception as e:
        conn.rollback()
        print(f"🔥 Error en DB: {e}")
        return "✅ Procesado."
    finally:
        cur.close()
        conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            
            # Filtro de frescura para evitar mensajes antiguos
            if int(datetime.now(pytz.utc).timestamp()) - int(msg.get('timestamp', 0)) > 300:
                return jsonify({"status": "old"}), 200

            numero = msg['from']
            id_num = int(numero)
            texto_cliente = msg.get('text', {}).get('body', "").strip()

            conn = get_db_connection()
            cur = conn.cursor()
            
            # 1. Verificar si el bot está encendido
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            bot_active = cur.fetchone()['value']
            
            # 2. Comandos de Administrador
            if ADMIN_PHONE and numero == ADMIN_PHONE:
                if texto_cliente.upper() == "/BOT_OFF":
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "😴 Bot desactivado.")
                    return jsonify({"status": "ok"}), 200
                elif texto_cliente.upper() == "/BOT_ON":
                    cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "🤖 Bot activado.")
                    return jsonify({"status": "ok"}), 200

            if bot_active == 'false': return jsonify({"status": "off"}), 200

            # 3. Identidad y Memoria del Cliente
            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone()
            nombre_actual = cli['nombre'] if cli else "Desconocido"
            
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            # 4. Procesar con IA
            instruccion = f"{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nHistorial:\n{historial}\nTags: [ACCION:AGENDAR], [ACCION:NOMBRE:Nombre], [ACCION:CANCELAR]"
            respuesta_ia = generar_respuesta_ia(instruccion, texto_cliente)

            # 5. Ejecutar y LIMPIAR etiquetas (Esto evita el error de image_f07180.png)
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_actual = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_actual, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            accion_procesada = False
            for tag in ["CANCELAR", "AGENDAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia and not accion_procesada:
                    feedback = ejecutar_logica(id_num, tag, texto_cliente, nombre_actual)
                    # Eliminamos la etiqueta para que el cliente no la vea
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"
                    accion_procesada = True

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