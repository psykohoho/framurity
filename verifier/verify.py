"""Checks a Framurity recording for cuts.   python verify.py video.mp4 --receipts receipts.jsonl [--json report.json]

Needs ffmpeg + ffprobe (on PATH, or FFMPEG_DIR) and numpy; for receipts also openssl (on PATH, or OPENSSL) and
certifi (or TSA_CAFILE). Fetches drand rounds over the internet.
"""
import argparse
import base64
import collections
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

import numpy as np

CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
RELAYS = ["https://drand.cloudflare.com/", "https://api.drand.sh/"]  # the usually faster one first
GENESIS, PERIOD = 1692803367, 3
JITTER = 1.0  # how late the mod may show a round (network delay)
MIN_FRAMES = 3  # a round seen in fewer frames than this is a misread
GW, GH, CELL = 12, 11, 2
RING = np.array([[min(x, y, GW - 1 - x, GH - 1 - y) for x in range(GW)] for y in range(GH)])
OUTER, INNER, DATA = RING == 0, RING == 1, RING >= 2
TW, TH = 32, 18  # receipt thumbnail, same as Receipts.java
LATE = 3.0  # most a receipt may trail its round's publication (display delay + timestamp round trip)
MATCH = 0.85  # thumbnail correlation that counts as the same picture
# Below MATCH the picture may still be right: fast camera turns fall between video frames, and in real gameplay
# a wrong frame of similar scenery can score up to ~0.96. Unrelated footage scores far lower, so only below WEAK
# is a receipt a mismatch; in between it's reported as a weak match and doesn't fail the video.
WEAK = 0.5
TPS = 20  # game ticks per second
CATCHUP = 2.0  # after a lag spike the game may run up to ~2 s of ticks fast to catch up; more than that is a skip
SLOW, FAST, SUSTAIN = 0.9, 1.1, 5.0  # game speed outside 90-110% for more than 5 s straight is reported
GAP_MAX = 10.0  # longest stretch without a readable code or receipt that still counts as a blip


def crc8(p):
    c = 0
    for s in range(40, -1, -8):
        c ^= p >> s & 0xFF
        for _ in range(8):
            c = (c << 1 ^ (7 if c & 0x80 else 0)) & 0xFF
    return c


def rand20(v):
    return v[0] << 12 | v[1] << 4 | v[2] >> 4


def bits(r, v):  # same as Code.java
    p = (r & 0xFFFFFFF) << 20 | rand20(v)
    a = p << 8 | crc8(p)
    return [a >> 55 - i & 1 for i in range(56)]


def decode(b):
    a = int("".join(map(str, b)), 2)
    p = a >> 8
    return (p >> 20, p & 0xFFFFF) if crc8(p) == a & 0xFF else None


def tool(name):
    d = os.environ.get("FFMPEG_DIR")
    p = os.path.join(d, name + ".exe") if d else shutil.which(name)
    return p if p and os.path.exists(p) else sys.exit(f"{name} not found (install ffmpeg or set FFMPEG_DIR)")


def drand(path):
    for base in RELAYS:
        try:
            req = urllib.request.Request(base + CHAIN + "/" + path, headers={"User-Agent": "framurity"})
            return json.load(urllib.request.urlopen(req, timeout=10))
        except Exception as e:
            err = e
    raise err


