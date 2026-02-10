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
# Limpieza de número admin para validación blindada
ADMIN_PHONE = str(os.environ.get("ADMIN_PHONE")).replace("+", "").strip()
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")
MODELO_IA = "google/gemini-2.5-flash" 

# 🛡️ SISTEMAS DE CONTROL
procesados = set() 
user_buffers = {} 

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# --- 🏦 FINANZAS: EXCHANGERATE-API ---
def obtener_tasas_dinamicas():
    conn = get_db_connection(); cur = conn.cursor()
    tasas = {"usd": 385.50, "eur": 415.00}
    try:
        # Consulta USD y EUR
        r_usd = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5).json()
        tasas['usd'] = float(r_usd['rates']['VES'])
        r_eur = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=5).json()
        tasas['eur'] = float(r_eur['rates']['VES'])
        # Respaldo en Neon
        cur.execute("UPDATE config SET value = %s WHERE key = 'last_tasa'", (str(tasas['usd']),))
        cur.execute("INSERT INTO config (key, value) VALUES ('last_tasa_eur', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(tasas['eur']),))
        conn.commit()
    except:
        cur.execute("SELECT key, value FROM config WHERE key IN ('last_tasa', 'last_tasa_eur')")
        for r in cur.fetchall():
            if r['key'] == 'last_tasa': tasas['usd'] = float(r['value'])
            if r['key'] == 'last_tasa_eur': tasas['eur'] = float(r['value'])
    finally: cur.close(); conn.close()
    return tasas

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

def consultar_ia(instruccion, texto, historial="", img_id=None):
    img_b64 = None
    if img_id:
        res = requests.get(f"https://graph.facebook.com/v18.0/{img_id}", headers={"Authorization": f"Bearer {META_TOKEN}"})
        img_data = requests.get(res.json().get('url'), headers={"Authorization": f"Bearer {META_TOKEN}"}).content
        img_b64 = base64.b64encode(img_data).decode('utf-8')
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODELO_IA, "messages": [{"role": "system", "content": instruccion}, {"role": "user", "content": [{"type": "text", "text": f"{historial}\n{texto}"}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}} if img_b64 else None]}], "temperature": 0.1}
    payload["messages"][1]["content"] = [i for i in payload["messages"][1]["content"] if i]
    return requests.post(url, headers=headers, json=payload).json()['choices'][0]['message']['content']

# --- 👑 LÓGICA DE ADMINISTRADOR ---
def ejecutar_comando_admin(msg_text, num):
    conn = get_db_connection(); cur = conn.cursor()
    prompt = "ADMIN: [BOT:ON], [BOT:OFF], [CONSULTAR_TASA], [REVISAR:nombre], [LISTAR_PENDIENTES], [APROBAR:REF]."
    intent = consultar_ia(prompt, msg_text)
    msg_final = "✅ Admin: Comando procesado."

    if "[REVISAR:" in intent:
        target = intent.split(":")[1].replace("]", "").strip()
        cur.execute("SELECT id, nombre FROM cliente WHERE nombre ILIKE %s LIMIT 1", (f"%{target}%",))
        cliente = cur.fetchone()
        if cliente:
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (cliente['id'],))
            hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
            resumen = consultar_ia("Resume qué tiene este cliente en su carrito actualmente.", hist)
            msg_final = f"🧐 *Carrito de {cliente['nombre']}:*\n{resumen}"
        else: msg_final = f"❌ No encontré al cliente '{target}'."
    elif "[CONSULTAR_TASA]" in intent:
        t = obtener_tasas_dinamicas()
        msg_final = f"📊 *Tasas:* 💵 USD: {t['usd']} | 💶 EUR: {t['eur']}"
    elif "[BOT:OFF]" in intent:
        cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
        msg_final = "😴 Bot apagado."
    elif "[BOT:ON]" in intent:
        cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
        msg_final = "🚀 Bot encendido."
    elif "[LISTAR_PENDIENTES]" in intent:
        cur.execute("SELECT referencia, monto FROM pagos WHERE estado = 'pendiente'")
        p = cur.fetchall()
        msg_final = "📝 Pendientes:\n" + "\n".join([f"- Ref {i['referencia']} ({i['monto']} Bs)" for i in p]) if p else "✅ Todo al día."

    conn.commit(); cur.close(); conn.close()
    enviar_meta(num, msg_final)

