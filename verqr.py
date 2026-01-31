import requests
import webbrowser
import os

# Configuración basada en tus logs
EVO_URL = "https://evolution-barberia.onrender.com" 
EVO_KEY = "barberia123" # Tu API Key confirmada
INSTANCIA = "barberia_v2" # Tu nueva instancia

def obtener_y_abrir_qr():
    url = f"{EVO_URL}/instance/connect/{INSTANCIA}"
    headers = {"apikey": EVO_KEY}
    
    print(f"🔍 Solicitando QR para la instancia: {INSTANCIA}...")
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            # El campo base64 contiene la imagen del QR
            qr_base64 = data.get("base64") 
            
            if qr_base64:
                # Creamos un archivo HTML temporal para mostrar el QR
                html_content = f"""
                <html>
                    <body style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; font-family:sans-serif;">
                        <h1>Escanea este QR con WhatsApp 💈🍕</h1>
                        <p>Instancia: <b>{INSTANCIA}</b></p>
                        <img src="{qr_base64}" alt="Código QR" style="border: 10px solid #f0f0f0; border-radius:10px;">
                        <p style="color:gray;">Este QR expirará pronto. ¡Date prisa!</p>
                    </body>
                </html>
                """
                
                with open("temp_qr.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
                
                # Abrimos el archivo en el navegador
                webbrowser.open('file://' + os.path.realpath("temp_qr.html"))
                print("✅ ¡QR abierto en tu navegador! Escanéalo ahora.")
            else:
                print("⚠️ La instancia ya está conectada o no devolvió un QR.")
        else:
            print(f"❌ Error {response.status_code}: {response.text}")
            
    except Exception as e:
        print(f"🔥 Fallo al conectar: {e}")

if __name__ == "__main__":
    obtener_y_abrir_qr()