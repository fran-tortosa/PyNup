# PyNup

Aplicación GUI de Windows para empaquetar o extraer un juego NUP y comprobarlo
automáticamente con SHA-256. El manifiesto `.sha256` generado usa el formato
que RapidCRC Unicode puede leer. Este proyecto no apoya a la piratería.

## Ejecutable

La compilación estable 1.1 es
`dist\PyNup-1.1-estable\PyNup.exe`. Para compilarla en Windows desde esta
carpeta con Python 3.10 o posterior:

```powershell
.\preparar-icono.ps1
..\..\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
..\..\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm PyNup.spec `
  --distpath ..\PyNup-1.1-estable `
  --workpath ..\..\build\PyNup-1.1
```

La aplicación final es de un solo archivo y no muestra una ventana de consola.
PyNup incluye la implementación MIT de NUP y la dependencia Zstandard.
Antes de compilar tras cambiar la imagen original, regenera los tamaños nativos
y el `.ico` con:

```powershell
.\preparar-icono.ps1
```

## Versiones locales

`dist\PyNup-1.0-estable` y `dist\PyNup-1.0-code` se conservan como referencia
histórica. Esta carpeta contiene el código estable `PyNup-1.1`; la beta de
evolución permanece separada en `dist\PyNup-1.1-beta-code` y su ejecutable en
`dist\PyNup-1.1-beta`. La ventana presenta versión y fecha de compilación. El
PNG maestro está en `icono-personalizado\icono.png`; el script anterior genera
las imágenes de ventana y barra de tareas y el `.ico` incrustado.

La versión 1.1 muestra esta secuencia de seis pasos al **empaquetar y comprobar**:

1. Analizar la estructura del dump.
2. Validar o crear el manifiesto SHA-256.
3. Crear el archivo NUP con Zstandard.
4. Verificar el contenedor con NUP Verify.
5. Extraer temporalmente para comprobarlo.
6. Comparar SHA-256 y limpiar los temporales.

Al **extraer y comprobar** muestra:

1. Validar el contenedor, el destino y el manifiesto si se seleccionó.
2. Verificar el contenedor con NUP Verify.
3. Reconstruir las rutas de los paquetes.
4. Extraer los archivos.
5. Comparar SHA-256 si hay manifiesto; si no, indicar que se omitió.
6. Finalizar la extracción y la comprobación disponible.

## Empaquetar y comprobar

1. En **Empaquetar a formato NUP + Comprobar**, selecciona la raíz completa del dump. NUP
   detecta los paquetes por su estructura oficial:

   ```text
   MiJuego\
   ├── base\
   │   ├── exefs\                 # exefs y romfs son opcionales por paquete
   │   └── romfs\                 # se permiten subcarpetas y archivos anidados
   ├── updates\
   │   ├── 655360\
   │   │   ├── exefs\
   │   │   └── romfs\
   │   └── 720896\
   │       └── romfs\
   └── dlc\
       └── 010074A01BF12000\
           └── 0\
               └── romfs\
   ```

   También se aceptan dumps solo de actualizaciones (`updates/` sin `base/`),
   DLC junto a estas, y una carpeta de contenido sencilla con `exefs/` y/o
   `romfs/` en la raíz. Las carpetas DLC que no cumplan la estructura reconocida
   por NUP se omiten, igual que hace el empaquetador de referencia.
2. El `.nup` se genera automáticamente en `output` junto al ejecutable, con el
   nombre `<nombre del juego> (<TITLE ID>) (v<versión máxima>).nup`, por ejemplo
   `Juego (0100A3A01EE4C000) (v720896).nup`. El nombre se busca por TITLE ID en
   `db-name.ini`, junto al ejecutable. Si no hay una entrada para ese ID, se
   usa el nombre de la carpeta de origen. Añade los nombres en la sección
   `[games]`, por ejemplo `0100A3A01EE4C000 = "Juego"`. Se usa
   el TITLE ID del juego base o, si no
   hay base, el de una actualización. Si el dump solo contiene DLC, se usa su
   TITLE ID cuando todos comparten uno; varios TITLE ID de DLC no permiten
   crear un nombre único. El manifiesto se guarda en `crc-sha256`, también
   junto al ejecutable, con el mismo nombre base y el sufijo `_original.sha256`.
   El manifiesto incluye los
   archivos de todos los paquetes que NUP va a empaquetar, con sus rutas
   relativas originales.
