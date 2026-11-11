#!/usr/bin/env python3
"""Turn a WAV file into an Omi offline-sync upload (.bin) for /v2/sync-local-files.

The backend's PCM sync format (backend/utils/sync/files.py decode_pcm_file_to_wav)
is a sequence of [uint32 little-endian frame length][PCM16 mono 16 kHz frame].
The codec, sample rate and capture time are encoded in the filename:
    audio_<device>_pcm16_16000_1_fs160_<unix-seconds>.bin

Usage: make_sync_bin.py INPUT.wav OUTPUT_DIR [UNIX_SECONDS]
Prints the path of the written file. Stdlib only. The input must already be
16 kHz mono PCM16 (data/samples/jfk.wav is).
"""

import struct
import sys
import time
import wave
from pathlib import Path

FRAME_SAMPLES = 160  # "fs160": 10 ms frames at 16 kHz


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    src, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    ts = int(sys.argv[3]) if len(sys.argv) == 4 else int(time.time()) - 120
    with wave.open(str(src), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            print(f"{src}: need 16 kHz mono PCM16", file=sys.stderr)
            return 1
        pcm = w.readframes(w.getnframes())
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"audio_localtest_pcm16_16000_1_fs{FRAME_SAMPLES}_{ts}.bin"
    step = FRAME_SAMPLES * 2
    with out.open("wb") as f:
        for i in range(0, len(pcm), step):
            frame = pcm[i : i + step]
            f.write(struct.pack("<I", len(frame)))
            f.write(frame)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
