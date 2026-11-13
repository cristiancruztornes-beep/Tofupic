"""
Bot de Telegram -> ToDus para GitHub Codespaces

Variables de entorno (Codespaces > Settings > Secrets, o `export` en la terminal):
    API_ID, API_HASH, BOT_TOKEN   (obligatorias)
    ALLOWED_IDS="123,456"         (opcional: solo esos usuarios)
    MAX_MB="100"                  (opcional: límite de tamaño, 0 = sin límite)

Ejecutar:  python main.py
"""
import asyncio
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import unicodedata
from urllib.parse import unquote, urlsplit

import httpx
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
MAX_MB = int(os.environ.get("MAX_MB", "0") or 0)
MAX_BYTES = MAX_MB * 1024 * 1024


def safe_name(name):
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("._") or "archivo"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return base[:80] + ext


async def todus(*args, timeout=900):
    exe = shutil.which("todus")
    cmd = [exe] if exe else [sys.executable, "-c", "import sys; from todus.cli import main; sys.exit(main())"]
    p = await asyncio.create_subprocess_exec(
        *cmd, "--no-progress", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, f"Tiempo agotado ({timeout}s): ToDus no respondió."
    return p.returncode, out.decode(errors="replace").strip()


async def descargar_todus(url, destino):
    """Descarga un enlace de s3.todus.cu por streaming (sin cargarlo en RAM)."""
    total = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=False) as http:
        async with http.stream("GET", url, headers={"User-Agent": "Wget/1.21.3"}) as r:
            if r.status_code != 200:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            size = r.headers.get("content-length", "")
            if MAX_BYTES and size.isdigit() and int(size) > MAX_BYTES:
                raise ValueError(f"El video supera el límite de {MAX_MB} MB.")
            with open(destino, "wb") as f:
                async for chunk in r.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if MAX_BYTES and total > MAX_BYTES:
                        raise ValueError(f"El video supera el límite de {MAX_MB} MB.")
                    f.write(chunk)
    if total == 0:
        raise ValueError("ToDus devolvió un archivo vacío.")
    return total


async def subir_y_compartir(ns, path, name, timeout):
    """Sube el archivo a ToDus y devuelve (ok, mensaje_o_enlace)."""
    code, out = await todus("ns", "create", ns, timeout=60)
    if code != 0:
        return False, f"No pude preparar tu espacio de ToDus:\n{out[-500:]}"
    code, out = await todus("upload", ns, path, "--as", name, timeout=timeout)
    if code != 0:
        return False, f"Error al subir:\n{out[-700:]}"
    code, out = await todus("share", ns, name, timeout=60)
    if code != 0:
        return False, f"Subido, pero no pude crear el enlace:\n{out[-500:]}"
    found = re.search(r"https?://\S+", out)
    return True, (found.group(0).rstrip(".,)") if found else out)


async def main():
    # Aviso temprano si ToDus no es alcanzable desde Codespaces
    def check():
        try:
            socket.create_connection(("s3.todus.cu", 443), timeout=10).close()
            log.info("✅ ToDus alcanzable")
        except Exception as e:
            log.warning("❌ ToDus NO alcanzable desde este host: %r", e)
    await asyncio.get_running_loop().run_in_executor(None, check)

    client = TelegramClient(MemorySession(), API_ID, API_HASH)

    def autorizado(e):
        return not ALLOWED or e.sender_id in ALLOWED

    @client.on(events.NewMessage(pattern=r"^/start"))
    async def start(e):
        await e.reply(
            "👋 Envíame un archivo, video, audio o foto y lo subo a ToDus.\n"
            "También puedes enviarme un enlace https://s3.todus.cu/... para volver a subirlo.\n"
            "/lista - ver tus archivos\n/enlace <nombre> - enlace de un archivo\n/id - tu ID"
        )

    @client.on(events.NewMessage(pattern=r"^/id"))
    async def my_id(e):
        await e.reply(f"Tu ID: {e.sender_id}")

    @client.on(events.NewMessage(pattern=r"^/lista"))
    async def lista(e):
        if not autorizado(e):
            return await e.reply("⛔ No estás autorizado.")
        _, out = await todus("list", f"tg_{e.sender_id}", timeout=60)
        await e.reply(out[:4000] or "No tienes archivos.")

    @client.on(events.NewMessage(pattern=r"^/enlace\s+(.+)"))
    async def enlace(e):
        if not autorizado(e):
            return await e.reply("⛔ No estás autorizado.")
        _, out = await todus("share", f"tg_{e.sender_id}", e.pattern_match.group(1).strip(), timeout=60)
        await e.reply(out[:4000])

    @client.on(events.NewMessage(pattern=r"(?i)^(?!/)[^\n]*?(https://s3\.todus\.cu/\S+)"))
    async def procesar_enlace(e):
        if not autorizado(e):
            return await e.reply("⛔ No estás autorizado.")
        url = e.pattern_match.group(1).rstrip(".,)")
        if urlsplit(url).hostname != "s3.todus.cu":
            return await e.reply("❌ Solo se admiten enlaces de s3.todus.cu.")

        name = safe_name(os.path.basename(unquote(urlsplit(url).path)) or "video.mp4")
        if "." not in name:
            name += ".mp4"

        status = await e.reply("⬇️ Descargando desde ToDus...")
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, name)
            size = await descargar_todus(url, path)
            await status.edit(f"✅ Descargado ({size / 1048576:.2f} MB).\n⬆️ Subiendo a tu espacio de ToDus...")
            ok, res = await subir_y_compartir(f"tg_{e.sender_id}", path, name, timeout=1800)
            await status.edit(f"✅ Listo ({size / 1048576:.2f} MB):\n{res}" if ok else f"❌ {res}")
        except httpx.HTTPStatusError as ex:
            await status.edit(f"❌ ToDus respondió {ex}. El enlace puede estar vencido.")
        except Exception as ex:
            log.exception("Error procesando enlace")
            await status.edit(f"❌ No se pudo procesar el video: {ex}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @client.on(events.NewMessage(func=lambda e: e.message.media is not None and e.file is not None))
    async def handle_file(e):
        if not autorizado(e):
            return await e.reply("⛔ No estás autorizado.")
        if MAX_BYTES and e.file.size and e.file.size > MAX_BYTES:
            return await e.reply(f"⚠️ Archivo demasiado grande ({e.file.size // 1048576} MB, máximo {MAX_MB} MB).")

        name = safe_name(os.path.basename(e.file.name or f"{e.id}{e.file.ext or ''}"))
        status = await e.reply("⬇️ Descargando...")
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, name)
            await e.download_media(file=path)
            await status.edit("⬆️ Subiendo a ToDus...")
            ok, res = await subir_y_compartir(f"tg_{e.sender_id}", path, name, timeout=1800)
            await status.edit(f"✅ Listo:\n{res}" if ok else f"❌ {res}")
        except Exception as ex:
            log.exception("Error subiendo archivo")
            await status.edit(f"❌ Error: {ex}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info("✅ Bot conectado como @%s", me.username)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
