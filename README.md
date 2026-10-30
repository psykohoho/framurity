# Framurity

Okay so basically, this is a Fabric mod (Minecraft 26.3) plus a checker, and together they can tell you if a
recording was cut, spliced or messed with in any way. That's it. That's the whole point.

If you've seen the whole jolt cracking thing, you know how hard it is to actually *prove* a run is legit. So
instead of guessing from hunger bars, I thought, why not just make the footage prove itself?

## How it works (the simple version)

Every 3 seconds, [drand](https://drand.love) publishes a brand new random number. It's made by a group of
independent organisations (the League of Entropy, Cloudflare being one of them), and nobody, and I mean nobody,
can know that number before it's out.

So the mod takes that number and shows it as a tiny black and white code in the top left corner of your screen.
That's the little thing you'd have seen in my video if you had a keen eye.

Then on top of that, every 3 seconds the mod takes a super tiny blurry snapshot of your screen and gets an
official timestamp server (DigiCert, with GlobalSign and SSL.com as backups) to sign it. Basically a notary going
"yep, this picture existed at exactly this time". All of those signed receipts get saved in a small file next to
your game.

And then the checker goes through your video frame by frame, reads the code, and matches everything up.

## Why it's actually hard to fake

So take one moment in your video.

- **It can't be older than it says.** It has a drand number in it, and you can't show a number that didn't exist
  yet. So no pre-recording stuff.
- **It can't be newer than it says.** The timestamp server signed it at an exact time, with actual cryptography.
  You can't go back and make it later.

So every few seconds of footage is basically locked in its own little window. Now try cutting two clips together.
Doesn't matter how clean your edit is, the numbers jump, the windows don't line up, and there's a hole. You can
see it.

On top of that:

- **Every receipt points to the one before it**, like a chain. So you can't just quietly delete one.
- **Every receipt also stores the game's own tick counter.** This one's for the sneaky people. If you record with a
  replay mod, play it back and skip ahead *inside* the replay, the video itself is technically uncut. But the game
  clock jumps ahead of real time, and that gets caught. (Yes, I learned this the hard way building a savestate mod.)

No receipt file? Then the checker just says **UNVERIFIED**. No receipts, no proof.

## For players

1. Install Fabric Loader (0.19.5+) and Fabric API for Minecraft 26.3.
2. Grab `framurity-<version>.jar` from the [Releases](../../releases) page and put it in your `mods` folder.
3. Record like you normally would. Just keep the HUD on (don't press F1) and stay online. If the mod can't reach
   drand, the code disappears and that part of the video can't be checked.
4. When you hand in your video, send the matching `receipts-<date>-<time>.jsonl` from `.minecraft/framurity` with
   it (you get one file every time you launch the game).

Privacy wise: the mod asks `api.drand.sh` (or `drand.cloudflare.com` as backup) for the latest number about twice
a second. The timestamp server only ever gets a fingerprint (a SHA-256 hash), never your actual screen. The tiny
32 x 18 grey snapshot stays in your receipt file, so whoever you send it to just sees a really blurry version of
your screen. That's all.

## For checkers

You'll need Python 3.10+, `numpy`, `certifi`, `ffmpeg` (with `ffprobe`) on your PATH or `FFMPEG_DIR` pointing to
its folder, and `openssl` on your PATH or `OPENSSL` pointing to it (Git for Windows comes with one).

```
pip install numpy certifi
python verifier/verify.py recording.mp4 --receipts receipts-20261024-101500.jsonl
```

It finds the code on its own (any GUI scale, window borders, even downscaled uploads), lists every problem with the
video time it happened at, and then gives you a verdict:

- **NO CUTS FOUND**: everything was in order, on time, and had a matching signed receipt. Clean.
- **NO CUTS FOUND IN THE CHECKED PARTS**: same thing, but some bits had no readable code or no receipt (it lists them).
- **EDITED**: it found a cut, a splice, a fake code, a late code, a receipt that doesn't match, or the game clock
  jumping. It tells you where.
- **GAME SPEED CHANGED**: no cuts, but the game ran noticeably faster or slower than normal for a while (`/tick rate`,
  slow-mo mods, or really heavy lag).
- **UNVERIFIED**: no usable receipts, or a long stretch with nothing to check. Short gaps are just internet or lag
  blips, but a long one could be hiding anything.

Want to try it without recording anything? `python verifier/make_test_video.py` makes fake test videos using real
drand rounds, so you can throw cuts and swaps at it (`--cut 12:7.3`, `--swap 10:4:30`) and see if it catches them.

## Is it perfect?

No, and I'm not gonna pretend it is. A few honest limits:

- It only works on recordings made **with the mod running**. It can't check old footage.
- It proves a video wasn't cut, not that it was your **first try**. Someone could record 50 runs and just post the
  best one.
- The mod runs on the player's own PC, so a seriously hacked game can still get away with some stuff. The stuff that
  makes it hard to fake (drand and the timestamp server) isn't on your PC though, so even then there's only so much
  you can hide.

The real fix for that last one is having our own impartial "notaries", basically servers that nobody playing
controls, keeping their own record. That's the next step, and honestly that's where I'd love some help.

## Code format (for devs)

`Code.java` and `verifier/verify.py` both define the corner code, so they have to match
(`java Code.java <round> <hex>` prints the bits). It's 12 x 11 cells of 2 GUI pixels at GUI position (2, 2): a
black outer ring, a white inner ring, then 56 data bits (white = 1): round number (28 bits), the first 20 bits of
that round's randomness, and a CRC-8 (poly 0x07).

Receipts: `Receipts.java` and `verify.py` have to match too. One JSON line per round with `round`, `tick` (game
time), `w`/`h` (GUI size), `thumb` (32 x 18 grey bytes, base64), `prev` (SHA-256 of the previous receipt's `tsr`,
zeros for the first one), `tsa` and `tsr` (the authority's DER TimeStampResp, base64). The signed digest is
SHA-256(`"BV2"` | round u64 | tick u64 | w u16 | h u16 | prev | thumb), all big-endian. (Older receipts from before
the game clock use `"BV1"` with no tick. The checker still reads them, just without the game clock check.)

## Wanna help?

This was inspired by a hackathon I took part in, where I used drand and worked with a team on the timestamp and
chaining side of things. If you've got ideas, find a hole, or just wanna help finish this so we never have to argue
about spliced runs again, open an issue or DM me. Seriously, any help is appreciated :)

## Licence

MIT
