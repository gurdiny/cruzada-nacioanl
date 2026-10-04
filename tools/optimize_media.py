#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
optimize_media.py — Compresor de imágenes y vídeos para la Cruzada Nacional por la Higiene Emocional.

Reduce el peso de los medios del sitio sin cambiar sus rutas: cada archivo se
re-codifica en su mismo sitio y, además, se genera un hermano `.webp` para que
el HTML pueda servirlo con `<picture>` o detección en JS.

Ideas clave
-----------
1. Cada archivo se mide contra el tamaño real con el que se muestra en pantalla.
   Una foto que se pinta en un círculo de 190 px no necesita 800 px.
2. Para cada imagen se codifican varios candidatos (WebP a distintas calidades,
   PNG cuantizado a distintas paletas, JPEG progresivo) y se elige el más
   pequeño que supere un piso de calidad medido en PSNR *visible* — comparando
   lo que el ojo ve, es decir componiendo sobre el fondo de la página, no los
   valores RGB que quedan debajo de los píxeles transparentes.
3. Nunca se escribe un resultado más grande que el original.

Uso
---
    python3 tools/optimize_media.py analyze      # qué hay, qué pesa, qué sobra
    python3 tools/optimize_media.py images       # comprime imágenes
    python3 tools/optimize_media.py videos       # comprime vídeos (requiere ffmpeg)
    python3 tools/optimize_media.py all

Opciones útiles: --dry-run, --jobs N, --force, --only PATRON, --backup-dir DIR

Requisitos: Pillow (imágenes) y ffmpeg (vídeos).
    pip install Pillow
    ffmpeg: apt install ffmpeg   ·   o   pip install imageio-ffmpeg
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import dataclasses
import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

# --------------------------------------------------------------------------- #
# Rutas base
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent
HTML_FILES = ["index.html", "support.js"]

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}

# Color de fondo de la página: se usa para medir la calidad visible de las
# imágenes con transparencia (html,body{background:#F7F9FD}).
PAGE_BG = (0xF7, 0xF9, 0xFD)


# --------------------------------------------------------------------------- #
# Perfiles
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ImageProfile:
    """Cómo tratar un grupo de imágenes.

    max_px      lado mayor de salida, en píxeles (≈ 2× el tamaño CSS mostrado)
    fallback    formato del archivo que conserva la ruta original
    psnr_floor  calidad visible mínima del WebP, en dB (40 ≈ indistinguible)
    note        de dónde sale el max_px, para que se pueda auditar

    El respaldo (PNG/JPEG) admite 2 dB menos que el WebP a propósito: lo recibe
    una minoría de navegadores viejos, y en PNG la diferencia entre 38 y 40 dB
    puede ser la que separa una paleta de 256 colores de un PNG cinco veces
    más pesado. El formato que sirve a casi todo el mundo mantiene el piso alto.
    """

    max_px: int
    fallback: str  # "jpeg" | "png"
    psnr_floor: float = 38.0
    note: str = ""

    @property
    def fallback_floor(self) -> float:
        return self.psnr_floor - 2.0


@dataclasses.dataclass(frozen=True)
class VideoProfile:
    """Cómo tratar un grupo de vídeos.

    height      altura de salida (el ancho se ajusta manteniendo proporción)
    crf         calidad H.264: más bajo = mejor y más pesado
    audio       False elimina la pista de audio (vídeos decorativos y muteados)
    poster      True extrae un fotograma como imagen de portada
    """

    height: int
    crf: int
    audio: bool
    poster: bool = True
    note: str = ""


# Tamaños tomados del CSS real de index.html. Si cambia el diseño, cambian aquí.
IMAGE_PROFILES: dict[str, ImageProfile] = {
    "foto-tanatologo": ImageProfile(
        max_px=420, fallback="jpeg", psnr_floor=36,
        note="círculo clamp(120px,14vw,168px) → 168 CSS px @2.5x",
    ),
    "logo-badge": ImageProfile(
        max_px=256, fallback="png", psnr_floor=40,
        note="insignia clamp(52px,6vw,68px) → 68 CSS px @3.5x",
    ),
    "poster-testimonio": ImageProfile(
        max_px=440, fallback="jpeg", psnr_floor=36,
        note="botón clamp(150px,19vw,220px) → 220 CSS px @2x",
    ),
    "logo-hero": ImageProfile(
        max_px=1100, fallback="png", psnr_floor=40,
        note="logo principal clamp(280px,36vw,500px) → 500 CSS px @2.2x",
    ),
    "logo-aliado": ImageProfile(
        max_px=256, fallback="png", psnr_floor=40,
        note="nodo de la pantalla de aliados: clamp(70px,7vw,96px) → 96 CSS px @2.5x",
    ),
    "generic": ImageProfile(
        max_px=1280, fallback="jpeg", psnr_floor=38,
        note="sin regla específica: se aplica un techo prudente",
    ),
}

