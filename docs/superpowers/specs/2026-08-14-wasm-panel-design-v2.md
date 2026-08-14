# Panel WASM - Dirección de diseño v2

Fecha: 2026-08-14
Sustituye a: `2026-08-12-wasm-panel-design-direction.md` (el dueño rechazó esa dirección
tras verla en producción: "pobre, asimétrica, componentes horribles"). Las restricciones
técnicas de aquel documento siguen vigentes; la estética no.

## El veredicto que origina esto

La v1 apostó por un instrumento austero: todo acromático, cero acento, tipografía
condensada, raíles de 3px. Intelectualmente coherente; en pantalla lee como wireframe.
El dueño construye una alternativa a Vercel/Coolify y exige que el panel aguante el
careo visual con esos productos. Esa es la nueva vara.

## Dirección

**SaaS profesional de 2026**: neutros fríos limpios, un acento índigo con propósito,
tipografía Geist, tarjetas con elevación sutil, simetría por esqueleto común. El modo
oscuro fija la identidad (es donde vive un operador); el claro es igual de completo.

Lo que se conserva de la v1 porque es correcto: **el color semántico de estado**
(verde=corriendo, ámbar=en curso, rojo=fallo, gris=parado) como única fuente de color
además del acento; los errores del sistema verbatim en mono; los vacíos como invitación;
todo vendorizado, cero CDN, cero build step, CSP intacta.

## Tokens

### Color

El acento vive en el espacio azul-violeta, que el sistema de estados deja libre.

| Rol | Claro | Oscuro |
|---|---|---|
| Fondo | `#FAFAFA` | `#0B0C0E` |
| Fondo hundido | `#F4F4F5` | `#08090A` |
| Superficie | `#FFFFFF` | `#141519` |
| Superficie elevada | `#FFFFFF` + sombra | `#1A1C21` |
| Borde | `#E4E4E7` | `#26282E` |
| Borde fuerte | `#D4D4D8` | `#363940` |
| Texto | `#18181B` | `#EDEEF0` |
| Texto atenuado | `#52525B` | `#A1A5AD` |
| Texto débil | `#71717A` | `#7D828B` |
| **Acento** | `#5B5BD6` | `#7C7CF0` |
| Acento hover | `#4F4FC4` | `#8F8FF5` |
| Acento tenue (fondos) | `#EEEEFB` | `#232345` |
| Activo | `#16A34A` | `#4ADE80` |
| Activo fondo | `#EAF7EF` | `#132E1D` |
| Fallo | `#DC2626` | `#F87171` |
| Fallo fondo | `#FDECEC` | `#341518` |
| En curso | `#D97706` | `#FBBF24` |
| En curso fondo | `#FDF3E4` | `#33270F` |
| Parado | `#71717A` | `#8B8F98` |
| Parado fondo | `#F1F1F2` | `#222329` |

Todos los pares texto/fondo y estado/fondo cumplen AA; el contrato de estilo lo
verifica computándolo, así que cualquier ajuste posterior queda vigilado.

### Tipografía: Geist, dos voces

- **Geist Sans** (400/500/600) para todo el UI. Tracking ligeramente negativo en
  títulos (−0.02em), pesos 600 para títulos, 500 para labels/botones, 400 para prosa.
- **Geist Mono** (400/500) para todo valor del sistema: rutas, units, puertos, logs,
  timestamps, commits. Cifras tabulares en todo valor que cambie.
- Se retira IBM Plex entero, incluida la condensada (era la principal fuente de la
  sensación "apretada"). Vendor: 5 woff2 de fontsource con checksum.

### Espaciado, radios, elevación

- Escala: 4/8/12/16/24/32/48/64. Radio: 6px (controles), 10px (tarjetas), 999px
  (pills). Contenedor de página: max-width 1200px centrado, gutter 24px (16px móvil).
- Sombras (solo claro; el oscuro eleva por color de superficie):
  `--shadow-xs: 0 1px 2px rgb(0 0 0 / 5%)`,
  `--shadow-sm: 0 1px 3px rgb(0 0 0 / 7%), 0 1px 2px rgb(0 0 0 / 5%)`.
- Transiciones 140ms ease-out en hover/focus; `prefers-reduced-motion` respetado.

## Esqueleto común (la cura de la asimetría)

Toda página usa el mismo armazón, sin excepciones:

```
┌─ cockpit bar (sticky, blur) ─────────────────────────────┐
├─ sidebar ─┬─ page ────────────────────────────────────────┤
│           │  header: h1 + descripción      [acción 1ª]   │
│           │  ── secciones con el mismo gap vertical ──    │
│           │  card / tabla / form                          │
└───────────┴───────────────────────────────────────────────┘
```

- Cabecera de página: título (xl/600) + una línea de descripción atenuada a la
  izquierda; la acción primaria de la página a la derecha. Siempre.
- Secciones: título de sección (base/600) + gap uniforme (32px entre secciones,
  16px título→contenido). Nada de espaciados ad-hoc por plantilla.
- Tarjetas: superficie + borde + radio 10 + padding 20/24; en claro además sombra xs.
- Tablas: cabecera en texto atenuado 500 (12px, sin condensada, sin mayúsculas
  forzadas salvo eyebrows), filas 44px, hover superficie, divisores internos solo
  horizontales, celdas numéricas alineadas a la derecha en tabular.
- Estado: pill con punto de color (`● Running`) sobre fondo tenue del estado.
  El raíl de 3px desaparece.

## Componentes

- **Botones**: primario (acento, texto blanco, sombra xs), secundario (superficie +
  borde), fantasma (sin borde, hover superficie), peligro (rojo solo como primario de
  confirmación destructiva). Alturas 32/36, padding 12/16, radio 6, peso 500.
- **Formularios**: campos 36px, borde 1px, focus ring de 3px en acento tenue + borde
  acento; labels 500 encima; hints atenuados debajo; errores en rojo con el mensaje
  del sistema verbatim en mono dentro de un bloque, no inline chillón.
- **Cockpit bar** (la firma): sticky, fondo translúcido con `backdrop-filter: blur`,
  hostname en mono + pill de estado global + sparkline de carga + memoria/disco como
  micro-medidores + accesos (⌘K, tema). Es la única pieza con licencia para ser
  distintiva; todo lo demás, disciplinado.
- **Sidebar**: grupos con eyebrow (11px/500/atenuado), item activo con fondo acento
  tenue + texto acento, counts como pills mínimas.
- **Gráficas uPlot**: línea acento para métricas neutras (CPU/red), estados solo
  cuando significan estado; ejes en Geist Mono 10px; marcas de deploy como líneas
  discontinuas de estado.
- **Drawer de logs**: cabecera con pestaña, fondo hundido, xterm con el tema nuevo.
- **Login**: tarjeta centrada, logo, un campo, botón primario — la primera impresión
  del producto, tratada como tal.

## Ejecución

Los nombres de clases y de variables CSS se conservan (los contratos de estilo y el
cliente vigilan por nombre); cambian los valores y el CSS de componentes. Los templates
solo se tocan para imponer el esqueleto común (cabeceras de página, secciones). Se
verifica con capturas reales en ambos temas revisadas por el implementador, más
`test_web_style_contract` (contraste AA recalculado) y `panel_browser_check` (22/22).
