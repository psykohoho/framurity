"""Self-test: draws the mod's code (real drand rounds) on moving noise, optionally cut or reordered.

    python make_test_video.py out.mp4 [--size 1280x720] [--px 2] [--offset 0,0] [--seconds 40]
                              [--cut START:LEN ...] [--swap A:LEN:B] [--receipts out.jsonl [--lag START:LEN]]

--receipts plays the recording out in real time and gets real timestamp receipts for it, like the mod does.
--lag START:LEN is the cheat they catch: cut LEN seconds at START and show the code LEN seconds late from then on.
--skip START:LEN fakes a cut made inside a replay: at START the game jumps LEN seconds ahead, video and code don't.
--speed START:LEN:RATE runs the game at RATE x normal speed (e.g. 0.7 = slow motion) for LEN seconds from START.
"""
import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify import CELL, GENESIS, PERIOD, RING, TH, TW, bits, digest, drand, tool  # noqa: E402

FPS = 30
TSAS = ["http://timestamp.digicert.com", "http://timestamp.globalsign.com/tsa/r6advanced1", "http://ts.ssl.com"]


HEDGE = 0.8  # seconds before also asking the next authority, as in Receipts.java
POOL = concurrent.futures.ThreadPoolExecutor(len(TSAS))


def stamp(dig):  # same hedged request as Receipts.java: first good answer wins
    req = bytes.fromhex("30390201013031300d06096086480165030402010500" + "0420") + dig + bytes.fromhex("0101ff")

    def ask(url):
        r = urllib.request.Request(url, req, {"Content-Type": "application/timestamp-query"})
        return url, urllib.request.urlopen(r, timeout=3).read()

    pending = set()
    for i, url in enumerate(TSAS + [None]):
        if url:
            pending.add(POOL.submit(ask, url))
        while pending:
            done, pending = concurrent.futures.wait(pending, HEDGE if url else 3, concurrent.futures.FIRST_COMPLETED)
            for f in done:
                if f.exception() is None:
                    return f.result()
            if not done:
                break
    raise RuntimeError("no timestamp authority answered")


def thumb(img):  # same box average as Receipts.java
    h, w = img.shape
    return bytes(round(img[ty * h // TH:(ty + 1) * h // TH, tx * w // TW:(tx + 1) * w // TW].mean())
                 for ty in range(TH) for tx in range(TW))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--px", type=float, default=2.0)
    ap.add_argument("--offset", default="0,0")
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--cut", action="append", default=[])
    ap.add_argument("--swap")
    ap.add_argument("--receipts")
    ap.add_argument("--lag", default="0:0")
    ap.add_argument("--skip", default="0:0")
    ap.add_argument("--speed", default="0:0:1")
    a = ap.parse_args()
    w, h = map(int, a.size.split("x"))
    ox, oy = map(int, a.offset.split(","))
    lag_at, lag = map(float, a.lag.split(":"))
    skip_at, skip = map(float, a.skip.split(":"))
    speed_at, speed_len, speed_rate = map(float, a.speed.split(":"))
    rng = random.Random(1)

    latest = drand("public/latest")["round"]
    r0 = latest + 1 if a.receipts else latest - 200  # live runs need rounds that haven't happened yet
    rounds = range(r0, r0 + int((a.seconds + lag) / PERIOD) + 3)
    rand = {}
    shown = {r: GENESIS + (r - 1) * PERIOD + rng.uniform(0.05, 0.6) for r in rounds}  # network delay
    t0 = GENESIS + (r0 - 1) * PERIOD + 1.0

    def code(t):  # round on screen at real time t; the cheat lags it after the cut
        late = lag if t >= t0 + lag_at + lag else 0
        return max((r for r in rounds if shown[r] <= t - late), default=None)

    g = np.random.default_rng(2)  # big blotches give the receipt thumbnails something to see, like real scenery
    blotch = np.kron(g.random((h // 10 + 1, w // 10 + 1)), np.ones((20, 20)))[:h * 2, :w * 2]
    noise = (blotch * 150 + g.random((h * 2, w * 2)) * 80 + 15).astype(np.uint8)
    cell = CELL * a.px

    def game_time(t):  # the game's own time at real time t (jumps ahead at a replay skip, slower or faster in --speed)
        g = t - t0
        if g > speed_at:
            g = speed_at + min(g - speed_at, speed_len) * speed_rate + max(0.0, g - speed_at - speed_len)
        return g + (skip if t >= t0 + skip_at else 0)

    def frame(t):
        k = round(game_time(t) * FPS)
        f = noise[(k * 3) % h:(k * 3) % h + h, (k * 7) % w:(k * 7) % w + w].copy()
        r = code(t)
        if r:
            if r not in rand:
                rand[r] = bytes.fromhex(drand(f"public/{r}")["randomness"])
            b = bits(r, rand[r])
            for cy in range(RING.shape[0]):
                for cx in range(RING.shape[1]):
                    on = RING[cy, cx] == 1 or RING[cy, cx] > 1 and b[(cy - 2) * 8 + cx - 2]
                    x, y = ox + (2 + cx * CELL) * a.px, oy + (2 + cy * CELL) * a.px
                    f[round(y):round(y + cell), round(x):round(x + cell)] = 255 if on else 0
        return f

    game = [t0 + i / FPS for i in range(int((a.seconds + lag) * FPS))]
    if a.receipts:  # like the mod: on the first frame of each new round, thumbnail it and get it signed, in order
        with open(a.receipts, "w") as out, concurrent.futures.ThreadPoolExecutor(1) as ex:  # executor finishes first
            prev, last, gw, gh = [bytes(32)], None, round((w - ox) / a.px), round((h - oy) / a.px)

            def send(rec):
                try:
                    sign(rec)
                except Exception as e:  # the executor would swallow it otherwise
                    print(f"receipt for round {rec['round']} failed: {e!r}", file=sys.stderr)

            def sign(rec):
                rec["prev"] = prev[0].hex()
                rec["tsa"], tsr = stamp(digest(rec))
                rec["tsr"] = base64.b64encode(tsr).decode()
                out.write(json.dumps(rec) + "\n")
                prev[0] = hashlib.sha256(tsr).digest()

            for t in game:
                r = code(t)
                if r is None or r == last:
                    continue
                last = r
                time.sleep(max(0.0, t + 0.05 - time.time()))
                while r not in rand:  # relays can trail the round by a moment
                    try:
                        frame(t)
                    except urllib.error.URLError:
                        time.sleep(0.2)
                ex.submit(send, {"round": r, "tick": 24000 + round(game_time(t) * 20), "w": gw, "h": gh,
                                 "thumb": base64.b64encode(thumb(frame(t)[oy:, ox:])).decode()})
    else:
        rand.update({r: bytes.fromhex(drand(f"public/{r}")["randomness"]) for r in rounds})

    real = [t for t in game if not t0 + lag_at <= t < t0 + lag_at + lag]
    for c in a.cut:
        s, n = map(float, c.split(":"))
        real = [t for t in real if not t0 + s <= t < t0 + s + n]
    if a.swap:
        s, n, b = map(float, a.swap.split(":"))
        chunk = [t for t in real if t0 + s <= t < t0 + s + n]
        rest = [t for t in real if t not in chunk]
        k = next(i for i, t in enumerate(rest) if t >= t0 + b)
        real = rest[:k] + chunk + rest[k:]

    p = subprocess.Popen([tool("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}",
                          "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p", a.out],
                         stdin=subprocess.PIPE)
    for t in real:
        p.stdin.write(frame(t).tobytes())
    p.stdin.close()
    sys.exit(p.wait())


if __name__ == "__main__":
    main()
