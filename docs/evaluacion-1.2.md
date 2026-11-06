# WordLens 1.2 — Evaluación de extracción y traducción

Todas las cifras de este documento se han **medido** en el equipo de desarrollo
(i7-11800H, 16 GB RAM, RTX 3050 Ti Laptop 4 GB, Windows 11, Ollama 0.40.3) el
14-10-2026. Los resultados en bruto (cada salida de cada motor) están en
`docs/benchmarks/*.json` y se pueden recalcular con las herramientas citadas.

## 1. Extracción exhaustiva

**Conjunto:** `tests/benchmark/exhaustive_cases.jsonl`, 50 subtítulos con 124
unidades esperadas (anotadas a mano), más listas de propuestas *aceptables*
(no cuentan) y *prohibidas* (palabras gramaticales, «have to», palabras sueltas
de un modismo ya detectado, nombres, ruido de OCR). Incluye los ejemplos del
enunciado, phrasal verbs, modismos, slang, colocaciones, nombres a principio
de frase, una línea en mayúsculas, un error de OCR («Tbe») y líneas sin
vocabulario.

**Herramienta:** `tools/benchmark_exhaustive.py` (modelo real, una línea por
llamada, mismo modelo `maternion/ling-3.0-tiny:8b` en ambas versiones;
`--impl` ejecuta una copia de la versión 1.1 con exactamente la misma
puntuación).

| Versión | Encontradas | Omitidas | Recall | Falsos positivos | Precisión | Prohibidos | Duplicados | Subtítulos completos | s / subtítulo |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 (filtro B2+/C1/C2) | 33 / 124 | 91 | 0,266 | 2 | 0,943 | 0 | 0 | 7 / 50 | 1,15 |
| **1.2** | **119 / 124** | **5** | **0,960** | 6 | **0,952** | 3 | 0 | **46 / 50** | 2,08 |

Ejemplos obligatorios con el modelo real (1.2): «He brushed off my concerns and
went ahead with the deal.» → *brush off, concern, go ahead with, deal*;
«I can't put up with this anymore.» → *put up with* (+ *anymore*);
«You really hit the nail on the head.» → *hit the nail on the head* (sin
*nail* ni *head* sueltos); «I have to go to the television store.» →
*go, television, store* (sin *have to*, *I*, *the*, *to*). En 1.1 el primer
ejemplo daba sólo *brush off* y *go ahead with* (faltaban *concern* y *deal*) y
el de *television store* no daba nada.

Por qué la 1.1 se quedaba corta: el prompt pedía sólo lo «digno de aprender a
nivel B2-C1/C2» y el validador descartaba por frecuencia (`zipf ≥ 4.5`), por
cognado y por «combinación trivial». Con el criterio nuevo (toda unidad
léxica con significado propio) eso eliminaba 3 de cada 4 candidatos válidos.

Qué hace la 1.2 (`wordlens/ai/vocabulary_analyzer.py`):

1. **Modelo** (ling, sin cambiar): un prompt nuevo que pide cada expresión con
   significado propio y cada palabra de contenido, en forma de diccionario y
   con su tipo (`word`, `phrasal verb`, `idiom`, `collocation`, `expression`).
   No traduce: así responde más rápido y la traducción la hace un motor mejor.
2. **Reglas** (`wordlens/vocabulary/lexicon.py`): todas las palabras de
   contenido de la línea. Es la red de seguridad: lo que el modelo se salta se
   propone igual. Una palabra que forma parte de un phrasal verb o modismo
   detectado no se repite («brushed off» → «brush off», no también «brush»).
3. **Validación determinista** (`wordlens/vocabulary/extractor.py`): todo
   candidato debe estar en el subtítulo (admite flexiones y la corrección de
   un error evidente de OCR); se rechazan palabras gramaticales, construcciones
   gramaticales («have to», «going to»), nombres propios, palabras inexistentes
   y frases enteras. **No hay ningún filtro por frecuencia, nivel o interés.**

Se valoró una segunda pasada del modelo sólo para palabras sueltas: no hace
falta, porque las reglas ya recuperan todas las palabras de contenido; la
pasada extra sólo añadiría latencia.

Fallos que quedan (salida completa en `docs/benchmarks/extraction_v1.2.json`):

- Omitidas: *get fired* (sale *fired*: desde la última corrección se respeta la
  forma que da el modelo, para no convertir *gutted* «destrozado» en *gut*;
  antes de esa corrección el resultado era 120/124 y 0,960 de precisión),
  *at the last minute* (salen *last* y *minute* por separado),
  *be done with* (el modelo no la ve y «done» es una forma de *do*), y en la
  línea en mayúsculas *nail* e *interview* quedan dentro de «nail the
  interview», que el modelo etiqueta como modismo.
