"""Cálculos de estado de forma para Garmin Local.

Cada indicador sigue un modelo publicado. Las fuentes están junto a cada función
y en la sección "Cómo se calcula" del panel.
"""
import math
from datetime import date, timedelta

# ---------------------------------------------------------------- carga de cada actividad

def trimp_banister(duration_s, hr_avg, hr_max, hr_rest, sex="h"):
    """TRIMP de Banister.

    TRIMP = minutos × ΔFC × a·e^(b·ΔFC), con ΔFC = (FC media − FC reposo) / (FC máx − FC reposo).
    Hombres: a = 0.64, b = 1.92. Mujeres: a = 0.86, b = 1.67.
    Se aplica con la FC media de la sesión, como hacen intervals.icu y otras plataformas.
    """
    if not duration_s or not hr_avg or not hr_max or hr_rest is None or hr_max <= hr_rest:
        return None
    x = (hr_avg - hr_rest) / (hr_max - hr_rest)
    x = max(0.0, min(1.0, x))
    a, b = (0.86, 1.67) if sex == "m" else (0.64, 1.92)
    return duration_s / 60.0 * x * a * math.exp(b * x)


def estimate_hrmax(values):
    """FC máxima estimada a partir de las FC máximas de las actividades.

    Usa la segunda más alta para descartar un pico aislado por error del sensor.
    """
    vals = sorted(v for v in values if isinstance(v, (int, float)) and 100 <= v <= 230)
    if not vals:
        return None, None
    if len(vals) >= 5:
        return float(vals[-2]), "estimada: la segunda FC máxima más alta registrada en tus actividades"
    return float(vals[-1]), "estimada: la FC máxima más alta registrada en tus actividades (hay pocas actividades con pulso)"


SPORT_GROUPS = ["Carrera", "Ciclismo", "Natación", "Caminar y montaña", "Fuerza y otros"]


def sport_group(type_key):
    t = (type_key or "").lower()
    if "run" in t:
        return "Carrera"
    if any(k in t for k in ("cycl", "bik", "ride", "spin")):
        return "Ciclismo"
    if "swim" in t:
        return "Natación"
    if any(k in t for k in ("walk", "hik", "mountaineer")):
        return "Caminar y montaña"
    return "Fuerza y otros"


# ---------------------------------------------------------------- series diarias

def _window(arr, i, n):
    return [v for v in arr[max(0, i - n + 1):i + 1] if v is not None]


def _mean(v):
    return sum(v) / len(v) if v else None


def _sd(v):
    if len(v) < 2:
        return None
    m = _mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def _r(v, n=2):
    return None if v is None else round(v, n)


