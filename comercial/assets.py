"""Persistent, content-addressed product photography; never writes to Odoo."""
import hashlib,io,json,os,sqlite3,uuid
from datetime import datetime,timezone
from pathlib import Path
from PIL import Image,ImageOps

def db(root):
    root=Path(root)/'media';root.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(root/'index.sqlite',timeout=30)
    con.execute('CREATE TABLE IF NOT EXISTS assets (product INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    return root,con

def index(root):
    _,con=db(root)
    try:return {r[0]:json.loads(r[1]) for r in con.execute('SELECT product,data FROM assets')}
    finally:con.close()

def put(root,pid,raw,source,owner):
    if not 0<len(raw)<=8*1024*1024:raise ValueError('La foto debe pesar menos de 8 MB')
    im=Image.open(io.BytesIO(raw))
    if im.format not in ('PNG','JPEG','WEBP'):raise ValueError('Usá una imagen JPG, PNG o WebP')
    if im.width*im.height>25000000 or getattr(im,'n_frames',1)!=1:raise ValueError('Imagen demasiado grande o animada')
    im=ImageOps.exif_transpose(im).convert('RGBA');original_size=list(im.size)
    if min(im.size)<100:raise ValueError('La imagen es demasiado pequeña; mínimo 100 píxeles por lado')
    im.thumbnail((840,840),Image.Resampling.LANCZOS)
    canvas=Image.new('RGB',(1000,1000),'white');canvas.paste(im,((1000-im.width)//2,(1000-im.height)//2),im)
    buf=io.BytesIO();canvas.save(buf,format='PNG');normalized=buf.getvalue()
    digest=hashlib.sha256(raw).hexdigest();root,con=db(root)
    try:
        for suffix,data in [('original',raw),('png',normalized)]:
            path=root/(digest+'.'+suffix)
            if not path.exists():
                tmp=root/(uuid.uuid4().hex+'.tmp');tmp.write_bytes(data);os.replace(tmp,path)
        row={'id':pid,'hash':digest,'source':source,'updated_at':datetime.now(timezone.utc).isoformat(),
             'updated_by':owner,'size':original_size,'low_resolution':min(original_size)<500}
        with con:con.execute('INSERT OR REPLACE INTO assets VALUES (?,?)',(pid,json.dumps(row)))
        return row
    finally:con.close()

def path(root,record):return Path(root)/'media'/(record['hash']+'.png')
