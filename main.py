import os, requests, psycopg2, json, time, re, threading
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

# ⚠️ MODELO CORREGIDO SEGÚN TU CONTEXTO PERSONAL
MODELO_IA = "google/gemini-2.5-flash"

# --- MEMORIA VOLÁTIL ---
user_buffers = {}   # Buffer para el delay de 5s
procesados = set()  # Anti-spam de Meta
cache_tasas = {"usd": 0.0, "eur": 0.0, "expira": 0} # Cache de 1 hora

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
    # Si el caché es válido (menos de 1 hora), úsalo
    if time.time() < cache_tasas["expira"]: return cache_tasas

    t_usd, t_eur = 0.0, 0.0
    try:
        # Consultar API Externa
        res_u = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=4).json()
        t_usd = float(res_u['rates']['VES'])
        
        res_e = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=4).json()
        t_eur = float(res_e['rates']['VES'])
        
        # Guardar respaldo en Neon
        conn = get_db()
        if conn:
            cur = conn.cursor()
            cur.execute("UPDATE config SET value=%s, updated_at=NOW() WHERE key='tasa_usd'", (str(t_usd),))
            cur.execute("UPDATE config SET value=%s, updated_at=NOW() WHERE key='tasa_eur'", (str(t_eur),))
            conn.commit(); conn.close()
            print(f"🔄 Tasas Actualizadas API: ${t_usd} | €{t_eur}")
    except:
        # Fallback: Leer de Neon si la API falla
        conn = get_db()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM config WHERE key IN ('tasa_usd','tasa_eur')")
            rows = cur.fetchall()
            for r in rows:
                if r['key'] == 'tasa_usd': t_usd = float(r['value'])
                if r['key'] == 'tasa_eur': t_eur = float(r['value'])
            conn.close()

    # Actualizar caché (expira en 3600 seg = 1 hora)
    cache_tasas = {"usd": t_usd, "eur": t_eur, "expira": time.time() + 3600}
    return cache_tasas

# --- CEREBRO IA (Gemini 2.5 Flash) ---
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
        if 'choices' in res.json():
            return res.json()['choices'][0]['message']['content']
        return "Error: Sin respuesta IA."
    except: return "Error técnico en IA."

# --- PROCESAMIENTO CENTRAL (CLIENTE) ---
def procesar_cliente(telefono, nombre_wa, mensaje_acumulado):
    conn = get_db(); cur = conn.cursor()
    
    # 1. Identificar Cliente
    cur.execute("SELECT nombre FROM clientes WHERE telefono = %s", (telefono,))
    res = cur.fetchone()
    if not res:
        cur.execute("INSERT INTO clientes (telefono, nombre) VALUES (%s, %s)", (telefono, nombre_wa))
        conn.commit()
        nombre_real = "Nuevo"
    else:
        nombre_real = res['nombre']

    # 2. Contexto (Carrito + Tasas)
    tasas = obtener_tasas()
    cur.execute("SELECT resumen, monto_total FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','confirmado') LIMIT 1", (telefono,))
    pedido = cur.fetchone()
    carrito_txt = f"Carrito: {pedido['resumen']} (${pedido['monto_total']})" if pedido else "Carrito Vacio"

    prompt = f"""
    Eres 'PizzaBros'. TASAS HOY: USD={tasas['usd']} Bs, EUR={tasas['eur']} Bs.
    Cliente: {nombre_real}. {carrito_txt}.
    Mensaje del cliente: "{mensaje_acumulado}"

    REGLAS ESTRICTAS:
    - Si el nombre es "Nuevo", PREGUNTA su nombre real. Acción: [NOMBRE: ...]
    - Si pide algo: Responde y agéndalo. Acción: [AGENDAR: Resumen | Monto en USD]
    - Si cancela: Acción: [CANCELAR]
    - Si da dirección/GPS: Confirma total en Bs. Acción: [DIRECCION: ...]
    - Si cierra venta: Acción: [FINALIZAR]
    """
    
    resp_ia = consultar_ia(prompt, mensaje_acumulado)
    
    # 3. Ejecutar Acciones DB
    if "[NOMBRE:" in resp_ia:
        n = resp_ia.split("[NOMBRE:")[1].split("]")[0].strip()
        cur.execute("UPDATE clientes SET nombre=%s WHERE telefono=%s", (n, telefono))
        
    if "[AGENDAR:" in resp_ia:
        data = resp_ia.split("[AGENDAR:")[1].split("]")[0].split("|")
        resumen = data[0].strip()
        monto = float(data[1].strip()) if len(data)>1 else 0
        if pedido:
            cur.execute("UPDATE pedidos SET resumen=%s, monto_total=%s WHERE id=(SELECT id FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','confirmado') LIMIT 1)", (resumen, monto, telefono))
        else:
            cur.execute("INSERT INTO pedidos (cliente_id, resumen, monto_total) VALUES (%s, %s, %s)", (telefono, resumen, monto))
            
    if "[CANCELAR]" in resp_ia:
        cur.execute("UPDATE pedidos SET estado='cancelado' WHERE cliente_id=%s AND estado IN ('carrito','confirmado')", (telefono,))
        
    if "[DIRECCION:" in resp_ia:
        d = resp_ia.split("[DIRECCION:")[1].split("]")[0].strip()
        cur.execute("UPDATE pedidos SET direccion=%s, estado='confirmado' WHERE cliente_id=%s AND estado IN ('carrito','confirmado')", (d, telefono))
        
        # Notificar Admin (Conversión a Bs)
        bs_total = (pedido['monto_total'] * tasas['usd']) if pedido else 0
        enviar_whatsapp(ADMIN_PHONE, f"🚨 PEDIDO CONFIRMADO\nCliente: {nombre_real}\nDirección: {d}\nTotal: {bs_total:.2f} Bs (${pedido['monto_total']})")

    conn.commit(); conn.close()
    
    # 4. Responder al cliente (Limpiando tags)
    texto_limpio = re.sub(r'\[.*?\]', '', resp_ia).strip()
    if texto_limpio: enviar_whatsapp(telefono, texto_limpio)

