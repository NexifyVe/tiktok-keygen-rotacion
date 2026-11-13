# Rotación automática de claves RapidAPI (v3.2.0 + parche)

Esta versión añade al proyecto original [Loukious/TikTokStreamKeyGenerator](https://github.com/Loukious/TikTokStreamKeyGenerator) (tag v3.2.0) un **pool de claves RapidAPI con rotación automática**, pensado para 3 claves de cuentas distintas.

## Qué hace

- Cada respuesta de RapidAPI ya trae las cabeceras `x-ratelimit-requests-*`. El pool las registra **por clave** (coste cero en peticiones) en `logs/rapidapi_keypool.json`.
- Cuando la clave activa baja de **200 peticiones restantes** (umbral configurable), la siguiente llamada de firma usa automáticamente la siguiente clave con cuota. El directo **no se interrumpe**: ni el espectador ni TikTok notan el cambio.
- Si un 429 llega con cuota mensual sana (límite horario/ráfaga), la clave se "estaciona" 5 minutos y se rota a la de respaldo; vuelve sola al expirar.
- Una clave rechazada con 401/403 se marca inválida y se salta hasta que una respuesta exitosa la rehabilite.
- Cuando la cuota mensual de una clave se resetea (cabecera `reset`), vuelve a considerarse disponible: el pool **se recupera solo el mes siguiente**.
- Si **ninguna** clave supera el umbral: el directo se **pausa automáticamente** (heartbeat en modo pausa, igual que el botón Pause) y se **reanuda solo** en cuanto haya de nuevo una clave con cuota (pegas una nueva, añades un respaldo, o pasa el reset mensual). Una pausa hecha a mano nunca se reanuda automáticamente.

## Dónde se configura (GUI)

Sección de cuenta: debajo del campo "RapidAPI Key" hay dos controles nuevos:

- **Backup RapidAPI Keys**: una clave de respaldo por línea (una de otra cuenta RapidAPI, no de la misma app).
- **"Rotate when the active key has ≤ [200] requests left"**: margen de rotación.

En `config.json` equivale a:

```json
{
  "rapidapi_key": "clave-principal",
  "rapidapi_backup_keys": ["respaldo-1", "respaldo-2"],
  "rapidapi_rotation_threshold": 200
}
```

La barra "RapidAPI Quota" muestra ahora la clave activa (`· key 2/3`) y su tooltip lista el estado de cada clave. Las rotaciones se anuncian en el estado del proxy del panel LIVE.

## Cómo ejecutarla (Windows)

El `.exe` oficial de la release no se puede parchear in situ (está compilado con Nuitka). Esta versión se ejecuta desde el código fuente:

1. Instala **Python 3.10–3.13** (3.12 recomendado) desde python.org y marca "Add to PATH".
2. Descomprime el ZIP y abre una terminal en la carpeta.
3. Instala las dependencias y lanza la app:

```
py -m pip install -r requirements.txt
py TiktokStreamKeyGenerator.py
```

4. **FFmpeg**: pon `ffmpeg.exe` en una subcarpeta `bin\` junto al código (o agrégalo al PATH). La app lo busca en `bin\`, en la carpeta actual y en el PATH.
5. Inicia sesión con QR como siempre, pega tu clave principal y las 2 de respaldo, y guarda.

> Alternativa para un `.exe` propio: `py -m pip install -r requirements-build.txt` y sigue `scripts/build_release.py` (requiere Nuitka; en Windows produce el ejecutable con estos cambios).

## Archivos modificados

| Archivo | Cambio |
| --- | --- |
| `Libs/rapidapi_keypool.py` | **Nuevo**: selección de clave, registro por clave, park 5 min, invalid, reset mensual. |
| `Libs/signers.py` | La firma webcast pide la clave al pool y reporta cuota/429/401/403 por clave. |
| `Libs/XFrameSign.py` | Ídem para la firma de frames SEI (la que usa el proceso hijo del proxy). |
| `TiktokStreamKeyGenerator.py` | GUI (respaldos + umbral), config, env del hijo, barra de cuota por clave activa, pausa/reanudación automáticas. |
| `tests/test_rapidapi_keypool.py`, `tests/test_key_rotation_gui.py` | **Nuevos**: 28 pruebas (rotación, park, invalid, reset, persistencia, GUI). |

## Diagnóstico del delay variable (4 s / 8 s / 15 s)

Lo verifiqué en el código del proxy SEI (`Libs/ffmpeg_sei_proxy.py` + `Libs/XFrameSign.py`):

1. El video no va directo a TikTok: pasa por el proxy local que inserta las firmas SEI. Cada firma se refresca con RapidAPI y se **cachea 5 minutos**.
2. Si un refresco falla (429 por cuota agotada, 5xx, red), `LocalSeiSigner.signed_payload` lanza `"Signature refresh failed; forwarding was halted"`: el proxy **deja de reenviar video** hasta que OBS se reconecta al listener local.
3. Durante ese hueco, TikTok bufferiza. Cuando el flujo retoma, el reproductor acumula el tiempo del hueco como **delay permanente** (TikTok no recorta para alcanzar). 4 s → 8 s → 15 s según cuánto duró cada parada, y nunca baja: por eso terminas reiniciando el directo.
4. El consumo es feroz: un heartbeat cada **5 s** son ~720 peticiones/hora, más los lotes SEI. Una sola clave muere en horas.

**Con la rotación a 200 la causa principal desaparece**: la clave nunca llega a 0, no hay 429 de cuota a mitad de firma, y el refresco de SEI siempre encuentra una clave sana. El delay debería quedarse plano como en la extensión de OBS. Aun así, en OBS conviene un intervalo de keyframes de **2 s** para que cualquier microcorte de red se recupere rápido.

## Sobre la extensión de OBS

No la analicé a fondo (es un repositorio aparte en C++). Tus síntomas son coherentes con lo que vi aquí: casi cero delay (firma nativa en proceso, sin proxy intermedio), pero sin la maquinaria de cuota — cuando se acaban las peticiones no hay pausa estructurada — y con opciones bloqueadas durante el stream, típico de plugins que solo habilitan cambios en parada. Si quieres, la analizo en otra sesión.

## Limitaciones

- No hay claves reales en este entorno: la validación contra RapidAPI/TikTok es tuya. La lógica está cubierta por 28 pruebas unitarias (suite original + nuevas; solo `test_system_theme` falla en este sandbox, y falla igual en el código original).
- El "remaining" de una clave de respaldo se conoce tras su primera llamada; hasta entonces la barra muestra esa clave con cuota desconocida.
- Cada clave debe ser de una **cuenta RapidAPI distinta**: dos claves de la misma app comparten cuota.
