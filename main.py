import os, requests, psycopg2, json, time, re, threading, base64
from flask import Flask, request
from psycopg2.extras import RealDictCursor
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = str(os.environ.get("ADMIN_PHONE")).replace("+", "").replace(" ", "").strip()
MODELO_IA = "google/gemini-2.5-flash"

# --- MEMORIA VOLÁTIL ---
user_buffers = {}   # Buffer para el delay de 5s
procesados = set()  # Anti-spam de Meta
cache_tasas = {"usd": 0.0, "eur": 0.0, "expira": 0}

# --- CONEXIÓN DB ---
def get_db():
    try: return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e: print(f"❌ Error DB: {e}"); return None

# --- WHATSAPP SEND ---
def enviar_whatsapp(to, body):
    try:
        url = f"https://graph.facebook.com/v17.0/{META_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        requests.post(url, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}, headers=headers, timeout=5)
    except: pass

# --- TASAS AUTOMÁTICAS (ExchangeRate-API) ---
def obtener_tasas():
    global cache_tasas
    if time.time() < cache_tasas["expira"]: return cache_tasas

    t_usd, t_eur = 0.0, 0.0
    try:
        res_u = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=4).json()
        t_usd = float(res_u['rates']['VES'])
        res_e = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=4).json()
        t_eur = float(res_e['rates']['VES'])
        
        conn = get_db()
        if conn:
            cur = conn.cursor()
            cur.execute("UPDATE config SET value=%s, updated_at=NOW() WHERE key='tasa_usd'", (str(t_usd),))
            cur.execute("UPDATE config SET value=%s, updated_at=NOW() WHERE key='tasa_eur'", (str(t_eur),))
            conn.commit(); conn.close()
    except:
        conn = get_db()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM config WHERE key IN ('tasa_usd','tasa_eur')")
            rows = cur.fetchall()
            for r in rows:
                if r['key'] == 'tasa_usd': t_usd = float(r['value'])
                if r['key'] == 'tasa_eur': t_eur = float(r['value'])
            conn.close()

    cache_tasas = {"usd": t_usd, "eur": t_eur, "expira": time.time() + 3600}
    return cache_tasas

# --- PROCESAMIENTO DE PAGOS (VISIÓN + ANTIFRAUDE) ---
def procesar_pago(image_id, cliente_id):
    try:
        # 1. Descargar imagen de Meta
        url_get = f"https://graph.facebook.com/v17.0/{image_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        res_url = requests.get(url_get, headers=headers).json().get('url')
        if not res_url: return

        img_data = requests.get(res_url, headers=headers).content
        b64_img = base64.b64encode(img_data).decode('utf-8')

        # 2. IA Visión: Extraer datos
        url_ai = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": "google/gemini-2.0-flash-001", # Modelo visión
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Extrae la REFERENCIA (números) y el MONTO exacto del comprobante. Responde SOLO JSON: {\"ref\": \"1234\", \"monto\": 100.00}. Si no es un comprobante de pago válido, responde {\"error\": true}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                ]}
            ]
        }
        res = requests.post(url_ai, json=payload, headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"}).json()
        content = res['choices'][0]['message']['content'].replace("```json", "").replace("```", "").strip()
        datos = json.loads(content)

        if "error" in datos:
            enviar_whatsapp(cliente_id, "⚠️ No pude leer los datos del comprobante. Por favor escribe la referencia y el banco.")
            return

        conn = get_db(); cur = conn.cursor()

        # 3. ANTIFRAUDE: Verificar si la referencia ya existe
        cur.execute("SELECT id FROM pagos WHERE referencia = %s", (str(datos['ref']),))
        duplicado = cur.fetchone()
        
        if duplicado:
            enviar_whatsapp(cliente_id, f"❌ ERROR: La referencia {datos['ref']} ya fue registrada anteriormente. Envía un comprobante válido.")
            enviar_whatsapp(ADMIN_PHONE, f"🚨 ALERTA DE FRAUDE\nCliente {cliente_id} intentó usar referencia repetida: {datos['ref']}")
            conn.close(); return

        # 4. Registrar Pago Pendiente
        cur.execute("INSERT INTO pagos (cliente_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (cliente_id, str(datos['ref']), datos['monto']))
        conn.commit()

        # 5. Notificar Admin y Cliente
        enviar_whatsapp(cliente_id, f"✅ Pago registrado (Ref: {datos['ref']}). Esperando confirmación del administrador.")
        enviar_whatsapp(ADMIN_PHONE, f"💰 *NUEVO PAGO PENDIENTE*\nCliente: {cliente_id}\nRef: {datos['ref']}\nMonto: {datos['monto']} Bs\n\nResponde 'Sí' o 'Confirmado' para aprobarlo.")
        
        conn.close()

    except Exception as e:
        print(f"Error Pago: {e}")
        enviar_whatsapp(cliente_id, "⚠️ Error procesando la imagen. Intenta de nuevo.")

# --- CEREBRO IA (TEXTO) ---
def consultar_ia(prompt_sistema, entrada):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": MODELO_IA,
            "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": entrada}],
            "temperature": 0.0
        }
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res.json()['choices'][0]['message']['content']
    except: return "Error técnico en IA."