VIDEO_PROFILES: dict[str, VideoProfile] = {
    "fondo": VideoProfile(
        height=720, crf=25, audio=False, poster=False,
        # Sin portada a propósito: estos vídeos arrancan en opacity:0 y con
        # preload="none". Un atributo poster sí se descarga de inmediato, así
        # que generarlas sumaría peso a la carga inicial sin mostrarse nunca.
        note="vídeo decorativo a pantalla completa, muteado y bajo un velo",
    ),
    "testimonio": VideoProfile(
        height=720, crf=23, audio=True,
        note="se reproduce en una caja de max-width:min(880px,94vw)",
    ),
}

# La primera regla que coincide gana.
IMAGE_RULES: Sequence[tuple[str, str]] = (
    ("assets/tanatologos/foto-*", "foto-tanatologo"),
    ("assets/tanatologos/logo-*", "logo-badge"),
    ("assets/testimonios/poster-*", "poster-testimonio"),
    ("assets/logo-cruzada-t2.png", "logo-hero"),
    ("assets/tanatologo-placeholder.png", "foto-tanatologo"),
    ("assets/aliados/*", "logo-aliado"),
    ("assets/rotary-roma-norte.png", "logo-aliado"),
    ("assets/paso13-transparente.png", "logo-aliado"),
    ("uploads/*.png", "logo-aliado"),
    ("uploads/*.jpg", "generic"),
    ("uploads/*.jpeg", "generic"),
)

VIDEO_RULES: Sequence[tuple[str, str]] = (
    ("assets/testimonios/*", "testimonio"),
    ("uploads/*", "fondo"),
    ("uploads/higgsfield/*", "fondo"),
)


