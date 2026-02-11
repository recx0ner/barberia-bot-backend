import os, requests, psycopg2, json, time, re, threading, base64
from psycopg2 import pool
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
ADMIN_PHONE_CLEAN = str(CONFIG["ADMIN_PHONE"]).replace("+", "").replace(" ", "").strip() if CONFIG["ADMIN_PHONE"] else ""
MODELO_IA = "google/gemini-2.5-flash"

user_buffers = {}
cache_tasas = {"usd": 0.0, "eur": 0.0, "expira": 0}

# --- POOL DB ---
try:
    pg_pool = psycopg2.pool.SimpleConnectionPool(1, 20, CONFIG["DATABASE_URL"], cursor_factory=RealDictCursor)
except: pg_pool = None

def get_db_connection():
    try: return pg_pool.getconn() if pg_pool else psycopg2.connect(CONFIG["DATABASE_URL"], cursor_factory=RealDictCursor)
    except: return None

def release_db_connection(conn):
    try: 
        if pg_pool: pg_pool.putconn(conn)
        else: conn.close()
    except: pass

# --- WHATSAPP ---
def enviar_whatsapp(to, body):
    try:
        url = f"https://graph.facebook.com/v17.0/{CONFIG['META_PHONE_ID']}/messages"
        headers = {"Authorization": f"Bearer {CONFIG['META_ACCESS_TOKEN']}", "Content-Type": "application/json"}
        requests.post(url, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}, headers=headers, timeout=5)
    except Exception as e: print(f"Error WA: {e}")

# --- MEMORIA ---
def obtener_historial(conn, cliente_id):
    cur = conn.cursor()
    cur.execute("SELECT role, content FROM messages WHERE cliente_id = %s ORDER BY id DESC LIMIT 6", (cliente_id,))
    rows = cur.fetchall()
    historial = ""
    for r in reversed(rows):
        role = "Cliente" if r['role'] == 'user' else "Tú (Asistente)"
        historial += f"{role}: {r['content']}\n"
    return historial

def guardar_mensaje(conn, cliente_id, role, content):
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (cliente_id, role, content) VALUES (%s, %s, %s)", (cliente_id, role, content))
    conn.commit()

# --- TASAS ---
def obtener_tasas():
    global cache_tasas
    if time.time() < cache_tasas["expira"]: return cache_tasas
    try:
        t_usd = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=3).json()['rates']['VES']
        t_eur = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=3).json()['rates']['VES']
        cache_tasas = {"usd": float(t_usd), "eur": float(t_eur), "expira": time.time() + 3600}
    except: pass
    return cache_tasas

def limpiar_monto_bs(valor_raw):
    try:
        v = str(valor_raw).strip().replace("Bs.", "").replace("Bs", "").strip()
        if "," in v and "." in v: v = v.replace(".", "").replace(",", ".")
        elif "," in v: v = v.replace(",", ".")
        return float(v)
    except: return 0.0

