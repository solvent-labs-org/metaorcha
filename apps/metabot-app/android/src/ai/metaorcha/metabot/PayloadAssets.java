package ai.metaorcha.metabot;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The payload manifest (assets/py/payload.list): what the APK carries under
 * assets/py, one {@code <path> <sha256> <bytes>} per line, generated at build
 * time. The first-run unpacker verifies every file against it before a byte is
 * used. A path is always relative and clean: no leading slash, no backslash, no
 * empty, "." or ".." segments — the manifest never walks out of the payload dir.
 * A size of zero is allowed (an empty {@code __init__.py} is a real payload file).
 */
public final class PayloadAssets {
    public static final class Entry {
        public final String path;
        public final String sha256;
        public final long bytes;

        Entry(String path, String sha256, long bytes) {
            this.path = path;
            this.sha256 = sha256;
            this.bytes = bytes;
        }
    }

    private final List<Entry> entries;
    private final Map<String, Entry> byPath;

    private PayloadAssets(List<Entry> entries) {
        this.entries = Collections.unmodifiableList(entries);
        this.byPath = new LinkedHashMap<String, Entry>();
        for (Entry e : entries) {
            byPath.put(e.path, e);
        }
    }

    public static PayloadAssets parse(String text) {
        if (text == null) {
            throw new IllegalArgumentException("empty manifest");
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
            if (f.length != 3) {
                throw new IllegalArgumentException("manifest line " + (i + 1) + ": want 3 fields");
            }
            if (!cleanPath(f[0])) {
                throw new IllegalArgumentException("manifest line " + (i + 1) + ": bad path");
            }
            if (seen.put(f[0], Boolean.TRUE) != null) {
                throw new IllegalArgumentException("manifest line " + (i + 1) + ": duplicate path");
            }
            if (!f[1].matches("[0-9a-f]{64}")) {
                throw new IllegalArgumentException("manifest line " + (i + 1) + ": bad sha256");
            }
            long bytes;
            try {
                bytes = Long.parseLong(f[2]);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("manifest line " + (i + 1) + ": bad size");
            }
            if (bytes < 0) {
                throw new IllegalArgumentException("manifest line " + (i + 1) + ": bad size");
            }
            out.add(new Entry(f[0], f[1], bytes));
        }
        if (out.isEmpty()) {
            throw new IllegalArgumentException("empty manifest");
        }
        return new PayloadAssets(out);
    }

    private static boolean cleanPath(String p) {
        if (p.isEmpty() || p.startsWith("/") || p.indexOf('\\') >= 0) {
            return false;
        }
        for (String seg : p.split("/", -1)) {
            if (seg.isEmpty() || ".".equals(seg) || "..".equals(seg)) {
                return false;
            }
        }
        return true;
    }

    public List<Entry> entries() {
        return entries;
    }

    public Entry find(String path) {
        return byPath.get(path);
    }

    /** The file at {@code path} is exactly what the build pinned: size and SHA-256. */
    public boolean verify(String path, byte[] data) {
        Entry e = byPath.get(path);
        if (e == null || data == null || data.length != e.bytes) {
            return false;
        }
        return e.sha256.equals(Fingerprint.sha256Hex(data));
    }
}