def match_profile(rel: str, rules: Sequence[tuple[str, str]]) -> str | None:
    for pattern, profile in rules:
        if fnmatch.fnmatch(rel, pattern):
            return profile
    return None


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def find_ffmpeg() -> str | None:
    """Localiza ffmpeg: variable de entorno, PATH, o el binario de imageio-ffmpeg."""
    env = os.environ.get("FFMPEG_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(exe).is_file():
            return exe
    except Exception:
        pass
    return None


FFMPEG_HELP = """
No se encontró ffmpeg, que es lo que comprime los vídeos. Instálalo con una de:

    sudo apt install ffmpeg                # Debian / Ubuntu / WSL
    brew install ffmpeg                    # macOS
    pip install imageio-ffmpeg             # binario propio, sin permisos de sistema

O apunta a un binario que ya tengas:

    FFMPEG_BIN=/ruta/a/ffmpeg python3 tools/optimize_media.py videos
""".strip()


def referenced_media(ambiguous: dict[str, list[str]] | None = None) -> set[str]:
    """Rutas de medios mencionadas en el HTML/JS, incluyendo las que se arman
    por concatenación en JavaScript (`'assets/tanatologos/foto-'+nn+'.jpg'`).

    El análisis es estático, así que para los nombres sueltos (`{file:'V_01.mp4'}`
    unido a un prefijo en otra línea) hay que adivinar la carpeta. Se toma el
    prefijo más específico que contenga el archivo y, si el mismo nombre existe
    bajo varios prefijos, se anota en `ambiguous` para poder revisarlo a mano.
    """
    blobs = []
    for name in HTML_FILES:
        path = ROOT / name
        if path.is_file():
            blobs.append(path.read_text(encoding="utf-8", errors="replace"))
    text = "\n".join(blobs)

    refs: set[str] = set()

    # Rutas literales completas.
    literal = re.compile(
        r"""(?:assets|uploads)/[^\s"'()<>{}]+?\.(?:png|jpe?g|webp|avif|gif|svg|mp4|webm|mov)""",
        re.IGNORECASE,
    )
    refs.update(m.group(0) for m in literal.finditer(text))

    # Nombres de archivo sueltos en listas JS, p. ej. ['jardin','01-jardin__v01.mp4'].
    # Los prefijos van de más específico a más general: gana el primero que
    # contenga el archivo, porque una carpeta anidada es la pista más fuerte.
    bare = re.compile(r"""['"]([^'"/\\]+\.(?:mp4|webm|mov|png|jpe?g|webp))['"]""", re.IGNORECASE)
    prefixes = sorted(
        {m.group(1) for m in re.finditer(
            r"""['"]((?:assets|uploads)/(?:[A-Za-z0-9_-]+/)*)['"]\s*\+""", text)}
        | {"uploads/", "uploads/higgsfield/", "assets/", "assets/testimonios/"},
        key=lambda s: -s.count("/"),
    )
    for name in {m.group(1) for m in bare.finditer(text)}:
        hits = [pre for pre in prefixes if (ROOT / pre / name).is_file()]
        if not hits:
            continue
        refs.add(hits[0] + name)
        if len(hits) > 1 and ambiguous is not None:
            ambiguous[name] = [h + name for h in hits]

    # Prefijos de concatenación: 'assets/tanatologos/foto-' + nn + '.jpg'
    concat = re.compile(
        r"""['"]((?:assets|uploads)/[A-Za-z0-9_/-]*?)['"]\s*\+\s*\w+\s*\+\s*['"](\.[a-z0-9]+)['"]""",
        re.IGNORECASE,
    )
    for m in concat.finditer(text):
        prefix, ext = m.group(1), m.group(2)
        parent = (ROOT / prefix).parent
        stem = Path(prefix).name
        if parent.is_dir():
            for f in parent.iterdir():
                if f.name.startswith(stem) and f.suffix.lower() == ext.lower():
                    refs.add(str(f.relative_to(ROOT)))

    # Carpetas citadas con archivos armados dinámicamente (uploads/higgsfield/).
    for m in re.finditer(r"""['"]((?:assets|uploads)/[A-Za-z0-9_/-]+/)['"]""", text):
        d = ROOT / m.group(1)
        if d.is_dir():
            for f in d.iterdir():
                if f.suffix.lower() in IMAGE_EXT | VIDEO_EXT:
                    refs.add(str(f.relative_to(ROOT)))

    return refs


def walk_media(kinds: set[str]) -> list[Path]:
    out = []
    for base in ("assets", "uploads"):
        d = ROOT / base
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in kinds:
                out.append(p)
    return out


def git_is_clean() -> bool | None:
    """True si el árbol de trabajo está limpio, None si no se puede saber."""
    try:
        r = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() == ""


# --------------------------------------------------------------------------- #
# Calidad visible
# --------------------------------------------------------------------------- #


def visible_psnr(a, b) -> float:
    """PSNR sobre lo que realmente se ve.

    Comparar RGBA en crudo miente con imágenes transparentes: los valores RGB
    que quedan bajo un píxel de alpha 0 son arbitrarios y cualquier codificador
    los reescribe, así que un WebP sin pérdida puede puntuar peor que un PNG
    con banding evidente. Aquí se componen ambas imágenes sobre el fondo de la
    página y se promedia con el error del canal alpha.
    """
    from PIL import Image, ImageChops

    a = a.convert("RGBA")
    b = b.convert("RGBA")
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)

    def mse(x, y) -> float:
        d = ImageChops.difference(x, y).convert("L")
        hist = d.histogram()
        total = sum(hist) or 1
        return sum(i * i * c for i, c in enumerate(hist)) / total

    flat_a = Image.new("RGB", a.size, PAGE_BG); flat_a.paste(a, (0, 0), a)
    flat_b = Image.new("RGB", b.size, PAGE_BG); flat_b.paste(b, (0, 0), b)

    err = (mse(flat_a, flat_b) + mse(a.getchannel("A"), b.getchannel("A"))) / 2.0
    return 99.0 if err < 1e-9 else 10.0 * math.log10(255.0 * 255.0 / err)


def has_real_alpha(im) -> bool:
    """True si la transparencia se usa de verdad (no un canal alpha opaco)."""
    if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
        return False
    conv = im.convert("RGBA")
    lo, _hi = conv.getchannel("A").getextrema()
    return lo < 255


# --------------------------------------------------------------------------- #
# Imágenes
# --------------------------------------------------------------------------- #


