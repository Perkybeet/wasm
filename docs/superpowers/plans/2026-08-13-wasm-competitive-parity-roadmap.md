# WASM Competitive Parity — Plan de ruta de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking. Cada tarea se implementa con TDD
> (superpowers:test-driven-development): test que falla → implementación mínima → verde →
> commit. Las tareas de las fases 2-5 están especificadas a nivel de contrato; al
> ejecutarlas, expande cada una en pasos TDD siguiendo los patrones de ficheros ya
> existentes que se citan como plantilla.

**Goal:** Llevar el panel y el producto WASM a paridad con Coolify/Dokploy en seguridad,
gestión, observabilidad y features, sin dependencias nuevas de runtime y sin romper las
cuatro reglas del proyecto.

**Architecture:** El panel es un cliente de los managers (regla 3). Cada fase añade
superficie server-rendered (Jinja+htmx) sobre APIs que en su mayoría ya existen, más
tres subsistemas nuevos acotados: colector de métricas con RRD en SQLite, historial de
despliegues en el store, y notificador/webhooks. Tiempo real por el SSE existente
`/events` con eventos con nombre.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2 (autoescape), htmx (+ ext SSE), uPlot,
xterm.js, SQLite, psutil, cgroup v2, stdlib (`hmac`, `secrets`, `urllib.request`).
Prohibido: deps nuevas de runtime, CDN, `unsafe-eval`, subprocess fuera del runner.

**Spec:** `docs/superpowers/specs/2026-08-13-wasm-competitive-parity-design.md`
(y sus precedentes de 2026-08-12: refactor v1 y dirección de diseño del panel).

## Global Constraints

- Las cuatro reglas de CLAUDE.md aplican a cada tarea. `tests/test_architecture.py`
  las hace cumplir; si una tarea necesita una excepción (solo el notifier), se añade al
  ratchet con comentario y se documenta.
- CSP intacta: `script-src 'self'` sin `unsafe-eval`. Nada de Alpine ni frameworks con
  compilación en runtime. JS nuevo va a `static/panel.js` (vanilla, delegación de
  eventos, hooks `data-*` con contrato en `test_web_client_contract.py`).
- Todo asset nuevo se vendoriza con checksum en `scripts/vendor.lock.json`
  (`python scripts/vendor_assets.py --check` debe pasar).
- Estética: obedecer `2026-08-12-wasm-panel-design-direction.md` (color solo para
  estado, IBM Plex, raíl de 3px, cifras tabulares, errores del sistema verbatim en mono).
- Textos de la UI en inglés (idioma actual del panel); docstrings Google-style; sin
  emojis; errores accionables con `details=`.
- Cada tarea termina con: `pytest -q` verde, `ruff check src/wasm tests` limpio,
  `python scripts/typecheck.py` sin errores nuevos, y un commit propio.
- Verificación de cierre de cada fase:
  `pytest -q && .venv/bin/ruff check src/wasm tests && python scripts/typecheck.py &&
  python scripts/vendor_assets.py --check && python scripts/release.py --check`.
- Los endpoints web nuevos: `def` síncrono (threadpool), errores vía `WASMErrorRoute`,
  operaciones largas como jobs (202 + id). Autorización: sesión para páginas,
  sesión/Bearer para API, y scope adecuado cuando existan tokens (Fase 1).

---

## FASE 0 — Andamiaje (con Fase 1 forma la release v1.3.0)

### Task 0.1: Contadores reales de units en el machine strip

El bug: `MachineState.units_active/failed/busy` (en `src/wasm/web/views/machine.py`)
nunca se rellenan; el strip siempre muestra 0. La fuente correcta es
`ServiceManager.list_services()` + `resolve_states` (ya usados por `views/resources.py`).

**Files:**
- Modify: `src/wasm/web/views/machine.py` (función `read_machine()`)
- Test: `tests/test_web_views.py` (añadir casos)

**Interfaces:**
- Produces: `MachineState` con `units_active/units_failed/units_busy` reales; el
  fragmento `templates/fragments/machine.html` ya los renderiza.

- [ ] Test que falla: con un `FakeRunner` que devuelve 2 units activas y 1 failed,
  `read_machine().units_failed == 1`.
