package ai.metaorcha.metabot;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The E1 Android payload lock (packaging/android-wheels.lock): every artifact the
 * APK ships, pinned by SHA-256 and size. One artifact per line:
 * {@code <kind> <file> <sha256> <bytes> <source>}; {@code #} comments and blank
 * lines are ignored. Kinds: cpython, wheel, sdist, launcher (all fetched or built
 * once and pinned) and local (our own wheels, built at build time — no hash in the
 * lock; the payload manifest hashes what the APK ships). The parser rejects
 * anything else: an unknown kind, an uppercase or short hash, a non-number or
 * non-positive size, a missing field, a duplicate file, a hash on a local entry or
 * no hash on a fetched one.
 */
public final class WheelLock {
    private static final String[] KINDS = { "cpython", "wheel", "sdist", "launcher", "local" };

    public static final class Entry {
        public final String kind;
        public final String file;
        public final String sha256;   // null on a local entry
        public final long bytes;      // -1 on a local entry
        public final String source;

        Entry(String kind, String file, String sha256, long bytes, String source) {
            this.kind = kind;
            this.file = file;
            this.sha256 = sha256;
            this.bytes = bytes;
            this.source = source;
        }

        /** Size and hash match: what the lock pinned. Never true for a local entry. */
        public boolean matches(byte[] data) {
            if (sha256 == null || data == null || data.length != bytes) {
                return false;
            }
            return sha256.equals(Fingerprint.sha256Hex(data));
        }
    }

    private final List<Entry> entries;
    private final Map<String, Entry> byFile;

    private WheelLock(List<Entry> entries) {
        this.entries = Collections.unmodifiableList(entries);
        this.byFile = new LinkedHashMap<String, Entry>();
        for (Entry e : entries) {
            byFile.put(e.file, e);
        }
    }

    public static WheelLock parse(String text) {
        if (text == null) {
            throw new IllegalArgumentException("empty lock");
        }
        List<Entry> out = new ArrayList<Entry>();
        Map<String, Boolean> seen = new LinkedHashMap<String, Boolean>();
        String[] lines = text.split("\n", -1);
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i].trim();
            if (line.isEmpty() || line.startsWith("#")) {
                continue;
            }
            String[] f = line.split(" ");
            if (f.length != 5) {
                throw new IllegalArgumentException("lock line " + (i + 1) + ": want 5 fields");
            }
            if (!isKind(f[0])) {
                throw new IllegalArgumentException("lock line " + (i + 1) + ": bad kind " + f[0]);
            }
            if (f[1].isEmpty() || seen.put(f[1], Boolean.TRUE) != null) {
                throw new IllegalArgumentException("lock line " + (i + 1) + ": duplicate or empty file");
            }
            boolean local = "local".equals(f[0]);
            String sha = null;
            long bytes = -1;
            if (local) {
                if (!"-".equals(f[2]) || !"-".equals(f[3])) {
                    throw new IllegalArgumentException("lock line " + (i + 1) + ": a local entry carries no hash");
                }
            } else {
                if (!f[2].matches("[0-9a-f]{64}")) {
                    throw new IllegalArgumentException("lock line " + (i + 1) + ": bad sha256");
                }
                sha = f[2];
                try {
                    bytes = Long.parseLong(f[3]);
                } catch (NumberFormatException e) {
                    throw new IllegalArgumentException("lock line " + (i + 1) + ": bad size");
                }
                if (bytes <= 0) {
                    throw new IllegalArgumentException("lock line " + (i + 1) + ": bad size");
                }
            }
            if (f[4].isEmpty()) {
                throw new IllegalArgumentException("lock line " + (i + 1) + ": missing source");
            }
            out.add(new Entry(f[0], f[1], sha, bytes, f[4]));
        }
        if (out.isEmpty()) {
            throw new IllegalArgumentException("empty lock");
        }
        return new WheelLock(out);
    }

    private static boolean isKind(String s) {
        for (String k : KINDS) {
            if (k.equals(s)) {
                return true;
            }
        }
        return false;
    }

    public List<Entry> entries() {
        return entries;
    }

    public Entry find(String file) {
        return byFile.get(file);
    }
}
