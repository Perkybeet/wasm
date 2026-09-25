# WASM 2 - Diseño

Fecha: 2026-09-25
Estado: aprobado por mandato de autonomía del dueño (ver "Gobierno" al final)
Precedentes: `2026-08-12-wasm-v1-refactor-design.md` (las cuatro reglas, siguen vigentes),
`2026-08-13-wasm-competitive-parity-design.md` (paridad v1.3-v1.6, ejecutada),
`2026-08-14-wasm-panel-design-v2.md` (estética v1.6, **superada por este documento**).
Plan: `docs/superpowers/plans/2026-09-25-wasm-v2-plan.md`

## El encargo

El dueño, textualmente: la parte web "es pésima, diseño horrible (se nota que no se usa un
stack como vite)", "no tiene tanto poder como el wasm terminal", y WASM "se queda muy por
detrás de Coolify o Vercel". Pide análisis completo del proyecto y del nicho, revisión del
código al 100% con veredicto sobre una refactorización completa, cambiar la web entera, y un
producto final bien testeado, accesible y con diseño "digno de ser competencia". Autonomía
total, incluido cambiar de stack.

## Método

Ocho investigaciones paralelas el 2026-09-25: mercado (Coolify, Dokploy, Vercel, Railway,
Render, Netlify, Fly, Kamal 2, Dokku, CapRover, Easypanel, Cloudron, Forge/Ploi, Sliplane,
Zeabur, Sevalla y entrantes 2025-26), UX de dashboards y stack frontend, inventario del
contrato web (~100 endpoints), matriz de paridad CLI↔panel, auditoría de núcleo/managers/
validators, auditoría de deployers/CLI, auditoría de tests/CI/empaquetado y revisión de
seguridad de la capa web. Más inspección directa del servidor de producción (solo lectura) y
capturas del panel actual.

## Diagnóstico

### El núcleo: sano. No se reescribe.

- Regla 1 (solo `CommandRunner` ejecuta procesos): cumplimiento 100%.
- Secretos en las cuatro BD tratados correctamente (env/stdin/defaults-file, nunca argv).
- Broad excepts: de 302 a 15 en el núcleo; 9 son del comprobador de actualizaciones.
- Guardas en chokepoint bien ejecutadas (propiedad de units en `ServiceManager`, inyección
  de entorno en systemd).
- Arreglos puntuales y baratos: SQLite sin `WAL` ni `busy_timeout` (panel + CLI a la vez
  dan `database is locked`), carrera en la inicialización del singleton del store y de
  `Config`, `chown` duplicado y silencioso en restore/BD, clave SSH privada que puede quedar
  con permisos laxos mientras se informa de éxito, shims muertos (`run_command_sudo`,
  `ensure_directory_sudo`).

Veredicto: **refactor dirigido**. Reescribir tiraría infraestructura probada (3500 tests)
para arreglar lo que es, en esencia, "terminar migraciones empezadas".

### El modelo de despliegue: la distancia real con Vercel/Kamal

- Se construye **dentro del directorio vivo** como root; un build lento o roto muta el árbol
  que el proceso en marcha está leyendo.
- El rollback es **restaurar un tar** del directorio entero: O(tamaño), no O(1).
- Había **tres semánticas de "actualizar"**: el CLI hacía pull incremental; el panel y el
  webhook re-ejecutaban el deploy completo, cuyo fetch **borra el directorio y re-clona**,
  destruyendo `.env`, ficheros subidos y secretos. Corregido en **v1.6.3** con una única
  orquestación `wasm.deployers.lifecycle.update_app`.
- Monorepo y docker-compose no usan el pipeline declarativo: sin historial de deploys, sin
  log de build capturado, con su propio flujo de pasos.
- Sin límites de recursos por app, sin health gate antes de servir, sin alias de dominio ni
  redirecciones, sin separar entorno de build y de runtime.

### El panel: el problema que el dueño ve

- Jinja + htmx + 1756 líneas de `panel.js` + 1738 de CSS a mano. Técnicamente correcto
  (capa fina sobre managers, SSE, CSP estricta), visualmente pobre: la página de una app es
  un volcado de tablas clave-valor de 2600 px sin pestañas ni jerarquía.