- [ ] Implementación: contar estados desde la misma fuente que usa la página de
  services (no duplicar detección: regla 3).
- [ ] Verde + commit `fix(web): machine strip unit counts were always zero`.

### Task 0.2: Cablear `web.*` de config.yaml a SecurityConfig (M-2)

Hoy `_build_security_config()` en `src/wasm/cli/commands/web.py` solo lee flags CLI;
las claves `web.ip_whitelist`, `web.rate_limit_*`, `web.max_failed_attempts`,
`web.lockout_duration`, `web.token_expiration_hours` de config.yaml se ignoran en
silencio.

**Files:**
- Modify: `src/wasm/cli/commands/web.py` (`_build_security_config`)
- Test: `tests/test_cli_web.py`

**Interfaces:**
- Produces: precedencia documentada `flag CLI > config.yaml > default`, por clave.

- [ ] Tests que fallan: (a) config.yaml con `web.max_failed_attempts: 3` y sin flag →
  `SecurityConfig.max_failed_attempts == 3`; (b) flag explícito gana a config.
- [ ] Implementación + docstring con la precedencia.
- [ ] Verde + commit `fix(web): config.yaml web.* keys were silently ignored`.

### Task 0.3: Usuario por defecto de servicios creados por API (B-1)

`CreateServiceRequest.user` en `src/wasm/web/api/services.py` tiene default `"root"`.
Debe ser el `service_user` de la config (www-data), igual que hace el CLI.

**Files:**
- Modify: `src/wasm/web/api/services.py`
- Test: `tests/test_web_services_api.py`

- [ ] Test que falla: POST /api/services sin `user` → la unit se crea con el
  service_user de config, no root.
- [ ] Implementación + verde + commit `fix(web): API-created services defaulted to root`.

### Task 0.4: E2E de navegador en CI + factory de datos del panel

`scripts/panel_browser_check.py` ya hace login, scroll, drawer, responsive y consola
limpia con Playwright, pero es manual. Se integra en CI y se extrae su siembra de datos
a una factory compartida.

**Files:**
- Create: `tests/panel_factory.py` (factory de apps/services/sites/certs/backups
  sembrados, usada por browser check y tests de API/vistas)
- Modify: `scripts/panel_browser_check.py` (usar la factory; exit codes limpios)
- Modify: `.github/workflows/ci.yml` (job `browser`, `continue-on-error: true` las dos
  primeras semanas — cambiar a bloqueante en cuanto pase 10 runs seguidos)
- Test: el propio job.

**Interfaces:**
- Produces: `panel_factory.seed_panel_state(store, *, apps=3, failed=1, ...)` →
  ids/nombres creados; reutilizable desde pytest y desde el script.

- [ ] Extraer la siembra actual del script a la factory sin cambiar comportamiento.
- [ ] Job de CI: `pip install -e .[web] playwright && playwright install --with-deps
  chromium && python scripts/panel_browser_check.py`.
- [ ] Commit `ci: run the panel browser check on every push`.

---

## FASE 1 — Seguridad a nivel de la competencia (v1.3.0)

### Task 1.1: TLS obligatorio fuera de loopback (M-1) + `--self-signed`

**Files:**
- Modify: `src/wasm/cli/commands/web.py` (validación de arranque, flags nuevos
  `--insecure-http` y `--self-signed`), `src/wasm/web/server.py` (aviso persistente en
  UI si TLS ausente/self-signed)
- Create: helper de generación en `src/wasm/managers/cert_manager.py`
  (`generate_self_signed(domain, cert_path, key_path)` vía `openssl req` con
  CommandRunner)
- Test: `tests/test_cli_web.py`, `tests/test_cert_manager.py`

**Interfaces:**
- Produces: arranque con bind no-loopback sin TLS ni `--insecure-http` → error
  accionable. `--self-signed` genera cert/clave 0600 bajo `/etc/wasm/panel-tls/` y
  arranca con ellos.

- [ ] Tests que fallan: (a) `host=0.0.0.0` sin TLS ni opt-out → `WASMError` con
  mensaje que menciona `--self-signed`; (b) `--allow-ip` NO exime; (c) loopback sigue
  permitido sin TLS; (d) `generate_self_signed` invoca openssl con argv esperado.
