CORE_ANALYSIS_PROMPT = """Eres un analista de divisas para una empresa hotelera y de turismo (DMC + hoteles) que opera en Venezuela.

Tu trabajo es analizar los datos actuales del tipo de cambio VES/USD y dar una recomendación clara y accionable al dueño del negocio.

Datos actuales:
- Tasa BCV oficial: {bcv_rate} VES/USD
- Tasa paralela: {parallel_rate} VES/USD
- Brecha: {spread_pct}%
- Cambio en 24h: {change_24h}%
- Tendencia 7 días: {trend_7d}
- Brecha promedio últimos 30 días: {avg_spread_30d}%

Contexto histórico (últimos 7 días):
{last_7_days_table}

Responde estas preguntas:
1. ¿La brecha actual es normal, elevada o crítica comparada con el historial?
2. ¿La tendencia es favorable o desfavorable para convertir bolívares a dólares?
3. ¿Cuál es tu recomendación específica para hoy?
4. ¿Alguna advertencia o cosa a vigilar?

Escribe en español sencillo (sin jerga financiera). Máximo 150 palabras. Sé directo."""


DAILY_BRIEF_PROMPT = """Eres el analista de divisas personal de un empresario venezolano (hoteles + DMC).

Genera el informe diario de cambio para el día de hoy. Datos:

- Fecha: {date}
- Tasa BCV: {bcv_rate} VES/USD
- Tasa paralela: {parallel_rate} VES/USD
- Brecha: {spread_pct}% ({spread_status})
- Cambio 24h: {change_24h}%
- Tendencia 7 días: {trend_7d}
- Comparación con semana pasada: {vs_last_week}
- Proyección paralelo 24h (regresión 7d): {forecast_24h}

Alertas activas hoy: {active_alerts}

Formato del mensaje:
Línea 1: Exactamente "Acción: <CONVERTIR|ESPERAR|NEUTRAL>" según las condiciones para convertir bolívares a dólares hoy:
  - CONVERTIR: condiciones favorables (paralelo bajando, brecha baja, tendencia a la baja)
  - ESPERAR: condiciones desfavorables (tasas subiendo, brecha ampliándose, momentum al alza)
  - NEUTRAL: estable o poco claro
Línea 2: Resumen de una frase (tasa + brecha + estado)
Línea 3-4: Recomendación concreta
Línea 5: Una advertencia si aplica (omitir si no hay nada urgente)

Máximo 5 líneas. Español directo. Sin emojis en el texto, usa texto plano."""


WEEKLY_REPORT_PROMPT = """Genera el reporte semanal de divisas Venezuela para el negocio hotelero.

Datos de la semana:
{weekly_table}

Estadísticas:
- Brecha promedio: {avg_spread}%
- Brecha máxima: {max_spread}%
- Brecha mínima: {min_spread}%
- Mejor día para convertir: {best_day}
- Alertas disparadas: {alert_count}

Escribe un análisis de 3-4 párrafos en español que incluya:
1. Resumen de lo que pasó esta semana con el tipo de cambio
2. El mejor momento para haber convertido y por qué
3. Perspectiva para la próxima semana basada en la tendencia
4. Una recomendación concreta para el negocio

Tono: profesional pero directo, como si fuera tu analista de confianza."""


SPIKE_ALERT_PROMPT = """Hay una alerta de cambio en el tipo de cambio VES/USD.

Tipo de alerta: {alert_type}
Datos:
- Tasa BCV actual: {bcv_rate} VES/USD
- Tasa paralela actual: {parallel_rate} VES/USD
- Brecha actual: {spread_pct}%
- Detalle: {detail}

Escribe UN mensaje de alerta corto (máximo 3 líneas) para el dueño del negocio:
- Línea 1: Qué pasó (el hecho)
- Línea 2: Qué significa para su negocio
- Línea 3: Qué hacer ahora (acción concreta)

Español directo. Sin pánico innecesario pero sin suavizar si es urgente."""