- ~100 endpoints JSON existen. Lo que solo vive en vistas HTML y bloquea una SPA:
  historial de deploys y sus logs, puntos de rollback, prueba de canales de notificación,
  entregas de webhook, la barra de máquina (se emite como **HTML** por SSE), validación de
  campos del formulario de deploy.
- Paridad: no hay botón "Update" en ningún sitio; el monitor tiene API completa y cero UI;
  backups manuales sin BD/volúmenes; certificados sin SAN/webroot; crear un site no emite
  certificado; borrar un servicio salta el guard de `ServiceManager` y deja fila huérfana;
  borrar un site deja certificado y vhost del otro servidor web. Al revés: el CLI no tiene
  `cron` ni `config set`, aunque el propio panel manda ejecutar `wasm config set`.
- Errores no uniformes: tres routers sin `WASMErrorRoute`, `install_error_handlers` nunca
  se llama.

### Seguridad: base por encima del segmento

Sesiones server-side firmadas, CSRF ligado a sesión, lockout compartido HTTP/WS, CSP sin
`unsafe-eval`, auditoría append-only, TOTP, tokens con scopes. Huecos (los dos primeros ya
cerrados en v1.6.3):

- ~~Un token `read` leía todos los `.env` en claro~~ (v1.6.3).
- ~~Un token `read` cancelaba jobs por WebSocket~~ (v1.6.3).
- La consola SQL "de solo lectura" ejecuta como superusuario: `pg_read_file('/etc/shadow')`
  pasa el filtro y la transacción `READ ONLY`.
- SSRF en el notificador (sin filtro de destino; el botón de prueba devuelve el cuerpo).
- No existe re-autenticación (sudo mode) antes de acciones destructivas.
- `Cache-Control: no-store` global (incompatible con assets con hash).
- Arrancar en `0.0.0.0` sin TLS no se impide.

### El mercado (septiembre 2026)

- WASM ya iguala o supera a la mayoría en seguridad (2FA, tokens con scopes, webhooks
  firmados) y observabilidad (métricas en vivo e históricas por app).
- Pierde en: **deploy atómico / rollback instantáneo** (Vercel, Netlify, Railway, Kamal lo
  tienen; Coolify tampoco), previews por PR, plantillas one-click, terminal web.
- Coolify: 11 CVE críticas en enero de 2026 (5 con CVSS 10, inyección de comandos en campos
  de git/backup, ~53k hosts expuestos) y sin límites de recursos por app (issue #873, la
  queja más citada). Dokploy pasó RBAC/SSO/auditoría a licencia propietaria en enero.
- Nadie del segmento sin Docker tiene panel moderno + API + logs en vivo. Aparece **Denia**
  (Rust, namespaces + cgroups sin Docker, muy temprano): el encuadre "sin Docker" ya no es
  exclusivo; hay que ganar por producto, no por eslogan.

## Posicionamiento

**"Tu servidor, con la experiencia de Vercel. Sin Docker."**

Lo que un competidor con Docker no puede decir y WASM sí: se instala con `apt`/`dnf`, cada
app es un unit de systemd que el operador puede inspeccionar con las herramientas de siempre,
cero demonios con socket privilegiado, ejecución de procesos solo por argv (garantizada por
la suite de tests), límites de recursos por app con cgroups nativos, y ninguna capa de
contenedores entre el operador y su proceso.

## Decisiones

### D1. El panel se reescribe como SPA: "WASM Console"

**Stack** (versiones fijadas en `panel/package.json` y `.nvmrc`):

| Pieza | Elección | Por qué |
|---|---|---|
| Framework | React 19 + TypeScript estricto | Ecosistema, accesibilidad, mantenible por personas y modelos |
| Build | Vite | Rápido, salida con hash de contenido, estándar |
| Router | TanStack Router | Rutas y search params tipados; deep-linking de filtros y pestañas |
| Datos | TanStack Query | Caché, invalidación, reintentos, estados de carga uniformes |
| Estilos | Tailwind CSS v4 + tokens CSS propios | CSS estático en fichero, compatible con CSP estricta |
| Primitivas | Base UI (`@base-ui/react`) | Accesibles (APG), sin estilos, `CSPProvider disableStyleElements` |
| Iconos | lucide-react | SVG tree-shaken, coherentes |
| Gráficas | uPlot | Ligero, canvas, ya validado en el proyecto |
| Logs | Visor propio virtualizado (ANSI → spans) | Texto real: seleccionable, buscable, legible por lector de pantalla, sin `<style>` inyectados (xterm los inyecta con el renderer DOM) |
| Tests | Vitest + Testing Library + axe; Playwright + @axe-core/playwright | Unitarios, accesibilidad y E2E contra el backend real |

