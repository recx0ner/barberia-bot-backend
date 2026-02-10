import os, requests, psycopg2, json, time, re, threading, base64
from flask import Flask, request
from psycopg2.extras import RealDictCursor
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
def cargar_configuracion():
    print("\n--- 🔍 DIAGNÓSTICO DE ARRANQUE ---")
    config = {
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY"),
        "META_ACCESS_TOKEN": os.environ.get("META_ACCESS_TOKEN"),
        "META_PHONE_ID": os.environ.get("META_PHONE_ID"),
        "ADMIN_PHONE": os.environ.get("ADMIN_PHONE"),
        "BUSINESS_CONTEXT": os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de PizzaBros."),
        "PORT": int(os.environ.get("PORT", 10000))
    }
    for k, v in config.items():
        if v: print(f"✅ {k}: OK")
        else: print(f"❌ {k}: FALTA")
    return config

CONFIG = cargar_configuracion()
ADMIN_PHONE_CLEAN = str(CONFIG["ADMIN_PHONE"]).replace("+", "").replace(" ", "").strip()
MODELO_IA = "google/gemini-2.5-flash"

# --- MEMORIA VOLÁTIL ---
user_buffers = {}
cache_tasas = {"usd": 0.0, "eur": 0.0, "expira": 0}

# --- DB & HERRAMIENTAS ---
def get_db():
    try: return psycopg2.connect(CONFIG["DATABASE_URL"], cursor_factory=RealDictCursor)
    except Exception as e: print(f"❌ Error DB: {e}"); return None

def enviar_whatsapp(to, body):
    try:
        url = f"https://graph.facebook.com/v17.0/{CONFIG['META_PHONE_ID']}/messages"
        headers = {"Authorization": f"Bearer {CONFIG['META_ACCESS_TOKEN']}", "Content-Type": "application/json"}
        requests.post(url, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}, headers=headers, timeout=5)
    except Exception as e: print(f"Error WA: {e}")

# --- GESTIÓN DE MEMORIA ---
def obtener_historial(cliente_id):
    conn = get_db()
    if not conn: return ""
    cur = conn.cursor()
    cur.execute("SELECT role, content FROM messages WHERE cliente_id = %s ORDER BY id DESC LIMIT 6", (cliente_id,))
    rows = cur.fetchall()
    conn.close()
    historial = ""
    for r in reversed(rows):
        role = "Cliente" if r['role'] == 'user' else "Tú (Asistente)"
        historial += f"{role}: {r['content']}\n"
    return historial

def guardar_mensaje(cliente_id, role, content):
    conn = get_db()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO messages (cliente_id, role, content) VALUES (%s, %s, %s)", (cliente_id, role, content))
        conn.commit(); conn.close()

# --- TASAS (API) ---
def obtener_tasas():
    global cache_tasas
    if time.time() < cache_tasas["expira"]: return cache_tasas
    try:
        # API Externa
        t_usd = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=3).json()['rates']['VES']
        t_eur = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=3).json()['rates']['VES']
        cache_tasas = {"usd": float(t_usd), "eur": float(t_eur), "expira": time.time() + 3600}
        print(f"🔄 TASAS ACTUALIZADAS: USD {t_usd} | EUR {t_eur}")
    except Exception as e: 
        print(f"⚠️ Error Tasas: {e}")
        # Si falla, intentamos mantener la última conocida o 0.0
    return cache_tasas