def compute(first, last, loads, rhr, hrv, sleep_h, hr_max, vo2_run=None, vo2_bike=None):
    """Calcula los indicadores día a día entre first y last (objetos date).

    loads: {día ISO: TRIMP del día}. rhr: FC reposo diaria. hrv: VFC nocturna (ms).
    sleep_h: horas de sueño. hr_max: FC máxima (para el VO2máx estimado).
    """
    days = []
    d = first
    while d <= last:
        days.append(d.isoformat())
        d += timedelta(days=1)
    n = len(days)
    load = [float(loads.get(x, 0.0)) for x in days]
    rhr_a = [rhr.get(x) for x in days]
    hrv_ln = [math.log(hrv[x]) if hrv.get(x) and hrv[x] > 0 else None for x in days]
    sleep_a = [sleep_h.get(x) for x in days]

    # La frescura en % no es fiable hasta 6 semanas después de la primera carga
    first_load = next((i for i, v in enumerate(load) if v > 0), None)
    rows = []
    ctl = atl = 0.0
    last_run = last_bike = None   # VO2máx de Garmin: vale el último hasta que el reloj da otro
    ewma_a = ewma_c = None
    lam_a, lam_c = 2 / (7 + 1), 2 / (28 + 1)   # EWMA con N = 7 y N = 28 días
    for i, day in enumerate(days):
        L = load[i]
        if vo2_run and day in vo2_run:
            last_run = vo2_run[day]
        if vo2_bike and day in vo2_bike:
            last_bike = vo2_bike[day]
        # Forma (TSB) = forma física de ayer − fatiga de ayer
        tsb = ctl - atl
        form = tsb / ctl * 100 if ctl >= 10 and first_load is not None and i >= first_load + 42 else None
        # Modelo de rendimiento: medias exponenciales de 42 y 7 días
        ctl += (L - ctl) / 42
        atl += (L - atl) / 7
        # Ratio de carga aguda:crónica con medias exponenciales (Williams et al., 2017)
        ewma_a = L if ewma_a is None else L * lam_a + (1 - lam_a) * ewma_a
        ewma_c = L if ewma_c is None else L * lam_c + (1 - lam_c) * ewma_c
        acwr = ewma_a / ewma_c if i >= 27 and ewma_c and ewma_c >= 1 else None
        # Monotonía y tensión de Foster sobre los últimos 7 días
        w7 = load[max(0, i - 6):i + 1]
        sd7 = _sd(w7)
        mono = _mean(w7) / sd7 if len(w7) == 7 and sd7 and sum(w7) > 0 else None
        strain = sum(w7) * mono if mono is not None else None

        # VFC: media de 7 días frente a la línea base de 60 días ± cambio mínimo relevante (0,5 × DE)
        h7 = _window(hrv_ln, i, 7)
        h60 = _window(hrv_ln, i, 60)
        hrv7 = _mean(h7) if len(h7) >= 3 else None
        base = _mean(h60) if len(h60) >= 14 else None
        sd60 = _sd(h60) if len(h60) >= 14 else None
        lo = base - 0.5 * sd60 if base is not None and sd60 is not None else None
        hi = base + 0.5 * sd60 if base is not None and sd60 is not None else None
        cv7 = (_sd(h7) / _mean(h7) * 100) if len(h7) >= 3 and _sd(h7) is not None else None

        r7 = _window(rhr_a, i, 7)
        r60 = _window(rhr_a, i, 60)
        rhr7 = _mean(r7) if len(r7) >= 3 else None
        rbase = _mean(r60) if len(r60) >= 14 else None
        rsd = _sd(r60) if len(r60) >= 14 else None

        s7 = _window(sleep_a, i, 7)

        rows.append({
            "day": day, "load": _r(L, 1), "ctl": _r(ctl, 1), "atl": _r(atl, 1), "tsb": _r(tsb, 1),
            "form": _r(form, 1), "acwr": _r(acwr), "mono": _r(mono), "strain": _r(strain, 0),
            "load7": _r(sum(w7), 0),
            "hrv": _r(math.exp(hrv_ln[i]), 1) if hrv_ln[i] is not None else None,
            "hrv7": _r(math.exp(hrv7), 1) if hrv7 is not None else None,
            "hrv_lo": _r(math.exp(lo), 1) if lo is not None else None,
            "hrv_hi": _r(math.exp(hi), 1) if hi is not None else None,
            "hrv_cv7": _r(cv7, 1),
            "rhr": rhr_a[i], "rhr7": _r(rhr7, 1), "rhr_base": _r(rbase, 1), "rhr_sd": _r(rsd, 2),
            "vo2_run": last_run, "vo2_bike": last_bike,
            "sleep7": _r(_mean(s7), 2) if len(s7) >= 3 else None,
            "sleep": _r(sleep_a[i], 2) if sleep_a[i] is not None else None,
        })
    return rows


# ---------------------------------------------------------------- valoración del día

FORM_ZONES = [  # Form % = TSB / CTL. Umbrales de Joe Friel aplicados en % (como intervals.icu)
    (-1e9, -30, "Riesgo alto", "bad"),
    (-30, -10, "Carga productiva", "ok"),
    (-10, 5, "Zona neutra", "ok"),
    (5, 20, "Fresco", "ok"),
    (20, 1e9, "Transición: perdiendo forma", "info"),
]


def form_zone(form):
    if form is None:
        return None
    for lo, hi, name, level in FORM_ZONES:
        if lo <= form < hi:
            return {"name": name, "level": level}
    return None


def _latest(rows, key, max_age=3):
    for r in reversed(rows[-max_age:]):
        if r.get(key) is not None:
            return r
    return None


