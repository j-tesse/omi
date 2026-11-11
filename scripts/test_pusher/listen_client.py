"""Pendant-style live session against the Omi backend, waiting for finalization.

Streams a 16-bit mono WAV to /v4/listen in real time (100 ms PCM16 frames), then
keeps sending silence (like a quiet pendant; the backend drops sessions that get
no audio for 90 s) until the conversation's silence timeout fires and the pusher
finalizes it. Polls GET /v1/conversations/{id} until it leaves in_progress, then
/v1/conversations/{id}/finalization until the job is terminal.

Runs inside the pusher container (it has python + websockets and is on the
compose network); scripts/test_pusher.sh pipes it in. Prints one JSON summary
line prefixed with RESULT and exits 0 only on a completed, structured conversation.
Stdlib + websockets only.
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

import websockets

CHUNK_MS = 100


def log(msg: str) -> None:
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def http_json(url: str, token: str):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


async def run(args) -> int:
    with wave.open(args.wav) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f'{args.wav}: need 16-bit mono WAV')
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    chunk = rate * 2 * CHUNK_MS // 1000
    token = args.admin_key + args.uid
    query = urllib.parse.urlencode(
        {
            'language': args.language,
            'sample_rate': rate,
            'codec': 'pcm16',
            'channels': 1,
            'include_speech_profile': 'false',
            'source': 'omi',
            'conversation_timeout': args.conversation_timeout,
        }
    )
    http = args.backend.rstrip('/')
    ws_url = http.replace('http', 'ws', 1) + f'/v4/listen?{query}'
    log(f'connect {ws_url} uid={args.uid} audio={len(pcm) / rate / 2:.1f}s')

    conv_ids: list[str] = []
    texts: list[str] = []
    events: list[str] = []

    async with websockets.connect(
        ws_url, extra_headers={'Authorization': f'Bearer {token}'}, max_size=10 * 1024 * 1024, open_timeout=30
    ) as ws:

        async def receive() -> None:
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(data, list):  # transcript segments
                    for seg in data:
                        if isinstance(seg, dict) and seg.get('text'):
                            texts.append(seg['text'])
                            log(f'segment: {seg["text"]}')
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get('type') or '?'
                status = data.get('status')
                cid = data.get('conversation_id') or (data.get('memory') or {}).get('id')
                events.append(f'{kind}:{status}' if status else kind)
                if kind == 'conversation_session' and cid and cid not in conv_ids:
                    conv_ids.append(cid)
                if kind not in ('ping',):
                    log(f'event {kind} status={status} conversation={cid}')
                if kind == 'service_status' and status == 'stt_failed':
                    log('STT failed')

        receiver = asyncio.create_task(receive())
        receiver.add_done_callback(lambda t: t.cancelled() or t.exception())
        step = CHUNK_MS / 1000 / args.speed
        for off in range(0, len(pcm) - chunk + 1, chunk):
            await ws.send(pcm[off : off + chunk])
            await asyncio.sleep(step)
        log(f'audio sent; holding the session open with silence (timeout {args.conversation_timeout}s)')

        silence = b'\x00' * chunk
        deadline = time.monotonic() + args.wait
        next_poll = 0.0
        result = fin = None
        # Like upstream's release probe: keep the socket open until the durable
        # finalization job is terminal (memories, embedding, index fan-out), not
        # just until the conversation reads completed.
        while time.monotonic() < deadline and not receiver.done():
            await ws.send(silence)
            await asyncio.sleep(CHUNK_MS / 1000)
            if time.monotonic() < next_poll or not conv_ids:
                continue
            next_poll = time.monotonic() + 5
            cid = conv_ids[0]
            if result is None:
                status, conv = await asyncio.to_thread(http_json, f'{http}/v1/conversations/{cid}', token)
                log(f'conversation {cid} status={conv.get("status") if conv else f"http {status}"}')
                if conv and conv.get('status') not in ('in_progress', 'processing'):
                    result = conv
                continue
            _, fin = await asyncio.to_thread(http_json, f'{http}/v1/conversations/{cid}/finalization', token)
            log(f'finalization {(fin or {}).get("status")} fanout={(fin or {}).get("fanout_status")}')
            if fin and fin.get('terminal'):
                break
        await ws.close(code=1000, reason='test_pusher done')
        receiver.cancel()

    if result is None:
        print('RESULT ' + json.dumps({'ok': False, 'reason': 'timeout', 'conversations': conv_ids, 'events': events}))
        return 1
    structured = result.get('structured') or {}
    fin = fin or {}
    summary = {
        'ok': result.get('status') == 'completed'
        and bool(structured.get('title'))
        and not result.get('discarded')
        and fin.get('terminal_outcome') == 'success',
        'id': result['id'],
        'status': result.get('status'),
        'discarded': result.get('discarded'),
        'title': structured.get('title'),
        'overview': structured.get('overview'),
        'category': structured.get('category'),
        'action_items': len(structured.get('action_items') or []),
        'transcript': ' '.join(s.get('text', '') for s in result.get('transcript_segments') or []),
        'finalization': {k: fin.get(k) for k in ('status', 'terminal', 'terminal_outcome', 'fanout_status')},
        'live_segments': len(texts),
    }
    print('RESULT ' + json.dumps(summary, ensure_ascii=False))
    return 0 if summary['ok'] else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('wav')
    p.add_argument('--backend', default='http://backend:8080')
    p.add_argument('--uid', required=True)
    p.add_argument('--admin-key', required=True)
    p.add_argument('--language', default='en')
    p.add_argument('--conversation-timeout', type=int, default=120, help='seconds of silence (min 120)')
    p.add_argument('--wait', type=int, default=600, help='max seconds to wait after the audio')
    p.add_argument('--speed', type=float, default=1.0, help='1 = real time like a pendant')
    try:
        sys.exit(asyncio.run(run(p.parse_args())))
    except websockets.ConnectionClosed as e:
        print('RESULT ' + json.dumps({'ok': False, 'reason': f'socket closed: {e}'}))
        sys.exit(1)


if __name__ == '__main__':
    main()
