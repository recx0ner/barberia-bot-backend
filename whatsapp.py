import requests
from config import Config

class WhatsApp:
    @staticmethod
    def enviar_mensaje(telefono, texto):
        try:
            url = f"https://graph.facebook.com/v17.0/{Config.META_PHONE_ID}/messages"
            headers = {
                "Authorization": f"Bearer {Config.META_TOKEN}",
                "Content-Type": "application/json"
            }
            data = {
                "messaging_product": "whatsapp",
                "to": telefono,
                "type": "text",
                "text": {"body": texto}
            }
            requests.post(url, json=data, headers=headers, timeout=5)
        except Exception as e:
            print(f"❌ Error WhatsApp: {e}")

    @staticmethod
    def obtener_url_imagen(media_id):
        try:
            url = f"https://graph.facebook.com/v17.0/{media_id}"
            headers = {"Authorization": f"Bearer {Config.META_TOKEN}"}
            res = requests.get(url, headers=headers).json()
            return res.get('url')
        except: return None
    
    @staticmethod
    def descargar_imagen(url):
        try:
            headers = {"Authorization": f"Bearer {Config.META_TOKEN}"}
            return requests.get(url, headers=headers).content
        except: return None