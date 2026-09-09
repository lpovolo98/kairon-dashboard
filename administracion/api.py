import hashlib
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from . import service

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='facturas')
lock = threading.Lock()
log = logging.getLogger(__name__)
MAX_BYTES = 15 * 1024 * 1024


def directory():
    p = Path(os.getenv('ADMIN_DATA_DIR', '/data/administracion' if Path('/data').is_dir()
                       else str(Path(__file__).resolve().parents[1] / 'administracion-data')))
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def connect():
    db = sqlite3.connect(directory() / 'jobs.sqlite', timeout=15)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)')
            yield db
    finally:
        db.close()


def save(job_id, **changes):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        data = json.loads(row[0])
        data.update(changes, updated_at=datetime.now(timezone.utc).isoformat())
        db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(data), job_id))


def authorize(request: Request):
    # Set only by the portal middleware after verifying Cloudflare's signature.
    identity = getattr(request.state, 'usuario', None)
    if identity and identity.get('email'):
        email = identity['email'].strip().lower()
        allowed = [v.strip().lower() for v in os.getenv('ADMIN_ALLOWED_EMAILS', '').split(',') if v.strip()]
        if not allowed or email in allowed:
            return email
        raise HTTPException(403, 'Tu cuenta no está habilitada para administración')
    token = os.getenv('ADMIN_ACCESS_TOKEN', '')
    if token and secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
        return 'administracion'
    domain, audience = os.getenv('CF_ACCESS_TEAM_DOMAIN'), os.getenv('CF_ACCESS_AUD')
    assertion = request.headers.get('cf-access-jwt-assertion')
    if domain and audience and assertion:
        import jwt
        if not domain.endswith('.cloudflareaccess.com') or '/' in domain:
            raise HTTPException(503, 'Configuración de acceso inválida')
        try:
            issuer = 'https://' + domain
            key = jwt.PyJWKClient(issuer + '/cdn-cgi/access/certs').get_signing_key_from_jwt(assertion)
            claims = jwt.decode(assertion, key.key, algorithms=['RS256'], audience=audience,
                                issuer=issuer, options={'require': ['exp', 'sub', 'email']})
            allowed = [v.strip().lower() for v in os.getenv('ADMIN_ALLOWED_EMAILS', '').split(',') if v.strip()]
            if claims['email'].lower() in allowed:
                return claims['email'].lower()
        except Exception:
            pass
    if not token and not (domain and audience):
        raise HTTPException(503, 'Falta habilitar el acceso al agente administrativo')
    raise HTTPException(401, 'Ingresá con una cuenta habilitada para administración')


def configured():
    required = ['ODOO_URL', 'ODOO_DB', 'ODOO_USER']
    missing = [k for k in required if not os.getenv(k)]
    if not (os.getenv('ODOO_KEY') or os.getenv('ODOO_PASSWORD')):
        missing.append('ODOO_PASSWORD')
    return missing


@router.get('/administracion')
def page():
    return FileResponse(Path(__file__).resolve().parents[1] / 'static' / 'administracion.html')


@router.get('/api/administracion/status')
def status(owner=Depends(authorize)):
    return {'ready': not configured(), 'missing': configured(), 'mode':'draft',
            'reader': 'general' if os.getenv('OPENAI_API_KEY') and os.getenv('ADMIN_MODEL') else 'cibos-starbread'}


@router.get('/api/administracion/facturas')
def history(owner=Depends(authorize)):
    with connect() as db:
        rows = db.execute('SELECT data FROM jobs WHERE owner=? ORDER BY rowid DESC LIMIT 50', (owner,)).fetchall()
    result = []
    for row in rows:
        item = json.loads(row[0])
        if item.get('move_id'):
            item['odoo_url'] = os.environ.get('ODOO_URL', '').rstrip('/') + '/web#id=' + str(item['move_id']) + '&model=account.move&view_type=form'
        if item.get('purchase_id'):
            item['purchase_url'] = os.environ.get('ODOO_URL','').rstrip('/') + '/web#id=' + str(item['purchase_id']) + '&model=purchase.order&view_type=form'
        item['picking_urls'] = [os.environ.get('ODOO_URL','').rstrip('/') + '/web#id=' + str(pid) + '&model=stock.picking&view_type=form' for pid in item.get('picking_ids',[])]
        result.append(item)
    return result


