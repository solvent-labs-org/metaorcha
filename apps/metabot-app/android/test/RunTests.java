package ai.metaorcha.metabot;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;

public class RunTests {
    private static int failed;

    public static void main(String[] args) throws Exception {
        boolean mockOnly = args.length == 1 && "--mock-pair".equals(args[0]);
        if (!mockOnly) {
            parseGood();
            parseBad();
            codeAndFingerprint();
            words();
            failover();
            pageUrl();
            wheelLock();
            payloadAssets();
        }
        mockPair();
        if (failed > 0) {
            System.out.println("FAILED " + failed);
            System.exit(1);
        }
        System.out.println("ALL PASS");
    }

    private static void parseGood() {
        String fp = repeat("ab", 32);
        String raw = "metabot-pair:1?a=home.example,vpn.example&p=8443&c=0123abcd&f=" + fp;
        Pairing.Result r = Pairing.parse(raw);
        check("parse good", r.ok());
        check("parse two addresses", r.payload.addresses.length == 2
            && "home.example".equals(r.payload.addresses[0])
            && "vpn.example".equals(r.payload.addresses[1]));
        check("parse port", r.payload.port == 8443);
        check("parse code upper", "0123ABCD".equals(r.payload.code));
        check("parse fp lower", fp.equals(r.payload.fingerprint));
        Pairing.Result upper = Pairing.parse(raw.replace(fp, fp.toUpperCase()));
        check("parse fp case", upper.ok() && fp.equals(upper.payload.fingerprint));
        check("parse alias O", "00000000".equals(Pairing.normalizeCode("oooooooo")));
    }

    private static void parseBad() {
        String fp = repeat("ab", 32);
        String code = "0123abcd";
        checkMsg("parse null", Pairing.parse(null), "That code is not a pairing code.");
        checkMsg("parse empty", Pairing.parse("  "), "That code is not a pairing code.");
        checkMsg("parse junk", Pairing.parse("hello"), "That code is not a pairing code.");
        checkMsg("parse version", Pairing.parse(
            "metabot-pair:2?a=home.example&p=8443&c=" + code + "&f=" + fp),
            "That pairing code is the wrong version.");
        checkMsg("parse missing a", Pairing.parse(
            "metabot-pair:1?p=8443&c=" + code + "&f=" + fp),
            "That pairing code is missing a part.");
        checkMsg("parse missing p", Pairing.parse(
            "metabot-pair:1?a=home.example&c=" + code + "&f=" + fp),
            "That pairing code is missing a part.");
        checkMsg("parse missing c", Pairing.parse(
            "metabot-pair:1?a=home.example&p=8443&f=" + fp),
            "That pairing code is missing a part.");
        checkMsg("parse missing f", Pairing.parse(
            "metabot-pair:1?a=home.example&p=8443&c=" + code),
            "That pairing code is missing a part.");
        checkMsg("parse bad hex", Pairing.parse(
            "metabot-pair:1?a=home.example&p=8443&c=" + code + "&f=" + repeat("ag", 32)),
            "That fingerprint is not valid.");
        checkMsg("parse short hex", Pairing.parse(
            "metabot-pair:1?a=home.example&p=8443&c=" + code + "&f=" + repeat("ab", 31)),
            "That fingerprint is not valid.");
        checkMsg("parse bad code", Pairing.parse(
            "metabot-pair:1?a=home.example&p=8443&c=1234567U&f=" + fp),
            "That code is not valid.");
        checkMsg("parse short code", Pairing.parse(
            "metabot-pair:1?a=home.example&p=8443&c=1234567&f=" + fp),
            "That code is not valid.");
        checkMsg("parse bad port", Pairing.parse(
            "metabot-pair:1?a=home.example&p=0&c=" + code + "&f=" + fp),
            "That port is not valid.");
        checkMsg("parse bad address", Pairing.parse(
            "metabot-pair:1?a=bad host&p=8443&c=" + code + "&f=" + fp),
            "That address is not valid.");
    }

