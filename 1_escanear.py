import requests
import base64
import time

# --- TUS DATOS ---
URL_EVOLUTION = "https://evolution-barberia.onrender.com" # Tu URL verde
API_KEY = "barberia123"
INSTANCE = "BarberiaBot"

headers = {"apikey": API_KEY, "Content-Type": "application/json"}

def escanear():
    print(f"--- 1. Creando instancia {INSTANCE} ---")
    
    # Payload CORRECTO para Evolution v2
    payload = {
        "instanceName": INSTANCE,
        "token": "token_random",
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS"  # <--- IMPORTANTE
    }
    
    try:
        # Intentamos crear
        requests.post(f"{URL_EVOLUTION}/instance/create", headers=headers, json=payload)
    except: pass # Si ya existe, seguimos

    print("--- 2. Obteniendo QR (Espera 3 seg) ---")
    time.sleep(3)
    
    resp = requests.get(f"{URL_EVOLUTION}/instance/connect/{INSTANCE}", headers=headers)
    
    if resp.status_code == 200:
        data = resp.json()
        b64 = data.get("base64") or data.get("qrcode", {}).get("base64")
        
        if b64:
            with open("qr_whatsapp.png", "wb") as f: 
                f.write(base64.b64decode(b64.split(",")[1]))
            print("✅ ¡ÉXITO! Abre la imagen 'qr_whatsapp.png' y ESCANEA.")
        else:
            print("⚠️ Conectado (o no envió imagen). Revisa el celular.")
    else:
        print(f"❌ Error: {resp.text}")

escanear()