import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import google.generativeai as genai
from supabase import create_client
from datetime import datetime, timedelta
import locale

# --- 1. CONFIGURACIÓN ---
load_dotenv()

app = FastAPI(title="API Barbería El Estilo")

# --- 🛡️ INTERRUPTOR DE EMERGENCIA ---
# Pon esto en True si Supabase está caído para seguir probando
MODO_OFFLINE = False

URL_DB = os.getenv("SUPABASE_URL")
KEY_DB = os.getenv("SUPABASE_KEY")

# Inicialización segura de Supabase
supabase = None
if not MODO_OFFLINE:
    try:
        supabase = create_client(URL_DB, KEY_DB)
        print("🟢 Conexión a Supabase: ACTIVA")
    except Exception as e:
        print(f"🔴 Error conectando a Supabase: {e}")
        print("⚠️ Activando MODO OFFLINE automáticamente.")
        MODO_OFFLINE = True
else:
    print("🟠 MODO OFFLINE: ACTIVO (Simulando base de datos)")

API_KEYS = [
    os.getenv("GEMINI_KEY_1"),
    os.getenv("GEMINI_KEY_2"),
    os.getenv("GEMINI_KEY_3")
]
ADMIN_PHONE = os.getenv("ADMIN_PHONE")
chat_sessions = {}

class MensajeEntrada(BaseModel):
    mensaje: str
    telefono: str

# --- 2. HERRAMIENTAS BLINDADAS ---

def crear_cliente(nombre: str, telefono: str):
    if MODO_OFFLINE: return f"✅ [OFFLINE]: Cliente {nombre} simulado con éxito."
    
    tel_limpio = "".join(filter(str.isdigit, str(telefono)))
    nombre_formato = nombre.strip().title() 
    try:
        check = supabase.table("cliente").select("id").eq("telefono", tel_limpio).execute()
        if check.data: return f"El cliente ya existe."
        supabase.table("cliente").insert({"nombre": nombre_formato, "telefono": tel_limpio}).execute()
        return f"Cliente registrado exitosamente."
    except Exception as e: return f"Error DB: {str(e)}"

def registrar_cita(servicio: str, fecha: str, hora: str, telefono: str):
    if MODO_OFFLINE: return f"✅ [OFFLINE]: Cita para {servicio} el {fecha} a las {hora} agendada (Simulación)."

    tel_limpio = "".join(filter(str.isdigit, str(telefono)))
    try:
        h_str = hora.strip()
        if ":" not in h_str: h_str = f"{h_str}:00"
        dt_cita = datetime.fromisoformat(f"{fecha}T{h_str}:00")
        if dt_cita.weekday() == 6: return "Cerrado los domingos."
        if not (9 <= dt_cita.hour < 20): return "Horario inválido (9-20h)."
        if dt_cita.minute not in [0, 30]: return "Solo turnos en punto (:00) o y media (:30)."
    except ValueError: return "Formato fecha incorrecto."

    try:
        res_cli = supabase.table("cliente").select("id").eq("telefono", tel_limpio).execute()
        if not res_cli.data: return "Regístrate primero."
        id_cli = res_cli.data[0]['id']
        ocupada = supabase.table("citas").select("id").eq("fecha_hora", f"{fecha}T{h_str}:00Z").neq("estado", "cancelado").execute()
        if ocupada.data: return f"Hora {h_str} ocupada."
        supabase.table("citas").insert({"cliente_id": id_cli, "servicio": servicio, "fecha_hora": f"{fecha}T{h_str}:00Z", "estado": "confirmado"}).execute()
        return f"Confirmado: {servicio} el {fecha} a las {h_str}."
    except Exception as e: return f"Error DB: {str(e)}"