- [ ] Implementación mínima + verde.
- [ ] Commit `feat(web): require TLS for non-loopback binds, add --self-signed`.

### Task 1.2: `configtest` antes de persistir configs de sites (M-4)

**Files:**
- Modify: `src/wasm/web/api/sites.py` (`update_site_config`),
  `src/wasm/managers/webserver.py` (método `validate_config_text(text) -> None` que
  escribe a tmp, corre `nginx -t -c` / `apachectl configtest` contra un include
  temporal y lanza `WASMError` con la salida verbatim si falla)
- Test: `tests/test_web_services_api.py` o `tests/test_webserver_managers.py`

- [ ] Test que falla: PUT config con texto inválido → 422 con el error de nginx
  verbatim en `details`, y el fichero original intacto.
- [ ] Implementación (escribir-validar-mover, nunca in-place) + verde.
- [ ] Commit `feat(web): validate webserver config before persisting edits`.

### Task 1.3: TOTP 2FA opcional (RFC 6238, stdlib)

**Files:**
- Create: `src/wasm/core/totp.py` — `generate_secret() -> str` (base32),
  `totp_now(secret, *, t=None) -> str`, `verify(secret, code, *, window=1) -> bool`,
  `provisioning_uri(secret, issuer="WASM", account="admin") -> str`
- Modify: `src/wasm/web/auth.py` (estado 2FA en el mismo almacén de auth: activado,
  secret cifrado con la signing key, códigos de respaldo hasheados de un solo uso;
  login exige código cuando está activo; lockout existente cuenta también fallos TOTP)
- Modify: `src/wasm/web/api/auth.py` (endpoints enroll/confirm/disable/backup-codes)
- Modify: `src/wasm/web/views/router.py` + `templates/pages/settings.html` +
  `templates/login.html` (campo de código cuando 2FA activo; flujo de enrolamiento con
  QR client-side + clave manual)
- Vendor: `src/wasm/web/static/vendor/qrcodegen.js` (Nayuki, MIT) con checksum en
  `scripts/vendor.lock.json`
- Test: `tests/test_totp.py` (vectores RFC 6238 SHA-1), `tests/test_web_auth.py`

**Interfaces:**
- Produces: `wasm.core.totp` reutilizable; `POST /api/auth/2fa/enroll` → secret+URI;
  `POST /api/auth/2fa/confirm {code}` activa; `POST /login` acepta `totp_code`.

Núcleo del algoritmo (stdlib pura):

```python
def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32, casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF) % 10**digits
    return str(code).zfill(digits)

def verify(secret_b32: str, code: str, *, window: int = 1, t: float | None = None) -> bool:
    now = int((t if t is not None else time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret_b32, now + o), code)
               for o in range(-window, window + 1))
```

- [ ] Tests con los vectores de la RFC 6238 (Appendix B, SHA-1) que fallan.
- [ ] `core/totp.py` + verde.
- [ ] Tests de integración login (2FA off = igual que hoy; on = exige código; código
  inválido cuenta para lockout; backup code funciona una sola vez).
- [ ] Integración auth + endpoints + UI + vendor del QR.
- [ ] Commit `feat(auth): optional TOTP two-factor with backup codes`.

### Task 1.4: Tokens API con scopes y expiración

**Files:**
- Modify: `src/wasm/web/auth.py` (tabla nueva `api_tokens` en web-sessions.db:
  id, nombre, hash, scope (`read`|`deploy`|`admin`), created_at, expires_at,
  last_used_at, revoked; emisión muestra el token una sola vez; validación en la misma
  ruta que el Bearer actual)
- Modify: `src/wasm/web/api/deps.py` (dependencia `require_scope(scope)`; los routers
  de lectura aceptan `read`, deploy/update/rollback aceptan `deploy`, el resto `admin`;
  el master token y las sesiones equivalen a `admin`)
- Modify: `src/wasm/web/api/auth.py` (CRUD de tokens), `templates/pages/settings.html`
  (UI: crear con nombre+scope+expiración, listar con last-used, revocar)
