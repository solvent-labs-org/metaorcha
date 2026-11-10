package ai.metaorcha.metabot;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.cert.CertificateException;
import java.security.cert.CertificateEncodingException;
import java.security.cert.X509Certificate;
import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public final class Pairing {
    private static final String CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

    private Pairing() {}

    public static final class Payload {
        public final String[] addresses;
        public final int port;
        public final String code;
        public final String fingerprint;

        Payload(String[] addresses, int port, String code, String fingerprint) {
            this.addresses = addresses;
            this.port = port;
            this.code = code;
            this.fingerprint = fingerprint;
        }
    }

    public static final class Result {
        public final Payload payload;
        public final String message;

        private Result(Payload payload, String message) {
            this.payload = payload;
            this.message = message;
        }

        public static Result ok(Payload payload) { return new Result(payload, null); }
        public static Result bad(String message) { return new Result(null, message); }
        public boolean ok() { return payload != null; }
    }

    public static final class Refused extends IOException {
        public Refused(String message) { super(message); }
    }

    public static Result parse(String raw) {
        if (raw == null) return Result.bad("That code is not a pairing code.");
        String text = raw.trim();
        if (text.length() == 0) return Result.bad("That code is not a pairing code.");
        String prefix = "metabot-pair:";
        if (!text.startsWith(prefix)) return Result.bad("That code is not a pairing code.");
        int q = text.indexOf('?');
        String ver = q < 0 ? text.substring(prefix.length()) : text.substring(prefix.length(), q);
        if (!"1".equals(ver)) return Result.bad("That pairing code is the wrong version.");
        if (q < 0 || q == text.length() - 1) return Result.bad("That pairing code is missing a part.");
        String query = text.substring(q + 1);
        String addr = null;
        String port = null;
        String code = null;
        String fp = null;
        String[] parts = query.split("&");
        for (int i = 0; i < parts.length; i++) {
            if (parts[i].length() == 0) return Result.bad("That pairing code is missing a part.");
            int eq = parts[i].indexOf('=');
            if (eq <= 0) return Result.bad("That pairing code is missing a part.");
            String key = parts[i].substring(0, eq);
            String val = decode(parts[i].substring(eq + 1));
            if (val == null) return Result.bad("That code is not a pairing code.");
            if ("a".equals(key)) {
                if (addr != null) return Result.bad("That pairing code is missing a part.");
                addr = val;
            } else if ("p".equals(key)) {
                if (port != null) return Result.bad("That pairing code is missing a part.");
                port = val;
            } else if ("c".equals(key)) {
                if (code != null) return Result.bad("That pairing code is missing a part.");
                code = val;
            } else if ("f".equals(key)) {
                if (fp != null) return Result.bad("That pairing code is missing a part.");
                fp = val;
            } else {
                return Result.bad("That code is not a pairing code.");
            }
        }
        if (addr == null || port == null || code == null || fp == null) {
            return Result.bad("That pairing code is missing a part.");
        }
        String[] addresses = splitAddresses(addr);
        if (addresses == null) return Result.bad("That address is not valid.");
        Integer p = parsePort(port);
        if (p == null) return Result.bad("That port is not valid.");
        String norm = normalizeCode(code);
        if (norm == null) return Result.bad("That code is not valid.");
        String hex = normalizeFp(fp);
        if (hex == null) return Result.bad("That fingerprint is not valid.");
        return Result.ok(new Payload(addresses, p.intValue(), norm, hex));
    }

    public static String normalizeCode(String raw) {
        if (raw == null) return null;
        String s = raw.trim().toUpperCase();
        if (s.length() != 8) return null;
        StringBuilder sb = new StringBuilder(8);
        for (int i = 0; i < 8; i++) {
            char c = s.charAt(i);
            if (c == 'I' || c == 'L') c = '1';
            else if (c == 'O') c = '0';
            if (CROCKFORD.indexOf(c) < 0) return null;
            sb.append(c);
        }
        return sb.toString();
    }

    public static String normalizeFp(String raw) {
        byte[] bytes = Fingerprint.decodeHex(raw);
        if (bytes == null || bytes.length != 32) return null;
        StringBuilder sb = new StringBuilder(64);
        for (int i = 0; i < bytes.length; i++) {
            sb.append(Character.forDigit((bytes[i] >> 4) & 0xf, 16));
            sb.append(Character.forDigit(bytes[i] & 0xf, 16));
        }
        return sb.toString();
    }

    public static String[] splitHostPort(String address) {
        if (address == null) return null;
        String s = address.trim();
        int c = s.lastIndexOf(':');
        if (c <= 0 || c == s.length() - 1) return null;
        String host = s.substring(0, c).trim();
        String port = s.substring(c + 1).trim();
        if (!validAddress(host) || parsePort(port) == null) return null;
        return new String[] { host, port };
    }

    public static String claimBody(String code, String name) {
        StringBuilder sb = new StringBuilder();
        sb.append('{');
        sb.append('"').append("code").append('"').append(':');
        sb.append('"').append(escape(code)).append('"');
        sb.append(',');
        sb.append('"').append("name").append('"').append(':');
        sb.append('"').append(escape(name)).append('"');
        sb.append('}');
        return sb.toString();
    }

    public static String readToken(String json) {
        if (json == null) return null;
        String key = "\"token\"";
        int i = json.indexOf(key);
        if (i < 0) return null;
        int j = i + key.length();
        while (j < json.length() && (json.charAt(j) == ' ' || json.charAt(j) == '\n' || json.charAt(j) == '\t')) j++;
        if (j >= json.length() || json.charAt(j) != ':') return null;
        j++;
        while (j < json.length() && (json.charAt(j) == ' ' || json.charAt(j) == '\n' || json.charAt(j) == '\t')) j++;
        if (j >= json.length() || json.charAt(j) != '"') return null;
        j++;
        StringBuilder sb = new StringBuilder();
        while (j < json.length()) {
            char c = json.charAt(j);
            if (c == '"') return sb.toString();
            if (c == '\\' && j + 1 < json.length()) {
                char n = json.charAt(j + 1);
                if (n == '"' || n == '\\') sb.append(n);
                else if (n == 'n') sb.append('\n');
                else sb.append(n);
                j += 2;
                continue;
            }
            sb.append(c);
            j++;
        }
        return null;
    }

    public static String pageUrl(String address, int port, String token) {
        String t = token == null ? "" : token;
        try {
            t = java.net.URLEncoder.encode(t, "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
        return "https://" + hostPart(address) + ":" + port + "/?t=" + t;
    }

    /** IPv6 literals need brackets inside a URL. */
    static String hostPart(String address) {
        return address.indexOf(':') >= 0 && !address.startsWith("[") ? "[" + address + "]" : address;
    }

    public static String pairUrl(String address, int port) {
        StringBuilder sb = new StringBuilder();
        sb.append("https://");
        sb.append(hostPart(address));
        sb.append(':');
        sb.append(port);
        sb.append("/api/pair");
        return sb.toString();
    }

    public static String postJson(String url, String body, int timeoutMs) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        if (c instanceof HttpsURLConnection) {
            throw new IOException("pinned connection required");
        }
        try {
            return exchange(c, body, timeoutMs);
        } finally {
            c.disconnect();
        }
    }

    public static String postPinned(String url, String body, int timeoutMs, String fingerprint) throws IOException {
        HttpsURLConnection c = (HttpsURLConnection) new URL(url).openConnection();
        try {
            SSLContext sc = SSLContext.getInstance("TLS");
            sc.init(null, new TrustManager[] { trust(fingerprint, null, true) }, null);
            c.setSSLSocketFactory(sc.getSocketFactory());
            c.setHostnameVerifier(new HostnameVerifier() {
                public boolean verify(String host, javax.net.ssl.SSLSession session) { return true; }
            });
            return exchange(c, body, timeoutMs);
        } catch (GeneralSecurityException e) {
            throw new IOException("This isn't your computer");
        } finally {
            c.disconnect();
        }
    }

    /** Read the peer certificate and return its SHA-256. Does not send a pairing code. */
    public static String peekFingerprint(String url, int timeoutMs) throws IOException {
        final byte[][] captured = new byte[1][];
        HttpsURLConnection c = (HttpsURLConnection) new URL(url).openConnection();
        try {
            SSLContext sc = SSLContext.getInstance("TLS");
            sc.init(null, new TrustManager[] { trust(null, captured, false) }, null);
            c.setSSLSocketFactory(sc.getSocketFactory());
            c.setHostnameVerifier(new HostnameVerifier() {
                public boolean verify(String host, javax.net.ssl.SSLSession session) { return true; }
            });
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            c.setInstanceFollowRedirects(false);
            try {
                c.connect();
            } catch (IOException e) {
                if (captured[0] == null) throw e;
            }
        } catch (GeneralSecurityException e) {
            throw new IOException("Couldn't reach your computer");
        } finally {
            c.disconnect();
        }
        if (captured[0] == null) throw new IOException("Couldn't reach your computer");
        return Fingerprint.sha256Hex(captured[0]);
    }

    private static X509TrustManager trust(final String fingerprint, final byte[][] captured, final boolean acceptMatch) {
        return new X509TrustManager() {
            public void checkClientTrusted(X509Certificate[] chain, String auth) {}

            public void checkServerTrusted(X509Certificate[] chain, String auth) throws CertificateException {
                if (chain == null || chain.length == 0 || chain[0] == null) {
                    throw new CertificateException("This isn't your computer");
                }
                byte[] der;
                try {
                    der = chain[0].getEncoded();
                } catch (CertificateEncodingException e) {
                    throw new CertificateException("This isn't your computer");
                }
                if (captured != null) captured[0] = der;
                if (!acceptMatch) throw new CertificateException("unconfirmed");
                if (!Fingerprint.matches(fingerprint, der)) {
                    throw new CertificateException("This isn't your computer");
                }
            }

            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        };
    }

    private static String exchange(HttpURLConnection c, String body, int timeoutMs) throws IOException {
        c.setConnectTimeout(timeoutMs);
        c.setReadTimeout(timeoutMs);
        c.setInstanceFollowRedirects(false);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        c.setFixedLengthStreamingMode(bytes.length);
        OutputStream out = c.getOutputStream();
        try {
            out.write(bytes);
        } finally {
            out.close();
        }
        int status = c.getResponseCode();
        InputStream in = status == 200 ? c.getInputStream() : c.getErrorStream();
        String text = readLimited(in);
        if (status != 200) throw new Refused("Pairing was refused");
        String token = readToken(text);
        if (token == null || token.length() == 0) throw new Refused("Pairing did not return a token");
        return token;
    }

    private static String readLimited(InputStream in) throws IOException {
        if (in == null) return "";
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            int n;
            int total = 0;
            while ((n = in.read(buf)) > 0 && total < 8192) {
                int take = n;
                if (total + take > 8192) take = 8192 - total;
                bos.write(buf, 0, take);
                total += take;
            }
            return bos.toString(StandardCharsets.UTF_8.name());
        } finally {
            in.close();
        }
    }

    static String escape(String s) {
        if (s == null) s = "";
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' || c == '"') sb.append('\\').append(c);
            else if (c == '\n') sb.append('\\').append('n');
            else if (c < 0x20) sb.append(' ');
            else sb.append(c);
        }
        return sb.toString();
    }

    private static String[] splitAddresses(String raw) {
        String[] bits = raw.split(",", -1);
        if (bits.length == 0) return null;
        String[] out = new String[bits.length];
        for (int i = 0; i < bits.length; i++) {
            String a = bits[i].trim();
            if (!validAddress(a)) return null;
            out[i] = a;
        }
        return out;
    }

    private static boolean validAddress(String a) {
        if (a == null || a.length() == 0 || a.length() > 253) return false;
        for (int i = 0; i < a.length(); i++) {
            char c = a.charAt(i);
            boolean ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9') || c == '.' || c == '-' || c == ':' || c == '[' || c == ']';
            if (!ok) return false;
        }
        return true;
    }

    private static Integer parsePort(String raw) {
        if (raw == null || raw.length() == 0 || raw.length() > 5) return null;
        int n = 0;
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (c < '0' || c > '9') return null;
            n = n * 10 + (c - '0');
        }
        if (n < 1 || n > 65535) return null;
        return Integer.valueOf(n);
    }

    private static String decode(String s) {
        try {
            return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8.name());
        } catch (Exception e) {
            return null;
        }
    }
}
