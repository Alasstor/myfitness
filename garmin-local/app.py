#!/usr/bin/env python3
"""Garmin Local: panel en tu ordenador para tus datos de Garmin Connect.

Usa la Personal API oficial de Garmin. Solo necesita Python 3.9 o superior,
sin instalar nada más. Tu clave y tus datos se quedan en la carpeta "datos".
"""
import base64
import bisect
import hmac
import http.client
import io
import json
import os
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import fitvo2
import forma

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST = "127.0.0.1"
PORT = 8765
BASE = os.environ.get("GARMIN_API_BASE", "https://personal.connectapi.garmin.com").rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
UA = "garmin-local/1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("GARMIN_LOCAL_DATA", os.path.join(HERE, "datos"))
DB_PATH = os.path.join(DATA_DIR, "garmin.sqlite")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# En el móvil, el servidor local exige este código secreto (cookie mf_token) para que
# ninguna otra app del teléfono pueda leer tus datos. En el PC no se usa.
TOKEN = os.environ.get("GARMIN_LOCAL_TOKEN") or ""

CHUNK_DAYS = 28      # tamaño de cada petición por rango de fechas
PAUSE = float(os.environ.get("GARMIN_LOCAL_PAUSE", "0.35"))  # pausa entre peticiones

# Endpoints de rango de la Personal API (rutas de la librería oficial de Garmin)
DATASETS = {
    "summary": {"label": "Resumen diario",
                "path": "/usersummary-service/v2/dailysummaries?startDate={s}&endDate={e}"},
    "steps": {"label": "Pasos",
              "path": "/usersummary-service/stats/steps/daily/{s}/{e}"},
    "stress": {"label": "Estrés",
               "path": "/usersummary-service/stats/stress/daily/{s}/{e}"},
    "sleep": {"label": "Sueño",
              "path": "/sleep-service/sleep/dailySleepSummariesByDate?startDate={s}&endDate={e}"},
    "bodybattery": {"label": "Body Battery",
                    "path": "/wellness-service/wellness/bodyBattery/reports/daily?startDate={s}&endDate={e}"},
    "hrv": {"label": "VFC",
            "path": "/hrv-service/hrv/daily/{s}/{e}"},
    "weight": {"label": "Peso",
               "path": "/weight-service/weight/range/{s}/{e}?includeAll=true"},
}

# Métricas principales. Cada una prueba varios campos posibles, porque Garmin
# no documenta del todo la forma de cada respuesta.
PRESETS = [
    {"id": "steps", "label": "Pasos", "unit": "", "dec": 0,
     "cands": ["steps:totalSteps", "summary:totalSteps", "summary:movement.steps.value"]},
    {"id": "distance", "label": "Distancia diaria", "unit": "km", "dec": 1, "scale": 0.001,
     "cands": ["steps:totalDistance", "summary:totalDistanceMeters", "summary:movement.steps.distanceInMeters"]},
    {"id": "rhr", "label": "FC en reposo", "unit": "ppm", "dec": 0,
     "cands": ["summary:heartRate.restingValue", "summary:restingHeartRate", "sleep:restingHeartRate"]},
    {"id": "hrv", "label": "VFC nocturna", "unit": "ms", "dec": 0,
     "cands": ["hrv:lastNightAvg"]},
    {"id": "sleep", "label": "Sueño", "unit": "h", "dec": 1, "scale": 1 / 3600,
     "cands": ["sleep:sleepTimeSeconds", "summary:sleepingSeconds"]},
    {"id": "sleepscore", "label": "Puntuación de sueño", "unit": "", "dec": 0,
     "cands": ["sleep:sleepScores.overall.value", "sleep:overallSleepScore.value",
               "sleep:sleepScore", "summary:sleepScore"]},
    {"id": "stress", "label": "Estrés medio", "unit": "", "dec": 0,
     "cands": ["stress:overallStressLevel", "summary:stress.avgLevel", "summary:averageStressLevel"]},
    {"id": "bbhigh", "label": "Body Battery máximo", "unit": "", "dec": 0,
     "cands": ["summary:bodyBattery.maxValue", "summary:bodyBatteryHighestValue", "bodybattery:bodyBatteryHighestValue",
               "bodybattery:highest"]},
    {"id": "bbcharged", "label": "Body Battery recargado", "unit": "", "dec": 0,
     "cands": ["bodybattery:charged", "summary:bodyBattery.chargedValue", "summary:bodyBatteryChargedValue"]},
    {"id": "active", "label": "Calorías activas", "unit": "kcal", "dec": 0,
     "cands": ["summary:calories.burnedActive", "summary:activeKilocalories"]},
    {"id": "weight", "label": "Peso", "unit": "kg", "dec": 1, "grams": True,
     "cands": ["weight:weight", "weight:latestWeight.weight"]},
]


class ApiError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class TokenError(ApiError):
    pass


