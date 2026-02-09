import os, requests, psycopg2, base64, json, threading, time
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE ENTORNO ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = str(os.environ.get("ADMIN_PHONE")).replace("+", "").strip()
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")
MODELO_IA = "google/gemini-2.5-flash"

# 🛡️ SISTEMA DE CONTROL DE FLUJO
procesados = set() # IDs de mensajes para evitar duplicados de Meta
user_buffers = {} # Debouncing de 7 segundos para clientes

# --- UTILIDADES DE INFRAESTRUCTURA ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_tasas_dinamicas():
    """Consulta fuentes y usa respaldo en DB si fallan"""
    fuentes = ["https://ve.dolarapi.com/v1/dolares/oficial", "https://api.exchangerate-api.com/v4/latest/USD"]
    conn = get_db_connection(); cur = conn.cursor()
    t_usd = None
    for url in fuentes:
        try:
            res = requests.get(url, timeout=5).json()
            t_usd = float(res.get('promedio') or res['rates']['VES'])
            if t_usd > 300:
                cur.execute("UPDATE config SET value = %s WHERE key = 'last_tasa'", (str(t_usd),))
                conn.commit(); break
        except: continue
    if not t_usd:
        cur.execute("SELECT value FROM config WHERE key = 'last_tasa'"); r = cur.fetchone()
        t_usd = float(r['value']) if r else 385.50
    cur.close(); conn.close()
    return {"usd": t_usd}

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

def consultar_ia(instruccion, texto, historial="", img_id=None):
    """Cerebro Gemini con visión y limpieza de JSON"""
    img_b64 = None
    if img_id:
        res = requests.get(f"https://graph.facebook.com/v18.0/{img_id}", headers={"Authorization": f"Bearer {META_TOKEN}"})
        img_data = requests.get(res.json().get('url'), headers={"Authorization": f"Bearer {META_TOKEN}"}).content
        img_b64 = base64.b64encode(img_data).decode('utf-8')

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODELO_IA,
        "messages": [
            {"role": "system", "content": instruccion},
            {"role": "user", "content": [
                {"type": "text", "text": f"HISTORIAL:\n{historial}\n\nACTUAL: {texto}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}} if img_b64 else None
            ]}
        ], "temperature": 0.1
    }
    payload["messages"][1]["content"] = [i for i in payload["messages"][1]["content"] if i]
    return requests.post(url, headers=headers, json=payload).json()['choices'][0]['message']['content']

# --- ⚙️ LÓGICA DE ADMINISTRADOR (Respuesta Única) ---
def ejecutar_comando_admin(msg_text, num, name):
    conn = get_db_connection(); cur = conn.cursor()
    prompt = "ADMIN: [BOT:ON], [BOT:OFF], [CONSULTAR_TASA], [LISTAR_PENDIENTES], [APROBAR:REF]."
    intent = consultar_ia(prompt, msg_text)
    
    msg_final = "✅ Admin: OK." 

    if "[BOT:OFF]" in intent:
        cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
        msg_final = "😴 Bot apagado. No responderé a clientes."
    elif "[BOT:ON]" in intent:
        cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
        msg_final = "🚀 Bot encendido y listo para vender."
    elif "[CONSULTAR_TASA]" in intent:
        t = obtener_tasas_dinamicas()
        msg_final = f"📊 Tasa Actual: {t['usd']} Bs/$"
    elif "[LISTAR_PENDIENTES]" in intent:
        cur.execute("SELECT referencia, monto FROM pagos WHERE estado = 'pendiente'")
        p = cur.fetchall()
        msg_final = "📝 Pagos Pendientes:\n" + "\n".join([f"- Ref {i['referencia']} ({i['monto']} Bs)" for i in p]) if p else "✅ No hay pagos pendientes."
    elif "[APROBAR:" in intent:
        ref = intent.split(":")[1].replace("]", "").strip()
        cur.execute("UPDATE pagos SET estado = 'aprobado' WHERE referencia = %s", (ref,))
        msg_final = f"✅ Pago con referencia {ref} aprobado."

    conn.commit(); cur.close(); conn.close()
    enviar_meta(num, msg_final)

