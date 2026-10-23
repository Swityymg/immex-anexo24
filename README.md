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

Para habilitar Gemini, define `GEMINI_API_KEY` en el entorno antes de arrancar o crea `.streamlit/secrets.toml` con el formato indicado en `.streamlit/secrets.toml.example`. La clave compartida en el chat debe revocarse y reemplazarse antes de volver a usar Gemini. IA IMMEX usa modelos Gemini Flash estables con fallback automático entre `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite` y `gemini-2.5-flash`. Sin credencial, responde desde los cálculos locales; si Gemini falla, la respuesta local se identifica como tal.

En la barra lateral se puede usar el archivo oficial del reto o cargar un Excel/CSV propio. El Excel puede usar las hojas Anexo 24 o una primera hoja tabular con encabezados mapeables. El CSV de entradas requiere pedimento, fecha, material y cantidad; admite campos opcionales de clave, fracción, descripción, valor unitario, plazo, RFC y razón social. La descarga genera una plantilla Excel vacía con las hojas de captura requeridas.

El workflow `.github/workflows/anexo24-daily.yml` programa `scripts/check_anexo24.py` a las 07:00 hora de Ciudad de México. El resultado se guarda en `data/normative_status.json`, que alimenta la campana, el resumen del día y el contexto de IA IMMEX. Para recibir el estado automático, publica el repositorio en GitHub, habilita Actions y ejecuta manualmente una primera vez «Chequeo diario Anexo 24»; después corre cada día. La app no ejecuta un proceso diario si el servidor está apagado.

El documento base incluido es el **Anexo 24 de RGCE 2026 publicado el 15/01/2026**, disponible en `data/Anexo_24_RGCE_2026.pdf` y como texto extraído `data/anexo24_2026.txt`. No se debe describir como “última versión” sin comprobar publicaciones posteriores. El escaneo automático de títulos/texto es una señal de revisión, no una validación jurídica completa; un fallo de red se muestra como “no verificado”, no como ausencia de cambios.

La UI muestra una campana ordenada por prioridad, cuatro tarjetas KPI blancas con borde uniforme, el resumen del día cerrado por defecto en un toggle y el chatbot IA IMMEX 🤖 flotante en la esquina inferior derecha. Gerencia y Operación tienen dashboards distintos: Gerencia ve agregados BI y decisiones; Operación conserva captura, ledger, simulador PEPS, excepciones y cierre.

La pestaña **Asistente de Consultas** permite preguntar en lenguaje natural sobre el libro Anexo 24 seleccionado usando Gemini. Ejecuta consultas de solo lectura sobre copias temporales de sus capturas y de los saldos, vencimientos, consumos, faltantes y mermas recalculados por `ledger.py`; muestra un resumen ejecutivo junto con la tabla de respaldo (máximo 500 filas). No modifica el Excel ni SQLite.

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