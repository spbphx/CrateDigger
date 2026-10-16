"""
Языки сообщений программы: русский (исходный), английский, испанский.

T("русский шаблон {x}", x=…) — строка на текущем языке. Ключ — сам русский текст, поэтому код
остаётся читаемым, а если перевода нет, показывается русский. Язык меняется из интерфейса
(флажки в шапке); процессы анализа получают его вместе с настройками (p["lang"]).
"""
from __future__ import annotations

LANGS = ("ru", "en", "es")
LANG = "ru"


def set_lang(lang: str | None):
    global LANG
    if lang in LANGS:
        LANG = lang


def T(ru: str, **kw) -> str:
    s = ru
    if LANG != "ru":
        tr = S.get(ru)
        if tr:
            s = tr[LANGS.index(LANG) - 1]
    return s.format(**kw) if kw else s


# формы слова по числу: русский (1, 2–4, 5+), английский и испанский (1, остальные)
PLURALS = {
    "record": (("пластинка", "пластинки", "пластинок"), ("record", "records"), ("disco", "discos")),
    "loop": (("луп", "лупа", "лупов"), ("loop", "loops"), ("loop", "loops")),
}


def P(n: int, word: str) -> str:
    """Слово в нужной форме: P(1, "loop") -> «луп» / «loop», P(5, "loop") -> «лупов» / «loops»."""
    forms = PLURALS[word][LANGS.index(LANG)]
    if len(forms) == 2:
        return forms[0] if n == 1 else forms[1]
    m10, m100 = n % 10, n % 100
    return forms[0] if m10 == 1 and m100 != 11 else forms[1] if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else forms[2]