- Test: `tests/test_web_auth.py` (scope insuficiente → 403 auditado; token expirado →
  401; revocado → 401; last_used_at se actualiza)

**Interfaces:**
- Produces: `require_scope("deploy")` importable por routers; auditoría registra el
  nombre del token, nunca el token.

- [ ] Tests que fallan (los cuatro del bloque Test).
- [ ] Implementación + UI + verde.
- [ ] Commit `feat(auth): scoped API tokens with expiry and per-token revocation`.

### Task 1.5: Vista de sesiones activas con revocación individual

`GET /api/auth/sessions` y `revoke-all` existen; falta revocación por id y la página.

**Files:**
- Modify: `src/wasm/web/api/auth.py` (`DELETE /api/auth/sessions/{sid_prefix}`),
  `src/wasm/web/auth.py` (revocación por id), `templates/pages/settings.html`
  (tabla: IP, creada, última actividad, badge "this session", botón Revoke)
- Test: `tests/test_web_auth.py`

- [ ] Test que falla: revocar la otra sesión la invalida y conserva la actual.
- [ ] Implementación + verde + commit `feat(auth): per-session revocation in the panel`.

### Task 1.6: Cabeceras residuales

**Files:**
- Modify: `src/wasm/web/server.py` (middleware de headers: añadir
  `Cross-Origin-Resource-Policy: same-origin`; suprimir el banner `Server` de uvicorn
  con `server_header=False` en el arranque)
- Test: `tests/test_web_auth.py` o `tests/test_security.py`

- [ ] Test que falla → implementación → verde → commit `feat(web): tighten residual headers`.

**Cierre Fase 0+1:** verificación completa de fase + `python scripts/release.py 1.3.0
-m "Panel security: TLS by default, TOTP 2FA, scoped API tokens, session management"`
+ tag + push (si el dueño del repo lo autoriza; si no, dejar preparado).

---

## FASE 2 — El panel gestiona todo el producto (v1.4.0)

Patrón común: cada página nueva copia la estructura de una existente
(`templates/pages/certificates.html` para listas con acciones;
`templates/pages/app.html` para detalle; `views/resources.py` para filas). Endpoint
nuevo solo cuando falte; si el manager ya lo hace, el endpoint traduce y punto.
Toda mutación desde el panel usa htmx + fragmento, no full reload.

### Task 2.1: Editor de variables de entorno por app
- API nueva: `GET/PUT /api/apps/{domain}/env` sobre el helper existente
  (`deployers/helpers/env_manager.py`); PUT valida con `validators/environment.py`,
  reescribe el `.env` 0600 y ofrece reinicio (job) opcional.
- UI en `/apps/{domain}`: valores enmascarados, edición por clave, añadir/borrar,
  aviso "restart required" tras guardar, todo auditado.
- Tests: secretos nunca en respuesta sin unmask explícito + roundtrip + validación.
- Commit `feat(panel): environment variable editor per app`.

### Task 2.2: Settings editables
- UI sobre `PUT/PATCH /api/config` existente, agrupada por secciones (paths, webserver,
  SSL, backups, web) con los secretos redactados y no editables desde el panel (los
  secretos se cambian por CLI: decisión — el panel no debe poder auto-elevar su config
  de seguridad; documentarlo en la página).
- Tests de vistas + contrato cliente.
- Commit `feat(panel): editable settings backed by the config API`.

### Task 2.3: Página de bases de datos completa
La API entera ya existe (`api/databases.py`). UI con tabs por engine:
- Estado del engine (running/stopped/not installed) con install/start/stop/restart
  (jobs para install/uninstall).
- Bases: crear/borrar (confirmación por nombre), connection string enmascarada.
- Usuarios: crear/borrar, grant/revoke por BD.
- Backups de BD: listar/crear/restaurar.
- Consola SQL: textarea mono + tabla de resultados; read-only por defecto, checkbox
  explícito "allow writes" (el backend ya lo audita y lo limita a una sentencia).
- Tests de vistas por bloque; browser check ampliado a la página.
- Commit por bloque (`feat(panel): database engines UI`, `... users UI`, `... SQL console`).

