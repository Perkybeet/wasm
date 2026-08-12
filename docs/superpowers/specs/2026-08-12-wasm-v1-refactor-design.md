# WASM v1 - Diseño del refactor

Fecha: 2026-08-12
Rama: `refactor/v1`
Estado: decisiones cerradas, ejecución autónoma

## Contexto

WASM es una CLI de ~39.000 líneas de Python que despliega aplicaciones web en servidores
Linux (Nginx/Apache, systemd, certbot, bases de datos, backups) y expone un panel web
opcional. Se distribuye por PyPI y por OBS como `.deb` y `.rpm`.

Una auditoría de 10 áreas sobre la totalidad del código (2026-08-12) encontró que el
proyecto no está roto por fuera pero sí por dentro: 327 tests en verde conviven con seis
vulnerabilidades críticas verificadas por ejecución, cinco llamadas a métodos que no
existen en producción desde hace versiones, y una capa web que es una reimplementación
paralela del producto en vez de un cliente de él.

Objetivo declarado por el dueño: dejar un producto que pueda competir de tú a tú con
Coolify.

## Diagnóstico: la causa raíz

Un solo mecanismo explica la mayoría de los defectos: **`except Exception` como flujo de
control por defecto**. Hay 302 bloques en `src/`, 149 de ellos sin registrar nada. Cada
`AttributeError` se degrada a un warning cosmético, así que el proyecto es estructuralmente
incapaz de detectar que está roto. De ahí que `ServiceManager.status()`,
`CertManager.create()`, `store.get_app_by_domain()` y otros dos métodos inexistentes lleven
versiones enteras en producción sin que nadie se entere.

El segundo mecanismo es la **ausencia de un seam de ejecución**: hay 76 llamadas directas a
`subprocess` repartidas por 16 ficheros que puentean el `run_command()` de `core/utils.py`.
Sin un punto de inyección, nada de lo que toca el sistema es testeable, y por eso los
deployers y los managers están a 0% de cobertura. El déficit de tests es una consecuencia
arquitectónica, no una cuestión de disciplina.

El tercero es la **falta de fuente única de verdad**: la versión vive en 6 ficheros, los
alias de comandos en 6 sitios, la detección de tipo de app tiene 4 implementaciones con
precedencias contradictorias, el formateo de bytes 8 copias y la generación de units systemd
4. Toda divergencia es cuestión de tiempo.

## Riesgos críticos verificados

Verificados ejecutando código, no por lectura:

1. **RCE como root vía plantilla systemd.** `templates/systemd/app.service.j2:17` hace
   `Environment="{{ key }}={{ value }}"` sin escapar. Renderizando con un valor que contiene
   comilla y salto de línea se obtiene una unidad con `User=root` y
   `ExecStartPre=/bin/sh -c "..."` inyectados. `env_vars` llega sin validar desde
   `POST /api/apps`.
2. **El monitor mata procesos y borra directorios por coincidencia de substring.**
   `auto_terminate=True` y `dry_run=False` por defecto; `re.search` sobre la cmdline completa
   sin consultar la whitelist; y si el `cwd` del proceso está bajo `/tmp`, el directorio
   entero va a `shutil.rmtree` (`process_monitor.py:442`). Corriendo como root.
3. **`--dry-run` global es pisado por los subparsers.** `parser.py:93` lo define global y
   `:673/:716/:782` lo redefinen, así que `wasm --dry-run monitor scan` ejecuta un escaneo
   real. `--json` y `--no-color` se declaran 11 veces y no se leen nunca.
4. **Path traversal escribiendo units systemd arbitrarias.** `web/api/services.py:391`
   interpola un nombre del body JSON en `/etc/systemd/system/{name}.service`.
5. **Panel root sin HTTPS con identidad falsificable.** `get_client_ip()` confía en
   `X-Forwarded-For` y es la única fuente de identidad para whitelist, rate limit y lockout.
   `require_https` se declara y no se lee. Token en `localStorage` sin CSP. Cero auditoría.
6. **Backups de BD por línea de comandos.** `postgres.py:532` hace
   `bash -c "echo '{result.stdout}' > {path}"` con el dump entero dentro.

Además: `--include-databases` nunca ha respaldado nada e `include_databases=True` se escribe
igual en la metadata; el rollback de despliegue reporta éxito sin revertir nada; el dict de
migraciones está vacío pero marca la BD como migrada, lo que bloquea toda evolución del
modelo de datos; y `--type auto`, que es el valor por defecto del parser, nunca autodetecta:
se degrada siempre a `nodejs`.

## Decisiones

Tomadas de forma autónoma, con su razón. Ninguna es reversible sin coste, así que quedan
aquí registradas.

### D1. Estrategia: reset del producto (opción C)

