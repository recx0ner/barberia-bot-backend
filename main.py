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
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"❌ Error DB: {e}")
        return None

def obtener_tasas_dinamicas():
    conn = get_db_connection()
    tasas = {"usd": 385.50, "eur": 415.00}
    if not conn: return tasas
    cur = conn.cursor()
    try:
        r_usd = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=3).json()
        tasas['usd'] = float(r_usd['rates']['VES'])
        r_eur = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=3).json()
        tasas['eur'] = float(r_eur['rates']['VES'])
    except: pass
    finally: cur.close(); conn.close()
    return tasas

def enviar_meta(to, text):
    try:
        url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Error enviando a Meta: {e}")

# 🛡️ IA BLINDADA
def consultar_ia(instruccion, texto, historial="", img_id=None):
    try:
        img_b64 = None
        if img_id:
            try:
                res = requests.get(f"https://graph.facebook.com/v18.0/{img_id}", headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=10)
                img_url = res.json().get('url')
                if img_url:
                    img_data = requests.get(img_url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=10).content
                    img_b64 = base64.b64encode(img_data).decode('utf-8')
            except: pass

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        
        payload = {
            "model": MODELO_IA,
            "messages": [
                {"role": "system", "content": instruccion},
                {"role": "user", "content": [
                    {"type": "text", "text": f"{historial}\nActual: {texto}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}} if img_b64 else None
                ]}
            ],
            "temperature": 0.1
        }
        payload["messages"][1]["content"] = [i for i in payload["messages"][1]["content"] if i]

        response = requests.post(url, headers=headers, json=payload, timeout=20)
        
        if response.status_code == 200:
            data = response.json()
            if 'choices' in data: return data['choices'][0]['message']['content']
        return "⚠️ Error de conexión."

    except Exception as e:
        print(f"🔥 Error IA: {e}")
        return "⚠️ Error técnico."

# --- ADMIN ---
def ejecutar_comando_admin(msg_text, num):
    try:
        conn = get_db_connection()
        if not conn: return
        cur = conn.cursor()
        
        prompt = "ADMIN: [BOT:ON], [BOT:OFF], [CONSULTAR_TASA], [MODO_PRUEBA:ON]."
        intent = consultar_ia(prompt, msg_text)
        
        if "[MODO_PRUEBA:ON]" in intent:
            cur.execute("UPDATE config SET value = 'true' WHERE key = 'test_mode'")
            enviar_meta(num, "🧪 MODO PRUEBA ON")
        elif "[CONSULTAR_TASA]" in intent:
            t = obtener_tasas_dinamicas()
            enviar_meta(num, f"📊 USD: {t['usd']} | EUR: {t['eur']}")
        else:
            enviar_meta(num, "✅ Admin OK.")
        
        conn.commit(); cur.close(); conn.close()
    except: pass

# --- CLIENTE ---
def procesar_flujo_cliente(id_num, num, name_wa, texto_extra=None):
    try:
        conn = get_db_connection()
        if not conn: return
        cur = conn.cursor()

        buffer = user_buffers.get(id_num, {"text": ""})
        texto_acum = (buffer.get("text", "") + " " + (texto_extra or "")).strip()
        if id_num in user_buffers: del user_buffers[id_num]
        
        if not texto_acum: 
            cur.close(); conn.close(); return

        t = obtener_tasas_dinamicas()
        cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 6", (id_num,))
        hist = "".join([f"Usuario: {f['user_input']}\nBot: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
        
        # PROMPT CORREGIDO: ETIQUETAS AL FINAL
        instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\n
        REGLAS DE FORMATO (IMPORTANTE):
        1. NO uses prefijos como "Bot:" o "B:". Responde directamente.
        2. Escribe de forma natural. NO incluyas etiquetas dentro de las oraciones.
        3. Pon las etiquetas TÉCNICAS SIEMPRE AL FINAL del mensaje, en una línea separada.
        
        ETIQUETAS DISPONIBLES:
        - [NOMBRE: nombre detectado]
        - [AGENDAR] (si pide producto)
        - [FINALIZAR] (si da dirección/GPS -> da el total y pago móvil)
        - [DIRECCION: dirección detectada]
        - [CANCELAR]
        """
        
        res_ia = consultar_ia(instr, texto_acum, hist)

        # Limpieza de prefijos alucinados (Solución a image_1fc7d0)
        res_ia = res_ia.replace("Bot:", "").replace("B:", "").strip()

        # Procesar etiquetas
        if "[NOMBRE:" in res_ia:
            try:
                n = res_ia.split("[NOMBRE:")[1].split("]")[0].strip()
                cur.execute("UPDATE cliente SET nombre = %s WHERE id = %s", (n, id_num))
            except: pass

        if "[AGENDAR]" in res_ia:
            cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando') ON CONFLICT DO NOTHING", (id_num,))
        
        if "[DIRECCION:" in res_ia:
            try:
                d = res_ia.split("[DIRECCION:")[1].split("]")[0].strip()
                cur.execute("UPDATE pedidos SET direccion = %s WHERE user_id = %s AND estado = 'confirmando'", (d, id_num))
            except: pass

        if "[FINALIZAR]" in res_ia: 
            cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
            enviar_meta(ADMIN_PHONE, f"🚨 NUEVO PEDIDO: {name_wa}")

        conn.commit()

        # Limpiar etiquetas del mensaje final al cliente
        limpia = re.sub(r'\[.*?\]', '', res_ia).strip()
        
        if limpia:
            enviar_meta(num, limpia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_acum, limpia))
            conn.commit()
            
        cur.close(); conn.close()

    except Exception as e:
        print(f"🔥 Error Flujo: {e}")

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    try:
        body = request.json
        entry = body.get('entry', [])[0]
        changes = entry.get('changes', [])[0]
        value = changes.get('value', {})
        
        if 'messages' in value:
            msg = value['messages'][0]
            num = msg['from']; id_num = int(num); msg_id = msg.get('id')

            if msg_id in procesados: return jsonify({"status": "ok"}), 200
            procesados.add(msg_id)

            conn = get_db_connection()
            if not conn: return jsonify({"status": "ok"}), 200
            cur = conn.cursor()
            
            cur.execute("SELECT value FROM config WHERE key = 'test_mode'")
            row = cur.fetchone()
            test_mode = row['value'] == 'true' if row else False
            num_lim = str(num).replace("+", "").strip()
            msg_body = msg.get('text', {}).get('body', "")

            if num_lim == ADMIN_PHONE and "[MODO_PRUEBA:OFF]" in msg_body:
                cur.execute("UPDATE config SET value = 'false' WHERE key = 'test_mode'")
                conn.commit(); cur.close(); conn.close()
                enviar_meta(num, "✅ Modo Prueba OFF.")
                return jsonify({"status": "ok"}), 200

            cur.close(); conn.close()

            if num_lim == ADMIN_PHONE and not test_mode:
                threading.Thread(target=ejecutar_comando_admin, args=(msg_body, num)).start()
            else:
                name_wa = value.get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
                
                if msg.get('type') == 'location':
                    gps = f"{msg['location']['latitude']},{msg['location']['longitude']}"
                    conn = get_db_connection(); cur = conn.cursor()
                    cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado = 'confirmando'", (gps, id_num))
                    conn.commit(); cur.close(); conn.close()
                    threading.Thread(target=procesar_flujo_cliente, args=(id_num, num, name_wa, "He enviado mi GPS.")).start()
                
                elif msg.get('type') == 'text':
                    if id_num in user_buffers:
                        user_buffers[id_num]["timer"].cancel()
                        user_buffers[id_num]["text"] += f" {msg_body}"
                    else:
                        user_buffers[id_num] = {"text": msg_body}
                    t = threading.Timer(5.0, procesar_flujo_cliente, args=[id_num, num, name_wa])
                    user_buffers[id_num]["timer"] = t; t.start()

    except Exception as e: print(f"🔥 Error: {e}")
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)