### Task 2.4: Crear y editar servicios systemd desde el panel
- UI de creación (modo simple: comando+dir+usuario+puerto; modo raw: textarea unit)
  sobre `POST /api/services` existente; editor del unit (`GET/PUT .../config`) con la
  salida de systemd verbatim si falla el reload.
- Enlazar como "daemons/queue workers": es la feature que el segmento clásico vende.
- Commit `feat(panel): create and edit systemd services from the panel`.

### Task 2.5: Crear sites y editar su config con validación
- UI sobre `POST /api/sites` + editor `GET/PUT .../config` que ya valida (Task 1.2),
  con `nginx -t` verbatim en fallo y botón reload.
- Commit `feat(panel): site creation and config editor with configtest gate`.

### Task 2.6: API + UI de backups programados
- API nueva `src/wasm/web/api/backup_schedules.py` sobre `BackupScheduler` existente
  (create/list/delete por app, schedule presets + OnCalendar libre, retención).
- UI en la página de backups: tabla de schedules con next-run (de `systemctl
  list-timers` vía manager), crear/borrar.
- Tests API + vistas. Commit `feat(panel): scheduled backups management`.

### Task 2.7: Rollback desde la UI
- En `/apps/{domain}`: sección "Rollback points" (lista de `RollbackManager.
  list_rollback_points`) con botón que lanza el job `rollback` existente
  (`POST /api/jobs/rollback`), confirmación por nombre, progreso por SSE ya presente.
- Commit `feat(panel): one-click rollback to a backup from the app page`.

### Task 2.8: Formulario de deploy en htmx con opciones avanzadas
- Convertir `/apps/new` a htmx (validación por campo con fragmentos), añadir:
  env vars iniciales (textarea KEY=VALUE), opciones monorepo (`subdomains`,
  `workspaces`, `no-database`) y docker-compose (`compose-file`, `profiles`) — los
  modelos de `POST /api/apps` y `jobs/deploy` deben transportarlas hasta el deployer
  (hoy solo viaja `include_www`).
- Commit `feat(panel): htmx deploy form with monorepo and compose options`.

**Cierre Fase 2:** verificación de fase + release 1.4.0.

---

## FASE 3 — Observabilidad y tiempo real (v1.5.0)

### Task 3.1: Migración v2 del store: tabla `deployments`
- `src/wasm/core/store.py`: migración versionada (el mecanismo v1 ya existe) con:
  `deployments(id, domain, status TEXT CHECK(queued|running|success|failed|rolled_back),
  trigger TEXT (panel|cli|webhook), git_commit, git_branch, started_at, finished_at,
  duration_s, log_path, error)`. Índices por (domain, started_at DESC).
- Test: BD v1 real migra a v2 conservando datos (patrón del test de migraciones
  existente).
- Commit `feat(store): deployments history table (schema v2)`.

### Task 3.2: Los jobs escriben el historial y capturan el log de build
- `src/wasm/web/jobs.py` + CLI deploy/update/rollback: cada operación crea su fila,
  actualiza estado, y vuelca la salida del pipeline a
  `/var/lib/wasm/deploy-logs/{domain}/{deployment_id}.log` (rotación: conservar 20 por
  app). Una sola implementación compartida CLI/web (regla 3): va en el pipeline, no en
  cada caller.
- Commit `feat(deploy): every deployment is recorded with its captured build log`.

### Task 3.3: Colector de métricas + RRD SQLite
- Create: `src/wasm/monitor/timeseries.py` — `MetricsStore` sobre
  `/var/lib/wasm/metrics.db`: `samples(metric TEXT, ts INTEGER, value REAL)` +
  consolidación (crudo 2s→1h; medias 1min→24h; medias 1h→30d) con DELETE+INSERT
  agregado en cada N inserciones.
- Create: `src/wasm/web/metrics_collector.py` — hilo daemon del proceso web: cada 2s
  muestrea sistema (psutil: cpu%, mem, swap, disco, net rate, load) y por app leyendo
  cgroup v2: `/sys/fs/cgroup/system.slice/{unit}/cpu.stat` (`usage_usec` → % por delta)
  y `memory.current`. Publica a los suscriptores SSE y persiste en MetricsStore.
  Lecturas de cgroup por filesystem (no subprocess); tolerante a units sin cgroup
  (contenedor, cgroup v1): degrada a psutil por PID principal de la unit.