def readiness(rows):
    reasons = []
    if not rows:
        return {"verdict": "Sin datos", "level": "info", "reasons": []}

    # VFC (guías del AIS y enfoque de Plews: media de 7 días frente a la línea base)
    r = _latest(rows, "hrv7")
    if r and r.get("hrv_lo") is not None:
        streak = 0
        for x in reversed(rows[-10:]):
            if x.get("hrv") is None:
                continue
            if x.get("hrv_lo") is not None and x["hrv"] < x["hrv_lo"]:
                streak += 1
            else:
                break
        if r["hrv7"] < r["hrv_lo"]:
            if streak >= 4:
                reasons.append(("bad", f"Tu VFC lleva {streak} días por debajo de tu rango normal. Toca uno o dos días de recuperación real y bajar la intensidad."))
            elif streak >= 2:
                reasons.append(("warn", f"Tu VFC lleva {streak} días por debajo de tu rango normal. Mejor cambiar el entreno intenso por sesiones suaves."))
            else:
                reasons.append(("warn", "Tu VFC media de 7 días está por debajo de tu rango normal."))
        elif r["hrv7"] > r["hrv_hi"]:
            reasons.append(("ok", "Tu VFC media de 7 días está por encima de tu rango normal."))
        else:
            reasons.append(("ok", "Tu VFC está dentro de tu rango normal."))
    elif _latest(rows, "hrv", 7) is None:
        reasons.append(("info", "No hay VFC reciente. Descarga los datos de VFC o usa el reloj para dormir."))
    else:
        reasons.append(("info", "Aún no hay bastantes noches de VFC para calcular tu rango normal (hacen falta 14)."))

    # Señal de posible enfermedad: VFC del día ≥15 % por debajo de su media + FC reposo alta
    rh = _latest(rows, "hrv")
    rr = _latest(rows, "rhr")
    if rh and rh.get("hrv7") and rr and rr.get("rhr_base") is not None and rr.get("rhr_sd") is not None:
        if rh["hrv"] <= rh["hrv7"] * 0.85 and rr["rhr"] > rr["rhr_base"] + rr["rhr_sd"]:
            reasons.append(("bad", "VFC muy baja y FC en reposo alta a la vez. Puede ser cansancio acumulado, estrés o el inicio de una enfermedad."))

    # Balance de carga
    f = _latest(rows, "form", 1)
    if f:
        z = form_zone(f["form"])
        text = {"Riesgo alto": "La fatiga supera mucho a tu forma física. Riesgo de sobrecarga.",
                "Carga productiva": "Estás en un bloque de carga productivo.",
                "Zona neutra": "Carga y fatiga equilibradas.",
                "Fresco": "Estás descansado: buen momento para una sesión exigente o una carrera.",
                "Transición: perdiendo forma": "Llevas tiempo con poca carga: estás perdiendo forma física."}[z["name"]]
        reasons.append((z["level"], text))

    a = _latest(rows, "acwr", 1)
    if a and a["acwr"] > 1.5:
        reasons.append(("warn", f"Has subido la carga muy deprisa (ratio agudo:crónico {a['acwr']:.2f}). Por encima de 1,5 aumentan las lesiones."))

    levels = [lv for lv, _ in reasons]
    if "bad" in levels:
        verdict, level = "Prioriza recuperar", "bad"
    elif "warn" in levels:
        verdict, level = "Entrena suave", "warn"
    elif any(lv == "ok" for lv in levels):
        verdict, level = "Listo para entrenar", "ok"
    else:
        verdict, level = "Faltan datos", "info"
    return {"verdict": verdict, "level": level, "reasons": [{"level": lv, "text": t} for lv, t in reasons]}


# ---------------------------------------------------------------- entreno recomendado

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def _es(v, dec):
    return f"{v:.{dec}f}".replace(".", ",")


def default_zones(hr_max):
    """Zonas de pulso por % de FC máxima (las de Garmin por defecto) si no hay otras."""
    if not hr_max:
        return None
    return {"z1": round(hr_max * .5), "z2": round(hr_max * .6), "z3": round(hr_max * .7),
            "z4": round(hr_max * .8), "z5": round(hr_max * .9), "source": "% de tu FC máxima"}


def _is_hard(a):
    return (a.get("te_aer") or 0) >= 4 or (a.get("te_ana") or 0) >= 2.5


