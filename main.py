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
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") 
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

# --- FUNCIÓN DE MEMORIA OPTIMIZADA ---
def obtener_memoria_completa(user_id):
    try:
        # Traemos los últimos 10 mensajes para un contexto sólido
        res = supabase.table('messages').select('user_input, bot_response')\
            .eq('user_id', user_id).order('created_at', desc=True).limit(10).execute()
        
        if not res.data: return "No hay charla previa."
        
        historial = "\n".join([f"Cliente: {m['user_input']}\nBot: {m['bot_response']}" for m in reversed(res.data)])
        return historial
    except:
        return "Error recuperando memoria."

def registrar_en_db(user_id, accion, texto=""):
    try:
        if accion == "AGENDAR":
            # Inserta en la tabla de citas
            supabase.table('citas').insert({"user_id": user_id, "detalle": texto, "estado": "pendiente"}).execute()
            return "✅ ¡Pedido anotado en cocina!"
        elif accion == "CANCELAR":
            supabase.table('citas').update({"estado": "cancelado"}).eq("user_id", user_id).eq("estado", "pendiente").execute()
            return "🗑️ Pedido cancelado."
        return ""
    except Exception as e:
        return f"⚠️ Error DB: {str(e)}"

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

            # 1. RECUPERAR MEMORIA
            historial_charla = obtener_memoria_completa(numero)

            # 2. PROMPT REFORZADO PARA EVITAR REPETICIONES
            instruccion = f"""
            {BUSINESS_CONTEXT}
            Hora VZLA: {ahora_ve}
            
            HISTORIAL CRÍTICO (Léelo antes de responder):
            {historial_charla}
            
            INSTRUCCIONES DE MEMORIA:
            - Si el cliente ya eligió un producto en el historial, NO vuelvas a mostrar el menú.
            - Si el cliente confirma (ej: "de una vez"), usa el tag [ACCION:AGENDAR].
            
            TAGS DISPONIBLES: [ACCION:AGENDAR], [ACCION:CANCELAR], [ACCION:PAGO]
            """

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(model='gemini-2.5-flash', 
                                                     config={'system_instruction': instruccion}, contents=texto)
            respuesta_ia = response.text.strip()

            # 3. PROCESAR ACCIONES (Agendamiento)
            for tag in ["AGENDAR", "CANCELAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = registrar_en_db(numero, tag, texto)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            enviar_meta(numero, respuesta_ia)
            # Guardar el nuevo intercambio para la próxima memoria
            supabase.table('messages').insert({"user_id": numero, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)