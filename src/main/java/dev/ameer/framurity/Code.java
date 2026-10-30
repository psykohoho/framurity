package dev.ameer.framurity;

import java.util.HexFormat;

// 12x11 cells: black ring, white ring, 56 bits = round (28) + randomness (20) + crc8.
// verify.py decodes the same layout, keep them in sync.
public final class Code {
	static boolean[] bits(long round, byte[] v) {
		long p = (round & 0xFFFFFFFL) << 20 | (v[0] & 0xFF) << 12 | (v[1] & 0xFF) << 4 | (v[2] & 0xFF) >> 4;
		int c = 0;
		for (int s = 40; s >= 0; s -= 8) {
			c ^= (int) (p >>> s) & 0xFF;
			for (int k = 0; k < 8; k++) c = (c << 1 ^ ((c & 0x80) != 0 ? 7 : 0)) & 0xFF;
		}
		long all = p << 8 | c;
		boolean[] b = new boolean[56];
		for (int i = 0; i < 56; i++) b[i] = (all >>> 55 - i & 1) == 1;
		return b;
	}

	static boolean white(boolean[] b, int x, int y) {
		int ring = Math.min(Math.min(x, y), Math.min(11 - x, 10 - y));
		return ring == 1 || ring > 1 && b[(y - 2) * 8 + x - 2];
	}

	// java Code.java <round> <hex>: prints the bits, to check verify.py against
	public static void main(String[] a) {
		var sb = new StringBuilder();
		for (boolean x : bits(Long.parseLong(a[0]), HexFormat.of().parseHex(a[1]))) sb.append(x ? 1 : 0);
		System.out.println(sb);
	}
}