def _pace(sec):
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def sport_versions(kind, z, ftp, prof, run_block):
    """La sesión del día en cada deporte: rodillo, elíptica, carrera, cinta y natación.

    Bici por potencia (FTP) y pulso; elíptica, carrera y cinta por pulso (tus zonas de Garmin);
    natación por tus ritmos por 100 m del último año (el pulso de muñeca en el agua no es fiable).
    """
    if kind == "descanso":
        return []
    z2, z3, z4, z5 = z.get("z2"), z.get("z3"), z.get("z4"), z.get("z5")

    def hr(a, b):
        return [a, b] if (a or b) else None

    def pw(a, b):
        return [round(ftp * a), round(ftp * b)] if ftp else None

    easy_cap = hr(None, z2 - 1 if z2 else None)
    zone2 = hr(z2, z3 - 1 if z3 else None)
    zone4 = hr(z4, z5 - 1 if z5 else None)
    zone5 = hr(z5, None)
    sw_easy, sw_mid, sw_fast = prof.get("swim_easy"), prof.get("swim_mid"), prof.get("swim_fast")

    def swim_pace(kind_):
        if not sw_mid:
            return None
        return {"rec": f"{_pace(sw_easy + 5)} o más lento",
                "suave": f"{_pace(sw_easy)} o más lento",
                "aerobico": f"{_pace(sw_mid)}–{_pace(sw_easy)}",
                "larga": f"{_pace(sw_mid)}–{_pace(sw_easy)}",
                "umbral": f"{_pace(sw_fast - 4)}–{_pace(sw_fast)}",
                "vo2": f"{_pace(sw_fast - 10)}–{_pace(sw_fast - 5)}"}[kind_]

    def v(sport, minutes=None, power=None, heart=None, pace=None, steps=(), note=None, ok=True):
        return {"sport": sport, "minutes": minutes, "power": power, "hr": heart, "pace": pace,
                "steps": list(steps), "note": note, "ok": ok}

    run_hr = prof.get("run_hr")
    run_note = (f"Ve por pulso: tus carreras del último año van de media a {round(run_hr)} ppm, así que irás bastante más lento de lo habitual. Si hace falta para no pasarte, camina a ratos."
                if run_hr and z2 and run_hr >= z2 else None)

    def running(sport, minutes, heart, steps, incline=None):
        if run_block:
            if sport == "Cinta":
                return v(sport, [40, 50], None, easy_cap, None,
                         ["Caminar a paso vivo con 6–10 % de inclinación.", "Sin correr: mueves el cuerpo sin impacto."],
                         f"Hoy sin correr: {run_block}. En su lugar, caminata inclinada.")
            return v(sport, None, None, None, None, [], f"Hoy no: {run_block}.", ok=False)
        st_ = list(steps)
        if incline:
            st_.insert(0, f"Pon la cinta al {incline} de inclinación (simula el aire de la calle).")
        return v(sport, minutes, None, heart, None, st_, run_note if kind in ("suave", "aerobico", "larga") else None)

    if kind == "recuperacion":
        return [
            v("Rodillo", [30, 45], pw(.50, .60), easy_cap, None, ["Muy suave, cadencia alta y cómoda.", "Sin ningún cambio de ritmo."]),
            v("Elíptica", [30, 40], None, easy_cap, None, ["Resistencia baja y movimiento fluido.", "Debes poder hablar sin esfuerzo."]),
            v("Carrera", None, None, None, None, [], "Hoy no: el impacto de correr no ayuda a recuperar.", ok=False),
            v("Cinta", [30, 40], None, easy_cap, None, ["Caminar a paso vivo con 5–8 % de inclinación.", "Sin correr."]),
            v("Natación", [20, 30], None, None, swim_pace("rec"), ["Nado continuo muy suave, con algún largo de técnica o espalda.", "Respiración cómoda cada 3 brazadas."]),
        ]
    if kind == "suave":
        return [
            v("Rodillo", [60, 75], pw(.58, .70), easy_cap, None, ["10 min de calentamiento progresivo.", "45–60 min constantes en la parte baja del rango.", "5 min soltando."]),
            v("Elíptica", [50, 60], None, easy_cap, None, ["10 min suaves para entrar en calor.", "40–50 min constantes, resistencia moderada.", "Debes poder hablar todo el rato."]),
            running("Carrera", [35, 45], easy_cap, ["Trote muy suave y constante.", "Si el pulso se pasa del límite, camina hasta que baje."]),
            running("Cinta", [35, 45], easy_cap, ["Trote muy suave y constante.", "Si el pulso se pasa del límite, baja la velocidad o camina."], "1 %"),
            v("Natación", [30, 40], None, None, swim_pace("suave"), ["Nado continuo y relajado, sin series.", "Si quieres, 4 × 50 m de técnica a mitad."]),
        ]
    if kind == "aerobico":
        return [
            v("Rodillo", [75, 90], pw(.65, .75), zone2, None, ["10 min de calentamiento.", "60–75 min en zona 2 sin picos.", "5 min soltando."]),
            v("Elíptica", [60, 75], None, zone2, None, ["10 min de calentamiento.", "45–60 min en zona 2, resistencia constante.", "5 min soltando."]),
            running("Carrera", [45, 60], zone2, ["10 min de trote muy suave.", "30–45 min en zona 2.", "5 min caminando."]),
            running("Cinta", [45, 60], zone2, ["10 min de trote muy suave.", "30–45 min en zona 2.", "5 min caminando."], "1 %"),
            v("Natación", [40, 50], None, None, swim_pace("aerobico"),
              ["200 m suaves de calentamiento.", "4 × 400 m con 30 s de descanso, a ritmo constante.", "200 m suaves."]),
        ]
    if kind == "larga":
        return [
            v("Bici", [120, 180], pw(.60, .72), hr(None, z3 - 1 if z3 else None), None,
              ["Mejor en exterior. Ritmo cómodo todo el tiempo, sin cebarte en las subidas.", "Come algo cada 45 min y bebe cada 15–20 min."]),
            v("Elíptica", [75, 90], None, zone2, None, ["Ritmo constante en zona 2.", "Bebe cada 15–20 min."]),
            running("Carrera", [70, 90], hr(None, z3 - 1 if z3 else None), ["Muy tranquilo, la mayor parte en zona 2.", "Bebe si pasa de 75 min."]),
            running("Cinta", [60, 75], zone2, ["Constante en zona 2.", "Cambia la inclinación entre 1 y 3 % cada 10 min para no cargar siempre igual."], "1 %"),
            v("Natación", [50, 60], None, None, swim_pace("larga"), ["Nado continuo largo, o 3 × 800 m con 45 s de descanso."]),
        ]
    if kind == "umbral":
        return [
            v("Rodillo", [70, 80], pw(.95, 1.00), zone4, None,
              ["15 min de calentamiento con 3 aceleraciones de 30 s.", "3 × 10 min al rango indicado, con 5 min muy suaves entre bloques.", "10–15 min soltando."]),
            v("Elíptica", [60, 70], None, zone4, None,
              ["15 min de calentamiento.", "3 × 10 min en zona 4 subiendo resistencia, con 4 min suaves entre bloques.", "10 min soltando."]),
            running("Carrera", [55, 65], zone4,
                    ["15 min de trote suave con 4 aceleraciones cortas.", "3 × 8 min en zona 4, con 3 min de trote suave entre series.", "10 min soltando."]),
            running("Cinta", [55, 65], zone4,
                    ["15 min de trote suave.", "3 × 8 min en zona 4, con 3 min de trote suave entre series.", "10 min soltando."], "1 %"),
            v("Natación", [45, 55], None, None, swim_pace("umbral"),
              ["300 m suaves y 4 × 50 m progresivos.", "6 × 200 m al ritmo indicado, con 30 s de descanso.", "200 m suaves."]),
        ]
    if kind == "vo2":
        return [
            v("Rodillo", [65, 75], pw(1.10, 1.18), zone5, None,
              ["15 min de calentamiento con 3 aceleraciones de 30 s.", "5 × 4 min al rango indicado, con 4 min muy suaves entre series.", "10–15 min soltando."]),
            v("Elíptica", [55, 65], None, zone5, None,
              ["15 min de calentamiento.", "5 × 3 min muy fuertes, con 3 min suaves entre series.", "10 min soltando."]),
            running("Carrera", [50, 60], zone5,
                    ["15 min de trote suave con 4 aceleraciones cortas.", "5 × 3 min fuertes, con 3 min de trote suave entre series.", "10 min soltando."]),
            running("Cinta", [50, 60], zone5,
                    ["15 min de trote suave.", "5 × 3 min fuertes, con 3 min de trote suave entre series.", "10 min soltando."], "2 %"),
            v("Natación", [40, 50], None, None, swim_pace("vo2"),
              ["300 m suaves y 4 × 50 m progresivos.", "10 × 100 m al ritmo indicado, con 20–30 s de descanso.", "200 m suaves."]),
        ]
    return []


