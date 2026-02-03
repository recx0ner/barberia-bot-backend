import os, random, requests, traceback, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE NEON (Obtén la URL en tu panel de Neon) ---
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- RESTO DE CONFIGURACIÓN ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k]
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def generar_respuesta_ia(instruccion, texto_usuario):
    llaves = VALID_KEYS[:]
    random.shuffle(llaves) #
    for key in llaves:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(model='gemini-2.5-flash', config={'system_instruction': instruccion}, contents=texto_usuario)
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): continue
            raise e
    return "Lo siento, estamos horneando demasiadas peticiones."

# --- LÓGICA DE NEGOCIO ---
def ejecutar_accion(id_num, accion, texto="", nombre="Cliente"):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre, str(id_num)))
        
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')",
                        (id_num, nombre, texto[:200], datetime.now(venezuela_tz)))
            res = "✅ ¡Pedido agendado!"
        elif accion == "CANCELAR":
            cur.execute("SELECT id, fecha_hora FROM citas WHERE user_id = %s AND estado = 'pendiente' ORDER BY fecha_hora DESC LIMIT 1", (id_num,))
            cita = cur.fetchone()
            if cita:
                diff = (datetime.now(venezuela_tz) - cita['fecha_hora']).total_seconds() / 60
                if diff <= 20:
                    cur.execute("UPDATE citas SET estado = 'cancelado' WHERE id = %s", (cita['id'],))
                    res = "🗑️ Pedido cancelado."
                else: res = "⛔ Pasaron más de 20 min."
            else: res = "❌ Sin pedidos."
        conn.commit()
        return res
    except Exception as e:
        conn.rollback()
        return "✅ Procesado."
    finally:
        cur.close()
        conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    payload = request.json
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero = msg['from']
            id_num = int(numero)
            texto = msg.get('text', {}).get('body', "").strip()

            # Verificar Bot ON/OFF
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false' and numero != ADMIN_PHONE: return jsonify({"status": "off"}), 200

            # Recuperar Identidad y Memoria
            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone()
            nombre = cli['nombre'] if cli else "Desconocido"
            
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            instruccion = f"{BUSINESS_CONTEXT}\nCliente: {nombre}\nHistorial:\n{historial}\nTags: [ACCION:AGENDAR], [ACCION:NOMBRE:Nombre], [ACCION:CANCELAR]"
            respuesta_ia = generar_respuesta_ia(instruccion, texto)

            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:AGENDAR]" in respuesta_ia:
                feedback = ejecutar_accion(id_num, "AGENDAR", texto, nombre)
                respuesta_ia = respuesta_ia.replace("[ACCION:AGENDAR]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto, respuesta_ia))
            conn.commit()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)