import requests

# --- TUS DATOS ---
URL_EVO = "https://evolution-barberia.onrender.com"
URL_BOT = "https://barberia-bot-8sju.onrender.com" # Tu Bot Python
API_KEY = "barberia123"
INSTANCE = "BarberiaBot"

webhook_url = f"{URL_BOT}/whatsapp"

print(f"Configurando webhook hacia: {webhook_url}")

# Payload CORRECTO (Con "webhook" anidado)
data = {
    "webhook": {
        "url": webhook_url,
        "webhook_by_events": True,
        "webhook_base64": True, 
        "events": ["MESSAGES_UPSERT"],
        "enabled": True
    }
}

headers = {"apikey": API_KEY, "Content-Type": "application/json"}
resp = requests.post(f"{URL_EVO}/webhook/set/{INSTANCE}", headers=headers, json=data)

print("Resultado:", resp.text)