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
            
            # 1. Definimos las herramientas (Tools / Function Calling)
            # Esto le dice a la IA qué puede "hacer" en tu código
            herramientas = [
                {
                    "type": "function",
                    "function": {
                        "name": "agendar_pedido",
                        "description": "Agrega un producto al pedido o carrito del cliente.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "item": {
                                    "type": "string",
                                    "description": "Nombre del producto a agendar (ej. Pizza Margarita)"
                                },
                                "precio": {
                                    "type": "number",
                                    "description": "Precio total del producto según el menú"
                                }
                            },
                            "required": ["item", "precio"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "finalizar_pedido",
                        "description": "Cierra el pedido actual cuando el cliente indica que ya no quiere más nada, que es todo o que está listo."
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "actualizar_nombre",
                        "description": "Actualiza el nombre del cliente en la base de datos si es nuevo o pide que lo llamen de otra forma.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "nombre": {
                                    "type": "string",
                                    "description": "El nombre del cliente"
                                }
                            },
                            "required": ["nombre"]
                        }
                    }
                }
            ]

            payload = {
                "model": CerebroIA.MODELO,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": mensaje_usuario}
                ],
                "temperature": 0.0,
                "tools": herramientas # <-- Le pasamos el "manual de funciones"
            }
            
            res = requests.post(url, json=payload, headers=headers, timeout=10)
            data = res.json()
            
            # OpenRouter / Gemini devolverá un mensaje que puede contener texto Y/O tool_calls
            message = data['choices'][0]['message']
            texto = message.get('content') or ""
            tool_calls = message.get('tool_calls')
            
            # Ya no necesitamos el "PARCHE DE SEGURIDAD MÁGICO CON REGEX"
            # porque la IA ahora devuelve un objeto JSON estricto para las funciones.
            
            return {
                "texto": texto,
                "tool_calls": tool_calls
            }
            
        except Exception as e:
            print(f"Error IA: {e}")
            return {"texto": "Error técnico en IA.", "tool_calls": None}

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