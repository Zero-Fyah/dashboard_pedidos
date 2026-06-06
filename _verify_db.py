import sqlite3
from pathlib import Path

db = Path("data/pedidos.db")
assert db.exists(), f"DB no encontrada en {db.resolve()}"

con = sqlite3.connect(db)

print("=== CLIENTE ===")
for row in con.execute("SELECT servicio_cliente, nombre_empresa FROM pedidos LIMIT 5"):
    print(row)

print("=== COLUMNAS _NUM ===")
cols = [r[1] for r in con.execute("PRAGMA table_info(lineas_pedido)")]
print([c for c in cols if c.endswith("_num")])

print("=== VIEWS ===")
for row in con.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"):
    print(row[0])

print("=== ALMACENES ===")
for row in con.execute(
    "SELECT DISTINCT almacen FROM lineas_pedido "
    "WHERE almacen IS NOT NULL AND almacen != '' ORDER BY almacen"
):
    print(row[0])

print("=== ESTADOS SUBPEDIDO ===")
for row in con.execute(
    "SELECT DISTINCT estado FROM subpedidos "
    "WHERE estado IS NOT NULL ORDER BY estado"
):
    print(row[0])

print("=== CONTEOS ===")
for t in ["pedidos","subpedidos","lineas_pedido","timeline_pedido",
          "estadisticas_monto","gestion_diferencias","detalle_diferencias",
          "registro_operaciones","errores"]:
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"{t}: {n}")

con.close()
