"""Deterministic promotional layouts: real packshots, exact price text."""
from pathlib import Path
from io import BytesIO
from xml.sax.saxutils import escape
import base64,zipfile
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor,white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph,Spacer,KeepTogether,Image as Photo
from reportlab.lib.styles import ParagraphStyle
from .pricing import discounted

ROOT=Path(__file__).resolve().parents[1]
INK='#102a33';TEAL='#0ea7b9';YELLOW='#f8d448'
def cash(v):return '$ '+f'{v:,.2f}'.replace(',','X').replace('.',',').replace('X','.')
def paragraph(text,size=10,color=INK,bold=False):
    return Paragraph(escape(str(text)).replace('\n','<br/>'),ParagraphStyle('p',fontName='Helvetica-Bold' if bold else 'Helvetica',fontSize=size,leading=size*1.28,textColor=HexColor(color)))
def draw_text(c,text,x,y,w,size=12,color=INK,bold=False):
    p=paragraph(text,size,color,bold);_,h=p.wrap(w,700);p.drawOn(c,x,y-h);return h
def header(c,w,h,title,period,draft=True):
    c.setFillColor(HexColor(INK));c.rect(0,h-68,w,68,fill=1,stroke=0)
    c.drawImage(str(ROOT/'static/kairon_logo.png'),26,h-52,30,30,mask='auto')
    draw_text(c,title,68,h-22,w-96,13,'#ffffff',True)
    draw_text(c,period,68,h-43,w-96,8,'#80d6df')
    c.setFillColor(HexColor(TEAL));c.rect(0,h-72,w,4,fill=1,stroke=0)
    c.setFillColor(HexColor(INK));c.setFont('Helvetica',7)
    c.drawString(26,20,'KAIRON DISTRIBUCIONES | Canal Proximidad | Precios finales por unidad')
    if draft:c.drawRightString(w-26,10,'BORRADOR - REVISAR ANTES DE COMPARTIR')

def tier_label(a,t):
    unit={'units':'unidades','boxes':'cajas','packs':'packs'}[a.get('unit','units')]
    label=f"Desde {t['min']} {unit}"
    if a.get('mixed') and len(a['products'])>1:label+=' surtidas'
    if a.get('unit')=='packs':label+=f" de {a['pack_units']} unidades"
    return label

def catalog(path,campaign,products,draft=True,images=None):
    images=images or {}
    doc=SimpleDocTemplate(str(path),pagesize=A4,rightMargin=25,leftMargin=25,topMargin=88,bottomMargin=42)
    story=[];actions={pid:a for a in campaign['actions'] for pid in a['products']};groups={}
    for p in products:groups.setdefault(p['category'],[]).append(p)
    story.append(paragraph('LISTA DE PRECIOS · CANAL PROXIMIDAD',15,bold=True));story.append(Spacer(1,8))
    story.append(paragraph(f"Vigencia {campaign['start']} al {campaign['end']}. IVA e impuestos de venta configurados en Odoo incluidos.",8));story.append(Spacer(1,16))
    for category,rows in groups.items():
        heading=paragraph(category.upper(),11,TEAL,True)
        heading.keepWithNext=True
        heading.spaceAfter=6
        story.append(heading)
        data=[['',paragraph('PRODUCTO',8,'#ffffff',True),paragraph('BULTO',8,'#ffffff',True),paragraph('PRECIO UNIT.',8,'#ffffff',True)]]
        for p in rows:
            text=p['name']+'\nCódigo: '+p['sku']
            a=actions.get(p['id'])
            if a:
                text+='\nACCIÓN: '+('; '.join(tier_label(a,t)+' - '+str(t['discount'])+'%' for t in a['tiers']) if a.get('kind','discount')=='discount' else a['title']+' (ver condiciones en carpeta)')
            pack=str(p['pack'])+(f" ({p['divisor']})" if p.get('divisor',1)>1 else '')
            photo=Photo(str(images[p['id']]),width=48,height=48) if p['id'] in images else paragraph('FOTO\nPENDIENTE',6)
            data.append([photo,paragraph(text,8),paragraph(pack,8),paragraph(cash(p['price']),9,bold=True)])
        table=Table(data,colWidths=[60,320,55,110],repeatRows=1,hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),HexColor(INK)),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,HexColor('#f0f6f7')]),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,-1),.3,HexColor('#dce9eb'))]))
        story.extend([table,Spacer(1,15)])
    story.append(paragraph('Precios sujetos a modificación. Sujeto a disponibilidad de stock. Las escalas no son acumulables y aplican sobre el precio de lista. Las condiciones específicas figuran en la carpeta de acciones.',8))
    def page(c,d):header(c,*A4,'Lista de precios',campaign['name'],draft)
    doc.build(story,onFirstPage=page,onLaterPages=page)

