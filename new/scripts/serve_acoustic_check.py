"""A local-only acceptance harness, launched explicitly on a separate port."""
import json
import time
import app.main as application
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse

from app.main import app

ROOT=Path(__file__).resolve().parent.parent


class CaptureHearing(application.Hearing):
    """Capture only the synthetic fixture input of this dedicated test server."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.frames=bytearray()
        self.capture_path=ROOT/'runtime/validation/acoustics'/f'browser-{time.time_ns()}.frames'

    async def feed(self,packet):
        self.frames.extend(packet)
        await super().feed(packet)

    async def close(self):
        await super().close()
        self.capture_path.write_bytes(self.frames)


application.Hearing=CaptureHearing


@app.get('/acoustic-check')
async def check_page():
    return FileResponse(ROOT/'scripts/browser-acoustic.html')


@app.get('/acoustic-fixture')
async def fixture():
    return FileResponse(ROOT/'runtime/validation/user-interrupt.pcm',media_type='application/octet-stream')


@app.post('/acoustic-result')
async def save_result(request:Request):
    data=await request.json()
    (ROOT/'runtime/validation/acoustics/browser.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'saved':True}
