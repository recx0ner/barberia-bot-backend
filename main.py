import os, requests, traceback, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

MODELO_IA = "google/gemini-2.5-flash" #

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def generar_respuesta_ia(instruccion, texto_usuario):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": MODELO_IA,
            "messages": [
                {"role": "system", "content": instruccion},
                {"role": "user", "content": texto_usuario}
            ]
        }
        response = requests.post(url, headers=headers, json=payload)
        return response.json()['choices'][0]['message']['content'].strip()
    except:
        return "Lo siento, tuve un problema técnico. ¿Puedes repetir?"

def ejecutar_logica_negocio(id_num, accion, texto_cliente="", nombre_actual="Cliente", extra_data=None):
    """Lógica central de base de datos en Neon"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", 
                    (id_num, nombre_actual, str(id_num)))
        
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')",
                        (id_num, nombre_actual, texto_cliente[:200], datetime.now(venezuela_tz)))
            return "✅ Pedido agendado en espera de pago."
            
        elif accion == "CANCELAR":
            cur.execute("UPDATE citas SET estado = 'cancelado' WHERE user_id = %s AND estado = 'pendiente'", (id_num,))
            return "🗑️ Pedido cancelado."
            
        elif accion == "PAGO" and extra_data:
            ref, monto = extra_data[0], extra_data[1]
            cur.execute("INSERT INTO pagos (user_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", 
                        (id_num, ref, monto))
            conn.commit()
            # Notificar al Administrador
            msg_admin = f"⚠️ *NUEVO PAGO RECIBIDO*\n👤 Cliente: {nombre_actual}\n💰 Monto: {monto}\n🔢 Ref: {ref}\n\nPara confirmar responde:\n`/PAGOK_{ref}`"
            enviar_meta(ADMIN_PHONE, msg_admin)
            return f"⏳ Referencia {ref} enviada a revisión. Te avisaré cuando el administrador la confirme."
            
        conn.commit()
    except:
        conn.rollback()
        return "✅ Procesado."
    finally:
        cur.close(); conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            if msg.get('type') != 'text': return jsonify({"status": "ignored"}), 200

            numero = msg['from']
            id_num = int(numero)
            texto_cliente = msg['text']['body'].strip()

            conn = get_db_connection()
            cur = conn.cursor()
            
            # --- LÓGICA DE ADMINISTRADOR ---
            if ADMIN_PHONE and numero == ADMIN_PHONE:
                # Comandos de Estado
                if "/BOT_OFF" in texto_cliente.upper():
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "😴 Bot desactivado.")
                    return jsonify({"status": "ok"}), 200
                elif "/BOT_ON" in texto_cliente.upper():
                    cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
                    conn.commit()
                    enviar_meta(numero, "🤖 Bot activado.")
                    return jsonify({"status": "ok"}), 200
                
                # Comando de Confirmación de Pago
                elif "/PAGOK_" in texto_cliente.upper():
                    ref_ok = texto_cliente.split("_")[1].strip()
                    cur.execute("UPDATE pagos SET estado = 'confirmado' WHERE referencia = %s RETURNING user_id", (ref_ok,))
                    res_pago = cur.fetchone()
                    if res_pago:
                        user_id_pago = res_pago['user_id']
                        cur.execute("UPDATE citas SET estado = 'pagado' WHERE user_id = %s AND estado = 'pendiente'", (user_id_pago,))
                        conn.commit()
                        enviar_meta(str(user_id_pago), f"✅ *¡Pago Confirmado!* Tu referencia {ref_ok} ha sido validada. ¡Tu pizza va en camino! 🍕")
                        enviar_meta(ADMIN_PHONE, f"👌 Pago {ref_ok} verificado. Cliente notificado.")
                    else:
                        enviar_meta(ADMIN_PHONE, f"❌ No encontré la referencia {ref_ok} en la base de datos.")
                    return jsonify({"status": "ok"}), 200

            # --- LÓGICA DE CLIENTE ---
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone()
            nombre_actual = cli['nombre'] if cli and cli['nombre'].lower() != "nombre" else "Desconocido"
            
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            instruccion = f"{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nHistorial:\n{historial}\nEtiquetas: [ACCION:NOMBRE:Nombre], [ACCION:AGENDAR], [ACCION:CANCELAR], [ACCION:PAGO:Ref:Monto]"
            respuesta_ia = generar_respuesta_ia(instruccion, texto_cliente)

            # Limpiar etiquetas
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_actual = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_actual, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:PAGO:" in respuesta_ia:
                datos = respuesta_ia.split("[ACCION:PAGO:")[1].split("]")[0].split(":")
                feedback = ejecutar_logica_negocio(id_num, "PAGO", extra_data=datos, nombre_actual=nombre_actual)
                respuesta_ia = respuesta_ia.split("[ACCION:PAGO:")[0].strip() + f"\n\n{feedback}"

            for tag in ["CANCELAR", "AGENDAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto_cliente, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_cliente, respuesta_ia))
            conn.commit()
            cur.close(); conn.close()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc(); return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)