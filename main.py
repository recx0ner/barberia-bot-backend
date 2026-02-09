import os, requests, psycopg2, base64
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE INFRAESTRUCTURA ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

MODELO_IA = "google/gemini-2.5-flash" #

# --- UTILIDADES DE BASE DE DATOS ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_historial(id_num):
    """Recupera los últimos 10 mensajes para la memoria del bot"""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (id_num,))
    filas = cur.fetchall()
    cur.close(); conn.close()
    return "".join([f"Usuario: {f['user_input']}\nBot: {f['bot_response']}\n" for f in reversed(filas)])

def guardar_mensaje(id_num, user_in, bot_out):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, user_in, bot_out))
    conn.commit(); cur.close(); conn.close()

def obtener_tasas_dinamicas():
    """Consulta automática de tasas BCV (Dólar y Euro)"""
    try:
        usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=5).json()
        eur = requests.get("https://ve.dolarapi.com/v1/euro/oficial", timeout=5).json()
        return {"usd": float(usd['promedio']), "eur": float(eur['promedio'])}
    except: return None

def ejecutar_logica_db(id_num, accion, extra=None):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        if accion == "AGENDAR":
            cur.execute("INSERT INTO pedidos (user_id, estado, fecha) VALUES (%s, 'confirmando', %s)", (id_num, datetime.now(venezuela_tz)))
        elif accion == "FINALIZAR":
            cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
        elif accion == "CANCELAR":
            cur.execute("UPDATE pedidos SET estado = 'cancelado' WHERE user_id = %s", (id_num,))
        elif accion == "GPS":
            cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado IN ('confirmando', 'esperando_pago')", (extra, id_num))
            cur.execute("INSERT INTO ubicaciones (user_id, latitud, longitud) VALUES (%s, %s, %s)", (id_num, extra.split(',')[0], extra.split(',')[1]))
        conn.commit()
    finally: cur.close(); conn.close()

# --- NÚCLEO IA ---
def consultar_ia(instruccion, texto, historial="", img_id=None):
    img_b64 = None
    if img_id:
        res = requests.get(f"https://graph.facebook.com/v18.0/{img_id}", headers={"Authorization": f"Bearer {META_TOKEN}"})
        img_data = requests.get(res.json().get('url'), headers={"Authorization": f"Bearer {META_TOKEN}"}).content
        img_b64 = base64.b64encode(img_data).decode('utf-8')

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    texto_completo = f"MEMORIA:\n{historial}\n\nMENSAJE ACTUAL: {texto}"
    
    contenido = [{"type": "text", "text": texto_completo}]
    if img_b64: contenido.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
    
    payload = {"model": MODELO_IA, "messages": [{"role": "system", "content": instruccion}, {"role": "user", "content": contenido}], "temperature": 0.2}
    return requests.post(url, headers=headers, json=payload).json()['choices'][0]['message']['content']

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    data = request.json['entry'][0]['changes'][0]['value']
    if 'messages' in data:
        msg = data['messages'][0]; numero = msg['from']; id_num = int(numero)
        conn = get_db_connection(); cur = conn.cursor()

        # 🛡️ MODO ADMIN
        if numero == ADMIN_PHONE:
            intent = consultar_ia("Tags: [APROBAR:REF], [LISTAR_PENDIENTES], [BOT:ON], [BOT:OFF].", msg.get('text', {}).get('body', ""))
            if "[APROBAR:" in intent:
                ref = intent.split(":")[1].replace("]", "").strip()
                cur.execute("UPDATE pagos SET estado = 'aprobado' WHERE referencia = %s", (ref,))
                conn.commit(); enviar_meta(ADMIN_PHONE, f"✅ Pago {ref} aprobado.")
            elif "[LISTAR_PENDIENTES]" in intent:
                cur.execute("SELECT referencia, monto FROM pagos WHERE estado = 'pendiente'")
                p = cur.fetchall()
                enviar_meta(ADMIN_PHONE, "📝 Pendientes:\n" + "\n".join([f"- {i['referencia']} ({i['monto']} Bs)" for i in p]) if p else "✅ Todo al día.")
            elif "[BOT:OFF]" in intent:
                cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                conn.commit(); enviar_meta(ADMIN_PHONE, "😴 Bot apagado.")
            elif "[BOT:ON]" in intent:
                cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
                conn.commit(); enviar_meta(ADMIN_PHONE, "🚀 Bot encendido.")
            return jsonify({"status": "ok"}), 200

        # 🍕 MODO CLIENTE
        cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
        if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

        # Auto-registro
        cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
        if not cur.fetchone():
            cur.execute("INSERT INTO cliente (id, nombre) VALUES (%s, 'Nuevo')", (id_num,))
            conn.commit()

        # Recepción de Ubicación
        if msg.get('type') == 'location':
            loc = msg['location']
            ejecutar_logica_db(id_num, "GPS", f"{loc['latitude']},{loc['longitude']}")
            enviar_meta(numero, "📍 Ubicación guardada. ¡Tu pizza va en camino!")
            return jsonify({"status": "ok"}), 200

        tasas = obtener_tasas_dinamicas()
        if not tasas:
            enviar_meta(numero, "⚠️ Error técnico con las tasas. Intenta en un momento.")
            return jsonify({"status": "error"}), 200

        historial = obtener_historial(id_num)
        instruccion = f"""{BUSINESS_CONTEXT}\n
        TASAS BCV: USD: {tasas['usd']} | EUR: {tasas['eur']}\n
        REGLAS:
        1. Usa [AGENDAR] al iniciar pedido.
        2. Si el cliente no quiere nada más, resume la orden, da el total ($ y Bs) y PIDE LA UBICACIÓN POR GPS de WhatsApp. Usa [FINALIZAR].
        3. Usa [CANCELAR] si desiste."""
        
        texto_cli = msg.get('text', {}).get('body', "Archivo")
        img_id = msg.get('image', {}).get('id') if msg.get('type') == 'image' else None
        
        respuesta_ia = consultar_ia(instruccion, texto_cli, historial, img_id)

        if "[AGENDAR]" in respuesta_ia: ejecutar_logica_db(id_num, "AGENDAR")
        if "[FINALIZAR]" in respuesta_ia: ejecutar_logica_db(id_num, "FINALIZAR")
        if "[CANCELAR]" in respuesta_ia: ejecutar_logica_db(id_num, "CANCELAR")
        
        limpia = respuesta_ia.replace("[AGENDAR]","").replace("[FINALIZAR]","").replace("[CANCELAR]","")
        enviar_meta(numero, limpia)
        guardar_mensaje(id_num, texto_cli, limpia)
        
        cur.close(); conn.close()
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)