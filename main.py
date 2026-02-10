import os, requests, psycopg2, base64, json, threading, time, re
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- CONFIGURACIÓN ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = str(os.environ.get("ADMIN_PHONE")).replace("+", "").strip()
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")
MODELO_IA = "google/gemini-2.5-flash"

procesados = set() 
user_buffers = {} 

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_tasas_dinamicas():
    conn = get_db_connection(); cur = conn.cursor()
    tasas = {"usd": 385.50, "eur": 415.00}
    try:
        r_usd = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5).json()
        tasas['usd'] = float(r_usd['rates']['VES'])
        r_eur = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=5).json()
        tasas['eur'] = float(r_eur['rates']['VES'])
    except: pass
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
    res = requests.post(url, headers=headers, json=payload).json()
    return res['choices'][0]['message']['content']

# --- 👑 LÓGICA ADMIN (Corregida) ---
def ejecutar_comando_admin(msg_text, num):
    conn = get_db_connection(); cur = conn.cursor()
    prompt = "ADMIN: [BOT:ON], [BOT:OFF], [CONSULTAR_TASA], [MODO_PRUEBA:ON], [LISTAR_PENDIENTES]."
    intent = consultar_ia(prompt, msg_text)
    
    if "[MODO_PRUEBA:ON]" in intent:
        cur.execute("UPDATE config SET value = 'true' WHERE key = 'test_mode'")
        enviar_meta(num, "🧪 *MODO PRUEBA ACTIVADO*")
    elif "[CONSULTAR_TASA]" in intent:
        t = obtener_tasas_dinamicas()
        enviar_meta(num, f"📊 Tasa: {t['usd']} Bs/$")
    else:
        enviar_meta(num, "✅ Comando Admin OK.")
    
    conn.commit(); cur.close(); conn.close()

# --- 🍕 LÓGICA CLIENTE (Flujo Reparado) ---
def procesar_flujo_cliente(id_num, num, name_wa, texto_extra=None):
    time.sleep(1)
    conn = get_db_connection(); cur = conn.cursor()
    buffer = user_buffers.get(id_num, {"text": ""})
    texto_acum = (buffer.get("text", "") + " " + (texto_extra or "")).strip()
    if not texto_acum: return
    if id_num in user_buffers: del user_buffers[id_num]

    t = obtener_tasas_dinamicas()
    cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 6", (id_num,))
    hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
    
    instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\n
    REGLAS TÉCNICAS (OBLIGATORIO):
    1. Si hay un producto/interés, usa [AGENDAR] de inmediato.
    2. Si da dirección o GPS, usa [FINALIZAR], da el total y datos de pago móvil.
    3. Si detectas dirección escrita, usa [DIRECCION: texto].
    4. NO muestres los corchetes [] al cliente jamás."""
    
    res_ia = consultar_ia(instr, texto_acum, hist)

    # Procesar etiquetas internas
    if "[NOMBRE:" in res_ia:
        n = res_ia.split("[NOMBRE:")[1].split("]")[0].strip()
        cur.execute("UPDATE cliente SET nombre = %s WHERE id = %s", (n, id_num))
    
    if "[AGENDAR]" in res_ia:
        cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando') ON CONFLICT DO NOTHING", (id_num,))
    
    if "[DIRECCION:" in res_ia:
        d = res_ia.split("[DIRECCION:")[1].split("]")[0].strip()
        cur.execute("UPDATE pedidos SET direccion = %s WHERE user_id = %s AND estado = 'confirmando'", (d, id_num))

    if "[FINALIZAR]" in res_ia: 
        cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
        enviar_meta(ADMIN_PHONE, f"🚨 NUEVO PEDIDO: {name_wa}")

    # 🧹 LIMPIEZA PROFUNDA DE TAGS (Evita error de image_1f5aba)
    limpia = re.sub(r'\[.*?\]', '', res_ia).strip()
    
    enviar_meta(num, limpia)
    cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_acum, limpia))
    conn.commit(); cur.close(); conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    body = request.json
    data = body['entry'][0]['changes'][0]['value']
    if 'messages' in data:
        msg = data['messages'][0]; num = msg['from']; id_num = int(num); msg_id = msg.get('id')
        if msg_id in procesados: return jsonify({"status": "ok"}), 200
        procesados.add(msg_id)

        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'test_mode'"); res_test = cur.fetchone()
        test_mode = res_test['value'] == 'true' if res_test else False
        num_lim = str(num).replace("+", "").strip()

        # Salida Modo Prueba
        if num_lim == ADMIN_PHONE and "[MODO_PRUEBA:OFF]" in msg.get('text', {}).get('body', ""):
            cur.execute("UPDATE config SET value = 'false' WHERE key = 'test_mode'")
            conn.commit(); enviar_meta(num, "✅ Modo Prueba OFF."); return jsonify({"status": "ok"}), 200

        # MODO ADMIN
        if num_lim == ADMIN_PHONE and not test_mode:
            threading.Thread(target=ejecutar_comando_admin, args=(msg.get('text', {}).get('body', ""), num)).start()
            return jsonify({"status": "ok"}), 200

        # MODO CLIENTE
        name_wa = data.get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
        if msg.get('type') == 'location':
            cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado = 'confirmando'", (f"{msg['location']['latitude']},{msg['location']['longitude']}", id_num))
            conn.commit()
            threading.Thread(target=procesar_flujo_cliente, args=(id_num, num, name_wa, "He enviado mi GPS.")).start()
        elif msg.get('type') == 'text':
            txt = msg['text']['body']
            if id_num in user_buffers:
                user_buffers[id_num]["timer"].cancel(); user_buffers[id_num]["text"] += f" {txt}"
            else: user_buffers[id_num] = {"text": txt}
            t = threading.Timer(5.0, procesar_flujo_cliente, args=[id_num, num, name_wa])
            user_buffers[id_num]["timer"] = t; t.start()

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)