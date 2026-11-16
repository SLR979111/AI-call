import fs from 'node:fs/promises';
import path from 'node:path';

const source = path.resolve('node_modules/@ai-ecoverse/wasm-webrtc-audio-processing');
const target = path.resolve('web/vendor/webrtc-audio-processing');
await fs.mkdir(target, {recursive:true});
for (const file of ['index.js', 'LICENSE', 'THIRD-PARTY-NOTICES.md']) {
  await fs.copyFile(path.join(source,file), path.join(target,file));
}
await fs.copyFile(path.join(source,'dist/webrtc-audio-processing.wasm'), path.join(target,'webrtc-audio-processing.wasm'));
console.log('Vendored the pinned WebRTC AEC3 runtime and licences.');