3. Selecciona la compresión Zstandard:

   | Opción | Nivel | Uso recomendado |
   |---|---:|---|
   | Baja | 3 | Recomendada para uso habitual: rápida y con buena compresión. |
   | Media | 11 | Para reducir más el tamaño aceptando que tarde más. |
   | Alta | 22 | Compresión máxima; muy lenta y normalmente solo útil si el tamaño importa mucho más que el tiempo. |

   Pulsa **Empaquetar y comprobar todo**. PyNup indica el paso actual de seis
   fases y presenta una barra de progreso general por etapas. Los archivos de
   hasta 16 MiB se procesan de una pieza; los mayores se comprimen por bloques
   de hasta 64 MiB. La verificación profunda y la extracción también leen y
   descomprimen en bloques
   de hasta 64 MiB, en lugar de cargar archivos completos. El lector no mapea
   el NUP completo en memoria; en Windows solicita acceso secuencial para
   favorecer la liberación de páginas ya leídas. La ventana Zstandard no tiene
   un límite artificial. La extracción mantiene escrituras con vaciado al disco
   por bloques de 16 MiB para evitar que las escrituras pendientes crezcan
   indefinidamente en la caché del sistema.
   PyNup también muestra en el registro la opción y el nivel seleccionados, y crea automáticamente
   un manifiesto SHA-256 compatible con RapidCRC, comprueba los archivos originales,
   empaqueta todos los paquetes juntos en un NUP y ejecuta la verificación
   profunda del contenedor. Después extrae todos los paquetes a una carpeta
   temporal dentro de `temp`, conservando `base/`, cada versión de `updates/` y
   las rutas de DLC conocidas; compara todos los hashes con el manifiesto y
   elimina la carpeta temporal al terminar, tanto si la comparación pasa como si
   falla.

NUP no almacena el nombre original del índice de cada DLC en el registro del
paquete. PyNup conserva ese mapeo durante la comprobación inmediata de
empaquetado y, al extraer posteriormente, lo recupera del manifiesto SHA-256
original. Por ello, para extraer y comprobar un NUP de DLC selecciona el
manifiesto generado para ese dump; PyNup se niega a adivinar un índice si no
puede reconstruirlo.

Las carpetas `output`, `crc-sha256` y `temp` se crean automáticamente junto al
ejecutable cuando se inicia el empaquetado. Si el manifiesto esperado ya existe, se conserva como referencia y se valida
antes de empaquetar. El `.nup` existente no se sobrescribe. La extracción temporal
de comprobación nunca se conserva. Si el manifiesto existente está vacío o no se
puede leer, se regenera automáticamente; si es válido pero sus hashes no
coinciden con el origen, el empaquetado se detiene.

Cada operación crea un registro independiente en `log`, junto al ejecutable,
con fecha y hora, el nombre del archivo asociado, los pasos, los comandos
internos y externos y los mensajes de diagnóstico. El progreso general,
incluido el paso de Zstandard, se resume en este log como máximo cada 10
segundos en lugar de escribir una línea por cada cambio de fase. Para cada
empaquetado se conserva además el detalle de todas las actualizaciones de
Zstandard, con fecha y hora, en `log\zstandard`; solo se mantienen los tres
registros más recientes. Los archivos empaquetados y desempaquetados se
registran en `log\build`, y el progreso de la GUI se actualiza en cada
actualización de archivo. Los comandos internos se conservan solo en el archivo
de log, no en el registro visible de la GUI. El registro de la GUI se limpia al
iniciar cada operación; los archivos anteriores de `log` se conservan.
Durante la creación del manifiesto se muestra qué archivo va
generando su SHA-256 (`archivo N de X`) y, al verificar, qué archivo del
manifiesto original está revisando (`Revisando N de X`). Se guardan logs de
texto con las líneas de los tres manifiestos más recientes en
`log\crc-sha256` tanto al crear como al revisar el manifiesto, no copias
`.sha256` de los manifiestos.
Cada comparación SHA-256 genera además su propio log línea por línea en
`log\crc-sha256`, identificado como `comparacion-empaqueta` o
`comparacion-extrae`, con hash esperado, hash calculado y resultado por archivo.

## Extraer y comprobar

1. En **Extraer a formato carpeta + Comprobar**, selecciona el `.nup` y la carpeta destino nueva.
   Por defecto, la extracción se crea junto al NUP y usa exactamente su nombre
   base, sin la extensión `.nup`.
   El manifiesto SHA-256 original es opcional; puedes elegir **Quitar CRC**.
2. PyNup verifica el contenedor con NUP Verify y extrae todos sus paquetes.
3. Si seleccionaste manifiesto, compara los archivos y reconstruye las rutas
   originales, incluidos los índices de DLC. Sin manifiesto, omite la
   comparación SHA-256. Si el NUP contiene DLC, asigna sus índices por orden
   porque NUP no guarda el índice original; sin DLC, no muestra ese aviso.

## Cancelar una operación

Al iniciar un trabajo, los campos y botones de configuración quedan desactivados
hasta que termine o se cancele. **Cancelar** solicita una detención segura al
terminar el bloque actual. Se elimina el NUP incompleto o la extracción parcial,
además del contenido de `temp`; todos los manifiestos de `crc-sha256` se conservan.

## RapidCRC Unicode

RapidCRC Unicode es una aplicación GUI y no ofrece un modo documentado de
verificación desatendida para este flujo. Por eso PyNup no la abre repetidamente:
crea un `.sha256` compatible con RapidCRC y verifica el mismo manifiesto
automáticamente con SHA-256. El manifiesto se puede abrir después en RapidCRC
para una comprobación manual independiente.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
