"""Small local neural speech detector; one recurrent state per microphone."""
from functools import lru_cache
from pathlib import Path

import numpy as np
import onnxruntime as ort


@lru_cache(maxsize=1)
def model():
    options=ort.SessionOptions()
    options.intra_op_num_threads=1
    options.inter_op_num_threads=1
    return ort.InferenceSession(str(Path(__file__).parent/'assets/silero-vad-16k.onnx'),
                                sess_options=options,providers=['CPUExecutionProvider'])


class SpeechDetector:
    def __init__(self):
        self.session=model()
        self.reset()

    def reset(self):
        self.state=np.zeros((2,1,128),dtype=np.float32)
        self.context=np.zeros((1,64),dtype=np.float32)
        self.pending=np.empty(0,dtype=np.float32)
        self.probability=0.

    def feed(self, pcm: bytes) -> float:
        self.pending=np.concatenate((self.pending,np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768.))
        while self.pending.size>=512:
            block=self.pending[:512].reshape(1,512)
            self.pending=self.pending[512:]
            probability,self.state=self.session.run(None,{
                'input':np.concatenate((self.context,block),axis=1),
                'state':self.state,'sr':np.asarray(16000,dtype=np.int64),
            })
            self.context=block[:,-64:].copy()
            self.probability=float(probability[0,0])
        return self.probability
