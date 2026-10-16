CRATEDIGGER 1.2.2 — excavar samples para boom bap
=================================================
(Русский: README.txt · English: README_EN.txt)

El programa «excava en las cajas» por sí solo: busca en YouTube Music pistas de
1960–1999 (por tus etiquetas o al azar), las descarga, encuentra las partes más estables
y corta 3 loops de cada pista. Cada loop:
  * está cortado justo en los tiempos fuertes y se repite sin clics ni desfases:
    los bordes caen en el inicio real del golpe (incluso con ataque suave o flam),
    y el loop terminado se comprueba: si el inicio del siguiente golpe se coló al final,
    el final se mueve y el loop se corta de nuevo; si no se arregla, el loop de batería
    se descarta y los demás terminan con un fundido suave;
  * está ajustado a un tempo múltiplo de 5 BPM (60, 65 … 120);
  * lleva la tonalidad y el tempo en el nombre, por ejemplo  Gmaj110Quark1.wav


CÓMO ABRIRLO
------------
Windows:  doble clic en START_WINDOWS.bat
          (si Windows avisa «protegió su PC» — «Más información» → «Ejecutar de todas formas»)
macOS:    clic derecho en START_MAC.command → «Abrir» → «Abrir»
          (así la primera vez; después basta con doble clic)
Linux:    bash START_LINUX.sh

La primera vez el programa instala Python (en Windows) y las bibliotecas necesarias
en esta misma carpeta: 1–3 minutos, hace falta internet. Después arranca en segundos.
Se abre la ventana del programa. No cierres la ventana negra de la consola mientras excavas.
Descomprime el archivo en una carpeta normal (no lo ejecutes directamente desde el zip).

Idioma de la interfaz: las banderas encima del nombre del programa, arriba a la izquierda:
inglés, ruso, español. La elección se recuerda. El registro y los archivos credits.txt /
MODEL_SAMPLES.txt de las cajas nuevas se escriben en el mismo idioma.


CÓMO SE USA
-----------
1. Escribe etiquetas: género, instrumento, año, ambiente; por ejemplo
     soul, rhodes, 1973        jazz funk flute        italian library 70s
   Puedes pulsar las etiquetas listas debajo del campo. Vacío = discos al azar.
2. Pulsa «Excavar». Los discos aparecen a medida que están listos.
3. El botón ▶ reproduce el loop en bucle. Puedes encender varios a la vez:
   los siguientes entran justo al inicio del compás y siguen el tempo del primero,
   así compruebas en la misma ventana cómo suenan juntos.
   Al pasar el ratón por un loop se marcan los loops de tonalidad compatible.
4. ★ — Favoritos: una copia del loop va a la carpeta Favorites. Otro clic quita la
   estrella y la copia. Todo lo que alguna vez marcaste está reunido en la caja
   «★ Favoritos», la primera de la lista «Añadir una caja anterior».
   Para dejar en pantalla solo los favoritos: «Limpiar pantalla» y luego «★ Favoritos».
   La papelera quita un loop de la pantalla (el archivo se queda en la carpeta; justo
   después del clic puedes «Deshacer»).
   Puedes arrastrar un loop con el ratón (por la tonalidad, el nombre o la onda) a una
   carpeta del Explorador, del Finder o al escritorio: allí se copia su WAV.
   Esto funciona en una ventana de Chrome/Edge (así se abre el programa solo); Firefox
   no lo permite.
   El disquete junto a la papelera guarda el WAV del loop en tu ordenador cuando
   arrastrar no funciona: Chrome/Edge preguntan dónde guardarlo (y recuerdan la última
   carpeta), otros navegadores lo dejan en la carpeta de descargas.
   Cada excavación nueva se añade debajo de las anteriores: todo lo que excavaste
   mientras el programa está abierto sigue a la vista (también al recargar la ventana).
   «Añadir una caja anterior» arriba devuelve a la pantalla excavaciones antiguas,
   «Limpiar pantalla» quita todo de la pantalla (los archivos del disco no se tocan).
   «Descargar todo en ZIP» abajo: todos los loops de la pantalla en un archivo para Model:Samples:
     90/Amin/Amin90Name1.wav
     110/G#maj/G#maj110Other2.wav
   (una carpeta por BPM y dentro una por tonalidad; son los mismos archivos que suenan en
   pantalla, estéreo 24 bits si se guardaron). Descomprímelo y pasa las carpetas enteras con
   Elektron Transfer: él mismo convierte el estéreo a mono.
