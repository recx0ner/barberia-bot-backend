import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_NUMBER = os.environ.get("ADMIN_NUMBER") # Tu número personal
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de Pizzas El Guaro.")

# --- FUNCIONES DE ENVÍO ---
def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

# --- LÓGICA DE NEGOCIO ---
def gestionar_pedido(user_id, accion, texto=""):
    try:
        if accion == "AGENDAR":
            supabase.table('citas').insert({"user_id": user_id, "detalle": texto, "estado": "pendiente"}).execute()
            return "✅ Pedido anotado."
        
        elif accion == "CANCELAR":
            # Cambia el estado de la última cita pendiente a 'cancelado'
            supabase.table('citas').update({"estado": "cancelado"}).eq("user_id", user_id).eq("estado", "pendiente").execute()
            return "🗑️ Tu pedido ha sido cancelado con éxito."
        
        elif accion == "PAGO":
            # Extraer solo números de la referencia (mínimo 6-8 dígitos para seguridad)
            ref = ''.join(filter(str.isdigit, texto))
            if len(ref) < 4: return "⚠️ Referencia de pago muy corta o inválida."
            
            supabase.table('pagos').insert({"user_id": user_id, "referencia": ref}).execute()
            # NOTIFICAR AL ADMIN [Nueva función solicitada]
            enviar_meta(ADMIN_PHONE, f"🔔 *NUEVO PAGO*\nCliente: {user_id}\nRef: {ref}\n\nResponde con 'CONFIRMAR {ref}' para validar.")
            return "⏳ Referencia recibida. El administrador verificará el pago en breve."
    except Exception as e:
        print(f"🔥 Error en DB: {e}")
        return "⚠️ Hubo un detalle técnico, intenta de nuevo."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero = msg['from']
            texto = msg.get('text', {}).get('body', "").strip()
            ahora_ve = datetime.now(venezuela_tz).strftime('%Y-%m-%d %H:%M:%S')

            # 1. Lógica especial para el Administrador
            if numero == ADMIN_PHONE and texto.upper().startswith("CONFIRMAR"):
                ref_confirmar = texto.split()[-1]
                # Buscar al cliente dueño de esa referencia
                pago_data = supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_confirmar).execute()
                if pago_data.data:
                    cliente_id = pago_data.data[0]['user_id']
                    enviar_meta(cliente_id, "✅ *¡Pago verificado!* Estamos preparando tu pedido ahora mismo. 🍕")
                    return jsonify({"status": "confirmed"}), 200

            # 2. Lógica para Clientes (IA)
            instruccion = f"""{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\n
            REGLAS: 
            - Si cancela el pedido: [ACCION:CANCELAR]
            - Si confirma pedido nuevo: [ACCION:AGENDAR]
            - Si envía referencia de pago: [ACCION:PAGO]"""

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(model='gemini-2.5-flash', 
                                                     config={'system_instruction': instruccion}, contents=texto)
            respuesta_ia = response.text.strip()

            # 3. Procesar Tags de Acción
            for tag in ["AGENDAR", "CANCELAR", "PAGO"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = gestionar_pedido(numero, tag, texto)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)