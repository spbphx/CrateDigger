"""
Languages of the program's messages: English (source), Russian and Spanish.

T("English template {x}", x=...) returns the string in the current language. The key is the English
text itself, so the code stays readable; if a translation is missing, English is shown. The language
is switched from the interface (flags in the header); analysis processes receive it together with
the settings (p["lang"]), because each process has its own copy of this module.
"""
from __future__ import annotations

LANGS = ("en", "ru", "es")
LANG = "en"


def set_lang(lang: str | None):
    """Switch the language of all following messages (ignored if the code is unknown)."""
    global LANG
    if lang in LANGS:
        LANG = lang


def T(en: str, **kw) -> str:
    """Translate an English template to the current language and fill in {placeholders}."""
    s = en
    if LANG != "en":
        tr = S.get(en)
        if tr:
            s = tr[0 if LANG == "ru" else 1]
    return s.format(**kw) if kw else s


# Word forms by number: Russian has three (1, 2-4, 5+), English and Spanish have two (1, other)
PLURALS = {
    "record": {"en": ("record", "records"), "ru": ("пластинка", "пластинки", "пластинок"), "es": ("disco", "discos")},
    "loop": {"en": ("loop", "loops"), "ru": ("луп", "лупа", "лупов"), "es": ("loop", "loops")},
}


def P(n: int, word: str) -> str:
    """The word in the right form for the number n: P(1, "loop") -> "loop", P(5, "loop") -> "loops"."""
    forms = PLURALS[word][LANG]
    if len(forms) == 2:
        return forms[0] if n == 1 else forms[1]
    m10, m100 = n % 10, n % 100
    return forms[0] if m10 == 1 and m100 != 11 else forms[1] if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else forms[2]