# ---------------------------------------------------------------- config / db

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS daily(
        dataset TEXT, day TEXT, json TEXT, updated REAL,
        PRIMARY KEY(dataset, day))""")
    con.execute("""CREATE TABLE IF NOT EXISTS activities(
        id INTEGER PRIMARY KEY, day TEXT, json TEXT, updated REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS fitdata(
        id INTEGER PRIMARY KEY, day TEXT, sport TEXT, vo2 REAL, status TEXT, updated REAL)""")
    return con


# ---------------------------------------------------------------- utilidades

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
DATE_KEYS = ("calendarDate", "date", "summaryDate", "day", "startDate",
             "startTimestampLocal", "startTimeLocal")


def find_date(obj, depth=0):
    for k in DATE_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and DATE_RE.match(v):
            return v[:10]
    if depth < 1:
        for v in obj.values():
            if isinstance(v, dict):
                d = find_date(v, depth + 1)
                if d:
                    return d
    return None


def find_records(obj):
    """Extrae (día, registro) de una respuesta de rango, sea cual sea su forma."""
    if isinstance(obj, list):
        out = []
        for it in obj:
            if isinstance(it, dict):
                d = find_date(it)
                if d:
                    out.append((d, it))
        return out
    if isinstance(obj, dict):
        best = []
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                recs = find_records(v)
                if len(recs) > len(best):
                    best = recs
        if best:
            return best
        d = find_date(obj)
        if d:
            return [(d, obj)]
    return []


SKIP_KEY = re.compile(r"(Id|ID|Pk|PK|pk|uuid|UUID)$")


def flatten(obj, prefix="", out=None, depth=0):
    """Convierte un registro en {ruta.del.campo: número}."""
    if out is None:
        out = {}
    if depth > 4 or not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            if SKIP_KEY.search(str(k)) or abs(v) > 1e11:  # ids y marcas de tiempo
                continue
            out[key] = v
        elif isinstance(v, dict):
            flatten(v, key, out, depth + 1)
    return out


def jwt_exp(tok):
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part)).get("exp"))
    except Exception:
        return None


def chunks(start, end, days):
    cur = start
    while cur <= end:
        e = min(end, cur + timedelta(days=days - 1))
        yield cur, e
        cur = e + timedelta(days=1)


def parse_day(s, default=None):
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return default


# ---------------------------------------------------------------- cliente Garmin

def _open(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        detail = body[:300].decode("utf-8", "replace").strip()
        path = urlparse(req.full_url).path
        raise ApiError(f"Garmin respondió {e.code} en {path}" + (f": {detail}" if detail else ""), e.code)
    except urllib.error.URLError as e:
        raise ApiError(f"No se pudo conectar con Garmin ({e.reason}). Revisa tu conexión a internet.", 0)
    except TimeoutError:
        raise ApiError("Garmin tardó demasiado en responder.", 0)
    except (OSError, http.client.HTTPException) as e:   # conexión cortada a mitad (frecuente en el móvil)
        raise ApiError(f"Se cortó la conexión con Garmin ({e.__class__.__name__}).", 0)


class Garmin:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        self.exp = 0.0
        self.token_error = None

    def reset(self):
        with self.lock:
            self.token, self.exp, self.token_error = None, 0.0, None

    def get_token(self, force=False):
        with self.lock:
            if not force and self.token and time.time() < self.exp - 300:
                return self.token
            key = load_config().get("api_key")
            if not key:
                raise TokenError("Falta tu clave de la Personal API de Garmin.", 0)
            # Igual que el ejemplo oficial con curl: POST sin cuerpo y aceptando cualquier formato.
            # Si Garmin rechaza el formato (406/415), prueba otra combinación de cabeceras.
            variants = [
                ({"Accept": "*/*", "Content-Length": "0"}, None),
                ({"Accept": "*/*", "Content-Type": "application/json"}, b"{}"),
                ({"Accept": "text/plain", "Content-Length": "0"}, None),
            ]
            body, last_err = None, None
            for extra, payload in variants:
                headers = {"Authorization": "ApiKey " + key, "User-Agent": UA, **extra}
                req = urllib.request.Request(BASE + TOKEN_PATH, data=payload, method="POST", headers=headers)
                try:
                    body, _ = _open(req, 30)
                    break
                except ApiError as e:
                    last_err = e
                    if e.status not in (406, 415):
                        break
            if body is None:
                e = last_err
                msg = str(e)
                if e.status in (401, 403):
                    msg = "Garmin no acepta la clave. Puede que esté mal copiada, caducada o borrada."
                self.token_error = msg
                raise TokenError(msg, e.status)
            text = body.decode("utf-8", "replace").strip()
            tok, expires = None, None
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    tok = data.get("access_token") or data.get("accessToken") or data.get("token")
                    expires = data.get("expires_in") or data.get("expiresIn")
                    if not tok:  # cualquier valor con forma de JWT
                        tok = next((v for v in data.values() if isinstance(v, str) and v.count(".") == 2
                                    and len(v) > 60 and " " not in v), None)
                elif isinstance(data, str):
                    tok = data
            except ValueError:
                text = text.strip('"')
                if text.count(".") == 2 and " " not in text:
                    tok = text
            if not tok:
                self.token_error = "Respuesta inesperada al pedir el token."
                raise TokenError(self.token_error + f" Contenido: {text[:200]}", 0)
            self.token = tok
            exp = time.time() + float(expires) if expires else (jwt_exp(tok) or time.time() + 23 * 3600)
            self.exp = exp
            self.token_error = None
            return tok

    def request(self, path, binary=False):
        refreshed, waits, retries, accept = False, 0, 0, ("*/*" if binary else "application/json, */*")
        while True:
            tok = self.get_token()
            req = urllib.request.Request(BASE + path, headers={
                "Authorization": "Bearer " + tok, "User-Agent": UA, "Accept": accept})
            try:
                body, headers = _open(req, 120)
            except ApiError as e:
                if e.status == 406 and accept != "*/*":
                    accept = "*/*"
                    continue
                if e.status == 401 and not refreshed:
                    refreshed = True
                    self.get_token(force=True)
                    continue
                if e.status == 429 and waits < 3:
                    waits += 1
                    time.sleep(20 * waits)
                    continue
                # Fallos puntuales de red o de los servidores de Garmin: reintentar con pausa
                if e.status in (0, 500, 502, 503, 504) and retries < 3:
                    retries += 1
                    time.sleep(5 * retries)
                    continue
                raise
            if binary:
                return body, headers
            text = body.decode("utf-8", "replace").strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                raise ApiError(f"Garmin devolvió algo que no es JSON en {path.split('?')[0]}", 0)


G = Garmin()


# ---------------------------------------------------------------- sincronización

SYNC = {"running": False, "total": 0, "done": 0, "current": "", "errors": [],
        "saved": 0, "started": None, "finished": None, "range": None}
SYNC_LOCK = threading.Lock()


def store_daily(ds, recs):
    if not recs:
        return 0
    con = db()
    now = time.time()
    with con:
        con.executemany("INSERT OR REPLACE INTO daily(dataset, day, json, updated) VALUES (?,?,?,?)",
                        [(ds, d, json.dumps(r, ensure_ascii=False), now) for d, r in recs])
    con.close()
    return len(recs)


def fetch_dataset(ds, s, e, allow_split=True):
    path = DATASETS[ds]["path"].format(s=s.isoformat(), e=e.isoformat())
    try:
        data = G.request(path)
    except TokenError:
        raise
    except ApiError as ex:
        # Algunos endpoints admiten rangos más cortos: reintenta por semanas
        if ex.status in (400, 413, 422) and allow_split and (e - s).days > 6:
            n = 0
            for a, b in chunks(s, e, 7):
                n += fetch_dataset(ds, a, b, False)
                time.sleep(PAUSE)
            return n
        raise
    return store_daily(ds, find_records(data) if data is not None else [])


def fetch_activities(s, e):
    start, n = 0, 0
    while True:
        data = G.request("/activitylist-service/activities/search/activities"
                         f"?start={start}&limit=100&startDate={s.isoformat()}&endDate={e.isoformat()}")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        if not data:
            break
        rows = []
        for a in data:
            aid = a.get("activityId")
            if aid is None:
                continue
            day = (a.get("startTimeLocal") or a.get("startTimeGMT") or "")[:10]
            rows.append((int(aid), day, json.dumps(a, ensure_ascii=False), time.time()))
        con = db()
        with con:
            con.executemany("INSERT OR REPLACE INTO activities(id, day, json, updated) VALUES (?,?,?,?)", rows)
        con.close()
        n += len(rows)
        if len(data) < 100:
            break
        start += 100
        time.sleep(PAUSE)
    return n


def vo2_pending(start, end):
    """Actividades de carrera y bici del periodo cuyo .FIT aún no se ha leído."""
    con = db()
    rows = con.execute("""SELECT a.id, a.day, a.json FROM activities a LEFT JOIN fitdata f ON f.id = a.id
                          WHERE f.id IS NULL AND a.day BETWEEN ? AND ? ORDER BY a.day DESC""",
                       (start.isoformat(), end.isoformat())).fetchall()
    con.close()
    out = []
    for aid, day, js in rows:
        t = (json.loads(js).get("activityType") or {}).get("typeKey")
        sport = fitvo2.sport_of(t)
        if sport:
            out.append((aid, day, sport))
    return out


def fetch_vo2(aid, day, sport):
    """Descarga el .FIT de una actividad y guarda el VO2máx que trae (o que no trae)."""
    data, _ = G.request(f"/download-service/files/activity/{aid}", binary=True)
    try:
        vo2 = fitvo2.read_vo2(fitvo2.unzip_fit(data))
        status = "ok" if vo2 is not None else "none"
    except Exception:
        vo2, status = None, "error"
    con = db()
    with con:
        con.execute("INSERT OR REPLACE INTO fitdata(id, day, sport, vo2, status, updated) VALUES (?,?,?,?,?,?)",
                    (aid, day, sport, vo2, status, time.time()))
    con.close()
    return 1 if vo2 is not None else 0


def run_sync(start, end, datasets, with_activities, with_vo2=True):
    tasks = [("ds", ds, s, e) for ds in datasets for s, e in chunks(start, end, CHUNK_DAYS)]
    if with_activities:
        tasks += [("act", None, s, e) for s, e in chunks(start, end, 365)]
    SYNC.update(total=len(tasks), done=0, errors=[], saved=0, current="",
                started=time.time(), finished=None, range=[start.isoformat(), end.isoformat()])
    try:
        SYNC["current"] = "Zonas de pulso"
        fetch_zones()
        for kind, ds, s, e in tasks:
            label = DATASETS[ds]["label"] if ds else "Actividades"
            SYNC["current"] = f"{label}: {s.strftime('%d/%m/%Y')} – {e.strftime('%d/%m/%Y')}"
            try:
                SYNC["saved"] += fetch_dataset(ds, s, e) if kind == "ds" else fetch_activities(s, e)
            except TokenError as ex:
                SYNC["errors"].append(str(ex))
                break
            except ApiError as ex:
                SYNC["errors"].append(f"{label} ({s.isoformat()}): {ex}")
            except Exception as ex:  # nunca dejar el hilo colgado
                SYNC["errors"].append(f"{label} ({s.isoformat()}): error inesperado: {ex!r}")
            SYNC["done"] += 1
            time.sleep(PAUSE)
        # VO2máx de Garmin: el .FIT de cada actividad de carrera o bici aún no leída
        if with_vo2 and not any("clave" in e for e in SYNC["errors"]):
            pending = vo2_pending(start, end)
            SYNC["total"] += len(pending)
            fails = 0
            for i, (aid, day, sport) in enumerate(pending):
                SYNC["current"] = f"VO2máx de Garmin: actividad {i + 1} de {len(pending)} ({day})"
                try:
                    SYNC["saved"] += fetch_vo2(aid, day, sport)
                except TokenError as ex:
                    SYNC["errors"].append(str(ex))
                    break
                except Exception as ex:
                    fails += 1
                    if fails <= 3:
                        SYNC["errors"].append(f"VO2máx ({day}): {ex}")
                    if fails >= 10:
                        SYNC["errors"].append("Demasiados fallos leyendo archivos de actividad: lo dejo para la próxima vez.")
                        break
                SYNC["done"] += 1
                time.sleep(PAUSE)
    finally:
        SYNC["running"] = False
        SYNC["current"] = ""
        SYNC["finished"] = time.time()


def start_sync(body):
    with SYNC_LOCK:
        if SYNC["running"]:
            raise ApiError("Ya hay una sincronización en marcha.", 409)
        today = date.today()
        vo2 = True
        if body.get("mode") == "vo2":
            # Solo leer el VO2máx de las actividades de carrera o bici que faltan, de todo el histórico
            con = db()
            first = con.execute("SELECT min(day) FROM activities").fetchone()[0]
            con.close()
            start = parse_day(first or "", today - timedelta(days=365))
            datasets, acts = [], False
        elif body.get("mode") == "update":
            con = db()
            last = con.execute("SELECT max(day) FROM daily").fetchone()[0]
            con.close()
            start = (parse_day(last) - timedelta(days=3)) if last else today - timedelta(days=30)
            datasets = list(DATASETS)
            acts = True
            vo2 = True
        else:
            start = parse_day(body.get("start") or "", today - timedelta(days=365))
            datasets = [d for d in body.get("datasets", list(DATASETS)) if d in DATASETS]
            acts = bool(body.get("activities", True))
            vo2 = bool(body.get("vo2", True))
        end = min(parse_day(body.get("end") or "", today), today)
        if start > end:
            raise ApiError("La fecha de inicio es posterior a la de fin.", 400)
        G.get_token()  # falla rápido si la clave no sirve
        SYNC["running"] = True
        threading.Thread(target=run_sync, args=(start, end, datasets, acts, vo2), daemon=True).start()


# ---------------------------------------------------------------- consultas

# ---------------------------------------------------------------- copia de seguridad

def export_backup():
    """ZIP con garmin.sqlite (copia coherente) y config.json (clave y ajustes)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = os.path.join(DATA_DIR, "export.tmp.sqlite")
    src = db()
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "garmin.sqlite")
        z.writestr("config.json", json.dumps(load_config(), indent=2, ensure_ascii=False))
    os.remove(tmp)
    return buf.getvalue()