# русский: (английский, испанский)
S: dict[str, tuple[str, str]] = {
    # ---- app.py
    "✕ Ошибка: {e}": ("✕ Error: {e}", "✕ Error: {e}"),
    "дождитесь окончания копания": ("wait until the dig is finished", "espera a que termine la excavación"),
    "не аудиофайл: {name}": ("not an audio file: {name}", "no es un archivo de audio: {name}"),
    "файл пустой или больше 800 МБ": ("the file is empty or larger than 800 MB",
                                      "el archivo está vacío o pesa más de 800 MB"),
    "нет такого ящика": ("no such crate", "no existe esa caja"),
    "файлы не загрузились": ("the files did not upload", "los archivos no se subieron"),
    "эта пластинка не нашлась в папке ящика": ("this record was not found in the crate folder",
                                                "este disco no está en la carpeta de la caja"),
    "исходный файл не сохранился — перетащите его в окно ещё раз": (
        "the source file was not kept — drag it into the window again",
        "el archivo original no se guardó: arrástralo de nuevo a la ventana"),
    "не могу создать папку: {e}": ("can't create the folder: {e}", "no puedo crear la carpeta: {e}"),
    "на экране нет лупов": ("there are no loops on screen", "no hay loops en pantalla"),
    "файлы лупов не найдены на диске": ("loop files not found on disk", "no se encontraron los archivos de los loops"),
    "файлы лупа не найдены на диске": ("loop files not found on disk", "no se encontraron los archivos del loop"),
    "нет свободного порта": ("no free port", "no hay ningún puerto libre"),
    "CrateDigger уже запущен — открываю окно.": ("CrateDigger is already running — opening the window.",
                                                 "CrateDigger ya está abierto: abro la ventana."),
    "  CrateDigger {v} работает:  {url}": ("  CrateDigger {v} is running:  {url}",
                                          "  CrateDigger {v} está funcionando:  {url}"),
    "  Лупы сохраняются в: {d}": ("  Loops are saved to: {d}", "  Los loops se guardan en: {d}"),
    "  Не закрывайте это окно, пока копаете.": ("  Keep this window open while digging.",
                                                "  No cierres esta ventana mientras excavas."),

    # ---- что ищем
    "любые лупы": ("any loops", "cualquier loop"),
    "брейки": ("breaks", "breaks"),
    "мелодия": ("melody", "melodía"),
    "только барабаны": ("drums only", "solo batería"),
    "только бас": ("bass only", "solo bajo"),
    "только вокал": ("vocals only", "solo voz"),
    "только пэды": ("pads only", "solo pads"),
    "одни барабаны": ("drums alone", "batería sola"),
    "один бас": ("bass alone", "bajo solo"),
    "голос без бита": ("voice without a beat", "voz sin ritmo"),
    "одни пэды": ("pads alone", "pads solos"),
    " или ": (" or ", " o "),

    # ---- core.py: нарезка
    "   темп ≈{tempo:.1f} BPM, строй {tuning:+.0f} центов, анализ {sec:.1f} с": (
        "   tempo ≈{tempo:.1f} BPM, tuning {tuning:+.0f} cents, analysis {sec:.1f} s",
        "   tempo ≈{tempo:.1f} BPM, afinación {tuning:+.0f} cents, análisis {sec:.1f} s"),
    "{why} — ровно не залупится": ("{why} — won't loop cleanly", "{why}: no se puede loopear limpio"),
    "темп {tempo:.1f} не подходит под диапазон {a}–{b}": (
        "tempo {tempo:.1f} is outside the {a}–{b} range", "el tempo {tempo:.1f} está fuera del rango {a}–{b}"),
    "нет мест, где звучит {what} — беру другую пластинку": (
        "no parts with {what} — taking another record", "no hay partes con {what}: paso a otro disco"),
    "нашёл только {k} хороших лупа(ов) из {n}": ("found only {k} good loop(s) of {n}",
                                                "solo encontré {k} loop(s) buenos de {n}"),
    "не нашёл ни одного ровного лупа": ("no clean loop found", "no encontré ningún loop limpio"),
    "почти тишина": ("almost silence", "casi silencio"),
    "не нашёл ритма": ("no rhythm found", "no encontré ritmo"),
    "нет чёткого ритма (шум, эмбиент или пэды)": ("no clear rhythm (noise, ambient or pads)",
                                                  "no hay un ritmo claro (ruido, ambient o pads)"),
    "темп «плавает» (рубато, живой темп без метронома)": (
        "the tempo drifts (rubato, live playing without a click)",
        "el tempo fluctúa (rubato, tocado en vivo sin metrónomo)"),
    "слишком короткий или пустой файл": ("the file is too short or empty", "el archivo es demasiado corto o está vacío"),
    "ffmpeg не найден (pip install imageio-ffmpeg)": ("ffmpeg not found (pip install imageio-ffmpeg)",
                                                       "no se encontró ffmpeg (pip install imageio-ffmpeg)"),

    # ---- core.py: ящик и конвейер
    "📦 Новый ящик: {name}": ("📦 New crate: {name}", "📦 Caja nueva: {name}"),
    "🎚 Подгонка темпа: питчинг (темп меняется вместе с высотой)": (
        "🎚 Tempo fit: pitching (tempo and pitch change together)",
        "🎚 Ajuste de tempo: pitching (el tempo cambia junto con la altura)"),
    "🎚 Подгонка темпа: стретч (удары на месте, высота та же)": (
        "🎚 Tempo fit: stretching (hits stay in place, same pitch)",
        "🎚 Ajuste de tempo: stretching (los golpes en su sitio, misma altura)"),
    "   (исходник не сохранился: {e})": ("   (source not kept: {e})", "   (no se guardó el original: {e})"),
    "🏁 Готово: {n} {rw}, {total} {lw}": ("🏁 Done: {n} {rw}, {total} {lw}", "🏁 Listo: {n} {rw}, {total} {lw}"),
    "⚙ Анализ идёт в {n} процессах параллельно": ("⚙ Analysing in {n} parallel processes",
                                                 "⚙ Analizando en {n} procesos en paralelo"),
    "✕ {label}: не сохранилось ({e})": ("✕ {label}: not saved ({e})", "✕ {label}: no se guardó ({e})"),
    "✕ {label}: процесс анализа завершился аварийно (возможно, не хватило памяти) — пропускаю": (
        "✕ {label}: the analysis process crashed (maybe out of memory) — skipping",
        "✕ {label}: el proceso de análisis falló (quizá por falta de memoria): lo salto"),
    "анализ: {names}": ("analysing: {names}", "analizando: {names}"),
    "Работаю…": ("Working…", "Trabajando…"),
    "🎛 Теги: {tags}; {years}; {bpm}; {n} пластинок по {k} лупа, {bars} такт(а)": (
        "🎛 Tags: {tags}; {years}; {bpm}; {n} records × {k} loops, {bars} bar(s)",
        "🎛 Etiquetas: {tags}; {years}; {bpm}; {n} discos × {k} loops, {bars} compás(es)"),
    "нет, случайные пластинки": ("none, random records", "ninguna, discos al azar"),
    "годы {a}–{b}": ("years {a}–{b}", "años {a}–{b}"),
    "годы любые": ("any years", "cualquier año"),
    "BPM любой": ("any BPM", "cualquier BPM"),
    "Не установлен yt-dlp. Запустите программу через start-файл — он всё поставит.": (
        "yt-dlp is not installed. Launch the program with the start file — it installs everything.",
        "yt-dlp no está instalado. Abre el programa con el archivo de inicio: lo instala todo."),
    "💎 Ищу редкие: сначала пластинки с наименьшим числом прослушиваний (до 150 тыс.)": (
        "💎 Looking for rare ones: records with the fewest plays first (up to 150K)",
        "💎 Busco rarezas: primero los discos con menos reproducciones (hasta 150 mil)"),
    "🎯 Ищу: {what}. Сначала беру «чистые» куски, потом «с примесью»; таких мест мало — копать придётся дольше": (
        "🎯 Looking for: {what}. Clean parts first, then parts with extras; such parts are rare, so digging takes longer",
        "🎯 Busco: {what}. Primero partes limpias, luego con extras; son raras, así que la excavación tardará más"),
    "⏹ Остановлено": ("⏹ Stopped", "⏹ Detenido"),
    "😕 Перебрал слишком много пластинок — попробуйте другие теги или ослабьте фильтры.": (
        "😕 Went through too many records — try other tags or loosen the filters.",
        "😕 Revisé demasiados discos: prueba otras etiquetas o afloja los filtros."),
    "😕 Поиск больше ничего не находит. Попробуйте другие теги.": (
        "😕 The search finds nothing more. Try other tags.",
        "😕 La búsqueda ya no encuentra nada. Prueba otras etiquetas."),
    "Ищу: «{q}»": ("Searching: “{q}”", "Buscando: «{q}»"),
    "🔎 «{q}»: найдено {n}, новых подходящих {k}": ("🔎 “{q}”: found {n}, new suitable {k}",
                                                  "🔎 «{q}»: encontrados {n}, nuevos aptos {k}"),
    "Смотрю: {label}": ("Checking: {label}", "Revisando: {label}"),
    "⏭ {label}: длина {sec} с — пропускаю": ("⏭ {label}: length {sec} s — skipping",
                                            "⏭ {label}: dura {sec} s, lo salto"),
    "⏭ {label}: слишком известная ({views} прослушиваний)": (
        "⏭ {label}: too well known ({views} plays)", "⏭ {label}: demasiado conocido ({views} reproducciones)"),
    "   (редкое тут не находится — поднимаю планку до {n})": (
        "   (nothing rare here — raising the limit to {n})", "   (aquí no hay rarezas: subo el límite a {n})"),
    "⏭ {label}: не та эпоха ({why})": ("⏭ {label}: wrong era ({why})", "⏭ {label}: época equivocada ({why})"),
    "Скачиваю: {label}": ("Downloading: {label}", "Descargando: {label}"),
    "{views} прослушиваний": ("{views} plays", "{views} reproducciones"),
    "   Сеть остановлена, чтобы не перебирать пластинки впустую; уже скачанное дорабатываю.": (
        "   Network stopped so as not to burn through records; finishing what's already downloaded.",
        "   Red detenida para no gastar discos en vano; termino lo que ya está descargado."),
    "   Сеть остановлена, чтобы YouTube не ограничил сильнее; уже скачанное дорабатываю.": (
        "   Network stopped so YouTube doesn't restrict more; finishing what's already downloaded.",
        "   Red detenida para que YouTube no limite más; termino lo que ya está descargado."),
    "Мой файл": ("My file", "Mi archivo"),
    "🔁 Прогоняю заново: {label} — {what}, {bars} такт(а), до {n} лупов": (
        "🔁 Re-running: {label} — {what}, {bars} bar(s), up to {n} loops",
        "🔁 Reprocesando: {label} — {what}, {bars} compás(es), hasta {n} loops"),
    "   исходника нет в папке ящика — скачиваю ещё раз": (
        "   the source isn't in the crate folder — downloading again",
        "   el original no está en la carpeta de la caja: lo descargo otra vez"),
    "С этими настройками лупы не нашлись — подробности в журнале": (
        "No loops found with these settings — details in the log",
        "No se encontraron loops con estos ajustes: detalles en el registro"),

    # ---- credits.txt / MODEL_SAMPLES.txt
    "CrateDigger — откуда взяты лупы (пригодится для клиринга семплов)": (
        "CrateDigger — where the loops come from (useful for sample clearance)",
        "CrateDigger — de dónde salen los loops (útil para licenciar samples)"),
    "исходный темп {bpm}, растяжение {pct:+.1f}%": ("original tempo {bpm}, stretch {pct:+.1f}%",
                                                   "tempo original {bpm}, estiramiento {pct:+.1f}%"),
    "без сетки, вырезано ровно под темп": ("no grid, cut exactly to tempo", "sin rejilla, cortado justo al tempo"),
    " (с примесью)": (" (with extras)", " (con extras)"),
    "{bars} такт(а)": ("{bars} bar(s)", "{bars} compás(es)"),
    "Как играть эти лупы на Elektron Model:Samples": ("How to play these loops on the Elektron Model:Samples",
                                                      "Cómo tocar estos loops en el Elektron Model:Samples"),
    "1. Перекиньте файлы из папки ModelSamples_mono16 через Elektron Transfer.": (
        "1. Transfer the files from the ModelSamples_mono16 folder with Elektron Transfer.",
        "1. Pasa los archivos de la carpeta ModelSamples_mono16 con Elektron Transfer."),
    "1. Перекиньте файлы из папки Stereo_24bit через Elektron Transfer (он сам переведёт в моно).": (
        "1. Transfer the files from the Stereo_24bit folder with Elektron Transfer (it converts to mono itself).",
        "1. Pasa los archivos de la carpeta Stereo_24bit con Elektron Transfer (los pasa a mono solo)."),
    "2. Темп паттерна = число в имени файла (Gmaj90Name1 -> 90 BPM).": (
        "2. Pattern tempo = the number in the file name (Gmaj90Name1 -> 90 BPM).",
        "2. Tempo del patrón = el número en el nombre del archivo (Gmaj90Name1 -> 90 BPM)."),
    "3. Длина трека (Track Length) = количество шагов из таблицы ниже, триг на шаге 1.": (
        "3. Track Length = the number of steps from the table below, trig on step 1.",
        "3. Track Length = el número de pasos de la tabla de abajo, trig en el paso 1."),
    "   Луп перезапускается каждый цикл, поэтому никогда не «уплывает».": (
        "   The loop restarts every cycle, so it never drifts.",
        "   El loop se reinicia en cada ciclo, así que nunca se desfasa."),
    "4. Чопы: лупы ровно по тактам, поэтому ручка START делит их на равные части:": (
        "4. Chops: the loops are exactly bar-length, so the START knob splits them evenly:",
        "4. Chops: los loops miden compases exactos, así que la perilla START los divide en partes iguales:"),
    "   0, 16, 32, 48, 64, 80, 96, 112 ≈ восьмые части лупа. Ставьте разные START на": (
        "   0, 16, 32, 48, 64, 80, 96, 112 ≈ eighths of the loop. Put different START values on",
        "   0, 16, 32, 48, 64, 80, 96, 112 ≈ octavos del loop. Pon distintos START en"),
    "   разные шаги (параметр-локи) — получится перенарезка, как на MPC.": (
        "   different steps (parameter locks) — you get re-chopping, like on an MPC.",
        "   distintos pasos (parameter locks): obtienes un re-chop, como en una MPC."),
    "5. Лупы с одинаковой цифрой Camelot или соседней (8A, 8B, 7A, 9A) звучат вместе.": (
        "5. Loops with the same or a neighbouring Camelot number (8A, 8B, 7A, 9A) sound good together.",
        "5. Los loops con el mismo número Camelot o uno vecino (8A, 8B, 7A, 9A) suenan bien juntos."),
    "6. Если шагов больше 64 — поставьте у трека SCALE 1/2X и длину вдвое меньше.": (
        "6. If there are more than 64 steps, set the track SCALE to 1/2X and halve the length.",
        "6. Si hay más de 64 pasos, pon SCALE 1/2X en la pista y la mitad de longitud."),
    "7. Файлы разложены по папкам: ModelSamples_mono16/<тональность>/<BPM>/ —": (
        "7. The files are sorted into folders: ModelSamples_mono16/<key>/<BPM>/ —",
        "7. Los archivos están ordenados en carpetas: ModelSamples_mono16/<tonalidad>/<BPM>/ —"),
    "   перекиньте папку целиком, на приборе найдёте нужное по тональности и темпу.": (
        "   transfer the whole folder and find loops on the device by key and tempo.",
        "   pasa la carpeta entera y en el aparato los encontrarás por tonalidad y tempo."),
    "Файл": ("File", "Archivo"),
    "Тактов": ("Bars", "Compases"),
    "Шагов": ("Steps", "Pasos"),
    "Тональн.": ("Key", "Tono"),

    # ---- workers.py
    "⚠ Параллельный анализ недоступен ({e}) — работаю в одном потоке": (
        "⚠ Parallel analysis unavailable ({e}) — working in a single thread",
        "⚠ El análisis en paralelo no está disponible ({e}): trabajo en un solo hilo"),
    "⚠ Процесс анализа упал — перезапускаю": ("⚠ An analysis process crashed — restarting",
                                             "⚠ Un proceso de análisis falló: lo reinicio"),
    "⚠ Процессы анализа падают — дальше работаю в одном потоке": (
        "⚠ Analysis processes keep crashing — continuing in a single thread",
        "⚠ Los procesos de análisis siguen fallando: sigo en un solo hilo"),

    # ---- sources.py
    "{n} млн": ("{n}M", "{n} M"),
    "{n} тыс.": ("{n}K", "{n} mil"),
    "YouTube просит подтвердить, что вы не бот.": ("YouTube asks you to confirm you're not a bot.",
                                                    "YouTube pide confirmar que no eres un bot."),
    "YouTube временно ограничил запросы (429). Подождите 10–30 минут.": (
        "YouTube has temporarily limited requests (429). Wait 10–30 minutes.",
        "YouTube ha limitado las solicitudes temporalmente (429). Espera 10–30 minutos."),
    "Трек недоступен в вашем регионе или удалён — беру следующий.": (
        "The track is unavailable in your region or removed — taking the next one.",
        "La pista no está disponible en tu región o fue eliminada: paso a la siguiente."),
    "Проблема с ffmpeg: {e}": ("ffmpeg problem: {e}", "Problema con ffmpeg: {e}"),
    "🍪 Использую cookies.txt из папки программы": ("🍪 Using cookies.txt from the program folder",
                                                   "🍪 Uso cookies.txt de la carpeta del programa"),
    "⚠ Не найден Deno/Node — YouTube может не отдавать звук. Перезапустите start-файл.": (
        "⚠ Deno/Node not found — YouTube may refuse to give audio. Run the start file again.",
        "⚠ No se encontró Deno/Node: YouTube puede no entregar el audio. Vuelve a abrir el archivo de inicio."),
    "⚠ поиск: {e}": ("⚠ search: {e}", "⚠ búsqueda: {e}"),
    "YouTube не принимает cookies.txt — похоже, файл устарел. Экспортируйте его заново "
    "(залогиньтесь в YouTube в браузере) или удалите cookies.txt из папки программы.": (
        "YouTube doesn't accept cookies.txt — the file seems outdated. Export it again "
        "(sign in to YouTube in the browser) or delete cookies.txt from the program folder.",
        "YouTube no acepta cookies.txt: parece que el archivo está caducado. Expórtalo de nuevo "
        "(inicia sesión en YouTube en el navegador) o borra cookies.txt de la carpeta del programa."),
    "🍪 YouTube просит подтверждение — беру cookies из браузера {b}…": (
        "🍪 YouTube asks for confirmation — taking cookies from {b}…",
        "🍪 YouTube pide confirmación: tomo las cookies de {b}…"),
    "   ✓ сработало: дальше копаю с cookies {b} (запомню в настройках)": (
        "   ✓ it worked: digging on with {b} cookies (saved in settings)",
        "   ✓ funcionó: sigo con las cookies de {b} (lo guardo en los ajustes)"),
    "   ✕ c cookies {b} YouTube всё равно не пускает": ("   ✕ YouTube still refuses even with {b} cookies",
                                                       "   ✕ YouTube sigue sin dejar pasar con las cookies de {b}"),
    "   – {b}: не нашёл cookies (браузер не установлен или закрыт доступ)": (
        "   – {b}: no cookies found (browser not installed or access denied)",
        "   – {b}: no encontré cookies (navegador no instalado o acceso denegado)"),
    "YouTube не пускает даже с cookies браузера. Откройте music.youtube.com в Firefox и "
    "войдите в свой Google-аккаунт (кнопка «Войти» справа вверху), включите любой трек на "
    "пару секунд, затем снова нажмите «Копать». Если не поможет — подождите 30–60 минут: "
    "YouTube временно ограничил ваш IP.": (
        "YouTube refuses even with browser cookies. Open music.youtube.com in Firefox, "
        "sign in to your Google account (the “Sign in” button at the top right), play any track "
        "for a couple of seconds, then press “Dig” again. If that doesn't help, wait 30–60 minutes: "
        "YouTube has temporarily limited your IP.",
        "YouTube no deja pasar ni con las cookies del navegador. Abre music.youtube.com en Firefox, "
        "inicia sesión con tu cuenta de Google (botón «Acceder» arriba a la derecha), reproduce cualquier "
        "pista un par de segundos y vuelve a pulsar «Excavar». Si no funciona, espera 30–60 minutos: "
        "YouTube ha limitado tu IP temporalmente."),
    "YouTube просит подтвердить, что вы не бот, а cookies браузеров прочитать не удалось. "
    "Откройте music.youtube.com в Firefox, войдите в аккаунт и снова нажмите «Копать». "
    "Запасной вариант — положить файл cookies.txt в папку программы (см. README).": (
        "YouTube asks you to confirm you're not a bot, and browser cookies couldn't be read. "
        "Open music.youtube.com in Firefox, sign in and press “Dig” again. "
        "Plan B: put a cookies.txt file into the program folder (see README).",
        "YouTube pide confirmar que no eres un bot y no se pudieron leer las cookies del navegador. "
        "Abre music.youtube.com en Firefox, inicia sesión y vuelve a pulsar «Excavar». "
        "Plan B: pon un archivo cookies.txt en la carpeta del programa (ver README)."),
    "файл не скачался": ("the file didn't download", "el archivo no se descargó"),
    "в названии": ("in the title", "en el título"),
    "дата релиза": ("release date", "fecha de lanzamiento"),
    "год не подтверждён": ("year not confirmed", "año no confirmado"),
    "слишком старая": ("too old", "demasiado antiguo"),
    "переиздание": ("reissue", "reedición"),
    "запись {y} г.": ("recorded {y}", "grabado en {y}"),
    "год неизвестен": ("year unknown", "año desconocido"),
    "не то (ремикс/лоуфай/бит)": ("not it (remix/lo-fi/beat)", "no sirve (remix/lo-fi/beat)"),
    "не та длина": ("wrong length", "duración incorrecta"),
}