# --- PROCESAMIENTO CENTRAL ---
def procesar_cliente(telefono, nombre_wa, mensaje_acumulado):
    conn = get_db(); cur = conn.cursor()
    
    # Identificar
    cur.execute("SELECT nombre FROM clientes WHERE telefono = %s", (telefono,))
    res = cur.fetchone()
    if not res:
        cur.execute("INSERT INTO clientes (telefono, nombre) VALUES (%s, %s)", (telefono, nombre_wa))
        conn.commit()
        nombre_real = "Nuevo"
    else:
        nombre_real = res['nombre']

    # Contexto Financiero
    tasas = obtener_tasas()
    cur.execute("SELECT resumen, monto_total FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','confirmado') LIMIT 1", (telefono,))
    pedido = cur.fetchone()
    carrito_txt = f"Carrito: {pedido['resumen']} (${pedido['monto_total']})" if pedido else "Carrito Vacio"

    prompt = f"""
    Eres 'PizzaBros'. TASAS HOY: USD={tasas['usd']} Bs, EUR={tasas['eur']} Bs.
    Cliente: {nombre_real}. {carrito_txt}.
    Mensaje: "{mensaje_acumulado}"

    REGLAS:
    - Si es Nuevo: Pide nombre real. Acción: [NOMBRE: ...]
    - Si pide: [AGENDAR: Resumen | Monto USD]
    - Si cancela: [CANCELAR]
    - Si da dirección: [DIRECCION: ...]
    - Si pregunta pago: "Total: X Bs. Pago Móvil: 0414-XXX".
    """
    
    resp_ia = consultar_ia(prompt, mensaje_acumulado)
    
    # Acciones DB
    if "[NOMBRE:" in resp_ia:
        n = resp_ia.split("[NOMBRE:")[1].split("]")[0].strip()
        cur.execute("UPDATE clientes SET nombre=%s WHERE telefono=%s", (n, telefono))
    if "[AGENDAR:" in resp_ia:
        data = resp_ia.split("[AGENDAR:")[1].split("]")[0].split("|")
        res = data[0].strip(); monto = float(data[1].strip()) if len(data)>1 else 0
        if pedido:
            cur.execute("UPDATE pedidos SET resumen=%s, monto_total=%s WHERE id=(SELECT id FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','confirmado') LIMIT 1)", (res, monto, telefono))
        else:
            cur.execute("INSERT INTO pedidos (cliente_id, resumen, monto_total) VALUES (%s, %s, %s)", (telefono, res, monto))
    if "[CANCELAR]" in resp_ia:
        cur.execute("UPDATE pedidos SET estado='cancelado' WHERE cliente_id=%s AND estado IN ('carrito','confirmado')", (telefono,))
    if "[DIRECCION:" in resp_ia:
        d = resp_ia.split("[DIRECCION:")[1].split("]")[0].strip()
        cur.execute("UPDATE pedidos SET direccion=%s, estado='confirmado' WHERE cliente_id=%s AND estado IN ('carrito','confirmado')", (d, telefono))
        enviar_whatsapp(ADMIN_PHONE, f"🚨 PEDIDO CONFIRMADO\nCliente: {nombre_real}\nDirección: {d}")

    conn.commit(); conn.close()
    
    # Responder
    clean = re.sub(r'\[.*?\]', '', resp_ia).strip()
    if clean: enviar_whatsapp(telefono, clean)

