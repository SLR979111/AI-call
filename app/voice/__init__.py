"""Voice adapter boundary (fake now, sherpa-onnx in W2)."""
from .ort_runtime import preload_onnxruntime

# Must run before any `import sherpa_onnx`; see ort_runtime.py for the ABI issue.
preload_onnxruntime()