def optimize_image(path: Path, profile: ImageProfile, *, dry_run: bool, force: bool) -> dict:
    from PIL import Image, ImageOps

    rel = str(path.relative_to(ROOT))
    before = path.stat().st_size
    result: dict = {
        "file": rel, "kind": "image", "profile_note": profile.note,
        "bytes_before": before, "bytes_after": before, "webp_bytes": None,
        "status": "skipped", "detail": "",
    }

    try:
        src = Image.open(path)
        src.load()
    except Exception as exc:
        result["status"] = "error"
        result["detail"] = f"no se pudo abrir: {exc}"
        return result

    real_format = (src.format or "?").upper()
    src = ImageOps.exif_transpose(src)  # respeta la orientación de la cámara
    orig_size = src.size
    alpha = has_real_alpha(src)

    # La extensión puede mentir: en este proyecto varias `foto-NN.jpg` son PNG.
    mislabelled = (
        real_format == "PNG" and path.suffix.lower() in (".jpg", ".jpeg")
    )

    # Redimensionar al tamaño con el que se muestra.
    work = src.convert("RGBA") if alpha else src.convert("RGB")
    if max(work.size) > profile.max_px:
        work.thumbnail((profile.max_px, profile.max_px), Image.LANCZOS)

    # El respaldo conserva la ruta original, así que su contenedor tiene que
    # coincidir con la extensión del archivo: escribir JPEG dentro de un .png
    # reproduce exactamente el defecto que este script viene a corregir.
    # Y si la imagen usa transparencia de verdad, JPEG queda descartado.
    ext_format = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}.get(path.suffix.lower())
    fallback = "png" if alpha else (ext_format or profile.fallback)

    Candidate = tuple[str, bytes, float]  # (etiqueta, datos, psnr visible)

    def encode(target: list, label: str, fmt: str, **kw) -> None:
        """Codifica un candidato en memoria y lo anota con su calidad visible."""
        import io

        img = work
        if fmt == "JPEG" and img.mode != "RGB":
            flat = Image.new("RGB", img.size, PAGE_BG)
            flat.paste(img, (0, 0), img if img.mode == "RGBA" else None)
            img = flat
        buf = io.BytesIO()
        try:
            img.save(buf, fmt, **kw)
        except Exception:
            return
        data = buf.getvalue()
        buf.seek(0)
        try:
            psnr = visible_psnr(work, Image.open(buf))
        except Exception:
            psnr = 0.0
        target.append((label, data, psnr))

    # --- candidatos de respaldo: conservan la ruta y extensión originales ---
    backs: list[Candidate] = []
    if fallback == "jpeg":
        for q in (78, 82, 88):
            encode(backs, f"jpeg-q{q}", "JPEG", quality=q, optimize=True, progressive=True)
    else:
        # Cuantizar a paleta suele ganarle por mucho al PNG de color completo;
        # el piso de PSNR descarta las paletas que producen banding visible.
        for colors in (64, 128, 256):
            pal = work.quantize(colors=colors, method=Image.FASTOCTREE,
                                dither=Image.FLOYDSTEINBERG)
            import io

            buf = io.BytesIO()
            pal.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            buf.seek(0)
            try:
                psnr = visible_psnr(work, Image.open(buf))
            except Exception:
                psnr = 0.0
            backs.append((f"png-quant{colors}", data, psnr))
        encode(backs, "png-full", "PNG", optimize=True)

    fallback_pick = pick_candidate(backs, profile.fallback_floor)

    # --- candidato WebP: hermano .webp, para servirlo con <picture> ---
    webp: list[Candidate] = []
    for q in (72, 80, 88):
        encode(webp, f"webp-q{q}", "WEBP", quality=q, method=6)
    encode(webp, "webp-lossless", "WEBP", lossless=True, method=6)
    webp_pick = pick_candidate(webp, profile.psnr_floor)

    notes = []
    if mislabelled:
        notes.append(f"era {real_format} con extensión {path.suffix}")
    if orig_size != work.size:
        notes.append(f"{orig_size[0]}x{orig_size[1]}→{work.size[0]}x{work.size[1]}")
    if alpha:
        notes.append("alpha")
    elif src.mode in ("RGBA", "LA", "PA"):
        notes.append("alpha inútil eliminado")

    # --- escribir ---
    wrote = []
    if fallback_pick and (len(fallback_pick[1]) < before or force):
        result["bytes_after"] = len(fallback_pick[1])
        notes.append(f"{fallback_pick[0]} @{fallback_pick[2]:.0f}dB")
        if not dry_run:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(fallback_pick[1])
            tmp.replace(path)
        wrote.append(path.name)
    else:
        notes.append("respaldo ya óptimo")

    # El WebP solo vale la pena si de verdad pesa menos que el respaldo elegido:
    # en logos planos un PNG de paleta puede ganarle, y entonces un <picture>
    # con ese WebP serviría el archivo más grande a los navegadores modernos.
    wp = path.with_suffix(".webp")
    fallback_bytes = len(fallback_pick[1]) if fallback_pick else before
    if webp_pick and len(webp_pick[1]) < fallback_bytes * 0.95:
        result["webp_bytes"] = len(webp_pick[1])
        notes.append(f"webp {webp_pick[0].replace('webp-', '')} @{webp_pick[2]:.0f}dB")
        if not dry_run:
            wp.write_bytes(webp_pick[1])
        wrote.append(wp.name)
    else:
        if webp_pick:
            notes.append(f"sin webp ({human(len(webp_pick[1]))} ≥ respaldo)")
        if not dry_run and wp.is_file():
            wp.unlink()  # no dejar un .webp viejo y peor que el respaldo

    result["status"] = "ok" if wrote else "skipped"
    result["detail"] = ", ".join(notes)
    return result