Se recorta alcance y se reconstruyen las capas expuestas, en vez de parchear. Un proyecto de
un solo mantenedor con 39k LOC y 16% de cobertura real necesita menos superficie, no más.

### D2. CLI: Click 8.x + Rich

El criterio decisor es el empaquetado, no la ergonomía. `python3-click` existe en todos los
targets de CI (Ubuntu 24.04 8.1.6, Fedora 42 8.1.7) y en Linux no tiene ninguna dependencia
de runtime: la migración añade una línea a `obs/debian.control` y otra a `rpm/wasm.spec`.
`python3-rich` está en Ubuntu 24.04 main.

Typer queda descartado porque su dependencia dura `annotated-doc` no existe en Ubuntu 24.04,
Fedora 42 ni Debian trixie. Cyclopts y Textual, por lo mismo.

Se escribe contra la API de Click 8.0 para no ramificar entre el 8.1.6 de Ubuntu y el 8.4.2
de Tumbleweed. Click genera completions de bash, zsh y fish, lo que permite borrar las 2.295
líneas de completions escritas a mano que hoy están desincronizadas por construcción.

`inquirer` se sustituye por `rich.prompt` y `questionary`: `python3-inquirer` no existe en
Debian ni Ubuntu, así que el modo interactivo nunca ha funcionado ahí.

### D3. Web: hipermedia server-driven, no SPA

htmx 2.x + Jinja2 (con fragmentos) + islas de Alpine.js, más xterm.js para terminal y logs y
uPlot para métricas. Todo vendorizado con checksums, cero CDN, cero Node en runtime.

**Sin Tailwind y sin ningún paso de build.** La investigación lo recomendaba para que "sin
build" no significara "feo", pero ese argumento vale cuando se adopta un sistema de diseño
ajeno. Al escribir uno propio (ver el documento de dirección de diseño), Tailwind solo aporta
comodidad de escritura, y a cambio mete un binario en CI y un fichero CSS generado que puede
divergir de su fuente. Para un paquete de distribución, que lo que se lee en el repositorio
sea exactamente lo que se sirve vale más. El sistema de diseño son propiedades personalizadas
de CSS y clases semánticas, escritas a mano.

Dos razones, ambas verificadas:

- **Coolify, el competidor de referencia, hace exactamente esto**: Livewire 3 + Alpine.js +
  Blade + Tailwind v4 + XTerm.js. Es hipermedia server-driven con islas de JS. La decisión no
  es una concesión al empaquetado; es la arquitectura del líder del sector traducida a Python.
- `release.yml:259` y `build-and-upload-obs.sh:61` generan el tarball de OBS con
  `git archive HEAD`, que solo empaqueta lo commiteado, y OBS construye sin red. Con htmx todo
  lo que se sirve está commiteado y el pipeline no se toca. Tailwind v4 tiene CLI standalone
  (binario Rust) así que ni siquiera CI necesita Node.

El sistema de diseño se construye a medida sobre tokens de Tailwind v4 en vez de depender de
Basecoat CSS (proyecto 1.0 con bus factor 1), tomando de él los patrones de componente pero
no la dependencia.

Se reevaluará una SPA solo si aparecen tablas de más de 5.000 filas con virtualización,
multiusuario concurrente con estado compartido, o un segundo desarrollador que amortice la
toolchain de Node.

### D4. El backend web se reescribe como capa fina

No se refactoriza: hoy `api/sites.py` escribe configs de nginx a mano con otro esquema de
nombres que hace los sitios invisibles para los managers, `api/certs.py` duplica
`CertManager`, `api/config.py` duplica `core.Config` y `jobs.py` lo duplica por tercera vez
lanzando el binario `wasm` por `Popen`. Son dos bases de código para un producto, ya
divergentes en producción.

El objetivo son unas 1.500 líneas menos: endpoints que solo traduzcan HTTP a llamadas a los
managers, con `def` síncrono para que FastAPI los lleve al threadpool (eso resuelve de un
plumazo el bloqueo del event loop de los 118 handlers `async def` que ejecutan certbot y
`apt install`), un `exception_handler` global para `WASMError`, y las operaciones largas por
el gestor de jobs devolviendo 202 y un id.

Hardening obligatorio antes de volver a exponerlo: validación estricta de nombres,
`get_client_ip` que solo confíe en `X-Forwarded-For` con lista de proxies configurada, HTTPS
real, sesión en cookie `HttpOnly`+`SameSite` en vez de `localStorage`, CSP, redacción de
secretos en `GET /api/config` y log de auditoría append-only.

### D5. El monitor deja de ser un antivirus y pasa a ser observabilidad

Tal como está —16 regex, cero tests, corriendo como root, con `auto_terminate` por defecto y
`shutil.rmtree` sobre el `cwd` de los procesos— es un pasivo neto: el riesgo que introduce
supera al que mitiga.

