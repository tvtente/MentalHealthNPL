# MentalHealthNPL

Repositorio para la construcción y documentación de un flujo de traducción controlada de textos sobre salud mental, orientado a tareas de Procesamiento de Lenguaje Natural.

## Contenido

- `mental_health_translation_agent.py`
  Flujo principal de traducción por lotes con validación, reducción automática de batch, control de timeouts y rescate por fragmentos.

- `translate_problematic_rows.py`
  Segunda pasada para filas problemáticas, reevaluación de traducciones sospechosas y retraducción dirigida de filas de alta confianza.

- `retranslate_selected_rows.py`
  Retraducción selectiva de un conjunto explícito de `row_id`, útil para outliers, auditorías manuales o correcciones focalizadas.

- `translation_methodology_memory.html`
  Memoria metodológica en formato HTML incrustable para notebooks o documentación técnica.

- `Combined Data.csv`
  Dataset de origen.

- `Combined Data es-ES.csv`
  Dataset traducido de salida visible y reutilizable como resultado final.

## Objetivo

El proyecto busca construir una versión traducida y metodológicamente trazable de un corpus textual relacionado con salud mental, preservando en la medida de lo posible el significado semántico, el tono emocional y el contexto comunicativo de los textos originales.

## Arquitectura del flujo

El procedimiento implementado:

- trabaja sobre un archivo fuente en CSV
- genera una columna de traducción dependiente del locale de salida
- procesa el corpus en lotes pequeños
- reduce el tamaño del lote automáticamente si la respuesta falla
- valida la estructura de salida antes de escribir en el dataset resultado
- registra filas problemáticas
- intenta rescate por fragmentos para textos demasiado largos

En la versión actual, los scripts quedaron alineados en estos principios:

- uso de `Ollama` local como backend único
- configuración por `source-locale` y `target-locale`
- traducción robusta por fragmentos
- fallback adaptativo cuando una respuesta falla o parece truncada
- persistencia de progreso para poder reanudar
- normalización de estados de reporte para no reprocesar filas ya resueltas

## Scripts y función de cada uno

### `mental_health_translation_agent.py`

Es el punto de entrada principal para traducir el dataset completo.

Responsabilidades:

- recorrer el dataset por lotes
- validar la estructura devuelta por el modelo
- reducir automáticamente el tamaño del lote si la salida falla
- registrar filas problemáticas en `.translation_problematic_rows.csv`
- intentar rescate inline por fragmentos cuando una fila individual no puede resolverse por lote
- reanudar desde el propio CSV de salida y desde el checkpoint

### `translate_problematic_rows.py`

Es el script de segunda pasada. Centraliza varias tareas de control de calidad.

Modos principales:

- `problematic`: rescata filas registradas como problemáticas
- `reevaluate`: vuelve a evaluar traducciones sospechosas ya escritas en el CSV final
- `high-confidence-report`: construye un subconjunto de sospechas con alta probabilidad de desalineación
- `high-confidence-retranslate`: retraduce ese subconjunto de alta confianza

### `retranslate_selected_rows.py`

Es el script más focalizado. Retraduce únicamente los `row_id` indicados por lista o archivo.

Casos de uso típicos:

- reintentar outliers detectados por análisis externo
- corregir una lista cerrada de filas
- rehacer una muestra sin tocar el resto del dataset

## Parámetros comunes

Los tres scripts comparten una base de parámetros operativos para mantener un comportamiento homogéneo:

- `--provider ollama`
- `--model gemma3:4b`
- `--source-locale en-US`
- `--target-locale es-ES`
- `--request-timeout 120`
- `--chunk-max-chars 500`
- `--min-chunk-chars 80`
- `--sleep-seconds 0.5`

Interpretación de los parámetros más importantes:

- `--chunk-max-chars`: tamaño máximo del fragmento inicial enviado al modelo cuando se traduce una fila larga por partes.
- `--min-chunk-chars`: tamaño mínimo permitido cuando un fragmento necesita subdividirse de nuevo por error o salida sospechosa.
- `--request-timeout`: límite máximo de espera por petición. No ralentiza el caso normal; sólo corta llamadas bloqueadas.
- `--save-every`: frecuencia de persistencia del progreso en scripts basados en reportes.

## Segmentación del texto fuente

La segmentación no se hace por corte ciego de caracteres. El flujo actual intenta preservar unidades lingüísticas razonables del inglés antes de dividir:

- párrafos
- oraciones
- cláusulas separadas por `;` o `:`
- segmentos después de comas cuando siguen conectores como `and`, `but`, `because`, `however`, `if`, `when`

Además, se protegen patrones frecuentes del inglés para evitar cortes incorrectos:

- abreviaturas como `Dr.`, `p.m.`, `U.S.`, `e.g.`, `i.e.`
- iniciales abreviadas
- números decimales
- cierres con comillas o paréntesis

Sólo cuando esos cortes más naturales no bastan, el sistema cae al corte por palabras.

## Fallback adaptativo por fragmentos

Cuando una fila larga no puede resolverse de forma fiable, la traducción por fragmentos sigue esta lógica:

1. se construyen fragmentos iniciales con `--chunk-max-chars`
2. cada fragmento se envía a Ollama
3. si la respuesta falla, queda vacía o parece anómala, el fragmento se subdivide
4. el tamaño baja en cascada hasta `--min-chunk-chars`

Ejemplo conceptual:

- `500 -> 250 -> 125 -> 80`

Esto permite rescatar filas largas sin descartar el proceso completo.

## Estados de reporte

Los scripts auxiliares usan un campo `action` para persistir el estado de cada fila y evitar recomenzar desde cero.

Estados actuales:

- `pending`: fila aún pendiente de procesamiento
- `success`: fila traducida y aceptada como resuelta
- `rejected_blank`: traducción propuesta pero rechazada; el campo final queda en blanco
- `missing_row`: la fila no pudo localizarse en el CSV de salida
- `empty_retry`: la retraducción devolvió vacío y quedó pendiente de decisión posterior
- `failed:...`: error durante el intento de traducción

Compatibilidad hacia atrás:

- `retranslated` se normaliza internamente como `success`
- `flagged` se normaliza internamente como `pending`

Esto permite reutilizar reportes antiguos sin romper la reanudación.

## Reanudación y persistencia

El proyecto está diseñado para soportar ejecuciones largas e interrumpibles.

### Dataset principal

- `Combined Data.csv` actúa como fuente canónica
- `Combined Data es-ES.csv` actúa como dataset traducido visible y acumulativo

### Reanudación del flujo principal

`mental_health_translation_agent.py` detecta filas pendientes directamente en la columna traducida del CSV final y además conserva un checkpoint técnico.

### Reanudación de scripts auxiliares

`translate_problematic_rows.py` y `retranslate_selected_rows.py` no dependen sólo de “la siguiente fila”, sino del estado persistido en sus archivos de control. Si una fila ya está en `success`, no vuelve a entrar como pendiente en la siguiente ejecución.

## Trazabilidad en consola

Para facilitar auditoría y depuración, los scripts emiten mensajes de progreso cuando:

- comienza una petición a Ollama
- termina una petición a Ollama y se conoce su duración
- se escribe un CSV grande
- se guarda checkpoint
- se activa el fallback por subdivisión de fragmentos

Esto permite distinguir mejor si una espera larga se debe al modelo, al guardado del CSV o a la lógica de rescate.

## Ejecución típica del flujo principal

```bash
cd /home/tvt/MEGA/GIT/ART/NPL
source venv/bin/activate
python mental_health_translation_agent.py \
  --input "Combined Data.csv" \
  --provider ollama \
  --model gemma3:4b \
  --source-locale en-US \
  --target-locale es-ES \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --skip-unrecoverable
```

## Ejecución de rescate de filas problemáticas

```bash
cd /home/tvt/MEGA/GIT/ART/NPL
source venv/bin/activate
python translate_problematic_rows.py \
  --mode problematic \
  --output "Combined Data es-ES.csv" \
  --provider ollama \
  --model gemma3:4b \
  --source-locale en-US \
  --target-locale es-ES \
  --chunk-max-chars 500 \
  --min-chunk-chars 80 \
  --request-timeout 120
```

## Ejecución de retraducción selectiva

```bash
cd /home/tvt/MEGA/GIT/ART/NPL
source venv/bin/activate
python retranslate_selected_rows.py \
  --input "Combined Data.csv" \
  --output "Combined Data es-ES.csv" \
  --provider ollama \
  --model gemma3:4b \
  --source-locale en-US \
  --target-locale es-ES \
  --row-ids-file "outlier_row_ids.txt" \
  --report-file "translation_selected_rows_retranslation.csv" \
  --chunk-max-chars 500 \
  --min-chunk-chars 80 \
  --request-timeout 120 \
  --row-batch-size 20 \
  --save-every 20
```

## Requisitos

- Python 3
- entorno virtual `venv`
- Ollama instalado y en ejecución
- modelo local descargado, por ejemplo `gemma3:4b`

## Licencia del código

El código de este repositorio se distribuye bajo licencia `Apache-2.0`. Consulta el archivo [LICENSE](/home/tvt/MEGA/GIT/ART/NPL/LICENSE:1).

## Nota sobre los datos

La licencia del código no implica automáticamente la licencia del dataset. El uso, redistribución o publicación de los datos debe verificarse de acuerdo con la procedencia y las condiciones del corpus original.
