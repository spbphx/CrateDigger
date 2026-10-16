"""
Ставит и обновляет всё нужное внутри папки .venv (ничего не трогает в системе).
Вызывается start-файлами перед запуском программы.
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQ = HERE / "requirements.txt"
MARK = Path(sys.prefix) / ".cratedigger_req"
UPD = Path(sys.prefix) / ".cratedigger_update"

# сообщения на языке, выбранном в программе (флажки в шапке); до первого выбора — русский
MSG = {
    "old": ("Нужен Python 3.10 или новее. Сейчас:", "Python 3.10 or newer is required. Current:",
            "Hace falta Python 3.10 o más reciente. Ahora:"),
    "get": ("Скачайте Python с https://www.python.org/downloads/ и запустите start-файл снова.",
            "Download Python from https://www.python.org/downloads/ and run the start file again.",
            "Descarga Python de https://www.python.org/downloads/ y vuelve a abrir el archivo de inicio."),
    "novenv": ("(запуск вне .venv — пакеты поставятся в текущий Python)",
               "(running outside .venv — packages go into the current Python)",
               "(fuera de .venv: los paquetes se instalan en el Python actual)"),
    "first": ("Первый запуск: ставлю библиотеки (1–3 минуты, нужен интернет)...",
              "First launch: installing libraries (1–3 minutes, internet required)...",
              "Primer inicio: instalo las bibliotecas (1–3 minutos, hace falta internet)..."),
    "fail": ("\nНе получилось установить библиотеки. Проверьте интернет и запустите снова.",
             "\nCouldn't install the libraries. Check the internet connection and try again.",
             "\nNo se pudieron instalar las bibliotecas. Revisa la conexión a internet y vuelve a intentarlo."),
    "upd": ("Проверяю обновления yt-dlp...", "Checking for yt-dlp updates...", "Buscando actualizaciones de yt-dlp..."),
    "skip": ("(обновление пропущено — нет интернета?)", "(update skipped — no internet?)",
             "(actualización omitida: ¿no hay internet?)"),
}


def msg(key):
    try:
        lang = json.loads((HERE / "settings.json").read_text("utf8")).get("lang")
    except Exception:
        lang = None
    return MSG[key][{"en": 1, "es": 2}.get(lang, 0)]


def pip(*args, timeout=None):
    cmd = [sys.executable, "-m", "pip", "--disable-pip-version-check", *args]
    return subprocess.run(cmd, timeout=timeout).returncode


def main():
    if sys.version_info < (3, 10):
        print(msg("old"), sys.version.split()[0])
        print(msg("get"))
        sys.exit(1)
    if sys.prefix == sys.base_prefix:
        print(msg("novenv"))

    h = hashlib.sha1(REQ.read_bytes()).hexdigest()
    if not MARK.exists() or MARK.read_text().strip() != h:
        print(msg("first"))
        pip("install", "--upgrade", "pip", "wheel")
        if pip("install", "-r", str(REQ)) != 0:
            print(msg("fail"))
            sys.exit(1)
        MARK.write_text(h)
        UPD.write_text(str(time.time()))
        return

    # YouTube часто меняется — раз в сутки тихо обновляем yt-dlp
    try:
        last = float(UPD.read_text()) if UPD.exists() else 0
    except ValueError:
        last = 0
    if time.time() - last > 24 * 3600:
        print(msg("upd"))
        try:
            pip("install", "-q", "--upgrade", "yt-dlp[default]", "ytmusicapi", timeout=180)
        except Exception:
            print(msg("skip"))
        UPD.write_text(str(time.time()))


if __name__ == "__main__":
    main()
