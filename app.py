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

# Llamar a la actualización en el arranque (sirve para Gunicorn o Flask run)
actualizar_tasas()

def procesar_mensaje(telefono, nombre, mensaje):
    # 1. Identificar Cliente
    nombre_real = Database.get_cliente(telefono, nombre)
    
    # 2. Contexto
    pedido = Database.get_pedido_activo(telefono)
    carrito_txt = f"{pedido['resumen']} (${pedido['monto_total']})" if pedido else "Vacio"
    historial = Database.get_historial(telefono)
    
    # 3. Prompt (Ahora orientado a herramientas)
    prompt = f"""
    CONTEXTO: {Config.BUSINESS_CONTEXT}
    TASA: {Config.TASA_USD} Bs.
    CLIENTE: {nombre_real}
    CARRITO: {carrito_txt}
    HISTORIAL: {historial}
    
    INSTRUCCIONES CLAVE DE COMPORTAMIENTO:
    Eres el vendedor de la pizzería. TU OBJETIVO ES LLEVAR LA VENTA HASTA EL PAGO.
    
    SIGUE ESTE FLUJO ESTRICTAMENTE:
    1. Si el cliente pide un producto: Ejecuta la herramienta 'agendar_pedido' enviando el resumen TOTAL y monto TOTAL de todo lo que ha pedido. En tu respuesta de texto, confirma lo que agregaste y PREGUNTA SIEMPRE: "¿Deseas agregar algo más o confirmamos tu orden?".
    2. Si el cliente tiene dudas: Respóndele amablemente.
    3. Si el cliente dice "solo eso", "es todo", "confirmo", "estoy listo": Ejecuta la herramienta 'finalizar_pedido'. En tu respuesta de texto puedes decir "Generando tu factura...".
    
    REGLA DE ORO: NUNCA te quedes callado. SIEMPRE debes enviar un texto al usuario. Las herramientas se ejecutan en segundo plano, pero el texto es lo que el cliente lee.
    """

    # 4. Consultar IA (Ahora devuelve un diccionario)
    import json # Asegúrate de importar json si no está
    respuesta = CerebroIA.consultar(prompt, mensaje)
    
    texto_limpio = respuesta["texto"].strip()
    tool_calls = respuesta.get("tool_calls")
    
    # 5. Ejecutar Acciones (Manejador de Herramientas / Tools Handler)
    msg_final = ""
    
    if tool_calls:
        print(f"⚙️ La IA decidió usar {len(tool_calls)} herramienta(s).")
        for tool in tool_calls:
            nombre_funcion = tool["function"]["name"]
            argumentos = json.loads(tool["function"]["arguments"])
            
            print(f"🔧 Ejecutando -> {nombre_funcion}({argumentos})")
            
            if nombre_funcion == "actualizar_nombre":
                Database.update_cliente(telefono, argumentos.get("nombre", nombre_real))

            elif nombre_funcion == "agendar_pedido":
                # Fíjate cómo Python recibe variables nativas y limpias (String, Float)
                item = argumentos.get("item")
                precio = float(argumentos.get("precio", 0))
                Database.update_pedido(telefono, item, precio)
                # Respaldo por si la IA a pesar de la orden se queda callada
                if not texto_limpio:
                    texto_limpio = f"✅ ¡Anotado! He agregado {item} a tu pedido. ¿Deseas algo más o confirmamos la orden?"

            elif nombre_funcion == "finalizar_pedido":
                Database.cerrar_pedido(telefono)
                ped = Database.get_pedido_activo(telefono)
                if ped and ped['monto_total'] > 0:
                    monto_bs = float(ped['monto_total']) * Config.TASA_USD
                    msg_final = f"✅ *Pedido Confirmado*\n📝 {ped['resumen']}\n💵 Total: ${ped['monto_total']} ({monto_bs:,.2f} Bs)\n📍 Retiro en Tienda\n📲 Envía pago móvil."
                else:
                    msg_final = "⚠️ Error: Carrito vacío. Pide de nuevo."

    # 6. Enviar Respuesta
    # Ya no hace falta quitar etiquetas con re.sub()
    
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
    app.run(host='0.0.0.0', port=Config.PORT)