def import_backup(raw):
    """Importa una copia (.zip), un garmin.sqlite o un config.json. Devuelve qué se importó."""
    if SYNC["running"]:
        raise ApiError("Espera a que termine la descarga en curso.", 409)
    parts = {}
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for n in z.namelist():
                    base = os.path.basename(n)
                    if base == "garmin.sqlite":
                        parts["db"] = z.read(n)
                    elif base == "config.json":
                        parts["cfg"] = z.read(n)
        except zipfile.BadZipFile:
            raise ApiError("El archivo .zip está dañado.", 400)
    elif raw[:16] == b"SQLite format 3\x00":
        parts["db"] = raw
    elif raw.lstrip()[:1] == b"{":
        parts["cfg"] = raw
    if not parts:
        raise ApiError("No reconozco el archivo. Elige la copia (.zip), garmin.sqlite o config.json.", 400)
    os.makedirs(DATA_DIR, exist_ok=True)
    done = []
    if "db" in parts:
        tmp = DB_PATH + ".import"
        with open(tmp, "wb") as f:
            f.write(parts["db"])
        try:
            c = sqlite3.connect(tmp)
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            check = c.execute("PRAGMA quick_check").fetchone()[0]
            n = c.execute("SELECT count(*) FROM activities").fetchone()[0] if "activities" in tables else 0
            c.close()
        except sqlite3.DatabaseError:
            os.remove(tmp)
            raise ApiError("La base de datos está dañada.", 400)
        if check != "ok" or not {"daily", "activities"} <= tables:
            os.remove(tmp)
            raise ApiError("Ese archivo no es una base de datos de Garmin Local.", 400)
        # Quitar el diario de la base anterior: si no, SQLite lo aplicaría a la nueva
        for ext in ("-wal", "-shm"):
            try:
                os.remove(DB_PATH + ext)
            except FileNotFoundError:
                pass
        os.replace(tmp, DB_PATH)
        done.append(f"{n} actividades y su histórico")
    if "cfg" in parts:
        try:
            new = json.loads(parts["cfg"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("config.json no es válido.", 400)
        if not isinstance(new, dict):
            raise ApiError("config.json no es válido.", 400)
        cfg = load_config()
        cfg.update({k: v for k, v in new.items() if k in ("api_key", "fc_max", "fc_reposo", "sexo", "ftp", "zones")})
        save_config(cfg)
        G.reset()
        done.append("tu clave y tus ajustes" if new.get("api_key") else "tus ajustes")
    _FIT_CACHE["key"] = None
    return done


def get_status():
    cfg = load_config()
    key = cfg.get("api_key") or ""
    con = db()
    ds = {r[0]: {"first": r[1], "last": r[2], "days": r[3]}
          for r in con.execute("SELECT dataset, min(day), max(day), count(*) FROM daily GROUP BY dataset")}
    a = con.execute("SELECT min(day), max(day), count(*) FROM activities").fetchone()
    v = con.execute("SELECT count(*), sum(vo2 IS NOT NULL), min(CASE WHEN vo2 IS NOT NULL THEN day END) FROM fitdata").fetchone()
    pend = con.execute("""SELECT count(*) FROM activities a LEFT JOIN fitdata f ON f.id = a.id WHERE f.id IS NULL AND (
        lower(json_extract(a.json, '$.activityType.typeKey')) LIKE '%run%' OR lower(json_extract(a.json, '$.activityType.typeKey')) LIKE '%cycl%'
        OR lower(json_extract(a.json, '$.activityType.typeKey')) LIKE '%bik%' OR lower(json_extract(a.json, '$.activityType.typeKey')) LIKE '%ride%'
        OR lower(json_extract(a.json, '$.activityType.typeKey')) LIKE '%spin%')""").fetchone()[0]
    con.close()
    return {
        "key_set": bool(key), "key_hint": key[-4:] if key else None,
        "token_ok": bool(G.token) and time.time() < G.exp, "token_error": G.token_error,
        "datasets": ds, "labels": {k: v["label"] for k, v in DATASETS.items()},
        "activities": {"first": a[0], "last": a[1], "count": a[2]},
        "vo2files": {"read": v[0], "with_vo2": v[1] or 0, "first": v[2], "unread_activities": pend},
        "sync": SYNC, "today": date.today().isoformat(), "settings": get_settings(),
    }


def get_settings():
    cfg = load_config()
    return {"fc_max": cfg.get("fc_max"), "fc_reposo": cfg.get("fc_reposo"), "sexo": cfg.get("sexo", "h"),
            "ftp": cfg.get("ftp"), "zones": cfg.get("zones"), "peso": cfg.get("peso")}


def fetch_zones():
    """Guarda tus zonas de pulso de Garmin (si la API las da) para el entreno recomendado."""
    try:
        data = G.request("/biometric-service/heartRateZones/v2")
    except ApiError:
        return
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    z = next((x for x in items if isinstance(x, dict) and x.get("sport") == "DEFAULT"), items[0] if items else None)
    if not isinstance(z, dict) or not z.get("zone2Floor"):
        return
    cfg = load_config()
    cfg["zones"] = {"z1": z.get("zone1Floor"), "z2": z.get("zone2Floor"), "z3": z.get("zone3Floor"),
                    "z4": z.get("zone4Floor"), "z5": z.get("zone5Floor"),
                    "lthr": z.get("lactateThresholdHeartRateUsed"), "source": "tus zonas de Garmin"}
    save_config(cfg)


def save_settings(body):
    cfg = load_config()

    def num(name, lo, hi, label):
        if name not in body:      # lo que no se envía se deja como está
            return
        v = body.get(name)
        if v in (None, ""):
            cfg.pop(name, None)
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ApiError(f"{label}: escribe un número.", 400)
        if not lo <= v <= hi:
            raise ApiError(f"{label}: tiene que estar entre {lo} y {hi}.", 400)
        cfg[name] = v

    num("fc_max", 120, 230, "FC máxima")
    num("fc_reposo", 30, 100, "FC en reposo")
    num("ftp", 80, 600, "FTP")
    num("peso", 35, 200, "Peso")
    if body.get("sexo") in ("h", "m"):
        cfg["sexo"] = body["sexo"]
    save_config(cfg)
    _FIT_CACHE["key"] = None


# ---------------------------------------------------------------- estado de forma

_FIT_CACHE = {"key": None, "val": None}


def fitness_data():
    """Calcula todos los indicadores sobre el histórico completo (con caché)."""
    con = db()
    key = (con.execute("SELECT count(*), max(updated) FROM daily").fetchone(),
           con.execute("SELECT count(*), max(updated) FROM activities").fetchone(),
           json.dumps(get_settings()), date.today().isoformat(),
           con.execute("SELECT count(*), max(updated) FROM fitdata").fetchone())
    if _FIT_CACHE["key"] == key:
        con.close()
        return _FIT_CACHE["val"]
    acts = [json.loads(j) for (j,) in con.execute("SELECT json FROM activities ORDER BY day")]
    con.close()

    st = get_settings()
    pre = {p["id"]: p for p in resolve_presets(all_fields())}

    def daily(pid, scale=1.0):
        if pid not in pre:
            return {}
        return {d: v * scale for d, v in series(pre[pid]["field"], "0000-01-01", "9999-12-31")}

    rhr = daily("rhr")
    hrv = daily("hrv")
    sleep = daily("sleep", pre["sleep"]["scale"] if "sleep" in pre else 1)

    if st["fc_max"]:
        hr_max, hr_max_src = st["fc_max"], "tu ajuste"
    else:
        hr_max, hr_max_src = forma.estimate_hrmax([a.get("maxHR") for a in acts])

    rhr_days = sorted(rhr)

    def rest_for(day):
        if st["fc_reposo"]:
            return st["fc_reposo"]
        lo = (date.fromisoformat(day) - timedelta(days=30)).isoformat()
        i0, i1 = bisect.bisect_left(rhr_days, lo), bisect.bisect_right(rhr_days, day)
        vals = sorted(rhr[d] for d in rhr_days[i0:i1])
        if len(vals) >= 3:
            return vals[len(vals) // 2]
        allv = sorted(rhr.values())
        return allv[len(allv) // 2] if allv else None

    # 1) TRIMP de Banister para las actividades con pulso
    computed = []
    for a in acts:
        day = (a.get("startTimeLocal") or a.get("startTimeGMT") or "")[:10]
        if not day:
            continue
        dur = a.get("duration") or a.get("movingDuration")
        t = forma.trimp_banister(dur, a.get("averageHR"), hr_max, rest_for(day), st["sexo"])
        computed.append((a, day, dur, t))

    # 2) Factor de conversión entre la carga de Garmin y el TRIMP, calibrado con tus
    #    propias actividades que tienen ambos datos
    pairs = [t / a["activityTrainingLoad"] for a, _, _, t in computed
             if t and isinstance(a.get("activityTrainingLoad"), (int, float)) and a["activityTrainingLoad"] > 5]
    if len(pairs) >= 20:
        ratio, ratio_src = statistics.median(pairs), f"calibrado con {len(pairs)} de tus actividades"
    else:
        ratio, ratio_src = 0.85, "valor por defecto (aún hay pocas actividades para calibrarlo)"

    # 3) Actividades sin pulso pero con potencia: TSS × factor TRIMP/TSS de las sesiones de la
    #    misma época (el factor depende del FTP que tuvieras configurado en Garmin).
    #    Para el resto, carga de Garmin × factor (Garmin infravalora las sesiones sin pulso).
    tss_pairs = sorted((date.fromisoformat(day).toordinal(), t / a["trainingStressScore"])
                       for a, day, _, t in computed
                       if t and isinstance(a.get("trainingStressScore"), (int, float)) and a["trainingStressScore"] > 10)
    tss_days = [p[0] for p in tss_pairs]

    def tss_ratio(day):
        if len(tss_pairs) < 20:
            return None
        o = date.fromisoformat(day).toordinal()
        i = bisect.bisect_left(tss_days, o)
        lo, hi, near = i - 1, i, []
        while len(near) < 40 and (lo >= 0 or hi < len(tss_pairs)):
            if hi >= len(tss_pairs) or (lo >= 0 and o - tss_days[lo] <= tss_days[hi] - o):
                near.append(tss_pairs[lo][1]); lo -= 1
            else:
                near.append(tss_pairs[hi][1]); hi += 1
        return statistics.median(near)

    loads, trimps, info, no_hr, estimated, est_tss = {}, {}, [], 0, 0, 0
    for a, day, dur, t in computed:
        est = False
        if t is None:
            tss, gl = a.get("trainingStressScore"), a.get("activityTrainingLoad")
            r = tss_ratio(day) if isinstance(tss, (int, float)) and tss > 0 else None
            if r is not None:
                t, est = tss * r, True
                estimated += 1
                est_tss += 1
            elif isinstance(gl, (int, float)) and gl > 0:
                t, est = gl * ratio, True
                estimated += 1
            else:
                no_hr += 1
        if t is not None:
            loads[day] = loads.get(day, 0.0) + t
            trimps[a.get("activityId")] = {"v": round(t), "est": est}
        typ = a.get("activityType") or {}
        info.append({"day": day, "type": typ.get("typeKey") if isinstance(typ, dict) else str(typ),
                     "duration": dur, "distance": a.get("distance"), "load": t,
                     "te_aer": a.get("aerobicTrainingEffect"), "te_ana": a.get("anaerobicTrainingEffect")})

    starts = [d for d in [min(loads) if loads else None, rhr_days[0] if rhr_days else None,
                          min(hrv) if hrv else None] if d]
    if not starts:
        val = {"rows": [], "readiness": forma.readiness([]), "acts": info, "trimps": trimps,
               "params": {}, "reliable_from": None}
    else:
        first = date.fromisoformat(min(starts))
        # VO2máx de Garmin leído de los .FIT (carrera y bici por separado)
        con = db()
        vrows = con.execute("SELECT day, sport, vo2 FROM fitdata WHERE vo2 IS NOT NULL ORDER BY day, updated").fetchall()
        con.close()
        vo2_run = {d_: v for d_, sp, v in vrows if sp == "carrera"}
        vo2_bike = {d_: v for d_, sp, v in vrows if sp == "bici"}
        rows = forma.compute(first, date.today(), loads, rhr, hrv, sleep, hr_max, vo2_run, vo2_bike)
        recent_rest = rest_for(date.today().isoformat())
        # FTP: tu ajuste, o el que usa Garmin en tu actividad con potencia más reciente (NP / IF)
        ftp, ftp_src = st.get("ftp"), "tu ajuste"
        if not ftp:
            for a in reversed(acts):
                if a.get("normPower") and (a.get("intensityFactor") or 0) > 0.3:
                    ftp, ftp_src = round(a["normPower"] / a["intensityFactor"]), "el de Garmin en tu última sesión con potencia"
                    break
        zones = st.get("zones") or forma.default_zones(hr_max)
        # Tu perfil por deporte del último año: ritmos de natación y pulso medio corriendo
        year_ago = (date.today() - timedelta(days=365)).isoformat()
        swim = []
        for a in acts:
            if (a.get("activityType") or {}).get("typeKey") == "lap_swimming" and (a.get("distance") or 0) >= 600 \
                    and (a.get("startTimeLocal") or "") >= year_ago:
                p = (a.get("movingDuration") or a.get("duration") or 0) / (a["distance"] / 100)
                if 60 <= p <= 300:
                    swim.append(p)
        runs_hr = [a["averageHR"] for a in acts
                   if (a.get("activityType") or {}).get("typeKey") in ("running", "treadmill_running", "trail_running")
                   and a.get("averageHR") and (a.get("duration") or 0) >= 1200 and (a.get("startTimeLocal") or "") >= year_ago]
        prof = {}
        if len(swim) >= 5:
            q = statistics.quantiles(swim, n=20)
            prof.update(swim_fast=q[3], swim_mid=statistics.median(swim), swim_easy=q[15])
        if len(runs_hr) >= 5:
            prof["run_hr"] = statistics.median(runs_hr)
        plan = forma.recommend(rows, info, date.today().isoformat(), zones, ftp, prof)
        if plan:
            plan["ftp_src"] = ftp_src
        val = {
            "rows": rows, "readiness": forma.readiness(rows), "acts": info, "trimps": trimps, "plan": plan,
            "reliable_from": (date.fromisoformat(min(loads)) + timedelta(days=42)).isoformat() if loads else None,
            "params": {"hr_max": hr_max, "hr_max_src": hr_max_src,
                       "hr_rest": recent_rest,
                       "hr_rest_src": "tu ajuste" if st["fc_reposo"] else "mediana de tus últimos 30 días",
                       "sexo": st["sexo"], "acts_with_hr": len(trimps) - estimated,
                       "acts_estimated": estimated, "acts_est_tss": est_tss, "acts_without_hr": no_hr,
                       "ratio": round(ratio, 3), "ratio_src": ratio_src},
        }
    _FIT_CACHE.update(key=key, val=val)
    return val


def fitness(start, end):
    f = fitness_data()
    rows = [r for r in f["rows"] if start <= r["day"] <= end]
    acts = [a for a in f["acts"] if start <= a["day"] <= end]
    return {"rows": rows, "readiness": f["readiness"], "params": f["params"],
            "reliable_from": f["reliable_from"], "weeks": forma.weekly(acts),
            "today": f["rows"][-1] if f["rows"] else None, "plan": f.get("plan")}


def all_fields():
    con = db()
    out = {}
    for (ds,) in con.execute("SELECT DISTINCT dataset FROM daily").fetchall():
        for (js,) in con.execute("SELECT json FROM daily WHERE dataset=? ORDER BY day DESC LIMIT 120", (ds,)):
            for k in flatten(json.loads(js)):
                out[f"{ds}:{k}"] = out.get(f"{ds}:{k}", 0) + 1
    con.close()
    return out


def resolve_presets(fields):
    res = []
    for p in PRESETS:
        found = None
        for cand in p["cands"]:
            ds, suffix = cand.split(":", 1)
            matches = [f for f in fields if f.startswith(ds + ":") and
                       (f.split(":", 1)[1] == suffix or f.endswith("." + suffix))]
            if matches:
                found = max(matches, key=lambda f: fields[f])
                break
        if found:
            res.append({"id": p["id"], "label": p["label"], "unit": p["unit"], "dec": p["dec"],
                        "scale": p.get("scale", 1), "grams": p.get("grams", False), "field": found})
    return res


def series(field, start, end):
    ds, path = field.split(":", 1)
    con = db()
    rows = con.execute("SELECT day, json FROM daily WHERE dataset=? AND day BETWEEN ? AND ? ORDER BY day",
                       (ds, start, end)).fetchall()
    con.close()
    pts = []
    for day, js in rows:
        v = flatten(json.loads(js)).get(path)
        if v is not None:
            pts.append([day, v])
    return pts


def overview(start, end):
    fields = all_fields()
    presets = resolve_presets(fields)
    for p in presets:
        p["points"] = series(p["field"], start, end)
    return {"presets": presets, "fields": sorted(fields)}


def activities(start, end):
    con = db()
    rows = con.execute("SELECT json FROM activities WHERE day BETWEEN ? AND ? ORDER BY day DESC, id DESC",
                       (start, end)).fetchall()
    vo2 = dict(con.execute("SELECT id, vo2 FROM fitdata WHERE vo2 IS NOT NULL AND day BETWEEN ? AND ?", (start, end)).fetchall())
    con.close()
    trimps = fitness_data()["trimps"]
    out = []
    for (js,) in rows:
        a = json.loads(js)
        t = a.get("activityType") or {}
        out.append({
            "id": a.get("activityId"), "name": a.get("activityName") or "",
            "start": a.get("startTimeLocal") or a.get("startTimeGMT") or "",
            "type": t.get("typeKey") if isinstance(t, dict) else str(t),
            "distance": a.get("distance"), "duration": a.get("duration") or a.get("movingDuration"),
            "avgHR": a.get("averageHR"), "maxHR": a.get("maxHR"), "calories": a.get("calories"),
            "elev": a.get("elevationGain"), "avgSpeed": a.get("averageSpeed"),
            "load": (trimps.get(a.get("activityId")) or {}).get("v"),
            "load_est": (trimps.get(a.get("activityId")) or {}).get("est", False),
            "vo2": vo2.get(a.get("activityId")),
        })
    return out


def raw(dataset, day):
    con = db()
    if dataset == "activity":
        row = con.execute("SELECT json FROM activities WHERE id=?", (int(day),)).fetchone()
    else:
        row = con.execute("SELECT json FROM daily WHERE dataset=? AND day=?", (dataset, day)).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------- servidor web

class Handler(BaseHTTPRequestHandler):
    server_version = "GarminLocal/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("GARMIN_LOCAL_VERBOSE"):
            super().log_message(fmt, *args)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def host_ok(self):
        # Solo atiende peticiones dirigidas a este ordenador
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        if TOKEN:
            sent = self.headers.get("X-MF-Token") or ""
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "mf_token":
                    sent = v
            return hmac.compare_digest(sent, TOKEN)
        return True

    def do_GET(self):
        if not self.host_ok():
            return self.error("Acceso no permitido", 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        today = date.today().isoformat()
        start, end = q.get("start", "2000-01-01"), q.get("end", today)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/status":
                self.send_json(get_status())
            elif u.path == "/api/overview":
                self.send_json(overview(start, end))
            elif u.path == "/api/series":
                self.send_json({"field": q.get("field"), "points": series(q["field"], start, end)})
            elif u.path == "/api/fitness":
                self.send_json(fitness(start, end))
            elif u.path == "/api/activities":
                self.send_json(activities(start, end))
            elif u.path == "/api/raw":
                self.send_json(raw(q.get("dataset", ""), q.get("day", "")))
            elif u.path.startswith("/api/activity/") and u.path.endswith("/download"):
                aid = int(u.path.split("/")[3])
                data, headers = G.request(f"/download-service/files/activity/{aid}", binary=True)
                ctype = headers.get("Content-Type") or "application/zip"
                ext = ".fit" if "fit" in ctype.lower() else ".zip"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="actividad_{aid}{ext}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/export":
                data = export_backup()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="garmin-local-copia-{today}.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/probe":
                path = q.get("path", "")
                if not path.startswith("/") or "//" in path:
                    return self.error("Escribe una ruta que empiece por /")
                self.send_json({"path": path, "result": G.request(path)})
            else:
                self.error("No encontrado", 404)
        except ApiError as e:
            self.error(str(e), 502 if e.status else 503)
        except (KeyError, ValueError) as e:
            self.error(f"Petición incorrecta: {e}")

    def do_POST(self):
        if not self.host_ok():
            return self.error("Acceso no permitido", 403)
        u = urlparse(self.path)
        if u.path == "/api/import":
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 500 * 1024 * 1024:
                return self.error("Archivo vacío o demasiado grande.")
            try:
                self.send_json({"ok": True, "imported": import_backup(self.rfile.read(n))})
            except ApiError as e:
                self.error(str(e), e.status or 400)
            return
        body = self.read_body()
        try:
            if u.path == "/api/key":
                key = (body.get("key") or "").strip()
                if not key:
                    return self.error("Pega tu clave antes de guardar.")
                cfg = load_config()
                cfg["api_key"] = key
                save_config(cfg)
                G.reset()
                G.get_token()
                self.send_json({"ok": True})
            elif u.path == "/api/key/delete":
                cfg = load_config()
                cfg.pop("api_key", None)
                save_config(cfg)
                G.reset()
                self.send_json({"ok": True})
            elif u.path == "/api/settings":
                save_settings(body)
                self.send_json({"ok": True, "settings": get_settings()})
            elif u.path == "/api/sync":
                start_sync(body)
                self.send_json({"ok": True})
            else:
                self.error("No encontrado", 404)
        except ApiError as e:
            self.error(str(e), e.status if e.status in (400, 409) else 502)


def main():
    db().close()
    server = None
    for port in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if not server:
        print("No hay ningún puerto libre entre 8765 y 8774.")
        sys.exit(1)
    url = f"http://localhost:{server.server_address[1]}/"
    print("Garmin Local en marcha:", url)
    print("Deja esta ventana abierta mientras lo uses. Para cerrarlo, pulsa Ctrl+C.")
    if not os.environ.get("GARMIN_LOCAL_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")


if __name__ == "__main__":
    main()
