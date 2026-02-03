import os, random, requests, traceback, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
DATABASE_URL = os.environ.get("DATABASE_URL") #
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
    # 1. Obtenemos las llaves disponibles
    llaves = VALID_KEYS[:]
    if not llaves:
        print("❌ ERROR: No hay ninguna GEMINI_API_KEY configurada en Render.")
        return "Error de configuración: No hay llaves de IA disponibles."
    
    # 2. Mezclamos para distribuir la carga
    random.shuffle(llaves)
    
    # 3. Probamos una por una
    for i, key in enumerate(llaves):
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.0-flash', # Actualizado a la versión más estable
                config={'system_instruction': instruccion}, 
                contents=texto_usuario
            )
            # Si tiene éxito, devolvemos el texto
            if response.text:
                return response.text.strip()
            return "La IA devolvió una respuesta vacía."
            
        except errors.ClientError as e:
            # Si es error de cuota (429), intentamos con la siguiente
            if "429" in str(e):
                print(f"⚠️ Llave {i+1} agotada (429). Probando la siguiente...")
                continue
            # Si es otro error, lo reportamos
            print(f"🔥 Error crítico en IA: {e}")
            break 

    return "🥪 *Pizzas El Guaro:* Estamos horneando demasiadas peticiones al mismo tiempo. ¡Danos un minuto y vuelve a intentarlo!"

def ejecutar_logica(id_num, accion, texto_cliente="", nombre_actual="Cliente"):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Aseguramos el registro del cliente con el nombre REAL
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", 
                    (id_num, nombre_actual, str(id_num)))
        
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')",
                        (id_num, nombre_actual, texto_cliente[:200], datetime.now(venezuela_tz)))
            res = "✅ ¡Pedido registrado en cocina!"
            
        elif accion == "CANCELAR":
            # Buscamos la última orden real en Neon
            cur.execute("SELECT id, fecha_hora FROM citas WHERE user_id = %s AND estado = 'pendiente' ORDER BY fecha_hora DESC LIMIT 1", (id_num,))
            cita = cur.fetchone()
            if cita:
                diff = (datetime.now(venezuela_tz) - cita['fecha_hora']).total_seconds() / 60
                if diff <= 20:
                    cur.execute("UPDATE citas SET estado = 'cancelado' WHERE id = %s", (cita['id'],))
                    res = f"🗑️ Pedido cancelado (hace {int(diff)} min)."
                else:
                    res = "⛔ No se puede cancelar, ya pasaron los 20 min reglamentarios."
            else:
                res = "❌ No tienes pedidos pendientes para cancelar."
        
        conn.commit()
        return res
    except Exception as e:
        conn.rollback()
        print(f"🔥 Error DB: {e}")
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
            
            # Filtro de frescura
            if int(datetime.now(pytz.utc).timestamp()) - int(msg.get('timestamp', 0)) > 300:
                return jsonify({"status": "old"}), 200

            numero = msg['from']
            id_num = int(numero)
            texto_cliente = msg.get('text', {}).get('body', "").strip()

            conn = get_db_connection()
            cur = conn.cursor()
            
            # 1. Estado del Bot
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            bot_active = cur.fetchone()['value']
            
            # 2. Comandos Admin
            if ADMIN_PHONE and numero == ADMIN_PHONE:
                if texto_cliente.upper() == "/BOT_OFF":
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "😴 Bot apagado.")
                    return jsonify({"status": "ok"}), 200
                elif texto_cliente.upper() == "/BOT_ON":
                    cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "🤖 Bot encendido.")
                    return jsonify({"status": "ok"}), 200

            if bot_active == 'false': return jsonify({"status": "off"}), 200

            # 3. Recuperar Nombre Real de Neon
            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone()
            # Si el nombre es 'Nombre', lo tratamos como Desconocido para que la IA lo pida de nuevo
            nombre_memoria = cli['nombre'] if cli and cli['nombre'] != "Nombre" else "Desconocido"
            
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            # 4. Instrucción de IA reforzada para el nombre
            instruccion = f"""{BUSINESS_CONTEXT}
            Cliente actual: {nombre_memoria}
            Historial:\n{historial}
            REGLAS CRÍTICAS:
            - Si sabes el nombre real, usa [ACCION:NOMBRE:NombreReal]. No escribas la palabra 'Nombre'.
            - Si el cliente quiere cancelar, usa [ACCION:CANCELAR].
            - Solo usa un tag por mensaje.
            """
            respuesta_ia = generar_respuesta_ia(instruccion, texto_cliente)

            # 5. Ejecutar y LIMPIAR etiquetas
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_nuevo = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                if nombre_nuevo.lower() != "nombre": # Evitamos guardar la palabra literal
                    nombre_memoria = nombre_nuevo
                    cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_memoria, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            accion_procesada = False
            for tag in ["CANCELAR", "AGENDAR"]:
                tag_full = f"[ACCION:{tag}]"
                if tag_full in respuesta_ia:
                    feedback = ejecutar_logica(id_num, tag, texto_cliente, nombre_memoria)
                    # Eliminamos la etiqueta para que NO la vea el cliente
                    respuesta_ia = respuesta_ia.replace(tag_full, "").strip() + f"\n\n{feedback}"
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