# --- PAGOS (VISIÓN) ---
def procesar_pago(image_id, cliente_id):
    try:
        url_get = f"https://graph.facebook.com/v17.0/{image_id}"
        headers = {"Authorization": f"Bearer {CONFIG['META_ACCESS_TOKEN']}"}
        res_url = requests.get(url_get, headers=headers).json().get('url')
        if not res_url: return
        
        img_data = requests.get(res_url, headers=headers).content
        b64_img = base64.b64encode(img_data).decode('utf-8')

        url_ai = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": "google/gemini-2.0-flash-001",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Extrae REFERENCIA (números) y MONTO. JSON: {\"ref\": \"1234\", \"monto\": 100.00}. Si falla: {\"error\": true}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                ]}
            ]
        }
        res = requests.post(url_ai, json=payload, headers={"Authorization": f"Bearer {CONFIG['OPENROUTER_API_KEY']}"}).json()
        content = res['choices'][0]['message']['content'].replace("```json", "").replace("```", "").strip()
        datos = json.loads(content)

        if "error" in datos:
            enviar_whatsapp(cliente_id, "⚠️ No pude leer el comprobante.")
            return

        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT id FROM pagos WHERE referencia = %s", (str(datos['ref']),))
        if cur.fetchone():
            enviar_whatsapp(cliente_id, f"❌ ERROR: Referencia {datos['ref']} DUPLICADA.")
            conn.close(); return

        cur.execute("INSERT INTO pagos (cliente_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (cliente_id, str(datos['ref']), datos['monto']))
        conn.commit()
        enviar_whatsapp(cliente_id, f"✅ Pago registrado (Ref: {datos['ref']}).")
        enviar_whatsapp(ADMIN_PHONE_CLEAN, f"💰 *PAGO PENDIENTE*\nCliente: {cliente_id}\nRef: {datos['ref']}\nMonto: {datos['monto']} Bs\nResponde 'Sí' para aprobar.")
        conn.close()
    except Exception as e: print(f"Error Pago: {e}")

# --- IA ---
def consultar_ia(prompt, entrada):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {CONFIG['OPENROUTER_API_KEY']}", "Content-Type": "application/json"}
        payload = {
            "model": MODELO_IA,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": entrada}],
            "temperature": 0.0
        }
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res.json()['choices'][0]['message']['content']
    except: return "Error técnico IA."

# --- PROCESO CLIENTE (CON CÁLCULO DE DIVISAS) ---
def procesar_cliente(telefono, nombre_wa, mensaje):
    conn = get_db(); cur = conn.cursor()
    
    # 1. Identidad
    cur.execute("SELECT nombre FROM clientes WHERE telefono = %s", (telefono,))
    res = cur.fetchone()
    if not res:
        cur.execute("INSERT INTO clientes (telefono, nombre) VALUES (%s, %s)", (telefono, nombre_wa))
        conn.commit()
        nombre_real = "Nuevo"
    else: nombre_real = res['nombre']

    # 2. Contexto
    tasas = obtener_tasas()
    print(f"💰 Tasa usada para el prompt: {tasas['usd']} Bs") # Debug en Render logs

    cur.execute("SELECT resumen, monto_total FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','confirmado') LIMIT 1", (telefono,))
    pedido = cur.fetchone()
    carrito_txt = f"Carrito: {pedido['resumen']} (${pedido['monto_total']})" if pedido else "Vacio"
    
    historial_chat = obtener_historial(telefono)

    # 3. PROMPT MAESTRO (Instrucciones de Cálculo)
    prompt = f"""
    CONTEXTO DE NEGOCIO: {CONFIG['BUSINESS_CONTEXT']}
    
    ⚠️ REGLA DE ORO DE PRECIOS:
    La Tasa del Dólar HOY es: {tasas['usd']} Bs.
    SIEMPRE que menciones un precio en Dólares ($), DEBES calcular y escribir al lado el monto en Bolívares (Bs).
    Ejemplo: "Son $10 (aprox. {10 * tasas['usd']} Bs)". ¡HAZ EL CÁLCULO!

    DATOS CLIENTE:
    - Nombre: {nombre_real}
    - Carrito: {carrito_txt}
    
    HISTORIAL:
    {historial_chat}
    
    ACCIONES TÉCNICAS:
    1. Nuevo cliente -> [NOMBRE: ...]
    2. Agrega prod -> [AGENDAR: Resumen | Monto USD]
    3. Cancela -> [CANCELAR]
    4. Dirección -> [DIRECCION: ...]
    """
    
    resp_ia = consultar_ia(prompt, mensaje)
    
    # 4. Ejecutar Acciones
    if "[NOMBRE:" in resp_ia:
        n = resp_ia.split("[NOMBRE:")[1].split("]")[0].strip()
        cur.execute("UPDATE clientes SET nombre=%s WHERE telefono=%s", (n, telefono))
    if "[AGENDAR:" in resp_ia:
        d = resp_ia.split("[AGENDAR:")[1].split("]")[0].split("|")
        resum = d[0].strip(); m = float(d[1].strip()) if len(d)>1 else 0
        if pedido: cur.execute("UPDATE pedidos SET resumen=%s, monto_total=%s WHERE id=(SELECT id FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','confirmado') LIMIT 1)", (resum, m, telefono))
        else: cur.execute("INSERT INTO pedidos (cliente_id, resumen, monto_total) VALUES (%s, %s, %s)", (telefono, resum, m))
    if "[CANCELAR]" in resp_ia:
        cur.execute("UPDATE pedidos SET estado='cancelado' WHERE cliente_id=%s AND estado IN ('carrito','confirmado')", (telefono,))
    if "[DIRECCION:" in resp_ia:
        d = resp_ia.split("[DIRECCION:")[1].split("]")[0].strip()
        cur.execute("UPDATE pedidos SET direccion=%s, estado='confirmado' WHERE cliente_id=%s AND estado IN ('carrito','confirmado')", (d, telefono))
        bs = (pedido['monto_total'] * tasas['usd']) if pedido else 0
        enviar_whatsapp(ADMIN_PHONE_CLEAN, f"🚨 CONFIRMADO\nCliente: {nombre_real}\nDir: {d}\nTotal: {bs:.2f} Bs (${pedido['monto_total']})")

    conn.commit(); conn.close()
    
    # 5. Guardar y Responder
    clean_resp = re.sub(r'\[.*?\]', '', resp_ia).strip()
    if clean_resp:
        enviar_whatsapp(telefono, clean_resp)
        guardar_mensaje(telefono, 'user', mensaje)
        guardar_mensaje(telefono, 'assistant', clean_resp)

# --- WEBHOOK ---
@app.route("/whatsapp", methods=["POST", "GET"])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    try:
        data = request.json['entry'][0]['changes'][0]['value']
        if 'messages' not in data: return "OK", 200
        
        msg = data['messages'][0]; num = msg['from']
        txt = msg.get('text', {}).get('body', '').lower()
        nombre = data['contacts'][0]['profile']['name']

        # ADMIN
        if num == ADMIN_PHONE_CLEAN:
            if "activar" in txt and "prueba" in txt:
                conn=get_db(); cur=conn.cursor(); cur.execute("UPDATE config SET value='true' WHERE key='test_mode'"); conn.commit(); conn.close()
                enviar_whatsapp(num, "🧪 TEST ON"); return "OK", 200
            if "desactivar" in txt and "prueba" in txt:
                conn=get_db(); cur=conn.cursor(); cur.execute("UPDATE config SET value='false' WHERE key='test_mode'"); conn.commit(); conn.close()
                enviar_whatsapp(num, "✅ TEST OFF"); return "OK", 200
            if "apagar bot" in txt:
                conn=get_db(); cur=conn.cursor(); cur.execute("UPDATE config SET value='false' WHERE key='bot_activo'"); conn.commit(); conn.close()
                enviar_whatsapp(num, "🔴 Bot OFF"); return "OK", 200
            if "encender bot" in txt:
                conn=get_db(); cur=conn.cursor(); cur.execute("UPDATE config SET value='true' WHERE key='bot_activo'"); conn.commit(); conn.close()
                enviar_whatsapp(num, "🟢 Bot ON"); return "OK", 200
            if any(x in txt for x in ["sí", "si", "ok", "confirmado"]):
                conn=get_db(); cur=conn.cursor()
                cur.execute("SELECT id, cliente_id, monto FROM pagos WHERE estado='pendiente' ORDER BY id DESC LIMIT 1")
                pago = cur.fetchone()
                if pago:
                    cur.execute("UPDATE pagos SET estado='aprobado' WHERE id=%s", (pago['id'],))
                    conn.commit()
                    enviar_whatsapp(num, "✅ Aprobado."); enviar_whatsapp(pago['cliente_id'], "🎉 Pago Aprobado.")
                conn.close(); return "OK", 200

            # Check Test Mode
            conn=get_db(); cur=conn.cursor(); cur.execute("SELECT value FROM config WHERE key='test_mode'"); res_test = cur.fetchone(); conn.close()
            if not res_test or res_test['value'] == 'false': return "OK", 200

        # CLIENTE
        conn=get_db(); cur=conn.cursor(); cur.execute("SELECT value FROM config WHERE key='bot_activo'"); res_bot = cur.fetchone(); conn.close()
        
        is_bot_on = res_bot and res_bot['value'] == 'true'
        is_admin_testing = (num == ADMIN_PHONE_CLEAN)
        if not is_bot_on and not is_admin_testing: return "OK", 200

        if msg['type'] == 'image':
            threading.Thread(target=procesar_pago, args=(msg['image']['id'], num)).start()
            return "OK", 200
        
        if msg['type'] == 'location':
            txt = f"GPS: http://maps.google.com/?q={msg['location']['latitude']},{msg['location']['longitude']}"

        if num in user_buffers:
            user_buffers[num]['timer'].cancel()
            user_buffers[num]['text'] += f" {txt}"
        else: user_buffers[num] = {'text': txt}
        
        t = threading.Timer(5.0, procesar_cliente, args=[num, nombre, user_buffers[num]['text']])
        user_buffers[num]['timer'] = t; t.start()
        threading.Timer(6.0, lambda: user_buffers.pop(num, None)).start()

    except Exception as e: print(e)
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))