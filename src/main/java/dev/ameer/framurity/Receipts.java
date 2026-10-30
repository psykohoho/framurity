package dev.ameer.framurity;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse.BodyHandlers;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Base64;
import java.util.HexFormat;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;

import com.google.gson.JsonObject;

import com.mojang.blaze3d.platform.NativeImage;

import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;

import net.fabricmc.loader.api.FabricLoader;

// Each new round, shrinks the finished frame to 32x18 grey and has a public timestamp authority (RFC 3161) sign
// "this picture, showing this round, existed by now". drand proves a frame is no older than its round; the signed
// time proves it's no newer, so a code shown late gets caught. verify.py rebuilds the same digest, keep them in sync.
final class Receipts {
	static final String[] TSAS = {"http://timestamp.digicert.com", "http://timestamp.globalsign.com/tsa/r6advanced1",
			"http://ts.ssl.com"};
	static final int TW = 32, TH = 18;
	static final long HEDGE_MS = 800; // DigiCert usually answers in ~500 ms

	// tick = the game's own clock (20 per second). In a replay it comes from the recording, so skipping
	// ahead inside the replay makes it jump while drand keeps steady time.
	record Shot(long round, long tick, int w, int h, byte[] thumb) {
	}

	static final BlockingQueue<Shot> QUEUE = new ArrayBlockingQueue<>(4); // full = TSAs too slow, skip a round
	static volatile long drawn; // round on the last frame the HUD drew
	static long shot;

	static void start() {
		Thread t = new Thread(Receipts::send, "framurity-receipts");
		t.setDaemon(true);
		t.start();
	}

	// at tick time the main target still holds the last finished frame, HUD included
	static void tick(Minecraft mc) {
		long r = drawn;
		if (r == 0 || r == shot || mc.level == null) return;
		shot = r;
		long tick = mc.level.getGameTime();
		var target = mc.gameRenderer.mainRenderTarget();
		var win = mc.getWindow();
		int gw = win.getGuiScaledWidth(), gh = win.getGuiScaledHeight();
		Screenshot.takeScreenshot(target, factor(target.width, target.height), img -> {
			try (img) {
				QUEUE.offer(new Shot(r, tick, gw, gh, thumb(img)));
			}
		});
	}

	// biggest downscale that divides the size and leaves at least 64x36, so the GPU does most of the shrinking
	static int factor(int w, int h) {
		for (int k = Math.min(w / (2 * TW), h / (2 * TH)); k > 1; k--)
			if (w % k == 0 && h % k == 0) return k;
		return 1;
	}

	static byte[] thumb(NativeImage img) {
		int w = img.getWidth(), h = img.getHeight();
		int[] px = img.getPixels(); // ARGB
		byte[] t = new byte[TW * TH];
		for (int ty = 0; ty < TH; ty++)
			for (int tx = 0; tx < TW; tx++) {
				int x0 = tx * w / TW, x1 = (tx + 1) * w / TW, y0 = ty * h / TH, y1 = (ty + 1) * h / TH;
				double sum = 0;
				for (int y = y0; y < y1; y++)
					for (int x = x0; x < x1; x++) {
						int c = px[y * w + x];
						sum += 0.2126 * (c >> 16 & 0xFF) + 0.7152 * (c >> 8 & 0xFF) + 0.0722 * (c & 0xFF);
					}
				t[ty * TW + tx] = (byte) Math.round(sum / ((x1 - x0) * (y1 - y0)));
			}
		return t;
	}

	static void send() {
		Path file = FabricLoader.getInstance().getGameDir().resolve("framurity")
				.resolve("receipts-" + LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")) + ".jsonl");
		byte[] prev = new byte[32]; // each receipt names the one before, so none can be dropped quietly
		while (true) {
			Shot s;
			try {
				s = QUEUE.take();
			} catch (InterruptedException e) {
				return;
			}
			byte[] digest = sha256(ByteBuffer.allocate(3 + 8 + 8 + 4 + 32 + s.thumb.length).put("BV2".getBytes(StandardCharsets.US_ASCII))
					.putLong(s.round).putLong(s.tick).putShort((short) s.w).putShort((short) s.h).put(prev).put(s.thumb).array());
			try {
				Stamp st = stamp(digest);
				var j = new JsonObject();
				j.addProperty("round", s.round);
				j.addProperty("tick", s.tick);
				j.addProperty("w", s.w);
				j.addProperty("h", s.h);
				j.addProperty("prev", HexFormat.of().formatHex(prev));
				j.addProperty("thumb", Base64.getEncoder().encodeToString(s.thumb));
				j.addProperty("tsa", st.tsa());
				j.addProperty("tsr", Base64.getEncoder().encodeToString(st.tsr()));
				Files.createDirectories(file.getParent());
				Files.writeString(file, j + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND);
				prev = sha256(st.tsr());
			} catch (Exception e) {
				// every authority failed: this round has no receipt
			}
		}
	}

	record Stamp(String tsa, byte[] tsr) {
	}

	// Hedged: ask the first authority; if it hasn't answered within HEDGE_MS, ask the next one too, and so on.
	// The first good answer wins. Normally that's one request, and a slow authority costs HEDGE_MS, not a timeout
	// (a receipt signed more than 3 s after its round counts as late).
	static Stamp stamp(byte[] digest) throws Exception {
		var winner = new CompletableFuture<Stamp>();
		var failed = new AtomicInteger();
		for (String url : TSAS) {
			request(url, digest).whenComplete((tsr, err) -> {
				if (err == null) winner.complete(new Stamp(url, tsr));
				else if (failed.incrementAndGet() == TSAS.length) winner.completeExceptionally(err);
			});
			try {
				return winner.get(HEDGE_MS, TimeUnit.MILLISECONDS);
			} catch (TimeoutException e) {
				// still nothing: ask the next authority as well
			}
		}
		return winner.get(3, TimeUnit.SECONDS);
	}

	// TimeStampReq { version 1, messageImprint { sha256, digest }, certReq TRUE }, DER by hand: it never changes shape
	static CompletableFuture<byte[]> request(String url, byte[] digest) {
		byte[] req = ByteBuffer.allocate(59)
				.put(HexFormat.of().parseHex("30390201013031300d06096086480165030402010500" + "0420"))
				.put(digest).put(HexFormat.of().parseHex("0101ff")).array();
		return Framurity.HTTP.sendAsync(HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(3))
				.header("Content-Type", "application/timestamp-query").POST(HttpRequest.BodyPublishers.ofByteArray(req)).build(),
				BodyHandlers.ofByteArray()).thenApply(res -> {
					byte[] b = res.body();
					// TimeStampResp starts with PKIStatusInfo { status }; 0 = granted, 1 = granted with mods
					int i = (b[1] & 0x80) != 0 ? 2 + (b[1] & 0x7F) : 2;
					if (res.statusCode() != 200 || b[i] != 0x30 || b[i + 2] != 2 || b[i + 3] != 1 || (b[i + 4] & 0xFE) != 0)
						throw new IllegalStateException("timestamp refused by " + url);
					return b;
				});
	}

	static byte[] sha256(byte[] v) {
		try {
			return MessageDigest.getInstance("SHA-256").digest(v);
		} catch (Exception e) {
			throw new IllegalStateException(e);
		}
	}
}