def action_pages(path,campaign,products,images,draft=True,backgrounds=None):
    backgrounds=backgrounds or {}
    byid={p['id']:p for p in products};w,h=A4;c=canvas.Canvas(str(path),pagesize=A4)
    header(c,w,h,'Carpeta de acciones comerciales',campaign['name'],draft)
    draw_text(c,'Más oportunidades.\nMejores ventas.',30,h-115,w-60,38,INK,True)
    draw_text(c,f"CANAL PROXIMIDAD\nVigencia: {campaign['start']} al {campaign['end']}",32,h-235,w-64,12,TEAL,True)
    y=h-320
    for i,a in enumerate(campaign['actions']):
        if y<90:c.showPage();header(c,w,h,'Índice de acciones',campaign['name'],draft);y=h-105
        c.setFillColor(HexColor('#eff7f8'));c.roundRect(28,y-45,w-56,46,6,fill=1,stroke=0)
        draw_text(c,f'{i+1:02d}',40,y-10,30,12,TEAL,True);draw_text(c,a['title'],85,y-10,w-130,12,INK,True);y-=58
    if not campaign['actions']:draw_text(c,'Sin acciones comerciales definidas para esta edición.',30,y,w-60,13)
    first_piece_page=c.getPageNumber()
    c.showPage()
    for action_index,a in enumerate(campaign['actions']):
        ps=[byid[i] for i in a['products']]
        # Four packshots per page; every selected SKU gets its own exact price.
        for offset in range(0,len(ps),4):
            subset=ps[offset:offset+4]
            background=backgrounds.get(action_index)
            if background:c.drawImage(str(background),0,0,w,h)
            header(c,w,h,'Acciones comerciales',campaign['name'],draft)
            if len(ps)==1 and a.get('kind','discount')=='discount':
                p=ps[0];accent='#dd731e' if 'wakas' in p['name'].lower() else TEAL
                if not background:
                    c.setFillColor(HexColor('#f4f1e8'));c.rect(0,105,w,h-177,fill=1,stroke=0)
                else:
                    c.setFillColor(white);c.roundRect(285,310,285,300,10,fill=1,stroke=0)
                c.setFillColor(HexColor(accent));c.rect(0,h-160,w,88,fill=1,stroke=0)
                draw_text(c,a['title'],28,h-90,w-56,28,'#ffffff',True)
                draw_text(c,'EL SURTIDO QUE TUS CLIENTES ELIGEN',30,h-184,w-60,11,accent,True)
                photo=images.get(p['id'])
                if photo:c.drawImage(ImageReader(str(photo)),35,310,235,305,preserveAspectRatio=True,anchor='c',mask='auto')
                else:draw_text(c,'Imagen pendiente',45,480,200,14)
                draw_text(c,p['name'],300,h-245,260,22,INK,True)
                draw_text(c,'PRECIO DE LISTA POR UNIDAD',300,440,260,9,accent,True)
                draw_text(c,cash(p['price']),300,421,260,34,INK,True)
                draw_text(c,f"Caja de {p['pack']} unidades"+(f"\nDisplay de {p['divisor']} unidades" if p.get('divisor',1)>1 else ''),300,365,240,12)
                tiers=a['tiers'];cw=(w-56-10*(len(tiers)-1))/len(tiers)
                for j,t in enumerate(tiers):
                    x=28+j*(cw+10);c.setFillColor(HexColor(INK));c.roundRect(x,160,cw,128,8,fill=1,stroke=0)
                    draw_text(c,tier_label(a,t),x+12,275,cw-24,10,'#ffffff',True)
                    draw_text(c,str(t['discount'])+'% OFF',x+12,243,cw-24,20 if len(tiers)==4 else 26,YELLOW,True)
                    draw_text(c,cash(discounted(p['price'],t['discount'])),x+12,207,cw-24,16 if len(tiers)==4 else 22,'#ffffff',True)
                    draw_text(c,'PRECIO POR UNIDAD',x+12,175,cw-24,7,'#a2c7ce')
                draw_text(c,a.get('conditions','') or 'Descuentos no acumulables. Sujeto a disponibilidad de stock.',30,93,w-60,9)
                draw_text(c,f"Vigencia {campaign['start']} al {campaign['end']}. IVA incluido.",30,43,w-60,8)
                c.showPage();continue
            draw_text(c,a['title'],28,h-94,w-56,26,INK,True)
            desc='ELEGÍ TUS VARIEDADES' if a.get('mixed') else 'PROMOCIÓN DEL MES'
            draw_text(c,desc,30,h-165,w-60,10,TEAL,True)
            cw=(w-70)/len(subset);top=h-200
            for i,p in enumerate(subset):
                x=28+i*(cw+5)
                c.setFillColor(HexColor('#eef6f7'));c.roundRect(x,top-235,cw,230,10,fill=1,stroke=0)
                photo=images.get(p['id'])
                if photo:
                    c.drawImage(ImageReader(str(photo)),x+8,top-155,cw-16,145,preserveAspectRatio=True,anchor='c',mask='auto')
                else:draw_text(c,'Imagen no disponible',x+8,top-65,cw-16,10,'#70868a')
                draw_text(c,p['name'],x+8,top-161,cw-16,8,INK,True)
                draw_text(c,'Caja: '+str(p['pack'])+' u.'+(f" / Display: {p['divisor']} u." if p.get('divisor',1)>1 else '')+'\nLista por unidad: '+cash(p['price']),x+8,top-204,cw-16,8)
            y=top-255
            if a.get('kind','discount')=='discount':
                for t in a['tiers']:
                    c.setFillColor(HexColor(YELLOW));c.roundRect(28,y-49,w-56,48,5,fill=1,stroke=0)
                    draw_text(c,str(t['discount'])+'% OFF',40,y-12,110,20,INK,True)
                    draw_text(c,tier_label(a,t),170,y-8,w-215,10,INK,True)
                    prices=' | '.join(p['sku']+': '+cash(discounted(p['price'],t['discount'])) for p in subset)
                    draw_text(c,prices,170,y-25,w-215,8);y-=57
            else:
                lines=[];total=0
                for p in ps:
                    q=a.get('quantities',{}).get(str(p['id']),0);gift=a.get('gifts',{}).get(str(p['id']),0)
                    if q:total+=q*p['price'];lines.append(f'{p["sku"]}: {q} unidades - {cash(q*p["price"])}')
                    if gift:lines.append(f'{p["sku"]}: {gift} unidades SIN CARGO')
                draw_text(c,'TOTAL COMBO: '+cash(total),30,y,w-60,20,TEAL,True);y-=36
                used=draw_text(c,'\n'.join(lines),30,y,w-60,9);y-=used+15
            condition=a.get('conditions','') or 'Sujeto a disponibilidad de stock. Descuentos no acumulables.'
            draw_text(c,condition,30,min(y-8,95),w-60,8)
            draw_text(c,f"Vigencia {campaign['start']} al {campaign['end']}. Precios finales por unidad, IVA incluido.",30,43,w-60,7)
            c.showPage()
    c.save()
    return first_piece_page

def generate(folder,campaign,snapshot,images,draft=True,backgrounds=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    catalog(folder/'catalogo.pdf',campaign,snapshot['products'],draft,images)
    first_piece_page=action_pages(folder/'acciones.pdf',campaign,snapshot['products'],images,draft,backgrounds)
    from pypdf import PdfReader,PdfWriter
    writer=PdfWriter()
    for name in ('acciones.pdf','catalogo.pdf'):writer.append(PdfReader(folder/name))
    with open(folder/'carpeta-completa.pdf','wb') as out:writer.write(out)
    import pypdfium2 as pdfium
    pdf=pdfium.PdfDocument(str(folder/'acciones.pdf'));files=[]
    for i in range(first_piece_page,len(pdf)):
        image=pdf[i].render(scale=2).to_pil();name=f'pieza-{i-first_piece_page+1:02d}.png';image.save(folder/name);files.append(name)
    pdf.close()
    with zipfile.ZipFile(folder/'piezas.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(folder/name,name)
    return ['catalogo.pdf','acciones.pdf','carpeta-completa.pdf','piezas.zip']+files
