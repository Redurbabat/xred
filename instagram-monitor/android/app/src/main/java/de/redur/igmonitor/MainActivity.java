package de.redur.igmonitor;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Startet den eingebetteten Python-Server (Flask + Instaloader) und zeigt
 * die bekannte Web-Oberflaeche in einer WebView – alles direkt auf dem
 * Handy, ganz ohne PC.
 */
public class MainActivity extends Activity {

    private static final String URL = "http://127.0.0.1:8756/";
    private static final int FILE_CHOOSER_REQUEST = 4711;
    private WebView webView;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private int retries = 0;
    private ValueCallback<Uri[]> filePathCallback;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // „Alle Dateien"-Zugriff erbitten: Damit landen Downloads sichtbar
        // unter Pictures/InstagramMonitor/<konto> (Galerie, TeraBox-Backup).
        // Ohne die Freigabe nutzt die App ihren privaten Speicher weiter.
        if (android.os.Build.VERSION.SDK_INT >= 30
                && !Environment.isExternalStorageManager()) {
            Toast.makeText(this,
                    "Bitte Dateizugriff erlauben – dann landen Fotos sichtbar "
                            + "im Ordner Pictures/InstagramMonitor",
                    Toast.LENGTH_LONG).show();
            try {
                Intent perm = new Intent(
                        android.provider.Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        Uri.parse("package:" + getPackageName()));
                startActivity(perm);
            } catch (Exception ignored) { }
        }

        // Python-Server im Hintergrund starten (idempotent).
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        String baseDir = getExternalFilesDir(null) != null
                ? getExternalFilesDir(null).getAbsolutePath()
                : getFilesDir().getAbsolutePath();
        Python.getInstance().getModule("android_main")
                .callAttr("start_server", baseDir);

        // Hintergrund-Dienst starten: haelt den Server am Leben, wenn die App
        // im Hintergrund ist oder das Display aus ist (Foreground Service mit
        // dauerhafter Benachrichtigung + Wake-Lock).
        try {
            Intent svc = new Intent(this, ServerService.class);
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                startForegroundService(svc);
            } else {
                startService(svc);
            }
        } catch (Exception ignored) { }

        // Ab Android 13 die Benachrichtigungs-Erlaubnis erfragen (sonst zeigt
        // der Dienst seine Pflicht-Benachrichtigung nicht an).
        if (android.os.Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            try {
                requestPermissions(
                        new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 5001);
            } catch (Exception ignored) { }
        }

        // Einmalig bitten, die App von der Akku-Optimierung auszunehmen –
        // sonst drosselt Android den Hintergrund-Dienst nach einer Weile.
        try {
            android.os.PowerManager pm =
                    (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (android.os.Build.VERSION.SDK_INT >= 23 && pm != null
                    && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                startActivity(new Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getPackageName())));
            }
        } catch (Exception ignored) { }

        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);

        // "⤓ Geraet"-Downloads in den oeffentlichen Download-Ordner legen.
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, length) -> {
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                req.setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                dm.enqueue(req);
                Toast.makeText(this, "Download gestartet: " + name, Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "Download fehlgeschlagen", Toast.LENGTH_SHORT).show();
            }
        });

        // Datei-Auswahl ermoeglichen (z. B. Instagram-Export-ZIP importieren):
        // Eine WebView oeffnet <input type="file"> nur, wenn die App den
        // Dialog selbst startet.
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                    startActivityForResult(
                            Intent.createChooser(intent, "Datei auswählen"),
                            FILE_CHOOSER_REQUEST);
                } catch (Exception e) {
                    filePathCallback = null;
                    Toast.makeText(MainActivity.this,
                            "Kein Datei-Manager gefunden", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }
        });

        // Beim allerersten Start braucht der Server einen Moment – bei
        // Verbindungsfehlern einfach erneut versuchen.
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame() && retries < 30) {
                    retries++;
                    handler.postDelayed(() -> view.loadUrl(URL), 1000);
                }
            }
        });

        setContentView(webView);
        handler.postDelayed(() -> webView.loadUrl(URL), 1200);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_CHOOSER_REQUEST) {
            if (filePathCallback != null) {
                filePathCallback.onReceiveValue(
                        WebChromeClient.FileChooserParams.parseResult(resultCode, data));
                filePathCallback = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
