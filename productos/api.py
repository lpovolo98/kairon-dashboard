"""Rutas del agente de productos.

Reutiliza la identidad que el middleware del portal ya verificó contra
Cloudflare Access: acá no hay login propio ni token nuevo.
"""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute

from . import cargar, imagenes, planilla, trabajos
from .esquema import COLUMNAS_PRODUCTOS, HOJAS

class RutaLimitada(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def limited(request):
            if request.method=='POST':
                limit=20*1024*1024 if request.url.path.endswith('/imagenes') else 8*1024*1024
                data=bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data)>limit:raise HTTPException(413,'La carga supera el tamaño permitido. Dividila en lotes.')
                request._body=bytes(data)
            return await original(request)
        return limited

router = APIRouter(route_class=RutaLimitada)
RAIZ = Path(__file__).resolve().parents[1]

# Tope de seguridad: por encima de esto la corrida se rechaza. Se puede subir
# por variable de entorno, pero el default protege de un pegado accidental.
TOPE_REGISTROS = int(os.getenv("PRODUCTOS_TOPE", "300"))


def ruta_datos():
    """Dónde vive el historial de corridas. En Railway hay un volumen montado
    en /data, así que el default ya es persistente y no hace falta configurar
    nada. Sin volumen cae al repo, que se pierde en cada deploy."""
    return Path(os.getenv("PRODUCTOS_DATA_DIR",
                          "/data/productos" if Path("/data").is_dir() else str(RAIZ / "productos-data")))


def persistente():
    """True si el historial sobrevive a un reinicio. Si es False, una corrida
    aplicada deja de poder revertirse cuando el contenedor se recicla."""
    return os.path.ismount("/data") and Path('/data').resolve() in (ruta_datos().resolve(), *ruta_datos().resolve().parents)