5. Puedes soltar en la ventana tus propios mp3/wav/flac (por ejemplo, rips de vinilo):
   se cortan igual.
6. Reprocesar: mantén pulsado el botón izquierdo del ratón sobre la portada o el título de un
   disco de la lista de la derecha y arrástralo a la columna izquierda (puedes soltarlo en
   cualquier parte; la columna se ilumina con un marco azul). El disco se corta de nuevo con
   los ajustes elegidos a la izquierda: por ejemplo «Pitching» en vez de «Stretching»,
   «Solo: bajo» en vez de «Cualquiera» o 1 compás en vez de 2. El resultado aparece como
   una caja aparte «Reproceso: …»; los loops anteriores se quedan donde estaban.
   El original se toma de la carpeta de la caja (Sources_mp3), así que el programa no
   contacta con YouTube; si no está allí, descarga la pista otra vez. Con tus propios archivos
   funciona si está activado «Guardar la pista original» en «Más ajustes».

Ajustes útiles:
  * «Breaks» — partes donde la batería va al frente; «Melodía» — partes con más armonía.
  * «Solo:» batería / bajo / voz / pads (para loops de 1–2 compases) — partes donde suena
    solo eso. Puedes elegir varios: funcionan como «o» (por ejemplo, batería + bajo:
    de cada disco salen tanto breaks limpios como bajo solo). Elegir algo en esta fila
    desactiva «Cualquiera / Breaks / Melodía»; un clic en la primera fila quita todos los «Solo».
    Orden: primero las partes limpias de todos los tipos elegidos (batería → bajo → voz → pads),
    luego las «con extras»; lo indica la descripción del loop («bajo con extras»).
      batería — solo batería, sin bajo, acordes ni voz (sin sonidos con serie de
                armónicos; entre golpes el nivel cae casi al silencio);
      bajo    — la línea de bajo al frente, sin bombo ni caja (incluidos breaks
                oscuros y filtrados); «con extras» — con percusión ligera, FX,
                un acorde o una voz de fondo;
      voz     — una voz (un tono claro por encima de 160 Hz) sin ritmo; «con extras» —
                con bajo ligero, coro, instrumentos o percusión;
      pads    — acordes sostenidos y fondos sin bajo, percusión ni melodía principal
                (un piano empapado de reverb también es pad; una melodía nota a nota no).
                Se admite un dron grave bajo el pad.
    Los pads y la voz también se toman de intros sin ritmo y de pistas totalmente sin ritmo
    (ambient): esa parte se corta a 1–2 compases exactos al BPM más frecuente de esta
    excavación, sin estirar, con una unión sin clic. En «Stretching» la voz se ajusta a la
    rejilla todo lo necesario (la altura no cambia), en «Pitching», hasta ±15 %.
    La tonalidad en el nombre de batería, bajo y voz es la de la canción (con una sola línea
    no se puede saber el modo); en los pads, la del propio loop.
    Estas partes son raras: un disco puede dar 1–2 loops y la búsqueda tarda más.
  * «Buscar rarezas» (casilla bajo el campo de etiquetas) — primero los discos con menos
    reproducciones (hasta 150 mil; si no aparece nada raro, el límite sube poco a poco).
    El número de reproducciones se ve en cada disco.
  * «Qué archivos guardar» (en «Más ajustes») — por defecto solo estéreo 24 bits
    (Elektron Transfer lo pasa a mono solo). Puedes añadir archivos mono de 16 bits
    listos para Model:Samples; entonces también sirve «Ordenar los archivos mono para
    Model:Samples en carpetas» —
    ModelSamples_mono16/Amin/095 BPM/Amin95Quark1.wav, etc.
  * «Stretching» cambia el tempo y conserva la altura (los golpes quedan en su sitio).
    «Pitching» — como el pitch de un tocadiscos o una MPC: el tempo cambia junto con la
    altura, normalmente menos de un semitono (la voz, hasta ±15 %). La tonalidad del nombre se recalcula.
  * Años y BPM: la casilla junto al título activa o desactiva el filtro.
    Años desactivados: se buscan grabaciones de cualquier año (también modernas);
    BPM desactivado: las pistas no se descartan por tempo (el ajuste a múltiplos de 5
    dentro de 60–120 se mantiene). Duración del loop: 1, 2 o 4 compases.


