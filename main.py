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
    try: return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except: return None

def enviar_meta(to, text):
    try:
        url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}, timeout=10)
    except: pass

# 👁️ SISTEMA DE VISIÓN (OCR DE PAGOS)
def analizar_comprobante(img_id):
    try:
        # Descargar imagen de Meta
        url_get = f"https://graph.facebook.com/v18.0/{img_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        res_url = requests.get(url_get, headers=headers).json().get('url')
        img_data = requests.get(res_url, headers=headers).content
        img_b64 = base64.b64encode(img_data).decode('utf-8')

        # Consultar a Gemini Vision
        url_ai = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": MODELO_IA,
            "messages": [
                {"role": "system", "content": "Eres un auditor financiero. Extrae la REFERENCIA (números) y el MONTO del comprobante bancario. Responde SOLO en JSON: {\"ref\": \"123456\", \"monto\": 100.50}. Si no es un pago, responde {\"error\": \"no_es_pago\"}."},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}]}
            ]
        }
        res = requests.post(url_ai, headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"}, json=payload).json()
        content = res['choices'][0]['message']['content'].replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        print(f"Error OCR: {e}")
        return {"error": "falla_tecnica"}

def consultar_ia(instruccion, texto, historial=""):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": MODELO_IA,
            "messages": [{"role": "system", "content": instruccion}, {"role": "user", "content": f"{historial}\nActual: {texto}"}],
            "temperature": 0.1
        }
        res = requests.post(url, headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"}, json=payload, timeout=15).json()
        if 'choices' in res: return res['choices'][0]['message']['content']
    except: pass
    return "⚠️ Error de conexión."

# --- FLUJO CLIENTE ---
def procesar_flujo_cliente(id_num, num, texto_entrada, es_imagen=False, img_id=None):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        
        # Buffer de texto
        if not es_imagen:
            buffer = user_buffers.get(id_num, {"text": ""})
            texto_acum = (buffer.get("text", "") + " " + texto_entrada).strip()
            if id_num in user_buffers: del user_buffers[id_num]
        else:
            texto_acum = "[IMAGEN_ENVIADA]"

        # 💰 LÓGICA DE PAGOS (Antifraude)
        info_pago = ""
        if es_imagen and img_id:
            datos = analizar_comprobante(img_id)
            if "ref" in datos:
                # Verificar duplicado
                cur.execute("SELECT id FROM pagos WHERE referencia = %s", (str(datos['ref']),))
                if cur.fetchone():
                    enviar_meta(num, f"❌ La referencia {datos['ref']} ya fue registrada antes.")
                    return # Cortamos flujo aquí
                else:
                    # Registrar pago
                    cur.execute("INSERT INTO pagos (user_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (id_num, str(datos['ref']), datos['monto']))
                    conn.commit()
                    info_pago = f"SISTEMA: El cliente envió comprobante Ref {datos['ref']} por {datos['monto']} Bs. CALCULA EL RESTANTE."
            else:
                info_pago = "SISTEMA: El cliente envió una imagen que NO parece un pago."

        # Historial
        cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 6", (id_num,))
        hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
        
        # Recuperar nombre actual
        cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
        cliente_db = cur.fetchone()
        nombre_actual = cliente_db['nombre'] if cliente_db else "Por definir"

        prompt = f"""{BUSINESS_CONTEXT}
        Cliente: {nombre_actual}.
        Nota del Sistema: {info_pago}
        
        REGLAS DE FLUJO:
        1. Si el nombre es "Por definir" o "Cliente Nuevo", TU PRIORIDAD es preguntar su nombre. NO tomes pedido aún.
        2. Cuando te diga su nombre, usa la etiqueta [NOMBRE: ...].
        3. Si envía pago: Confirma recepción, resta el monto del total del pedido y dile cuánto le falta (o si está listo).
        4. ETIQUETAS AL FINAL: [AGENDAR], [FINALIZAR], [CANCELAR], [DIRECCION:...].
        5. [CANCELAR]: Úsalo si el cliente dice "cancela", "olvídalo" o "no quiero nada".
        """

        res_ia = consultar_ia(prompt, texto_acum, hist)

        # Procesar Etiquetas
        if "[NOMBRE:" in res_ia:
            n = res_ia.split("[NOMBRE:")[1].split("]")[0].strip()
            cur.execute("UPDATE cliente SET nombre = %s WHERE id = %s", (n, id_num))
        
        if "[CANCELAR]" in res_ia:
            cur.execute("UPDATE pedidos SET estado = 'cancelado' WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (id_num,))
            res_ia = "Entendido, pedido cancelado. Avísame si deseas algo más." # Respuesta forzada limpia

        if "[AGENDAR]" in res_ia:
            cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando') ON CONFLICT DO NOTHING", (id_num,))

        if "[FINALIZAR]" in res_ia: 
            cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
            enviar_meta(ADMIN_PHONE, f"🚨 PEDIDO CONFIRMADO: {nombre_actual}")

        conn.commit()
        
        # Limpieza
        limpia = re.sub(r'\[.*?\]', '', res_ia).replace("Bot:", "").strip()
        if limpia:
            enviar_meta(num, limpia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_acum, limpia))
            conn.commit()
            
        cur.close(); conn.close()

    except Exception as e: print(f"Error: {e}")

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    try:
        data = request.json['entry'][0]['changes'][0]['value']
        if 'messages' in data:
            msg = data['messages'][0]; num = msg['from']; id_num = int(num); msg_id = msg.get('id')
            if msg_id in procesados: return jsonify({"status": "ok"}), 200
            procesados.add(msg_id)

            conn = get_db_connection(); cur = conn.cursor()
            
            # 1. Registro Inicial "Mudo" (Para evitar error de Foreign Key)
            cur.execute("SELECT id FROM cliente WHERE id = %s", (id_num,))
            if not cur.fetchone():
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, 'Cliente Nuevo', %s)", (id_num, str(num)))
                conn.commit()
            
            # Verificar Test Mode
            cur.execute("SELECT value FROM config WHERE key = 'test_mode'")
            row = cur.fetchone(); test_mode = row['value'] == 'true' if row else False
            cur.close(); conn.close()

            # Rutas
            if str(num).replace("+","") == ADMIN_PHONE and not test_mode:
                return jsonify({"status": "admin_mode"}), 200 # Aquí iría tu lógica admin

            # Cliente (o Admin en Test)
            if msg['type'] == 'text':
                txt = msg['text']['body']
                # Salida Emergencia
                if "[MODO_PRUEBA:OFF]" in txt: 
                    conn = get_db_connection(); cur = conn.cursor()
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'test_mode'")
                    conn.commit(); cur.close(); conn.close()
                    enviar_meta(num, "✅ Modo Prueba OFF"); return jsonify({"status": "ok"}), 200

                if id_num in user_buffers:
                    user_buffers[id_num]["timer"].cancel(); user_buffers[id_num]["text"] += f" {txt}"
                else: user_buffers[id_num] = {"text": txt}
                t = threading.Timer(5.0, procesar_flujo_cliente, args=[id_num, num, txt])
                user_buffers[id_num]["timer"] = t; t.start()
            
            elif msg['type'] == 'image':
                # Procesar imagen inmediatamente (sin delay 5s) para agilidad
                threading.Thread(target=procesar_flujo_cliente, args=(id_num, num, "", True, msg['image']['id'])).start()

    except: pass
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)