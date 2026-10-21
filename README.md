# Control de inventarios IMMEX | Anexo 24

MVP en Streamlit para registrar entradas y salidas, recalcular saldos con PEPS estricto, conciliar diferencias y generar reportes CSV. El libro de ejemplo se conserva en `data/IMMEX_Template_v2.xlsx`; los valores derivados del Excel no son fuente del cálculo.

## Ejecutar

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Abre la URL local que Streamlit muestre en la terminal. SQLite crea `data/anexo24.sqlite3` al iniciar; guarda altas, salidas, parámetros, avances de cierre y eventos de auditoría. `.gitignore` excluye la base demo y el entorno virtual.

Para habilitar el análisis remoto, define `ANTHROPIC_API_KEY` en el entorno antes de arrancar, o declárala en `.streamlit/secrets.toml`. El modelo predeterminado es `claude-sonnet-5-5`; `ANTHROPIC_MODEL` permite configurarlo. Sin credencial, el asistente produce un análisis determinista local de todas las excepciones; un fallo remoto se muestra explícitamente y no se disfraza como respuesta de IA.

Las cargas Excel se agregan como perfiles en `data/profiles/`. El CSV masivo de entradas requiere `pedimento, clave, fecha_entrada, material, cantidad_kg, valor_unit_usd`; el de salidas requiere `pedimento, clave, fecha, material, tipo, cantidad_kg` y opcionalmente `observacion`.

## Criterios del MVP

- El corte inicial, tipo de cambio y tolerancia se cargan desde `Parametros` y se pueden simular en la barra lateral.
- La tolerancia de merma es un umbral interno; no modifica los porcentajes capturados ni se presenta como límite legal.
- La exposición se valúa como saldo por USD/kg por tipo de cambio. Las alertas se determinan por vencimiento legal individual.
- Las validaciones internas contrastan descargas PEPS, faltante MP-003 y métricas globales contra los oráculos del reto.
- La aplicación ofrece vistas de Operación, Gerencia y Auditor de solo lectura; el tablero de gerencia prioriza el siguiente vencimiento y su acción sugerida.
- La bitácora registra automáticamente usuario, fecha, acción, motivo, referencia y valores anterior/nuevo; las capturas y el cierre mensual persisten en SQLite.
- El reporte de saldos agrupa inventario activo por fracción arancelaria.
- La simulación PEPS se puede aplicar temporalmente y revertir; el registro definitivo es una acción separada auditada.
- Los oráculos específicos solo se comparan contra la plantilla oficial de demo, no contra perfiles diferentes.

El análisis y las sugerencias de regularización son apoyo operativo, no una opinión legal. La bitácora SQLite del MVP no incorpora autenticación, controles de acceso de base de datos, respaldo automatizado ni protección contra manipulación fuera de la app; esos controles deben añadirse antes de producción.