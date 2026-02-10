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

# Cache en memoria
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
    tasas = {"usd": 385.50, "eur": 415.00} # Valores por defecto
    if not conn: return tasas
    
    cur = conn.cursor()
    try:
        # Intento de API
        r_usd = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=3).json()
        tasas['usd'] = float(r_usd['rates']['VES'])
        r_eur = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=3).json()
        tasas['eur'] = float(r_eur['rates']['VES'])
    except:
        # Fallback a DB si API falla
        pass
    finally: 
        cur.close(); conn.close()
    return tasas

def enviar_meta(to, text):
    try:
        url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Error enviando a Meta: {e}")

# 🛡️ FUNCIÓN IA BLINDADA (SOLUCIÓN KEYERROR)
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
            except Exception as e:
                print(f"⚠️ Error descargando imagen: {e}")

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        
        payload = {
            "model": MODELO_IA,
            "messages": [
                {"role": "system", "content": instruccion},
                {"role": "user", "content": [
                    {"type": "text", "text": f"{historial}\n{texto}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}} if img_b64 else None
                ]}
            ],
            "temperature": 0.1
        }
        # Limpiar contenido nulo (por si no hay imagen)
        payload["messages"][1]["content"] = [i for i in payload["messages"][1]["content"] if i]

        response = requests.post(url, headers=headers, json=payload, timeout=20)
        
        if response.status_code == 200:
            data = response.json()
            if 'choices' in data and len(data['choices']) > 0:
                return data['choices'][0]['message']['content']
            else:
                print(f"❌ API Respondió pero sin 'choices': {data}")
                return "⚠️ Estoy teniendo un pequeño lapso de memoria. ¿Podrías repetirme eso?"
        else:
            print(f"🔥 Error API {response.status_code}: {response.text}")
            return "⚠️ Error de conexión con mi cerebro digital."

    except Exception as e:
        print(f"🔥 EXCEPCIÓN CRÍTICA EN IA: {e}")
        return "⚠️ Error técnico interno."

# --- 👑 LÓGICA ADMIN ---
def ejecutar_comando_admin(msg_text, num):
    try:
        conn = get_db_connection()
        if not conn: return
        cur = conn.cursor()
        
        prompt = "ADMIN: [BOT:ON], [BOT:OFF], [CONSULTAR_TASA], [MODO_PRUEBA:ON], [LISTAR_PENDIENTES]."
        intent = consultar_ia(prompt, msg_text)
        
        if "[MODO_PRUEBA:ON]" in intent:
            cur.execute("UPDATE config SET value = 'true' WHERE key = 'test_mode'")
            enviar_meta(num, "🧪 *MODO PRUEBA ACTIVADO*")
        elif "[CONSULTAR_TASA]" in intent:
            t = obtener_tasas_dinamicas()
            enviar_meta(num, f"📊 Tasas: {t['usd']} Bs/$ | {t['eur']} Bs/€")
        else:
            enviar_meta(num, "✅ Admin: Comando procesado.")
        
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"Error Admin Thread: {e}")

# --- 🍕 LÓGICA CLIENTE ---
def procesar_flujo_cliente(id_num, num, name_wa, texto_extra=None):
    try:
        conn = get_db_connection()
        if not conn: return
        cur = conn.cursor()

        # Consumir Buffer
        buffer = user_buffers.get(id_num, {"text": ""})
        texto_acum = (buffer.get("text", "") + " " + (texto_extra or "")).strip()
        if id_num in user_buffers: del user_buffers[id_num]
        
        if not texto_acum: 
            cur.close(); conn.close(); return

        t = obtener_tasas_dinamicas()
        cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 6", (id_num,))
        hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
        
        instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\n
        REGLAS TÉCNICAS OBLIGATORIAS:
        1. Si elige producto/sabor: usa [AGENDAR].
        2. Si da dirección/GPS: usa [FINALIZAR].
        3. Si escribe dirección: usa [DIRECCION: texto].
        4. Si se presenta: usa [NOMBRE: texto].
        """
        
        res_ia = consultar_ia(instr, texto_acum, hist)

        # ⚙️ PROCESAMIENTO ETIQUETAS
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

        # 🧹 LIMPIEZA TOTAL DE ETIQUETAS (SOLUCIÓN A TU FOTO)
        limpia = re.sub(r'\[.*?\]', '', res_ia).strip()
        
        if limpia:
            enviar_meta(num, limpia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_acum, limpia))
            conn.commit()
            
        cur.close(); conn.close()

    except Exception as e:
        print(f"🔥 Error Flujo Cliente: {e}")
        enviar_meta(num, "⚠️ Ocurrió un error procesando tu mensaje. Intenta de nuevo.")

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
            num = msg['from']
            id_num = int(num)
            msg_id = msg.get('id')

            # Anti-duplicados
            if msg_id in procesados: return jsonify({"status": "ok"}), 200
            procesados.add(msg_id)

            conn = get_db_connection()
            if not conn: return jsonify({"status": "error_db"}), 200
            cur = conn.cursor()
            
            # Verificar Test Mode
            cur.execute("SELECT value FROM config WHERE key = 'test_mode'")
            row = cur.fetchone()
            test_mode = row['value'] == 'true' if row else False
            
            num_lim = str(num).replace("+", "").strip()
            msg_body = msg.get('text', {}).get('body', "")

            # 1. Salida de Emergencia
            if num_lim == ADMIN_PHONE and "[MODO_PRUEBA:OFF]" in msg_body:
                cur.execute("UPDATE config SET value = 'false' WHERE key = 'test_mode'")
                conn.commit(); cur.close(); conn.close()
                enviar_meta(num, "✅ Modo Prueba OFF.")
                return jsonify({"status": "ok"}), 200

            cur.close(); conn.close()

            # 2. Rutas
            if num_lim == ADMIN_PHONE and not test_mode:
                threading.Thread(target=ejecutar_comando_admin, args=(msg_body, num)).start()
            else:
                # Cliente o Admin en Test
                name_wa = value.get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
                
                if msg.get('type') == 'location':
                    # Guardar GPS y forzar respuesta
                    conn = get_db_connection(); cur = conn.cursor()
                    gps = f"{msg['location']['latitude']},{msg['location']['longitude']}"
                    cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado = 'confirmando'", (gps, id_num))
                    conn.commit(); cur.close(); conn.close()
                    
                    threading.Thread(target=procesar_flujo_cliente, args=(id_num, num, name_wa, "He enviado mi GPS.")).start()
                
                elif msg.get('type') == 'text':
                    # Debouncing 5s
                    if id_num in user_buffers:
                        user_buffers[id_num]["timer"].cancel()
                        user_buffers[id_num]["text"] += f" {msg_body}"
                    else:
                        user_buffers[id_num] = {"text": msg_body}
                    
                    t = threading.Timer(5.0, procesar_flujo_cliente, args=[id_num, num, name_wa])
                    user_buffers[id_num]["timer"] = t; t.start()

    except Exception as e:
        print(f"🔥 Error Webhook General: {e}")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)