import os, requests, traceback, psycopg2, base64
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
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # Tu número de prueba
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

MODELO_IA = "google/gemini-2.5-flash"

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def descargar_imagen_meta(image_id):
    try:
        res = requests.get(f"https://graph.facebook.com/v18.0/{image_id}", headers={"Authorization": f"Bearer {META_TOKEN}"})
        url_descarga = res.json().get('url')
        img_res = requests.get(url_descarga, headers={"Authorization": f"Bearer {META_TOKEN}"})
        return base64.b64encode(img_res.content).decode('utf-8')
    except: return None

def generar_respuesta_ia(instruccion, texto_usuario, img_b64=None):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        contenido = [{"type": "text", "text": texto_usuario}]
        if img_b64:
            contenido.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
        payload = {"model": MODELO_IA, "messages": [{"role": "system", "content": instruccion}, {"role": "user", "content": contenido}]}
        response = requests.post(url, headers=headers, json=payload)
        return response.json()['choices'][0]['message']['content'].strip()
    except: return "Error de conexión."

def ejecutar_logica_negocio(id_num, accion, texto_cliente="", nombre_actual="Cliente", extra_data=None):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_actual, str(id_num)))
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')", (id_num, nombre_actual, texto_cliente[:200], datetime.now(venezuela_tz)))
            return "✅ Pedido agendado. Esperando pago."
        elif accion == "PAGO" and extra_data:
            ref, monto, banco = extra_data[0].strip(), extra_data[1].strip(), extra_data[2].strip()
            if ref.lower() == "ref": return "⚠️ Datos inválidos."
            cur.execute("INSERT INTO pagos (user_id, referencia, monto, banco, estado) VALUES (%s, %s, %s, %s, 'pendiente')", (id_num, ref, monto, banco))
            conn.commit()
            msg_admin = f"⚠️ *PAGO POR VERIFICAR*\n👤 Cliente: {nombre_actual}\n💰 Monto: {monto}\n🏦 Banco: {banco}\n🔢 Ref: {ref}\n\nResponde 'sí' para confirmar."
            enviar_meta(ADMIN_PHONE, msg_admin)
            return f"⏳ Referencia {ref} de {banco} recibida. Validaremos el ingreso en Bs. y te avisaré."
        conn.commit()
    except: conn.rollback(); return "✅ Procesado."
    finally: cur.close(); conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]; numero = msg['from']; id_num = int(numero)
            texto_cliente = msg.get('text', {}).get('body', "").strip() if msg.get('type') == 'text' else "(Imagen)"
            img_data = descargar_imagen_meta(msg['image']['id']) if msg.get('type') == 'image' else None

            conn = get_db_connection(); cur = conn.cursor()
            
            # --- 🛡️ LÓGICA EXCLUSIVA DE ADMINISTRADOR ---
            if ADMIN_PHONE and numero == ADMIN_PHONE:
                # Buscamos el último pago pendiente en Neon
                cur.execute("SELECT p.referencia, p.user_id, c.nombre FROM pagos p JOIN cliente c ON p.user_id = c.id WHERE p.estado = 'pendiente' ORDER BY p.fecha_pago DESC LIMIT 1")
                pago = cur.fetchone()
                
                instruccion_admin = f"Asistente interno. Último pago pendiente de {pago['nombre'] if pago else 'nadie'} Ref: {pago['referencia'] if pago else '0'}. Si el admin confirma (si, ok, listo, ya llego), responde SOLO [CONFIRMAR:REF]. Si pide apagar [BOT:OFF], si encender [BOT:ON]. NO saludes."
                res_admin = generar_respuesta_ia(instruccion_admin, texto_cliente)

                if "[CONFIRMAR:REF]" in res_admin and pago:
                    cur.execute("UPDATE pagos SET estado = 'confirmado' WHERE referencia = %s", (pago['referencia'],))
                    cur.execute("UPDATE citas SET estado = 'pagado' WHERE user_id = %s AND estado = 'pendiente'", (pago['user_id'],))
                    conn.commit()
                    enviar_meta(str(pago['user_id']), f"✅ *¡Pago en Bs. Confirmado!* Tu pizza ya está en el horno. 🍕")
                    enviar_meta(ADMIN_PHONE, f"👌 Confirmado el pago de {pago['nombre']} (Ref: {pago['referencia']}).")
                elif "[BOT:OFF]" in res_admin:
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'"); conn.commit()
                    enviar_meta(numero, "😴 Bot desactivado.")
                elif "[BOT:ON]" in res_admin:
                    cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'"); conn.commit()
                    enviar_meta(numero, "🤖 Bot reactivado.")
                
                # Cierre inmediato para evitar el bucle
                cur.close(); conn.close()
                return jsonify({"status": "admin_ok"}), 200

            # --- 🍕 LÓGICA DE CLIENTE ---
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone(); nombre_actual = cli['nombre'] if cli and cli['nombre'].lower() != "nombre" else "Desconocido"
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            instruccion = f"{BUSINESS_CONTEXT}\nCliente: {nombre_actual}\nHistorial:\n{historial}\nEtiquetas: [ACCION:NOMBRE:Nombre], [ACCION:AGENDAR], [ACCION:PAGO:Ref:Monto:Banco]"
            respuesta_ia = generar_respuesta_ia(instruccion, texto_cliente, img_data)

            # Limpieza de etiquetas
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_nuevo = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_nuevo, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:PAGO:" in respuesta_ia:
                datos = respuesta_ia.split("[ACCION:PAGO:")[1].split("]")[0].split(":")
                if len(datos) >= 3:
                    feedback = ejecutar_logica_negocio(id_num, "PAGO", extra_data=datos, nombre_actual=nombre_actual)
                    respuesta_ia = respuesta_ia.split("[ACCION:PAGO:")[0].strip() + f"\n\n{feedback}"

            for tag in ["CANCELAR", "AGENDAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto_cliente, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_cliente, respuesta_ia))
            conn.commit(); cur.close(); conn.close()
        return jsonify({"status": "ok"}), 200
    except: traceback.print_exc(); return jsonify({"status": "error"}), 500

if __name__ == "__main__": app.run(host='0.0.0.0', port=10000)