"""Lee el VO2máx que el reloj guarda en el archivo .FIT de cada actividad.

Garmin no publica ese dato en la API, pero el .FIT original lo lleva en un mensaje
interno sin documentar (número 140): el campo 7 es el VO2máx × 65536 / 3,5.
Como el formato no está documentado, solo se acepta un valor entre 15 y 90 ml/kg/min;
cualquier otra cosa se descarta en lugar de mostrar una cifra errónea.
"""
import io
import struct
import zipfile

MSG = 140
FIELD = 7
SCALE = 3.5 / 65536
VALID = (15.0, 90.0)


def unzip_fit(data):
    """Devuelve los bytes del .FIT, venga suelto o dentro de un ZIP."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fit")] or z.namelist()
            return z.read(names[0]) if names else b""
    return data


def read_vo2(fit):
    """VO2máx del archivo, o None si no lo trae o el valor no es creíble.

    Lanza ValueError si el archivo no es un .FIT válido.
    """
    if len(fit) < 14 or fit[8:12] != b".FIT":
        raise ValueError("no es un archivo .FIT")
    hsize = fit[0]
    end = min(len(fit), hsize + struct.unpack_from("<I", fit, 4)[0])
    p, defs, value = hsize, {}, None
    while p < end:
        h = fit[p]
        p += 1
        if h & 0x80:                       # cabecera de marca de tiempo comprimida
            local, is_def, dev = (h >> 5) & 3, False, False
        else:
            local, is_def, dev = h & 0x0F, bool(h & 0x40), bool(h & 0x20)
        if is_def:
            arch = fit[p + 1]
            gnum = struct.unpack_from(">H" if arch else "<H", fit, p + 2)[0]
            nf = fit[p + 4]
            p += 5
            fields = []
            for _ in range(nf):
                fields.append((fit[p], fit[p + 1]))
                p += 3
            devsize = 0
            if dev:
                nd = fit[p]
                p += 1
                for _ in range(nd):
                    devsize += fit[p + 1]
                    p += 3
            defs[local] = (gnum, fields, arch, devsize)
        else:
            if local not in defs:
                raise ValueError("mensaje sin definición")
            gnum, fields, arch, devsize = defs[local]
            if gnum == MSG:
                q = p
                for num, size in fields:
                    if num == FIELD and size == 4:
                        raw = struct.unpack_from(">I" if arch else "<I", fit, q)[0]
                        if raw not in (0, 0xFFFFFFFF):
                            v = raw * SCALE
                            if VALID[0] <= v <= VALID[1]:
                                value = round(v, 1)
                    q += size
            p += sum(s for _, s in fields) + devsize
    return value


RUN_TYPES = ("running", "treadmill_running", "trail_running", "track_running", "street_running")


def sport_of(type_key):
    t = (type_key or "").lower()
    if t in RUN_TYPES or "run" in t:
        return "carrera"
    if any(k in t for k in ("cycl", "bik", "ride", "spin")):
        return "bici"
    return None
