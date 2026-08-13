# WASM - Paridad competitiva: diagnóstico y diseño

Fecha: 2026-08-13
Estado: decisiones cerradas, ejecución autónoma en curso
Precedentes: `2026-08-12-wasm-v1-refactor-design.md` (arquitectura y reglas),
`2026-08-12-wasm-panel-design-direction.md` (estética del panel). Este documento no
revierte ninguna decisión de aquellos; construye encima.
Plan de ejecución: `docs/superpowers/plans/2026-08-13-wasm-competitive-parity-roadmap.md`

## Objetivo declarado

Dejar WASM, como mínimo, al nivel de la competencia self-hosted (Coolify, Dokploy,
Easypanel, CapRover) en seguridad, gestión, panel web y funcionalidades, explotando el
nicho que ninguno cubre: **PaaS sin Docker obligatorio, self-hosted, gratuito y con API**.

## Método

Ocho líneas de investigación en paralelo (2026-08-13): cuatro sobre el propio código
(panel, núcleo/CLI, seguridad, calidad/CI) y cuatro sobre el mercado (Coolify; Dokploy/
Easypanel/CapRover/Dokku/Kamal; UX de Vercel/Railway/Render/Fly; estándares de seguridad
y tiempo real para paneles server-rendered). Línea base verificada en local: 2959 tests
en verde, ruff limpio, mypy con baseline de 28.

## Diagnóstico

### Dónde está WASM hoy (v1.2.1)

**El núcleo ya es competitivo.** Pipeline de deploy declarativo con undo real por paso,
7 tipos de app (nextjs/vite/nodejs/python/static/monorepo/docker-compose), 4 motores de
BD con usuarios/privilegios/dumps/consola SQL, backups con scheduling systemd, retención,
verify y restore cross-machine, certificados Let's Encrypt completos, servicios systemd
con guarda de propiedad, monitor de observabilidad honesta con alertas por email, health
check global. Nada de esto lo tiene el segmento clásico sin Docker al mismo nivel.

**El panel expone quizá un 30% de ese núcleo.** El backend web es correcto (capa fina
sobre managers, jobs con SSE, logs en vivo por WebSocket + xterm.js, sistema de diseño
propio con identidad real) pero:

- La API de bases de datos (1264 líneas: engines, usuarios, backups, consola SQL) tiene
  **cero UI**. Lo mismo `monitor.py` y `system.py` completos.
- Env vars de una app: solo lectura. Settings: solo lectura pese a que `PUT /api/config`
  existe. Crear/editar services y sites: API sí, UI no.
- **No hay ni una gráfica.** uPlot está vendorizado y cargado en cada página sin un solo
  uso. No se persiste ninguna serie temporal. Los contadores de units del machine strip
  siempre marcan 0 (bug: `MachineState.units_*` nunca se rellena).
- No hay historial de despliegues (el store no tiene tabla `deployments`), no hay
  rollback desde la UI (el manager existe), no hay webhooks de git, no hay
  notificaciones más allá del email del monitor.

**Seguridad: base sólida, por encima del segmento clásico.** Sesiones server-side
firmadas con HMAC, cookies HttpOnly/SameSite=Strict, CSRF doble-envío ligado a sesión,
lockout + rate limit compartidos entre HTTP/WS, middleware ASGI único, CSP estricta
(`script-src 'self'`, sin unsafe-eval; Alpine se eliminó por esto), auditoría append-only,
runner argv-only. Hallazgos accionables: M-1 (HTTP en claro permitido con `--allow-ip`),
M-2 (claves `web.*` de config.yaml que no se leen nunca → falsa confianza), M-3 (sin
2FA ni tokens API con scopes), M-4 (editor de config de sites sin `configtest` previo),
B-1 (servicios creados como root por defecto), B-6 (sin vista/revocación individual de
sesiones).

