# Local voice activity model

`silero-vad-16k.onnx` is the official `silero_vad_16k_op15.onnx` model from the
PyPI `silero-vad==6.2.3` distribution (MIT, accompanying licence retained).

- Source project: https://github.com/snakers4/silero-vad
- SHA-256: `7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49`
- 16 kHz mono, 512 new samples + 64 context samples per inference.
- Local CPU only, no PyTorch dependency and no runtime model download.

The application wrapper in `app/vad.py` is new. No model or implementation
was imported from the previous AI-call project.
