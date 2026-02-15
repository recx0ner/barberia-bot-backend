import requests
import json
import re
from config import Config

class CerebroIA:
    MODELO = "google/gemini-2.5-flash"
    
    @staticmethod
    def consultar(prompt, mensaje_usuario):
        try:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {Config.OPENROUTER_KEY}"}
            payload = {
                "model": CerebroIA.MODELO,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": mensaje_usuario}
                ],
                "temperature": 0.0
            }
            res = requests.post(url, json=payload, headers=headers, timeout=10)
            texto = res.json()['choices'][0]['message']['content']
            
            # --- PARCHE DE SEGURIDAD (BUG $0) ---
            # Si la IA devuelve monto 0 en la etiqueta, buscamos $ en el texto
            if "[AGENDAR:" in texto:
                try:
                    partes = texto.split("[AGENDAR:")[1].split("]")[0].split("|")
                    monto_ia = float(partes[1].strip()) if len(partes) > 1 else 0
                    
                    if monto_ia == 0:
                        print("⚠️ IA envió $0. Aplicando Regex...")
                        # Busca "$10", "$ 10", "10$"
                        match = re.search(r'\$\s?(\d+(?:\.\d+)?)', texto) or re.search(r'(\d+(?:\.\d+)?)\s?\$', texto)
                        if match:
                            nuevo_monto = match.group(1)
                            # Reconstruimos la etiqueta con el monto correcto
                            texto = texto.replace(f"| {partes[1].strip()}", f"| {nuevo_monto}")
                            print(f"✅ Corregido a: {nuevo_monto}")
                except: pass
            
            return texto
        except Exception as e:
            print(f"Error IA: {e}")
            return "Error técnico en IA."

    @staticmethod
    def analizar_pago(base64_img):
        try:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {Config.OPENROUTER_KEY}"}
            payload = {
                "model": "google/gemini-2.0-flash-001",
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": "Extrae REFERENCIA (números) y MONTO. JSON: {\"ref\": \"1234\", \"monto\": 1950.00}. Si falla: {\"error\": true}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}
                    ]}
                ]
            }
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            content = res.json()['choices'][0]['message']['content'].replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        except: return {"error": True}