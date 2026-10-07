# WordLens

Herramienta de escritorio para Windows que **lee los subtítulos en inglés que aparecen en pantalla**, detecta vocabulario y expresiones útiles de nivel **B2-C1** (phrasal verbs, idioms, collocations, palabras avanzadas) y los **añade automáticamente a un único documento Word** con su traducción al español:

> **figure out** — averiguar / descubrir
> **take for granted** — dar por sentado
> **overwhelmed** — abrumado / sobrepasado

Todo se ejecuta **en local**: captura, OCR, modelo de IA (Ollama) y documento. Ni las capturas ni las frases salen de tu equipo.

---

## Índice

1. [Cómo funciona](#1-cómo-funciona)
2. [Instalación](#2-instalación)
3. [Uso](#3-uso)
4. [Configuración](#4-configuración)
5. [El documento Word](#5-el-documento-word)
6. [Probar sin una serie](#6-probar-sin-una-serie)
7. [Solución de problemas](#7-solución-de-problemas)
8. [Decisiones técnicas y rendimiento](#8-decisiones-técnicas-y-rendimiento)
9. [Arquitectura](#9-arquitectura)
10. [Limitaciones conocidas](#10-limitaciones-conocidas)

---

## 1. Cómo funciona

```text
Ctrl+Shift+V ─▶ cada 0,5 s: captura el tercio superior (33 %) y el inferior (33 %) de la pantalla
                 │   (sólo en memoria, nunca se guardan en disco)
                 ▼
             Preprocesado: aísla texto blanco/amarillo (los colores de subtítulo)
                 ▼
             OCR nativo de Windows  (~15-35 ms por franja)
                 ▼
             Filtro: ¿parece un subtítulo? (centrado, ≥2 palabras, en inglés, no un reloj o menú)
                 ▼
             ¿Es nuevo? (visto en 2 capturas seguidas + no procesado en los últimos 3 min)
                 ▼  sólo si hay texto nuevo
             LLM local (Ollama) con la frase completa como contexto → JSON
                 ▼
             Validación determinista (descarta palabras básicas, cognados, frases enteras,
             expresiones inventadas que no están en el subtítulo, JSON roto...)
                 ▼
             ¿Ya está en el documento? (comparación normalizada)
                 ▼  sólo las nuevas
             Se añaden al final del .docx  (escritura atómica + copias de seguridad)
Ctrl+Shift+X ─▶ se detiene
```

Estados (visibles en el icono junto al reloj y en su tooltip, p. ej. "WordLens — Capturando (12 nuevas)"):

| Icono | Estado | Significado |
|---|---|---|
| ⚪ gris | **Inactivo** | Esperando a que pulses el atajo de inicio |
| 🟢 verde | **Capturando** | Leyendo subtítulos |
| 🟠 naranja | **Procesando** | El modelo analiza frases nuevas (la captura sigue en paralelo) |
| 🔴 rojo | **Detenido** | Sesión terminada; puedes iniciar otra |

---

## 2. Instalación

### Requisitos

| Componente | Para qué | ¿Instalación manual? |
|---|---|---|
| Windows 10/11 | API de OCR y atajos nativos | — |
| **Python 3.10+** | Ejecutar la aplicación | **Sí** |
| **Ollama** | Ejecutar el modelo de IA en local | **Sí** |
| Modelo `maternion/ling-3.0-tiny:8b` (5,3 GB) | Analizar vocabulario | Automática (install.ps1) |
| Paquete OCR de inglés de Windows | Leer texto en inglés | Normalmente ya viene; si no, ver abajo |
| Paquetes de Python | Ver `requirements.txt` | Automática (install.ps1) |

Hardware: 16 GB de RAM recomendados (el modelo ocupa ~5,5 GB). Una GPU NVIDIA acelera el modelo, pero no es imprescindible.

### Paso 1 — Python

1. Descarga Python desde <https://www.python.org/downloads/windows/> (o en una terminal: `winget install Python.Python.3.13`).
2. En el instalador, **marca "Add python.exe to PATH"**.
3. Comprueba en una terminal nueva:

```bash
python --version
```

### Paso 2 — Ollama

1. Descarga e instala desde <https://ollama.com/download/windows> (o `winget install Ollama.Ollama`).
2. Ollama se queda ejecutándose en segundo plano (icono de la llama junto al reloj).
3. Comprueba:

```bash
ollama --version
```

### Paso 3 — WordLens

Abre PowerShell en la carpeta del proyecto y ejecuta:

```bash
powershell -ExecutionPolicy Bypass -File install.ps1
```

El instalador:
- crea un entorno virtual en `.venv` e instala los paquetes;
- comprueba que el OCR de inglés de Windows está disponible;
- descarga el modelo si falta (`ollama pull maternion/ling-3.0-tiny:8b`, ~5,3 GB, sólo la primera vez);
- ejecuta un diagnóstico completo (OCR, captura, Word, atajos, modelo).

Al final debe aparecer **"Todo correcto."**

<details>
<summary>Instalación manual (sin el script)</summary>

```bash
python -m venv .venv
```
```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```
```bash
ollama pull maternion/ling-3.0-tiny:8b
```
```bash
.venv\Scripts\python.exe tools\diagnose.py
```
</details>

### Si falta el OCR de inglés

El diagnóstico lo indica con "Falta el paquete OCR". Soluciones:
- **Configuración → Hora e idioma → Idioma y región → Agregar un idioma → English (United States)** (no hace falta ponerlo como idioma de Windows), o
- en PowerShell **como administrador**:

```bash
Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
```

---

## 3. Uso

1. Asegúrate de que Ollama está abierto (icono de la llama).
2. Ejecuta **`start_wordlens.bat`** (doble clic). Aparece un icono ⚪ junto al reloj (quizá dentro de la flecha `^` de iconos ocultos) y una notificación "Listo".
3. Pon tu serie o película **con subtítulos en inglés** (mejor a pantalla completa).
4. Pulsa **Ctrl + Shift + V** → el icono se pone 🟢 y empieza a capturar. La primera vez tras arrancar Ollama, el modelo tarda ~1 min en cargarse; los subtítulos de ese minuto esperan en cola.
5. Mira la serie con normalidad. Las expresiones nuevas se van añadiendo al documento.
6. Pulsa **Ctrl + Shift + X** para detener → 🔴 y una notificación con el número de expresiones nuevas.
7. Para cerrar la aplicación: clic derecho en el icono → **Salir**.

Menú del icono (clic derecho): iniciar/detener, abrir el documento, elegir otro documento, abrir la carpeta, editar la configuración y ver el registro.

> **Consejo:** puedes tener el documento abierto en Word para ir viéndolo, pero Word bloquea el archivo y no deja guardarlo. Las expresiones quedan en espera y se escriben en cuanto cierras Word. Si sales con Word abierto, se guardan en `Vocabulario.pendientes.txt` junto al documento para que no se pierda nada.

Para ver lo que ocurre en tiempo real, ejecuta con consola:

```bash
.venv\Scripts\python.exe main.py
```

Para que arranque con Windows: pulsa `Win + R`, escribe `shell:startup` y crea ahí un acceso directo a `start_wordlens.bat`.

---

## 4. Configuración

Al primer arranque se crea **`config.json`** en la carpeta del proyecto (menú del icono → *Editar configuración*). Reinicia WordLens tras modificarlo. Opciones principales:

| Clave | Por defecto | Descripción |
|---|---|---|
| `hotkey_start` / `hotkey_stop` | `ctrl+shift+v` / `ctrl+shift+x` | Atajos globales. Formato `ctrl+alt+shift+win` + tecla (`a`-`z`, `0`-`9`, `f1`-`f24`, `space`...) |
| `document_path` | `Vocabulario.docx` | Documento Word de destino. Las rutas relativas se resuelven desde la carpeta del proyecto (`WordLens\Vocabulario.docx`); también admite rutas absolutas |
| `ollama_model` | `maternion/ling-3.0-tiny:8b` | Modelo de Ollama |
| `target_level` | `B2-C1` | Nivel objetivo (se usa en el prompt) |
| `target_language` | `Spanish` | Idioma de las traducciones |
| `capture_interval_seconds` | `0.5` | Frecuencia de captura |
| `top_band_fraction` / `bottom_band_fraction` | `0.33` / `0.33` | Altura de las franjas analizadas (fracción de la pantalla; máximo `0.5`). El tercio central no se captura |
| `scan_top` / `scan_bottom` | `true` / `true` | Analizar la franja superior / inferior |
| `monitor` | `1` | Monitor a capturar (1 = principal) |
| `ocr_preprocess` | `subtitle` | `subtitle` aísla texto blanco/amarillo; usa `gray` si tus subtítulos son de otro color |
| `stability_frames` | `2` | Capturas seguidas en las que debe verse un subtítulo antes de procesarlo |
| `repeat_window_seconds` | `180` | Tiempo durante el que una frase ya procesada no se vuelve a enviar al modelo |
| `batch_max_lines` | `4` | Frases por llamada al modelo |
| `max_single_word_zipf` | `4.5` | Umbral de frecuencia: las palabras sueltas más frecuentes se consideran básicas. Bájalo (p. ej. `4.0`) para ser más estricto |
| `require_centered` | `true` | Ignorar texto que no esté centrado (logos, menús laterales) |
| `notifications` | `true` | Notificaciones de Windows |
| `app_name` | `WordLens` | Nombre mostrado en el icono y las notificaciones |

### Cambiar de modelo

```bash
ollama pull qwen3.5:4b-q4_K_M
```

y en `config.json`: `"ollama_model": "qwen3.5:4b-q4_K_M"`. Ver la comparativa en la [sección 8](#8-decisiones-técnicas-y-rendimiento).

---

## 5. El documento Word

- Es **un único documento permanente**: cada entrada nueva se añade **al final**.
- **Ubicación por defecto:** dentro de la propia carpeta del proyecto:

  ```text
  Proyectos/
  └── WordLens/
      ├── Vocabulario.docx          ← el documento
      ├── Vocabulario.pendientes.txt ← sólo si sales con Word bloqueándolo
      └── .wordlens_backups/        ← copias de seguridad
  ```

  En `config.json` se guarda como ruta relativa (`"Vocabulario.docx"`), así que todo sigue funcionando aunque muevas o renombres la carpeta del proyecto. Los `.docx`, copias y pendientes están excluidos de git.
- Formato de cada línea: **expresión en negrita** — traducción en texto normal.
- **Elegir el documento:** menú del icono → *Elegir documento Word…*. Puedes seleccionar uno existente (se conserva todo su contenido) o escribir un nombre nuevo (se creará). Si lo eliges dentro de la carpeta del proyecto se guarda como ruta relativa. También puedes indicarlo en `config.json` o con `start_wordlens.bat --document "D:\Inglés\vocab.docx"`.
- **Documento existente:** al iniciar cada sesión se leen todas sus entradas para no repetirlas. Se reconocen líneas `expresión — traducción` (también con `–` o `-`) y, si no hay separador, el texto en negrita. Puedes editarlo, reordenarlo o borrar líneas a mano cuando quieras: el documento se vuelve a leer antes de cada escritura.
- **Seguridad:**
  - nunca se sobrescribe un documento existente; si el archivo no es un .docx válido, no se toca;
  - al iniciar cada sesión se guarda una copia en `.wordlens_backups\` (junto al documento; se conservan las 10 últimas);
  - cada guardado se escribe primero en un archivo temporal y se sustituye de forma atómica: un corte de luz no deja el documento a medias.

### Duplicados

Antes de añadir nada se normaliza la expresión y se compara con lo que ya hay en el documento. Se consideran **la misma entrada**:

- `figure out` = `Figure out` = `FIGURE OUT` = `to figure out` = `figure it out`
- `take for granted` = `take it for granted` = `take something for granted`
- `get under one's skin` = `get under my skin`; `get at` = `getting at`; `a long shot` = `long shot`

Y **no** se fusionan expresiones distintas: `get at` ≠ `get out`, `make out` ≠ `make up`, `put up` ≠ `put up with`, `make it` ≠ `make`. A propósito no se usa similitud aproximada, porque fusionaría phrasal verbs con significados diferentes.

---

## 6. Probar sin una serie

**Analizar frases de ejemplo** (usa el modelo real; `--dry-run` no escribe en Word):

```bash
.venv\Scripts\python.exe tools\try_text.py --dry-run "I couldn't figure out what he was getting at."
```

Salida real obtenida:

```text
> I couldn't figure out what he was getting at.
   figure out — averiguar / descubrir   [nuevo]
   get at — insinuar / querer decir   [nuevo]
```

Sin `--dry-run` escribe en el documento configurado (o en el de `--document`). Con `--file frases.txt` analiza un archivo con una frase por línea.

**Simular subtítulos en pantalla** (prueba la cadena completa: captura → OCR → LLM → Word). Inicia WordLens, pulsa Ctrl+Shift+V y ejecuta:

```bash
.venv\Scripts\python.exe tools\subtitle_simulator.py
```

Abre una ventana a pantalla completa con un fondo en movimiento y va mostrando subtítulos arriba y abajo (Esc para salir). La ventana debe quedarse en primer plano: no cambies a otra aplicación mientras dura.

**Diagnóstico completo:**

```bash
.venv\Scripts\python.exe tools\diagnose.py
```

**Tests** (154 en total; los marcados `llm` usan Ollama y tardan ~30 s):

```bash
.venv\Scripts\python.exe -m pytest
```
```bash
.venv\Scripts\python.exe -m pytest -m "not llm"
```

Cubren OCR real sobre imágenes de subtítulos (fondos con ruido y degradado, texto amarillo, dos líneas, franja superior, relojes y texto lateral), captura real, detección de cambios, estabilidad e historial de subtítulos, filtro de idioma, normalización y duplicados, parseo de JSON inválido, validación de vocabulario, cognados, escritura de Word (formato, documentos existentes, archivos corruptos, bloqueo por Word, copias), máquina de estados (incluida concurrencia), atajos globales disparados con pulsaciones reales del sistema, y el modelo real con los ejemplos de esta especificación.

---

## 7. Solución de problemas

| Problema | Solución |
|---|---|
| **"Ollama no responde"** | Abre Ollama desde el menú Inicio. Comprueba con `ollama list`. |
| **"El modelo ... no está descargado"** | `ollama pull maternion/ling-3.0-tiny:8b` |
| **"No se pudo registrar el atajo"** | Otra aplicación usa esa combinación. Cámbiala en `config.json` (p. ej. `ctrl+alt+f9`) y reinicia. |
| **Ctrl+Shift+V deja de "pegar sin formato" en otras apps** | Es normal mientras WordLens está abierto (el atajo es global). Si lo usas a menudo, cambia `hotkey_start`. |
| **"WordLens ya se está ejecutando"** | Ya hay una instancia: busca el icono junto al reloj (también en la flecha `^`). |
| **No detecta subtítulos** | 1) Mira el registro (menú → *Ver registro*): si no aparecen líneas "Subtítulo nuevo", el problema es la captura/OCR. 2) Si tus subtítulos no son blancos ni amarillos, pon `"ocr_preprocess": "gray"`. 3) Si están en otra posición, aumenta `top_band_fraction`/`bottom_band_fraction`. 4) Comprueba que el vídeo está en el monitor configurado (`monitor`). |
| **La captura sale en negro (Netflix, Disney+, Prime en el navegador o en sus apps)** | Es la protección DRM: Windows no deja capturar ese vídeo. En Chrome/Edge suele resolverse desactivando **Configuración → Sistema → Usar aceleración por hardware** y reiniciando el navegador. Los reproductores locales (VLC, MPC-HC...) y YouTube funcionan sin cambios. |
| **Detecta texto que no es un subtítulo** | Lo normal es que los filtros lo descarten (posición, idioma, letras/dígitos) y el modelo no extraiga nada. Si molesta, desactiva la franja que no uses (`scan_top: false`). |
| **"No puedo guardar: cierra el documento en Word"** | Word bloquea el archivo mientras está abierto. Ciérralo; las expresiones en espera se escriben solas. |
| **Va lento / la cola se llena** | El modelo tarda ~1-2 s por frase sin vocabulario y ~4 s por expresión encontrada en una GPU de 4 GB. Cierra programas que usen la GPU (juegos, editores 3D) para que quepa más del modelo en VRAM. En escenas con mucho diálogo se pueden descartar algunas frases antiguas (queda registrado en el log). |
| **Entradas poco útiles** | Baja `max_single_word_zipf` (p. ej. `4.2`) o prueba otro modelo (sección 8). |
| **Error al instalar paquetes** | Actualiza pip: `.venv\Scripts\python.exe -m pip install --upgrade pip` y repite. |
| **"la ejecución de scripts está deshabilitada"** | Usa exactamente `powershell -ExecutionPolicy Bypass -File install.ps1`. |

Registro detallado: `logs\wordlens.log` (subtítulos detectados, respuestas del modelo, entradas descartadas y añadidas). Se guarda sólo en local y rota automáticamente (máximo ~8 MB).

---

## 8. Decisiones técnicas y rendimiento

Mediciones en el equipo de desarrollo: i7-11800H, 16 GB de RAM, RTX 3050 Ti Laptop (4 GB), Windows 11, Ollama 0.40, Python 3.14.

### Modelo de IA: `maternion/ling-3.0-tiny` (se mantiene)

- **Existe y funciona con Ollama**: `ollama pull maternion/ling-3.0-tiny:8b`, 5,3 GB (Q4_K_M), contexto de 128K.
- **Arquitectura**: Mixture-of-Experts de 7,9B parámetros totales y **sólo 1,3B activos por token**, con modo de razonamiento opcional (se desactiva: aquí sólo añade latencia).
- **Requisitos**: ~5,5 GB entre RAM y VRAM. En una GPU de 4 GB, el 42 % va en GPU y el resto en CPU.
- **Rendimiento medido**: ~6 tokens/s de generación. Gracias a la caché de prefijo de Ollama (el prompt fijo de ~560 tokens se reutiliza), una frase sin vocabulario tarda ~1-2 s y cada expresión encontrada ~4 s. Primera carga: ~40-80 s (se hace al iniciar la sesión).
- **Calidad para la tarea** (con el prompt few-shot y los filtros de WordLens): detecta bien phrasal verbs, idioms y palabras avanzadas (figure out, get at, take for granted, make out, call it a day, get under one's skin, put up with, overwhelmed, flimsy, long shot), lematiza correctamente y su español es natural en la mayoría de casos. **Puntos débiles**: tiende a proponer palabras básicas o cognados ("supermarket") y alguna frase entera, cosa que WordLens filtra de forma determinista. A veces da traducciones imprecisas o con alguna palabra no española (p. ej. "flimsy — fraco / débil", donde "fraco" es portugués). Esto último no se puede filtrar con fiabilidad.

**Alternativas evaluadas** con el mismo prompt y las mismas frases:

| Modelo | Tamaño | Velocidad aquí | Calidad observada |
|---|---|---|---|
| **ling-3.0-tiny 8B (MoE, 1,3B activos)** | 5,3 GB | **~6,2 tok/s** | Buena detección; algún falso positivo (filtrado por WordLens) |
| qwen3.5:4b (denso) | 3,3 GB | ~4,8 tok/s | Algo más preciso (sin falsos positivos en frases básicas); español similar; **más lento** |
| mistral 7B | 4,4 GB | 13-29 s por frase (vs 6-19 s de ling) | Traducciones peores ("estar bajo mi piel", "corriendo de tiempo") |
| gemma4:e2b / e4b | 4,3-6,6 GB | no probado | Por tamaño tampoco caben en 4 GB de VRAM |

**Conclusión**: ninguna alternativa local es *claramente* mejor en este hardware. qwen3.5:4b es una opción razonable si prefieres algo más de precisión a cambio de velocidad (ver arriba cómo cambiarlo). Con una GPU de 8 GB o más, ling-3.0-tiny cabría entero y sería varias veces más rápido.

### OCR: Windows.Media.Ocr

| Opción | Precisión en subtítulos | Velocidad | Instalación | Veredicto |
|---|---|---|---|---|
| **Windows OCR (WinRT)** | Muy buena con texto grande y limpio | **15-35 ms/franja** con preprocesado (medido) | Ya incluido en Windows | **Elegido** |
| Tesseract | Buena con texto binarizado, peor sobre vídeo | más lento (estimado, no medido) | Instalador externo + PATH | Más fricción |
| EasyOCR / PaddleOCR | Muy buena | Lento en CPU; rápido con CUDA (estimado) | PyTorch/Paddle (varios GB), soporte irregular en Python recientes | Excesivo para texto de subtítulos |

Clave de rendimiento: el **preprocesado de subtítulos** (aislar píxeles blanco/amarillo brillantes y binarizar) elimina el fondo del vídeo. En las pruebas pasó de 170 ms con errores ("VI couldn't...") a **16 ms con texto exacto**, incluso sobre ruido y degradados.

### Captura: mss

GDI BitBlt vía ctypes, ~5-20 ms por franja, sin binarios. Sólo se capturan los tercios superior e inferior (≈66 % de la pantalla; el tercio central, donde casi nunca hay subtítulos, se omite). Se amplió desde 25 %/30 % para detectar subtítulos situados algo más hacia el centro. Las imágenes viven en memoria y se descartan tras el OCR. Además, si la franja preprocesada no cambia (vídeo en pausa, subtítulo fijo), se reutiliza el resultado anterior sin repetir el OCR.

**Frecuencia: 0,5 s.** Un subtítulo dura normalmente entre 1 y 7 segundos. Con 0,5 s y estabilidad de 2 capturas, cualquier subtítulo de ≥1 s se detecta, con un consumo de CPU bajo (~2 OCR/s por franja de unos 20-35 ms).

### Word: python-docx

Librería estándar y estable para .docx, sin necesidad de tener Word instalado. Se añade escritura atómica, copias rotativas y relectura antes de cada guardado.

### Atajos: RegisterHotKey (Win32)

API nativa vía ctypes: no requiere administrador, no instala hooks de teclado (no "ve" el resto de teclas que pulsas) y Windows avisa si la combinación ya está ocupada.

### Recursos en sesiones largas

- Captura y OCR nunca esperan al modelo (hilos separados).
- La cola hacia el LLM está acotada (40 frases). Si el modelo va por detrás, se descartan las más antiguas.
- El historial de subtítulos está acotado (ventana de 3 min y 500 entradas como máximo).
- Las imágenes no se acumulan ni se guardan en disco.
- Al detener, la petición en curso al modelo se cancela al instante.
- El modelo se mantiene cargado en Ollama 30 min tras el último uso (`ollama_keep_alive`).

---

## 9. Arquitectura

```text
WordLens/
├── main.py                      # Entrada: logging, instancia única, atajos, bandeja
├── start_wordlens.bat          # Lanzador sin consola
├── install.ps1                  # Instalador + diagnóstico
├── wordlens/
│   ├── app.py                   # Controlador: sesiones, hilos capture/analyzer, cola
│   ├── state.py                 # Máquina de estados INACTIVE/CAPTURING/PROCESSING/STOPPED
│   ├── capture/
│   │   ├── screen_capture.py    # Franjas, preprocesado de subtítulos, detector de cambios
│   │   └── subtitle_detector.py # Filtro de líneas, agrupación, estabilidad e historial
│   ├── ocr/ocr_engine.py        # Windows.Media.Ocr
│   ├── ai/
│   │   ├── ollama_client.py     # API REST de Ollama (cancelable)
│   │   ├── prompts.py           # Prompt de sistema, ejemplos few-shot, esquema JSON
│   │   └── vocabulary_analyzer.py
│   ├── vocabulary/
│   │   ├── normalization.py     # Lematización ligera, forma canónica, clave de duplicados
│   │   ├── extractor.py         # Parseo/validación del JSON y filtros anti falsos positivos
│   │   ├── deduplicator.py
│   │   └── models.py
│   ├── document/word_writer.py  # Lectura/escritura segura del .docx
│   ├── input/hotkeys.py         # RegisterHotKey
│   ├── ui/tray.py               # Icono de bandeja, menú, notificaciones
│   └── config/settings.py       # config.json
├── tools/
│   ├── try_text.py              # Analizar frases de ejemplo
│   ├── subtitle_simulator.py    # Subtítulos simulados en pantalla
│   └── diagnose.py              # Comprobación de todas las piezas
└── tests/                       # pytest
```

---

## 10. Limitaciones conocidas

- **Vídeo con DRM**: puede capturarse en negro (ver *Solución de problemas*).
- **Calidad de las traducciones**: depende del modelo. ling-3.0-tiny acierta en la mayoría de casos, pero alguna traducción puede ser imprecisa. Revisa el documento de vez en cuando; puedes corregir o borrar líneas a mano.
- **Velocidad del modelo en GPUs pequeñas**: en escenas con diálogo muy rápido, algunas frases pueden descartarse si la cola se llena.
- **Subtítulos de colores poco habituales**: usa `"ocr_preprocess": "gray"`.
- **Sólo Windows** (OCR y atajos nativos).

---

## Actualizar desde VocabLens

El proyecto se llamaba antes **VocabLens**. Al arrancar, WordLens actualiza automáticamente un `config.json` antiguo: cambia el nombre de la aplicación, mueve la ruta del documento a la carpeta del proyecto (si apuntaba a `Documentos\VocabLens\` o a la antigua carpeta del proyecto) y amplía las franjas de captura antiguas (25 %/30 %) a los nuevos tercios. Los valores que hayas personalizado se respetan. El lanzador ahora es `start_wordlens.bat`.