- API: `GET /api/metrics/{metric}?window=1h|24h|30d` para el histórico (uPlot lo pide
  al cargar; el vivo llega por SSE).
- Tests: MetricsStore roundtrip + consolidación con relojes inyectados; parser de
  cpu.stat; el colector nunca lanza (errores → log y sigue).
- Commit `feat(monitor): metrics collector with RRD-style SQLite retention`.

### Task 3.4: SSE multiplexado y machine strip en vivo
- `src/wasm/web/events.py`: además de jobs, emitir evento con nombre `metrics` (JSON
  compacto cada 2s) y `machine` (fragmento del strip cada 5s server-rendered). Vendor
  de la extensión SSE de htmx (`htmx-ext-sse`) con checksum; el strip pasa de polling a
  `sse-swap="machine"` sobre la conexión única existente.
- Contrato cliente actualizado (test_web_client_contract).
- Commit `feat(web): named SSE events; machine strip streams instead of polling`.

### Task 3.5: Gráficas reales con uPlot
- Dashboard: banda de 4 gráficas (CPU, memoria, red, disco) con histórico de
  `GET /api/metrics` + vivo por SSE (ventana deslizante 120 puntos, `uplot.setData`).
- Página de app: CPU/RAM de su unit + marcas verticales de deploys (overlay de eventos,
  patrón Railway) usando la tabla deployments.
- Cards de la lista de apps: sparkline SVG server-rendered (patrón existente del strip).
- Carga perezosa de uPlot solo en páginas con gráficas (hoy se carga siempre: quitarlo
  de base.html).
- JS en panel.js con hooks `data-chart` + contrato. Browser check: las gráficas pintan
  y no hay errores de consola.
- Commit `feat(panel): live system and per-app charts with deploy markers`.

### Task 3.6: Páginas de historial de deploys + favicon de estado
- `/apps/{domain}/deployments`: tabla (estado con raíl, trigger, commit, duración) +
  detalle con log capturado verbatim en mono + botón rollback (Task 2.7).
- `/activity` enlaza a deployments; favicon dinámico refleja el peor estado activo
  (verde/ámbar/rojo — patrón Vercel), swap por JS sobre evento SSE `state`.
- Commit `feat(panel): deployment history pages and status favicon`.

**Cierre Fase 3:** verificación de fase + release 1.5.0.

---

## FASE 4 — Features de producto (v1.6.0)

### Task 4.1: Auto-deploy por webhook git
- Create: `src/wasm/web/api/hooks.py` — `POST /hooks/deploy/{domain}`, SIN sesión:
  autentica por HMAC del body con secret por app (columna nueva en `apps`, generado con
  `secrets.token_urlsafe(32)`, mostrado una vez, regenerable). Acepta
  `X-Hub-Signature-256` (GitHub, `hmac.compare_digest`), `X-Gitlab-Token` (igualdad
  constante) y `X-Gitea-Signature`. Filtro de rama (payload.ref vs branch de la app).
  Rate limit del middleware aplica; cada golpe se audita. Respuesta 202 + job id.
- UI en `/apps/{domain}`: URL del hook + secret (una vez) + regenerar + últimas
  entregas (de la tabla deployments con trigger=webhook).
- Tests: firma buena/mala/ausente, rama distinta ignorada (200 sin job), replay del
  mismo delivery id ignorado.
- Commit `feat(deploy): git push auto-deploy via signed webhooks`.

### Task 4.2: Notificaciones multi-canal
- Create: `src/wasm/core/notifier.py` — canales webhook genérico/Slack/Discord/Telegram
  vía `urllib.request` (timeout 10s, sin reintentos que bloqueen; cola en el worker de
  jobs), email delegado en `EmailNotifier`. Eventos: deploy ok/fail, cert <14 días,
  unit failed (del colector: transición a failed), disco >90%, backup fallido.
  Excepción de import documentada en test_architecture (urllib saliente, no proceso).
