package ai.metaorcha.metabot;

/** Address order is the saved order. The first address that opens wins. */
public final class Failover {
    public static final int TIMEOUT_MS = 4000;

    public interface Probe {
        boolean open(String address, int timeoutMs);
    }

    private Failover() {}

    public static String[] sequence(String[] addresses) {
        if (addresses == null) return new String[0];
        int n = 0;
        for (int i = 0; i < addresses.length; i++) {
            if (addresses[i] != null && addresses[i].length() > 0) n++;
        }
        String[] out = new String[n];
        int j = 0;
        for (int i = 0; i < addresses.length; i++) {
            if (addresses[i] != null && addresses[i].length() > 0) out[j++] = addresses[i];
        }
        return out;
    }

    public static String select(String[] addresses, int timeoutMs, Probe probe) {
        if (probe == null) return null;
        String[] seq = sequence(addresses);
        for (int i = 0; i < seq.length; i++) {
            if (probe.open(seq[i], timeoutMs)) return seq[i];
        }
        return null;
    }
}
