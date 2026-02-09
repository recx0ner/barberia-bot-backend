import os, requests, psycopg2, base64, json
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

MODELO_IA = "google/gemini-2.5-flash" #

# --- UTILIDADES ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def obtener_tasas_dinamicas():
    """Consulta fuentes y usa respaldo en DB si fallan"""
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
    return {"usd": t_usd, "eur": t_usd * 1.08}

def consultar_ia(instruccion, texto, historial="", img_id=None):
    """Cerebro Gemini 2.5 Flash para texto y visión"""
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
    # Filtramos el None de la lista de contenido si no hay imagen
    payload["messages"][1]["content"] = [i for i in payload["messages"][1]["content"] if i]
    return requests.post(url, headers=headers, json=payload).json()['choices'][0]['message']['content']

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

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

            # 🛡️ MODO ADMIN
            if num == ADMIN_PHONE:
                res_ia = consultar_ia("Tags: [APROBAR:REF], [LISTAR_PENDIENTES], [CONSULTAR_TASA].", msg.get('text', {}).get('body', ""))
                if "[APROBAR:" in res_ia:
                    ref = res_ia.split(":")[1].replace("]", "").strip()
                    cur.execute("UPDATE pagos SET estado = 'aprobado' WHERE referencia = %s", (ref,))
                    conn.commit(); enviar_meta(ADMIN_PHONE, f"✅ Ref {ref} aprobada.")
                elif "[LISTAR_PENDIENTES]" in res_ia:
                    cur.execute("SELECT referencia, monto FROM pagos WHERE estado = 'pendiente'")
                    p = cur.fetchall()
                    enviar_meta(ADMIN_PHONE, "📝 Pendientes:\n" + "\n".join([f"- {i['referencia']} ({i['monto']} Bs)" for i in p]) if p else "✅ Al día.")
                return jsonify({"status": "ok"}), 200

            # 🍕 MODO CLIENTE - AUTO-REGISTRO
            cur.execute("SELECT id FROM cliente WHERE id = %s", (id_num,))
            if not cur.fetchone():
                cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s)", (id_num, name, str(num)))
                conn.commit()

            # 📸 PROCESAMIENTO DE IMAGEN (ANTIFRAUDE)
            if msg.get('type') == 'image':
                inst = "Extrae JSON: {ref: text, monto: float}. Si no es pago, di 'null'."
                res_pago = consultar_ia(inst, "Analiza pago", "", msg['image']['id'])
                try:
                    p = json.loads(res_pago)
                    ref, monto = str(p['ref']), float(p['monto'])
                    
                    # Verificar Duplicados
                    cur.execute("SELECT id FROM pagos WHERE referencia = %s", (ref,))
                    if cur.fetchone():
                        enviar_meta(num, "❌ Error: Esta referencia ya fue registrada. No se permiten duplicados.")
                    else:
                        cur.execute("INSERT INTO pagos (user_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (id_num, ref, monto))
                        conn.commit()
                        # Sumar pagos parciales
                        cur.execute("SELECT SUM(monto) as total FROM pagos WHERE user_id = %s", (id_num,))
                        acumulado = cur.fetchone()['total']
                        enviar_meta(num, f"✅ Comprobante {ref} recibido. Total abonado: {acumulado} Bs.")
                        enviar_meta(ADMIN_PHONE, f"🚨 *PAGO NUEVO*\nDe: {name}\nRef: {ref}\nMonto: {monto} Bs.\nAcumulado: {acumulado} Bs.")
                except: enviar_meta(num, "⚠️ No pude leer los datos del pago. Envía una foto más clara.")
                return jsonify({"status": "ok"}), 200

            # 📍 UBICACIÓN
            if msg.get('type') == 'location':
                loc = f"{msg['location']['latitude']},{msg['location']['longitude']}"
                cur.execute("UPDATE pedidos SET gps = %s WHERE user_id = %s AND estado IN ('confirmando','esperando_pago')", (loc, id_num))
                cur.execute("INSERT INTO ubicaciones (user_id, latitud, longitud) VALUES (%s, %s, %s)", (id_num, loc.split(',')[0], loc.split(',')[1]))
                conn.commit(); enviar_meta(num, "📍 Ubicación guardada. ¡Gracias!"); return jsonify({"status": "ok"}), 200

            # 💬 TEXTO Y MEMORIA
            t = obtener_tasas_dinamicas()
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 10", (id_num,))
            hist = "".join([f"U: {f['user_input']}\nB: {f['bot_response']}\n" for f in reversed(cur.fetchall())])
            
            instr = f"{BUSINESS_CONTEXT}\nTasa: {t['usd']} Bs.\nREGLAS: [AGENDAR] al iniciar. Al cerrar: Resume orden, da total ($ y Bs), pide GPS y usa [FINALIZAR]."
            res_ia = consultar_ia(instr, msg.get('text', {}).get('body', "Archivo"), hist)

            if "[AGENDAR]" in res_ia: cur.execute("INSERT INTO pedidos (user_id, estado, fecha) VALUES (%s, 'confirmando', %s)", (id_num, datetime.now(venezuela_tz)))
            if "[FINALIZAR]" in res_ia: cur.execute("UPDATE pedidos SET estado = 'esperando_pago' WHERE user_id = %s AND estado = 'confirmando'", (id_num,))
            conn.commit()
            
            limpia = res_ia.replace("[AGENDAR]","").replace("[FINALIZAR]","")
            enviar_meta(num, limpia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, msg.get('text', {}).get('body', ""), limpia))
            conn.commit(); cur.close(); conn.close()
            
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)