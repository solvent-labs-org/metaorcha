package ai.metaorcha.metabot;

import java.security.MessageDigest;
import java.security.GeneralSecurityException;

public final class Fingerprint {
    private Fingerprint() {}

    public static byte[] decodeHex(String hex) {
        if (hex == null) return null;
        String s = hex.trim();
        if (s.length() == 0 || (s.length() & 1) == 1) return null;
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            int hi = Character.digit(s.charAt(i * 2), 16);
            int lo = Character.digit(s.charAt(i * 2 + 1), 16);
            if (hi < 0 || lo < 0) return null;
            out[i] = (byte) ((hi << 4) | lo);
        }
        return out;
    }

    public static String sha256Hex(byte[] der) {
        if (der == null) return null;
        try {
            byte[] dig = MessageDigest.getInstance("SHA-256").digest(der);
            StringBuilder sb = new StringBuilder(dig.length * 2);
            for (int i = 0; i < dig.length; i++) {
                sb.append(Character.forDigit((dig[i] >> 4) & 0xf, 16));
                sb.append(Character.forDigit(dig[i] & 0xf, 16));
            }
            return sb.toString();
        } catch (GeneralSecurityException e) {
            return null;
        }
    }

    /** True when the stored lowercase (or mixed-case) hex is the SHA-256 of these DER bytes. */
    public static boolean matches(String stored, byte[] der) {
        if (stored == null || der == null) return false;
        byte[] want = decodeHex(stored);
        if (want == null || want.length != 32) return false;
        String got = sha256Hex(der);
        if (got == null) return false;
        byte[] have = decodeHex(got);
        if (have == null || have.length != want.length) return false;
        int diff = 0;
        for (int i = 0; i < want.length; i++) diff |= want[i] ^ have[i];
        return diff == 0;
    }
}
