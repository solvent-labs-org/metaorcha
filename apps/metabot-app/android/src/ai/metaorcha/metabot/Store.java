package ai.metaorcha.metabot;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;

public final class Store {
    private static final String PREF = "pairing";
    private final SharedPreferences prefs;

    public Store(Context context) {
        prefs = context.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    public boolean ready() {
        if (token() == null || fingerprint() == null) return false;
        if (port() < 1) return false;
        String[] a = addresses();
        return a.length > 0;
    }

    public String[] addresses() {
        String raw = prefs.getString("addrs", "");
        if (raw == null || raw.length() == 0) return new String[0];
        try {
            JSONArray arr = new JSONArray(raw);
            String[] out = new String[arr.length()];
            int n = 0;
            for (int i = 0; i < arr.length(); i++) {
                String s = arr.optString(i, "");
                if (s != null && s.length() > 0) out[n++] = s;
            }
            if (n == out.length) return out;
            String[] trimmed = new String[n];
            System.arraycopy(out, 0, trimmed, 0, n);
            return trimmed;
        } catch (Exception e) {
            return new String[0];
        }
    }

    public int port() { return prefs.getInt("port", 0); }

    public String fingerprint() {
        String fp = prefs.getString("fp", "");
        if (fp == null || fp.length() != 64) return null;
        return fp;
    }

    public String token() {
        String t = prefs.getString("token", "");
        if (t == null || t.length() == 0) return null;
        return t;
    }

    public void save(String[] addresses, int port, String fingerprint, String token) {
        JSONArray arr = new JSONArray();
        if (addresses != null) {
            for (int i = 0; i < addresses.length; i++) {
                if (addresses[i] != null && addresses[i].length() > 0) arr.put(addresses[i]);
            }
        }
        prefs.edit()
            .putString("addrs", arr.toString())
            .putInt("port", port)
            .putString("fp", fingerprint == null ? "" : fingerprint)
            .putString("token", token == null ? "" : token)
            .apply();
    }

    public void clearToken() {
        prefs.edit().remove("token").apply();
    }
}
