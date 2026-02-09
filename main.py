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
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")
MODELO_IA = "google/gemini-2.5-flash"

# 🧠 BUFFER PARA DELAY (DEBOUNCING)
user_buffers = {} # {id_num: {"text": "", "timer": Timer}}

# --- UTILIDADES DE BASE DE DATOS ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_tasas_dinamicas():
    """Consulta APIs de tasas con respaldo en DB"""
    fuentes = ["https://ve.dolarapi.com/v1/dolares/oficial", "https://api.exchangerate-api.com/v4/latest/USD"]
    conn = get_db_connection(); cur = conn.cursor()
    t_usd = None
    for url in fuentes:
        try:
            res = requests.get(url, timeout=5).json()
            t_usd = float(res.get('promedio') or res['rates']['VES'])
            if t_usd > 300: #
                cur.execute("UPDATE config SET value = %s WHERE key = 'last_tasa'", (str(t_usd),))
                conn.commit(); break
        except: continue
    if not t_usd:
        cur.execute("SELECT value FROM config WHERE key = 'last_tasa'"); r = cur.fetchone()
        t_usd = float(r['value']) if r else 385.50
    cur.close(); conn.close()
    return {"usd": t_usd}

# --- NÚCLEO IA Y MENSAJERÍA ---
def consultar_ia(instruccion, texto, historial="", img_id=None):
    """Cerebro Gemini para texto y visión reforzada"""
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

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

# --- ⚙️ PROCESAMIENTO DIFERIDO (LOGICA CLIENTE) ---
def procesar_flujo_cliente(id_num, num, name):
    """Agrupa mensajes y procesa tras 7 seg de silencio"""
    time.sleep(1)
    conn = get_db_connection(); cur = conn.cursor()
    buffer = user_buffers.get(id_num, {})
    texto_acumulado = buffer.get("text", "").strip()
    if not texto_acumulado: return
    del user_buffers[id_num]

    t = obtener_tasas_dinamicas()
    cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (id_num,))
    hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
    
    instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\n
    REGLAS:
    1. Usa [AGENDAR] al iniciar un pedido.
    2. Usa [FINALIZAR] al cerrar (da total $ y Bs, pide GPS).
    3. Usa [CANCELAR] si el cliente desiste.
    4. SI EL CLIENTE ESCRIBE UNA DIRECCIÓN, usa [DIRECCION: texto de la dirección]."""
    
    res_ia = consultar_ia(instr, texto_acumulado, hist)

    # Ejecución de etiquetas en DB
    if "[DIRECCION:" in res_ia:
        dir_text = res_ia.split("[DIRECCION:")[1].split("]")[0].strip()
        cur.execute("UPDATE pedidos SET direccion = %s WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (dir_text, id_num))
    if "[AGENDAR]" in res_ia: cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando')", (id_num,))
    if "[FINALIZAR]" in res_ia: cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
    if "[CANCELAR]" in res_ia: cur.execute("UPDATE pedidos SET estado = 'cancelado' WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (id_num,))
    
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
            msg = data['messages'][0]; num = msg['from']; id_num = int(num)
            name = body['entry'][0]['changes'][0]['value'].get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
            conn = get_db_connection(); cur = conn.cursor()

            # 🛡️ MODO ADMIN REFORZADO
            num_lim = str(num).replace("+", "").strip()
            adm_lim = str(ADMIN_PHONE).replace("+", "").strip()
            if num_lim == adm_lim:
                res_adm = consultar_ia("ADMIN: [BOT:ON], [BOT:OFF], [CONSULTAR_TASA], [LISTAR_PENDIENTES], [APROBAR:REF].", msg.get('text', {}).get('body', ""))
                if "[BOT:OFF]" in res_adm: cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'")
                elif "[BOT:ON]" in res_adm: cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'")
                elif "[CONSULTAR_TASA]" in res_adm: 
                    t = obtener_tasas_dinamicas(); enviar_meta(num, f"📊 Tasa Actual: {t['usd']} Bs/$")
                elif "[APROBAR:" in res_adm:
                    ref = res_adm.split(":")[1].replace("]", "").strip()
                    cur.execute("UPDATE pagos SET estado = 'aprobado' WHERE referencia = %s", (ref,))
                conn.commit(); enviar_meta(num, "✅ Comando Admin ejecutado."); return jsonify({"status": "ok"}), 200

            # 🍕 MODO CLIENTE: Verificar si bot está encendido
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false': return jsonify({"status": "off"}), 200

            # Auto-registro de Cliente
            cur.execute("SELECT id FROM cliente WHERE id = %s", (id_num,))
            if not cur.fetchone():
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s)", (id_num, name, str(num)))
                conn.commit()

            # 📍 UBICACIÓN GPS
            if msg.get('type') == 'location':
                loc = f"{msg['location']['latitude']},{msg['location']['longitude']}"
                cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (loc, id_num))
                conn.commit(); enviar_meta(num, "📍 Ubicación GPS guardada."); return jsonify({"status": "ok"}), 200

            # 📸 PAGOS Y ANTIFRAUDE
            if msg.get('type') == 'image':
                res_v = consultar_ia('JSON: {"ref": "text", "monto": float}', "Pago", "", msg['image']['id'])
                try:
                    p = json.loads(res_v.replace("```json","").replace("```","").strip())
                    cur.execute("SELECT id FROM pagos WHERE referencia = %s", (str(p['ref']),))
                    if cur.fetchone(): enviar_meta(num, "❌ Referencia duplicada detectada.")
                    else:
                        cur.execute("INSERT INTO pagos (user_id, referencia, monto) VALUES (%s, %s, %s)", (id_num, str(p['ref']), p['monto']))
                        conn.commit()
                        cur.execute("SELECT SUM(monto) as tot FROM pagos WHERE user_id = %s", (id_num,))
                        enviar_meta(num, f"✅ Recibido {p['monto']} Bs. Acumulado: {cur.fetchone()['tot']} Bs.")
                        enviar_meta(ADMIN_PHONE, f"🚨 PAGO: {name} - Ref {p['ref']}")
                except: enviar_meta(num, "⚠️ Error procesando el pago.")
                return jsonify({"status": "ok"}), 200

            # ⏳ DEBOUNCING (Delay de 7 seg)
            if msg.get('type') == 'text':
                txt = msg['text']['body']
                if id_num in user_buffers:
                    user_buffers[id_num]["timer"].cancel()
                    user_buffers[id_num]["text"] += f" {txt}"
                else: user_buffers[id_num] = {"text": txt}
                
                t_obj = threading.Timer(7.0, procesar_flujo_cliente, args=[id_num, num, name])
                user_buffers[id_num]["timer"] = t_obj
                t_obj.start()

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)