- Sobrantes: combinaciones libres que el modelo llama colocación (*grab an
  umbrella*, *moving abroad*; sus palabras también se proponen) y palabras
  sueltas cuando el modelo etiqueta mal el tipo (*take* junto a *rain check*;
  *cut* y *corner* junto a *cutting corners*). Son fáciles de descartar con ✕
  y no se repiten.
- Latencia: ~2 s por subtítulo durante la captura (1,15 s en 1.1, que pedía
  menos). Los subtítulos no se descartan nunca: se guardan y se analizan en
  segundo plano y, si queda alguno, al detener.

## 2. Traducción: alternativas y elección

### Condición de coste cero

| Alternativa | ¿Cumple «0 € sin riesgo de cargos»? | Motivo |
| --- | --- | --- |
| Google Cloud Translation | **No** | Exige activar la facturación en el proyecto; los primeros 500 000 caracteres/mes son gratis (como crédito de 10 $), el resto se cobra a la cuenta vinculada. ([precios](https://cloud.google.com/products/translate/pricing), [configuración](https://docs.cloud.google.com/translate/docs/setup)) |
| DeepL API Free | **No** | 500 000 car./mes sin cargos, pero el alta pide tarjeta bancaria (requisito explícito del usuario: sin tarjeta). |
| Azure AI Translator F0 | **No** | Requiere una suscripción de Azure (alta con tarjeta). |
| libretranslate.com (instancia pública) | **No** | Requiere clave de API de pago. Autoalojado = Argos (abajo). |
| MyMemory (API pública) | Sí (técnicamente) | Sin clave ni tarjeta, 5000 car./día anónimos; al superarlo deja de responder. Pero envía los subtítulos a un servidor externo y no admite contexto. |
| Argos Translate (local) | Sí | Local, sin red. Necesita ~1,1 GB (PyTorch, stanza) + 100 MB de modelo. |
| Modelos locales de Ollama | Sí | Locales, sin red, sin cuentas: imposible generar cargos. |

### Prueba comparativa

**Conjunto:** `tests/benchmark/translation_cases.jsonl`, 80 subtítulos con 117
unidades a traducir y sus traducciones de referencia aceptables (revisadas a
mano): palabras con varios significados (*deal*, *run*, *remote*, *mad*,
*pick up*, *look up*, *dump*), phrasal verbs (*give up* / *give in*,
*brush off*, *go ahead with*), modismos (*hit the nail on the head*,
*break a leg*…), slang e insultos (*sick*, *dude*, *lit*, *jerk*), colocaciones
(*heavy rain*, *heavy smoker*, *make a decision*), negaciones (*couldn't care
less*, *not rocket science*), ironía (*just what I needed*, *big deal*) y
coherencia (la misma palabra en frases distintas).

**Método:** cada motor recibe lo mismo. Los modelos de Ollama, el subtítulo
completo y la lista numerada de unidades (prompt de
`wordlens/translation/translator.py`); Argos y MyMemory sólo pueden traducir
la unidad aislada. La puntuación automática (`tools/benchmark_translation.py`)
da por buena una salida si alguna de sus opciones coincide con una referencia
(sin tildes ni artículo). Es estricta con las paráfrasis correctas que no
están en la lista, por eso los dos mejores motores se revisaron además a mano.

| Motor | Correctas (automático) | Modismos | Phrasal verbs | Polisemia | Slang | Contexto | Negación | s / subtítulo |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **gemma3:4b** (Ollama) | **98 / 117 (83,8 %)** | 15/23 | 33/34 | 33/39 | 11/12 | 12/15 | 8/13 | 3,7 |
| ling-3.0-tiny:8b (Ollama, el de extracción) | 88 / 117 (75,2 %) | 13/23 | 33/34 | 28/39 | 7/12 | 11/15 | 8/13 | 1,2 |
| mistral:7b (Ollama) | 78 / 117 (66,7 %) | 10/23 | 26/34 | 32/39 | 7/12 | 9/15 | 6/13 | 7,3 |
| qwen2.5:3b (Ollama) | 65 / 117 (55,6 %) | 6/23 | 24/34 | 27/39 | 3/12 | 8/15 | 7/13 | 0,6 |
| MyMemory (externo, sin contexto) | 52 / 117 (44,4 %) | 8/23 | 15/34 | 16/39 | 5/12 | 6/15 | 5/13 | ~1 (red) |
| Argos Translate (local, sin contexto) | 33 / 117 (28,2 %) | 1/23 | 12/34 | 18/39 | 4/12 | 4/15 | 2/13 | 0,4 |

**Revisión manual** de los fallos automáticos de los dos mejores (criterios:
fidelidad, corrección, naturalidad, contexto, phrasal verbs/modismos, sin
omisiones ni invenciones):

- **gemma3:4b:** de los 19 «fallos», 9 son traducciones correctas que no estaban
  en la lista (*rain cats and dogs* → «está lloviendo a cántaros», *couldn't
  care less* → «no me importa en absoluto», *buy* → «creerlo», *give a hand* →
  «ayudarme»…), 2 son aceptables pero poco naturales (*not rocket science* →
  «no es cosa complicada») y **8 son errores**: *deal* (cartas) → «barajar»,
  *just what I needed* (ironía) → «exactamente lo que necesitaba», *go over
  one's head* → «pasar por alto», *nice try* → «intento fallido», *take out on*
  → «cagársela a», *dump* → «basuraleza» (palabra inventada), *have a crush on*
  → «tener una coqueta fijación en», *give a big hand* → «darle una palmada».
  **Resultado: 107 correctas, 2 aceptables, 8 incorrectas (≈ 91–93 %).**
- **ling:** 6 de sus 29 «fallos» son correctos y 3 aceptables; **20 errores**,
  varios graves de contexto: *sick* (slang) → «asqueroso», *remote* (mando) →
  «reloj», *close call* → «quiebre de espadas», *nice try* → «un intento de
  pasaporte», *no way* → «no hay manera». **≈ 80 %.**
- Ejemplos de Argos / MyMemory: *brush off* → «Cepillo apagado» /
  «Cepillado apagado», *hit the nail on the head* → «golpeó el clavo en la
  cabeza», *deal* (negocio) → «oferta», *dude* → «petimetre». Sin contexto no
  pueden distinguir sentidos.

**Coherencia:** con gemma, *deal* da «acuerdo» en los dos subtítulos de
negocios (t01, t70); *pick up*, *look up*, *remote* y *mad* cambian
correctamente de traducción según la frase. *give up* / *give in* →
«rendirse» / «ceder».

### Decisión

**gemma3:4b vía Ollama**, como motor independiente de la extracción
(`wordlens/translation/translator.py`, ajuste `translation_model`):

- Es la mejor calidad medida, con diferencia, entre las opciones gratuitas.
- Coste: **0 €, sin posibilidad de cargos**. Es un modelo local: no hay cuenta,
  clave, tarjeta, cuota ni conexión a internet. Un test
  (`tests/test_translation.py::test_translator_only_talks_to_local_ollama`)
  comprueba que el código de traducción no contiene direcciones externas ni
  librerías de red, y otro que ningún archivo de WordLens referencia APIs de
  traducción de pago.
- Ya estaba instalado en este equipo: no hubo que descargar nada nuevo
  (3,3 GB en disco). Al cargarse ocupa 3,5 GB de memoria (en esta GPU de 4 GB, 54 % en CPU y 46 % en GPU, según `ollama ps`); Ollama lo descarga de memoria a los 10 minutos sin uso.
- Latencia: ~3,7 s por subtítulo. Se traduce **al detener** la captura (no
  compite con la extracción en una GPU de 4 GB) y sólo lo que es nuevo.
- Se descartó TranslateGemma (variante de Gemma 3 especializada en traducción):
  habría que descargar 3,3 GB más y está pensada para traducir frases, no para
  dar la acepción de una unidad en contexto; con gemma3:4b ya instalado y un
  91–93 % revisado a mano, la descarga no está justificada sin evidencia. Queda
  como mejora posible.

Medidas añadidas sobre el motor:

- Cada petición lleva el subtítulo completo y todas sus unidades: el modelo
  decide la acepción con el contexto (*deal* → «acuerdo» / «repartir»).
- Se descartan opciones con palabras que no existen en español (wordfreq):
  «basuraleza» no llega nunca a la revisión.
- Si gemma no da una traducción válida, se intenta una vez con el modelo de
  extracción; si tampoco, la entrada queda «Translation pending» y se reintenta.
- Si `gemma3:4b` no está instalado, WordLens traduce con el modelo de
  extracción (peor calidad) y lo registra en el log.
- Caché persistente por (unidad, subtítulo, motor) en `vocabulary.db`. Las
  traducciones guardan el motor y la versión del prompt: si se cambia de motor
  o de prompt, las pendientes se regeneran solas.
- En la revisión, doble clic sobre una traducción para corregirla antes de
  aceptarla.

**Medición final** con el código entregado (mismo conjunto,
`docs/benchmarks/translation_gemma3-4b.json`): 98 / 117 correctas en la
puntuación automática, 1 sin traducción (la opción inventada «basuraleza» se
descarta; en la aplicación esa entrada pasa al modelo de reserva), 4,2 s por
subtítulo. La revisión manual de la primera ejecución sigue siendo válida: las
salidas son las mismas salvo esa.

## 3. Limitaciones de la evaluación

- Las referencias de traducción las he escrito yo y la revisión manual es mía:
  no hay un segundo revisor humano. Las cifras automáticas son reproducibles;
  las manuales, no exactamente.
- Los conjuntos (50 y 80 subtítulos) son representativos pero pequeños: ±3–4
  puntos de diferencia no son concluyentes.
- No se midió con subtítulos de series reales capturados de un vídeo (sí con
  subtítulos simulados en pantalla, OCR real, en las pruebas E2E).