def pick_candidate(
    candidates: list[tuple[str, bytes, float]], floor: float
) -> tuple[str, bytes, float] | None:
    """El candidato más pequeño que alcanza el piso de calidad.

    Si ninguno llega al piso, gana el de mejor calidad: vale más un archivo
    algo mayor que uno visiblemente degradado.
    """
    if not candidates:
        return None
    good = [c for c in candidates if c[2] >= floor]
    if good:
        return min(good, key=lambda c: len(c[1]))
    return max(candidates, key=lambda c: c[2])


# --------------------------------------------------------------------------- #
# Vídeos
# --------------------------------------------------------------------------- #


def optimize_video(
    path: Path, profile: VideoProfile, ffmpeg: str, *,
    dry_run: bool, force: bool, webm: bool, backup_dir: Path | None,
) -> dict:
    rel = str(path.relative_to(ROOT))
    before = path.stat().st_size
    result: dict = {
        "file": rel, "kind": "video", "profile_note": profile.note,
        "bytes_before": before, "bytes_after": before, "webm_bytes": None,
        "status": "skipped", "detail": "",
    }

    out = path.with_suffix(".opt.mp4")
    cmd = [
        # -nostdin: ffmpeg lee la entrada estándar por omisión y, con varias
        # codificaciones en paralelo, unas le roban a otras lo que llegue por
        # ahí. Sin esto el lote se corrompe de formas difíciles de rastrear.
        ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
        # -2 mantiene la proporción y fuerza un ancho par (lo exige yuv420p).
        "-vf", f"scale=-2:'min({profile.height},ih)':flags=lanczos",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(profile.crf),
        "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
        "-g", "60",                     # keyframes regulares: mejor seek/loop
        "-movflags", "+faststart",      # metadatos al inicio: empieza a pintar antes
    ]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2"] if profile.audio else ["-an"]
    cmd += [str(out)]

    if dry_run:
        result["detail"] = f"h≤{profile.height} crf{profile.crf} " + (
            "con audio" if profile.audio else "sin audio"
        )
        return result

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        result["status"] = "error"
        result["detail"] = (proc.stderr or "ffmpeg falló").strip().splitlines()[-1:][0][:200]
        return result

    after = out.stat().st_size
    notes = [f"h≤{profile.height} crf{profile.crf}", f"{time.time() - t0:.0f}s"]
    if not profile.audio:
        notes.append("audio eliminado")

    if after < before or force:
        if backup_dir is not None:
            dest = backup_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        out.replace(path)
        result["bytes_after"] = after
        result["status"] = "ok"
    else:
        out.unlink(missing_ok=True)
        notes.append("el original ya era más pequeño")
        result["status"] = "skipped"

    if profile.poster:
        poster = path.with_name(path.stem + "-poster.jpg")
        subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.5",
             "-i", str(path), "-frames:v", "1",
             "-vf", "scale=-2:'min(720,ih)'", "-q:v", "6", str(poster)],
            capture_output=True, stdin=subprocess.DEVNULL,
        )
        if poster.is_file():
            notes.append(f"portada {poster.name}")

    if webm:
        wout = path.with_suffix(".webm")
        wcmd = [
            ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-c:v", "libvpx-vp9", "-crf", str(profile.crf + 6), "-b:v", "0",
            "-row-mt", "1", "-deadline", "good", "-cpu-used", "2",
        ]
        wcmd += ["-c:a", "libopus", "-b:a", "80k"] if profile.audio else ["-an"]
        wcmd += [str(wout)]
        if subprocess.run(wcmd, capture_output=True, stdin=subprocess.DEVNULL).returncode == 0 and wout.is_file():
            result["webm_bytes"] = wout.stat().st_size
            notes.append(f"webm {human(wout.stat().st_size)}")

    result["detail"] = ", ".join(notes)
    return result


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def cmd_analyze(args) -> int:
    ambiguous: dict[str, list[str]] = {}
    refs = referenced_media(ambiguous)
    images = walk_media(IMAGE_EXT)
    videos = walk_media(VIDEO_EXT)

    print(f"\nProyecto: {ROOT}")
    print(f"Referencias encontradas en {', '.join(HTML_FILES)}: {len(refs)}\n")

    used_bytes = unused_bytes = 0
    unused: list[tuple[int, str]] = []
    rows: list[tuple[int, str, str, str]] = []

    for p in images + videos:
        rel = str(p.relative_to(ROOT))
        size = p.stat().st_size
        is_video = p.suffix.lower() in VIDEO_EXT
        prof = match_profile(rel, VIDEO_RULES if is_video else IMAGE_RULES) or "—"
        if rel in refs:
            used_bytes += size
            rows.append((size, rel, "vídeo" if is_video else "imagen", prof))
        else:
            unused_bytes += size
            unused.append((size, rel))

    print("EN USO — los 25 más pesados")
    print(f"  {'peso':>10}  {'tipo':<7} {'perfil':<18} archivo")
    for size, rel, kind, prof in sorted(rows, reverse=True)[:25]:
        print(f"  {human(size):>10}  {kind:<7} {prof:<18} {rel}")

    print(f"\n  Subtotal en uso ...... {human(used_bytes)}  ({len(rows)} archivos)")

    if unused:
        print(f"\nSIN REFERENCIA — {len(unused)} archivos, {human(unused_bytes)}")
        print("  Este script no los borra. Revísalos y elimina los que sobren:")
        listed = sorted(unused, reverse=True)
        shown = listed if args.all else listed[:15]
        for size, rel in shown:
            print(f"  {human(size):>10}  {rel}")
        if len(listed) > len(shown):
            print(f"  … y {len(listed) - len(shown)} más (añade --all para verlos todos)")

    if ambiguous:
        print(f"\nAMBIGUOS — {len(ambiguous)} nombres existen en más de una carpeta.")
        print("  El análisis eligió el primero; confirma a mano cuál sirve el sitio:")
        for name, options in sorted(ambiguous.items()):
            print(f"  {name}")
            for i, opt in enumerate(options):
                print(f"      {'→ elegido' if i == 0 else '  ignorado'}  {opt}")

    print(f"\n  TOTAL en assets/ + uploads/ ... {human(used_bytes + unused_bytes)}\n")
    return 0