# --- 🍕 LÓGICA DE CLIENTE (CON DELAY 5 SEG Y NOMBRE REAL) ---
def procesar_flujo_cliente(id_num, num, name_wa, texto_extra=None):
    time.sleep(1)
    conn = get_db_connection(); cur = conn.cursor()
    buffer = user_buffers.get(id_num, {"text": ""})
    texto_acum = (buffer.get("text", "") + " " + (texto_extra or "")).strip()
    if not texto_acum: return
    if id_num in user_buffers: del user_buffers[id_num]

    t = obtener_tasas_dinamicas()
    cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (id_num,))
    hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
    
    instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\n
    REGLAS CRÍTICAS:
    - [NOMBRE: nombre y apellido] si el cliente se identifica. PRIORIDAD sobre apodos.
    - [AGENDAR] al primer producto.
    - [CANCELAR] para anular.
    - [FINALIZAR] al confirmar orden o recibir GPS. Dar total ($ y Bs) y métodos de pago.
    - [DIRECCION: texto] si la escriben."""
    
    res_ia = consultar_ia(instr, texto_acum, hist)

    # Actualizaciones en Neon
    if "[NOMBRE:" in res_ia:
        nombre_real = res_ia.split("[NOMBRE:")[1].split("]")[0].strip()
        cur.execute("UPDATE cliente SET nombre = %s WHERE id = %s", (nombre_real, id_num))
    if "[AGENDAR]" in res_ia: cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando')", (id_num,))
    if "[CANCELAR]" in res_ia: cur.execute("UPDATE pedidos SET estado = 'cancelado' WHERE user_id = %s", (id_num,))
    if "[FINALIZAR]" in res_ia: 
        cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
        cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
        nombre_final = cur.fetchone()['nombre']
        enviar_meta(ADMIN_PHONE, f"🚨 NUEVO PEDIDO: {nombre_final}\nResumen: {res_ia.split('[FINALIZAR]')[0].strip()}")
    
    conn.commit()
    limpia = res_ia.replace("[AGENDAR]","").replace("[FINALIZAR]","").replace("[CANCELAR]","")
    if "[NOMBRE:" in limpia: limpia = limpia.split("[NOMBRE:")[0]
    
    enviar_meta(num, limpia)
    cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_acum, limpia))
    conn.commit(); cur.close(); conn.close()

# --- WEBHOOK ---
@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    body = request.json
    if 'entry' in body:
        data = body['entry'][0]['changes'][0]['value']
        if 'messages' in data:
            msg = data['messages'][0]; num = msg['from']; id_num = int(num); msg_id = msg.get('id')

            # 🛡️ FILTRO ANTI-REPETICIÓN
            if msg_id in procesados: return jsonify({"status": "ok"}), 200
            procesados.add(msg_id)

            # 👑 MODO ADMIN REFORZADO
            if str(num).replace("+", "").strip() == ADMIN_PHONE:
                threading.Thread(target=ejecutar_comando_admin, args=(msg.get('text', {}).get('body', ""), num)).start()
                return jsonify({"status": "ok"}), 200

            # 🍕 MODO CLIENTE: Verificar activo
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

            name_wa = data.get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
            cur.execute("SELECT id FROM cliente WHERE id = %s", (id_num,))
            if not cur.fetchone():
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s)", (id_num, name_wa, str(num)))
                conn.commit()

            # Forzar cierre tras GPS
            if msg.get('type') == 'location':
                loc = f"{msg['location']['latitude']},{msg['location']['longitude']}"
                cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado = 'confirmando'", (loc, id_num))
                conn.commit()
                threading.Thread(target=procesar_flujo_cliente, args=(id_num, num, name_wa, "He enviado mi ubicación GPS.")).start()
                return jsonify({"status": "ok"}), 200

            # OCR y Antifraude
            if msg.get('type') == 'image':
                res_v = consultar_ia('JSON: {"ref": "text", "monto": float}', "Pago", "", msg['image']['id'])
                try:
                    p = json.loads(res_v.replace("```json","").replace("```","").strip())
                    cur.execute("SELECT id FROM pagos WHERE referencia = %s", (str(p['ref']),))
                    if cur.fetchone(): enviar_meta(num, "❌ Referencia ya registrada.")
                    else:
                        cur.execute("INSERT INTO pagos (user_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (id_num, str(p['ref']), p['monto']))
                        conn.commit()
                        enviar_meta(num, f"✅ Recibido Ref {p['ref']} por {p['monto']} Bs.")
                except: enviar_meta(num, "⚠️ Error en comprobante.")
                return jsonify({"status": "ok"}), 200

            # ⏳ DELAY CONFIGURADO A 5 SEGUNDOS
            if msg.get('type') == 'text':
                txt = msg['text']['body']
                if id_num in user_buffers:
                    user_buffers[id_num]["timer"].cancel(); user_buffers[id_num]["text"] += f" {txt}"
                else: user_buffers[id_num] = {"text": txt}
                threading.Timer(5.0, procesar_flujo_cliente, args=[id_num, num, name_wa]).start()

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)