# --- ⚙️ LÓGICA DE CLIENTE (Debouncing y GPS/Dirección) ---
def procesar_flujo_cliente(id_num, num, name):
    time.sleep(1)
    conn = get_db_connection(); cur = conn.cursor()
    buffer = user_buffers.get(id_num, {})
    texto_acumulado = buffer.get("text", "").strip()
    if not texto_acumulado: return
    del user_buffers[id_num]

    t = obtener_tasas_dinamicas()
    cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (id_num,))
    hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
    
    instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\nREGLAS: [AGENDAR], [FINALIZAR] (pide GPS), [CANCELAR]. SI DA DIRECCIÓN, usa [DIRECCION: texto]."""
    res_ia = consultar_ia(instr, texto_acumulado, hist)

    # Procesamiento de Etiquetas
    if "[DIRECCION:" in res_ia:
        dir_cli = res_ia.split("[DIRECCION:")[1].split("]")[0].strip()
        cur.execute("UPDATE pedidos SET direccion = %s WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (dir_cli, id_num))
    if "[AGENDAR]" in res_ia: cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando')", (id_num,))
    if "[FINALIZAR]" in res_ia: 
        cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
        cur.execute("SELECT gps, direccion FROM pedidos WHERE user_id = %s ORDER BY id DESC LIMIT 1", (id_num,))
        p_info = cur.fetchone()
        enviar_meta(ADMIN_PHONE, f"🚨 *NUEVO PEDIDO CONFIRMADO*\n👤 Cliente: {name}\n📝 Orden: {res_ia.split('[FINALIZAR]')[0].strip()}\n📍 Dir: {p_info['direccion'] or 'GPS'}")
    
    conn.commit()
    limpia = res_ia.replace("[AGENDAR]","").replace("[FINALIZAR]","").replace("[CANCELAR]","")
    if "[DIRECCION:" in limpia: limpia = limpia.split("[DIRECCION:")[0]
    
    enviar_meta(num, limpia)
    cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_acumulado, limpia))
    conn.commit(); cur.close(); conn.close()

# --- WEBHOOK PRINCIPAL ---
@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    body = request.json
    if 'entry' in body:
        data = body['entry'][0]['changes'][0]['value']
        if 'messages' in data:
            msg = data['messages'][0]; num = msg['from']; id_num = int(num); msg_id = msg.get('id')

            # 🛡️ FILTRO ANTI-REPETICIÓN
            if msg_id in procesados: return jsonify({"status": "duplicated"}), 200
            procesados.add(msg_id)
            if len(procesados) > 200: procesados.pop()

            name = data.get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
            conn = get_db_connection(); cur = conn.cursor()

            # 👑 MODO ADMIN (Detección de Número y Comandos)
            if str(num).replace("+", "").strip() == ADMIN_PHONE:
                threading.Thread(target=ejecutar_comando_admin, args=(msg.get('text', {}).get('body', ""), num, name)).start()
                return jsonify({"status": "ok"}), 200

            # 🍕 MODO CLIENTE: Verificar si el bot está encendido
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

            # Registro de Cliente y GPS (Instantáneo)
            cur.execute("SELECT id FROM cliente WHERE id = %s", (id_num,))
            if not cur.fetchone():
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s)", (id_num, name, str(num)))
                conn.commit()

            if msg.get('type') == 'location':
                loc = f"{msg['location']['latitude']},{msg['location']['longitude']}"
                cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (loc, id_num))
                conn.commit(); enviar_meta(num, "📍 Ubicación guardada. ¡Gracias!"); return jsonify({"status": "ok"}), 200

            # 📸 PAGOS CON OCR REFORZADO
            if msg.get('type') == 'image':
                res_v = consultar_ia('JSON: {"ref": "text", "monto": float}', "Pago", "", msg['image']['id'])
                try:
                    p = json.loads(res_v.replace("```json","").replace("```","").strip())
                    cur.execute("SELECT id FROM pagos WHERE referencia = %s", (str(p['ref']),))
                    if cur.fetchone(): enviar_meta(num, "❌ Referencia ya registrada.")
                    else:
                        cur.execute("INSERT INTO pagos (user_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (id_num, str(p['ref']), p['monto']))
                        conn.commit()
                        cur.execute("SELECT SUM(monto) as tot FROM pagos WHERE user_id = %s", (id_num,))
                        enviar_meta(num, f"✅ Recibido {p['monto']} Bs. Acumulado: {cur.fetchone()['tot']} Bs.")
                        enviar_meta(ADMIN_PHONE, f"🚨 PAGO: {name} - Ref {p['ref']} ({p['monto']} Bs)")
                except: enviar_meta(num, "⚠️ No pude leer el comprobante. Intenta de nuevo.")
                return jsonify({"status": "ok"}), 200

            # ⏳ DEBOUNCING (Delay 7 seg para texto)
            if msg.get('type') == 'text':
                txt = msg['text']['body']
                if id_num in user_buffers:
                    user_buffers[id_num]["timer"].cancel(); user_buffers[id_num]["text"] += f" {txt}"
                else: user_buffers[id_num] = {"text": txt}
                t_obj = threading.Timer(7.0, procesar_flujo_cliente, args=[id_num, num, name])
                user_buffers[id_num]["timer"] = t_obj; t_obj.start()

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)