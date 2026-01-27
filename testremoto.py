import requests

# Tu URL de Render
url = "https://barberia-bot-8sju.onrender.com/chat"

# El mensaje que enviamos
payload = {
    "message": "Hola, ¿qué servicios ofrecen y cuánto cuesta el corte?"
}

try:
    print(f"Enviando mensaje a {url}...")
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        print("\n✅ ¡ÉXITO! Respuesta del bot:")
        print("------------------------------------------------")
        print(response.json()['response'])
        print("------------------------------------------------")
    else:
        print(f"\n❌ Error {response.status_code}:")
        print(response.text)

except Exception as e:
    print(f"\n❌ Error de conexión: {e}")