Se descartan Radix/shadcn (inyectan `<style>` y atributos incompatibles con `style-src`
estricta; su propio equipo creó Base UI por eso), sonner (CSS inline) y xterm para logs.

**Empaquetado sin romper OBS.** El código fuente vive en `panel/` (raíz del repo). El build
se **commitea** en `src/wasm/web/static/` (sustituye el contenido actual). El glob
`web/static/**/*` de `pyproject.toml` ya lo empaqueta en wheel, sdist, deb y rpm sin tocar la
spec ni debian. OBS nunca ejecuta Node. Un job de CI reconstruye y falla si el `dist`
commiteado difiere del que produce el código (mismo patrón que man pages y completions), con
`strictRequires` y `maxParallelFileOps: 1` para salida determinista.

**Contrato tipado.** FastAPI genera el OpenAPI; `openapi-typescript` genera
`panel/src/api/schema.gen.ts` (commiteado). CI regenera y falla si difiere. El panel no puede
llamar a un endpoint que no existe ni leer un campo que no se envía: el compilador lo impide.
El esquema se sirve autenticado en `/api/openapi.json` (valor para usuarios de la API).

**CSP endurecida.** `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'
data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none';
form-action 'self'; object-src 'none'`. Sin `unsafe-inline` en estilos (React aplica los
estilos por CSSOM, que la CSP no bloquea; Base UI con `disableStyleElements`). Trusted Types
(`require-trusted-types-for 'script'`) se activa si el E2E no registra violaciones; el E2E
falla ante cualquier violación de CSP.

### D2. El backend es solo API

- Se eliminan `web/views/`, `web/templates/`, `panel.js`, `app.css`, htmx, sse.js, xterm,
  qrcodegen y las fuentes vendorizadas (pasan a dependencias npm empaquetadas en el build).
  Jinja se queda para plantillas de nginx/systemd.
- `index.html` se sirve para toda ruta GET fuera de `/api`, `/assets`, `/events`, `/ws`,
  `/hooks`, `/health`; `no-store` para `index.html`, `immutable` un año para `/assets/*`.
- Endpoints nuevos: deployments (lista filtrable, detalle, log capturado), puntos de
  rollback, prueba de canal de notificación, entregas de webhook, sesión actual
  (`GET /api/auth/session`), inspección de repositorio para el asistente de alta
  (`POST /api/apps/inspect`), diagnóstico de app (`GET /api/apps/{d}/diagnose`).
- Errores uniformes en **todos** los routers: `{error, detail, hint, fields?}`; los 422 de
  validación llevan `fields` por campo, que el formulario pinta junto a cada control.
- Tiempo real en un solo stream SSE `/events` con eventos JSON: `machine`, `metrics`,
  `app` (cambio de estado), `job` (progreso y líneas de log), `notice`. Se elimina
  `/ws/system` (tercera implementación de métricas). Se quedan `/ws/logs/{domain}` (journal)
  y `/ws/jobs/{id}`.
- **Jobs persistentes**: tabla `jobs` en el store + log por job a fichero (como los logs de
  deploy). Un reinicio del panel ya no pierde qué pasó.
- Una sola ruta de alta de app (`POST /api/apps`); `POST /api/jobs/deploy` desaparece.

### D3. Motor de despliegue v2: releases atómicas

```
/var/www/apps/{app}/
  releases/20260925-143012-a1b2c3d/   un build completo, inmutable tras activarse
  releases/20260924-101500-9f8e7d6/
  current -> releases/20260925-143012-a1b2c3d
  shared/.env                          secretos, 0600, fuera de cualquier release
  shared/<rutas persistentes>          uploads, storage, data... enlazadas en cada release
  repo/                                caché git para clonar releases sin red completa
```

- **Build aislado**: cada release se construye en su propio directorio. El proceso en marcha
  no ve nada hasta la activación.
- **Instalación reutilizada**: si el lockfile no cambió respecto a la release activa,
  `node_modules`/`.venv` se copian (reflink/hardlink cuando se pueda) en vez de reinstalar.
