package dev.ameer.framurity;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse.BodyHandlers;
import java.time.Duration;
import java.util.HexFormat;

import com.google.gson.JsonParser;

import net.minecraft.resources.Identifier;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.rendering.v1.hud.HudElementRegistry;

// Draws the latest drand round (new every 3 s) in the HUD corner; Receipts gets each one witnessed.
public final class Framurity implements ClientModInitializer {
	static final String CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971";
	static final String[] RELAYS = {"https://api.drand.sh/", "https://drand.cloudflare.com/"};
	static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

	record Beacon(long round, byte[] rand, long at) {
	}

	static volatile Beacon latest;

	@Override
	public void onInitializeClient() {
		Thread t = new Thread(Framurity::poll, "drand");
		t.setDaemon(true);
		t.start();
		Receipts.start();
		HudElementRegistry.addLast(Identifier.fromNamespaceAndPath("framurity", "code"), (g, delta) -> {
			Beacon b = live();
			if (b == null) return;
			Receipts.drawn = b.round;
			boolean[] bits = Code.bits(b.round, b.rand);
			for (int y = 0; y < 11; y++)
				for (int x = 0; x < 12; x++)
					g.fill(2 + 2 * x, 2 + 2 * y, 4 + 2 * x, 4 + 2 * y, Code.white(bits, x, y) ? 0xFFFFFFFF : 0xFF000000);
		});
		ClientTickEvents.END_CLIENT_TICK.register(Receipts::tick);
	}

	// no code beats an old one: a stale round would look like a cut
	static Beacon live() {
		Beacon b = latest;
		return b != null && System.nanoTime() - b.at < 4_000_000_000L ? b : null;
	}

	// Asks both relays at once each time: whichever brings a newer round first wins. One relay can take 1-3 s,
	// and a round shown that late gets its receipt signed too late too.
	static void poll() {
		while (true) {
			for (String relay : RELAYS)
				HTTP.sendAsync(HttpRequest.newBuilder(URI.create(relay + CHAIN + "/public/latest")).timeout(Duration.ofSeconds(2))
						.build(), BodyHandlers.ofString()).thenAccept(res -> offer(res.body()));
			try {
				Thread.sleep(400);
			} catch (InterruptedException e) {
				return;
			}
		}
	}

	// synchronized: both relays' answers can arrive at the same moment
	static synchronized void offer(String body) {
		try {
			var json = JsonParser.parseString(body).getAsJsonObject();
			long r = json.get("round").getAsLong();
			if (latest == null || r > latest.round)
				latest = new Beacon(r, HexFormat.of().parseHex(json.get("randomness").getAsString()), System.nanoTime());
		} catch (Exception e) {
			// a bad or empty reply: the other relay or the next poll will do
		}
	}

}
