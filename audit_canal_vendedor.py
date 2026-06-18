"""
Verifica los campos x_studio_canal y x_studio_tipo_de_comercio en res.partner,
y prueba el filtro de "clientes en cartera" que pasó el usuario.
"""
import xmlrpc.client
import os
from dotenv import load_dotenv

load_dotenv()
ODOO_URL  = os.getenv("ODOO_URL")
ODOO_DB   = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASS = os.getenv("ODOO_PASSWORD")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print(f"Conectado como UID {uid}\n")

# 1. Verificar que los campos existen en res.partner
print("=" * 90)
print("Campos x_studio_canal y x_studio_tipo_de_comercio en res.partner:")
print("=" * 90)
fields_info = models.execute_kw(
    ODOO_DB, uid, ODOO_PASS,
    "res.partner", "fields_get", [],
    {"attributes": ["string", "type", "selection"]}
)
for fname in ["x_studio_canal", "x_studio_tipo_de_comercio"]:
    if fname in fields_info:
        print(f"  {fname}: {fields_info[fname]}")
    else:
        print(f"  {fname}: NO EXISTE")

# 2. Probar el filtro de cartera que dio el usuario
print("\n" + "=" * 90)
print("Clientes en cartera (filtro del usuario):")
print("=" * 90)
domain = ["&", "&", "&",
    ("sale_order_ids", "!=", False),
    ("name", "not ilike", "NUTREGAL"),
    ("name", "not ilike", "MERCADERIA"),
    ("name", "not ilike", "Julio K")
]
clientes = models.execute_kw(
    ODOO_DB, uid, ODOO_PASS,
    "res.partner", "search_read",
    [domain],
    {"fields": ["id", "name", "x_studio_canal", "x_studio_tipo_de_comercio", "user_id"], "limit": 500}
)
print(f"Total clientes en cartera: {len(clientes)}")
print("\nPrimeros 20:")
for c in clientes[:20]:
    canal = c.get("x_studio_canal") or "?"
    tipo = c.get("x_studio_tipo_de_comercio") or "?"
    vendedor = c["user_id"][1] if c.get("user_id") else "?"
    print(f"  {c['name']:<40} canal={canal:<20} tipo={tipo:<20} vendedor={vendedor}")

# 3. Distribución de canales y tipos de comercio
print("\n" + "=" * 90)
print("Distribución de valores de canal:")
print("=" * 90)
from collections import Counter
canales = Counter(c.get("x_studio_canal") or "(vacío)" for c in clientes)
for canal, count in canales.most_common():
    print(f"  {canal:<30} {count} clientes")

print("\n" + "=" * 90)
print("Distribución de valores de tipo de comercio:")
print("=" * 90)
tipos = Counter(c.get("x_studio_tipo_de_comercio") or "(vacío)" for c in clientes)
for tipo, count in tipos.most_common():
    print(f"  {tipo:<30} {count} clientes")

# 4. Ver si user_id en res.partner está poblado o si hay que sacarlo de sale.order
print("\n" + "=" * 90)
print("¿El vendedor en res.partner (user_id) coincide con vendedores reales de ventas?")
print("=" * 90)
con_vendedor = sum(1 for c in clientes if c.get("user_id"))
print(f"Clientes con user_id seteado: {con_vendedor} de {len(clientes)}")

# 5. Vendedores reales en sale.order (para comparar)
ordenes_recientes = models.execute_kw(
    ODOO_DB, uid, ODOO_PASS,
    "sale.order", "search_read",
    [[["state","in",["sale","done"]]]],
    {"fields": ["id", "user_id", "partner_id"], "limit": 50}
)
print("\nVendedores en órdenes de venta recientes (sale.order.user_id):")
vendedores_ordenes = Counter(o["user_id"][1] if o.get("user_id") else "(sin vendedor)" for o in ordenes_recientes)
for v, count in vendedores_ordenes.most_common():
    print(f"  {v:<30} {count} órdenes")