# --- WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json['entry'][0]['changes'][0]['value']
        if 'messages' not in data: return "OK", 200
        
        msg = data['messages'][0]; num = msg['from']
        txt = msg.get('text', {}).get('body', '').lower()
        nombre = data['contacts'][0]['profile']['name']

        # --- ADMIN (Control y Aprobación) ---
        if num == ADMIN_PHONE:
            # 1. Aprobación Natural de Pagos
            if any(x in txt for x in ["sí", "si", "ok", "confirmado", "recibido", "aprobado"]):
                conn=get_db(); cur=conn.cursor()
                # Buscar último pago pendiente
                cur.execute("SELECT id, cliente_id, monto FROM pagos WHERE estado='pendiente' ORDER BY id DESC LIMIT 1")
                pago = cur.fetchone()
                if pago:
                    cur.execute("UPDATE pagos SET estado='aprobado' WHERE id=%s", (pago['id'],))
                    conn.commit()
                    enviar_whatsapp(num, f"✅ Pago de {pago['monto']} Bs APROBADO.")
                    enviar_whatsapp(pago['cliente_id'], "🎉 Tu pago ha sido confirmado. ¡Tu pedido se está procesando!")
                else:
                    enviar_whatsapp(num, "No hay pagos pendientes para confirmar.")
                conn.close()
                return "OK", 200

            # 2. Comandos Técnicos
            if "activar prueba" in txt:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='true' WHERE key='test_mode'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "🧪 MODO PRUEBA ON")
                return "OK", 200
            
            if "desactivar prueba" in txt:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='false' WHERE key='test_mode'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "✅ MODO PRUEBA OFF")
                return "OK", 200

            if "apagar bot" in txt:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='false' WHERE key='bot_activo'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "🔴 Bot OFF")
                return "OK", 200

            if "encender bot" in txt:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='true' WHERE key='bot_activo'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "🟢 Bot ON")
                return "OK", 200
            
            # Verificar si Admin está en test
            conn=get_db(); cur=conn.cursor()
            cur.execute("SELECT value FROM config WHERE key='test_mode'")
            res_test = cur.fetchone()
            conn.close()
            if not res_test or res_test['value'] == 'false': return "OK", 200

        # --- CLIENTE ---
        conn=get_db(); cur=conn.cursor()
        cur.execute("SELECT value FROM config WHERE key='bot_activo'")
        res_bot = cur.fetchone()
        conn.close()
        if not res_bot or res_bot['value'] == 'false': return "OK", 200

        # A. Imagen (Pago) -> Directo a procesar (Sin delay)
        if msg['type'] == 'image':
            threading.Thread(target=procesar_pago, args=(msg['image']['id'], num)).start()
            return "OK", 200
        
        # B. Ubicación -> Convertir a texto para el buffer
        if msg['type'] == 'location':
            txt = f"GPS: http://maps.google.com/?q={msg['location']['latitude']},{msg['location']['longitude']}"

        # C. Texto -> Buffer 5s (Anti-spam)
        if num in user_buffers:
            user_buffers[num]['timer'].cancel()
            user_buffers[num]['text'] += f" {txt}"
        else:
            user_buffers[num] = {'text': txt}
        
        t = threading.Timer(5.0, procesar_cliente, args=[num, nombre, user_buffers[num]['text']])
        user_buffers[num]['timer'] = t
        t.start()
        
        threading.Timer(6.0, lambda: user_buffers.pop(num, None)).start()

    except Exception as e: print(e)
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)