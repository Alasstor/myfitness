package com.alasstor.myfitness;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.chaquo.python.Python;

/**
 * Mantiene viva la descarga de Garmin aunque se apague la pantalla: servicio en primer
 * plano con notificación de progreso, CPU y Wi-Fi despiertos. Se cierra solo al terminar.
 */
public class SyncService extends Service {
    private static final String CHANNEL = "descargas";
    private static final int ID_PROGRESS = 1;
    private static final int ID_DONE = 2;

    private volatile boolean watching = false;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        createChannel();
        Notification n = progress("Preparando la descarga…", 0, 0);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(ID_PROGRESS, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(ID_PROGRESS, n);
        }
        acquireLocks();
        if (!watching) {
            watching = true;
            new Thread(this::watch, "vigilar-descarga").start();
        }
        return START_NOT_STICKY;
    }

    private void watch() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        String last = "";
        int misses = 0;
        try {
            while (watching) {
                Thread.sleep(3000);
                String[] p;
                try {
                    p = Python.getInstance().getModule("movil").callAttr("sync_state").toString().split("\t", -1);
                } catch (Throwable t) {
                    if (++misses > 5) break;
                    continue;
                }
                boolean running = "1".equals(p[0]);
                int done = Integer.parseInt(p[1]);
                int total = Integer.parseInt(p[2]);
                if (!running) {
                    int saved = Integer.parseInt(p[4]);
                    int errors = Integer.parseInt(p[5]);
                    String text = saved + " registros guardados"
                            + (errors > 0 ? ", " + errors + (errors == 1 ? " aviso" : " avisos") : "") + ".";
                    nm.notify(ID_DONE, done("Descarga de Garmin terminada", text));
                    break;
                }
                String text = p[3].isEmpty() ? "Descargando…" : p[3];
                if (!text.equals(last)) {
                    nm.notify(ID_PROGRESS, progress(text, done, total));
                    last = text;
                }
            }
        } catch (InterruptedException ignored) {
        } finally {
            watching = false;
            releaseLocks();
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
    }

    private void acquireLocks() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MyFitness:descarga");
            wakeLock.setReferenceCounted(false);
        }
        if (!wakeLock.isHeld()) wakeLock.acquire(3 * 60 * 60 * 1000L);  // máximo 3 h
        if (wifiLock == null) {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "MyFitness:descarga");
                wifiLock.setReferenceCounted(false);
            }
        }
        if (wifiLock != null && !wifiLock.isHeld()) wifiLock.acquire();
    }

    private void releaseLocks() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (RuntimeException ignored) {
        }
        try {
            if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        } catch (RuntimeException ignored) {
        }
    }

    @Override
    public void onDestroy() {
        watching = false;
        releaseLocks();
        super.onDestroy();
    }

    private void createChannel() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Descargas de Garmin",
                    NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Progreso de las descargas de tus datos de Garmin");
            nm.createNotificationChannel(ch);
        }
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification progress(String text, int done, int total) {
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentTitle("Descargando de Garmin")
                .setContentText(text)
                .setContentIntent(openApp())
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        if (total > 0) b.setProgress(total, done, false);
        else b.setProgress(0, 0, true);
        if (Build.VERSION.SDK_INT >= 31) b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        return b.build();
    }

    private Notification done(String title, String text) {
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentTitle(title)
                .setContentText(text)
                .setContentIntent(openApp())
                .setAutoCancel(true)
                .build();
    }
}