VELOCIDAD Y MEMORIA
-------------------
La excavación funciona como una cadena: mientras se descarga una pista, las ya descargadas
se analizan en paralelo en procesos separados. Hay tantos procesos como núcleos menos uno
(uno queda para la ventana y el reproductor, para que el jam no se entrecorte), pero no más
de tres y menos si el ordenador tiene poca memoria; funcionan con prioridad baja. Un análisis
ocupa unos 0,2–0,35 GB de memoria. Las solicitudes a YouTube siguen yendo estrictamente de una
en una, con pausas aleatorias, y el programa no descarga más de lo necesario.
Ante una comprobación de bot o «demasiadas solicitudes» la red se detiene al momento y los
discos ya descargados se terminan. «Detener» lo interrumpe todo al instante.
Tus propios archivos y los reprocesos también se analizan en paralelo. Si los procesos no
arrancan o fallan, el programa pasa solo a trabajar en un único hilo.

QUÉ SE OBTIENE
--------------
Music/CrateDigger/<fecha>_<etiquetas>/
  Stereo_24bit/          loops para el DAW (48 kHz, 24 bits, estéreo)
  ModelSamples_mono16/   los mismos loops en mono 16 bits (si está activado en «Más ajustes»)
  Sources_mp3/           las pistas originales completas (de ellas sale el reproceso)
  Chops/                 cada loop cortado en 8 partes (si está activado)
  MODEL_SAMPLES.txt      tabla: tempo, compases y pasos de cada loop
  credits.txt            de dónde sale cada loop (artista, enlace, tiempo en la pista)

Nombre del archivo: tonalidad + BPM + título + número de loop:  Amin95Quark2.wav
Los loops de «Solo …» llevan una letra de tipo tras el BPM: D — batería, B — bajo,
V — voz, P — pads:  Amin95BQuark1.wav, Cmin80DKyoto2.wav
Los loops se guardan en WAV y no en mp3: el mp3 siempre añade silencio al principio y al
final, y el loop deja de ser exacto.


MODEL:SAMPLES — JAM
-------------------
1. Pasa los loops con Elektron Transfer (lo más cómodo: «Descargar todo en ZIP»
   y carpetas enteras; él mismo convierte el estéreo a mono).
2. Tempo del patrón = el número del nombre del archivo.
3. Longitud de la pista = el número de pasos de MODEL_SAMPLES.txt (2 compases = 32 pasos), trig en el paso 1.
   El loop se reinicia en cada ciclo y nunca se desfasa.
4. Los loops miden compases exactos, así que la perilla START los divide en partes iguales
   (0, 16, 32 … 112 ≈ octavos). Distintos START en distintos pasos = re-chop, como en una MPC.
5. Para el jam conviene estrechar el BPM (por ejemplo 90–90): así todos los loops
   caben en un proyecto sin cambiar de tempo.
6. La tonalidad y el código Camelot (8A, 10B…) indican qué combina:
   el mismo número o uno vecino (8A + 8B, 8A + 9A, 8A + 7A).


SI ALGO NO FUNCIONA
-------------------
* «Confirma que no eres un bot». El programa toma solo las cookies de Firefox
  (si no está, de Edge/Chrome/Safari) y repite la misma pista: para YouTube la solicitud
  parece venir de tu navegador. El navegador que funcionó se recuerda.
  Si YouTube aun así no deja pasar:
    1) abre music.youtube.com en Firefox e inicia sesión con tu cuenta de Google
       (botón «Acceder» arriba a la derecha), reproduce cualquier pista un par de segundos;
    2) vuelve a pulsar «Excavar».
  Plan B: la extensión «cookies.txt» para Firefox → en la página music.youtube.com
  púlsala y guarda el archivo como cookies.txt directamente en la carpeta del programa
  (junto al README). El programa lo detecta solo.
  Si nada funciona, YouTube ha limitado tu IP temporalmente: espera una hora.
* «429 / demasiadas solicitudes» — espera 10–30 minutos.
* No se encuentra nada — cambia las etiquetas, amplía los años, desactiva la comprobación estricta del año.
* No se abrió la ventana — abre en el navegador la dirección de la ventana negra (http://127.0.0.1:8765).
* Reinstalación completa: borra la carpeta .venv junto al programa y ábrelo de nuevo.
* yt-dlp se actualiza solo una vez al día (YouTube cambia cosas a menudo).


SOBRE LOS DERECHOS
------------------
Los loops salen de grabaciones ajenas. Para practicar, hacer beats y tocar en casa es la
práctica habitual del digging. Si publicas un tema con un sample, hay que licenciarlo con
el titular de los derechos; credits.txt guarda todo lo que necesitarás para ello.
Descargar de YouTube puede infringir sus condiciones: la decisión es tuya.
