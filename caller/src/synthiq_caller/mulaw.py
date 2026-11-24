"""G.711 mu-law to and from 16-bit linear PCM, in plain Python.

The standard library's ``audioop`` did this and was removed in Python 3.13.
Telephone legs need it, so it lives here with no dependency.
"""

from __future__ import annotations

import array

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    magnitude = min(-sample if sample < 0 else sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (magnitude & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_byte(value: int) -> int:
    value = ~value & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    return -sample if sign else sample


_DECODE = [_decode_byte(i) for i in range(256)]


def lin2ulaw(pcm: bytes) -> bytes:
    """16-bit little-endian mono PCM to mu-law, one byte per sample."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    return bytes(_encode_sample(s) for s in samples)


def ulaw2lin(data: bytes) -> bytes:
    """Mu-law bytes to 16-bit little-endian mono PCM."""
    return array.array("h", (_DECODE[b] for b in data)).tobytes()