def probe(video):
    out = subprocess.run([tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,avg_frame_rate:frame=best_effort_timestamp_time", "-of", "json", video],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    n, d = s["avg_frame_rate"].split("/")
    return s["width"], s["height"], float(n) / float(d), [float(f["best_effort_timestamp_time"]) for f in j["frames"]]


def frames(video, w, h, times):
    w, h = min(360, w), min(360, h)  # the code sits in the top-left corner
    p = subprocess.Popen([tool("ffmpeg"), "-v", "error", "-i", video, "-vf", f"crop={w}:{h}:0:0", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-fps_mode", "passthrough", "-"], stdout=subprocess.PIPE)
    for t in times:
        buf = p.stdout.read(w * h)
        if len(buf) < w * h:
            break
        yield t - times[0], np.frombuffer(buf, np.uint8).reshape(h, w).astype(np.float32)
    p.stdout.close()
    p.wait()


def read(img, x0, y0, px):
    c = CELL * px
    r = int(c / 4)
    xs = (x0 + (np.arange(GW) + 0.5) * c).astype(int)
    ys = (y0 + (np.arange(GH) + 0.5) * c).astype(int)
    if xs[-1] + r >= img.shape[1] or ys[-1] + r >= img.shape[0]:
        return None
    cells = np.array([[img[y - r:y + r + 1, x - r:x + r + 1].mean() for x in xs] for y in ys])
    lo, hi = cells[OUTER].mean(), cells[INNER].mean()
    return decode((cells[DATA] > (lo + hi) / 2).astype(int).tolist()) if hi - lo > 60 else None


def find(img):
    """Try every GUI scale and position (window borders, downscaled uploads) for a code with a valid crc."""
    best = None
    for px in np.arange(1, 8.01, 0.25):
        xr, yr = (np.arange(GW) + 0.5) * CELL * px, (np.arange(GH) + 0.5) * CELL * px
        nx, ny = min(80, int(img.shape[1] - xr[-1] - 1)), min(80, int(img.shape[0] - yr[-1] - 1))
        if nx < 0 or ny < 0:
            continue
        v = img[(np.arange(ny + 1)[:, None, None, None] + yr[None, None, :, None]).astype(int),
                (np.arange(nx + 1)[None, :, None, None] + xr[None, None, None, :]).astype(int)]
        score = v[..., INNER].mean(-1) - v[..., OUTER].mean(-1)
        for i in np.argsort(score, axis=None)[::-1][:5]:
            y, x = np.unravel_index(i, score.shape)
            if score[y, x] < 80:
                break
            if read(img, x, y, px) and (not best or score[y, x] > best[0]):
                best = (score[y, x], (int(x), int(y), float(px)))
    return best and best[1]


def digest(rec):  # same as Receipts.java; BV1 receipts (before the game clock was added) have no tick
    clock = b"BV2" + rec["round"].to_bytes(8, "big") + rec["tick"].to_bytes(8, "big") if "tick" in rec else \
        b"BV1" + rec["round"].to_bytes(8, "big")
    return hashlib.sha256(clock + rec["w"].to_bytes(2, "big") + rec["h"].to_bytes(2, "big") +
                          bytes.fromhex(rec["prev"]) + base64.b64decode(rec["thumb"])).digest()


def signed_time(rec):
    """Checks the timestamp authority's signature over the receipt; returns the signed unix time, or None."""
    ssl = os.environ.get("OPENSSL") or shutil.which("openssl") or sys.exit("openssl not found (install it or set OPENSSL)")
    ca = os.environ.get("TSA_CAFILE")
    if not ca:
        try:
            import certifi
            ca = certifi.where()
        except ImportError:
            sys.exit("needs certifi (pip install certifi) or TSA_CAFILE set to a CA bundle")
    fd, tsr = tempfile.mkstemp(suffix=".tsr")
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(rec["tsr"]))
    try:
        ok = subprocess.run([ssl, "ts", "-verify", "-digest", digest(rec).hex(), "-in", tsr, "-CAfile", ca],
                            capture_output=True, text=True).stdout
        text = subprocess.run([ssl, "ts", "-reply", "-in", tsr, "-text"], capture_output=True, text=True).stdout
    finally:
        os.unlink(tsr)
    m = re.search(r"Time stamp: (\w+) +(\d+) ([\d:]+)(\.\d+)? (\d{4}) GMT", text)
    if "Verification: OK" not in ok or not m:
        return None
    t = datetime.strptime(f"{m[1]} {m[2]} {m[3]} {m[5]}", "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    return t.timestamp() + float(m[4] or 0)


def thumbs(video, box):
    """Every frame's game area shrunk to TW x TH grey, to compare with the receipts."""
    p = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", video, "-vf", "crop=%d:%d:%d:%d,scale=%d:%d:flags=area" % (
        *box, TW, TH), "-f", "rawvideo", "-pix_fmt", "gray", "-fps_mode", "passthrough", "-"], capture_output=True, check=True)
    return np.frombuffer(p.stdout, np.uint8).reshape(-1, TW * TH).astype(np.float32)


def corr(a, b):
    a, b = a - a.mean(), b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d else 0.0


def witness(path, video, w, h, lock, seen, full):
    """Which rounds have a receipt that's signed, on time, and matches a frame showing that round."""
    recs = [json.loads(line) for line in open(path) if line.strip()]
    problems = [f"receipt chain broken before round {b['round']} (receipts removed or reordered)" for a, b in
                zip(recs, recs[1:]) if b["prev"] != hashlib.sha256(base64.b64decode(a["tsr"])).hexdigest()]
    frames_of = {}
    for i, (_, d) in enumerate(seen):
        if d:
            frames_of.setdefault(full[d[0]], []).append(i)
    recs = [r for r in recs if r["round"] in frames_of]
    if not recs:
        return set(), problems, [], []
    shown = [full[d[0]] if d else None for _, d in seen]  # round on each frame
    # game area: the code sits 2 GUI px in from its corner; most common GUI size wins (window resizes are rare)
    gw, gh = max({(r["w"], r["h"]) for r in recs}, key=[(r["w"], r["h"]) for r in recs].count)
    x, y = max(0, round(lock[0] - 2 * lock[2])), max(0, round(lock[1] - 2 * lock[2]))
    thumb = thumbs(video, (min(w - x, round(gw * lock[2])), min(h - y, round(gh * lock[2])), x, y))
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        times = list(ex.map(signed_time, recs))
    good, weak, rows, clock = set(), [], [], []
    for r, t in zip(recs, times):
        idx = frames_of[r["round"]]
        cand = range(max(0, idx[0] - 2), min(len(thumb), idx[-1] + 3))
        mine = np.frombuffer(base64.b64decode(r["thumb"]), np.uint8).astype(np.float32)
        best = max(corr(thumb[i], mine) for i in cand)
        late = None if t is None else t - (GENESIS + (r["round"] - 1) * PERIOD)
        rows.append({"round": r["round"], "tsa": r["tsa"], "late": late, "match": best, "tick": r.get("tick")})
        # The mod reads the tick the moment a round first shows, so time it by the round's first frame (the best
        # matching frame can be anywhere in a still scene). Only when that start is in the video: right after the
        # previous round, not at the start of the recording or after an unreadable stretch.
        starts = idx[0] > 0 and shown[idx[0] - 1] == r["round"] - 1
        if t is not None and -1 <= late <= LATE and best >= WEAK and "tick" in r and starts:
            clock.append((seen[idx[0]][0], r["tick"], r["round"]))
        if t is None:
            problems.append(f"receipt for round {r['round']} has an invalid signature")
        elif late < -1:
            problems.append(f"receipt for round {r['round']} was signed {-late:.1f} s before the round existed")
        elif late > LATE:
            problems.append(f"LATE: round {r['round']} was still new on screen {late:.1f} s after drand published it "
                            f"(allowed {LATE:g} s): the code was shown late")
        elif best < WEAK and idx[0] == 0:
            pass  # recording started mid-round: the thumbnail was taken before the first frame
        elif best < WEAK:
            problems.append(f"receipt for round {r['round']} doesn't match the video (similarity {best:.2f})")
        else:
            good.add(r["round"])
            if best < MATCH:
                weak.append((r["round"], best))
    # Two clocks: the video's own time vs the game's ticks. Lag and pausing only make the game fall behind;
    # a game clock that leaps ahead (or goes backwards) means time was skipped inside the game, e.g. in a replay.
    most, prev = None, None  # most = furthest the game has fallen behind the video so far
    for tv, tick, rnd in sorted(clock):
        behind = tv - tick / TPS
        if prev and tick < prev:
            problems.append(f"GAME CLOCK went backwards {(prev - tick) / TPS:.1f} s at round {rnd}: rewound inside the game")
        elif most is not None and behind < most - CATCHUP:
            problems.append(f"GAME CLOCK jumped {most - behind:.1f} s ahead of the video at round {rnd}: "
                            f"time was cut from the video or skipped inside the game (e.g. in a replay)")
            most = behind  # report each jump once
        most = behind if most is None else max(most, behind)
        prev = tick
    # Game speed between back-to-back receipts (game seconds per real second). A lag spike throws off one 3 s step;
    # a game run slower or faster on purpose (/tick rate, slow-motion mods) stays off for a whole stretch.
    clock.sort()
    steps = [(a[0], b[0], a[1], b[1]) for a, b in zip(clock, clock[1:]) if b[2] == a[2] + 1 and b[0] > a[0]]
    rate = lambda t0, t1, k0, k1: (k1 - k0) / TPS / (t1 - t0)
    speed, run = [], []
    for st in steps + [None]:
        off = st is not None and not SLOW <= rate(*st) <= FAST
        if off and run and run[-1][1] == st[0] and (rate(*run[-1]) < 1) == (rate(*st) < 1):
            run.append(st)
            continue
        if run and run[-1][1] - run[0][0] > SUSTAIN:
            speed.append({"from": run[0][0], "to": run[-1][1], "rate": rate(run[0][0], run[-1][1], run[0][2], run[-1][3])})
        run = [st] if off else []
    return good, problems, rows, weak, speed


def analyse(segs, dt):
    """Each round pins (video time - real time) to a window. In an uncut video the windows overlap."""
    events, lo, hi, prev = [], None, None, None
    for s in segs:
        r0 = GENESIS + (s["round"] - 1) * PERIOD
        a, b = s["last"] - r0 - PERIOD - JITTER - dt, s["first"] - r0 + dt
        if lo is not None and max(lo, a) > min(hi, b):
            cut = b < lo
            gap = (lo - b, hi - a) if cut else (a - hi, b - lo)
            events.append({"kind": "CUT" if cut else "SPLICE", "at": s["first"], "from": prev, "to": s["round"],
                           "seconds": [max(0.0, gap[0]), max(0.0, gap[1])]})
            lo = None
        lo, hi = (a, b) if lo is None else (max(lo, a), min(hi, b))
        prev = s["round"]
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--receipts", help="receipts-*.jsonl from the player's .minecraft/framurity folder")
    ap.add_argument("--json")
    args = ap.parse_args()
    w, h, fps, times = probe(args.video)
    print(f"Video: {w}x{h} @ {fps:.2f} fps")

    lock, seen, misses = None, [], 0
    for t, img in frames(args.video, w, h, times):
        d = read(img, *lock) if lock else None
        if d is None and misses % max(1, int(fps)) == 0:  # look again about once a second
            lock = find(img) or lock
            d = read(img, *lock) if lock else None
        misses = 0 if d else misses + 1
        seen.append((t, d))
    if not lock:
        sys.exit("No Framurity code anywhere in this video.")
    print(f"Code at x={lock[0]} y={lock[1]}, {lock[2]:g} px per GUI px")

    # A real round stays on screen for ~3 s. A code seen in only a frame or two is a misread that happened to
    # pass the 8-bit checksum (about 1 in 256 misreads do), so treat those frames as unreadable.
    count = collections.Counter(d[0] for _, d in seen if d)
    seen = [(t, d if d and count[d[0]] >= MIN_FRAMES else None) for t, d in seen]
    latest = drand("public/latest")["round"]
    full = {lo: latest - ((latest - lo) & 0xFFFFFFF) for lo in {d[0] for _, d in seen if d}}
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        real = dict(zip(full.values(), ex.map(lambda r: drand(f"public/{r}"), full.values())))
    for r, j in real.items():  # quicknet: randomness is the hash of the signature
        if hashlib.sha256(bytes.fromhex(j["signature"])).hexdigest() != j["randomness"]:
            sys.exit(f"drand relay returned inconsistent data for round {r}")
    fake = sorted({full[d[0]] for _, d in seen if d and rand20(bytes.fromhex(real[full[d[0]]]["randomness"])) != d[1]})

    segs = []
    for t, d in seen:
        r = full[d[0]] if d else None
        if segs and segs[-1]["round"] == r:
            segs[-1]["last"] = t
        else:
            segs.append({"round": r, "first": t, "last": t})
    events = analyse([s for s in segs if s["round"] and s["round"] not in fake], 1 / fps)
    readable = sum(1 for _, d in seen if d) / len(seen)
    blind = [s for s in segs if s["round"] is None and s["last"] - s["first"] > PERIOD]
    good, problems, rows, weak, speed = witness(args.receipts, args.video, w, h, lock, seen, full) if args.receipts \
        else (set(), [], [], [], [])
    unwitnessed = []  # stretches of rounds with no good receipt: a late code could hide there
    for s in segs:
        if s["round"] and s["round"] not in good:
            if unwitnessed and unwitnessed[-1]["open"]:
                unwitnessed[-1]["last"] = s["last"]
            else:
                unwitnessed.append({"first": s["first"], "last": s["last"], "open": True})
        elif s["round"] and unwitnessed:
            unwitnessed[-1]["open"] = False
    unwitnessed = [u for u in unwitnessed if u["last"] - u["first"] > PERIOD]
    # A few seconds without a code or receipt is an internet or lag blip. Longer, and that stretch could be anything
    # (e.g. old footage painted with drand values collected earlier), so the video can't count as checked.
    gaps = [g for g in blind + unwitnessed if g["last"] - g["first"] > GAP_MAX]

    fmt = lambda t: f"{int(t // 60)}:{t % 60:05.2f}"
    start = datetime.fromtimestamp(GENESIS + (min(full.values()) - 1) * PERIOD, timezone.utc)
    print(f"Readable on {readable:.0%} of frames, {len(full)} rounds, recorded around {start:%Y-%m-%d %H:%M:%S} UTC\n")
    if fake:
        print(f"!! FAKE CODES: rounds {fake} don't match drand")
    for e in events:
        a, b = e["seconds"]
        what = "of real time missing" if e["kind"] == "CUT" else "added (out of order / from elsewhere)"
        print(f"!! {e['kind']} at {fmt(e['at'])}: {a:.1f}-{b:.1f} s {what} (round {e['from']} -> {e['to']})")
    for p in problems:
        print(f"!! {p}")
    for s in blind:
        print(f"?? no code {fmt(s['first'])}-{fmt(s['last'])}")
    for u in unwitnessed if good else []:
        print(f"?? no receipt {fmt(u['first'])}-{fmt(u['last'])}")
    for s in speed:
        why = "slowed down (/tick rate, a slow-motion mod) or very heavy lag" if s["rate"] < 1 else "sped up"
        print(f"!! GAME SPEED {s['rate']:.0%} from {fmt(s['from'])} to {fmt(s['to'])}: the game was {why}")
    if weak:
        print(f"?? weak match (fast motion or similar scenery, not a failure): " +
              ", ".join(f"round {r} {m:.2f}" for r, m in weak))
    if args.receipts:
        print(f"Receipts: {len(good)} of {len(full)} rounds signed on time and matching the video")
    if fake or events or problems:
        verdict = "EDITED"
    elif speed:
        verdict = "GAME SPEED CHANGED (no cuts, but the game didn't run at normal speed)"
    elif not good:
        verdict = "UNVERIFIED (no receipts: a modified mod showing the code late can't be ruled out)"
    elif gaps:
        verdict = "UNVERIFIED (" + ", ".join(f"{fmt(g['first'])}-{fmt(g['last'])}" for g in gaps) + \
                  f" can't be checked: over {GAP_MAX:g} s without a readable code or a receipt)"
    else:
        verdict = "NO CUTS FOUND" + (" IN THE CHECKED PARTS" if blind or unwitnessed or readable < .9 else "")
    print(f"\nVerdict: {verdict}  (cuts under ~{JITTER:.0f} s can't be seen)")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"verdict": verdict, "readable": readable, "lock": lock, "events": events, "fake": fake,
                       "problems": problems, "speed": speed, "receipts": rows, "unwitnessed": unwitnessed, "unreadable": blind,
                       "segments": segs}, f, indent=1)


if __name__ == "__main__":
    main()