**Calidad: andamiaje fuerte con un agujero.** CI con 5 jobs bloqueantes, enforcement
arquitectónico por AST con ratchets, contratos del panel sin navegador (estilo, cliente
JS↔rutas). El agujero: el único chequeo con navegador real (`panel_browser_check.py`,
Playwright) es manual y no está en CI — exactamente la clase de bug (scroll, drawer,
EventSource vs WS) que ya se coló en releases. Cobertura débil justo donde vamos a
construir: websockets 37%, jobs 40%.

### El mercado (agosto 2026)

| | Coolify | Dokploy | Easypanel | CapRover | Dokku | Ploi/RunCloud | CloudPanel | WASM hoy |
|---|---|---|---|---|---|---|---|---|
| Self-hosted gratis | ✓ | ✓ (core) | parcial | ✓ | ✓ | ✗ (SaaS) | ✓ | ✓ |
| Sin Docker obligatorio | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ | **✓** |
| Panel web completo | ✓ | ✓ | ✓ | ✓ | ✗ (de pago) | ✓ | parcial | parcial |
| Historial deploys + rollback UI | parcial | ✓ | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ |
| Auto-deploy por webhook git | ✓ | ✓ | parcial | ✓ | ✗ | ✓ | ✗ | ✗ |
| Métricas vivo + históricas | ✓ (Sentinel 10s) | ✓ (agente 20s) | ✓ (Prometheus) | parcial (NetData) | ✗ | ✓ | parcial | ✗ |
| Logs en vivo en panel | ✓ | ✓ | ✓ (Loki) | ✓ | ✗ | ✗ (¡nadie!) | ✗ | **✓** |
| Env vars editables en panel | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ |
| BD gestionadas con UI | ✓ | ✓ | ✓ | parcial | plugins | ✓ | ✓ | ✗ (API sí) |
| Backups programados UI | ✓ | ✓ | ✓ (pago) | parcial | plugins | ✓ | ✓ | ✗ (CLI sí) |
| Notificaciones multi-canal | ✓ (6 canales) | parcial | ✓ (pago) | pago | ✗ | ✓ | ✗ | ✗ (solo email) |
| 2FA | ✓ | ✓ | ✓ | pago | ✗ | ✓ | ✓ | ✗ |
| API tokens con scopes | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
| Terminal web | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ (diferido) |
| Cron jobs de usuario UI | ✗ | ✓ | parcial | ✗ | ✗ | ✓ | ✓ | ✗ |

Datos duros: Coolify 60,5k estrellas, Apache-2.0, terminal web, Sentinel, 294 one-click,
equipos/roles/audit; su rollback es tosco y su monitorización superficial (quejas
recurrentes). Dokploy dual-license con multi-server/compose/schedules propietarios.
Easypanel core cerrado y features tras muros de pago por servidor. CapRover sin 2FA ni
multiusuario gratis, backups parciales. Dokku sin panel gratuito (Pro $849). El segmento
clásico sin Docker: Ploi y RunCloud son SaaS (RunCloud además mantiene un agente con
puerto abierto permanente), CloudPanel no tiene git deploy nativo ni API ni streaming de
logs, GridPane es WordPress-only. **Nadie del segmento sin Docker tiene logs en vivo ni
terminal; Ploi lo rechazó explícitamente.**

## La conclusión

1. **El nicho de WASM está genuinamente vacío.** "Self-hosted + gratuito + sin Docker +
   git deploy + API + panel moderno" no lo cubre nadie. Los PaaS modernos exigen Docker;
   los gestores clásicos sin Docker son SaaS, cerrados o cojos. WASM no necesita ganar a
   Coolify en su terreno; necesita ser el Coolify del terreno donde Coolify no juega.

2. **El gap no está donde parecía.** La lectura "la web está al 5%" es errónea en el
   diagnóstico aunque acertada en la sensación: la arquitectura del panel (htmx+SSE+
   sistema de diseño+auth) es correcta y competitiva; lo que falta es **superficie**
   (el panel expone ~30% del núcleo) y **observabilidad** (cero gráficas, cero historia).
   No hay que rehacer el panel: hay que terminarlo.