def directorio():
    p = ruta_datos()
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def conectar():
    db = sqlite3.connect(directorio() / "corridas.sqlite", timeout=15)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS corridas ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS pendientes ("
                       "id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            yield db
    finally:
        db.close()


def autorizar(request: Request):
    """La identidad la pone el middleware del portal después de verificar la
    firma de Cloudflare. Un header suelto no alcanza."""
    identidad = getattr(request.state, "usuario", None)
    if not (identidad and identidad.get("email")):
        raise HTTPException(401, "Ingresá por app.kaironsrl.com.ar con una cuenta habilitada.")
    email = identidad["email"].strip().lower()
    permitidos = [v.strip().lower() for v in os.getenv("PRODUCTOS_ALLOWED_EMAILS", "").split(",") if v.strip()]
    if permitidos and email not in permitidos:
        raise HTTPException(403, "Tu cuenta no está habilitada para el ABM de productos.")
    return email


class ClienteHTTP:
    """Traduce los errores de Odoo a respuestas con mensaje.

    Todo lo que hace este módulo pasa por .call(), así que envolverlo una vez
    alcanza para que ningún fallo de Odoo llegue al navegador como un
    "Internal Server Error" sin explicación.
    """

    def __init__(self, odoo):
        self._odoo = odoo

    def call(self, modelo, metodo, args, kwargs=None):
        from administracion.odoo import OdooError
        try:
            return self._odoo.call(modelo, metodo, args, kwargs)
        except OdooError as e:
            raise HTTPException(502, f"Odoo rechazó la consulta: {e}")

    def __getattr__(self, nombre):
        return getattr(self._odoo, nombre)


def _odoo():
    """Cliente de Odoo con las mismas credenciales que usa el dashboard.

    La clave se busca en ODOO_KEY y, si no está, en ODOO_PASSWORD: el
    dashboard la tiene con el segundo nombre. No se usa Odoo.desde_config()
    justamente porque solo mira ODOO_KEY y en Railway eso falla.
    """
    from administracion.odoo import Odoo, OdooError
    clave = os.getenv("ODOO_KEY") or os.getenv("ODOO_PASSWORD") or ""
    datos = {"url": os.getenv("ODOO_URL", ""), "db": os.getenv("ODOO_DB", ""),
             "usuario": os.getenv("ODOO_USER", ""), "clave": clave}
    faltan = [k.upper() for k, v in datos.items() if not v]
    if faltan:
        raise HTTPException(503, "Faltan credenciales de Odoo: " + ", ".join(
            "ODOO_PASSWORD" if f == "CLAVE" else "ODOO_" + f for f in faltan))
    try:
        return ClienteHTTP(Odoo(**datos))
    except OdooError as e:
        raise HTTPException(503, str(e))


def _limpiar(plan):
    """Las claves internas (_escribir, _id) no salen al navegador: el cliente
    manda filas, nunca escrituras."""
    return dict({hoja: [{k: v for k, v in f.items() if not k.startswith("_")} for f in filas]
            for hoja, filas in plan.items()}, _revision=trabajos.revision(plan))


# ─── Pantalla y datos de apoyo ───────────────────────────────

@router.get("/productos")
def pagina():
    return FileResponse(RAIZ / "static" / "productos.html")


@router.get("/api/productos/status")
def status(owner=Depends(autorizar)):
    faltan = [k for k in ("ODOO_URL", "ODOO_DB", "ODOO_USER") if not os.getenv(k)]
    if not (os.getenv("ODOO_KEY") or os.getenv("ODOO_PASSWORD")):
        faltan.append("ODOO_PASSWORD")
    return {"ready": not faltan, "missing": faltan, "tope": TOPE_REGISTROS,
            # Para poder verificar desde la pantalla que revertir va a seguir
            # siendo posible después de un reinicio.
            "datos": str(ruta_datos()), "persistente": persistente()}


def _catalogo(o):
    """Los valores válidos de cada desplegable, tal como están hoy en Odoo.
    Es lo que hacía descubrir_catalogo.py. Lo usan la pantalla y la plantilla
    que se descarga."""
    avisos = []

    def leer(modelo, dominio=None, limite=400):
        # Sin "order": display_name es un campo calculado y no almacenado, y
        # pedirle a Odoo que ordene por él termina en un error del servidor.
        # Ordenar acá cuesta nada y no depende de qué campos tenga el modelo.
        #
        # Y si una lista falla -un modelo sin permisos, un campo que no existe
        # en esta instancia- se devuelve vacía con un aviso, en vez de tirar
        # abajo la pantalla entera por un solo desplegable.
        try:
            filas=[]; offset=0
            while True:
                page=o.call(modelo, "search_read", [dominio or []],
                           {"fields": ["display_name"], "limit": limite, 'offset':offset,'order':'id'})
                filas.extend(page)
                if len(page)<limite: break
                offset+=limite
                if offset>=5000:
                    avisos.append('El catálogo '+modelo+' supera 5000 opciones. Usá un ID explícito para los restantes.');break
        except HTTPException as e:
            avisos.append(f"{modelo}: {e.detail}")
            return []
        return sorted(({"id": f["id"], "name": f.get("display_name") or str(f["id"])}
                       for f in filas), key=lambda x: x["name"].lower())

    return {
        "categorias":       leer("product.category"),
        "unidades":         leer("uom.uom"),
        "impuestos_venta":  leer("account.tax", [["type_tax_use", "=", "sale"]]),
        "impuestos_compra": leer("account.tax", [["type_tax_use", "=", "purchase"]]),
        "listas":           leer("product.pricelist"),
        "proveedores":      leer("res.partner", [["supplier_rank", ">", 0]]),
        # Para que la modificación masiva ofrezca solo columnas que existen,
        # con las operaciones que su tipo admite.
        "columnas": [{"col": c["col"], "tipo": c["tipo"]}
                     for c in COLUMNAS_PRODUCTOS if not c.get("clave")],
        "avisos": avisos,
    }


@router.get("/api/productos/catalogo")
def catalogo(owner=Depends(autorizar)):
    return _catalogo(_odoo())


@router.get("/api/productos/plantilla.xlsx")
def plantilla(owner=Depends(autorizar)):
    """Plantilla en blanco, con los valores que Odoo acepta hoy en una hoja
    aparte. Es lo que hacía descubrir_catalogo.py, ya integrado."""
    try:
        datos = planilla.generar_plantilla(_catalogo(_odoo()))
    except ValueError as e:
        raise HTTPException(503, str(e))
    return Response(datos, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="plantilla_catalogo.xlsx"'})


@router.post("/api/productos/leer")
async def leer(request: Request, nombre: str = "", owner=Depends(autorizar)):
    """Lee la plantilla y devuelve las filas crudas. No toca Odoo: la
    previsualización es un paso aparte.

    El archivo llega como cuerpo crudo, igual que los PDF del agente
    administrativo: así el tope de tamaño se aplica mientras se recibe, sin
    juntar en memoria un archivo enorme antes de rechazarlo."""
    contenido = bytearray()
    async for pedazo in request.stream():
        contenido.extend(pedazo)
        if len(contenido) > planilla.TOPE_BYTES:
            raise HTTPException(413, f"El archivo supera los {planilla.TOPE_BYTES // 1024 // 1024} MB.")
    if not contenido:
        raise HTTPException(422, "El archivo llegó vacío.")
    try:
        return planilla.leer(nombre, bytes(contenido))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/api/productos/buscar")
def buscar(categoria: str = "", proveedor: str = "", texto: str = "",
           owner=Depends(autorizar)):
    """Productos que matchean el filtro, para la modificación masiva sin
    archivo."""
    if not (categoria or proveedor or texto):
        raise HTTPException(422, "Elegí al menos un filtro: se estarían trayendo todos los productos.")
    try:
        return cargar.buscar_para_editar(_odoo(), categoria or None, proveedor or None, texto or None)
    except cargar.Frenar as e:
        raise HTTPException(422, str(e))


@router.post("/api/productos/operacion")
def operacion(cuerpo: dict, owner=Depends(autorizar)):
    """Arma las filas que resultan de aplicar una operación al resultado de
    un filtro, y las previsualiza por el camino de siempre. La modificación
    masiva no tiene un camino de escritura propio."""
    o = _odoo()
    try:
        productos = cargar.buscar_para_editar(
            o, cuerpo.get("categoria") or None, cuerpo.get("proveedor") or None,
            cuerpo.get("texto") or None)
        filas = cargar.aplicar_operacion(productos, cuerpo.get("columna"),
                                         cuerpo.get("operacion"), cuerpo.get("valor"))
    except cargar.Frenar as e:
        raise HTTPException(422, str(e))
    if not filas:
        raise HTTPException(422, "Ningún producto del filtro cambia con esa operación.")
    return {"filas": filas, "plan": _limpiar(cargar.previsualizar(o, {"productos": filas}))}


@router.get("/api/productos/sin-imagen")
def productos_sin_imagen(owner=Depends(autorizar), todos: bool=False):
    if not todos: return imagenes.sin_imagen(_odoo())
    o=_odoo();rows=[];offset=0
    while True:
        page=o.call('product.template','search_read',[[['default_code','!=',False]]],{'fields':['default_code','name','image_128'],'limit':300,'offset':offset,'order':'id','context':{'bin_size':True}})
        rows.extend(page)
        if len(page)<300:break
        offset+=300
        if offset>=5000:raise HTTPException(422,'Más de 5000 productos: dividí la selección de fotos por catálogo.')
    return [{'sku':r['default_code'].strip(),'nombre':r['name'],'tiene_imagen':bool(r.get('image_128'))} for r in rows]

@router.get('/api/productos/foto')
def foto(sku: str, owner=Depends(autorizar)):
    import base64
    o=_odoo();errors=[];id_=cargar.buscar_producto_por_sku(o,sku,errors)
    if not id_ or errors:raise HTTPException(404,'Producto no disponible.')
    rows=o.call('product.template','read',[[id_]],{'fields':['image_128']})
    if not rows or not rows[0].get('image_128'):raise HTTPException(404,'El producto no tiene foto.')
    data=base64.b64decode(rows[0]['image_128'])
    return Response(data,media_type='image/png' if data.startswith(b'\x89PNG') else 'image/jpeg',headers={'Cache-Control':'private, no-store'})


# ─── Previsualizar y aplicar ─────────────────────────────────

@router.post("/api/productos/previsualizar")
def previsualizar(cuerpo: dict, owner=Depends(autorizar)):
    hojas = _hojas_de(cuerpo)
    try:
        return _limpiar(cargar.previsualizar(_odoo(), hojas))
    except cargar.Frenar as e:
        raise HTTPException(422, str(e))


@router.post("/api/productos/aplicar")
def aplicar(cuerpo: dict, owner=Depends(autorizar)):
    hojas = _hojas_de(cuerpo)
    revision=cuerpo.get('revision',''); request_id=cuerpo.get('request_id','')
    if not isinstance(revision,str) or len(revision)!=64 or not isinstance(request_id,str) or not 16<=len(request_id)<=100:
        raise HTTPException(422,'Previsualizá los cambios antes de aplicar.')
    with conectar() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute("SELECT id,data FROM corridas WHERE owner=? AND json_extract(data,'$.request_id')=?",(owner,request_id)).fetchone()
        if existing:
            old=json.loads(existing[1])
            if old.get('revision')!=revision:raise HTTPException(409,'La clave de carga ya se usó con otra revisión. Volvé a previsualizar.')
            return {'id':existing[0],'estado':old['estado']}
        active=db.execute("SELECT count(*) FROM corridas WHERE json_extract(data,'$.estado') IN ('pendiente','validando','ejecutando')").fetchone()[0]
        if active>=10: raise HTTPException(429,'Hay varias cargas en proceso. Intentá más tarde.')
        data={'tipo':'catalogo','estado':'pendiente','revision':revision,'request_id':request_id,'cuando':datetime.now(timezone.utc).isoformat(),'operaciones':[]}
        job_id=db.execute('INSERT INTO corridas(owner,data) VALUES (?,?)',(owner,json.dumps(data))).lastrowid
    try: trabajos.executor.submit(trabajos.ejecutar,job_id,hojas,revision)
    except Exception:
        trabajos.guardar(job_id,estado='rechazada',mensaje='No se pudo iniciar. Volvé a previsualizar.')
        raise HTTPException(503,'No se pudo iniciar la carga.')
    return dict(data,id=job_id)


@router.post("/api/productos/imagenes")
async def subir_imagenes(request: Request, owner=Depends(autorizar)):
    content=bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content)>20*1024*1024: raise HTTPException(413,'El lote de fotos supera los 20 MB. Dividilo en varias cargas.')
    try: cuerpo=json.loads(content)
    except Exception: raise HTTPException(422,'El lote de fotos no es válido.')
    if not isinstance(cuerpo,dict): raise HTTPException(422,'Cuerpo inválido.')
    asignaciones = cuerpo.get("asignaciones") or []
    if not isinstance(asignaciones, list) or not asignaciones:
        raise HTTPException(422, "No llegó ninguna imagen.")
    if len(asignaciones) > TOPE_REGISTROS:
        raise HTTPException(422, f"Son {len(asignaciones)} imágenes y el tope es {TOPE_REGISTROS}.")
    if any(not isinstance(a,dict) for a in asignaciones): raise HTTPException(422,'Asignaciones inválidas.')
    request_id=cuerpo.get('request_id','')
    if not isinstance(request_id,str) or not 16<=len(request_id)<=100: raise HTTPException(422,'Volvé a revisar las fotos antes de subirlas.')
    with conectar() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute("SELECT id,data FROM corridas WHERE owner=? AND json_extract(data,'$.request_id')=?",(owner,request_id)).fetchone()
        if previous: return {'id':previous[0],'estado':json.loads(previous[1]).get('estado')}
        active=db.execute("SELECT count(*) FROM corridas WHERE json_extract(data,'$.estado') IN ('pendiente','validando','ejecutando')").fetchone()[0]
        if active>=3: raise HTTPException(429,'Hay cargas pendientes. Esperá a que terminen.')
        data={'tipo':'imagenes','estado':'pendiente','request_id':request_id,'cuando':datetime.now(timezone.utc).isoformat(),'operaciones':[]}
        job_id=db.execute('INSERT INTO corridas(owner,data) VALUES (?,?)',(owner,json.dumps(data))).lastrowid
    try: trabajos.executor.submit(trabajos.ejecutar,job_id,asignaciones,None,True)
    except Exception:
        trabajos.guardar(job_id,estado='rechazada',mensaje='No se pudo iniciar la carga.')
        raise HTTPException(503,'No se pudo iniciar la carga.')
    return {'id':job_id,'estado':'pendiente'}


@router.get("/api/productos/corridas")
def corridas(owner=Depends(autorizar)):
    with conectar() as db:
        filas = db.execute("SELECT id,data FROM corridas WHERE owner=? ORDER BY id DESC LIMIT 20",
                           (owner,)).fetchall()
    result=[]
    for i,d in filas:
        data=json.loads(d)
        operations=data.pop('operaciones',[]); data.pop('deshacer',None)
        data['avance']=len([op for op in operations if op.get('estado')=='confirmada'])
        data['registros']=[{'modelo':op['modelo'],'id':op['ids'][0]} for op in operations if op.get('ids') and op.get('estado')=='confirmada']
        data['odoo_url']=os.getenv('ODOO_URL','')
        result.append(dict(data,id=i))
    return result


@router.post("/api/productos/corridas/{corrida_id}/revertir")
def revertir(corrida_id: int, owner=Depends(autorizar), revisar: bool = False):
    with conectar() as db:
        fila = db.execute("SELECT data FROM corridas WHERE id=? AND owner=?",
                          (corrida_id, owner)).fetchone()
    if not fila:
        raise HTTPException(404, "Esa corrida no existe.")
    datos = json.loads(fila[0])
    if datos.get("revertida"):
        raise HTTPException(409, "Esa corrida ya fue revertida.")
    with closing(sqlite3.connect(directorio()/'escritor.sqlite',timeout=60)) as guard, guard:
        guard.execute('CREATE TABLE IF NOT EXISTS mutex (id INTEGER)'); guard.execute('BEGIN IMMEDIATE')
        # Read again under the writer lock; another request may have completed.
        with conectar() as db:
            datos=json.loads(db.execute('SELECT data FROM corridas WHERE id=? AND owner=?',(corrida_id,owner)).fetchone()[0])
        if datos.get('estado') not in ('completada','parcial'):
            raise HTTPException(409,'Esta corrida no está disponible para una reversión automática.')
        try:
            o=_odoo()
            if revisar: return {'acciones':trabajos.plan_reversion(o,datos)}
            trabajos.revertir(o,corrida_id,datos)
        except cargar.Frenar as e: raise HTTPException(409,str(e))
        except Exception:
            trabajos.guardar(corrida_id,estado='incierta',mensaje='La reversión se interrumpió. Revisá los registros antes de continuar.')
            raise HTTPException(502,'Reversión interrumpida. El avance quedó registrado.')
    return {'estado':'revertida'}


# ─── Cola compartida con el agente administrativo ────────────

@router.get("/api/productos/pendientes")
def pendientes(owner=Depends(autorizar)):
    with conectar() as db:
        filas = db.execute("SELECT data FROM pendientes").fetchall()
    return [json.loads(d) for (d,) in filas
            if json.loads(d).get("estado", "pendiente") == "pendiente" and any(f.get('owner')==owner for f in json.loads(d).get('facturas',[]))]


def encolar(item):
    """La llama el agente administrativo cuando una factura trae un código de
    proveedor que todavía no existe como producto. Idempotente por
    proveedor + código: una factura reintentada no duplica el pedido."""
    import hashlib
    clave = hashlib.sha256(f"{item.get('proveedor_id','')}|{item.get('codigo_proveedor','')}".encode()).hexdigest()
    item = dict(item, estado=item.get("estado", "pendiente"),
                creado=datetime.now(timezone.utc).isoformat())
    with conectar() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT data FROM pendientes WHERE id=?',(clave,)).fetchone()
        facts=json.loads(previous[0]).get('facturas',[]) if previous else []
        fact={'id':item.get('job_id'),'owner':item.get('owner')}
        if fact not in facts:facts.append(fact)
        item.update(id=clave,facturas=facts)
        db.execute('INSERT OR REPLACE INTO pendientes VALUES (?,?)',(clave,json.dumps(item)))
    return clave

def resolver_pendientes(o):
    from administracion.api import continuar_productos
    with conectar() as db:rows=db.execute('SELECT id,data FROM pendientes').fetchall()
    completed=[]
    for key,encoded in rows:
        item=json.loads(encoded)
        if item.get('estado')=='resuelto':
            completed.extend(f['id'] for f in item.get('facturas',[]) if f.get('id'))
            continue
        if item.get('estado')!='pendiente' or not item.get('proveedor_id'):continue
        matches=o.call('product.supplierinfo','search',[[['partner_id','=',item['proveedor_id']],['product_code','=',item['codigo_proveedor']]]])
        if len(matches)!=1:continue
        item['estado']='resuelto'
        with conectar() as db:db.execute('UPDATE pendientes SET data=? WHERE id=?',(json.dumps(item),key))
        completed.extend(f['id'] for f in item.get('facturas',[]) if f.get('id'))
    with conectar() as db:remaining=[json.loads(r[0]) for r in db.execute('SELECT data FROM pendientes').fetchall()]
    for job_id in set(completed):
        if not any(item.get('estado')=='pendiente' and any(f.get('id')==job_id for f in item.get('facturas',[])) for item in remaining):
            continuar_productos(job_id)

@router.post('/api/productos/pendientes/revisar')
def revisar_pendientes(owner=Depends(autorizar)):
    resolver_pendientes(_odoo())
    return {'ok':True}

@router.post('/api/productos/pendientes/{pendiente_id}/preparar')
def preparar_pendiente(pendiente_id: str, cuerpo: dict, owner=Depends(autorizar)):
    items=pendientes(owner)
    item=next((i for i in items if i['id']==pendiente_id),None)
    if not item:raise HTTPException(404,'Pendiente no disponible.')
    sku=str(cuerpo.get('sku') or '').strip()
    if not sku:raise HTTPException(422,'Indicá el SKU del producto existente o el código nuevo.')
    o=_odoo();errors=[];exists=cargar.buscar_producto_por_sku(o,sku,errors)
    if errors:raise HTTPException(422,'; '.join(errors))
    product={'Referencia interna (SKU)':sku}
    if not exists:
        product.update({'Nombre':cuerpo.get('nombre',''),'Unidades por caja':cuerpo.get('unidades')})
    hojas={'productos':[product],'proveedores':[{'SKU (igual al de Productos)':sku,
        'Proveedor':f"{item['proveedor']} (id {item['proveedor_id']})",'Código del proveedor':item['codigo_proveedor'],
        'Precio':cuerpo.get('precio')}],'precios':[]}
    return {'filas':hojas,'plan':_limpiar(cargar.previsualizar(o,hojas))}


# ─── Auxiliares ──────────────────────────────────────────────

def _hojas_de(cuerpo):
    if not isinstance(cuerpo, dict):
        raise HTTPException(422, "Cuerpo inválido.")
    hojas = {h: cuerpo.get(h) or [] for h in HOJAS}
    if not any(hojas.values()):
        raise HTTPException(422, "No llegó ninguna fila.")
    for nombre, filas in hojas.items():
        if not isinstance(filas, list) or any(not isinstance(f, dict) for f in filas):
            raise HTTPException(422, f"La hoja «{nombre}» tiene que ser una lista de filas.")
    if sum(len(f) for f in hojas.values())>TOPE_REGISTROS:
        raise HTTPException(413,f'El máximo por carga es {TOPE_REGISTROS} filas entre las tres hojas.')
    return hojas

@router.on_event('startup')
def recuperar_trabajos():
    with conectar() as db:
        for job_id,encoded in db.execute('SELECT id,data FROM corridas').fetchall():
            data=json.loads(encoded)
            if data.get('estado') in ('pendiente','validando','ejecutando','revirtiendo'):
                data.update(estado='incierta' if data.get('operaciones') else 'rechazada',mensaje='El servidor se reinició. Revisá el historial antes de repetir la carga.')
                db.execute('UPDATE corridas SET data=? WHERE id=?',(json.dumps(data),job_id))


def _guardar_corrida(owner, tipo, resultado):
    datos = dict(resultado, tipo=tipo, cuando=datetime.now(timezone.utc).isoformat())
    with conectar() as db:
        cur = db.execute("INSERT INTO corridas (owner,data) VALUES (?,?)", (owner, json.dumps(datos)))
        datos["id"] = cur.lastrowid
    return datos
