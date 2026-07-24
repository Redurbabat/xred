package de.redur.igmonitor;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Hintergrund-Dienst: haelt den eingebetteten Python-Server (Flask +
 * Instaloader) am Leben, auch wenn die App im Hintergrund ist oder das
 * Display aus ist. So laufen laufende Downloads und geplante Downloads
 * weiter, ohne dass die App offen bleiben muss.
 *
 * Android verlangt dafuer eine dauerhafte Benachrichtigung (Foreground
 * Service). Zusaetzlich wird ein Partial-Wake-Lock gehalten, damit die CPU
 * bei ausgeschaltetem Display nicht schlaeft und Downloads nicht pausieren.
 * Fuer volle Zuverlaessigkeit sollte die App zusaetzlich von der
 * Akku-Optimierung ausgenommen werden (dazu fragt die MainActivity).
 */
public class ServerService extends Service {

    private static final String CHANNEL_ID = "igmonitor_server";
    private static final int NOTIF_ID = 1;

    private PowerManager.WakeLock wakeLock;
    private boolean serverStarted = false;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // 1. ZUERST in den Vordergrund gehen (Android verlangt startForeground
        //    innerhalb weniger Sekunden nach dem Start).
        startForegroundWithNotification();

        // 2. Wake-Lock, damit Downloads bei Display-aus weiterlaufen.
        try {
            if (wakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                        "igmonitor:server");
                wakeLock.setReferenceCounted(false);
            }
            if (!wakeLock.isHeld()) {
                wakeLock.acquire();
            }
        } catch (Exception ignored) { }

        // 3. Python-Server einmalig starten (idempotent; start_server im
        //    Python-Modul ist zusaetzlich per Lock gegen Doppelstart gesichert).
        if (!serverStarted) {
            serverStarted = true;
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(this));
                }
                String baseDir = getExternalFilesDir(null) != null
                        ? getExternalFilesDir(null).getAbsolutePath()
                        : getFilesDir().getAbsolutePath();
                Python.getInstance().getModule("android_main")
                        .callAttr("start_server", baseDir);
            } catch (Exception ignored) { }
        }

        // START_STICKY: Android startet den Dienst nach einem Kill wieder.
        return START_STICKY;
    }

    private void startForegroundWithNotification() {
        NotificationManager nm =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && nm != null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "Instagram Monitor",
                    NotificationManager.IMPORTANCE_LOW);
            channel.setShowBadge(false);
            channel.setDescription("Haelt die App im Hintergrund aktiv.");
            nm.createNotificationChannel(channel);
        }

        // Neutraler Text: Der Dienst laeuft DAUERHAFT (damit die App im
        // Hintergrund erreichbar bleibt) – NICHT nur waehrend eines Downloads.
        // Frueher stand hier „Downloads laufen…", was faelschlich einen
        // laufenden Download suggerierte, obwohl gerade keiner lief.
        Notification.Builder builder =
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                        ? new Notification.Builder(this, CHANNEL_ID)
                        : new Notification.Builder(this);
        Notification notification = builder
                .setContentTitle("Instagram Monitor aktiv")
                .setContentText("Laeuft im Hintergrund – zum Oeffnen tippen.")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .build();

        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIF_ID, notification);
        }
    }

    @Override
    public void onDestroy() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Exception ignored) { }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;   // kein Binding noetig
    }
}