- **Activación con health gate**: cambio atómico del symlink (`rename` de un symlink
  temporal), reinicio del unit, health check; si falla, **vuelta automática** a la release
  anterior y deploy marcado como fallido con el log verbatim.
- **Rollback instantáneo**: apuntar `current` a una release anterior + reinicio. Segundos,
  no minutos. El rollback por backup queda para recuperación ante desastres.
- **Retención**: últimas 5 releases por defecto (configurable).
- **Límites de recursos por app**: `MemoryMax`, `CPUQuota`, `TasksMax` en el unit,
  editables en el panel. Lo que Coolify no tiene.
- **Migración sin riesgo**: las apps nuevas nacen con releases. Las existentes siguen en su
  modo in-place (que `update_app` ya maneja) hasta que el operador pulsa "Activar releases"
  o ejecuta `wasm app migrate <dominio>`: el árbol vivo pasa a ser la primera release, el
  `.env` y las rutas persistentes detectadas (no rastreadas por git) pasan a `shared/`, el
  unit y el site se reescriben sobre `current`. Nunca automático en un update.
- Monorepo y docker-compose se integran en el pipeline con historial y log capturado.

### D4. Dominios de verdad

Tabla `domains` (app, dominio, tipo: primario / alias / redirección) en vez de un único
`include_www`. nginx recibe `server_name` de todos y bloques de redirección 301 para los de
tipo redirección (apex↔www). El certificado cubre todos. Pantalla de dominios con
comprobación DNS (registro esperado frente a detectado) antes de pedir el certificado y el
error de certbot verbatim.

### D5. Seguridad

- **Sudo mode**: `POST /api/auth/elevate` (código TOTP, o master token si no hay 2FA) marca
  la sesión como elevada 10 minutos. Lo exigen: borrar app/BD/servicio/site, consola SQL en
  escritura, editar unit o site, revelar `.env`, crear tokens, desactivar 2FA y escribir la
  configuración. La SPA muestra "Confirma que eres tú" y reintenta.
- Consola SQL de lectura con un rol por BD **sin** privilegios de fichero
  (`pg_read_server_files`, `FILE`).
- Notificador con filtro SSRF (sin loopback, RFC1918, link-local ni metadata salvo lista
  explícita) y sin eco del cuerpo remoto.
- Arranque en no-loopback sin TLS rechazado salvo `--insecure-http` explícito.
- Fallos de firma de webhook alimentan el lockout.

### D6. Paridad completa CLI ↔ panel

Todo lo que hace el CLI lo hace el panel y viceversa (salvo lo que por naturaleza es de
terminal: `db connect`, `setup init`). Se añaden al panel: Update, monitor (instalar,
estado, observaciones), opciones completas de backup y de certificados, emisión de
certificado al crear un site. Se añaden al CLI: `wasm cron`, `wasm config get/set`,
`wasm releases`, `wasm app migrate`, `--json` en `list`/`status`/`logs`. Borrar un site o
un servicio pasa por el manager (una implementación).

### D7. Diferenciación (v2.0)

- **Diagnóstico ("¿por qué está caída?")**: una página por app que correlaciona estado del
  unit, últimas líneas del journal, error log de nginx, puerto escuchando, certificado,
  último deploy, OOM kills y disco, todo verbatim, con la causa probable arriba.
- **Asistente de alta**: pegar el repo, el backend lo inspecciona (tipo detectado editable,
  comandos, puerto, variables de `.env.example` como formulario), desplegar, y ver el log de
  build en vivo hasta que la app responde.
- **Recursos de la máquina**: cada app con su consumo frente a su límite.

Diferido con fecha (v2.1+): previews por rama con cupos y TTL, recetas one-click sin Docker,
terminal web con sesión grabada, blue/green sin downtime (dos units + upstream nginx),
`wasm export` / `wasm import --from vercel|railway`.

### D8. Diseño: dirección visual

**Carácter**: "instrumento de precisión": denso donde el operador trabaja (tablas, logs),
generoso donde decide (cabeceras, estados). Superficies acromáticas; el color solo significa
algo: estado (verde corriendo, ámbar en curso, rojo fallo, gris parado) y un acento violeta
para lo interactivo, heredado del inicio del degradado del logo. El degradado de marca vive
solo en el logotipo.

