# optimize_media.py

Compresor de imágenes y vídeos del sitio. Reemplaza cada archivo **en su misma
ruta**, así que no hay que tocar el HTML para que el sitio siga funcionando, y
además deja un hermano `.webp` donde ese formato pesa menos.

## Uso

```bash
python3 tools/optimize_media.py analyze     # qué hay, qué pesa, qué sobra
python3 tools/optimize_media.py images      # comprime imágenes
python3 tools/optimize_media.py videos      # comprime vídeos
python3 tools/optimize_media.py all
```

Opciones: `--dry-run`, `--only PATRON`, `--jobs N`, `--force`, `--webm`,
`--backup-dir DIR`, `--include-unused`, `--all`, `--yes`.

## Requisitos

| Para        | Necesitas | Cómo instalarlo                                     |
|-------------|-----------|-----------------------------------------------------|
| Imágenes    | Pillow    | `pip install Pillow`                                  |
| Vídeos      | ffmpeg    | `apt install ffmpeg` · `brew install ffmpeg` · `pip install imageio-ffmpeg` |

Si ffmpeg está en otra ruta: `FFMPEG_BIN=/ruta/a/ffmpeg python3 tools/optimize_media.py videos`.

## Cómo decide

**Tamaño.** Cada grupo de imágenes se reduce al tamaño con el que de verdad se
muestra, leído del CSS de `index.html`. La foto de un tanatólogo se pinta en un
círculo de `clamp(140px,17vw,190px)`, así que 420 px de lado (≈2× para pantallas
retina) sobra; venía a 800 px. Los perfiles viven en `IMAGE_PROFILES` /
`VIDEO_PROFILES` y cada uno dice de dónde sale su medida. **Si cambia el diseño,
hay que cambiarlos ahí.**

**Formato.** Para cada imagen se codifican varios candidatos —WebP a distintas
calidades, PNG con paleta de 64/128/256 colores, JPEG progresivo— y gana **el más
pequeño que supere un piso de calidad**, no simplemente el más pequeño.

**La calidad se mide sobre lo que se ve.** Comparar los cuatro canales RGBA en
crudo miente con imágenes transparentes: los valores RGB que quedan debajo de un
píxel de alpha 0 son arbitrarios y cualquier codificador los reescribe, de modo
que un WebP sin pérdida puede puntuar peor que un PNG con banding evidente.
`visible_psnr()` compone ambas imágenes sobre el fondo real de la página
(`#F7F9FD`) antes de compararlas, y promedia con el error del canal alpha.

**El respaldo admite 2 dB menos que el WebP.** El `.webp` lo recibe la enorme
mayoría de navegadores, así que mantiene el piso alto; el PNG/JPEG que conserva
la ruta original es para los pocos que no entienden WebP, y en PNG esos 2 dB
pueden ser la diferencia entre una paleta de 256 colores y un archivo cinco veces
más pesado.

**El `.webp` solo se escribe si de verdad pesa menos** que el respaldo elegido.
En logos planos un PNG de paleta suele ganarle a WebP; en esos casos no se genera
`.webp` y el HTML no debe declarar un `<source>` para ellos (ver más abajo).

## Salvaguardas

- **No toca lo que el sitio no referencia.** Esos suelen ser los maestros de
  diseño (`logo-src.png`, `logo-cruzada.png`) y copias sueltas en `uploads/`;
  recomprimirlos destruiría el original a cambio de cero bytes servidos.
  `--include-unused` lo fuerza.
- **Nunca escribe un resultado más grande** que el original (`--force` lo salta).
- **El contenedor siempre coincide con la extensión.** Escribir JPEG dentro de un
  `.png` es exactamente el defecto que este script vino a corregir: en este
  proyecto las 25 `foto-NN.jpg` y las 3 `poster-NN.jpg` eran PNG disfrazados de
  JPEG, y de ahí venía buena parte del peso.
- **Se niega a correr con cambios sin guardar en git**, porque reemplaza archivos
  en su sitio y git es lo que permite recuperar los originales (`--yes` lo salta,
  `--backup-dir` copia los vídeos antes de tocarlos).
- **`-nostdin` en todas las llamadas a ffmpeg.** ffmpeg lee la entrada estándar
  por omisión y, con varias codificaciones en paralelo, unas le roban a otras lo
  que llegue por ahí.

Cada corrida deja el detalle en `tools/media-manifest.json`.

## Resultado de la primera pasada

| | Antes | Después |
|---|---|---|
| Imágenes | 47.6 MB | 1.8 MB |
| Vídeos | 285.3 MB | 21.8 MB |
| **Total que sirve el sitio** | **333 MB** | **23.7 MB** (14×) |

Los 48 mp4 resultantes decodifican completos sin un solo error, y las pruebas
visuales a tamaño de pantalla no muestran diferencia.

## Lo que hay que saber al editar `index.html`

- El `<head>` real lo parsea el navegador: ahí los atributos van en **HTML plano**
  (`fetchpriority="high"`).
- Todo lo que está dentro de `<x-dc>` es plantilla y lo renderiza **React 18**, así
  que los props van en camelCase. `support.js` trae el prefijo `sc-camel-` para
  eso: `sc-camel-src-set="…"` se convierte en `srcSet`. Escribir `srcset` en
  minúsculas dispara el aviso *Invalid DOM property* en consola.
- **Un `<source>` que da 404 no cae al `<img>`**: rompe la imagen. Por eso solo hay
  `<picture>` en los grupos donde el script genera `.webp` para el 100% de los
  archivos (fotos de tanatólogos y carátulas de testimonios). Los 12 logos cuyo
  PNG de paleta le ganó a WebP se sirven como `<img>` a secas, que es lo correcto:
  ese PNG ya es el archivo más pequeño.
- **No pongas `poster=` en los vídeos de fondo.** Arrancan en `opacity:0` con
  `preload="none"`, pero el atributo `poster` sí se descarga de inmediato: sumaría
  peso a la carga inicial para una imagen que no se ve nunca.