    private static void codeAndFingerprint() {
        byte[] der = new byte[] { 1, 2, 3 };
        String hex = Fingerprint.sha256Hex(der);
        check("fp length", hex != null && hex.length() == 64);
        check("fp match", Fingerprint.matches(hex, der));
        check("fp match upper", Fingerprint.matches(hex.toUpperCase(), der));
        byte[] other = new byte[] { 1, 2, 4 };
        check("fp mismatch", !Fingerprint.matches(hex, other));
        check("fp null", !Fingerprint.matches(null, der));
        check("fp short", !Fingerprint.matches("abcd", der));
        String body = Pairing.claimBody("0123ABCD", "Phone");
        check("claim body", "{\"code\":\"0123ABCD\",\"name\":\"Phone\"}".equals(body));
        check("read token", "abc".equals(Pairing.readToken("{\"token\":\"abc\"}")));
        check("read token spaced", "abc".equals(Pairing.readToken("{ \"token\" : \"abc\" }")));
        check("read token missing", Pairing.readToken("{\"code\":\"x\"}") == null);
        String[] hp = Pairing.splitHostPort("home.example:8443");
        check("host port", hp != null && "home.example".equals(hp[0]) && "8443".equals(hp[1]));
        check("host port bad", Pairing.splitHostPort("home.example") == null);
    }

    private static void words() {
        check("word count", Words.LIST.length == 256);
        HashSet<String> set = new HashSet<String>();
        boolean plain = true;
        for (int i = 0; i < Words.LIST.length; i++) {
            String w = Words.LIST[i];
            set.add(w);
            if (w == null || !w.matches("[a-z]+")) plain = false;
        }
        check("words distinct", set.size() == 256);
        check("words plain", plain);
        String fp = "00010203" + repeat("ab", 30);
        String[] a = Words.four(fp);
        String[] b = Words.four(fp);
        check("words stable", a != null && Arrays.equals(a, b));
        check("words index", a != null
            && Words.LIST[0].equals(a[0])
            && Words.LIST[1].equals(a[1])
            && Words.LIST[2].equals(a[2])
            && Words.LIST[3].equals(a[3]));
        String[] c = Words.four("00010204" + repeat("ab", 30));
        check("words differ", c != null && !a[3].equals(c[3]));
        check("words bad fp", Words.four("zz") == null);
    }

    private static void failover() {
        String[] seq = Failover.sequence(new String[] { "one.example", "", null, "two.example" });
        check("sequence", seq.length == 2
            && "one.example".equals(seq[0]) && "two.example".equals(seq[1]));
        final StringBuilder calls = new StringBuilder();
        String chosen = Failover.select(new String[] { "one.example", "two.example", "three.example" },
            Failover.TIMEOUT_MS, new Failover.Probe() {
                public boolean open(String address, int timeoutMs) {
                    calls.append(address).append(':').append(timeoutMs).append(';');
                    return "two.example".equals(address);
                }
            });
        check("select second", "two.example".equals(chosen));
        check("select stops", "one.example:4000;two.example:4000;".equals(calls.toString()));
        String none = Failover.select(new String[] { "one.example" }, 4000, new Failover.Probe() {
            public boolean open(String address, int timeoutMs) { return false; }
        });
        check("select none", none == null);
        check("select null probe", Failover.select(new String[] { "one.example" }, 4000, null) == null);
    }

    private static void pageUrl() {
        String u = Pairing.pageUrl("home.example", 8443, "tok");
        check("page url", "https://home.example:8443/?t=tok".equals(u));
        check("page url encodes token", "https://h.example:1/?t=a%2Bb%26c".equals(Pairing.pageUrl("h.example", 1, "a+b&c")));
        check("page url ipv6", "https://[fd7a::1]:8443/?t=x".equals(Pairing.pageUrl("fd7a::1", 8443, "x")));
        check("pair url ipv6", "https://[fd7a::1]:8443/api/pair".equals(Pairing.pairUrl("fd7a::1", 8443)));
        check("pair url", "https://home.example:8443/api/pair".equals(Pairing.pairUrl("home.example", 8443)));
    }