3. **Tres carencias son table stakes absolutas** — sin ellas no hay comparación posible
   con nadie: (a) historial de despliegues con logs de build capturados y rollback en un
   clic; (b) métricas en vivo e históricas, por sistema y por app; (c) edición real desde
   el panel (env vars, config, servicios, sites, BD completa, backups programados).

4. **En seguridad, WASM ya supera al segmento clásico y a CapRover;** para igualar a
   Coolify/Dokploy faltan exactamente tres piezas: 2FA TOTP, tokens API con scopes y
   expiración, y gestión visible de sesiones. Más cerrar M-1/M-2/M-4/B-1.

5. **Hay ventajas netas que explotar, no solo gaps que cerrar:** logs en vivo reales
   (nadie sin Docker los tiene), systemd nativo (los "queue workers/daemons" que el
   segmento clásico vende como feature son nuestro `service create` con UI), cero
   CDN/build (auditable, empaquetable en distro), auditoría append-only ya operativa, y
   una identidad visual propia (instrumento, no SaaS genérico) que ningún competidor tiene.

## Decisiones

### D1. El panel es el producto
Prioridad absoluta: exponer el núcleo existente antes de construir features nuevas.
Cada página nueva es una traducción HTTP→manager ya probado (regla 3 del proyecto), no
lógica nueva. Orden: seguridad → gestión → observabilidad → producto.

### D2. Cero dependencias de runtime nuevas
Todo lo nuevo se hace con stdlib o assets vendorizados con checksum:
- **2FA TOTP**: RFC 6238 con `hmac`+`struct`+`base64` de stdlib (~60 líneas). El QR de
  enrolamiento se genera en cliente con `qrcodegen` de Nayuki (MIT, un fichero JS)
  vendorizado; fallback siempre visible de clave base32 manual + URI `otpauth://`.
- **Tokens API**: aleatorios de alta entropía (`secrets.token_urlsafe(32)`), almacenados
  como `sha256(token + signing_key)` igual que el master token. No necesitan KDF lento.
- **Notificaciones**: `urllib.request` de stdlib con timeout obligatorio (HTTP saliente
  no es un proceso: no viola la regla del runner; se añade excepción documentada en
  `test_architecture.py` para el módulo notifier únicamente).
- **Series temporales**: SQLite (ya presente), sin TSDB externa.

### D3. Observabilidad: colector único + RRD en SQLite + SSE
Un solo hilo colector en el proceso web muestrea cada 2 s (psutil para sistema; cgroup v2
por app leyendo `/sys/fs/cgroup/system.slice/wasm-*.service/{cpu.stat,memory.current}`
directamente del filesystem, sin subprocess). Persistencia tipo RRD en
`/var/lib/wasm/metrics.db` con tres niveles: crudo 2s→1h, medias 1min→24h, medias
1h→30d; consolidación periódica con DELETE+INSERT agregado. Difusión por el SSE
existente `/events` con eventos con nombre (patrón oficial htmx `sse-swap` múltiple
sobre una sola conexión). uPlot pasa a usarse de verdad (dashboard y página de app) con
ventana deslizante en cliente; si no, se desvendoriza. El machine strip deja el polling
de 5s y pasa a SSE, y sus contadores de units se calculan de verdad (bug actual).

### D4. Historial de despliegues como entidad de primera clase
Migración v2 del store: tabla `deployments` (domain, estado, trigger, commit/branch,
started/finished, duración, ruta del log de build capturado a fichero). Los jobs de
deploy/update/rollback escriben en ella. Página de historial por app con log verbatim de
cada deploy y botón de rollback (sobre `RollbackManager` existente). El favicon refleja
el peor estado activo (patrón Vercel).

