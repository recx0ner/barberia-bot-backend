import os, requests, psycopg2, base64, json, threading, time
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")
MODELO_IA = "google/gemini-2.5-flash"

# 🧠 MEMORIA TEMPORAL PARA DELAY (DEBOUNCE)
user_buffers = {} # {id_num: {"text": "", "timer": TimerObject, "name": ""}}

# --- UTILIDADES ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_tasas_dinamicas():
    """Consulta fuentes y usa respaldo en DB"""
    fuentes = ["https://ve.dolarapi.com/v1/dolares/oficial", "https://api.exchangerate-api.com/v4/latest/USD"]
    conn = get_db_connection(); cur = conn.cursor()
    t_usd = None
    for url in fuentes:
        try:
            res = requests.get(url, timeout=5).json()
            t_usd = float(res.get('promedio') or res['rates']['VES'])
            if t_usd > 300:
                cur.execute("UPDATE config SET value = %s WHERE key = 'last_tasa'", (str(t_usd),))
                conn.commit(); break
        except: continue
    if not t_usd:
        cur.execute("SELECT value FROM config WHERE key = 'last_tasa'"); r = cur.fetchone()
        t_usd = float(r['value']) if r else 385.50
    cur.close(); conn.close()
    return {"usd": t_usd}

def consultar_ia(instruccion, texto, historial="", img_id=None):
    """Cerebro Gemini con visión reforzada"""
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

# --- ⚙️ LÓGICA DE PROCESAMIENTO DIFERIDO (EL MOTOR DEL DELAY) ---
def procesar_respuesta_acumulada(id_num, num, name):
    """Se ejecuta solo cuando el cliente deja de escribir"""
    time.sleep(1) # Pequeño margen de seguridad
    conn = get_db_connection(); cur = conn.cursor()
    
    # 1. Recuperar lo acumulado y limpiar buffer
    buffer = user_buffers.get(id_num, {})
    texto_final = buffer.get("text", "").strip()
    if not texto_final: return
    
    del user_buffers[id_num] # Limpiar para el siguiente bloque de mensajes

    # 2. Lógica de Tasas y Memoria
    t = obtener_tasas_dinamicas()
    cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (id_num,))
    hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
    
    instr = f"""{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\nREGLAS: [AGENDAR] al iniciar, [FINALIZAR] al cerrar con resumen y GPS, [CANCELAR] para anular."""
    
    # 3. Consultar IA
    res_ia = consultar_ia(instr, texto_final, hist)

    # 4. Etiquetas de DB
    if "[AGENDAR]" in res_ia: cur.execute("INSERT INTO pedidos (user_id, estado) VALUES (%s, 'confirmando')", (id_num,))
    if "[FINALIZAR]" in res_ia: cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
    if "[CANCELAR]" in res_ia: cur.execute("UPDATE pedidos SET estado = 'cancelado' WHERE user_id = %s", (id_num,))
    conn.commit()

    # 5. Enviar respuesta limpia
    limpia = res_ia.replace("[AGENDAR]","").replace("[FINALIZAR]","").replace("[CANCELAR]","")
    enviar_meta(num, limpia)
    cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_final, limpia))
    conn.commit(); cur.close(); conn.close()

# --- WEBHOOK ---
@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    body = request.json
    data = body['entry'][0]['changes'][0]['value']
    if 'messages' in data:
        msg = data['messages'][0]; num = msg['from']; id_num = int(num)
        name = body['entry'][0]['changes'][0]['value'].get('contacts', [{}])[0].get('profile', {}).get('name', 'Cliente')
        
        # 🛡️ EXCEPCIÓN: Imágenes, Ubicación y Admin se procesan al instante
        if msg.get('type') in ['image', 'location'] or str(num) == str(ADMIN_PHONE):
            # (Aquí va la lógica de pagos/gps/admin que ya tienes, procesada inmediatamente)
            # Para brevedad, el código sigue procesando texto con delay
            pass

        # ⏳ LÓGICA DE DELAY PARA TEXTO
        if msg.get('type') == 'text':
            texto_nuevo = msg['text']['body']
            
            # Si el usuario ya tiene un buffer, cancelamos el timer anterior y sumamos el texto
            if id_num in user_buffers:
                user_buffers[id_num]["timer"].cancel()
                user_buffers[id_num]["text"] += f" {texto_nuevo}"
            else:
                user_buffers[id_num] = {"text": texto_nuevo, "name": name}

            # Creamos un nuevo timer de 7 segundos
            nuevo_timer = threading.Timer(7.0, procesar_respuesta_acumulada, args=[id_num, num, name])
            user_buffers[id_num]["timer"] = nuevo_timer
            nuevo_timer.start()

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)