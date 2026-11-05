"""Stream an audio file to /v3/stream the way Omi's backend does, and print segments.

Usage: python stream_client.py ws://host:8000 AUDIO_FILE [--speed 1.0]
--speed 1 sends at real time (like a pendant); higher values send faster.
"""

import argparse
import asyncio
import json
import time

import numpy as np
import websockets
from faster_whisper import decode_audio

CHUNK_S = 0.1


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("audio")
    p.add_argument("--speed", type=float, default=1.0)
    args = p.parse_args()

    audio = decode_audio(args.audio, sampling_rate=16000)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    step = int(16000 * CHUNK_S) * 2

    url = args.url.rstrip("/") + "/v3/stream?sample_rate=16000"
    t0 = time.monotonic()
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        ready = json.loads(await ws.recv())
        assert ready.get("type") == "ready", ready

        async def receive():
            async for msg in ws:
                seg = json.loads(msg)
                lag = (time.monotonic() - t0) * args.speed - seg["end"]
                print(f"[{seg['start']:7.2f}-{seg['end']:7.2f}] ({seg['detected_language']}, "
                      f"+{lag:.1f}s after speech end) {seg['text']}", flush=True)

        recv_task = asyncio.create_task(receive())
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step])
            await asyncio.sleep(CHUNK_S / args.speed)
        await ws.send("finalize")
        await recv_task
    print(f"done: {len(audio) / 16000:.1f}s audio in {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