def work(job_id, pdf, document=None):
    try:
        # Keep the exact document for a controlled continuation after product mapping.
        if len(job_id)==64 and all(c in '0123456789abcdef' for c in job_id):
            (directory()/(job_id+'.pdf')).write_bytes(pdf)
        save(job_id, state='leyendo', message='Leyendo factura')
        doc = document if document is not None else service.extract(pdf)
        save(job_id, state='validando', message='Controlando datos en Odoo', document=doc)
        # SQLite lock spans the Odoo workflow, serializing writes across workers sharing a volume.
        with closing(sqlite3.connect(directory() / 'writer.sqlite', timeout=600)) as guard, guard:
            guard.execute('CREATE TABLE IF NOT EXISTS mutex (id INTEGER)')
            guard.execute('BEGIN IMMEDIATE')
            service.process(doc, pdf, lambda **kw: save(job_id, **kw))
    except Exception as exc:
        with connect() as db:
            data = json.loads(db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()[0])
            owner=db.execute('SELECT owner FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
        if isinstance(exc,service.ProductosPendientes) and not data.get('purchase_id') and not data.get('move_id'):
            from productos.api import encolar
            for line in exc.lineas:
                encolar({'proveedor':data['document']['proveedor']['nombre'],
                    'proveedor_id':exc.proveedor['id'],'codigo_proveedor':line['codigo_proveedor'],
                    'descripcion':line['descripcion'],'precio':line['precio_unitario'],
                    'factura':data['document']['comprobante']['numero'],'job_id':job_id,'owner':owner})
            save(job_id,state='esperando_productos',message=str(exc))
            return
        mutation = data['state'] not in ('recibido', 'leyendo', 'validando') or bool(data.get('supplier_id'))
        if isinstance(exc, (ValueError, service.loader.Frenar)) and not mutation:
            message = str(exc)[:2000]
        elif mutation:
            message = 'La operación se interrumpió. Revisá la orden y la factura en Odoo antes de volver a cargarla.'
        else:
            message = 'No se pudo completar la lectura o conexión. Revisá la configuración del servicio.'
        log.warning('Administrative job %s stopped (%s)', job_id, type(exc).__name__)
        save(job_id, state='revision' if not mutation else 'resultado_incierto', message=message)


@router.post('/api/administracion/facturas', status_code=202)
async def upload(request: Request, owner=Depends(authorize)):
    if configured():
        raise HTTPException(503, 'El agente todavía requiere configuración')
    if request.headers.get('content-type', '').split(';')[0] != 'application/pdf':
        raise HTTPException(415, 'Seleccioná un archivo PDF')
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise HTTPException(413, 'El PDF supera los 15 MB')
    pdf = bytes(content)
    if not pdf.startswith(b'%PDF-'):
        raise HTTPException(422, 'El archivo no es un PDF válido')
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 20:
            raise ValueError()
    except Exception:
        raise HTTPException(422, 'Usá un PDF sin contraseña y de hasta 20 páginas')
    job_id = hashlib.sha256(pdf).hexdigest()
    data = {'id': job_id, 'state': 'recibido', 'message': 'Factura recibida',
            'created_at': datetime.now(timezone.utc).isoformat()}
    with lock, connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT owner,data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if previous:
            old = json.loads(previous[1])
            retryable = previous[0] == owner and old.get('state') == 'revision' and not any(
                old.get(key) for key in ('supplier_id','purchase_id','move_id','original_move_id','picking_ids'))
            if not retryable:
                raise HTTPException(409, 'Este PDF ya fue recibido. Revisá el historial antes de volver a cargarlo.')
            data['previous_attempts'] = old.get('previous_attempts', []) + [{
                'state':old['state'], 'message':old.get('message'), 'created_at':old.get('created_at')}]
        # Bound queue memory and prevent unbounded paid extraction requests.
        active = db.execute("SELECT count(*) FROM jobs WHERE json_extract(data, '$.state') IN ('recibido','leyendo','validando','creando_proveedor','creando_orden','confirmando_orden','creando_factura','verificando','contabilizando')").fetchone()[0]
        if active >= 10:
            raise HTTPException(429, 'Hay varias facturas en proceso. Intentá más tarde.')
        if previous:
            db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(data), job_id))
        else:
            db.execute('INSERT INTO jobs VALUES (?,?,?)', (job_id, owner, json.dumps(data)))
    try:
        executor.submit(work, job_id, pdf)
    except Exception:
        save(job_id, state='revision', message='No se pudo iniciar la carga. Consultá al administrador.')
        raise HTTPException(503, 'No se pudo iniciar la carga')
    return data


@router.on_event('startup')
def recover():
    # Single ASGI worker required. Never replay writes after process termination.
    with connect() as db:
        for job_id, encoded in db.execute('SELECT id,data FROM jobs').fetchall():
            data = json.loads(encoded)
            if data['state'] not in ('completado', 'borrador', 'existente', 'revision', 'resultado_incierto','esperando_productos'):
                data.update(state='resultado_incierto', message='El servidor se reinició. Revisá Odoo antes de repetir la carga.')
                db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(data), job_id))

def continuar_productos(job_id):
    if len(job_id)!=64 or any(c not in '0123456789abcdef' for c in job_id):return
    path=directory()/(job_id+'.pdf')
    if not path.is_file():return
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT data FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:return
        data=json.loads(row[0])
        if data.get('state')!='esperando_productos' or data.get('purchase_id') or data.get('move_id'):return
        data.update(state='recibido',message='Equivalencias resueltas. Revalidando el comprobante.')
        db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(data),job_id))
    try:executor.submit(work,job_id,path.read_bytes(),data.get('document'))
    except Exception:save(job_id,state='esperando_productos',message='No se pudo retomar. Reintentá desde Productos.')
