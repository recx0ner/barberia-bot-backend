import requests
import base64
import time

# --- TUS DATOS ---
URL_EVOLUTION = "https://evolution-barberia.onrender.com"
API_KEY = "barberia123"
INSTANCE = "BarberiaBot"

headers = {
    "apikey": API_KEY,
    "Content-Type": "application/json"
}

def reiniciar_y_escanear():
    print(f"--- 🔄 REINICIANDO {INSTANCE} ---")

    # 1. BORRAR LA VIEJA (Limpieza)
    print("1. Borrando instancia anterior...")
    try:
        requests.delete(f"{URL_EVOLUTION}/instance/delete/{INSTANCE}", headers=headers)
        print("   🗑️ Instancia vieja eliminada (si existía).")
    except: pass
    
    time.sleep(3) # Damos tiempo a Evolution para que respire

    # 2. CREAR LA NUEVA
    print("2. Creando instancia nueva...")
    payload = {
        "instanceName": INSTANCE,
        "token": "token_random",
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS" 
    }
    
    try:
        resp = requests.post(f"{URL_EVOLUTION}/instance/create", headers=headers, json=payload)
        if resp.status_code == 201 or resp.status_code == 200:
            print("   ✅ Instancia creada correctamente.")
        else:
            print(f"   ❌ Error creando: {resp.text}")
            return
    except Exception as e:
        print(f"Error conexión: {e}")
        return

    # 3. OBTENER QR
    print("3. Esperando QR (3 seg)...")
    time.sleep(3)
    
    resp_qr = requests.get(f"{URL_EVOLUTION}/instance/connect/{INSTANCE}", headers=headers)
    
    if resp_qr.status_code == 200:
        data = resp_qr.json()
        b64 = data.get("base64") or data.get("qrcode", {}).get("base64")
        
        if b64:
            with open("qr_whatsapp.png", "wb") as f: 
                f.write(base64.b64decode(b64.split(",")[1]))
            print("\n✨ ¡LISTO! ✨")
            print("✅ Abre 'qr_whatsapp.png' y escanea con el celular.")
        else:
            print("⚠️ Conectado, pero no dio imagen (Revisa si ya sale conectado en el cel).")
    else:
        print(f"❌ Error pidiendo QR: {resp_qr.text}")

reiniciar_y_escanear()