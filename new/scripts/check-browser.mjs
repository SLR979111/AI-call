import {chromium} from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const out=path.resolve('runtime/validation');
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true,args:['--autoplay-policy=no-user-gesture-required']});
const errors=[],frames=[],report={};
try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',error=>errors.push(error.message));
  page.on('websocket',ws=>ws.on('framereceived',frame=>{
    if(typeof frame.payload==='string') {
      try{frames.push({...JSON.parse(frame.payload),observed:Date.now()});}catch{}
    } else frames.push({type:'pcm',bytes:frame.payload.length,observed:Date.now()});
  }));
  await page.goto('http://127.0.0.1:8088');
  await page.getByText('服务就绪',{exact:true}).waitFor();
  await page.screenshot({path:path.join(out,'desktop.png'),fullPage:true});
  await page.getByRole('button',{name:'文字试聊',exact:true}).click();
  await page.waitForFunction(()=>document.getElementById('phase').textContent.includes('在听'));
  await page.getByRole('textbox',{name:'输入一句话',exact:true}).fill('我现在在开会，不方便沟通。');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await page.getByText('已尊重对方意愿，暂停沟通',{exact:true}).waitFor({timeout:30000});
  report.text_paused=true;
  assert.equal(await page.locator('#error').isVisible(),false);
  await page.screenshot({path:path.join(out,'text-dialogue.png'),fullPage:true});
  await page.getByRole('button',{name:'结束对话',exact:true}).click();

  // Use a generated MediaStream as microphone input. This exercises the new
  // AudioWorklet, resampler, PCM transport and ASR without recording the user.
  await page.addInitScript(()=>{
    navigator.mediaDevices.getUserMedia=async()=>{
      const ctx=new AudioContext(); await ctx.resume();
      const destination=ctx.createMediaStreamDestination();
      window.feedTestSpeech=async base64=>{
        const bytes=Uint8Array.from(atob(base64),char=>char.charCodeAt(0));
        const view=new DataView(bytes.buffer), buffer=ctx.createBuffer(1,bytes.length/2,24000);
        const values=buffer.getChannelData(0);
        for(let i=0;i<values.length;i++)values[i]=view.getInt16(i*2,true)/32768;
        const source=ctx.createBufferSource(); source.buffer=buffer; source.connect(destination);
        source.start(); return buffer.duration;
      };
      return destination.stream;
    };
  });
  await page.reload();
  await page.getByText('服务就绪',{exact:true}).waitFor();
  await page.locator('#diagnostics').evaluate(element=>element.open=true);
  await page.locator('#prediction').check();
  await page.locator('#diagnostics').evaluate(element=>element.open=false);
  const voiceStart=frames.length;
  await page.getByRole('button',{name:'开始通话',exact:true}).click();
  await page.waitForFunction(()=>document.getElementById('phase').textContent.includes('在听'),{},{timeout:30000});
  report.opening_pcm=frames.slice(voiceStart).filter(e=>e.type==='pcm').length;
  assert.ok(report.opening_pcm>10);
  const first=(await fs.readFile(path.join(out,'user-question.pcm'))).toString('base64');
  const second=(await fs.readFile(path.join(out,'user-interrupt.pcm'))).toString('base64');
  await page.evaluate(data=>window.feedTestSpeech(data),first);
  await page.waitForFunction(()=>document.querySelectorAll('.message.user').length>=1,{},{timeout:30000});
  await page.waitForFunction(()=>document.getElementById('latency').textContent.includes('接话'),{},{timeout:30000});
  await page.evaluate(data=>window.feedTestSpeech(data),second);
  await page.waitForFunction(()=>document.querySelectorAll('.message.user').length>=2,{},{timeout:30000});
  await page.waitForFunction(()=>document.getElementById('phase').textContent.includes('在听'),{},{timeout:30000});
  const voiceFrames=frames.slice(voiceStart);
  report.transcripts=voiceFrames.filter(e=>e.type==='transcript'&&e.final).map(e=>e.text);
  report.cancellations=voiceFrames.filter(e=>e.type==='cancel');
  report.voice_latencies=voiceFrames.filter(e=>e.type==='latency');
  report.voice_sources=voiceFrames.filter(e=>e.type==='response_source');
  assert.equal(report.transcripts.length,2);
  assert.ok(report.transcripts[0].includes('补'));
  assert.ok(report.transcripts[1].includes('听清')||report.transcripts[1].includes('再说'));
  assert.ok(report.cancellations.some(e=>e.reason==='client_speech'||e.reason==='speech'));
  assert.equal(await page.locator('#error').isVisible(),false);
  await page.screenshot({path:path.join(out,'voice-dialogue.png'),fullPage:true});
  await page.getByRole('button',{name:'结束对话',exact:true}).click();
  const mobile=await browser.newPage({viewport:{width:390,height:844},isMobile:true,deviceScaleFactor:1});
  mobile.on('pageerror',error=>errors.push(error.message));
  await mobile.goto('http://127.0.0.1:8088');
  await mobile.getByText('服务就绪',{exact:true}).waitFor();
  assert.equal(await mobile.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await mobile.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
  await mobile.getByRole('button',{name:'编辑任务 ↗',exact:true}).click();
  await mobile.locator('#editTitle').fill('下一本读什么');
  await mobile.locator('#editGoal').fill('了解阅读偏好，确认一本想读的书。');
  await mobile.getByRole('button',{name:'保存任务',exact:true}).click();
  await mobile.reload();
  await mobile.getByRole('heading',{name:'下一本读什么',exact:true}).waitFor();
  report.task_editor_persists=true;
  assert.deepEqual(errors,[]);
  report.errors=errors;report.passed=true;
} finally {
  await fs.writeFile(path.join(out,'browser.json'),JSON.stringify(report,null,2));
  await fs.writeFile(path.join(out,'browser-events.json'),JSON.stringify(frames,null,2));
  await browser.close();
}
console.log(JSON.stringify(report,null,2));