# --- WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json['entry'][0]['changes'][0]['value']
        if 'messages' not in data: return "OK", 200
        
        msg = data['messages'][0]; num = msg['from']
        txt = msg.get('text', {}).get('body', '')
        nombre = data['contacts'][0]['profile']['name']

        # --- GESTIÓN DEL ADMIN ---
        if num == ADMIN_PHONE:
            cmd = txt.lower()
            if "activar prueba" in cmd:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='true' WHERE key='test_mode'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "🧪 MODO PRUEBA: ON (Eres cliente).")
                return "OK", 200
            
            if "desactivar prueba" in cmd:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='false' WHERE key='test_mode'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "✅ MODO PRUEBA: OFF (Eres Admin).")
                return "OK", 200

            if "apagar bot" in cmd:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='false' WHERE key='bot_activo'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "🔴 Bot APAGADO.")
                return "OK", 200
                
            if "encender bot" in cmd:
                conn=get_db(); cur=conn.cursor()
                cur.execute("UPDATE config SET value='true' WHERE key='bot_activo'")
                conn.commit(); conn.close()
                enviar_whatsapp(num, "🟢 Bot ENCENDIDO.")
                return "OK", 200

            # Verificar si Admin está en MODO PRUEBA
            conn=get_db(); cur=conn.cursor()
            cur.execute("SELECT value FROM config WHERE key='test_mode'")
            res_test = cur.fetchone()
            conn.close()
            
            # Si NO está en modo prueba, salir (ignorar comandos de cliente)
            if not res_test or res_test['value'] == 'false':
                return "OK", 200

        # --- GESTIÓN DE CLIENTES ---
        
        # 1. Verificar Interruptor General
        conn=get_db(); cur=conn.cursor()
        cur.execute("SELECT value FROM config WHERE key='bot_activo'")
        res_bot = cur.fetchone()
        conn.close()
        if not res_bot or res_bot['value'] == 'false':
            return "OK", 200 # Bot apagado

        # 2. Manejo de Ubicación
        if msg['type'] == 'location':
            loc = f"GPS: http://maps.google.com/?q={msg['location']['latitude']},{msg['location']['longitude']}"
            txt = loc 

        # 3. BUFFER DE 5 SEGUNDOS (Debouncing)
        if num in user_buffers:
            user_buffers[num]['timer'].cancel() # Resetear timer
            user_buffers[num]['text'] += f" {txt}"
        else:
            user_buffers[num] = {'text': txt}
        
        # Ejecutar tras 5 segundos de silencio
        t = threading.Timer(5.0, procesar_cliente, args=[num, nombre, user_buffers[num]['text']])
        user_buffers[num]['timer'] = t
        t.start()
        
        # Limpieza diferida del buffer
        threading.Timer(6.0, lambda: user_buffers.pop(num, None)).start()

    except Exception as e: print(e)
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)