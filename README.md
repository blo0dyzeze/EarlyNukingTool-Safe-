# EarlyNukingTool-Safe-
# Early

**Early** es una tool escrita en Python enfocada en operaciones rápidas sobre recursos de servidores de Discord usando la API.
Está diseñada para ejecutar acciones de eliminación de múltiples recursos de forma muy rápida utilizando concurrencia asíncrona y optimizaciones de red.

El objetivo principal del proyecto fue experimentar con:

* manejo avanzado de **asyncio**
* optimización de **requests HTTP**
* control de **rate limits**
* estructuras de datos eficientes para alto rendimiento

No es un proyecto enorme, pero internamente tiene varias optimizaciones interesantes.

---

# Características

* Arquitectura completamente **asíncrona** usando `asyncio` y `aiohttp`
* Eliminación paralela de múltiples tipos de recursos
* Sistema de **rate limiting distribuido**
* Pool de headers pre-generados para evitar overhead
* Logger basado en **ring buffer** sin locks
* Métricas de rendimiento en tiempo real
* Manejo automático de errores, retries y rate limits

---

# Recursos que puede procesar

La tool puede interactuar con varios tipos de recursos del servidor:

* Channels
* Roles
* Webhooks
* Emojis
* Stickers
* Threads
* Scheduled Events
* Invites
* Soundboard sounds
* Integrations
* Templates
* AutoMod rules
* Bans

Durante la ejecución también muestra estadísticas como:

* recursos procesados
* errores
* rate limits recibidos
* requests por segundo
* latencia promedio

---

# Requisitos

Python 3.9 o superior.

Instalar dependencias:

```
pip install aiohttp
```

---

# Uso

Ejecutar el script:

```
python NukingFast.py
```

Luego el programa pedirá:

* token de Discord
* ID del servidor

Después de confirmar la ejecución, la herramienta empezará a procesar los recursos detectados.

---

# Estructura interna (simplificada)

El proyecto tiene varios componentes principales:

**HeaderPool**

Genera miles de headers diferentes previamente para evitar construirlos en cada request.

**DistributedRateLimiter**

Sistema de buckets independientes para distribuir requests y reducir bloqueos por rate limit.

**RingLogger**

Logger basado en buffer circular que permite imprimir logs sin bloquear el flujo principal.

**Stats**

Recolecta métricas de rendimiento durante toda la ejecución.

**delete_resource**

Función central que maneja la eliminación de recursos con manejo de errores y reintentos.

---

# Propósito del proyecto

Este proyecto fue creado principalmente como práctica para:

* optimización de herramientas de red
* manejo avanzado de concurrencia en Python
* experimentación con estructuras eficientes

---

# Nota

Usa este proyecto de forma responsable y únicamente en entornos donde tengas autorización.

---

# Autor

ZeZe
