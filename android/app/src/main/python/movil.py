"""Arranque de Garmin Local dentro de la app de Android.

Ejecuta exactamente el mismo app.py que en el PC, en un servidor local que solo
atiende a la propia app (127.0.0.1 y un código secreto en cada petición).
"""
import os
import pkgutil
import threading

_server = None


def start(files_dir, token, build=0):
    """Arranca el servidor (una sola vez por proceso) y devuelve el puerto."""
    global _server
    if _server is not None:
        return _server.server_address[1]

    # Variables que app.py lee al importarse
    os.environ["GARMIN_LOCAL_DATA"] = os.path.join(files_dir, "datos")
    os.environ["GARMIN_LOCAL_NO_BROWSER"] = "1"
    os.environ["GARMIN_LOCAL_TOKEN"] = token
    os.environ["GARMIN_LOCAL_PLATFORM"] = "android"
    os.environ["GARMIN_LOCAL_BUILD"] = str(build)

    import app
    from http.server import ThreadingHTTPServer

    # index.html viaja dentro del APK: si no está en disco junto a app.py, se copia
    if not os.path.exists(os.path.join(app.HERE, "index.html")):
        web = os.path.join(files_dir, "web")
        os.makedirs(web, exist_ok=True)
        with open(os.path.join(web, "index.html"), "wb") as f:
            f.write(pkgutil.get_data("app", "index.html"))
        app.HERE = web

    app.db().close()
    srv = None
    for port in range(8765, 8785):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), app.Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise RuntimeError("No hay ningún puerto libre para el servidor local")
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="garmin-local", daemon=True).start()
    _server = srv
    return srv.server_address[1]


def sync_state():
    """Estado de la descarga para la notificación: en curso, hechas, total, aviso, errores."""
    import app
    s = app.SYNC
    current = (s.get("current") or "").replace("\t", " ").replace("\n", " ")
    step = s.get("step") or [0, 0]
    if step[1] > 1 and current:
        current = "Paso %d de %d · %s" % (step[0], step[1], current)
    return "%d\t%d\t%d\t%s\t%d\t%d" % (1 if s["running"] else 0, s["done"], s["total"], current,
                                       s.get("saved") or 0, len(s.get("errors") or []))
