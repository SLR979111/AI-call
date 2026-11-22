"""Remove provider padding before the first sound in a 24 kHz PCM16 utterance."""

import re
from array import array


def first_sound(pcm):
    # Detect non-silent 5 ms windows, retaining 20 ms for quiet consonants.
    # Return None while more input could still establish the onset.
    samples = array("h", pcm[:min(len(pcm) // 2 * 2, 24000)])
    for offset in range(0, len(samples) - 119, 120):
        if sum(v * v for v in samples[offset:offset + 120]) > 120 * 32 ** 2:
            return max(0, offset - 480) * 2
    return 0 if len(samples) >= 12000 else None


def spoken_prefix(text):
    # Bound the extra synthesis to one natural clause. A separate recording
    # keeps a reusable boundary without guessing where a word ends in PCM.
    match = re.match(r".{4,}?[，。！？；：]", text)
    return match[0] if match else None