- Config `notifications.*` en config.yaml + UI en settings con test por canal.
- El monitor (email hoy) pasa a publicar eventos al notifier (una implementación).
- Tests con servidor HTTP fake local (o inyección del opener).
- Commit `feat(core): multi-channel notifications (webhook, Slack, Discord, Telegram, email)`.

### Task 4.3: Cron jobs de usuario desde el panel
- Reutilizar el patrón de `BackupScheduler`: timers systemd `wasm-cron-{name}` con
  comando, directorio, usuario (default service_user), presets + OnCalendar.
- API `src/wasm/web/api/cron.py` (CRUD + última ejecución con exit code desde journal
  vía manager) + página `/cron` con historial por job.
- Ownership guard igual que services (solo tocar lo que WASM creó).
- Commit `feat(panel): user cron jobs as first-class systemd timers`.

### Task 4.4: Deep links CLI↔panel
- `--open` en `wasm status/logs/list` imprime (y abre con xdg-open si hay DISPLAY) la
  URL del panel equivalente. Barato, patrón Fly.io.
- Commit `feat(cli): --open deep links into the panel`.

**Cierre Fase 4:** verificación de fase + release 1.6.0.

---

## FASE 5 — Pulido UX (se reparte en los cierres de release)

- **5.1 Command palette (Ctrl+K)**: overlay vanilla en panel.js, catálogo server-rendered
  (JSON embebido de rutas+acciones+apps), filtro client-side, CSP-safe. Test de contrato.
- **5.2 Confirmación por nombre en acciones destructivas**: borrar app/BD/servicio exige
  teclear el nombre (patrón GitHub). Sustituye los `confirm()` actuales en esas tres.
- **5.3 Deep-linking**: `hx-push-url` en tabs/filtros (databases, deployments, activity);
  estado reconstruible desde la query string.
- **5.4 Logs mejorados**: autoscroll pausable con botón "jump to bottom", búsqueda en el
  buffer del drawer, y multi-stream (tabs en el drawer, una conexión WS por tab, límite 3).
- **5.5 Esqueletos de carga** (`hx-indicator` con placeholders) en dashboard y páginas
  con datos remotos, como promete la dirección de diseño.

---

## Registro de ejecución

(Los ejecutores anotan aquí el estado real al terminar cada sesión.)

- 2026-08-13: Plan creado. Investigación completa (8 informes). Línea base: 2959 tests
  verdes, ruff limpio, mypy baseline 28. Fases 0-1 en ejecución en esta sesión.
- 2026-08-14: **Fases 0 y 1 COMPLETAS** (v1.3.0 committeada en local, sin tag/push:
  la publicación queda a decisión del dueño). Hecho: 0.1 contadores del strip,
  0.2 config web.* cableada, 0.3 default user de servicios, 0.4 browser check en CI +
  panel_factory, 1.1 TLS obligatorio + --self-signed/--insecure-http, 1.2 configtest
  antes de persistir, 1.3 TOTP 2FA (core/totp.py, qrcodegen vendorizado), 1.4 tokens
  API con scopes (matriz en required_scope, un solo chokepoint), 1.5 sesiones visibles
  y revocables, 1.6 cabeceras. Además adelantado de fases 2-4: **2.1** editor de env
  vars; **3.1** tabla deployments (v2, `triggered_by`); **3.2** DeploymentRecorder con
  log de build capturado y rotación (monorepo/docker-compose quedan fuera: no usan el
  pipeline — follow-up anotado); **3.3** MetricsStore RRD + MetricsCollector (2s,
  cgroup v2 por app) + `GET /api/metrics`; **3.4** SSE multiplexado (eventos `metrics`
  y `machine`, strip sin polling, htmx-ext-sse vendorizado); **4.2a** core/notifier.py
  (webhook/Slack/Discord/Telegram/email; falta cablear eventos de jobs/monitor y UI).
  Suite: 3189 verdes. En curso al cierre de esta anotación: 2.3 página BD completa,
  3.6+2.7 historial+rollback UI, 4.1 webhooks backend, 3.5 gráficas dashboard+favicon.
  Pendiente de fases 2-5: 2.2 settings editables, 2.4 services UI, 2.5 sites UI,
  2.6 backup schedules API+UI, 2.8 deploy form htmx avanzado, gráficas en página de
  app, 4.2b wiring+UI notificaciones, 4.3 cron UI, 4.4 deep links, fase 5 entera.
  Nota: entrada "Deployments" en la sidebar de base.html puede quedar pendiente de
  cablear (base.html estaba asignado al agente de charts). Hallazgo lateral anotado:
  test_cli_webapp deja DryRunFileSystem global sin resetear (conviene fixture en
  conftest).
