import time, threading, re, base64, requests
import os # Asegúrate de importar os
from flask import Flask, request
from config import Config
from database import Database
from whatsapp import WhatsApp
from ia import CerebroIA

app = Flask(__name__)

# Iniciar DB
Database.initialize()
user_buffers = {}

# --- FUNCIÓN DE TASAS ---
def actualizar_tasas():
    print("🔄 Consultando Tasa BCV/Monitor...")
    try:
        # Intentamos obtener la tasa
        res = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5).json()
        tasa = float(res['rates']['VES'])
        Config.TASA_USD = tasa
        print(f"✅ Tasa Actualizada: {tasa} Bs/USD")
    except Exception as e:
        print(f"⚠️ Error obteniendo tasa (Usando {Config.TASA_USD} por defecto): {e}")

def procesar_mensaje(telefono, nombre, mensaje):
    # 1. Identificar Cliente
    nombre_real = Database.get_cliente(telefono, nombre)
    
    # 2. Contexto
    pedido = Database.get_pedido_activo(telefono)
    carrito_txt = f"{pedido['resumen']} (${pedido['monto_total']})" if pedido else "Vacio"
    historial = Database.get_historial(telefono)
    
    # 3. Prompt (Ahora limpio)
    prompt = f"""
    CONTEXTO: {Config.BUSINESS_CONTEXT}
    TASA: {Config.TASA_USD} Bs.
    CLIENTE: {nombre_real}
    CARRITO: {carrito_txt}
    HISTORIAL: {historial}
    
    INSTRUCCIONES:
    1. Si pide producto -> Usa [AGENDAR: Item | Precio]. Ej: [AGENDAR: Pizza | 10]
    2. Si dice "SOLO ESO/LISTO" -> Usa [FINALIZAR].
    3. Nuevo cliente -> [NOMBRE: ...]
    """

    # 4. Consultar IA
    respuesta = CerebroIA.consultar(prompt, mensaje)
    
    # 5. Ejecutar Acciones
    msg_final = ""
    
    if "[NOMBRE:" in respuesta:
        n = respuesta.split("[NOMBRE:")[1].split("]")[0].strip()
        Database.update_cliente(telefono, n)

    if "[AGENDAR:" in respuesta:
        try:
            d = respuesta.split("[AGENDAR:")[1].split("]")[0].split("|")
            Database.update_pedido(telefono, d[0].strip(), float(d[1].strip()))
        except: pass

    if "[CANCELAR]" in respuesta:
        # Lógica de cancelar (puedes implementarla en DB si quieres)
        pass

    if "[FINALIZAR]" in respuesta:
        Database.cerrar_pedido(telefono)
        ped = Database.get_pedido_activo(telefono) # Traer datos frescos
        if ped and ped['monto_total'] > 0:
            monto_bs = float(ped['monto_total']) * Config.TASA_USD
            msg_final = f"✅ *Pedido Confirmado*\n📝 {ped['resumen']}\n💵 Total: ${ped['monto_total']} ({monto_bs:,.2f} Bs)\n📍 Retiro en Tienda\n📲 Envía pago móvil."
        else:
            msg_final = "⚠️ Error: Carrito vacío. Pide de nuevo."

    # 6. Enviar Respuesta
    texto_limpio = re.sub(r'\[.*?\]', '', respuesta).strip()
    
    if msg_final:
        WhatsApp.enviar_mensaje(telefono, msg_final)
        Database.save_mensaje(telefono, 'assistant', msg_final)
    elif texto_limpio:
        WhatsApp.enviar_mensaje(telefono, texto_limpio)
        Database.save_mensaje(telefono, 'user', mensaje)
        Database.save_mensaje(telefono, 'assistant', texto_limpio)

@app.route("/whatsapp", methods=["GET", "POST"])
def webhook():
    if request.method == 'GET': return request.args.get("hub.challenge"), 200
    try:
        data = request.json['entry'][0]['changes'][0]['value']
        if 'messages' not in data: return "OK", 200
        
        msg = data['messages'][0]
        tel = msg['from']
        nombre = data['contacts'][0]['profile']['name']
        
        # Lógica de Buffer (Evitar mensajes partidos)
        if msg['type'] == 'text':
            txt = msg['text']['body']
            if tel in user_buffers:
                user_buffers[tel]['timer'].cancel()
                user_buffers[tel]['text'] += " " + txt
            else:
                user_buffers[tel] = {'text': txt}
            
            # Esperar 2 seg antes de responder
            t = threading.Timer(2.0, procesar_mensaje, args=[tel, nombre, user_buffers[tel]['text']])
            user_buffers[tel]['timer'] = t
            t.start()
            # Limpieza del buffer
            threading.Timer(2.5, lambda: user_buffers.pop(tel, None)).start()
            
        elif msg['type'] == 'image':
            # Lógica de Pago (Simplificada)
            # Aquí llamarías a CerebroIA.analizar_pago y Database.registrar_pago
            pass

    except Exception as e: print(e)
    return "OK", 200

if __name__ == "__main__":
    actualizar_tasas()
    app.run(host='0.0.0.0', port=Config.PORT)