Se elimina la clasificación de procesos por IA y la terminación automática. En su lugar,
`wasm monitor` pasa a ser observabilidad honesta: métricas de recursos, salud de servicios,
tail de logs y alertas. Sin matar procesos, sin borrar ficheros, sin enviar datos a terceros.
Eso saca además `httpx` y la llamada a OpenAI del paquete base.

### D6. Modelo de privilegios: `wasm` requiere root

Hoy conviven tres modelos (`run_command` a secas, `run_command_sudo`, y `sudo` incrustado en
el argv de postgres) y `check_root()` existe sin que nadie lo llame. Esa indecisión causa al
menos cuatro bugs reales, entre ellos que `certbot plugins` se ejecute sin sudo y por eso
siempre degrade a `--webroot /var/www/html`.

Se elige el modelo simple y honesto: los comandos que tocan el sistema exigen root y lo
comprueban al entrar. Se elimina `run_command_sudo` y el `sudo` incrustado.

### D7. Migraciones de esquema reales

El dict de migraciones está vacío y aun así marca la BD como migrada, lo que bloquea toda
evolución del modelo de datos. Se implementan migraciones versionadas con una línea base v1
que reconoce los esquemas ya desplegados, y un test que parte de una BD v1 y llega a la
actual.

### D8. Fuente única de verdad para la versión

`[project].version` de `pyproject.toml` como único literal; `src/wasm/__init__.py` deriva de
`importlib.metadata`; `scripts/release.py` propaga a spec, dsc y changelogs en un commit; y un
job de CI `version-consistency` que compara los 6 sitios contra el tag y falla antes de
publicar. Hoy eso es una checklist manual en CLAUDE.md, y las checklists manuales fallan.

No se usa setuptools-scm ni hatch-vcs: ninguno toca los ficheros de packaging de distro, y
ambos fallan con el tarball de `git archive` que no lleva `.git`.

### D9. Multi-distro se mantiene

Ya hay paquetes publicados para Debian/Ubuntu, Fedora y openSUSE. Se unifican los tres
instaladores de paquetes incompatibles que hoy conviven en `setup.py`, `web.py` y
`monitor.py` en un solo `PackageInstaller` con backends por distro.

## Arquitectura objetivo

```
wasm/
  cli/              Click groups; una función por comando, sin lógica de negocio
  core/
    runner.py       CommandRunner: el unico punto por el que se ejecuta algo externo
    store/          persistencia SQLite + migraciones versionadas
    config.py       config en capas, inmutable, con redaccion de secretos
    errors.py       jerarquia WASMError + frontera de errores
  domain/           modelos y reglas; sin I/O
  managers/         adaptadores del sistema (webserver, systemd, certs, backups, db)
  deployers/        estrategias de despliegue sobre un pipeline declarativo
  web/
    api/            capa fina sobre managers; sin logica propia
    views/          fragmentos htmx renderizados con Jinja
    static/         assets vendorizados con checksums
```

Las cuatro reglas que sostienen el diseño:

1. Nada ejecuta procesos externos salvo `CommandRunner`, que exige timeout y es inyectable.
   Un fixture autouse hace fallar cualquier `subprocess` real en los tests.
2. Los managers exponen un contrato tipado; nada cruza fronteras como dict con claves
   mágicas. Se eliminan las 94 funciones anotadas `-> bool` que solo pueden devolver `True`.
3. `except Exception` solo se permite en la frontera de errores del CLI y de la API. Dentro,
   captura tipada.
4. La capa web es un cliente de los managers, nunca una segunda implementación.

## Plan de ejecución

| Fase | Contenido | Criterio de hecho |
|---|---|---|
| 1 | Red de seguridad: `CommandRunner`, `conftest` que prohíbe subprocess real, pytest unificado, CI con lint y mypy bloqueantes, smoke test de importación | CI en rojo ante un método inexistente |
| 2 | Los 6 riesgos críticos, cada uno con un test que falla antes del arreglo | Tests de regresión en verde |
| 3 | Consolidación del núcleo: migraciones, `WebServerManager`, managers de BD, fuentes únicas de verdad, frontera de errores | Duplicación medida a la baja, mypy limpio |
| 4 | CLI a Click, completions y man generados | `parser.py` borrado, paridad de comandos verificada |
| 5 | Web: backend fino y frontend nuevo | Sin CDN, auth endurecida, e2e de los flujos críticos |
| 6 | Tests, docs veraces, packaging, release | Cobertura real por encima del 70% en el núcleo |

## Lo que queda explícitamente fuera

- Despliegues con Docker como ciudadano de primera (Coolify lo tiene; WASM lo trata como un
  deployer más). Es producto, no refactor.
- Auto-deploy por webhook de Git y entornos de preview.
- Multiusuario con roles en el panel.

Se anotan como el siguiente incremento de producto una vez la base sea sólida.
