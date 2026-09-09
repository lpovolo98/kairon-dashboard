"""Loopback-only integration fixture. Never imports production credentials."""
import os,tempfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from tests.test_productos import base
from productos import api
import uvicorn
root=Path(__file__).resolve().parents[1]
os.environ.update(PRODUCTOS_DATA_DIR=tempfile.mkdtemp(prefix='productos-e2e-'),ODOO_URL='https://example.invalid',ODOO_DB='fake',ODOO_USER='fake',ODOO_PASSWORD='fake',PRODUCTOS_ALLOWED_EMAILS='')
o,_=base();api._odoo=lambda:o
app=FastAPI()
@app.middleware('http')
async def local_identity(request,call_next):
    request.state.usuario={'email':'test@localhost'}
    return await call_next(request)
app.include_router(api.router)
app.mount('/static',StaticFiles(directory=root/'static'),name='static')
uvicorn.run(app,host='127.0.0.1',port=8767,log_level='error')