def cancelar_cita(fecha: str, hora: str, telefono: str):
    if MODO_OFFLINE: return f"✅ [OFFLINE]: Cita del {fecha} a las {hora} cancelada (Simulación)."
    
    tel_limpio = "".join(filter(str.isdigit, str(telefono)))
    if not hora: return "Necesito la hora exacta."
    try:
        h_str = hora.strip()
        if ":" not in h_str: h_str = f"{h_str}:00"
        res_cli = supabase.table("cliente").select("id").eq("telefono", tel_limpio).execute()
        if not res_cli.data: return "Usuario no encontrado."
        id_cli = res_cli.data[0]['id']
        citas = supabase.table("citas").select("id").eq("cliente_id", id_cli).eq("fecha_hora", f"{fecha}T{h_str}:00Z").eq("estado", "confirmado").execute()
        if not citas.data: return f"No encontré cita el {fecha} a las {h_str}."
        supabase.table("citas").update({"estado": "cancelado"}).eq("id", citas.data[0]['id']).execute()
        return "Cita cancelada exitosamente."
    except Exception as e: return f"Error DB: {str(e)}"

def consultar_citas(telefono: str):
    if MODO_OFFLINE: return "📅 [OFFLINE]: Tienes una cita simulada mañana a las 10:00."
    
    tel_limpio = "".join(filter(str.isdigit, str(telefono)))
    try:
        res_cli = supabase.table("cliente").select("id").eq("telefono", tel_limpio).execute()
        if not res_cli.data: return "No tienes cuenta."
        id_cli = res_cli.data[0]['id']
        ahora = datetime.now().isoformat()
        res_citas = supabase.table("citas").select("servicio, fecha_hora").eq("cliente_id", id_cli).eq("estado", "confirmado").gte("fecha_hora", ahora).order("fecha_hora").execute()
        if not res_citas.data: return "Sin citas futuras."
        texto = "📅 Tus próximas citas:\n"
        for cita in res_citas.data:
            dt = datetime.fromisoformat(cita['fecha_hora'].replace("Z", ""))
            texto += f"- {cita['servicio']}: {dt.strftime('%d/%m %H:%M')}\n"
        return texto
    except Exception as e: return f"Error DB: {str(e)}"

def consultar_disponibilidad(fecha: str):
    if MODO_OFFLINE: return f"✅ [OFFLINE]: Para el {fecha} tengo libre todo el día (Simulación)."
    
    try:
        dt_check = datetime.strptime(fecha, "%Y-%m-%d")
        if dt_check.weekday() == 6: return "El domingo estamos cerrados."
        slots_totales = []
        hora_actual = datetime.strptime(f"{fecha} 09:00", "%Y-%m-%d %H:%M")
        fin_jornada = datetime.strptime(f"{fecha} 20:00", "%Y-%m-%d %H:%M")
        while hora_actual < fin_jornada:
            slots_totales.append(hora_actual.strftime("%H:%M"))
            hora_actual += timedelta(minutes=30)
        inicio_dia = f"{fecha}T00:00:00Z"
        fin_dia = f"{fecha}T23:59:59Z"
        ocupados_db = supabase.table("citas").select("fecha_hora").gte("fecha_hora", inicio_dia).lte("fecha_hora", fin_dia).neq("estado", "cancelado").execute()
        horas_ocupadas = [datetime.fromisoformat(r['fecha_hora'].replace("Z", "")).strftime("%H:%M") for r in ocupados_db.data]
        disponibles = [s for s in slots_totales if s not in horas_ocupadas]
        return f"Libres {fecha}: " + ", ".join(disponibles)
    except Exception as e: return f"Error DB: {str(e)}"