- 2026-08-14 (cierre de sesión): **FASES 2, 3 y 4 COMPLETAS y Fase 5 casi entera.**
  v1.4.0 y v1.5.0 committeadas en local (SIN tag ni push: la publicación la decide el
  dueño). Terminado desde la anotación anterior: 2.2 settings editables, 2.3 página BD
  completa (engines/usuarios/backups/consola SQL), 2.4 services UI (create+editor unit),
  2.5 sites UI (create+editor con configtest), 2.6 backups programados API+UI (con
  next-run real; list_schedules del scheduler estaba roto y se corrigió), 2.7 rollback
  UI, 2.8 deploy form htmx con opciones monorepo/compose extremo a extremo, 3.5 gráficas
  uPlot (dashboard + por-app con marcas de deploys), 3.6 páginas de historial + favicon
  de estado, 4.1 webhooks firmados (GitHub/GitLab/Gitea, anti-replay, store v3) con UI,
  4.2 notificaciones (módulo + wiring jobs/monitor + UI con test por canal), 4.3 cron
  jobs (manager+API+UI), 4.4 deep links --open, 5.1 palette Ctrl+K, 5.2 confirmación
  por nombre (adoptada en BD/rollback/cron/webhook), 5.4 logs (búsqueda + autoscroll
  pausable), 5.5 esqueletos. Estado final verificado: **3468 tests verdes** (+509 sobre
  la línea base), ruff/format limpios, mypy en baseline (28), vendor 14/14, paridad CLI
  OK, browser check 22/22.
  **Pendiente para futuras sesiones**: 5.3 deep-linking hx-push-url en tabs/filtros;
  `wasm cron` CLI (paridad con el panel); retención en backups programados (el template
  backup-service.j2 no interpola --retention-*: las ejecuciones programadas no rotan);
  monorepo/docker-compose no registran historial de deployments (no usan el pipeline);
  cert_expiring no lo detecta el monitor daemon (solo la página de certificados);
  fuga de aislamiento en tests (USER_DB_PATH se resuelve al importar, antes del sandbox
  — un test sin fixture de store puede abrir la BD real del usuario); DryRunFileSystem
  global sin reset en test_cli_webapp; sección "Diferido con razón" del spec (terminal
  web, preview deploys, multiusuario, one-click, multi-server) sigue vigente.
  Decisión de publicación: tags v1.3.0/v1.4.0/v1.5.0 sin crear; cuando el dueño quiera
  publicar, seguir el proceso del spec de release (tag + push dispara PyPI/OBS).
- 2026-08-14 (publicación): **v1.5.0 publicada** (solo ese tag; 1.3/1.4 quedan como
  hitos internos — los números publicados no necesitan ser contiguos y esos árboles
  tenían man pages sin regenerar). **Rota en Ubuntu 24.04 al arrancar el panel**:
  noble empaqueta pydantic v1 y tres ficheros nuevos usaban `field_validator` (v2).
  **v1.5.1 publicada con el fix**: `web/pydantic_compat.py` (puente v1/v2), guardias
  AST en test_architecture contra API exclusiva de una major de pydantic, job CI
  `pydantic-v1` (tests web con 1.10 pineado) y paso de release que importa la capa web
  dentro del contenedor deb/rpm con las deps de la distro. Lección permanente: los
  tests con deps de pip NO validan las deps de distro; toda API nueva de una librería
  empaquetada debe contrastarse con la versión de noble/bookworm/Fedora (tabla en
  CLAUDE.md) y el gate del contenedor es el chokepoint que lo garantiza.
