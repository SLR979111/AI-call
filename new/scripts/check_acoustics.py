"""Synthetic speaker echo/noise/double-talk through production DSP and gate."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.hearing import Hearing
from app.models import TaskCard
from app.providers import Cloud, Settings, wav_bytes


async def main():
    directory=ROOT/'runtime/validation/acoustics'
    directory.mkdir(parents=True,exist_ok=True)
    cloud=Cloud(Settings.load())
    try:
        card=TaskCard.model_validate_json((ROOT/'tasks/jd-qualification.json').read_text(encoding='utf-8'))
        for name,text in [('jd-opening',card.opening),('user-interrupt','等等，我还没听清，请再说一次。')]:
            target=directory.parent/(name+'.pcm')
            if not target.exists():
                target.write_bytes(b''.join([part async for part in cloud.speech(text)]))
        if '--reuse-dsp' not in sys.argv:
            with (directory/'dsp-output.txt').open('w',encoding='utf-8') as output:
                subprocess.run(['node','scripts/check-acoustics.mjs'],cwd=ROOT,check=True,stdout=output)
        cases=json.loads((directory/'dsp.json').read_text(encoding='utf-8'))
        results=[]
        for case in cases:
            events,errors,clips=[],[],[]
            current_ms=0
            class FakeASR:
                async def transcribe(self,pcm):
                    clips.append(pcm)
                    return '合成测试语音'
            async def started(): events.append({'type':'start','at_ms':current_ms,'evidence':dict(hearing.last_onset)})
            async def text(value,at): events.append({'type':'turn','at_ms':at})
            async def error(value): errors.append(value)
            async def noop(*args): pass
            hearing=Hearing(FakeASR(),on_start=started,on_text=text,on_partial=noop,on_error=error)
            try:
                packets=(directory/(case['name']+'.frames')).read_bytes()
                for offset in range(0,len(packets),1316):
                    current_ms=offset//1316*20
                    await hearing.feed(packets[offset:offset+1316])
                    await asyncio.sleep(0)
                await asyncio.sleep(.01)
                onsets=[event['at_ms'] for event in events if event['type']=='start']
                result={**case,'events':events,'errors':errors,'asr_requests':len(clips)}
                if case['expectsSpeech']:
                    result['detection_ms']=onsets[0]-case['firstNearMs'] if onsets else None
                    result['passed']=bool(onsets) and onsets[0]>=case['firstNearMs'] and result['detection_ms']<=800 and not errors
                else:
                    result['passed']=not events and not clips and not errors
                if case['name'] in {'double_talk','near_with_noise'} and clips:
                    result['recognized']=await cloud.transcribe(clips[-1])
                    result['passed'] &= '听清' in result['recognized'] or '再说' in result['recognized']
                    (directory/(case['name']+'-recognized.wav')).write_bytes(wav_bytes(clips[-1]))
                results.append(result)
                print(json.dumps({k:result.get(k) for k in ('name','passed','detection_ms','asr_requests','recognized','events')},ensure_ascii=False),flush=True)
            finally:
                await hearing.close()
        (directory/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        assert all(case['passed'] for case in results),[c['name'] for c in results if not c['passed']]
        print(f'Acoustic checks passed: {len(results)} synthetic scenarios. These do not replace real-room verification.')
    finally:
        await cloud.close()


if __name__=='__main__': asyncio.run(main())
