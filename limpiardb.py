import psycopg2
import os

# 1. PEGA TU URL DE NEON AQUÍ (Entre las comillas)
DATABASE_URL = "postgresql://neondb_owner:npg_Za7onqsT1Ihw@ep-rough-tree-ai2htli9-pooler.c-4.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

def limpiar_base_de_datos():
    conn = None
    try:
        print("🔗 Conectando a Neon...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        print("🚀 Iniciando limpieza profunda...")
        
        # Agregamos la tabla que sale en tu imagen (mensajes/historial)
        # Si tu tabla tiene otro nombre, cámbialo aquí:
        tablas = ['pagos', 'citas', 'cliente', 'messages']
        
        for tabla in tablas:
            try:
                cur.execute(f"DELETE FROM {tabla};")
                print(f"  ✔ Tabla '{tabla}' limpiada.")
            except Exception as e:
                print(f"  ⚠ Saltando '{tabla}': {e}")
                conn.rollback() # Reinicia la transacción si la tabla no existe
                continue
        
        conn.commit()
        print("\n✅ ¡LISTO! Base de datos impecable para VENTIFY.")
        
    except Exception as e:
        print(f"\n❌ ERROR CRÍTICO: {e}")
    finally:
        if conn:
            cur.close()
            conn.close()
            print("🔌 Conexión cerrada.")

# ESTA PARTE ES VITAL: Es la que hace que el script corra al darle Play
if __name__ == "__main__":
    limpiar_base_de_datos()