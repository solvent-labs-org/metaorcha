package ai.metaorcha.metabot;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.hardware.Camera;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.NotFoundException;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.common.HybridBinarizer;
import java.io.IOException;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

public class PairActivity extends Activity implements SurfaceHolder.Callback {
    private static final int REQ_CAMERA = 1;
    private WebView web;
    private SurfaceView preview;
    private Button stop;
    private Camera camera;
    private final AtomicBoolean scanning = new AtomicBoolean(false);
    private final AtomicBoolean found = new AtomicBoolean(false);
    private String[] pendingAddrs;
    private int pendingPort;
    private String pendingCode;
    private String pendingFp;
    private boolean busy;
    // The scripted walk (tools/tablet-e2e.sh): in a debuggable build only, intent extras
    // "address" and "code" type for the tester once the page has loaded, and "confirm"
    // says Yes to the words. The words are logged (they derive from the public
    // fingerprint). A release build ignores the extras.
    private static final String TAG = "MetaBot";
    static final String EXTRA_ADDRESS = "address";
    static final String EXTRA_CODE = "code";
    static final String EXTRA_CONFIRM = "confirm";

    /** The scripted-walk intent for a debuggable build whose launcher intent carries the
     *  extras (the pairing screen itself is not exported, so the walk enters through the
     *  launcher activity). Null for a release build or a plain launch. */
    static Intent hookFrom(android.content.Context ctx, Intent in) {
        if (in == null) return null;
        if ((ctx.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) == 0) return null;
        String address = in.getStringExtra(EXTRA_ADDRESS);
        String code = in.getStringExtra(EXTRA_CODE);
        if (address == null || code == null) return null;
        Intent out = new Intent(ctx, PairActivity.class);
        out.putExtra(EXTRA_ADDRESS, address);
        out.putExtra(EXTRA_CODE, code);
        out.putExtra(EXTRA_CONFIRM, in.getBooleanExtra(EXTRA_CONFIRM, false));
        return out;
    }
    private String hookAddress;
    private String hookCode;
    private boolean hookConfirm;
    private boolean hooked;