### D5. Auto-deploy por webhook git
`POST /hooks/deploy/{domain}` sin sesión, autenticado por firma HMAC-SHA256 con secret
por app (generado, mostrado una vez, regenerable desde la UI). Se aceptan los formatos
de firma de GitHub (`X-Hub-Signature-256`), GitLab (`X-Gitlab-Token`) y Gitea. Filtro
por rama. Rate-limited y auditado. Dispara el job `update` existente.

### D6. Notificaciones multi-canal
Módulo `core/notifier.py` con canales: webhook genérico, Slack, Discord, Telegram
(todos POST JSON vía urllib) y email (reutiliza `EmailNotifier`). Eventos: deploy
ok/fail, certificado por caducar, unit en failed, disco sobre umbral, backup fallido.
Config en `config.yaml` (`notifications.*`), UI de configuración con botón de prueba
por canal. El monitor y el gestor de jobs publican al mismo módulo (una implementación).

### D7. Auth: 2FA + tokens con scopes + sesiones visibles
- TOTP opcional (D2). Al activarlo, login = master token + código. Códigos de respaldo
  de un solo uso. Lockout ya existente aplica también al segundo factor.
- Tokens API con scopes `read` / `deploy` / `admin`, expiración opcional, hasheados,
  revocables individualmente, con UI de gestión y auditoría de uso.
- Página de sesiones activas (IP, creada, última actividad, actual) con revocación
  individual además del "revoke all" existente.
- La CSP no se relaja. Nada de esto introduce JS nuevo que la viole.

### D8. TLS por defecto fuera de loopback
Bind no-loopback sin TLS pasa a exigir opt-out explícito (`--insecure-http`), incluso
con `--allow-ip` (cierra M-1). Nueva opción `--self-signed` genera certificado con
`openssl req` vía CommandRunner para paneles LAN-only, con aviso persistente en la UI.
Las claves `web.*` de config.yaml se cablean de verdad a `SecurityConfig` (cierra M-2).

### D9. E2E de navegador en CI
`panel_browser_check.py` se integra como job de CI (Playwright/Chromium, no bloqueante
las dos primeras semanas, bloqueante después). Factory compartida de datos de panel
para tests de contrato, API y navegador. Sin esto, cada página nueva del plan repite la
clase de bug que ya costó tres releases.

### D10. Diferido con razón (no es "no")
- **Terminal web interactiva**: shell root desde el navegador exige diseño propio de
  scopes/confirmación/grabación de sesión. Después de D7, no antes. El segmento sin
  Docker no la tiene; no bloquea la paridad.
- **Preview deployments por PR**: en un único host compiten por los recursos de
  producción. Requiere diseño de cuotas. Coolify tardó años; no es table stakes.
- **Equipos/roles multi-usuario**: fuera del alcance v1 (decisión previa mantenida).
  D7 deja la base (tokens con scopes) para cuando llegue.
- **One-click templates**: el catálogo de los competidores es de apps Docker; el nicho
  de WASM es otro. Se reevalúa cuando el deployer docker-compose madure.
- **Multi-servidor**: cambia la arquitectura entera. No.

### D11. Versionado del plan
v1.3.0 = Fase 0+1 (andamiaje + seguridad). v1.4.0 = Fase 2 (gestión). v1.5.0 = Fase 3
(observabilidad). v1.6.0 = Fase 4 (producto: webhooks, notificaciones, cron UI).
Fase 5 (pulido UX) se reparte como cierre de cada release. Cada fase es publicable por
sí sola; ninguna deja el producto en estado intermedio roto.

## Criterios de éxito

Al completar el plan, la columna WASM de la matriz de arriba queda: historial+rollback ✓,
webhook git ✓, métricas ✓, logs ✓ (ya), env vars ✓, BD UI ✓, backups UI ✓,
notificaciones ✓, 2FA ✓, tokens ✓, cron UI ✓, terminal diferido consciente. Todo con:
2959+ tests en verde, ruff/mypy/contratos/E2E en CI, cero dependencias nuevas de
runtime, cero CDN, CSP intacta, y las cuatro reglas del proyecto sin excepciones nuevas
(salvo la del notifier, documentada y acotada).
