package ai.metaorcha.metabot;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public class MainActivity extends Activity {
    private static final int MENU_CHANGE = 1;
    private Store store;
    private WebView web;
    private String[] order = new String[0];
    private int index;
    private int generation;
    private boolean waiting;
    private boolean committed;
    private String currentHost = "";

    @Override
    public void onCreate(Bundle saved) {
        super.onCreate(saved);
        store = new Store(this);
        Intent hook = PairActivity.hookFrom(this, getIntent());
        if (hook != null) {   // the scripted walk (debuggable builds only): straight to pairing
            startActivity(hook);
            finish();
            return;
        }
        if (!store.ready()) {
            startActivity(new Intent(this, PairActivity.class));
            finish();
            return;
        }
        web = new WebView(this);
        web.setSaveEnabled(false);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.setWebViewClient(new Client());
        setContentView(web);
        begin();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        MenuItem item = menu.add(0, MENU_CHANGE, 0, "Change computer");
        item.setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == MENU_CHANGE) {
            changeComputer();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void begin() {
        order = Failover.sequence(store.addresses());
        index = 0;
        attempt();
    }

    private void attempt() {
        if (web == null) return;
        if (index >= order.length) {
            waiting = false;
            showPage("Can't reach your computer.", true);
            return;
        }
        waiting = true;
        committed = false;
        final int gen = ++generation;
        currentHost = order[index];
        web.loadUrl(Pairing.pageUrl(currentHost, store.port(), store.token()));
        web.postDelayed(new Runnable() {
            public void run() {
                if (gen == generation) failThis();
            }
        }, Failover.TIMEOUT_MS);
    }

    private void failThis() {
        if (!waiting || committed) return;
        waiting = false;
        index++;
        attempt();
    }

    private void changeComputer() {
        waiting = false;
        committed = true;
        generation++;
        startActivity(new Intent(this, PairActivity.class));
        finish();
    }

    private void showPage(String message, boolean retry) {
        StringBuilder sb = new StringBuilder();
        sb.append("<html><head><meta name=viewport content='width=device-width,initial-scale=1'>");
        sb.append("<style>body{background:#17171A;color:#F5F5F7;font-family:sans-serif;");
        sb.append("display:flex;flex-direction:column;align-items:center;justify-content:center;");
        sb.append("height:100vh;margin:0;text-align:center;padding:24px}");
        sb.append("a{color:#F5F5F7;background:#0A84FF;padding:14px 18px;border-radius:10px;");
        sb.append("text-decoration:none;margin:8px;display:inline-block}</style></head><body><p>");
        sb.append(message);
        sb.append("</p><p>");
        if (retry) sb.append("<a href='metabot://retry'>Try again</a>");
        sb.append("<a href='metabot://change'>Change computer</a></p></body></html>");
        web.loadDataWithBaseURL(null, sb.toString(), "text/html", "utf-8", null);
    }

    private boolean isAppHost(Uri u) {
        if (u == null) return true;
        String scheme = u.getScheme();
        if (scheme == null) return true;
        if ("about".equals(scheme) || "data".equals(scheme) || "blob".equals(scheme) || "javascript".equals(scheme)) {
            return true;
        }
        if (!"http".equals(scheme) && !"https".equals(scheme)) return false;
        String host = u.getHost();
        return host != null && host.equalsIgnoreCase(currentHost);
    }

    private void openOutside(Uri u) {
        try {
            Intent i = new Intent(Intent.ACTION_VIEW, u);
            i.addCategory(Intent.CATEGORY_BROWSABLE);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (ActivityNotFoundException e) {
            // the page stays put
        }
    }

    private final class Client extends WebViewClient {
        @Override
        public void onPageCommitVisible(WebView view, String url) {
            if (url != null && url.startsWith("https")) committed = true;
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            byte[] der = null;
            try {
                if (error != null && error.getCertificate() != null) {
                    android.os.Bundle bundle = SslCertificate.saveState(error.getCertificate());
                    if (bundle != null) der = bundle.getByteArray("x509-certificate");
                }
            } catch (Exception e) {
                der = null;
            }
            if (Fingerprint.matches(store.fingerprint(), der)) {
                handler.proceed();
            } else {
                handler.cancel();
                waiting = false;
                committed = true;
                generation++;
                showPage("This isn't your computer", false);
            }
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request != null && request.isForMainFrame() && response != null && response.getStatusCode() == 403) {
                store.clearToken();
                waiting = false;
                committed = true;
                generation++;
                startActivity(new Intent(MainActivity.this, PairActivity.class));
                finish();
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request != null && request.isForMainFrame()) failThis();
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (u != null && "metabot".equals(u.getScheme())) {
                if ("retry".equals(u.getHost())) begin();
                else if ("change".equals(u.getHost())) changeComputer();
                return true;
            }
            if (isAppHost(u)) return false;
            openOutside(u);
            return true;
        }
    }

}