def ver_agenda_admin(fecha: str, telefono_solicitante: str):
    if MODO_OFFLINE: return f"📋 [OFFLINE]: Agenda simulada del {fecha}: Nadie."
    
    tel_limpio = "".join(filter(str.isdigit, str(telefono_solicitante)))
    if tel_limpio != ADMIN_PHONE: return "⛔ ACCESO DENEGADO."
    try:
        inicio = f"{fecha}T00:00:00Z"
        fin = f"{fecha}T23:59:59Z"
        res = supabase.table("citas").select("fecha_hora, servicio, estado, cliente(nombre, telefono)").gte("fecha_hora", inicio).lte("fecha_hora", fin).neq("estado", "cancelado").order("fecha_hora").execute()
        if not res.data: return f"Agenda libre para el {fecha}."
        reporte = f"📋 REPORTE {fecha}:\n"
        for item in res.data:
            dt = datetime.fromisoformat(item['fecha_hora'].replace("Z", ""))
            reporte += f"🕒 {dt.strftime('%H:%M')} | {item['cliente']['nombre']} | {item['servicio']}\n"
        return reporte
    except Exception as e: return f"Error DB: {str(e)}"

def finalizar_sesion():
    return "##CERRAR_SESION##"

# --- 3. CEREBRO Y CHAT ---

def obtener_instrucciones():
    ahora = datetime.now()
    estado_db = "OFFLINE (Simulación)" if MODO_OFFLINE else "ONLINE"
    return f"""
    Eres el recepcionista de 'Barbería El Estilo'. HOY: {ahora.strftime("%Y-%m-%d %H:%M")}.
    ESTADO SISTEMA: {estado_db}.
    INSTRUCCIONES:
    - Si 'registrar_cita' o 'cancelar_cita' son exitosas -> Despídete y llama a 'finalizar_sesion'.
    - Si estás en MODO OFFLINE, avisa al usuario que es una simulación.
    HERRAMIENTAS: finalizar_sesion, registrar_cita, cancelar_cita, consultar_citas, consultar_disponibilidad, ver_agenda_admin.
    """

# (Esta función selecciona la API Key disponible)
def obtener_chat_sesion(telefono: str):
    if telefono in chat_sessions: return chat_sessions[telefono]
    
    # Rotación de keys
    for key in API_KEYS:
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel(
                model_name="gemini-2.5-flash",
                tools=[crear_cliente, registrar_cita, cancelar_cita, consultar_citas, consultar_disponibilidad, ver_agenda_admin, finalizar_sesion],
                system_instruction=obtener_instrucciones()
            )
            chat = model.start_chat(enable_automatic_function_calling=True)
            chat_sessions[telefono] = chat 
            return chat
        except Exception: continue
    return None

# --- 4. ENDPOINT WEB ---

@app.post("/chat")
async def recibir_mensaje(datos: MensajeEntrada):
    """
    Endpoint para recibir mensajes de WhatsApp (o pruebas locales).
    """
    # 1. Recuperamos o creamos la sesión de chat
    chat = obtener_chat_sesion(datos.telefono)
    if not chat:
        raise HTTPException(status_code=503, detail="Servidor saturado (APIs agotadas).")

    try:
        # 2. Enviamos mensaje a Gemini
        resp = chat.send_message(f"{datos.mensaje} (Soy Tel: {datos.telefono})")
        
        # 3. Procesamos respuesta de forma segura
        texto_respuesta = ""
        try: texto_respuesta = resp.text
        except: texto_respuesta = "✅ Acción simulada/realizada."

        # 4. Manejo de cierre de sesión
        estado_sesion = "activo"
        if "##CERRAR_SESION##" in texto_respuesta:
            if datos.telefono in chat_sessions:
                del chat_sessions[datos.telefono] # Borramos memoria
            texto_respuesta = texto_respuesta.replace("##CERRAR_SESION##", "")
            estado_sesion = "finalizada"

        return {"respuesta": texto_respuesta, "estado": estado_sesion}

    except Exception as e:
        # Si falla (ej: error 500 de Google), limpiamos sesión para reintentar limpio la próxima
        if datos.telefono in chat_sessions:
            del chat_sessions[datos.telefono]
        return {"respuesta": "Tuve un error técnico momentáneo.", "error": str(e)}

@app.get("/")
def home():
    estado = "🟠 Modo Offline" if MODO_OFFLINE else "🟢 Online"
    return {"status": f"Barbería Activa - {estado}"}