def select_paths(paths: list[Path], args, skip_suffixes: set[str] | None = None) -> list[Path]:
    """Filtra qué archivos se tocan.

    Por omisión se saltan los que el sitio no referencia: en este proyecto esos
    suelen ser los maestros de diseño (logo-src.png, logo-cruzada.png) y copias
    sueltas en uploads/. Recomprimirlos en su sitio destruiría el original a
    cambio de cero bytes servidos al visitante.
    """
    refs = referenced_media()
    out, skipped_unused = [], 0
    for p in paths:
        rel = str(p.relative_to(ROOT))
        if skip_suffixes and p.suffix.lower() in skip_suffixes:
            continue
        if args.only and not fnmatch.fnmatch(rel, args.only):
            continue
        if not args.include_unused and rel not in refs:
            skipped_unused += 1
            continue
        out.append(p)
    if skipped_unused:
        print(
            f"  ({skipped_unused} archivos sin referencia quedan intactos; "
            f"usa --include-unused para incluirlos, o 'analyze' para listarlos)"
        )
    return out


# Cosas que nunca deben servirse como asset estático, pase lo que pase.
# Pages las excluía sola; Workers no, y por eso un repo con .git de 200 MB
# revienta el deploy con "Asset too large" (el límite es 25 MiB por archivo).
NEVER_DEPLOY = (
    ".git", ".github", ".wrangler", "node_modules", ".DS_Store",
    "tools", "*.md", ".gitignore", ".assetsignore", "wrangler.jsonc",
    # Copias de referencia de los exports (cientos de MB). Son material de
    # trabajo, no del sitio: publicarlas duplicaría todos los medios sin
    # comprimir en una URL pública.
    "Cruzada Nacional Higiene Emocional/",
    "cruzada-new/",
    "comprimir.py", "comprimir-informe.json",
)


def derived_siblings(refs: set[str]) -> set[str]:
    r"""Rutas que el sitio arma en tiempo de ejecución, no en el código fuente.

    index.html construye algunas rutas con .replace() sobre otra ruta:

        tanaFotoWebp: …foto.replace(/\.(jpe?g|png)$/i, '.webp')
        tsVidPoster : …video.replace(/\.mp4$/i, '-poster.jpg')

    Un análisis estático del texto nunca las ve, así que hay que derivarlas
    igual que hace el navegador. Sin esto, .assetsignore las toma por
    archivos muertos y los excluye del deploy: el <source> de cada <picture>
    da 404 y la imagen sale rota, porque un <source> que falla NO cae al
    <img> de respaldo.
    """
    out: set[str] = set()
    for r in refs:
        low = r.lower()
        if low.endswith((".jpg", ".jpeg", ".png")):
            out.add(re.sub(r"\.(jpe?g|png)$", ".webp", r, flags=re.IGNORECASE))
        elif low.endswith(".mp4"):
            out.add(re.sub(r"\.mp4$", "-poster.jpg", r, flags=re.IGNORECASE))
    # Solo las que de verdad existen: el optimizador no genera webp cuando el
    # PNG de paleta le gana, ni portada para los vídeos de fondo.
    return {o for o in out if (ROOT / o).is_file()}


