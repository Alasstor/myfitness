# MyFitness

Panel personal para tus datos de Garmin Connect, con estado de forma, VO2máx de Garmin y entreno recomendado del día. Usa la [Personal API oficial de Garmin](https://developer.garmin.com/gc-developer-program/personal/personal-api).

El mismo código funciona en dos sitios:

| Carpeta | Qué es |
|---|---|
| `garmin-local/` | La aplicación: `app.py` (servidor y sincronización), `forma.py` (cálculos y entreno recomendado), `fitvo2.py` (VO2máx de los .FIT) e `index.html` (la interfaz). En el PC se arranca con `iniciar.bat`. |
| `android/` | La app de Android. Lleva Python dentro (Chaquopy) y ejecuta los archivos de `garmin-local/` sin cambios. |

Cualquier mejora en `garmin-local/` llega a las dos.

## Instalar en el móvil

1. En el móvil, abre la última versión en **Releases** y descarga `MyFitness-….apk`.
2. Ábrelo y permite instalar apps de este origen cuando Android lo pida.
3. En la app, importa tus datos del PC o pega tu clave de la Personal API.

Para pasar tus datos del PC: en el PC, **Ajustes → Copia de seguridad → Exportar copia**, y en el móvil, **Importar archivo…** Otra opción es elegir directamente `garmin.sqlite` y `config.json` de la carpeta `datos` si está en Google Drive.

## Cómo se compila

Cada cambio en `android/` o `garmin-local/` lanza el workflow **APK de Android**, que compila el APK y lo publica en Releases.

Para que cada versión se instale encima de la anterior sin perder datos, el APK se firma siempre con la misma clave. Necesita dos secretos del repositorio (Settings → Secrets and variables → Actions):

- `KEYSTORE_BASE64`: el archivo de firma en base64.
- `KEYSTORE_PASSWORD`: su contraseña.

Sin ellos, el APK se firma con una clave temporal y para actualizar hay que desinstalar la versión anterior.

## Privacidad

Tus datos y tu clave nunca van al repositorio: `.gitignore` excluye la carpeta `datos`, las bases de datos y las claves de firma. En el móvil, el servidor interno solo responde a la propia app, que envía un código secreto en cada petición.
