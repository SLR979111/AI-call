// The server owns the single round, including when several pages request it.
export async function prepareTask(task, {wait = true, retry = false, phoneFile} = {}) {
  try {
    const response = await fetch('/api/warm', {method: 'POST',
      headers: {'content-type': 'application/json'}, body: JSON.stringify({...(phoneFile ? {phone_file: phoneFile} : {task}), wait, retry})});
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
