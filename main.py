import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN GLOBAL ---
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # <--- ¡ESTA ES LA VARIABLE QUE FALTABA!
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Eres el asistente de Pizzas El Guaro.")

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

# --- SISTEMA DE GESTIÓN (Citas, Cancelaciones y Pagos) ---
def procesar_logica_negocio(user_id, accion, texto=""):
    try:
        if accion == "AGENDAR":
            supabase.table('citas').insert({"user_id": user_id, "detalle": texto, "estado": "pendiente"}).execute()
            return "✅ Pedido agendado. ¡Estamos listos!"
        
        elif accion == "CANCELAR":
            supabase.table('citas').update({"estado": "cancelado"}).eq("user_id", user_id).eq("estado", "pendiente").execute()
            return "🗑️ Pedido cancelado correctamente."
        
        elif accion == "PAGO":
            ref = ''.join(filter(str.isdigit, texto)) # Extraer solo números de la referencia
            if len(ref) < 6: return "⚠️ Referencia inválida. Por favor, envía el número completo."
            
            # Guardar referencia para que el admin la vea
            supabase.table('pagos').insert({"user_id": user_id, "referencia": ref, "verificado": False}).execute()
            
            # NOTIFICAR AL ADMIN (AQUÍ SE USA LA VARIABLE ADMIN_PHONE)
            if ADMIN_PHONE:
                enviar_meta(ADMIN_PHONE, f"🔔 *NUEVO PAGO*\nCliente: {user_id}\nRef: {ref}\n\nResponde: *CONFIRMAR {ref}*")
            return "⏳ Referencia recibida. Espera un momento mientras el administrador la verifica."
    except Exception as e:
        return f"⚠️ Error procesando la solicitud: {str(e)}"

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

            # --- LÓGICA DE ADMINISTRADOR: Confirmación de Pago ---
            if ADMIN_PHONE and numero == ADMIN_PHONE and texto.upper().startswith("CONFIRMAR"):
                ref_a_validar = texto.split()[-1]
                res_pago = supabase.table('pagos').select('user_id').eq('referencia', ref_a_validar).execute()
                if res_pago.data:
                    id_cliente = res_pago.data[0]['user_id']
                    supabase.table('pagos').update({"verificado": True}).eq("referencia", ref_a_validar).execute()
                    enviar_meta(id_cliente, "✅ *¡Pago verificado!* Tu pedido ya está en el horno. 🍕🔥")
                    return jsonify({"status": "pago_confirmado"}), 200

            # --- LÓGICA DE CLIENTE: IA con Memoria ---
            instruccion = f"{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\nTags: [ACCION:AGENDAR], [ACCION:CANCELAR], [ACCION:PAGO]"
            
            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(model='gemini-2.5-flash', 
                                                     config={'system_instruction': instruccion}, contents=texto)
            respuesta_ia = response.text.strip()

            # Procesar Tags y limpiar respuesta
            for tag in ["AGENDAR", "CANCELAR", "PAGO"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = procesar_logica_negocio(numero, tag, texto)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)