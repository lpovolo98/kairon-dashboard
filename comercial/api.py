import os,json,sqlite3,uuid,threading,base64,io,shutil
from pathlib import Path
from datetime import datetime,timezone,date
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from PIL import Image
from productos.api import RutaLimitada,_odoo
from . import pricing,assets,creative

ROOT=Path(__file__).resolve().parents[1]
router=APIRouter(route_class=RutaLimitada);executor=ThreadPoolExecutor(max_workers=1);lock=threading.Lock()
def directory():
    p=Path(os.getenv('COMERCIAL_DATA_DIR','/data/comercial' if Path('/data').is_dir() else str(ROOT/'comercial-data')));p.mkdir(parents=True,exist_ok=True);return p
def identity(request:Request):
    who=getattr(request.state,'usuario',{}) or {};email=who.get('email','').strip().lower()
    if not email:raise HTTPException(401,'Ingresá por el portal de Kairon')
    return email
def editor(email=Depends(identity)):
    allowed=[s.strip().lower() for s in os.getenv('COMERCIAL_ALLOWED_EMAILS',os.getenv('PRODUCTOS_ALLOWED_EMAILS','')).split(',') if s.strip()]
    if allowed and email not in allowed:raise HTTPException(403,'Tu cuenta solo puede consultar materiales aprobados')
    return email
@contextmanager
def connection():
    db=sqlite3.connect(directory()/'comercial.sqlite',timeout=20)
    db.execute('CREATE TABLE IF NOT EXISTS editions (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS settings (owner TEXT PRIMARY KEY, data TEXT NOT NULL)')
    try:
        with db:yield db
    finally:db.close()
def settings(owner):
    with connection() as db:r=db.execute('SELECT data FROM settings WHERE owner=?',(owner,)).fetchone()
    return json.loads(r[0]) if r else {'divisors':{'158':10,'149':10,'160':10,'159':10,'161':16,'201':16,'200':12,'198':5,'195':5,'196':5,'194':5,'197':5,'199':16,'202':16},'confirmed':False}
@router.get('/api/comercial/equivalencias')
def get_settings(owner=Depends(editor)):return settings(owner)
@router.post('/api/comercial/equivalencias')
def save_settings(data:dict,owner=Depends(editor)):
    divisors=data.get('divisors',{})
    if not isinstance(divisors,dict) or len(divisors)>5000:raise HTTPException(422,'Equivalencias inválidas')
    for k,v in divisors.items():
        if not str(k).isdigit() or isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=1000:raise HTTPException(422,'Equivalencias: IDs y unidades enteras positivas')
    value={'divisors':divisors,'confirmed':data.get('confirmed') is True}
    with connection() as db:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(owner,json.dumps(value)))
    return value
def save(row):
    with connection() as db:db.execute('INSERT OR REPLACE INTO editions VALUES (?,?,?)',(row['id'],row['owner'],json.dumps(row,ensure_ascii=False)))
def get(id,owner=None,approved=False):
    with connection() as db:r=db.execute('SELECT data FROM editions WHERE id=?',(id,)).fetchone()
    if not r:raise HTTPException(404,'Edición inexistente')
    row=json.loads(r[0])
    if owner and row['owner']!=owner and not(approved and row['state']=='approved'):raise HTTPException(404,'Edición inexistente')
    return row
def rows(owner):
    with connection() as db:return [json.loads(r[0]) for r in db.execute('SELECT data FROM editions WHERE owner=?',(owner,))]
def event(row,text):row.setdefault('log',[]).append({'at':datetime.now(timezone.utc).isoformat(),'message':text})
def public(row):return {k:v for k,v in row.items() if k!='owner'}
def readonly():
    from productos.cargar import SoloLectura
    return SoloLectura(_odoo())

@router.get('/api/comercial/imagenes')
def media_list(owner=Depends(editor)):
    return {'images':list(assets.index(directory()).values()),'ai_configured':creative.configured()}

@router.get('/api/comercial/imagenes/{pid}')
def media_image(pid:int,owner=Depends(editor)):
    row=assets.index(directory()).get(pid)
    if not row:raise HTTPException(404,'Imagen pendiente')
    return FileResponse(assets.path(directory(),row),media_type='image/png',headers={'Cache-Control':'private, no-store'})

