import requests

# Tus datos (Asegúrate que la URL NO termine en /)
EVO_URL = "https://evolution-barberia.onrender.com" 
EVO_KEY = "barberia123" # Pon tu API Key real aquí
NUEVA_INSTANCIA = "barberia_v2"

def crear_instancia():
    url = f"{EVO_URL}/instance/create"
    headers = {
        "apikey": EVO_KEY, 
        "Content-Type": "application/json"
    }
    
    # Hemos añadido 'integration' para corregir el error 400
    payload = {
        "instanceName": NUEVA_INSTANCIA,
        "token": "123456",
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS" 
    }
    
    print(f"🛠️ Intentando crear instancia: {NUEVA_INSTANCIA}...")
    res = requests.post(url, headers=headers, json=payload)
    
    if res.status_code == 201 or res.status_code == 200:
        print("✅ ¡Instancia creada con éxito!")
        print("👉 Ahora usa el comando de 'connect' para ver el QR.")
    else:
        print(f"❌ Falló de nuevo: {res.status_code} - {res.text}")

if __name__ == "__main__":
    crear_instancia()