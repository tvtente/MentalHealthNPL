# Agente de traduccion por lotes para salud mental

Este proyecto traduce un CSV grande en bloques pequenos para reducir errores y permitir reanudar el trabajo si la ejecucion se corta.

Tambien puede usar archivos `.ods` como entrada y como salida, apoyandose en LibreOffice en modo consola.

## Que hace

- Lee `Combined Data.csv`
- Puede leer `Combined Data.ods`
- Usa la columna `statement` como texto fuente
- Usa la columna `status` como contexto clinico ligero
- Escribe la traduccion en una columna derivada del locale, por ejemplo `statement_es_es`
- Cuando la salida es `ODS`, acumula el progreso en un CSV interno de trabajo y solo sincroniza el `ODS` periodicamente
- Procesa por defecto en lotes de `8` filas
- Reduce automaticamente el lote si el modelo devuelve salida truncada o invalida
- Puede dejar filas en blanco y seguir si una traduccion es irrecuperable
- Guarda progreso en `.translation_checkpoint.json`
- Permite continuar desde donde se quedo

## Preparacion para Ollama

1. Activa tu entorno virtual:

```bash
source venv/bin/activate
```

2. Instala Ollama desde su pagina oficial:

`https://ollama.com/download`

3. Verifica que el binario exista:

```bash
ollama --version
```

4. Descarga un modelo. Para empezar, prueba uno pequeno o medio:

```bash
ollama pull gemma3:4b
```

5. Inicia Ollama si tu sistema no lo deja corriendo automaticamente:

```bash
ollama serve
```

6. Prueba el modelo:

```bash
ollama run gemma3:4b "Translate to Spanish: I feel nervous and tired."
```

## Primera prueba

Haz primero una corrida corta de validacion:

```bash
python mental_health_translation_agent.py --batch-size 8 --max-batches 1
```

Con Ollama:

```bash
python mental_health_translation_agent.py \
  --input "Combined Data.ods" \
  --provider ollama \
  --model gemma3:4b \
  --response-format csv \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --sync-output-every 25 \
  --skip-unrecoverable \
  --max-batches 1
```

Esto traduce solo el primer bloque y genera:

- `Combined Data es-ES.csv`
- `.translation_checkpoint.json`

## Ejecucion completa

Cuando la prueba salga bien:

```bash
python mental_health_translation_agent.py --batch-size 8
```

Con Ollama:

```bash
python mental_health_translation_agent.py \
  --input "Combined Data.ods" \
  --provider ollama \
  --model gemma3:4b \
  --response-format csv \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --sync-output-every 25 \
  --skip-unrecoverable
```

## Reanudar si se interrumpe

Solo vuelve a ejecutar el mismo comando. El script detecta que filas ya tienen contenido en la columna derivada del locale, por ejemplo `statement_es_es`, y sigue con las pendientes.

## Cambiar el punto de inicio

Si quieres arrancar mas adelante:

```bash
python mental_health_translation_agent.py --start-row 1000 --batch-size 8
```

## Por que 8 filas

`8` es un mejor equilibrio cuando ya quedan textos largos y complejos:

- reduce mejor por mitades cuando hay errores: `8 -> 4 -> 2 -> 1`
- disminuye el riesgo de respuestas truncadas
- hace mas estable la traduccion local
- evita mezclar demasiados textos largos en una sola respuesta

## Flujo recomendado

1. Corre `1` lote.
2. Revisa `20` o `30` traducciones manualmente.
3. Ajusta el prompt si el tono no te convence.
4. Corre `5` lotes.
5. Vuelve a revisar calidad.
6. Ejecuta el resto.

## Recomendaciones de calidad para salud mental

- Usa un espanol neutral y no estigmatizante.
- No conviertas frases ambiguas en diagnosticos.
- Conserva la intensidad emocional del original.
- Manten la primera persona si el texto la usa.
- No "mejores" el mensaje agregando consejos o interpretaciones.

## Siguiente mejora util

Si luego quieres mas control, el siguiente paso es agregar un glosario fijo por etiqueta, por ejemplo:

- `Anxiety` -> preferir `ansiedad`, `inquietud`, `preocupacion`
- `Depression` -> preferir `desanimo`, `vacío`, `agotamiento`
- `PTSD` -> preferir `recuerdos intrusivos`, `alerta constante`

Eso puede incorporarse dentro del prompt del script.

## Recomendacion especifica para Ollama

En modelos locales, `200` filas puede ser demasiado para equipos con poca RAM o VRAM. Empieza con:

- `5` filas si ves respuestas truncadas
- `10` filas para una primera validacion segura
- `25` filas si el modelo responde estable
- `50` filas solo si ya comprobaste estabilidad

Para traduccion local, es preferible un lote mas pequeno y estable antes que uno grande e inconstante.

Si un lote falla, el script ahora intenta automaticamente con un lote mas pequeno sobre las mismas filas.

## Saltar filas problematicas

Si algunas filas son demasiado largas o el modelo no las devuelve correctamente, puedes hacer que el proceso continue y deje esas traducciones en blanco:

```bash
python mental_health_translation_agent.py \
  --provider ollama \
  --model gemma3:4b \
  --response-format csv \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --skip-unrecoverable
```

Las filas omitidas se guardan en:

- `.translation_problematic_rows.csv`

Asi luego puedes revisarlas y completarlas manualmente.

## Uso con ODS

Si prefieres evitar revisiones intermedias en CSV, puedes trabajar directamente con hojas de calculo `ODS`:

```bash
python mental_health_translation_agent.py \
  --input "Combined Data.ods" \
  --output "Combined Data Spanish.ods" \
  --provider ollama \
  --model gemma3:4b \
  --response-format csv \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --skip-unrecoverable
```

Internamente el script convierte temporalmente el archivo con LibreOffice, pero para ti el flujo queda en `ODS`.

Cuando trabajas con un `ODS` grande:

- el archivo fuente se toma como referencia principal
- las traducciones nuevas se van guardando en un archivo interno de trabajo
- el `ODS` de salida no se relee en cada lote
- el `ODS` final se actualiza cada cierto numero de lotes o al terminar

Esto reduce trabajo innecesario sobre la hoja de calculo grande y evita degradar el archivo de salida en cada escritura.

## Timeout por peticion

Si Ollama se queda colgado en alguna tanda, puedes limitar el tiempo maximo de espera por solicitud:

```bash
python mental_health_translation_agent.py \
  --input "Combined Data.ods" \
  --output "Combined Data Spanish.ods" \
  --provider ollama \
  --model gemma3:4b \
  --response-format csv \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --sync-output-every 25 \
  --skip-unrecoverable
```

Si una peticion supera ese tiempo, el script la trata como fallo normal y vuelve a intentar con un lote mas pequeno si corresponde.

## Locale de salida

El flujo puede generar traducciones para variantes concretas de idioma usando locales como:

- `es-ES` para espanol de Espana
- `es-AR` para espanol de Argentina
- `en-GB` para ingles del Reino Unido

Si no indicas manualmente `--translated-col`, el script crea la columna de salida automaticamente a partir del locale. Por ejemplo:

- `es-ES` -> `statement_es_es`
- `es-AR` -> `statement_es_ar`
- `en-GB` -> `statement_en_gb`

Ejemplo:

```bash
python mental_health_translation_agent.py \
  --input "Combined Data.csv" \
  --provider ollama \
  --model gemma3:4b \
  --source-locale en-US \
  --target-locale es-AR \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --skip-unrecoverable
```

Si ya estabas trabajando con una columna anterior, por ejemplo `statement_es`, y quieres migrarla a la variante nueva `statement_es_es`, puedes renombrarla directamente dentro del archivo de salida:

```bash
python mental_health_translation_agent.py \
  --input "Combined Data.csv" \
  --provider ollama \
  --model gemma3:4b \
  --source-locale en-US \
  --target-locale es-ES \
  --rename-translated-col-from statement_es \
  --batch-size 8 \
  --min-batch-size 1 \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --skip-unrecoverable
```

## Cola de filas problematicas

Cuando una fila falla incluso con lote minimo, el sistema puede dejarla en blanco y seguir, pero ahora tambien la envia a un archivo de problemáticas para segunda pasada:

- `.translation_problematic_rows.csv`

Ese archivo guarda:

- `row_id`
- `label`
- `text_length`
- `reason`
- `source_text`
- `logged_at`

Asi puedes detectar textos largos, filas conflictivas o casos donde el modelo omitio identificadores, y tratarlos luego con otra estrategia.

Ademas, si el fallo ocurre con una sola fila, el script principal intenta de inmediato una recuperacion por fragmentos:

- registra la fila en `.translation_problematic_rows.csv`
- divide el texto largo en fragmentos
- traduce cada fragmento
- recompone la traduccion final
- si funciona, rellena la columna derivada del locale, por ejemplo `statement_es_es`, y elimina esa fila del archivo de problemáticas
- si no funciona, la fila queda registrada para una segunda pasada posterior

## Segunda pasada para textos largos

Para rescatar filas largas o truncadas, usa el segundo script:

```bash
cd /home/tvt/MEGA/GIT/ART/NPL
source venv/bin/activate
python translate_problematic_rows.py \
  --mode problematic \
  --provider ollama \
  --model gemma3:4b \
  --problematic-file ".translation_problematic_rows.csv" \
  --output "Combined Data es-ES.csv" \
  --chunk-max-chars 500 \
  --request-timeout 120
```

Ese proceso:

- toma las filas de `.translation_problematic_rows.csv`
- divide cada texto largo en fragmentos manejables
- traduce fragmento por fragmento
- recompone la traduccion final
- la escribe en la columna derivada del locale dentro del archivo de salida, por ejemplo `statement_es_es`
- elimina del archivo de problemáticas las filas que ya resolvio

Si quieres probar solo unas pocas filas:

```bash
python translate_problematic_rows.py \
  --mode problematic \
  --provider ollama \
  --model gemma3:4b \
  --max-rows 5
```

## Reevaluacion de traducciones sospechosas

Si algunas filas parecen mal alineadas con el original, el mismo script puede detectar traducciones sospechosas ya existentes y rehacerlas.

Primero conviene generar solo el reporte sin modificar el dataset:

```bash
python translate_problematic_rows.py \
  --mode reevaluate \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --dry-run
```

Ese modo genera:

- `.translation_suspicious_rows.csv`

El reporte incluye, entre otras variables:

- `char_ratio`
- `word_ratio`
- `english_token_ratio`
- `suspicion_score`
- `reasons`
- `current_translation`
- `new_translation`
- `action`

Las filas se marcan cuando presentan patrones extremos, por ejemplo:

- una traducción muchísimo más larga o más corta que el original
- expansión anómala del número de frases
- residuos importantes de inglés dentro de la supuesta traducción en español

La reevaluación actual usa criterios adaptativos para reducir falsos positivos:

- relaja los umbrales cuando el texto fuente es muy largo
- deja de confiar en `word_ratio` cuando el original viene mal espaciado o con palabras pegadas
- añade comprobaciones de anclas relativamente estables, como años, números, URLs y ciertos identificadores no traducibles

Si el reporte te convence, puedes lanzar la reevaluación real:

```bash
python translate_problematic_rows.py \
  --mode reevaluate \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --model gemma3:4b \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --save-every 10
```

En este modo:

- detecta las filas sospechosas
- intenta retraducirlas por fragmentos
- escribe la nueva traducción en la columna derivada del locale
- actualiza `.translation_suspicious_rows.csv` con `new_translation` y `action`

## Reanudacion de la reevaluacion

La reevaluación ya es reanudable por bloques, sin necesidad de guardar en cada fila.

- `--save-every 10` guarda el avance cada 10 filas procesadas
- en cada guardado verás un aviso como `Saving progress (...) ...`
- si interrumpes el proceso, al ejecutarlo de nuevo leerá `.translation_suspicious_rows.csv`
- las filas ya registradas como `retranslated`, `failed:...`, `missing_row` o `empty_retry` no se rehacen

Si quieres una reanudación más conservadora puedes bajar, por ejemplo, a:

```bash
python translate_problematic_rows.py \
  --mode reevaluate \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --save-every 5
```

## Filtro de alta confianza para desajustes graves

Si el reporte amplio de reevaluación sigue conteniendo demasiados falsos positivos, el mismo script puede construir un subconjunto más estricto con casos de alta probabilidad real de desalineación, truncamiento o colapso fuerte del contenido.

Primero se genera el archivo de alta confianza:

```bash
python translate_problematic_rows.py \
  --mode high-confidence-report \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --high-confidence-report-file ".translation_high_confidence_mismatches.csv"
```

Ese modo genera:

- `.translation_high_confidence_mismatches.csv`

Este archivo:

- se construye a partir de `.translation_suspicious_rows.csv`
- aplica reglas más duras para quedarse con casos realmente graves
- reinicia su propio `action` a `pending`
- conserva el estado anterior en `source_report_action` solo como referencia

Esto es importante porque el reporte de alta confianza no debe heredar como resueltas filas que solo fueron marcadas en el reporte amplio anterior.

Ademas, para evitar reconstruir accidentalmente la cola de pendientes, si `.translation_high_confidence_mismatches.csv` ya existe el modo `high-confidence-report` se detiene por defecto y no lo sobrescribe.

Solo si realmente quieres regenerarlo desde `.translation_suspicious_rows.csv` debes usar:

```bash
python translate_problematic_rows.py \
  --mode high-confidence-report \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --rebuild-report
```

