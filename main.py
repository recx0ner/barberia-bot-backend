import os, requests, traceback, psycopg2, base64
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify

app = Flask(__name__)
# Zona horaria local para registros precisos en Venezuela
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") 
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

# Modelo Gemini 2.5 Flash vía OpenRouter (Multimodal: Texto + Imágenes)
MODELO_IA = "google/gemini-2.5-flash"

def get_db_connection():
    """Conexión a la base de datos Neon"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def enviar_meta(to, text):
    """Envía mensajes a través de la API de WhatsApp Business"""
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def descargar_imagen_meta(image_id):
    """Descarga el comprobante de pago enviado por el cliente"""
    try:
        res = requests.get(f"https://graph.facebook.com/v18.0/{image_id}", 
                           headers={"Authorization": f"Bearer {META_TOKEN}"})
        url_descarga = res.json().get('url')
        img_res = requests.get(url_descarga, headers={"Authorization": f"Bearer {META_TOKEN}"})
        return base64.b64encode(img_res.content).decode('utf-8')
    except:
        return None

def generar_respuesta_ia(instruccion, texto_usuario, img_b64=None):
    """Procesa la intención del usuario con Gemini 2.5 Flash"""
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        
        contenido = [{"type": "text", "text": texto_usuario}]
        if img_b64:
            contenido.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
            })

        payload = {
            "model": MODELO_IA,
            "messages": [
                {"role": "system", "content": instruccion},
                {"role": "user", "content": contenido}
            ],
            "temperature": 0.5
        }
        response = requests.post(url, headers=headers, json=payload)
        return response.json()['choices'][0]['message']['content'].strip()
    except:
        return "⚠️ Error de conexión con el motor de IA."

def ejecutar_logica_negocio(id_num, accion, texto_cliente="", nombre_actual="Cliente", extra_data=None):
    """Gestiona registros de clientes, pedidos y pagos en Neon"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Siempre registrar/actualizar cliente
        cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", 
                    (id_num, nombre_actual, str(id_num)))
        
        if accion == "AGENDAR":
            cur.execute("INSERT INTO citas (user_id, nombre, servicio, fecha_hora, estado) VALUES (%s, %s, %s, %s, 'pendiente')",
                        (id_num, nombre_actual, texto_cliente[:200], datetime.now(venezuela_tz)))
            return "✅ Pedido agendado. Por favor, envía los datos del Pago Móvil (Bs.)."
            
        elif accion == "PAGO" and extra_data:
            ref, monto, banco = extra_data[0].strip(), extra_data[1].strip(), extra_data[2].strip()
            # Evitar datos basura de la IA
            if ref.lower() == "ref" or "monto" in monto.lower():
                return "⚠️ No pude extraer los datos del pago. ¿Podrías indicarme Referencia, Monto en Bs. y Banco?"
            
            cur.execute("INSERT INTO pagos (user_id, referencia, monto, banco, estado) VALUES (%s, %s, %s, %s, 'pendiente')", 
                        (id_num, ref, monto, banco))
            conn.commit()
            
            # Notificación al Administrador
            msg_admin = f"⚠️ *PAGO POR VERIFICAR*\n👤 Cliente: {nombre_actual}\n💰 Monto: {monto}\n🏦 Banco: {banco}\n🔢 Ref: {ref}\n\nResponde 'sí' para confirmar."
            enviar_meta(ADMIN_PHONE, msg_admin)
            return f"⏳ Referencia {ref} de {banco} recibida. Validaremos el ingreso y te avisaré pronto."
            
        conn.commit()
    except:
        conn.rollback()
        return "✅ Procesado."
    finally:
        cur.close(); conn.close()

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200
        
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero = msg['from']
            id_num = int(numero)
            
            # Identificación del tipo de mensaje
            texto_cliente = msg.get('text', {}).get('body', "").strip() if msg.get('type') == 'text' else "(Imagen enviada)"
            img_data = descargar_imagen_meta(msg['image']['id']) if msg.get('type') == 'image' else None

            conn = get_db_connection()
            cur = conn.cursor()
            
            # --- 🛡️ BLOQUE EXCLUSIVO ADMINISTRADOR (Anti-Bucle) ---
            if ADMIN_PHONE and numero == ADMIN_PHONE:
                # Consultar pagos pendientes en Neon
                cur.execute("SELECT p.referencia, p.monto, p.banco, c.nombre, p.user_id FROM pagos p JOIN cliente c ON p.user_id = c.id WHERE p.estado = 'pendiente' ORDER BY p.fecha_pago DESC")
                pendientes = cur.fetchall()
                
                instruccion_admin = f"""Eres el asistente de gestión de Pizzas El Guaro.
                NO saludes al admin. Analiza su intención:
                - Si pregunta por pendientes: [LISTAR:PAGOS]
                - Si confirma un pago (si, ok, listo, ya llego): [CONFIRMAR:REF]
                - Si pide apagar bot: [BOT:OFF], si pide encender: [BOT:ON]
                Total pendientes: {len(pendientes)}."""
                
                res_admin = generar_respuesta_ia(instruccion_admin, texto_cliente)

                if "[LISTAR:PAGOS]" in res_admin:
                    if not pendientes:
                        enviar_meta(ADMIN_PHONE, "✅ No hay pagos pendientes por ahora.")
                    else:
                        txt = "📋 *PAGOS EN ESPERA:*\n"
                        for p in pendientes:
                            txt += f"• {p['nombre']}: {p['monto']} ({p['banco']}) Ref: {p['referencia']}\n"
                        enviar_meta(ADMIN_PHONE, txt)
                
                elif "[CONFIRMAR:REF]" in res_admin and pendientes:
                    p = pendientes[0] # Confirmar el último por defecto
                    cur.execute("UPDATE pagos SET estado = 'confirmado' WHERE referencia = %s", (p['referencia'],))
                    cur.execute("UPDATE citas SET estado = 'pagado' WHERE user_id = %s AND estado = 'pendiente'", (p['user_id'],))
                    conn.commit()
                    enviar_meta(str(p['user_id']), f"✅ *¡Pago Confirmado!* Referencia {p['referencia']} válida. ¡Tu pizza va al horno! 🍕")
                    enviar_meta(ADMIN_PHONE, f"👌 Hecho. Pago de {p['nombre']} confirmado.")
                
                elif "[BOT:OFF]" in res_admin:
                    cur.execute("UPDATE config SET value = 'false' WHERE key = 'bot_active'"); conn.commit()
                    enviar_meta(numero, "😴 Bot desactivado.")
                elif "[BOT:ON]" in res_admin:
                    cur.execute("UPDATE config SET value = 'true' WHERE key = 'bot_active'"); conn.commit()
                    enviar_meta(numero, "🤖 Bot reactivado.")
                
                cur.close(); conn.close()
                return jsonify({"status": "admin_processed"}), 200

            # --- 🍕 LÓGICA DE CLIENTE ---
            cur.execute("SELECT value FROM config WHERE key = 'bot_active'")
            if cur.fetchone()['value'] == 'false':
                return jsonify({"status": "off"}), 200

            # Memoria e Identidad
            cur.execute("SELECT nombre FROM cliente WHERE id = %s", (id_num,))
            cli = cur.fetchone()
            nombre_actual = cli['nombre'] if cli and cli['nombre'].lower() != "nombre" else "Desconocido"
            
            cur.execute("SELECT user_input, bot_response FROM messages WHERE user_id = %s ORDER BY created_at DESC LIMIT 5", (id_num,))
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(cur.fetchall())])

            instruccion = f"""{BUSINESS_CONTEXT}
            Cliente: {nombre_actual}
            Historial:\n{historial}
            ETIQUETAS REQUERIDAS (Extrae los datos reales, no uses las palabras del ejemplo):
            - [ACCION:NOMBRE:NombreReal]
            - [ACCION:AGENDAR]
            - [ACCION:PAGO:ReferenciaReal:MontoEnBs:BancoEmisor]
            """
            respuesta_ia = generar_respuesta_ia(instruccion, texto_cliente, img_data)

            # Procesar Etiquetas y Limpiar respuesta del chat
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nombre_nuevo = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                if nombre_nuevo.lower() != "nombre":
                    nombre_actual = nombre_nuevo
                    cur.execute("INSERT INTO cliente (id, nombre, telefono) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre", (id_num, nombre_actual, numero))
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            if "[ACCION:PAGO:" in respuesta_ia:
                datos = respuesta_ia.split("[ACCION:PAGO:")[1].split("]")[0].split(":")
                if len(datos) >= 3:
                    feedback = ejecutar_logica_negocio(id_num, "PAGO", extra_data=datos, nombre_actual=nombre_actual)
                    respuesta_ia = respuesta_ia.split("[ACCION:PAGO:")[0].strip() + f"\n\n{feedback}"

            for tag in ["CANCELAR", "AGENDAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto_cliente, nombre_actual)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            # Registro y Envío
            enviar_meta(numero, respuesta_ia)
            cur.execute("INSERT INTO messages (user_id, user_input, bot_response) VALUES (%s, %s, %s)", (id_num, texto_cliente, respuesta_ia))
            conn.commit(); cur.close(); conn.close()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)