- **Tipografía**: Mona Sans (variable, OFL) para la interfaz; JetBrains Mono (OFL) para todo
  valor del sistema (rutas, puertos, commits, logs, IDs). Pesos 400/500/600, cifras
  tabulares en valores que cambian, tracking negativo en títulos.
- **Escala**: espaciado 4-8-12-16-20-24-32-40-48-64; radios 6 (controles), 10 (tarjetas),
  999 (pills); bordes de 1 px; en oscuro la elevación es luminancia de superficie, en claro
  además sombra mínima; sombras marcadas solo en overlays.
- **Movimiento**: 120-180 ms ease-out de entrada; cambios de estado como pulso de opacidad,
  nunca deslizamientos; todo degradado a instantáneo con `prefers-reduced-motion`.
- **Temas**: oscuro y claro completos, por defecto el del sistema, contraste AA verificado
  por test sobre los tokens.
- **Arquitectura de información**: sidebar global (Resumen, Aplicaciones, Bases de datos,
  Backups, Dominios y certificados, Servicios, Cron, Actividad, Servidor, Ajustes);
  dentro de una app, pestañas: Resumen, Deploys, Logs, Métricas, Entorno, Dominios,
  Diagnóstico, Ajustes. Barra superior con estado de la máquina, `⌘K` y usuario.
  Atajos `g a` (apps), `g d` (deploys), `/` (buscar), `?` (ayuda).

### D9. Accesibilidad (WCAG 2.2 AA, verificada, no declarada)

Primitivas con patrones APG; foco visible siempre; nada depende solo del color (estado =
color + forma + texto); regiones vivas para transiciones de deploy (polite) y fallos
(assertive), nunca para líneas de log; gráficas con resumen textual y tabla alternativa;
objetivos de 24×24 px mínimo; el foco nunca queda tapado por barras sticky; diálogos con foco
atrapado y devuelto; navegación completa por teclado. axe se ejecuta en cada página y en
ambos temas dentro del E2E, que falla ante cualquier violación.

### D10. Calidad

- **Backend**: la suite actual sigue; los tests de vistas Jinja se sustituyen por tests de
  API de los endpoints nuevos.
- **Frontend**: unitarios de componentes y hooks (Vitest), E2E de cada flujo principal contra
  el backend real sembrado con `tests/panel_factory.py`, axe en todas las páginas, captura de
  violaciones de CSP, capturas en ambos temas y móvil revisadas por una persona (o modelo).
- **Integración real**: un arnés con Docker levanta un contenedor con systemd y nginx,
  instala WASM desde el wheel y despliega apps de ejemplo (estática, Node, Next.js) por CLI y
  por API: release, activación, rollback instantáneo, update, migración de una app in-place.
  Se ejecuta en local antes de cada release mayor y en CI bajo demanda.
- **CI**: job `panel` (build reproducible, lint, typecheck, unitarios, OpenAPI sincronizado)
  y job `e2e` bloqueantes desde el primer día.

### D11. Versionado

v1.6.3 (publicada 2026-09-25): corrección de pérdida de datos en updates desde panel y
webhook, y los dos huecos de scope. **v2.0.0**: consola nueva, API completa, motor de
releases, dominios, sudo mode, paridad. Mayor porque cambia la superficie web entera y el
layout en disco de las apps nuevas.

## Riesgos

- **Producción del dueño** (≈19 apps en arennalabs.com): la v2 no toca apps existentes hasta
  que se migran a mano. El paquete no se instala allí sin haber pasado el arnés real.
- **Determinismo del build**: si Vite no reproduce byte a byte en CI, el gate compara el
  build de CI consigo mismo dos veces y contra el commiteado a nivel de manifiesto.
- **pydantic v1 en Ubuntu 24.04**: cualquier modelo nuevo pasa por `pydantic_compat` y el
  job `pydantic-v1` del CI (lección de v1.5.0).
- **Licencia**: WASM-NCSAL prohíbe el uso comercial, en tensión con competir con Coolify en
  agencias. Es decisión del dueño; aquí solo se señala.

## Gobierno

El dueño delegó autonomía total ("Tienes libertad y permiso absoluto... Sé 100% autónomo").
Este documento sustituye la revisión interactiva del spec: queda escrito, commiteado y
justificado para que el dueño lo revise cuando quiera; cualquier cambio suyo prevalece.