En la práctica:

- `high-confidence-report` se usa para construir la cola inicial
- `high-confidence-retranslate` se usa para continuar procesando esa cola ya creada
- si vuelves a lanzar solo `high-confidence-retranslate`, el proceso continúa sobre pendientes
- si vuelves a lanzar `high-confidence-report` sin `--rebuild-report`, el script protege la cola existente y no la rehace

Si además quieres que las filas dudosas queden vacías en la columna traducida del CSV final mientras esperan una nueva traducción, puedes añadir:

```bash
--blank-flagged-translations
```

Con ese parámetro:

- la traducción previa se conserva en `current_translation` dentro del archivo de control
- la columna traducida del archivo `Combined Data es-ES.csv` se vacía para esas filas marcadas
- el dataset final deja explícitamente visibles los registros que todavía están bajo duda metodológica

## Retraduccion de filas de alta confianza

Generar el archivo de alta confianza no modifica todavía el dataset final. Para retraducir realmente esas filas se usa un segundo modo:

```bash
python translate_problematic_rows.py \
  --mode high-confidence-retranslate \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --model gemma3:4b \
  --high-confidence-report-file ".translation_high_confidence_mismatches.csv" \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --row-batch-size 20 \
  --save-every 20
```

En este modo:

- lee `.translation_high_confidence_mismatches.csv`
- toma solo las filas con `action = pending`
- retraduce esas filas
- escribe el resultado en `Combined Data es-ES.csv`
- actualiza también el propio archivo de alta confianza

## Lotes visibles en alta confianza

La retraducción de alta confianza puede organizarse en lotes visibles de filas para seguir mejor el avance:

- `--row-batch-size 20` muestra bloques como `Processing high-confidence row batch 1..20 ...`
- `--save-every 20` guarda avance cada 20 filas procesadas

Esto no significa que el modelo traduzca 20 filas dentro de una sola respuesta. Cada fila sigue retraduciéndose de forma controlada, pero la ejecución se organiza en grupos visibles para facilitar el seguimiento y la persistencia del progreso.

## Revision interactiva por consola

Si quieres decidir manualmente si una propuesta debe escribirse o no en el dataset final, puedes añadir:

```bash
--interactive-review
```

Con ese parámetro, después de cada propuesta el script muestra en consola:

- el `row_id`
- la etiqueta
- el texto original
- la traducción propuesta

Y luego pregunta:

```text
Accept this translation and write it to the output file? [y/N]:
```

Comportamiento:

- si respondes `y` o `yes`, la traducción se escribe
- si respondes `n`, `no` o simplemente Enter, la fila queda vacía en la columna traducida del archivo final
- en los modos de reevaluación y alta confianza, una fila rechazada queda marcada como `rejected_blank` para no reprocesarse automáticamente en la siguiente ejecución

Esto es útil cuando quieres observar el proceso y decidir caso por caso sobre registros especialmente delicados o ambiguos.

## Cierre practico del proceso

Si una revisión muestral manual sugiere que las traducciones finales son suficientemente coherentes, puede considerarse razonable cerrar la fase de traducción y pasar al preprocesamiento o al análisis NLP, manteniendo los archivos de control únicamente como trazabilidad metodológica.

En un cierre conservador de este proyecto conviene distinguir entre:

- dataset final operativo:
  `Combined Data es-ES.csv`
- scripts del pipeline:
  `mental_health_translation_agent.py`
  `translate_problematic_rows.py`
- archivos de trazabilidad:
  `.translation_problematic_rows.csv`
  `.translation_suspicious_rows.csv`
  `.translation_high_confidence_mismatches.csv`

Los archivos de prueba o subconjuntos temporales, como reportes de muestra o selecciones parciales creadas solo para inspección puntual, pueden conservarse aparte o excluirse del cierre principal si ya no forman parte del flujo metodológico definitivo.

## Flujo completo de alta confianza

Si quieres generar el subconjunto estricto y luego retraducirlo inmediatamente, puedes encadenar ambos pasos:

```bash
python translate_problematic_rows.py \
  --mode high-confidence-report \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --blank-flagged-translations \
  --high-confidence-report-file ".translation_high_confidence_mismatches.csv" \
&& python translate_problematic_rows.py \
  --mode high-confidence-retranslate \
  --output "Combined Data es-ES.csv" \
  --target-locale es-ES \
  --model gemma3:4b \
  --high-confidence-report-file ".translation_high_confidence_mismatches.csv" \
  --chunk-max-chars 500 \
  --request-timeout 120 \
  --row-batch-size 20 \
  --save-every 20
```
