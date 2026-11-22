import {readPhone} from './phone-file.js';

// A raw work order also needs its phone file. Compile it in parallel so a cold
// graph does not delay connection after the opening audio becomes ready.
export async function prepareTask(task, {wait = true, retry = false, phoneFile} = {}) {
  try {
    const audio = fetch('/api/warm', {method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({...(phoneFile ? {phone_file: phoneFile, analysis_task: task} : {task}), wait, retry})});
    const [response] = await Promise.all([audio, phoneFile ? null : readPhone(task)]);
    if (response.ok) {
      const {status} = await response.json();
      if (['preparing', 'ready', 'disabled', 'unconfigured'].includes(status)) return status;
    }
  } catch {
    // The caller can offer an explicit retry after connection loss.
  }
  return 'failed';
}

export function checkPreparation(status) {
  if (status === 'unconfigured') throw new Error('云端模式需要配置 API 凭据。');
  if (status !== 'ready' && status !== 'disabled') throw new Error('工单准备未完成，点击开始可重试。');
}
