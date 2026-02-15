import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from config import Config

class Database:
    _pool = None

    @classmethod
    def initialize(cls):
        if cls._pool is None:
            try:
                cls._pool = psycopg2.pool.SimpleConnectionPool(
                    1, 20, Config.DATABASE_URL, cursor_factory=RealDictCursor
                )
                print("🚀 DB Pool: INICIADO")
            except Exception as e:
                print(f"❌ Error DB Pool: {e}")

    @classmethod
    def get_conn(cls):
        return cls._pool.getconn() if cls._pool else psycopg2.connect(Config.DATABASE_URL, cursor_factory=RealDictCursor)

    @classmethod
    def release(cls, conn):
        if cls._pool: cls._pool.putconn(conn)
        else: conn.close()

    # --- CLIENTES ---
    @staticmethod
    def get_cliente(telefono, nombre_wa):
        conn = Database.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT nombre FROM clientes WHERE telefono = %s", (telefono,))
            res = cur.fetchone()
            if not res:
                cur.execute("INSERT INTO clientes (telefono, nombre) VALUES (%s, %s)", (telefono, nombre_wa))
                conn.commit()
                return "Nuevo"
            return res['nombre']
        finally: Database.release(conn)

    @staticmethod
    def update_cliente(telefono, nombre):
        conn = Database.get_conn()
        try:
            cur = conn.cursor(); cur.execute("UPDATE clientes SET nombre=%s WHERE telefono=%s", (nombre, telefono)); conn.commit()
        finally: Database.release(conn)

    # --- MEMORIA ---
    @staticmethod
    def get_historial(telefono):
        conn = Database.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT role, content FROM messages WHERE cliente_id = %s ORDER BY id DESC LIMIT 6", (telefono,))
            rows = cur.fetchall()
            return "\n".join([f"{'Cliente' if r['role'] == 'user' else 'Tú'}: {r['content']}" for r in reversed(rows)])
        finally: Database.release(conn)

    @staticmethod
    def save_mensaje(telefono, role, content):
        conn = Database.get_conn()
        try:
            cur = conn.cursor(); cur.execute("INSERT INTO messages (cliente_id, role, content) VALUES (%s, %s, %s)", (telefono, role, content)); conn.commit()
        finally: Database.release(conn)

    # --- PEDIDOS ---
    @staticmethod
    def get_pedido_activo(telefono):
        conn = Database.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago') LIMIT 1", (telefono,))
            return cur.fetchone()
        finally: Database.release(conn)

    @staticmethod
    def update_pedido(telefono, resumen, monto):
        conn = Database.get_conn()
        try:
            cur = conn.cursor()
            # Upsert lógico: Si existe actualiza, si no crea
            cur.execute("SELECT id FROM pedidos WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago')", (telefono,))
            if cur.fetchone():
                cur.execute("UPDATE pedidos SET resumen=%s, monto_total=%s WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago')", (resumen, monto, telefono))
            else:
                cur.execute("INSERT INTO pedidos (cliente_id, resumen, monto_total, estado) VALUES (%s, %s, %s, 'carrito')", (telefono, resumen, monto))
            conn.commit()
        finally: Database.release(conn)
    
    @staticmethod
    def cerrar_pedido(telefono):
        conn = Database.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE pedidos SET estado='pendiente_pago', direccion='Retiro en Tienda' WHERE cliente_id=%s AND estado IN ('carrito','pendiente_pago')", (telefono,))
            conn.commit()
        finally: Database.release(conn)

    # --- PAGOS ---
    @staticmethod
    def registrar_pago(telefono, ref, monto):
        conn = Database.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM pagos WHERE referencia=%s", (ref,))
            if cur.fetchone(): return False # Duplicado
            
            cur.execute("INSERT INTO pagos (cliente_id, referencia, monto, estado) VALUES (%s, %s, %s, 'pendiente')", (telefono, ref, monto))
            conn.commit()
            return True
        finally: Database.release(conn)