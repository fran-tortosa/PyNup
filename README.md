<div align="center">

# 🖥️ PyNup

*Interfaz gráfica moderna para la gestión y trabajo con contenedores NUP (`.nup`).*

</div>

---

**PyNup** es un proyecto en desarrollo cuyo objetivo es ofrecer una interfaz gráfica para trabajar con contenedores **NUP (`.nup`)**, sin necesidad de utilizar la línea de comandos.

NUP es un formato de contenedor diseñado para agrupar contenido de juegos de **Nintendo Switch** y **Nintendo Switch 2** a partir de volcados previamente descifrados. Permite reunir el contenido base, las actualizaciones y los complementos (DLC) en un único archivo.

> [!NOTE]
> PyNup está pensado para facilitar la gestión y preservación de volcados propios. No descifra juegos ni proporciona contenido protegido.

---

## 📦 ¿Qué es NUP?

**NUP (NX Package)** es un formato de contenedor de código limpio (*clean-room*) para almacenar volcados sin cifrar de Nintendo Switch y Nintendo Switch 2.

Un archivo `.nup` puede contener distintos tipos de paquetes:

| Tipo | Contenido |
| :--- | :--- |
| **Base** | Contenido principal del juego. |
| **Update** | Una o varias actualizaciones del juego. |
| **Addon** | Contenido adicional o DLC. |

Cada paquete puede incluir los archivos de **ExeFS** y el árbol completo de **RomFS**. De esta forma, el contenido base, las actualizaciones y los DLC pueden organizarse dentro de un mismo contenedor, en lugar de mantener necesariamente cada parte como un conjunto independiente de carpetas.

---

## 💡 ¿Qué pretende ofrecer PyNup?

- El propósito principal de PyNup es hacer más accesible el uso del formato NUP mediante una interfaz visual cómoda. 
- También busca incorporar una capa adicional de comprobación en los procesos de creación y extracción, utilizando la herramienta RapidCRC Unicode para una verificación complementaria.

---

## 📂 Organización de los volcados

La implementación de referencia de NUP reconoce una estructura de carpetas específica para identificar automáticamente los paquetes que formarán el contenedor.

### Ejemplo de estructura:

```text
Volcado/
├── base/
│   ├── exefs/
│   └── romfs/
│
├── updates/
│   └── 458752/
│       ├── exefs/
│       └── romfs/
│
└── dlc/
    └── 040074A01BF13001/
        └── 0/
            ├── exefs/
            └── romfs/
```
### Descripción de elementos:

| Carpeta / Ruta | Descripción |
| :--- | :--- |
| `base/` | Contenido base del juego. |
| `updates/<versión>/` | Una carpeta por versión de actualización. El identificador de versión se expresa como número decimal. |
| `dlc/<title_id>/<índice>/` | Contenido adicional identificado por su Title ID (16 caracteres hexadecimales) y un índice de paquete. |
| `exefs/` y `romfs/` | Árboles de archivos correspondientes a cada paquete (según el contenido, puede existir uno o ambos). |

---

## 📄 Archivos `.listing`

Los archivos `.listing` generados por determinadas herramientas de volcado son listados auxiliares y no forman parte del contenido del juego. La implementación de referencia los excluye de forma predeterminada al crear un contenedor NUP.

---

## 🛡️ Doble verificación con RapidCRC

Además de la verificación interna del contenedor mediante `verify`, en el flujo de trabajo también se utiliza RapidCRC Unicode para realizar una segunda comprobación independiente de los archivos extraídos.

RapidCRC permite generar y verificar sumas de comprobación (*checksums*), como CRC, MD5 y SHA, a partir de archivos de referencia compatibles.

De este modo, una vez extraído el contenido, se pueden contrastar sus archivos con el correspondiente listado de checksums del volcado original para comprobar que coinciden.

> [!TIP]
> Esta comprobación externa complementa la validación SHA-256 que realiza NUP sobre los datos incluidos en el contenedor; no la sustituye y requiere disponer de un listado de checksums fiable.

---

## 🔗 Implementación de referencia y documentación

PyNup se desarrolla como una herramienta complementaria para facilitar el trabajo con el formato NUP. La especificación y la implementación de referencia oficial se mantienen en el proyecto original:

- 🛠️ **Proyecto NUP:** [a55uka/nup](https://github.com/a55uka/nup)
- 📄 **Especificación del formato:** [SPEC.md](https://github.com/a55uka/nup/blob/main/SPEC.md)
- 📖 **README oficial:** [Consultar en el repositorio de referencia](https://github.com/a55uka/nup)

---

## 🤝 Contribuciones y errores

Las sugerencias, los informes de errores y las propuestas de mejora son siempre bienvenidos. Para reportar cualquier incidencia, puedes abrir un ticket en la sección de **Issues** del repositorio.

Al informar de un problema, por favor incluye la siguiente información siempre que sea posible:

- 💻 La versión de PyNup que estás utilizando.
- ⚙️ Tu sistema operativo.
- 📋 Los pasos detallados para reproducir el error.
- 📝 El mensaje de error o un registro (*log*) de la operación (evitando compartir claves o contenido protegido).

---

## ⚖️ Licencia

PyNup se distribuye bajo los términos de la **licencia MIT**. Consulta el archivo [LICENSE](LICENSE) para más detalles.