# English source text: (Russian, Spanish)
S: dict[str, tuple[str, str]] = {
    # ---- app.py
    "✕ Error: {e}": (
        "✕ Ошибка: {e}",
        "✕ Error: {e}"),
    "wait until the dig is finished": (
        "дождитесь окончания копания",
        "espera a que termine la excavación"),
    "not an audio file: {name}": (
        "не аудиофайл: {name}",
        "no es un archivo de audio: {name}"),
    "the file is empty or larger than 800 MB": (
        "файл пустой или больше 800 МБ",
        "el archivo está vacío o pesa más de 800 MB"),
    "no such crate": (
        "нет такого ящика",
        "no existe esa caja"),
    "the files did not upload": (
        "файлы не загрузились",
        "los archivos no se subieron"),
    "this record was not found in the crate folder": (
        "эта пластинка не нашлась в папке ящика",
        "este disco no está en la carpeta de la caja"),
    "the source file was not kept — drag it into the window again": (
        "исходный файл не сохранился — перетащите его в окно ещё раз",
        "el archivo original no se guardó: arrástralo de nuevo a la ventana"),
    "can't create the folder: {e}": (
        "не могу создать папку: {e}",
        "no puedo crear la carpeta: {e}"),
    "there are no loops on screen": (
        "на экране нет лупов",
        "no hay loops en pantalla"),
    "loop files not found on disk": (
        "файлы лупов не найдены на диске",
        "no se encontraron los archivos de los loops"),
    "no hits found in the loop": (
        "в лупе не найдено ударов",
        "no se encontraron golpes en el loop"),
    "the loop's files were not found on disk": (
        "файлы лупа не найдены на диске",
        "no se encontraron los archivos del loop"),
    "no free port": (
        "нет свободного порта",
        "no hay ningún puerto libre"),
    "CrateDigger is already running — opening the window.": (
        "CrateDigger уже запущен — открываю окно.",
        "CrateDigger ya está abierto: abro la ventana."),
    "  CrateDigger {v} is running:  {url}": (
        "  CrateDigger {v} работает:  {url}",
        "  CrateDigger {v} está funcionando:  {url}"),
    "  Loops are saved to: {d}": (
        "  Лупы сохраняются в: {d}",
        "  Los loops se guardan en: {d}"),
    "  Keep this window open while digging.": (
        "  Не закрывайте это окно, пока копаете.",
        "  No cierres esta ventana mientras excavas."),
    # ---- what we look for
    "any loops": (
        "любые лупы",
        "cualquier loop"),
    "breaks": (
        "брейки",
        "breaks"),
    "melody": (
        "мелодия",
        "melodía"),
    "drums only": (
        "только барабаны",
        "solo batería"),
    "bass only": (
        "только бас",
        "solo bajo"),
    "vocals only": (
        "только вокал",
        "solo voz"),
    "pads only": (
        "только пэды",
        "solo pads"),
    "drums alone": (
        "одни барабаны",
        "batería sola"),
    "bass alone": (
        "один бас",
        "bajo solo"),
    "voice without a beat": (
        "голос без бита",
        "voz sin ritmo"),
    "pads alone": (
        "одни пэды",
        "pads solos"),
    " or ": (
        " или ",
        " o "),
    # ---- core.py: cutting
    "   tempo ≈{tempo:.1f} BPM, tuning {tuning:+.0f} cents, analysis {sec:.1f} s": (
        "   темп ≈{tempo:.1f} BPM, строй {tuning:+.0f} центов, анализ {sec:.1f} с",
        "   tempo ≈{tempo:.1f} BPM, afinación {tuning:+.0f} cents, análisis {sec:.1f} s"),
    "{why} — won't loop cleanly": (
        "{why} — ровно не залупится",
        "{why}: no se puede loopear limpio"),
    "tempo {tempo:.1f} is outside the {a}–{b} range": (
        "темп {tempo:.1f} не подходит под диапазон {a}–{b}",
        "el tempo {tempo:.1f} está fuera del rango {a}–{b}"),
    "no parts with {what} — taking another record": (
        "нет мест, где звучит {what} — беру другую пластинку",
        "no hay partes con {what}: paso a otro disco"),
    "found only {k} good loop(s) of {n}": (
        "нашёл только {k} хороших лупа(ов) из {n}",
        "solo encontré {k} loop(s) buenos de {n}"),
    "no clean loop found": (
        "не нашёл ни одного ровного лупа",
        "no encontré ningún loop limpio"),
    "almost silence": (
        "почти тишина",
        "casi silencio"),
    "no rhythm found": (
        "не нашёл ритма",
        "no encontré ritmo"),
    "no clear rhythm (noise, ambient or pads)": (
        "нет чёткого ритма (шум, эмбиент или пэды)",
        "no hay un ritmo claro (ruido, ambient o pads)"),
    "the tempo drifts (rubato, live playing without a click)": (
        "темп «плавает» (рубато, живой темп без метронома)",
        "el tempo fluctúa (rubato, tocado en vivo sin metrónomo)"),
    "the file is too short or empty": (
        "слишком короткий или пустой файл",
        "el archivo es demasiado corto o está vacío"),
    "ffmpeg not found (pip install imageio-ffmpeg)": (
        "ffmpeg не найден (pip install imageio-ffmpeg)",
        "no se encontró ffmpeg (pip install imageio-ffmpeg)"),
    # ---- core.py: crate and pipeline
    "📦 New crate: {name}": (
        "📦 Новый ящик: {name}",
        "📦 Caja nueva: {name}"),
    "🎚 Tempo fit: pitching (tempo and pitch change together)": (
        "🎚 Подгонка темпа: питчинг (темп меняется вместе с высотой)",
        "🎚 Ajuste de tempo: pitching (el tempo cambia junto con la altura)"),
    "🎚 Tempo fit: stretching (hits stay in place, same pitch)": (
        "🎚 Подгонка темпа: стретч (удары на месте, высота та же)",
        "🎚 Ajuste de tempo: stretching (los golpes en su sitio, misma altura)"),
    "   (source not kept: {e})": (
        "   (исходник не сохранился: {e})",
        "   (no se guardó el original: {e})"),
    "🏁 Done: {n} {rw}, {total} {lw}": (
        "🏁 Готово: {n} {rw}, {total} {lw}",
        "🏁 Listo: {n} {rw}, {total} {lw}"),
    "⚙ Analysing in {n} parallel processes": (
        "⚙ Анализ идёт в {n} процессах параллельно",
        "⚙ Analizando en {n} procesos en paralelo"),
    "✕ {label}: not saved ({e})": (
        "✕ {label}: не сохранилось ({e})",
        "✕ {label}: no se guardó ({e})"),
    "✕ {label}: the analysis process crashed (maybe out of memory) — skipping": (
        "✕ {label}: процесс анализа завершился аварийно (возможно, не хватило памяти) — пропускаю",
        "✕ {label}: el proceso de análisis falló (quizá por falta de memoria): lo salto"),
    "analysing: {names}": (
        "анализ: {names}",
        "analizando: {names}"),
    "Working…": (
        "Работаю…",
        "Trabajando…"),
    "🎛 Tags: {tags}; {years}; {bpm}; {n} records × {k} loops, {bars} bar(s)": (
        "🎛 Теги: {tags}; {years}; {bpm}; {n} пластинок по {k} лупа, {bars} такт(а)",
        "🎛 Etiquetas: {tags}; {years}; {bpm}; {n} discos × {k} loops, {bars} compás(es)"),
    "none, random records": (
        "нет, случайные пластинки",
        "ninguna, discos al azar"),
    "years {a}–{b}": (
        "годы {a}–{b}",
        "años {a}–{b}"),
    "any years": (
        "годы любые",
        "cualquier año"),
    "any BPM": (
        "BPM любой",
        "cualquier BPM"),
    "yt-dlp is not installed. Launch the program with the start file — it installs everything.": (
        "Не установлен yt-dlp. Запустите программу через start-файл — он всё поставит.",
        "yt-dlp no está instalado. Abre el programa con el archivo de inicio: lo instala todo."),
    "💎 Looking for rare ones: records with the fewest plays first (up to 150K)": (
        "💎 Ищу редкие: сначала пластинки с наименьшим числом прослушиваний (до 150 тыс.)",
        "💎 Busco rarezas: primero los discos con menos reproducciones (hasta 150 mil)"),
    "🎯 Looking for: {what}. Clean parts first, then parts with extras; such parts are rare, so "
    "digging takes longer": (
        "🎯 Ищу: {what}. Сначала беру «чистые» куски, потом «с примесью»; таких мест мало — копать "
        "придётся дольше",
        "🎯 Busco: {what}. Primero partes limpias, luego con extras; son raras, así que la excavación "
        "tardará más"),
    "⏹ Stopped": (
        "⏹ Остановлено",
        "⏹ Detenido"),
    "😕 Went through too many records — try other tags or loosen the filters.": (
        "😕 Перебрал слишком много пластинок — попробуйте другие теги или ослабьте фильтры.",
        "😕 Revisé demasiados discos: prueba otras etiquetas o afloja los filtros."),
    "😕 The search finds nothing more. Try other tags.": (
        "😕 Поиск больше ничего не находит. Попробуйте другие теги.",
        "😕 La búsqueda ya no encuentra nada. Prueba otras etiquetas."),
    "Searching: “{q}”": (
        "Ищу: «{q}»",
        "Buscando: «{q}»"),
    "🔎 “{q}”: found {n}, new suitable {k}": (
        "🔎 «{q}»: найдено {n}, новых подходящих {k}",
        "🔎 «{q}»: encontrados {n}, nuevos aptos {k}"),
    "Checking: {label}": (
        "Смотрю: {label}",
        "Revisando: {label}"),
    "⏭ {label}: length {sec} s — skipping": (
        "⏭ {label}: длина {sec} с — пропускаю",
        "⏭ {label}: dura {sec} s, lo salto"),
    "⏭ {label}: too well known ({views} plays)": (
        "⏭ {label}: слишком известная ({views} прослушиваний)",
        "⏭ {label}: demasiado conocido ({views} reproducciones)"),
    "   (nothing rare here — raising the limit to {n})": (
        "   (редкое тут не находится — поднимаю планку до {n})",
        "   (aquí no hay rarezas: subo el límite a {n})"),
    "⏭ {label}: wrong era ({why})": (
        "⏭ {label}: не та эпоха ({why})",
        "⏭ {label}: época equivocada ({why})"),
    "Downloading: {label}": (
        "Скачиваю: {label}",
        "Descargando: {label}"),
    "{views} plays": (
        "{views} прослушиваний",
        "{views} reproducciones"),
    "   Network stopped so as not to burn through records; finishing what's already downloaded.": (
        "   Сеть остановлена, чтобы не перебирать пластинки впустую; уже скачанное дорабатываю.",
        "   Red detenida para no gastar discos en vano; termino lo que ya está descargado."),
    "   Network stopped so YouTube doesn't restrict more; finishing what's already downloaded.": (
        "   Сеть остановлена, чтобы YouTube не ограничил сильнее; уже скачанное дорабатываю.",
        "   Red detenida para que YouTube no limite más; termino lo que ya está descargado."),
    "My file": (
        "Мой файл",
        "Mi archivo"),
    "🔁 Re-running: {label} — {what}, {bars} bar(s), up to {n} loops": (
        "🔁 Прогоняю заново: {label} — {what}, {bars} такт(а), до {n} лупов",
        "🔁 Reprocesando: {label} — {what}, {bars} compás(es), hasta {n} loops"),
    "   the source isn't in the crate folder — downloading again": (
        "   исходника нет в папке ящика — скачиваю ещё раз",
        "   el original no está en la carpeta de la caja: lo descargo otra vez"),
    "No loops found with these settings — details in the log": (
        "С этими настройками лупы не нашлись — подробности в журнале",
        "No se encontraron loops con estos ajustes: detalles en el registro"),
    # ---- credits.txt / MODEL_SAMPLES.txt
    "CrateDigger — where the loops come from (useful for sample clearance)": (
        "CrateDigger — откуда взяты лупы (пригодится для клиринга семплов)",
        "CrateDigger — de dónde salen los loops (útil para licenciar samples)"),
    "original tempo {bpm}, stretch {pct:+.1f}%": (
        "исходный темп {bpm}, растяжение {pct:+.1f}%",
        "tempo original {bpm}, estiramiento {pct:+.1f}%"),
    "no grid, cut exactly to tempo": (
        "без сетки, вырезано ровно под темп",
        "sin rejilla, cortado justo al tempo"),
    " (with extras)": (
        " (с примесью)",
        " (con extras)"),
    "{bars} bar(s)": (
        "{bars} такт(а)",
        "{bars} compás(es)"),
    "How to play these loops on the Elektron Model:Samples": (
        "Как играть эти лупы на Elektron Model:Samples",
        "Cómo tocar estos loops en el Elektron Model:Samples"),
    "1. Transfer the files from the ModelSamples_mono16 folder with Elektron Transfer.": (
        "1. Перекиньте файлы из папки ModelSamples_mono16 через Elektron Transfer.",
        "1. Pasa los archivos de la carpeta ModelSamples_mono16 con Elektron Transfer."),
    "1. Transfer the files from the Stereo_24bit folder with Elektron Transfer (it converts to mono itself).": (
        "1. Перекиньте файлы из папки Stereo_24bit через Elektron Transfer (он сам переведёт в моно).",
        "1. Pasa los archivos de la carpeta Stereo_24bit con Elektron Transfer (los pasa a mono solo)."),
    "2. Pattern tempo = the number in the file name (Gmaj90Name1 -> 90 BPM).": (
        "2. Темп паттерна = число в имени файла (Gmaj90Name1 -> 90 BPM).",
        "2. Tempo del patrón = el número en el nombre del archivo (Gmaj90Name1 -> 90 BPM)."),
    "3. Track Length = the number of steps from the table below, trig on step 1.": (
        "3. Длина трека (Track Length) = количество шагов из таблицы ниже, триг на шаге 1.",
        "3. Track Length = el número de pasos de la tabla de abajo, trig en el paso 1."),
    "   The loop restarts every cycle, so it never drifts.": (
        "   Луп перезапускается каждый цикл, поэтому никогда не «уплывает».",
        "   El loop se reinicia en cada ciclo, así que nunca se desfasa."),
    "4. Chops: the loops are exactly bar-length, so the START knob splits them evenly:": (
        "4. Чопы: лупы ровно по тактам, поэтому ручка START делит их на равные части:",
        "4. Chops: los loops miden compases exactos, así que la perilla START los divide en partes iguales:"),
    "   0, 16, 32, 48, 64, 80, 96, 112 ≈ eighths of the loop. Put different START values on": (
        "   0, 16, 32, 48, 64, 80, 96, 112 ≈ восьмые части лупа. Ставьте разные START на",
        "   0, 16, 32, 48, 64, 80, 96, 112 ≈ octavos del loop. Pon distintos START en"),
    "   different steps (parameter locks) — you get re-chopping, like on an MPC.": (
        "   разные шаги (параметр-локи) — получится перенарезка, как на MPC.",
        "   distintos pasos (parameter locks): obtienes un re-chop, como en una MPC."),
    "5. Loops with the same or a neighbouring Camelot number (8A, 8B, 7A, 9A) sound good together.": (
        "5. Лупы с одинаковой цифрой Camelot или соседней (8A, 8B, 7A, 9A) звучат вместе.",
        "5. Los loops con el mismo número Camelot o uno vecino (8A, 8B, 7A, 9A) suenan bien juntos."),
    "6. If there are more than 64 steps, set the track SCALE to 1/2X and halve the length.": (
        "6. Если шагов больше 64 — поставьте у трека SCALE 1/2X и длину вдвое меньше.",
        "6. Si hay más de 64 pasos, pon SCALE 1/2X en la pista y la mitad de longitud."),
    "7. The files are sorted into folders: ModelSamples_mono16/<key>/<BPM>/ —": (
        "7. Файлы разложены по папкам: ModelSamples_mono16/<тональность>/<BPM>/ —",
        "7. Los archivos están ordenados en carpetas: ModelSamples_mono16/<tonalidad>/<BPM>/ —"),
    "   transfer the whole folder and find loops on the device by key and tempo.": (
        "   перекиньте папку целиком, на приборе найдёте нужное по тональности и темпу.",
        "   pasa la carpeta entera y en el aparato los encontrarás por tonalidad y tempo."),
    "File": (
        "Файл",
        "Archivo"),
    "Bars": (
        "Тактов",
        "Compases"),
    "Steps": (
        "Шагов",
        "Pasos"),
    "Key": (
        "Тональн.",
        "Tono"),
    # ---- workers.py
    "⚠ Parallel analysis unavailable ({e}) — working in a single thread": (
        "⚠ Параллельный анализ недоступен ({e}) — работаю в одном потоке",
        "⚠ El análisis en paralelo no está disponible ({e}): trabajo en un solo hilo"),
    "⚠ An analysis process crashed — restarting": (
        "⚠ Процесс анализа упал — перезапускаю",
        "⚠ Un proceso de análisis falló: lo reinicio"),
    "⚠ Analysis processes keep crashing — continuing in a single thread": (
        "⚠ Процессы анализа падают — дальше работаю в одном потоке",
        "⚠ Los procesos de análisis siguen fallando: sigo en un solo hilo"),
    # ---- sources.py
    "{n}M": (
        "{n} млн",
        "{n} M"),
    "{n}K": (
        "{n} тыс.",
        "{n} mil"),
    "YouTube asks you to confirm you're not a bot.": (
        "YouTube просит подтвердить, что вы не бот.",
        "YouTube pide confirmar que no eres un bot."),
    "YouTube has temporarily limited requests (429). Wait 10–30 minutes.": (
        "YouTube временно ограничил запросы (429). Подождите 10–30 минут.",
        "YouTube ha limitado las solicitudes temporalmente (429). Espera 10–30 minutos."),
    "The track is unavailable in your region or removed — taking the next one.": (
        "Трек недоступен в вашем регионе или удалён — беру следующий.",
        "La pista no está disponible en tu región o fue eliminada: paso a la siguiente."),
    "ffmpeg problem: {e}": (
        "Проблема с ffmpeg: {e}",
        "Problema con ffmpeg: {e}"),
    "🍪 Using cookies.txt from the program folder": (
        "🍪 Использую cookies.txt из папки программы",
        "🍪 Uso cookies.txt de la carpeta del programa"),
    "⚠ Deno/Node not found — YouTube may refuse to give audio. Run the start file again.": (
        "⚠ Не найден Deno/Node — YouTube может не отдавать звук. Перезапустите start-файл.",
        "⚠ No se encontró Deno/Node: YouTube puede no entregar el audio. Vuelve a abrir el archivo de inicio."),
    "⚠ search: {e}": (
        "⚠ поиск: {e}",
        "⚠ búsqueda: {e}"),
    "YouTube doesn't accept cookies.txt — the file seems outdated. Export it again (sign in to "
    "YouTube in the browser) or delete cookies.txt from the program folder.": (
        "YouTube не принимает cookies.txt — похоже, файл устарел. Экспортируйте его заново "
        "(залогиньтесь в YouTube в браузере) или удалите cookies.txt из папки программы.",
        "YouTube no acepta cookies.txt: parece que el archivo está caducado. Expórtalo de nuevo "
        "(inicia sesión en YouTube en el navegador) o borra cookies.txt de la carpeta del programa."),
    "🍪 YouTube asks for confirmation — taking cookies from {b}…": (
        "🍪 YouTube просит подтверждение — беру cookies из браузера {b}…",
        "🍪 YouTube pide confirmación: tomo las cookies de {b}…"),
    "   ✓ it worked: digging on with {b} cookies (saved in settings)": (
        "   ✓ сработало: дальше копаю с cookies {b} (запомню в настройках)",
        "   ✓ funcionó: sigo con las cookies de {b} (lo guardo en los ajustes)"),
    "   ✕ YouTube still refuses even with {b} cookies": (
        "   ✕ c cookies {b} YouTube всё равно не пускает",
        "   ✕ YouTube sigue sin dejar pasar con las cookies de {b}"),
    "   – {b}: no cookies found (browser not installed or access denied)": (
        "   – {b}: не нашёл cookies (браузер не установлен или закрыт доступ)",
        "   – {b}: no encontré cookies (navegador no instalado o acceso denegado)"),
    "YouTube refuses even with browser cookies. Open music.youtube.com in Firefox, sign in to your "
    "Google account (the “Sign in” button at the top right), play any track for a couple of seconds, "
    "then press “Dig” again. If that doesn't help, wait 30–60 minutes: YouTube has temporarily "
    "limited your IP.": (
        "YouTube не пускает даже с cookies браузера. Откройте music.youtube.com в Firefox и войдите в "
        "свой Google-аккаунт (кнопка «Войти» справа вверху), включите любой трек на пару секунд, "
        "затем снова нажмите «Копать». Если не поможет — подождите 30–60 минут: YouTube временно "
        "ограничил ваш IP.",
        "YouTube no deja pasar ni con las cookies del navegador. Abre music.youtube.com en Firefox, "
        "inicia sesión con tu cuenta de Google (botón «Acceder» arriba a la derecha), reproduce "
        "cualquier pista un par de segundos y vuelve a pulsar «Excavar». Si no funciona, espera 30–60 "
        "minutos: YouTube ha limitado tu IP temporalmente."),
    "YouTube asks you to confirm you're not a bot, and browser cookies couldn't be read. Open "
    "music.youtube.com in Firefox, sign in and press “Dig” again. Plan B: put a cookies.txt file into "
    "the program folder (see README).": (
        "YouTube просит подтвердить, что вы не бот, а cookies браузеров прочитать не удалось. "
        "Откройте music.youtube.com в Firefox, войдите в аккаунт и снова нажмите «Копать». Запасной "
        "вариант — положить файл cookies.txt в папку программы (см. README).",
        "YouTube pide confirmar que no eres un bot y no se pudieron leer las cookies del navegador. "
        "Abre music.youtube.com en Firefox, inicia sesión y vuelve a pulsar «Excavar». Plan B: pon un "
        "archivo cookies.txt en la carpeta del programa (ver README)."),
    "the file didn't download": (
        "файл не скачался",
        "el archivo no se descargó"),
    "in the title": (
        "в названии",
        "en el título"),
    "release date": (
        "дата релиза",
        "fecha de lanzamiento"),
    "year not confirmed": (
        "год не подтверждён",
        "año no confirmado"),
    "too old": (
        "слишком старая",
        "demasiado antiguo"),
    "reissue": (
        "переиздание",
        "reedición"),
    "recorded {y}": (
        "запись {y} г.",
        "grabado en {y}"),
    "year unknown": (
        "год неизвестен",
        "año desconocido"),
    "not it (remix/lo-fi/beat)": (
        "не то (ремикс/лоуфай/бит)",
        "no sirve (remix/lo-fi/beat)"),
    "wrong length": (
        "не та длина",
        "duración incorrecta"),
}