def cmd_assetsignore(args) -> int:
    """Escribe .assetsignore para que Cloudflare Workers no suba lo que no sirve."""
    refs = referenced_media()
    refs |= derived_siblings(refs)
    dead = sorted(
        str(p)
        for base in ("assets", "uploads")
        for p in Path(ROOT / base).rglob("*")
        if p.is_file() and str(p.relative_to(ROOT)) not in refs
        for p in [p.relative_to(ROOT)]
    )

    lines = [
        "# Generado por: python3 tools/optimize_media.py assetsignore",
        "# Sintaxis de .gitignore. Marca lo que Cloudflare Workers NO debe subir",
        "# como asset estático. No tiene nada que ver con .gitignore: ese decide",
        "# qué rastrea git, este qué se publica.",
        "",
        "# --- nunca se publica ---",
        *NEVER_DEPLOY,
        "",
        f"# --- medios que el sitio no referencia ({len(dead)} archivos) ---",
        "# Detectados leyendo index.html y support.js. Si añades un archivo y no",
        "# aparece en la página, vuelve a generar este archivo con el comando de",
        "# arriba. Para publicarlo todo, basta con borrar esta sección.",
    ]
    # Las rutas de .assetsignore se comparan tal cual: sin escapes, y los
    # nombres con espacios o paréntesis van literales.
    lines += ["/" + d for d in dead]

    out = ROOT / ".assetsignore"
    text = "\n".join(lines) + "\n"
    if args.dry_run:
        print(text)
        return 0
    out.write_text(text, encoding="utf-8")

    kept = sum((ROOT / r).stat().st_size for r in refs if (ROOT / r).is_file())
    skipped = sum((ROOT / d).stat().st_size for d in dead if (ROOT / d).is_file())
    print(f"Escrito {out.relative_to(ROOT)}")
    print(f"  se publica : {human(kept)} en {len(refs)} archivos de medios")
    print(f"  se omite   : {human(skipped)} en {len(dead)} archivos sin referencia")
    print(f"  más .git, tools/ y metadatos del repo")
    return 0


def run_batch(jobs: list, workers: int, label: str) -> list[dict]:
    results: list[dict] = []
    total = len(jobs)
    if total == 0:
        print(f"  (no hay {label} que procesar)")
        return results
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(fn): name for fn, name in jobs}
        for i, fut in enumerate(futures.as_completed(pending), 1):
            try:
                r = fut.result()
            except Exception as exc:  # una falla no debe tumbar el lote
                r = {
                    "file": pending[fut], "status": "error", "detail": str(exc)[:200],
                    "bytes_before": 0, "bytes_after": 0,
                }
            results.append(r)
            before, after = r.get("bytes_before", 0), r.get("bytes_after", 0)
            ratio = f"{before / after:.1f}x" if after else "—"
            flag = {"ok": "✓", "skipped": "·", "error": "✗"}.get(r["status"], "?")
            print(
                f"  [{i:3}/{total}] {flag} {r['file'][:52]:<52} "
                f"{human(before):>9} → {human(after):>9} {ratio:>6}  {r.get('detail','')}"
            )
    return results


def summarize(results: list[dict], kind: str) -> None:
    rows = [r for r in results if r.get("kind") == kind or kind == "all"]
    if not rows:
        return
    before = sum(r.get("bytes_before", 0) for r in rows)
    after = sum(r.get("bytes_after", 0) for r in rows)
    extra = sum((r.get("webp_bytes") or 0) + (r.get("webm_bytes") or 0) for r in rows)
    errors = [r for r in rows if r["status"] == "error"]
    print(f"\n  {kind}: {human(before)} → {human(after)}", end="")
    if before:
        print(f"   ({before / max(after, 1):.1f}x menos, ahorro {human(before - after)})", end="")
    print()
    if extra:
        print(f"  formatos modernos añadidos: {human(extra)}")
    if errors:
        print(f"  ⚠ {len(errors)} con error:")
        for r in errors:
            print(f"      {r['file']}: {r.get('detail','')}")