@router.post('/api/comercial/imagenes/{pid}')
async def media_upload(pid:int,request:Request,owner=Depends(editor)):
    if pid<1 or not readonly().uno('product.product',[['id','=',pid]],['name']):raise HTTPException(404,'Producto inexistente')
    try:return assets.put(directory(),pid,await request.body(),'manual',owner)
    except (ValueError,OSError) as e:raise HTTPException(422,str(e))

class PhotoSync(BaseModel):
    ids:list[int]=Field(min_length=1,max_length=100)

@router.post('/api/comercial/imagenes-sincronizar')
def media_sync(data:PhotoSync,owner=Depends(editor)):
    current=assets.index(directory());missing=[];count=0
    for p in readonly().call('product.product','read',[list(set(data.ids))],{'fields':['image_1024']}):
        if current.get(p['id'],{}).get('source')=='manual':continue
        if not p.get('image_1024'):missing.append(p['id']);continue
        try:assets.put(directory(),p['id'],base64.b64decode(p['image_1024'],validate=True),'odoo',owner);count+=1
        except (ValueError,OSError):missing.append(p['id'])
    return {'updated':count,'missing':missing}

@router.get('/comercial')
def page():return FileResponse(ROOT/'static/comercial.html',headers={'Cache-Control':'no-store'})
@router.get('/api/comercial/catalogo')
def catalogo(fecha:str= '',owner=Depends(editor)):
    try:return pricing.snapshot(readonly(),fecha or date.today().isoformat(),settings(owner)['divisors'])
    except (ValueError,KeyError) as e:raise HTTPException(422,str(e))
@router.get('/api/comercial/ediciones')
def list_editions(owner=Depends(editor)):
    return [public(r) for r in sorted(rows(owner),key=lambda r:r['created_at'],reverse=True)[:60]]
@router.get('/api/comercial/publicados')
def published(owner=Depends(identity)):
    with connection() as db:allrows=[json.loads(r[0]) for r in db.execute('SELECT data FROM editions')]
    return [public(r) for r in allrows if r['state']=='approved']

