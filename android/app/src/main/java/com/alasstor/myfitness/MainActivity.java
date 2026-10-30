package com.alasstor.myfitness;

import android.app.Activity;
import android.Manifest;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.TextUtils;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.security.SecureRandom;

/**
 * Garmin Local en el móvil: arranca el mismo código Python que en el PC dentro de la app
 * y muestra su página (index.html) a pantalla completa.
 */
public class MainActivity extends Activity {
    private static final int PICK_FILE = 1;
    private static final String TOKEN = newToken();
    private static volatile int port = 0;

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;

    private static String newToken() {
        byte[] b = new byte[24];
        new SecureRandom().nextBytes(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    private static boolean isLocal(Uri u) {
        return "127.0.0.1".equals(u.getHost());
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);

        // Puente con la página: al empezar una descarga, arranca el servicio que la mantiene viva
        web.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void syncStarted() {
                runOnUiThread(MainActivity.this::startSyncService);
            }
        }, "MyFitnessApp");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (isLocal(u)) return false;
                // Enlaces externos (Garmin Connect, etc.): en el navegador del móvil
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (ActivityNotFoundException e) {
                    Toast.makeText(MainActivity.this, "No hay navegador para abrir el enlace", Toast.LENGTH_SHORT).show();
                }
                return true;
            }
        });

        // Selector de archivos para «Importar…»
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                try {
                    startActivityForResult(Intent.createChooser(i, "Elige el archivo"), PICK_FILE);
                    return true;
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
            }
        });

        // Descargas (copia de seguridad, archivos de actividad): a la carpeta Descargas
        web.setDownloadListener((url, userAgent, disposition, mime, length) -> {
            try {
                String name = URLUtil.guessFileName(url, disposition, mime);
                DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
                r.addRequestHeader("X-MF-Token", TOKEN);
                r.setTitle(name);
                r.setMimeType(mime);
                r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
                Toast.makeText(this, "Descargando " + name, Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "No se pudo descargar: " + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        });

        web.loadDataWithBaseURL(null, page("Arrancando…", "La primera vez tarda unos segundos."),
                "text/html", "utf-8", null);

        new Thread(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                if (port == 0) {
                    port = Python.getInstance().getModule("movil")
                            .callAttr("start", getFilesDir().getAbsolutePath(), TOKEN, versionCode()).toInt();
                }
                String url = "http://127.0.0.1:" + port + "/";
                runOnUiThread(() -> {
                    CookieManager.getInstance().setCookie(url, "mf_token=" + TOKEN + "; path=/");
                    CookieManager.getInstance().flush();
                    web.loadUrl(url);
                });
            } catch (Throwable t) {
                runOnUiThread(() -> web.loadDataWithBaseURL(null,
                        page("No se pudo arrancar", TextUtils.htmlEncode(String.valueOf(t))),
                        "text/html", "utf-8", null));
            }
        }, "arranque").start();
    }

    private int versionCode() {
        try {
            android.content.pm.PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            return (int) (Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode);
        } catch (PackageManager.NameNotFoundException e) {
            return 0;
        }
    }

    private void startSyncService() {
        // Android 13+: permiso para mostrar la notificación de progreso (si no, la descarga sigue igual)
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
        try {
            startForegroundService(new Intent(this, SyncService.class));
        } catch (Exception e) {
            Toast.makeText(this, "Mantén la app abierta durante la descarga", Toast.LENGTH_LONG).show();
        }
    }

    private static String page(String title, String body) {
        return "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{font:16px system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;"
                + "justify-content:center;height:90vh;margin:0 16px;text-align:center;color:#15201b;background:#eef1ee}"
                + "@media(prefers-color-scheme:dark){body{color:#e4ebe7;background:#0d1311}}"
                + "h1{font-size:22px;margin:0 0 8px}p{opacity:.75;margin:0;word-break:break-word}</style>"
                + "<h1>" + title + "</h1><p>" + body + "</p>";
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request == PICK_FILE && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(request, result, data);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
