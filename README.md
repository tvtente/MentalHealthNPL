# MentalHealthNPL

Repositorio para la construcción y documentación de un flujo de traducción controlada de textos sobre salud mental, orientado a tareas de Procesamiento de Lenguaje Natural.

## Contenido

- `mental_health_translation_agent.py`
  Flujo principal de traducción por lotes con validación, reducción automática de batch, control de timeouts y rescate por fragmentos.

- `translate_problematic_rows.py`
  Utilidad auxiliar para una segunda pasada sobre filas problemáticas o textos especialmente largos.

- `translation_methodology_memory.html`
  Memoria metodológica en formato HTML incrustable para notebooks o documentación técnica.

- `Combined Data.csv`
  Dataset de origen.

- `Combined Data es-ES.csv`
  Dataset traducido de salida visible y reutilizable como resultado final.

## Objetivo

El proyecto busca construir una versión traducida y metodológicamente trazable de un corpus textual relacionado con salud mental, preservando en la medida de lo posible el significado semántico, el tono emocional y el contexto comunicativo de los textos originales.

## Flujo principal

El procedimiento implementado:

- trabaja sobre un archivo fuente en CSV
- genera una columna de traducción dependiente del locale de salida
- procesa el corpus en lotes pequeños
- reduce el tamaño del lote automáticamente si la respuesta falla
- valida la estructura de salida antes de escribir en el dataset resultado
- registra filas problemáticas
- intenta rescate por fragmentos para textos demasiado largos

## Ejecución típica

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

## Requisitos

- Python 3
- entorno virtual `venv`
- Ollama instalado y en ejecución
- modelo local descargado, por ejemplo `gemma3:4b`

## Licencia del código

El código de este repositorio se distribuye bajo licencia `Apache-2.0`. Consulta el archivo [LICENSE](/home/tvt/MEGA/GIT/ART/NPL/LICENSE:1).

## Nota sobre los datos

La licencia del código no implica automáticamente la licencia del dataset. El uso, redistribución o publicación de los datos debe verificarse de acuerdo con la procedencia y las condiciones del corpus original.