# --- PAGOS ---
def procesar_pago(image_id, cliente_id):
    conn = get_db_connection()
    if not conn: return
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
                    {"type": "text", "text": "Extrae REFERENCIA (números) y MONTO. JSON: {\"ref\": \"1234\", \"monto\": 1950.00}. Si falla: {\"error\": true}"},
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

        monto_real = limpiar_monto_bs(datos['monto'])
        ref_real = str(datos['ref'])

        cur = conn.cursor()
        cur.execute("SELECT id FROM pagos WHERE referencia = %s", (ref_real,))
        if cur.fetchone():
            enviar_whatsapp(cliente_id, f"❌ ERROR: Referencia {ref_real} DUPLICADA.")
            return

        cur.execute("INSERT INTO pagos (cliente_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (cliente_id, ref_real, monto_real))
        conn.commit()

        tasas = obtener_tasas()
        cur.execute("SELECT monto_total FROM pedidos WHERE cliente_id = %s AND estado IN ('carrito', 'pendiente_pago', 'confirmado') ORDER BY id DESC LIMIT 1", (cliente_id,))
        pedido = cur.fetchone()
        
        mensaje_cliente = f"✅ *Pago Recibido*\n🔖 Ref: {ref_real}\n💵 Monto: {monto_real:,.2f} Bs"
        msg_admin_extra = ""

        if pedido:
            total_pedido_bs = float(pedido['monto_total']) * tasas['usd']
            cur.execute("SELECT SUM(monto) as pagado FROM pagos WHERE cliente_id = %s AND estado IN ('pendiente', 'aprobado')", (cliente_id,))
            res_pagos = cur.fetchone()
            total_pagado = float(res_pagos['pagado']) if res_pagos and res_pagos['pagado'] else 0.0
            
            restante = total_pedido_bs - total_pagado
            
            if restante > 1.0:
                mensaje_cliente += f"\n\n📉 *Abonado:* {total_pagado:,.2f} Bs\n⚠️ *Faltan:* {restante:,.2f} Bs\n(Tu pedido sigue Pendiente de Pago)"
                msg_admin_extra = f"\n⚠️ Incompleto. Restan: {restante:,.2f} Bs"
            else:
                # CONFIRMACIÓN AUTOMÁTICA
                cur.execute("SELECT id FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago') LIMIT 1", (cliente_id,))
                pid = cur.fetchone()
                if pid:
                    cur.execute("UPDATE pedidos SET estado='confirmado' WHERE id=%s", (pid['id'],))
                    conn.commit()
                
                mensaje_cliente += "\n\n🎉 *¡PAGO COMPLETADO!*\nTu pedido ha pasado a estado: *CONFIRMADO*."
                msg_admin_extra = "\n✅ PAGADO TOTALMENTE. DESPACHAR."
        
        enviar_whatsapp(cliente_id, mensaje_cliente)
        if ADMIN_PHONE_CLEAN:
            enviar_whatsapp(ADMIN_PHONE_CLEAN, f"💰 *PAGO NUEVO*\nRef: {ref_real}\nMonto: {monto_real:,.2f} Bs{msg_admin_extra}\nResponde 'Sí' para aprobar el dinero.")

    except Exception as e: print(f"Error Pago: {e}")
    finally: release_db_connection(conn)

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

# --- CLIENTE ---
def procesar_cliente(telefono, nombre_wa, mensaje):
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        
        # Identidad
        cur.execute("SELECT nombre FROM clientes WHERE telefono = %s", (telefono,))
        res = cur.fetchone()
        if not res:
            cur.execute("INSERT INTO clientes (telefono, nombre) VALUES (%s, %s)", (telefono, nombre_wa))
            conn.commit(); nombre_real = "Nuevo"
        else: nombre_real = res['nombre']

        tasas = obtener_tasas()
        cur.execute("SELECT resumen, monto_total, estado FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago') LIMIT 1", (telefono,))
        pedido_db = cur.fetchone()
        
        # Variables locales para manejar estado en tiempo real
        pedido_actual = {
            'resumen': pedido_db['resumen'] if pedido_db else "Vacio",
            'monto_total': float(pedido_db['monto_total']) if pedido_db else 0.0,
            'existe': True if pedido_db else False
        }
        
        historial_chat = obtener_historial(conn, telefono)

        # 🔥 PROMPT "RED DE SEGURIDAD"
        prompt = f"""
        CONTEXTO: {CONFIG['BUSINESS_CONTEXT']}
        TASAS: USD={tasas['usd']} Bs.
        POLITICA: Retiro en Tienda.
        
        CLIENTE: {nombre_real}
        CARRITO ACTUAL (BD): {pedido_actual['resumen']} (${pedido_actual['monto_total']})
        HISTORIAL: {historial_chat}
        
        ⚠️ INSTRUCCIONES DE EMERGENCIA:
        1. Tu trabajo principal es REGISTRAR. Si el cliente pide algo (ej: "2 pepperonis"), DEBES usar [AGENDAR].
           Si no usas la etiqueta, el cliente recibirá una factura de $0 y se molestará.
        
        2. CIERRE INTELIGENTE (RED DE SEGURIDAD):
           Si el cliente dice "LISTO/SOLO ESO" y el CARRITO ACTUAL está vacío o en $0, pero en el HISTORIAL ves que pidió algo (ej: "quiero 2 pizzas"), significa que OLVIDASTE agendarlo antes.
           -> EN ESE CASO: Usa [AGENDAR: ...] Y [FINALIZAR] en el mismo mensaje.
           
        3. Si el carrito ya tiene datos correctos y dice "LISTO", solo usa [FINALIZAR].
        """
        
        resp_ia = consultar_ia(prompt, mensaje)
        
        msg_to_user = "" 

        if "[NOMBRE:" in resp_ia:
            n = resp_ia.split("[NOMBRE:")[1].split("]")[0].strip()
            cur.execute("UPDATE clientes SET nombre=%s WHERE telefono=%s", (n, telefono))
            
        # PROCESAR AGENDAR (Primero, para que si finaliza use el dato nuevo)
        if "[AGENDAR:" in resp_ia:
            try:
                d = resp_ia.split("[AGENDAR:")[1].split("]")[0].split("|")
                resum = d[0].strip(); m = float(d[1].strip()) if len(d)>1 else 0
                
                if pedido_actual['existe']:
                    cur.execute("UPDATE pedidos SET resumen=%s, monto_total=%s WHERE id=(SELECT id FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago') LIMIT 1)", (resum, m, telefono))
                else: 
                    cur.execute("INSERT INTO pedidos (cliente_id, resumen, monto_total, estado) VALUES (%s, %s, %s, 'carrito')", (telefono, resum, m))
                    pedido_actual['existe'] = True
                
                # ⚠️ ACTUALIZAMOS LA MEMORIA LOCAL PARA EL FINALIZAR
                pedido_actual['resumen'] = resum
                pedido_actual['monto_total'] = m
            except: pass
        
        if "[CANCELAR]" in resp_ia:
            cur.execute("UPDATE pedidos SET estado='cancelado' WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago')", (telefono,))
            pedido_actual['monto_total'] = 0
            
        if "[FINALIZAR]" in resp_ia:
            # Ahora sí, leemos el monto actualizado (sea de DB o del [AGENDAR] que acaba de pasar)
            monto_usd = pedido_actual['monto_total']
            
            if monto_usd > 0:
                cur.execute("UPDATE pedidos SET direccion='Retiro en Tienda', estado='pendiente_pago' WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago')", (telefono,))
                
                resumen_final = pedido_actual['resumen']
                monto_bs = monto_usd * tasas['usd']
                
                msg_to_user = f"📝 *Pedido Generado*\n\n{resumen_final}\n💵 Total a Pagar: ${monto_usd:.2f} ({monto_bs:,.2f} Bs)\n\n⚠️ *Estado: Pendiente por Pagar*\n\n📲 *Datos para Transferir:*\n- Pago Móvil: 0412-1234567\n- Banco: Venezuela\n- RIF: V-12345678\n\n📸 *Espera tu confirmación tras enviar el pago.*"
                
                if ADMIN_PHONE_CLEAN:
                    try: enviar_whatsapp(ADMIN_PHONE_CLEAN, f"⏳ NUEVA ORDEN (Pendiente Pago)\nCliente: {nombre_real}\nTotal: {monto_bs:,.2f} Bs")
                    except: pass
            else:
                # Si llega aquí y sigue siendo 0, es un error grave. Avisamos.
                msg_to_user = "⚠️ *Error:* No detecté productos en tu carrito. Por favor repite tu pedido (ej: 'Quiero una pizza de...') para poder generarte el recibo."

        conn.commit()
        
        if msg_to_user:
            enviar_whatsapp(telefono, msg_to_user)
            guardar_mensaje(conn, telefono, 'assistant', msg_to_user)
        else:
            clean_resp = re.sub(r'\[.*?\]', '', resp_ia).strip()
            if clean_resp:
                enviar_whatsapp(telefono, clean_resp)
                guardar_mensaje(conn, telefono, 'user', mensaje)
                guardar_mensaje(conn, telefono, 'assistant', clean_resp)

    except Exception as e: print(f"Error Cliente: {e}")
    finally: release_db_connection(conn)

# --- WEBHOOK ---
@app.route("/whatsapp", methods=["POST", "GET"])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    try:
        data = request.json['entry'][0]['changes'][0]['value']
        if 'messages' not in data: return "OK", 200
        msg = data['messages'][0]; num = msg['from']; txt = msg.get('text', {}).get('body', '').lower()
        nombre = data['contacts'][0]['profile']['name']

        if num == ADMIN_PHONE_CLEAN:
            # Comandos Admin
            if "activar" in txt and "prueba" in txt:
                conn=get_db_connection(); cur=conn.cursor(); cur.execute("UPDATE config SET value='true' WHERE key='test_mode'"); conn.commit(); release_db_connection(conn)
                enviar_whatsapp(num, "🧪 TEST ON"); return "OK", 200
            if "desactivar" in txt:
                conn=get_db_connection(); cur=conn.cursor(); cur.execute("UPDATE config SET value='false' WHERE key='test_mode'"); conn.commit(); release_db_connection(conn)
                enviar_whatsapp(num, "✅ TEST OFF"); return "OK", 200
            if "apagar bot" in txt:
                conn=get_db_connection(); cur=conn.cursor(); cur.execute("UPDATE config SET value='false' WHERE key='bot_activo'"); conn.commit(); release_db_connection(conn)
                enviar_whatsapp(num, "🔴 Bot OFF"); return "OK", 200
            if "encender bot" in txt:
                conn=get_db_connection(); cur=conn.cursor(); cur.execute("UPDATE config SET value='true' WHERE key='bot_activo'"); conn.commit(); release_db_connection(conn)
                enviar_whatsapp(num, "🟢 Bot ON"); return "OK", 200
            if any(x in txt for x in ["sí", "si", "ok", "confirmado"]):
                conn=get_db_connection(); cur=conn.cursor()
                cur.execute("SELECT id, cliente_id, monto FROM pagos WHERE estado='pendiente' ORDER BY id DESC LIMIT 1")
                pago = cur.fetchone()
                if pago:
                    cur.execute("UPDATE pagos SET estado='aprobado' WHERE id=%s", (pago['id'],))
                    conn.commit(); enviar_whatsapp(num, "✅ Aprobado."); enviar_whatsapp(pago['cliente_id'], "🎉 Pago Aprobado.")
                release_db_connection(conn); return "OK", 200
            
            conn=get_db_connection(); cur=conn.cursor(); cur.execute("SELECT value FROM config WHERE key='test_mode'"); res_test = cur.fetchone(); release_db_connection(conn)
            if not res_test or res_test['value'] == 'false': return "OK", 200

        conn=get_db_connection(); cur=conn.cursor(); cur.execute("SELECT value FROM config WHERE key='bot_activo'"); res_bot = cur.fetchone(); release_db_connection(conn)
        if (not res_bot or res_bot['value'] == 'false') and num != ADMIN_PHONE_CLEAN: return "OK", 200

        if msg['type'] == 'image': threading.Thread(target=procesar_pago, args=(msg['image']['id'], num)).start(); return "OK", 200
        if msg['type'] == 'location': return "OK", 200 

        if num in user_buffers: user_buffers[num]['timer'].cancel(); user_buffers[num]['text'] += f" {txt}"
        else: user_buffers[num] = {'text': txt}
        
        t = threading.Timer(5.0, procesar_cliente, args=[num, nombre, user_buffers[num]['text']])
        user_buffers[num]['timer'] = t; t.start()
        threading.Timer(6.0, lambda: user_buffers.pop(num, None)).start()

    except Exception as e: print(e)
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))