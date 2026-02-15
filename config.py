import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    DATABASE_URL = os.environ.get("DATABASE_URL")
    OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
    META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
    META_PHONE_ID = os.environ.get("META_PHONE_ID")
    ADMIN_PHONE = str(os.environ.get("ADMIN_PHONE", "")).replace("+", "").strip()
    BUSINESS_CONTEXT = os.environ.get("BUSINESS_CONTEXT", "Asistente virtual.")
    PORT = int(os.environ.get("PORT", 10000))
    
    # Tasas por defecto (se actualizan solas)
    TASA_USD = 0.0 
    TASA_EUR = 0.0