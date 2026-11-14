# Start only the experimental 8087 service. Never starts 8081/8091.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# Optional local file. It is ignored by git; if the user chooses to create it,
# values are loaded here. The agent itself never writes this secret file.
$localEnv = Join-Path $root '.env.8087'
if (Test-Path $localEnv) {
    Get-Content $localEnv | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2].Trim()
        }
    }
}

if (-not $env:ARK_KEY -or -not $env:VOLC_KEY) {
    Write-Host 'Missing ARK_KEY / VOLC_KEY in the current user environment.' -ForegroundColor Yellow
    Write-Host 'Set them once, then reopen the terminal:'
    Write-Host '  setx ARK_KEY "your-ark-key"'
    Write-Host '  setx VOLC_KEY "your-volc-key"'
    exit 1
}

$env:PYTHONPATH = "H:\aicall;H:\pytorch-env\Lib\site-packages;H:\aicall\.livekit311;H:\aicall\.web311"
$env:ORT_DLL_DIR = "H:\aicall\.web311\onnxruntime\capi"
$env:APP_ENV = "dev"
$env:DEV_NO_AUTH = "1"
$env:SCENARIO_ID = "pop_qualification_audit"
$env:AGENT_PROVIDER = "llm"
$env:LLM_PRIMARY_NAME = "volcengine"
$env:LLM_PRIMARY_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
$env:LLM_PRIMARY_API_KEY = $env:ARK_KEY
$env:LLM_PRIMARY_MODEL = "doubao-seed-2-1-lite-260915"
$env:LLM_PRIMARY_THINKING = "disabled"
$env:LLM_TIMEOUT_MS = "5000"
$env:LLM_STREAM_MODE = "reply"
$env:LLM_PLAIN_REPLY = "1"
$env:LLM_TOOL_ALLOWLIST = "dynamic"
$env:LLM_MAX_TOKENS = "160"
$env:LLM_PLAIN_MAX_TOKENS = "96"
$env:LLM_MAX_REPLY_CHARS = "50"
$env:PREDICTIVE_DIALOGUE = "1"
$env:ASR_PROVIDER = "volc"
$env:VOLC_ASR_API_KEY = $env:VOLC_KEY
$env:VOLC_ASR_RESOURCE_ID = "volc.seedasr.sauc.duration"
$env:VOLC_ASR_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
$env:VOLC_ASR_END_WINDOW_MS = "800"
$env:TTS_PROVIDER = "hybrid"
$env:VOLC_TTS_API_KEY = $env:VOLC_KEY
$env:VOLC_TTS_RESOURCE_ID = "seed-tts-2.0"
$env:VOLC_TTS_SPEAKER = "zh_female_vv_uranus_bigtts"
$env:VOLC_TTS_BIDIRECTIONAL_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
$env:HYBRID_FILLER_DIR = "H:\aicall\data\voice\fillers-volc"
$env:LIVEKIT_URL = "ws://127.0.0.1:7880"
$env:LIVEKIT_API_KEY = "devkey"
$env:LIVEKIT_API_SECRET = "secret"
$env:LIVEKIT_BRIDGE = "1"
$env:POP_CONTEST_CONTEXT = '{"request_id":"Q983001","brand":"\u6f14\u793a\u677e\u6f6e\u751f\u6d3b","reject_l1":"\u5165\u9a7b","reject_l2":"\u516c\u53f8\u4e3b\u4f53","reject_l3":"\u4e3b\u4f53\u8d44\u8d28\u5230\u671f","doc_id":"pop-scene-034"}'
$env:DATA_DIR = "H:\aicall\data\ark-manual"

Set-Location $root
Write-Host 'Starting experimental 8087 only: http://127.0.0.1:8087/rtc/?transport=livekit'
& 'H:\pytorch-env\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8087
