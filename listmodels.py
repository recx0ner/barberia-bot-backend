import google.generativeai as genai
import os

# Usa una de tus claves para probar
genai.configure(api_key="AIzaSyCorBS1rL5hNCjCUx-XqDFGplem1cxM9YE")

print("Modelos disponibles para generar contenido:")
for m in genai.list_models():
  if 'generateContent' in m.supported_generation_methods:
    print(f"- {m.name}")