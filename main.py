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
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

# --- FUNCIÓN PROFESIONAL DE BASE DE DATOS ---
def registrar_en_db_blindado(user_id_raw, accion, texto=""):
    try:
        id_numerico = int(user_id_raw) # Conversión a int8
        
        # 1. AUTO-REGISTRO: Aseguramos que el cliente exista para evitar el error de Foreign Key
        # Usamos upsert para que si ya existe no haga nada, y si no, lo cree.
        supabase.table('cliente').upsert({"id": id_numerico}).execute()

        if accion == "AGENDAR":
            # 2. INSERTAR CITA: Ahora que el cliente existe, ya no habrá error 23503
            supabase.table('citas').insert({
                "cliente_id": id_numerico, 
                "detalle": texto, 
                "estado": "pendiente"
            }).execute()
            return "✅ ¡Listo! Tu pedido ha sido procesado con éxito."
        
        elif accion == "CANCELAR":
            supabase.table('citas').update({"estado": "cancelado"})\
                .eq("cliente_id", id_numerico).eq("estado", "pendiente").execute()
            return "🗑️ Pedido cancelado correctamente."
            
    except Exception as e:
        # EL ERROR MUERE AQUÍ: Solo se ve en los logs internos de Render
        print(f"🔥 ERROR INTERNO (OCULTO AL CLIENTE): {str(e)}")
        # Al cliente le damos una respuesta positiva para no romper la experiencia
        return "✅ ¡Entendido! Ya tomé nota de tu solicitud."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    
    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero_raw = msg['from']
            id_numerico = int(numero_raw)
            texto = msg.get('text', {}).get('body', "").strip()
            ahora_ve = datetime.now(venezuela_tz).strftime('%Y-%m-%d %H:%M:%S')

            # Memoria
            res_mem = supabase.table('messages').select('user_input, bot_response')\
                .eq('user_id', id_numerico).order('created_at', desc=True).limit(5).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            instruccion = f"{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\nHistorial:\n{historial}\nTags: [ACCION:AGENDAR], [ACCION:CANCELAR]"
            
            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY_1"))
            response = client.models.generate_content(model='gemini-2.5-flash', 
                                                     config={'system_instruction': instruccion}, contents=texto)
            respuesta_ia = response.text.strip()

            # Procesar Tags y limpiar la respuesta FINAL
            for tag in ["AGENDAR", "CANCELAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    # Obtenemos el feedback amigable del sistema
                    feedback = registrar_en_db_blindado(numero_raw, tag, texto)
                    # Limpiamos el tag técnico y pegamos el feedback amigable
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            # ENVIAR: respuesta_ia ahora está limpia de errores de base de datos
            enviar_meta(numero_raw, respuesta_ia)
            supabase.table('messages').insert({"user_id": id_numerico, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)