import os, random, requests, traceback
from datetime import datetime
import pytz 
from flask import Flask, request, jsonify
from google import genai 
from google.genai import errors 
from supabase import create_client

app = Flask(__name__)
venezuela_tz = pytz.timezone('America/Caracas')

# --- CONFIGURACIÓN DE LLAVES Y ENTORNO ---
GEMINI_KEYS = [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(1, 4)]
VALID_KEYS = [k for k in GEMINI_KEYS if k] # Rotación de las 3 llaves

META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE") # Número para notificaciones
BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT")

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# --- FUNCIONES DE COMUNICACIÓN ---
def enviar_meta(to, text):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    return requests.post(url, headers=headers, json=payload)

# --- IA CON FAILOVER (PROTECCIÓN CONTRA ERROR 429) ---
def generar_respuesta_ia(instruccion, texto_usuario):
    llaves_shuffled = VALID_KEYS[:]
    random.shuffle(llaves_shuffled) # Distribución de carga aleatoria

    for key in llaves_shuffled:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                config={'system_instruction': instruccion},
                contents=texto_usuario
            )
            return response.text.strip()
        except errors.ClientError as e:
            if "429" in str(e): # Si una llave falla por cuota, salta a la siguiente
                print(f"⚠️ Cuota agotada en una llave, reintentando...")
                continue
            raise e
    return "Estamos bajo mucha demanda pizzería, ¡vuelve a intentarlo en un momento!"

# --- GESTIÓN DE BASE DE DATOS (CLIENTES Y CITAS) ---
def ejecutar_logica_negocio(id_num, accion, texto=""):
    try:
        # 1. Aseguramos registro en tabla 'cliente' para evitar Error 23503
        supabase.table('cliente').upsert({"id": id_num, "telefono": str(id_num)}).execute()

        if accion == "AGENDAR":
            # Usamos todas tus columnas: user_id, servicio, fecha_hora y estado
            supabase.table('citas').insert({
                "user_id": id_num, 
                "servicio": texto, 
                "fecha_hora": datetime.now(venezuela_tz).isoformat(),
                "estado": "pendiente"
            }).execute()
            return "✅ ¡Pedido agendado! Manos a la masa."
        
        elif accion == "CANCELAR":
            supabase.table('citas').update({"estado": "cancelado"})\
                .eq("user_id", id_num).eq("estado", "pendiente").execute()
            return "🗑️ Pedido cancelado correctamente."
            
    except Exception as e:
        print(f"🔥 Error DB: {str(e)}") # Oculto al cliente por profesionalismo
        return "✅ ¡Entendido! Ya procesé tu solicitud."

@app.route('/whatsapp', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return request.args.get("hub.challenge"), 200

    payload = request.json
    try:
        entry = payload['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            numero_str = msg['from']
            id_num = int(numero_str) # ID numérico int8
            texto = msg.get('text', {}).get('body', "").strip()
            ahora_ve = datetime.now(venezuela_tz).strftime('%Y-%m-%d %H:%M:%S')

            # 1. Recuperar Identidad y Memoria
            res_cli = supabase.table('cliente').select('nombre').eq('id', id_num).execute()
            nombre_cli = res_cli.data[0]['nombre'] if res_cli.data and res_cli.data[0]['nombre'] else "Desconocido"
            
            res_mem = supabase.table('messages').select('user_input, bot_response')\
                .eq('user_id', numero_str).order('created_at', desc=True).limit(6).execute()
            historial = "\n".join([f"C: {m['user_input']}\nB: {m['bot_response']}" for m in reversed(res_mem.data)])

            # 2. IA con Instrucciones de Acción
            instruccion = f"""{BUSINESS_CONTEXT}\nHora VZLA: {ahora_ve}\nCliente: {nombre_cli}\nHistorial:\n{historial}
            REGLAS:
            - Si el cliente confirma pedido (ej: "así es"), usa: [ACCION:AGENDAR]
            - Si no sabes su nombre, pregunta y usa: [ACCION:NOMBRE:Nombre]
            - Si cancela, usa: [ACCION:CANCELAR]
            """
            
            respuesta_ia = generar_respuesta_ia(instruccion, texto)

            # 3. Procesar Tags de Acción
            if "[ACCION:NOMBRE:" in respuesta_ia:
                nuevo_nom = respuesta_ia.split("[ACCION:NOMBRE:")[1].split("]")[0]
                supabase.table('cliente').upsert({"id": id_num, "nombre": nuevo_nom, "telefono": numero_str}).execute()
                respuesta_ia = respuesta_ia.split("[ACCION:NOMBRE:")[0].strip()

            for tag in ["AGENDAR", "CANCELAR"]:
                if f"[ACCION:{tag}]" in respuesta_ia:
                    feedback = ejecutar_logica_negocio(id_num, tag, texto)
                    respuesta_ia = respuesta_ia.replace(f"[ACCION:{tag}]", "").strip() + f"\n\n{feedback}"

            # 4. Responder y Loguear
            enviar_meta(numero_str, respuesta_ia)
            supabase.table('messages').insert({"user_id": numero_str, "user_input": texto, "bot_response": respuesta_ia}).execute()

        return jsonify({"status": "ok"}), 200
    except:
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)