    private static void wheelLock() {
        String text = "# the E1 payload lock\n"
            + "\n"
            + "cpython python-3.14.10-aarch64-linux-android.tar.gz " + repeat("a", 64)
            + " 23027305 https://example.test/py.tar.gz\n"
            + "wheel anyio-4.14.2-py3-none-any.whl " + repeat("b", 64) + " 125813 pypi\n"
            + "launcher libmetabot_py.so " + repeat("c", 64) + " 6768 spike-build\n"
            + "local orcha-metabot - - env:MB_CORE_ROOT/apps/metabot\n";
        WheelLock lock = WheelLock.parse(text);
        check("lock entry count", lock.entries().size() == 4);
        WheelLock.Entry w = lock.find("anyio-4.14.2-py3-none-any.whl");
        check("lock find wheel", w != null && "wheel".equals(w.kind)
            && w.bytes == 125813 && "pypi".equals(w.source));
        check("lock wheel sha", w != null && repeat("b", 64).equals(w.sha256));
        WheelLock.Entry c = lock.find("python-3.14.10-aarch64-linux-android.tar.gz");
        check("lock find cpython", c != null && "cpython".equals(c.kind));
        WheelLock.Entry l = lock.find("orcha-metabot");
        check("lock local has no hash", l != null && "local".equals(l.kind) && l.sha256 == null);
        check("lock miss", lock.find("nope-1.0.whl") == null);
        byte[] data = "wheel-bytes".getBytes(StandardCharsets.UTF_8);
        String sha = Fingerprint.sha256Hex(data);
        WheelLock ok = WheelLock.parse("wheel x-1.0-py3-none-any.whl " + sha + " " + data.length + " pypi\n");
        check("lock matches bytes", ok.find("x-1.0-py3-none-any.whl").matches(data));
        check("lock rejects wrong bytes",
            !ok.find("x-1.0-py3-none-any.whl").matches("other".getBytes(StandardCharsets.UTF_8)));
        check("lock rejects wrong size", !ok.find("x-1.0-py3-none-any.whl").matches(
            "wheel-bytes!".getBytes(StandardCharsets.UTF_8)));
        check("lock local never matches", !l.matches(data));
        check("lock dup file", lockThrows("wheel a.whl " + repeat("b", 64) + " 1 pypi\n"
            + "sdist a.whl " + repeat("c", 64) + " 2 pypi\n"));
        check("lock bad kind", lockThrows("thing a.whl " + repeat("b", 64) + " 1 pypi\n"));
        check("lock bad sha", lockThrows("wheel a.whl " + repeat("B", 64) + " 1 pypi\n"));
        check("lock short sha", lockThrows("wheel a.whl abcd 1 pypi\n"));
        check("lock bad bytes", lockThrows("wheel a.whl " + repeat("b", 64) + " x pypi\n"));
        check("lock zero bytes", lockThrows("wheel a.whl " + repeat("b", 64) + " 0 pypi\n"));
        check("lock missing source", lockThrows("wheel a.whl " + repeat("b", 64) + " 1\n"));
        check("lock local with hash", lockThrows("local orcha-metabot " + repeat("b", 64) + " 1 tree\n"));
        check("lock fetched sans hash", lockThrows("wheel a.whl - - pypi\n"));
    }