def cmd_images(args) -> int:
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Falta Pillow. Instálalo con:  pip install Pillow", file=sys.stderr)
        return 2

    paths = select_paths(walk_media(IMAGE_EXT), args, skip_suffixes={".webp"})
    print(f"\nImágenes ({len(paths)}){' — SIMULACIÓN' if args.dry_run else ''}\n")

    jobs = []
    for p in paths:
        rel = str(p.relative_to(ROOT))
        name = match_profile(rel, IMAGE_RULES) or "generic"
        prof = IMAGE_PROFILES[name]
        jobs.append((
            lambda p=p, prof=prof: optimize_image(
                p, prof, dry_run=args.dry_run, force=args.force
            ),
            rel,
        ))

    results = run_batch(jobs, args.jobs, "imágenes")
    summarize(results, "image")
    write_manifest(results, args)
    return 0


def cmd_videos(args) -> int:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print(FFMPEG_HELP, file=sys.stderr)
        return 2
    print(f"\nffmpeg: {ffmpeg}")

    paths = [
        p for p in select_paths(walk_media(VIDEO_EXT), args)
        if p.suffix.lower() == ".mp4" and not p.name.endswith(".opt.mp4")
    ]
    print(f"Vídeos ({len(paths)}){' — SIMULACIÓN' if args.dry_run else ''}\n")

    backup = None
    if args.backup_dir:
        backup = Path(args.backup_dir)
        if not backup.is_absolute():
            backup = ROOT / backup
        backup.mkdir(parents=True, exist_ok=True)
        print(f"Originales se copian a: {backup}\n")

    jobs = []
    for p in paths:
        rel = str(p.relative_to(ROOT))
        name = match_profile(rel, VIDEO_RULES) or "fondo"
        prof = VIDEO_PROFILES[name]
        jobs.append((
            lambda p=p, prof=prof: optimize_video(
                p, prof, ffmpeg, dry_run=args.dry_run, force=args.force,
                webm=args.webm, backup_dir=backup,
            ),
            rel,
        ))

    # ffmpeg ya usa varios hilos por proceso: pocos en paralelo rinden mejor.
    results = run_batch(jobs, max(1, min(args.jobs, 4)), "vídeos")
    summarize(results, "video")
    write_manifest(results, args)
    return 0


def write_manifest(results: list[dict], args) -> None:
    if args.dry_run:
        return
    out = ROOT / "tools" / "media-manifest.json"
    prev = {}
    if out.is_file():
        try:
            prev = {r["file"]: r for r in json.loads(out.read_text())["files"]}
        except Exception:
            prev = {}
    for r in results:
        prev[r["file"]] = r
    out.write_text(
        json.dumps(
            {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "files": list(prev.values())},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n  Informe: {out.relative_to(ROOT)}")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Comprime imágenes y vídeos del sitio sin cambiar sus rutas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ejemplos:\n"
               "  python3 tools/optimize_media.py analyze\n"
               "  python3 tools/optimize_media.py images --dry-run\n"
               "  python3 tools/optimize_media.py videos --jobs 4\n"
               "  python3 tools/optimize_media.py all\n"
               "  python3 tools/optimize_media.py assetsignore   # para Cloudflare\n",
    )
    ap.add_argument("command",
                    choices=["analyze", "images", "videos", "all", "assetsignore"])
    ap.add_argument("--dry-run", action="store_true", help="no escribe nada; solo informa")
    ap.add_argument("--force", action="store_true", help="escribe aunque el resultado no sea menor")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4)), help="tareas en paralelo")
    ap.add_argument("--only", metavar="PATRON", help="limita a rutas que casen, p. ej. 'assets/tanatologos/*'")
    ap.add_argument("--webm", action="store_true", help="genera además VP9/WebM (lento)")
    ap.add_argument("--backup-dir", metavar="DIR", help="copia los vídeos originales aquí antes de reemplazarlos")
    ap.add_argument("--include-unused", action="store_true",
                    help="procesa también los archivos que el sitio no referencia (maestros de diseño)")
    ap.add_argument("--all", action="store_true", help="en 'analyze', lista todos los archivos sin referencia")
    ap.add_argument("--yes", action="store_true", help="no pide confirmación con cambios sin guardar en git")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "assetsignore":
        return cmd_assetsignore(args)

    # Los archivos se reemplazan en su sitio. Si git ya los tiene guardados,
    # el original siempre se puede recuperar; si no, conviene avisar.
    if not args.dry_run and not args.yes and git_is_clean() is False:
        print(
            "⚠ El árbol de git tiene cambios sin guardar y este script reemplaza\n"
            "  los archivos en su sitio. Haz commit primero (así los originales\n"
            "  quedan recuperables) o vuelve a ejecutar con --yes.",
            file=sys.stderr,
        )
        return 1

    rc = 0
    if args.command in ("images", "all"):
        rc |= cmd_images(args)
    if args.command in ("videos", "all"):
        rc |= cmd_videos(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