    @Override
    public void onCreate(Bundle saved) {
        super.onCreate(saved);
        FrameLayout root = new FrameLayout(this);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        web.addJavascriptInterface(new Bridge(), "Pair");
        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            Intent in = getIntent();
            hookAddress = in.getStringExtra(EXTRA_ADDRESS);
            hookCode = in.getStringExtra(EXTRA_CODE);
            hookConfirm = in.getBooleanExtra(EXTRA_CONFIRM, false);
        }
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                if (hookAddress != null && hookCode != null && !hooked) {
                    hooked = true;
                    Log.i(TAG, "pair hook: typing the address and the code");
                    onTyped(hookAddress, hookCode);
                }
            }
        });
        web.loadUrl("file:///android_asset/pair.html");
        preview = new SurfaceView(this);
        preview.setVisibility(View.GONE);
        preview.getHolder().addCallback(this);
        stop = new Button(this);
        stop.setText("Stop");
        stop.setVisibility(View.GONE);
        stop.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { stopScan(); }
        });
        root.addView(web, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(preview, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        FrameLayout.LayoutParams stopLp = new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        stopLp.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
        stopLp.bottomMargin = 48;
        root.addView(stop, stopLp);
        root.setBackgroundColor(Color.BLACK);
        setContentView(root);
    }

    @Override
    public void onPause() {
        stopScan();
        super.onPause();
    }

    private void startScan() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] { Manifest.permission.CAMERA }, REQ_CAMERA);
            return;
        }
        found.set(false);
        scanning.set(true);
        preview.setVisibility(View.VISIBLE);
        stop.setVisibility(View.VISIBLE);
        if (preview.getHolder().getSurface() != null && preview.getHolder().getSurface().isValid()) {
            openCamera();
        }
    }

    private void stopScan() {
        scanning.set(false);
        releaseCamera();
        if (preview != null) preview.setVisibility(View.GONE);
        if (stop != null) stop.setVisibility(View.GONE);
    }

    @Override
    public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        if (request == REQ_CAMERA && results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            startScan();
        } else if (request == REQ_CAMERA) {
            show("The camera is off. Type the address and the code instead.");
        }
    }

    public void surfaceCreated(SurfaceHolder holder) {
        if (scanning.get()) openCamera();
    }

    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {}

    public void surfaceDestroyed(SurfaceHolder holder) {
        releaseCamera();
    }

    private void openCamera() {
        if (camera != null || !scanning.get()) return;
        try {
            camera = Camera.open();
            if (camera == null) {
                show("The camera did not open. Type the address and the code instead.");
                stopScan();
                return;
            }
            Camera.Parameters params = camera.getParameters();
            Camera.Size size = chooseSize(params.getSupportedPreviewSizes());
            if (size != null) params.setPreviewSize(size.width, size.height);
            params.setPreviewFormat(android.graphics.ImageFormat.NV21);
            List<String> focus = params.getSupportedFocusModes();
            if (focus != null && focus.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO)) {
                params.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO);
            }
            camera.setParameters(params);
            camera.setPreviewDisplay(preview.getHolder());
            camera.setPreviewCallback(new Camera.PreviewCallback() {
                public void onPreviewFrame(byte[] data, Camera cam) {
                    decodeFrame(data, cam);
                }
            });
            camera.startPreview();
        } catch (Exception e) {
            releaseCamera();
            show("The camera did not open. Type the address and the code instead.");
            stopScan();
        }
    }

    private static Camera.Size chooseSize(List<Camera.Size> sizes) {
        if (sizes == null || sizes.isEmpty()) return null;
        Camera.Size best = sizes.get(0);
        int bestDelta = Integer.MAX_VALUE;
        for (int i = 0; i < sizes.size(); i++) {
            Camera.Size s = sizes.get(i);
            int d = Math.abs(s.width - 640) + Math.abs(s.height - 480);
            if (d < bestDelta) {
                best = s;
                bestDelta = d;
            }
        }
        return best;
    }

    private void decodeFrame(byte[] data, Camera cam) {
        if (!scanning.get() || found.get() || data == null || cam == null) return;
        try {
            Camera.Size size = cam.getParameters().getPreviewSize();
            if (size == null) return;
            PlanarYUVLuminanceSource src = new PlanarYUVLuminanceSource(
                data, size.width, size.height, 0, 0, size.width, size.height, false);
            com.google.zxing.Result result = new MultiFormatReader().decode(
                new BinaryBitmap(new HybridBinarizer(src)));
            if (result != null && found.compareAndSet(false, true)) {
                final String text = result.getText();
                runOnUiThread(new Runnable() {
                    public void run() {
                        stopScan();
                        onScanned(text);
                    }
                });
            }
        } catch (NotFoundException e) {
            // keep looking
        } catch (Exception e) {
            // a bad frame is not fatal
        }
    }

    private void releaseCamera() {
        Camera cam = camera;
        camera = null;
        if (cam == null) return;
        try {
            cam.setPreviewCallback(null);
            cam.stopPreview();
        } catch (Exception e) {
            // already stopped
        }
        try {
            cam.release();
        } catch (Exception e) {
            // already released
        }
    }

    private void onScanned(String text) {
        Pairing.Result result = Pairing.parse(text);
        if (!result.ok()) {
            show(result.message);
            return;
        }
        pendingAddrs = result.payload.addresses;
        pendingPort = result.payload.port;
        pendingCode = result.payload.code;
        pendingFp = result.payload.fingerprint;
        showWords(Words.four(pendingFp));
    }

    private void onTyped(String address, String code) {
        if (busy) return;
        String norm = Pairing.normalizeCode(code);
        if (norm == null) {
            show("That code is not valid.");
            return;
        }
        String[] hostPort = Pairing.splitHostPort(address);
        if (hostPort == null) {
            show("Type the address and its port.");
            return;
        }
        pendingAddrs = new String[] { hostPort[0] };
        pendingPort = Integer.parseInt(hostPort[1]);
        pendingCode = norm;
        pendingFp = null;
        busy = true;
        show("Looking at that computer.");
        final String url = "https://" + hostPort[0] + ":" + hostPort[1] + "/";
        new Thread(new Runnable() {
            public void run() {
                try {
                    final String fp = Pairing.peekFingerprint(url, Failover.TIMEOUT_MS);
                    runOnUiThread(new Runnable() {
                        public void run() {
                            busy = false;
                            pendingFp = fp;
                            showWords(Words.four(fp));
                        }
                    });
                } catch (IOException e) {
                    runOnUiThread(new Runnable() {
                        public void run() {
                            busy = false;
                            show("Couldn't reach your computer.");
                        }
                    });
                }
            }
        }).start();
    }

    private void onConfirm(boolean yes) {
        if (!yes) {
            clearPending();
            show("This isn't your computer");
            return;
        }
        if (pendingFp == null || pendingCode == null || pendingAddrs == null || busy) {
            show("Scan the code, or type the address and the code.");
            return;
        }
        busy = true;
        show("Pairing.");
        final String[] addrs = pendingAddrs;
        final int port = pendingPort;
        final String code = pendingCode;
        final String fp = pendingFp;
        final String name = Build.MODEL == null ? "Android" : Build.MODEL;
        new Thread(new Runnable() {
            public void run() {
                final String[] token = new String[1];
                final String[] message = new String[1];
                String chosen = Failover.select(addrs, Failover.TIMEOUT_MS, new Failover.Probe() {
                    public boolean open(String address, int timeoutMs) {
                        try {
                            token[0] = Pairing.postPinned(
                                Pairing.pairUrl(address, port),
                                Pairing.claimBody(code, name),
                                timeoutMs,
                                fp);
                            return true;
                        } catch (Pairing.Refused e) {
                            message[0] = e.getMessage();
                            return true;
                        } catch (javax.net.ssl.SSLException e) {
                            message[0] = "This isn't your computer";
                            return true;
                        } catch (IOException e) {
                            return false;
                        }
                    }
                });
                final String picked = chosen;
                runOnUiThread(new Runnable() {
                    public void run() {
                        busy = false;
                        if (token[0] != null && token[0].length() > 0) {
                            new Store(PairActivity.this).save(addrs, port, fp, token[0]);
                            startActivity(new Intent(PairActivity.this, MainActivity.class));
                            finish();
                            return;
                        }
                        clearPending();
                        if (picked == null) show("Couldn't reach your computer.");
                        else if (message[0] != null) show(message[0]);
                        else show("Pairing was refused");
                    }
                });
            }
        }).start();
    }

    private void clearPending() {
        pendingAddrs = null;
        pendingCode = null;
        pendingFp = null;
        pendingPort = 0;
    }

    private void show(final String message) {
        if (hooked) Log.i(TAG, "pair message: " + message);
        runOnUiThread(new Runnable() {
            public void run() {
                if (web == null) return;
                web.evaluateJavascript("showMsg(" + quote(message) + ")", null);
            }
        });
    }

    private void showWords(String[] words) {
        if (words == null) {
            show("That fingerprint is not valid.");
            return;
        }
        StringBuilder sb = new StringBuilder();
        sb.append('[');
        for (int i = 0; i < words.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(quote(words[i]));
        }
        sb.append(']');
        web.evaluateJavascript("showWords(" + sb + ")", null);
        if (hooked) {
            StringBuilder plain = new StringBuilder();
            for (int i = 0; i < words.length; i++) {
                if (i > 0) plain.append(' ');
                plain.append(words[i]);
            }
            Log.i(TAG, "pair words: " + plain);
            if (hookConfirm) onConfirm(true);
        }
    }

    private static String quote(String s) {
        if (s == null) s = "";
        StringBuilder sb = new StringBuilder(s.length() + 2);
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' || c == '"') sb.append('\\').append(c);
            else if (c == '\n') sb.append('\\').append('n');
            else sb.append(c);
        }
        sb.append('"');
        return sb.toString();
    }

    private void scanFromBridge() {
        runOnUiThread(new Runnable() {
            public void run() { startScan(); }
        });
    }

    private void typedFromBridge(final String address, final String code) {
        runOnUiThread(new Runnable() {
            public void run() { onTyped(address, code); }
        });
    }

    private void confirmFromBridge(final boolean yes) {
        runOnUiThread(new Runnable() {
            public void run() { onConfirm(yes); }
        });
    }

    private final class Bridge {
        @JavascriptInterface
        public void scan() { scanFromBridge(); }

        @JavascriptInterface
        public void typed(String address, String code) { typedFromBridge(address, code); }

        @JavascriptInterface
        public void confirm(boolean yes) { confirmFromBridge(yes); }
    }
}