    private static void payloadAssets() {
        byte[] a = "aaa".getBytes(StandardCharsets.UTF_8);
        byte[] b = "bb".getBytes(StandardCharsets.UTF_8);
        String text = "# what the APK carries under assets/py\n"
            + "stdlib.zip " + Fingerprint.sha256Hex(a) + " 3\n"
            + "wheels/x-1.0-py3-none-any.whl " + Fingerprint.sha256Hex(b) + " 2\n";
        PayloadAssets m = PayloadAssets.parse(text);
        check("manifest verify both", m.verify("stdlib.zip", a) && m.verify("wheels/x-1.0-py3-none-any.whl", b));
        check("manifest wrong bytes", !m.verify("stdlib.zip", b));
        check("manifest wrong size",
            !m.verify("stdlib.zip", "aaaa".getBytes(StandardCharsets.UTF_8)));
        check("manifest unknown path", !m.verify("wheels/nope.whl", a));
        check("manifest entry count", m.entries().size() == 2);
        String sha = Fingerprint.sha256Hex(a);
        check("manifest bad sha", assetsThrows("stdlib.zip zz 3\n"));
        check("manifest bad bytes", assetsThrows("stdlib.zip " + sha + " x\n"));
        check("manifest dup path", assetsThrows("stdlib.zip " + sha + " 3\nstdlib.zip " + sha + " 3\n"));
        check("manifest parent traversal", assetsThrows("../escape " + sha + " 3\n"));
        check("manifest nested traversal", assetsThrows("wheels/../escape " + sha + " 3\n"));
        check("manifest absolute path", assetsThrows("/abs " + sha + " 3\n"));
        check("manifest backslash", assetsThrows("a\\b " + sha + " 3\n"));
        check("manifest empty segment", assetsThrows("a//b " + sha + " 3\n"));
        check("manifest dot segment", assetsThrows("a/./b " + sha + " 3\n"));
        // an empty package marker (__init__.py) is a legitimate payload file
        byte[] empty = new byte[0];
        PayloadAssets zm = PayloadAssets.parse("site/x/__init__.py " + Fingerprint.sha256Hex(empty) + " 0\n");
        check("manifest zero-byte entry verifies", zm.verify("site/x/__init__.py", empty));
        check("manifest zero-byte rejects content", !zm.verify("site/x/__init__.py", a));
        check("manifest negative bytes", assetsThrows("stdlib.zip " + sha + " -1\n"));
    }

    private static boolean lockThrows(String text) {
        try {
            WheelLock.parse(text);
            return false;
        } catch (IllegalArgumentException e) {
            return true;
        }
    }

    private static boolean assetsThrows(String text) {
        try {
            PayloadAssets.parse(text);
            return false;
        } catch (IllegalArgumentException e) {
            return true;
        }
    }

    private static void mockPair() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/pair", new com.sun.net.httpserver.HttpHandler() {
            public void handle(HttpExchange ex) throws java.io.IOException {
                if (!"POST".equals(ex.getRequestMethod())) {
                    ex.sendResponseHeaders(405, -1);
                    ex.close();
                    return;
                }
                byte[] req = readAll(ex.getRequestBody());
                String body = new String(req, StandardCharsets.UTF_8);
                String code = "0123ABCD";
                String name = "sdk_gphone";
                boolean ok = body.equals(Pairing.claimBody(code, name));
                byte[] resp = (ok ? "{\"token\":\"mock-token\"}" : "{\"token\":\"\"}").getBytes(StandardCharsets.UTF_8);
                ex.sendResponseHeaders(ok ? 200 : 400, resp.length);
                OutputStream out = ex.getResponseBody();
                out.write(resp);
                out.close();
            }
        });
        server.start();
        try {
            int port = server.getAddress().getPort();
            String url = "http://127.0.0.1:" + port + "/api/pair";
            String token = Pairing.postJson(url, Pairing.claimBody("0123ABCD", "sdk_gphone"), 4000);
            check("mock-pair", "mock-token".equals(token));
        } finally {
            server.stop(0);
        }
    }

    private static byte[] readAll(InputStream in) throws java.io.IOException {
        try {
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[256];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return bos.toByteArray();
        } finally {
            in.close();
        }
    }

    private static void check(String name, boolean ok) {
        if (ok) System.out.println("PASS " + name);
        else {
            failed++;
            System.out.println("FAIL " + name);
        }
    }

    private static void checkMsg(String name, Pairing.Result result, String message) {
        check(name, result != null && !result.ok() && message.equals(result.message));
    }

    private static String repeat(String s, int n) {
        StringBuilder sb = new StringBuilder(s.length() * n);
        for (int i = 0; i < n; i++) sb.append(s);
        return sb.toString();
    }
}
