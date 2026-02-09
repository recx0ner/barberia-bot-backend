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

# Motor exclusivo: Gemini 2.5 Flash
MODELO_IA = "google/gemini-2.5-flash" 

# --- CONEXIÓN Y UTILIDADES ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_tasas_bcv():
    """Consulta automática de tasas oficiales del BCV"""
    try:
        usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=5).json()
        return {"usd": float(usd['promedio']), "eur": 65.0}
    except: return {"usd": 55.0, "eur": 60.0}

def ejecutar_logica_db(id_num, accion, extra=None):
    """Maneja las escrituras en las tablas de Pedidos y Citas"""
    conn = get_db_connection(); cur = conn.cursor()
    try:
        if accion == "AGENDAR":
            cur.execute("INSERT INTO pedidos (user_id, estado, fecha) VALUES (%s, 'pendiente', %s)", (id_num, datetime.now(venezuela_tz)))
        elif accion == "CANCELAR":
            cur.execute("UPDATE pedidos SET estado = 'cancelado' WHERE user_id = %s AND estado = 'pendiente'", (id_num,))
        elif accion == "GPS":
            cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado = 'pendiente'", (extra, id_num))
        conn.commit()
    finally: cur.close(); conn.close()

# --- NÚCLEO IA CON VISIÓN ---
def consultar_ia(instruccion, texto, img_id=None):
    img_b64 = None
    if img_id:
        res = requests.get(f"https://graph.facebook.com/v18.0/{img_id}", headers={"Authorization": f"Bearer {META_TOKEN}"})
        img_data = requests.get(res.json().get('url'), headers={"Authorization": f"Bearer {META_TOKEN}"}).content
        img_b64 = base64.b64encode(img_data).decode('utf-8')

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    contenido = [{"type": "text", "text": texto}]
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

        # 🛡️ LÓGICA DE ADMIN (AUDITORÍA Y CONTROL)
        if numero == ADMIN_PHONE:
            texto_admin = msg.get('text', {}).get('body', "")
            instr_admin = "Detecta intención: [APROBAR:REF], [LISTAR_PENDIENTES], [BOT:ON], [BOT:OFF]."
            intent = consultar_ia(instr_admin, texto_admin)

            if "[APROBAR:" in intent:
                ref = intent.split(":")[1].replace("]", "").strip()
                cur.execute("UPDATE pagos SET estado = 'aprobado' WHERE referencia = %s", (ref,))
                conn.commit(); enviar_meta(ADMIN_PHONE, f"✅ Pago {ref} aprobado con éxito.")
            
            elif "[LISTAR_PENDIENTES]" in intent:
                cur.execute("SELECT referencia, monto FROM pagos WHERE estado = 'pendiente' LIMIT 10")
                pendientes = cur.fetchall()
                if pendientes:
                    msg_p = "📝 *PAGOS PENDIENTES:* \n" + "\n".join([f"- Ref: {p['referencia']} ({p['monto']} Bs)" for p in pendientes])
                else:
                    msg_p = "✅ *Jefe, no hay pagos pendientes por aprobar.*"
                enviar_meta(ADMIN_PHONE, msg_p)

            elif "[BOT:OFF]" in intent:
                cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                conn.commit(); enviar_meta(ADMIN_PHONE, "😴 Bot apagado. Estás al mando manual.")
            
            return jsonify({"status": "ok"}), 200

        # 🍕 LÓGICA DE CLIENTE
        cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
        if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

        # Auto-registro
        cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
        if not cur.fetchone():
            cur.execute("INSERT INTO cliente (id, nombre) VALUES (%s, 'Nuevo')", (id_num,))
            conn.commit()

        tasas = obtener_tasas_bcv()
        instruccion = f"{BUSINESS_CONTEXT}\nTasa: {tasas['usd']} Bs.\nUsa etiquetas: [AGENDAR], [CANCELAR], [PAGO:ref]."
        
        texto_cli = msg.get('text', {}).get('body', "Archivo")
        img_id = msg.get('image', {}).get('id') if msg.get('type') == 'image' else None
        
        respuesta_ia = consultar_ia(instruccion, texto_cli, img_id)

        if "[AGENDAR]" in respuesta_ia: ejecutar_logica_db(id_num, "AGENDAR")
        if "[CANCELAR]" in respuesta_ia: ejecutar_logica_db(id_num, "CANCELAR")
        
        enviar_meta(numero, respuesta_ia.replace("[AGENDAR]","").replace("[CANCELAR]",""))
        cur.close(); conn.close()
        
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)