def recommend(rows, acts, today, zones=None, ftp=None, prof=None):
    """Elige el entreno de hoy (o de mañana si hoy ya hay actividad).

    rows: salida de compute(). acts: [{day, duration, type, te_aer, te_ana}].
    zones: {z1..z5: suelo de cada zona en ppm}. ftp: vatios.
    Las reglas siguen las guías de VFC del AIS, el modelo forma/fatiga, el ratio
    agudo:crónico y la monotonía de Foster, y lo que mejor te ha funcionado en tu histórico
    (constancia, salidas largas y unas dos sesiones duras por semana).
    """
    if not rows:
        return None
    R = {r["day"]: r for r in rows}
    t = date.fromisoformat(today)
    trained_today = any(a["day"] == today for a in acts)
    target = t + timedelta(days=1) if trained_today else t
    last = target - timedelta(days=1)          # último día que cuenta como pasado
    lastd = last.isoformat()

    by_day = {}
    for a in acts:
        by_day.setdefault(a["day"], []).append(a)

    def window(n):
        return [(last - timedelta(days=i)).isoformat() for i in range(n)]

    streak = 0
    d = last
    while by_day.get(d.isoformat()):
        streak += 1
        d -= timedelta(days=1)
    hard3 = sum(1 for x in window(3) for a in by_day.get(x, []) if _is_hard(a))
    hard7 = sum(1 for x in window(7) for a in by_day.get(x, []) if _is_hard(a))
    hard_yday = any(_is_hard(a) for a in by_day.get(lastd, []))
    hard_days = sorted({a["day"] for a in acts if _is_hard(a) and a["day"] <= lastd})
    days_since_hard = (target - date.fromisoformat(hard_days[-1])).days if hard_days else 99
    long_days = sorted({a["day"] for a in acts if (a.get("duration") or 0) >= 7200 and a["day"] <= lastd})
    days_since_long = (target - date.fromisoformat(long_days[-1])).days if long_days else 99

    # Estado: la fila del último día pasado (forma, fatiga, ratio, monotonía al cierre de ese día)
    st = R.get(lastd) or rows[-1]
    form = None
    if st.get("ctl") and st["ctl"] >= 10:
        form = (st["ctl"] - st["atl"]) / st["ctl"] * 100   # frescura al empezar el día objetivo
    # Recuperación: la noche más reciente medida (hoy por la mañana, o la última que haya)
    morning = R.get(target.isoformat()) if not trained_today else None
    rec = morning if morning and morning.get("hrv") is not None else R.get(today) or st
    hrv, lo, hi, hrv7 = rec.get("hrv"), rec.get("hrv_lo"), rec.get("hrv_hi"), rec.get("hrv7")
    rhr, base, sd = rec.get("rhr"), rec.get("rhr_base"), rec.get("rhr_sd")
    sleep = rec.get("sleep")
    hrv_low_streak = 0
    for x in sorted(R)[::-1]:
        if x > (target.isoformat() if morning else today):
            continue
        r = R[x]
        if r.get("hrv") is None:
            continue
        if r.get("hrv_lo") is not None and r["hrv"] < r["hrv_lo"]:
            hrv_low_streak += 1
        else:
            break

    hrv_low = hrv is not None and lo is not None and hrv < lo
    hrv7_low = hrv7 is not None and lo is not None and hrv7 < lo
    hrv7_high = hrv7 is not None and hi is not None and hrv7 > hi
    rhr_high = rhr is not None and base is not None and sd is not None and rhr > base + sd
    acwr = st.get("acwr")
    mono = st.get("mono")

    why = []

    def add(level, text):
        why.append({"level": level, "text": text})

    # La frescura en % solo es fiable con algo de forma física acumulada
    form_ok = form is not None and (st.get("ctl") or 0) >= 30
    fatigued = (form_ok and form < -20) or hard3 >= 2 or hrv7_low or hrv_low

    # ---- elección (de más a menos restrictivo)
    kind = None
    # Descanso
    if hrv_low_streak >= 4:
        kind = "descanso"; add("bad", f"Tu VFC lleva {hrv_low_streak} noches por debajo de tu rango normal.")
    if hrv_low and rhr_high and hrv_low_streak >= 2:
        kind = "descanso"; add("bad", "VFC baja y FC en reposo alta varios días: puede ser cansancio acumulado o el inicio de una enfermedad.")
    if streak >= 6 and fatigued:
        kind = "descanso"; add("warn", f"Llevas {streak} días seguidos entrenando y se nota la fatiga: toca un día libre para asimilar.")
    if form_ok and form < -30 and hrv7_low:
        kind = "descanso"; add("bad", "Mucha fatiga acumulada y la VFC ya lo acusa.")
    if sleep is not None and sleep < 5.5 and (hrv_low or rhr_high):
        kind = "descanso"; add("warn", f"Has dormido solo {_es(sleep, 1)} h y la recuperación sale baja.")

    # Recuperación activa
    if kind is None:
        triggers = []
        if 2 <= hrv_low_streak <= 3:
            triggers.append(f"Tu VFC lleva {hrv_low_streak} noches por debajo de tu rango normal.")
        if hrv_low and rhr_high and hrv_low_streak < 2:
            triggers.append("VFC baja y FC en reposo alta a la vez.")
        if form_ok and form < -40:
            triggers.append(f"La fatiga supera mucho a tu forma física (frescura {_es(form, 0)} %).")
        if triggers:
            kind = "recuperacion"
            for x in triggers:
                add("warn", x)

    soft = []
    if hrv_low and hrv_low_streak < 2 and not rhr_high:
        soft.append("Tu VFC de anoche está por debajo de tu rango normal.")
    if rhr_high and not hrv_low:
        soft.append("Tu FC en reposo está más alta de lo normal.")
    if form_ok and -40 <= form < -30:
        soft.append(f"La fatiga supera a tu forma física (frescura {_es(form, 0)} %).")
    if acwr is not None and acwr > 1.5:
        soft.append(f"Has subido la carga muy deprisa (ratio agudo:crónico {_es(acwr, 2)}): sin intensidad.")
    if streak >= 6 and not fatigued:
        soft.append(f"Llevas {streak} días seguidos entrenando.")
    if hard3 >= 2:
        soft.append(f"Llevas {hard3} sesiones duras en los últimos 3 días.")
    elif hard_yday:
        soft.append("Ayer hiciste una sesión dura.")
    if mono is not None and mono > 2 and streak >= 4:
        soft.append(f"Semana muy monótona (monotonía {_es(mono, 2)}) y {streak} días seguidos.")
    elif 5 <= streak < 6:
        soft.append(f"Llevas {streak} días seguidos entrenando.")
    if soft and kind is None:
        kind = "suave"
    for x in soft:
        add("warn", x)

    ready = ((hrv is not None or rhr is not None) and not hrv_low and not hrv7_low and not rhr_high
             and (not form_ok or form >= -20) and (acwr is None or acwr <= 1.3))
    if kind is None and target.weekday() >= 5 and days_since_long >= 6:
        kind = "larga"
        add("ok", f"Tu última salida de 2 horas o más fue hace {days_since_long if days_since_long < 99 else 'mucho'} días. Las salidas largas son lo que más te ha hecho mejorar en bici.")
    if kind is None and ready and hard7 < 2 and days_since_hard >= 2:
        kind = "vo2" if (hrv7_high and days_since_hard >= 4) else "umbral"
        add("ok", "Estás recuperado: VFC y FC en reposo en tu rango normal.")
        add("ok", f"Llevas {hard7} sesiones duras esta semana; en tus mejores bloques hacías unas 2.")
    if kind is None:
        kind = "aerobico"
        if hard7 >= 2:
            add("ok", f"Ya llevas {hard7} sesiones duras esta semana: el resto, en zona 2.")
        elif not ready:
            add("ok", "Recuperación correcta, aunque no del todo para meter intensidad.")
        else:
            add("ok", "Toca volumen en zona 2, la base de tus mejores bloques.")

    if hrv is not None and lo is not None and not hrv_low and not any("VFC" in w["text"] for w in why):
        add("ok", "Tu VFC está dentro de tu rango normal.")
    if rhr is not None and base is not None and not rhr_high and not any("FC en reposo" in w["text"] for w in why):
        add("ok", "Tu FC en reposo está en tu nivel normal.")

    # ---- prescripción
    z = zones or {}

    def pw(a, b):
        return [round(ftp * a), round(ftp * b)] if ftp else None

    z2, z3, z4, z5 = z.get("z2"), z.get("z3"), z.get("z4"), z.get("z5")
    P = {
        "descanso": dict(title="Descanso", minutes=None, power=None, hr=None,
                         summary="Día libre. Como mucho, un paseo o movilidad suave.",
                         steps=["Sin entreno. Si te apetece moverte, paseo de 20–30 min o estiramientos."]),
        "recuperacion": dict(title="Recuperación activa", minutes=[30, 45], power=pw(.50, .60),
                             hr=[None, z2 - 1] if z2 else None,
                             summary="Muy suave y corto, para mover las piernas sin sumar fatiga.",
                             steps=["Rodillo muy suave, cadencia alta y cómoda.", "Alternativa: 20–30 min de piscina tranquila."]),
        "suave": dict(title="Rodaje suave", minutes=[60, 75], power=pw(.58, .70),
                      hr=[None, z2 - 1] if z2 else None,
                      summary="Constante y fácil: debes poder hablar todo el rato.",
                      steps=["10 min de calentamiento progresivo.", "45–60 min constantes en la parte baja del rango.", "5 min soltando."]),
        "aerobico": dict(title="Fondo aeróbico", minutes=[75, 90], power=pw(.65, .75),
                         hr=[z2, z3 - 1] if z2 and z3 else None,
                         summary="Zona 2 sostenida: la base que más te ha funcionado.",
                         steps=["10 min de calentamiento.", "60–75 min en zona 2 sin picos.", "5 min soltando."]),
        "larga": dict(title="Salida larga", minutes=[120, 180], power=pw(.60, .72),
                      hr=[None, z3 - 1] if z3 else None,
                      summary="Larga y tranquila, mejor en exterior. Come y bebe desde el principio.",
                      steps=["Ritmo cómodo todo el tiempo, sin cebarte en las subidas.", "Come algo cada 45 min y bebe cada 15–20 min."]),
        "umbral": dict(title="Umbral", minutes=[70, 80], power=pw(.95, 1.00),
                       hr=[z4, z5 - 1] if z4 and z5 else None,
                       summary="Calidad: 3 bloques de 10 minutos a tu umbral.",
                       steps=["15 min de calentamiento con 3 aceleraciones de 30 s.",
                              "3 × 10 min al rango indicado, con 5 min muy suaves entre bloques.",
                              "10–15 min soltando."]),
        "vo2": dict(title="Intervalos VO2máx", minutes=[65, 75], power=pw(1.10, 1.18),
                    hr=[z5, None] if z5 else None,
                    summary="Calidad: 5 series de 4 minutos fuertes.",
                    steps=["15 min de calentamiento con 3 aceleraciones de 30 s.",
                           "5 × 4 min al rango indicado, con 4 min muy suaves entre series.",
                           "10–15 min soltando."]),
    }[kind]
    rest_if = []
    if kind not in ("descanso",):
        if lo is not None:
            rest_if.append(f"tu VFC de esa noche sale por debajo de {_es(lo, 1)} ms")
        if base is not None and sd is not None:
            rest_if.append(f"tu FC en reposo llega a {round(base + sd)} ppm o más")

    # Carrera: el impacto necesita unas 48 h tras una carrera dura
    RUN = ("running", "treadmill_running", "trail_running", "track_running", "street_running")
    runs2 = [a for x in window(2) for a in by_day.get(x, []) if (a.get("type") or "") in RUN]
    run_block = None
    if any(_is_hard(a) for a in runs2):
        run_block = "corriste fuerte hace menos de 48 h y el impacto necesita más recuperación"
    elif runs2 and kind in ("umbral", "vo2"):
        run_block = "corriste en los dos últimos días: mejor no sumar series de impacto"
    sports = sport_versions(kind, z, ftp, prof or {}, run_block)
    return {
        "day": target.isoformat(),
        "label": ("Hoy" if not trained_today else "Mañana") + f", {DIAS[target.weekday()]} {target.day} de {MESES[target.month - 1]}",
        "trained_today": trained_today, "kind": kind, **P, "sports": sports,
        "why": why, "rest_if": rest_if, "ftp": ftp, "zones_source": z.get("source"),
        "state": {"streak": streak, "hard3": hard3, "hard7": hard7, "days_since_long": days_since_long if days_since_long < 99 else None,
                  "form": round(form, 1) if form is not None else None, "hrv": hrv, "hrv_lo": lo, "rhr": rhr},
    }


# ---------------------------------------------------------------- resumen semanal

def weekly(acts):
    """acts: [{day, type, duration, distance, load}] → una fila por semana (de lunes a domingo)."""
    weeks = {}
    for a in acts:
        try:
            d = date.fromisoformat(a["day"])
        except Exception:
            continue
        monday = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(monday, {"week": monday, "n": 0, "hours": 0.0, "km": 0.0, "load": 0.0,
                                      "by": {g: 0.0 for g in SPORT_GROUPS}})
        h = (a.get("duration") or 0) / 3600
        w["n"] += 1
        w["hours"] += h
        w["km"] += (a.get("distance") or 0) / 1000
        w["load"] += a.get("load") or 0
        w["by"][sport_group(a.get("type"))] += h
    out = []
    for k in sorted(weeks):
        w = weeks[k]
        out.append({**w, "hours": round(w["hours"], 2), "km": round(w["km"], 1), "load": round(w["load"]),
                    "by": {g: round(v, 2) for g, v in w["by"].items()}})
    return out