class Campaign(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    start:str
    end:str
    actions:list[dict]=Field(default_factory=list,max_length=30)
    snapshot_hash:str
    accept_exclusions:bool=False
    ai_design:bool=False

@router.post('/api/comercial/ediciones')
def create(data:Campaign,owner=Depends(editor)):
    with lock:
        if data.ai_design and not creative.configured():raise HTTPException(422,'Falta configurar OpenAI para generar diseños con IA')
        if any(r['state'] in ('queued','generating','approving') for r in rows(owner)):raise HTTPException(409,'Esperá que termine la edición en curso')
        try:snap=pricing.snapshot(readonly(),data.start,settings(owner)['divisors'])
        except (ValueError,KeyError) as e:raise HTTPException(422,str(e))
        if snap['hash']!=data.snapshot_hash:raise HTTPException(409,'Los precios cambiaron. Actualizá el catálogo y revisá antes de generar.')
        if snap['issues'] and not data.accept_exclusions:raise HTTPException(422,'Revisá los productos excluidos y aceptá la exclusión explícitamente')
        if not snap['products']:raise HTTPException(422,'No hay productos válidos')
        campaign=data.model_dump();campaign.pop('snapshot_hash');campaign.pop('accept_exclusions')
        try:pricing.validate_campaign(campaign,snap['products'])
        except (ValueError,KeyError,TypeError) as e:raise HTTPException(422,str(e))
        previous=sorted([r for r in rows(owner) if r['state']=='approved'],key=lambda r:r['created_at'],reverse=True)
        changed=not previous or previous[0]['snapshot']['hash']!=snap['hash']
        row={'id':uuid.uuid4().hex,'owner':owner,'created_at':datetime.now(timezone.utc).isoformat(),'state':'queued','campaign':campaign,'snapshot':snap,'catalog_changed':changed,'files':[],'log':[]}
        event(row,'Solicitud recibida. Lista Canal Proximidad; precios finales con impuestos.');event(row,f"{len(snap['products'])} productos válidos; {len(snap['issues'])} excluidos con aceptación explícita." if snap['issues'] else f"{len(snap['products'])} productos válidos, sin exclusiones.")
        event(row,'El catálogo cambió respecto de la última aprobación.' if changed else 'Precios sin cambios respecto de la última aprobación; se conserva una copia de referencia en la edición.')
        save(row);executor.submit(work,row['id']);return {'id':row['id']}

def work(id):
    row=get(id);folder=directory()/id;folder.mkdir(exist_ok=True)
    try:
        row['state']='generating';event(row,'Preparando fotografías reales desde Odoo.');save(row)
        ids=[p['id'] for p in row['snapshot']['products']];images={};missing=[]
        repository=assets.index(directory())
        absent=[pid for pid in ids if pid not in repository]
        for offset in range(0,len(absent),50):media_sync(PhotoSync(ids=absent[offset:offset+50]),row['owner'])
        repository=assets.index(directory())
        for pid in ids:
            if pid in repository:
                path=folder/(str(pid)+'.png');shutil.copyfile(assets.path(directory(),repository[pid]),path);images[pid]=path
            else:missing.append(pid)
        row['image_versions']={str(pid):repository[pid]['hash'] for pid in ids if pid in repository}
        row['missing_images']=missing
        event(row,'Imágenes preparadas. '+(str(len(missing))+' productos sin imagen; deben completarse antes de aprobar.' if missing else 'Todas las imágenes disponibles.'));save(row)
        from .documents import generate
        backgrounds={}
        if row['campaign'].get('ai_design'):
            for i,action in enumerate(row['campaign']['actions']):
                event(row,f'Generando diseño promocional {i+1} con OpenAI.');save(row)
                path=folder/f'art-{i}.png'
                metadata=creative.generate_background(action,[p for p in row['snapshot']['products'] if p['id'] in action['products']],path)
                (folder/f'art-{i}.json').write_text(json.dumps(metadata),encoding='utf-8');backgrounds[i]=path
        row['files']=generate(folder/'draft',row['campaign'],row['snapshot'],images,True,backgrounds)
        row['state']='review';event(row,'PDF de catálogo, carpeta y piezas generados. Pendiente de revisión y aprobación.');save(row)
    except Exception as e:
        row['state']='error';event(row,'No se pudo generar: '+str(e));save(row)

@router.post('/api/comercial/ediciones/{id}/aprobar')
def approve(id:str,owner=Depends(editor)):
    with lock:
        row=get(id,owner)
        if row['state']!='review':raise HTTPException(409,'La edición no está lista para aprobar')
        if row.get('missing_images'):raise HTTPException(422,'Completá las fotos en el repositorio y generá una nueva edición')
        if not settings(owner)['confirmed']:raise HTTPException(422,'Revisá y confirmá las equivalencias de unidad/display antes de aprobar')
        try:current=pricing.snapshot(readonly(),row['campaign']['start'],settings(owner)['divisors'])
        except (ValueError,KeyError) as e:raise HTTPException(422,str(e))
        if current['hash']!=row['snapshot']['hash']:raise HTTPException(409,'Cambió el catálogo en Odoo. Generá y revisá una nueva edición.')
        if date.fromisoformat(row['campaign']['end'])<date.today():raise HTTPException(422,'La vigencia ya terminó')
        row['state']='approving';event(row,'Aprobación solicitada por '+owner);save(row);executor.submit(publish,id);return {'id':id}
def publish(id):
    row=get(id);folder=directory()/id
    try:
        from .documents import generate
        images={int(p.stem):p for p in folder.glob('*.png') if p.stem.isdigit()}
        backgrounds={int(p.stem.split('-')[1]):p for p in folder.glob('art-*.png')}
        row['files']=generate(folder/'approved',row['campaign'],row['snapshot'],images,False,backgrounds)
        row['state']='approved';row['approved_at']=datetime.now(timezone.utc).isoformat();event(row,'Edición aprobada y disponible en la biblioteca del portal.');save(row)
    except Exception as e:row['state']='review';event(row,'No se pudo publicar: '+str(e));save(row)
@router.get('/api/comercial/ediciones/{id}/archivo/{name}')
def download(id:str,name:str,owner=Depends(identity)):
    row=get(id,owner,approved=True)
    if name not in row['files']:raise HTTPException(404,'Archivo inexistente')
    phase='approved' if row['state']=='approved' else 'draft'
    path=directory()/row['id']/phase/name
    return FileResponse(path,filename=name,headers={'Cache-Control':'private, no-store'})

def recover():
    with connection() as db:allrows=[json.loads(r[0]) for r in db.execute('SELECT data FROM editions')]
    for row in allrows:
        if row['state'] in ('queued','generating','approving'):
            row['state']='error';event(row,'El servicio se reinició antes de terminar. Generá una nueva edición.');save(row)
