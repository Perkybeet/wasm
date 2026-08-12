# Panel WASM - Dirección de diseño

Fecha: 2026-08-12
Aplica a: `src/wasm/web/` (backend de vistas y assets)

## El sujeto

Quien usa este panel administra su propio servidor Linux. Eligió no usar Vercel. Está
cómodo en una terminal y abre el panel porque quiere ver el estado de la máquina de un
vistazo, no porque quiera que le escondan la máquina.

El trabajo de la interfaz es uno solo: **enseñar el estado real de esta máquina y dejar
actuar sobre él sin tener que abrir una sesión SSH.**

De ahí sale todo lo demás. El material del sujeto son units de systemd, bloques de nginx,
logs de journald, fechas de caducidad de certificados, números de puerto, códigos de salida.
Su vernáculo es la salida de `systemctl status`. La interfaz se construye con ese material,
no a pesar de él.

## Lo que este panel NO es

No es una consola de nube. Imitar la estética SaaS neutra de Vercel sería a la vez el camino
más trillado y una mentira sobre lo que es el producto: aquí solo hay una máquina, es tuya, y
si la rompes se rompe.

Tampoco es una terminal nostálgica. Verde sobre negro es un disfraz, no un diseño.

## Tokens

### Color: el color solo significa estado

La regla que sostiene la identidad: **ningún elemento lleva color si no codifica estado.**
La navegación, las cabeceras, las tarjetas, los bordes y el texto son acromáticos. Si ves
color en la pantalla, es información: algo está corriendo, algo ha fallado, algo caduca
pronto.

Eso hace la pantalla escaneable y es exactamente lo contrario de un dashboard con degradados
decorativos.

| Rol | Claro | Oscuro | Significa |
|---|---|---|---|
| Fondo | `#FBFAF8` | `#12161A` | |
| Superficie | `#FFFFFF` | `#191E24` | |
| Borde | `#E4E1DC` | `#262D35` | |
| Texto | `#1A1D21` | `#E8EAED` | |
| Texto tenue | `#6B7178` | `#8A929B` | |
| Activo | `#1F7A4D` | `#3FA96E` | unidad corriendo, certificado válido |
| Detenido | `#6B7178` | `#8A929B` | parado a propósito, no es un problema |
| Fallo | `#B3261E` | `#E5534B` | unidad caída, despliegue fallido |
| En curso | `#9A6700` | `#D4A017` | desplegando, renovando, caduca pronto |

El modo claro es el que fija la identidad: un técnico denso, tipo hoja de datos, poco
frecuente en esta categoría. El oscuro es igual de completo, porque a las tres de la mañana
es el que se usa.

### Tipografía: IBM Plex, tres voces

Una sola familia, tres cortes, todos OFL y vendorizables en cinco ficheros woff2.

- **IBM Plex Sans** para interfaz y prosa.
- **IBM Plex Sans Condensed** para cabeceras de tabla, etiquetas y ejes. El corte condensado
  es la decisión distintiva: da densidad real y un aire de hoja de datos técnica que una
  grotesca neutra no consigue.
- **IBM Plex Mono** para todo valor que sea del sistema: rutas, nombres de unit, puertos,
  códigos de salida, marcas de tiempo, logs.

Plex no es a lo que se recurre por defecto en herramientas de desarrollo, que es Inter o
Geist. Tiene forma propia: terminaciones planas, `a` y `g` reconocibles, y fue diseñada
precisamente como tipografía de sistema para trabajo técnico.

**Cifras tabulares en todo valor que cambie.** Las columnas no bailan cuando se actualiza un
número. Es un detalle de instrumento que casi ningún panel acierta.

### Estructura: el raíl de estado

Los recursos no son tarjetas flotando. Son filas de una tabla densa, y cada fila lleva un
raíl vertical de 3px a la izquierda cuyo color es su estado.

Recorrer el borde izquierdo con la vista cuenta la salud de todo sin leer una palabra. El
mismo lenguaje sirve para aplicaciones, servicios, sitios, certificados y backups, así que se
aprende una vez.

Los marcadores numerados (01 / 02 / 03) no se usan: aquí nada es una secuencia. Lo único que
lleva numeración es el pipeline de despliegue, porque ahí el orden sí es información.

### El elemento firma: la banda de máquina

Una franja persistente en la parte superior, visible en todas las páginas, con el estado real
de esta máquina: hostname, uptime, carga con un sparkline en línea, memoria y disco como
barras finas de capacidad, y el recuento de units por estado. Todo en mono, cifras tabulares,
actualizándose por SSE.

No son tarjetas de estadística. Es una línea de estado de instrumento, como la de un equipo
de laboratorio. Y es lo que hace que se sienta una sala de control de una máquina concreta en
vez de un CRUD genérico. También es honesto: siempre sabes qué máquina estás a punto de
tocar.

### El riesgo: el cajón de logs anclado

Los logs viven en un cajón anclado abajo, persistente entre navegaciones, con streaming de
journald por WebSocket sobre xterm.js. Como el terminal integrado de un editor.

Cuesta espacio vertical, y esa es la apuesta. Se justifica porque el momento en que más
falta hacen los logs es en mitad de un despliegue, y porque esconderlos detrás de un clic por
recurso es justo lo que hace que un panel web se sienta peor que una sesión SSH.

## Movimiento

Casi ninguno. Esto es un instrumento.

1. Un pulso breve en el raíl cuando una unidad cambia de estado, porque un cambio de estado
   es el evento más importante que puede ocurrir.
2. El deslizamiento del cajón de logs.
3. Esqueleto a contenido.

`prefers-reduced-motion` se respeta en los tres.

## Texto

- Nombrar por lo que la persona controla: "Reiniciar", no "Enviar".
- La acción conserva su nombre en todo el flujo: el botón "Publicar" produce el aviso
  "Publicado".
- **Un error del sistema no se parafrasea nunca.** Se muestra literal, en mono, y encima se
  pone qué hacer. Este público quiere el mensaje real de nginx o de systemd, no una versión
  amable.
- Los vacíos son una invitación: "Todavía no hay aplicaciones. Despliega una desde aquí o con
  `wasm deploy`."

## Suelo de calidad

Responsive hasta móvil, foco de teclado visible, contraste AA como mínimo en texto y en los
colores de estado sobre su fondo, movimiento reducido respetado, y funcionamiento completo
sin conexión a internet: todos los assets vendorizados, cero CDN.
