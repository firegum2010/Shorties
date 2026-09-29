"""
Shorties - Descargador y reproductor de YouTube Shorts offline (estilo TikTok)
Kivy/KivyMD, pensado para compilarse a APK con Buildozer.
"""

import os
import time
import json
import shutil
import socket
import random
import threading
from functools import partial

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.video import Video
from kivy.properties import ListProperty, StringProperty, BooleanProperty
from kivy.metrics import dp

from kivymd.app import MDApp
from kivymd.uix.dialog import MDDialog
from kivymd.uix.button import MDFlatButton

import yt_dlp

# ==========================================
# DETECCIÓN DE ENTORNO (Android vs escritorio)
# ==========================================
ON_ANDROID = "ANDROID_ARGUMENT" in os.environ

if ON_ANDROID:
    from android.permissions import request_permissions, Permission
    from android.storage import app_storage_path


def configurar_directorios():
    """
    En Android usamos el almacenamiento privado de la app (no requiere
    permisos en tiempo de ejecución desde Android 10+, compatible con
    "scoped storage"). En escritorio usamos una carpeta en el home, para
    poder probar la app sin más (Pydroid, PC, etc.)
    """
    if ON_ANDROID:
        base = os.path.join(app_storage_path(), "ShortsAppData")
    else:
        base = os.path.join(os.path.expanduser("~"), "ShortsAppData")
    try:
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        import tempfile
        base = os.path.join(tempfile.gettempdir(), "ShortsAppData")
        os.makedirs(base, exist_ok=True)
        return base


BASE_DIR = configurar_directorios()
TEMP_DIR = os.path.join(BASE_DIR, "Temporales")
PERMANENT_DIR = os.path.join(BASE_DIR, "Favoritos")
CONFIG_FILE = os.path.join(BASE_DIR, "canales.json")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")

# Archivo "de descargas" nativo de yt-dlp: una línea por vídeo ya bajado.
# yt-dlp lo lee solo y se salta esos vídeos - nunca se vuelve a descargar
# algo que ya tienes (esté en Temporales, en Favoritos, o lo hayas borrado).
ARCHIVE_FILE = os.path.join(BASE_DIR, "descargados.txt")

# Historial legible en JSON (título, canal, fecha) de cada vídeo bajado.
HISTORIAL_FILE = os.path.join(BASE_DIR, "historial.json")

# Ruta absoluta al PNG del botón (para que cargue igual sin importar el
# directorio de trabajo desde el que se lance la app).
ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))
FAB_IMAGE = os.path.join(ASSETS_DIR, "fab_gradient.png")

for carpeta in (TEMP_DIR, PERMANENT_DIR, BACKUPS_DIR):
    os.makedirs(carpeta, exist_ok=True)

VIDEO_EXTS = (".mp4", ".mkv", ".webm")

# Cuántos shorts nuevos se bajan por canal en cada ronda: un número al azar
# entre estos dos límites (no lo elige el usuario, varía cada vez).
RANDOM_MIN = 2
RANDOM_MAX = 6
# Cuántos shorts recientes del canal se miran como candidatos para el sorteo.
MAX_CANDIDATOS = 60


# ==========================================
# UTILIDADES
# ==========================================
def obtener_tamanio_carpeta(carpeta):
    total = 0
    if os.path.exists(carpeta):
        for dirpath, _, filenames in os.walk(carpeta):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.isfile(fp):
                    total += os.path.getsize(fp)
    return round(total / (1024 * 1024), 2)


def normalizar_url_shorts(canal_str):
    """Convierte texto plano, @usuario o URLs incompletas en /shorts."""
    canal = canal_str.strip()
    if not canal:
        return ""
    if not canal.startswith(("http://", "https://")):
        if not canal.startswith("@"):
            canal = "@" + canal
        canal = f"https://www.youtube.com/{canal}"
    canal = canal.replace("m.youtube.com", "www.youtube.com")
    canal_base = canal.rstrip("/")
    if not canal_base.endswith("/shorts"):
        canal_base += "/shorts"
    return canal_base


def listar_videos(carpeta):
    if not os.path.exists(carpeta):
        return []
    return [
        os.path.join(carpeta, f)
        for f in sorted(os.listdir(carpeta))
        if f.lower().endswith(VIDEO_EXTS)
    ]


def cargar_historial():
    try:
        if os.path.exists(HISTORIAL_FILE):
            with open(HISTORIAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def guardar_historial(historial):
    try:
        with open(HISTORIAL_FILE, "w", encoding="utf-8") as f:
            json.dump(historial, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def hay_internet(timeout=3):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except Exception:
        return False


def espacio_libre_mb():
    try:
        return round(shutil.disk_usage(BASE_DIR).free / (1024 * 1024))
    except Exception:
        return None


# ==========================================
# DESCARGA: shorts aleatorios, sin repetir, sin ffmpeg
# ==========================================
def descargar_canales(canales, progreso_cb, terminado_cb):
    """
    Se ejecuta en un hilo aparte. Por cada canal:
      1) Mira (sin descargar) sus shorts más recientes.
      2) Descarta los que ya están en el historial.
      3) Sortea una cantidad aleatoria (RANDOM_MIN a RANDOM_MAX) de los que
         queden, elegidos al azar (no siempre los primeros de la lista).
      4) Descarga solo esos, uno por uno.
    progreso_cb(mensaje) da feedback en vivo; terminado_cb(ok, resumen) al
    finalizar todo.
    """
    historial = cargar_historial()
    ids_conocidos = set(historial.keys())

    opts_listado = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": MAX_CANDIDATOS,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
    }

    contador_actual = {"canal": "", "descargados": 0}

    def registrar_en_historial(d):
        if d.get("status") != "finished":
            return
        info = d.get("info_dict") or {}
        vid = info.get("id")
        if vid:
            historial[vid] = {
                "titulo": info.get("title", ""),
                "canal": info.get("uploader") or info.get("channel") or "",
                "fecha_descarga": time.strftime("%Y-%m-%d %H:%M"),
            }
        contador_actual["descargados"] += 1
        progreso_cb(f"{contador_actual['canal']}: {contador_actual['descargados']} nuevo(s)")

    opts_descarga = {
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best",
        "outtmpl": os.path.join(TEMP_DIR, "%(id)s.%(ext)s"),
        "ignoreerrors": "only_download",
        "download_archive": ARCHIVE_FILE,
        "progress_hooks": [registrar_en_historial],
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 60,
        "retries": 5,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
    }

    resultados = []  # (canal, ok, error, cuantos_nuevos)

    for canal in canales:
        canal_objetivo = normalizar_url_shorts(canal)
        contador_actual["canal"] = canal_objetivo
        contador_actual["descargados"] = 0
        progreso_cb(f"Buscando shorts en: {canal_objetivo}")

        candidatos = []
        try:
            with yt_dlp.YoutubeDL(opts_listado) as ydl:
                info = ydl.extract_info(canal_objetivo, download=False)
            for e in (info or {}).get("entries") or []:
                if e and e.get("id") and e["id"] not in ids_conocidos:
                    candidatos.append(e["id"])
        except Exception as e:
            resultados.append((canal_objetivo, False, f"No se pudo revisar el canal: {e}", 0))
            continue

        if not candidatos:
            resultados.append((canal_objetivo, True, "", 0))
            continue

        cantidad = random.randint(RANDOM_MIN, RANDOM_MAX)
        elegidos = random.sample(candidatos, min(cantidad, len(candidatos)))

        ok_alguno = False
        error_canal = ""
        for vid in elegidos:
            url_video = f"https://www.youtube.com/watch?v={vid}"
            for intento in range(2):
                try:
                    with yt_dlp.YoutubeDL(opts_descarga) as ydl:
                        ydl.download([url_video])
                    ok_alguno = True
                    ids_conocidos.add(vid)
                    break
                except Exception as e:
                    error_canal = str(e)
                    time.sleep(2)

        resultados.append((canal_objetivo, ok_alguno or not elegidos, error_canal, contador_actual["descargados"]))

    guardar_historial(historial)

    algun_exito = any(ok for _, ok, _, _ in resultados)
    lineas = []
    for c, ok, err, n in resultados:
        if ok and n:
            lineas.append(f"✔ {c} — {n} nuevo(s)")
        elif ok and not n:
            lineas.append(f"• {c} — sin novedades")
        else:
            lineas.append(f"✘ {c}" + (f"\n   {err[:120]}" if err else ""))
    resumen = "\n".join(lineas)

    libre = espacio_libre_mb()
    if libre is not None and libre < 200:
        resumen += f"\n\n⚠ Poco espacio libre: {libre} MB"

    terminado_cb(algun_exito, resumen)


# ==========================================
# CANALES: guardado en disco + backups automáticos
# ==========================================
MAX_BACKUPS = 10


def cargar_canales():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(c).strip() for c in data if str(c).strip()]
    except Exception:
        pass
    return []


def listar_backups():
    try:
        nombres = sorted(
            f for f in os.listdir(BACKUPS_DIR)
            if f.startswith("canales_") and f.endswith(".json")
        )
        return [os.path.join(BACKUPS_DIR, f) for f in nombres]
    except Exception:
        return []


def guardar_canales(canales):
    """Guarda la lista. Antes de sobrescribir, deja copia del estado anterior."""
    try:
        if os.path.exists(CONFIG_FILE):
            sello = time.strftime("%Y%m%d_%H%M%S")
            shutil.copy2(CONFIG_FILE, os.path.join(BACKUPS_DIR, f"canales_{sello}.json"))
            for viejo in listar_backups()[:-MAX_BACKUPS]:
                try:
                    os.remove(viejo)
                except Exception:
                    pass
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(canales, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ==========================================
# DIÁLOGOS
# ==========================================
def mostrar_dialogo(titulo, texto, on_aceptar=None, texto_aceptar="Aceptar"):
    """Mensaje simple. Si se pasa on_aceptar, muestra también 'Cancelar'."""
    ref = {}

    def cerrar(*_):
        ref["d"].dismiss()

    def aceptar(*_):
        cerrar()
        if on_aceptar:
            on_aceptar()

    botones = []
    if on_aceptar:
        botones.append(MDFlatButton(text="CANCELAR", on_release=cerrar))
    botones.append(MDFlatButton(text=texto_aceptar.upper(), on_release=aceptar))
    ref["d"] = MDDialog(title=titulo, text=texto, buttons=botones)
    ref["d"].open()


# ==========================================
# UI - KV LANGUAGE
# ==========================================
KV = """
#:import dp kivy.metrics.dp

<Card@BoxLayout>:
    orientation: "vertical"
    canvas.before:
        Color:
            rgba: 0.11, 0.11, 0.125, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [22,]
    padding: dp(16)
    spacing: dp(8)

<SectionLabel@Label>:
    color: 0.67, 0.68, 0.92, 1
    font_size: "13sp"
    size_hint_y: None
    height: self.texture_size[1]
    halign: "left"
    text_size: self.width, None
    padding: [0, dp(4)]

<ChannelRow@BoxLayout>:
    canal: ""
    borrar_cb: None
    size_hint_y: None
    height: dp(34)
    spacing: dp(6)
    Label:
        text: root.canal
        color: 0.96, 0.96, 0.96, 1
        font_size: "12sp"
        halign: "left"
        valign: "middle"
        text_size: self.width, None
        shorten: True
    Button:
        text: "x"
        size_hint_x: None
        width: dp(30)
        background_normal: ""
        background_color: 0.12, 0.12, 0.14, 1
        color: 0.94, 0.27, 0.27, 1
        bold: True
        on_release: root.borrar_cb() if root.borrar_cb else None

<HomeScreen>:
    canvas.before:
        Color:
            rgba: 0, 0, 0, 1
        Rectangle:
            pos: self.pos
            size: self.size

    ScrollView:
        id: sv_home
        do_scroll_x: False
        AnchorLayout:
            size_hint_y: None
            height: max(home_content.height, sv_home.height)
            anchor_y: "center"
            BoxLayout:
                id: home_content
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                padding: dp(16)
                spacing: dp(10)

                Label:
                    text: "Shorties"
                    font_size: "26sp"
                    bold: True
                    color: 0.96, 0.96, 0.96, 1
                    size_hint_y: None
                    height: dp(40)
                    halign: "left"
                    text_size: self.width, None

                Card:
                    size_hint_y: None
                    height: dp(70)
                    SectionLabel:
                        text: "ALMACENAMIENTO"
                    Label:
                        id: lbl_storage
                        text: root.texto_almacenamiento
                        color: 0.96, 0.96, 0.96, 1
                        font_size: "13sp"
                        halign: "left"
                        text_size: self.width, None

                Card:
                    size_hint_y: None
                    height: dp(230)
                    SectionLabel:
                        text: "CANALES (usuario o URL)"
                    RecycleView:
                        id: rv_canales
                        viewclass: "ChannelRow"
                        size_hint_y: None
                        height: dp(110)
                        RecycleBoxLayout:
                            default_size: None, dp(36)
                            default_size_hint: 1, None
                            size_hint_y: None
                            height: self.minimum_height
                            orientation: "vertical"

                    BoxLayout:
                        size_hint_y: None
                        height: dp(46)
                        spacing: dp(6)
                        TextInput:
                            id: input_canal
                            hint_text: "@usuario o URL del canal"
                            multiline: False
                            background_color: 0.12, 0.12, 0.14, 1
                            foreground_color: 0.96, 0.96, 0.96, 1
                            cursor_color: 1, 1, 1, 1
                            padding: [dp(10), dp(10)]
                        Button:
                            text: "+"
                            size_hint_x: None
                            width: dp(46)
                            background_normal: ""
                            background_color: 0.15, 0.39, 0.92, 1
                            on_release: root.agregar_canal()

                Card:
                    size_hint_y: None
                    height: dp(56)
                    Label:
                        text: "Cada descarga trae una cantidad al azar de shorts nuevos por canal (no elegidos por ti, ni repetidos)."
                        font_size: "11sp"
                        color: 0.63, 0.63, 0.67, 1
                        halign: "left"
                        valign: "middle"
                        text_size: self.width, None

                AnchorLayout:
                    anchor_x: "center"
                    size_hint_y: None
                    height: dp(112)
                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: dp(100), dp(112)
                        spacing: dp(4)
                        Button:
                            id: btn_descargar
                            text: ""
                            background_normal: app.fab_image
                            background_down: app.fab_image
                            border: 0, 0, 0, 0
                            size_hint: None, None
                            size: dp(84), dp(84)
                            pos_hint: {"center_x": 0.5}
                            on_release: root.iniciar_descarga()
                        Label:
                            text: "Descargando..." if root.descargando else "Iniciar descarga"
                            color: 0.85, 0.85, 0.88, 1
                            font_size: "11sp"
                            size_hint_y: None
                            height: dp(18)

                Label:
                    id: lbl_progreso
                    text: root.texto_progreso
                    color: 0.63, 0.63, 0.67, 1
                    font_size: "11sp"
                    size_hint_y: None
                    height: dp(20) if root.texto_progreso else 0

                SectionLabel:
                    text: "feeds"

                BoxLayout:
                    size_hint_y: None
                    height: dp(56)
                    spacing: dp(10)
                    Button:
                        text: "Feed Temporal"
                        bold: True
                        background_normal: ""
                        background_color: 0.11, 0.11, 0.125, 1
                        color: 0.96, 0.96, 0.96, 1
                        on_release: root.abrir_feed("temp")
                    Button:
                        text: "Favoritos"
                        bold: True
                        background_normal: ""
                        background_color: 0.11, 0.11, 0.125, 1
                        color: 0.98, 0.75, 0.14, 1
                        on_release: root.abrir_feed("fav")

                SectionLabel:
                    text: "mantenimiento"

                BoxLayout:
                    size_hint_y: None
                    height: dp(46)
                    spacing: dp(10)
                    Button:
                        text: "Vaciar temporales"
                        font_size: "11sp"
                        background_normal: ""
                        background_color: 0.11, 0.11, 0.125, 1
                        color: 0.94, 0.55, 0.35, 1
                        on_release: root.vaciar_temporales()
                    Button:
                        text: "Restaurar backup canales"
                        font_size: "11sp"
                        background_normal: ""
                        background_color: 0.11, 0.11, 0.125, 1
                        color: 0.6, 0.75, 0.98, 1
                        on_release: root.restaurar_backup()

<FeedScreen>:
    canvas.before:
        Color:
            rgba: 0, 0, 0, 1
        Rectangle:
            pos: self.pos
            size: self.size

    FloatLayout:
        id: video_holder

        Button:
            text: "< Volver"
            size_hint: None, None
            size: dp(90), dp(40)
            pos_hint: {"x": 0.02, "top": 0.98}
            background_normal: ""
            background_color: 0, 0, 0, 0.55
            on_release: root.volver()

        Label:
            id: lbl_contador
            text: root.texto_contador
            size_hint: None, None
            size: dp(90), dp(30)
            pos_hint: {"right": 0.98, "top": 0.98}
            color: 1, 1, 1, 1
            bold: True

        Label:
            id: lbl_hint
            text: "Toca: pausa/reproduce.  Doble toque: guardar.  Swipe: siguiente"
            size_hint: None, None
            size: dp(300), dp(24)
            pos_hint: {"center_x": 0.5, "y": 0.13}
            color: 1, 1, 1, 0.6
            font_size: "10sp"

        BoxLayout:
            pos_hint: {"center_x": 0.5, "y": 0.04}
            size_hint: 0.92, None
            height: dp(48)
            spacing: dp(10)
            Button:
                id: btn_guardar_fav
                text: root.texto_boton_guardar
                background_normal: ""
                background_color: (0.2, 0.65, 0.35, 0.85) if root.guardado_ahora else (0, 0, 0, 0.55)
                color: 1, 1, 1, 1
                bold: True
                on_release: root.guardar_favorito()
            Button:
                id: btn_eliminar
                text: "Eliminar"
                background_normal: ""
                background_color: 0.55, 0.15, 0.15, 0.85
                color: 1, 1, 1, 1
                bold: True
                on_release: root.eliminar_actual()
"""


# ==========================================
# PANTALLA: INICIO
# ==========================================
class HomeScreen(Screen):
    texto_almacenamiento = StringProperty("")
    texto_progreso = StringProperty("")
    descargando = BooleanProperty(False)

    def on_pre_enter(self, *args):
        self.actualizar_storage()
        self.actualizar_canales()

    def actualizar_storage(self):
        mb_temp = obtener_tamanio_carpeta(TEMP_DIR)
        mb_perm = obtener_tamanio_carpeta(PERMANENT_DIR)
        self.texto_almacenamiento = f"Temporal: {mb_temp} MB   |   Favoritos: {mb_perm} MB"
    def actualizar_canales(self):
        canales = cargar_canales()
        rv = self.ids.rv_canales
        rv.data = [
            {"canal": c, "borrar_cb": partial(self.borrar_canal, c)}
            for c in canales
        ]
        rv.refresh_from_data()

    # ---------- canales ----------
    def agregar_canal(self):
        campo = self.ids.input_canal
        texto = campo.text.strip()
        if not texto:
            return
        canales = cargar_canales()
        nueva = normalizar_url_shorts(texto).lower()
        if any(normalizar_url_shorts(c).lower() == nueva for c in canales):
            mostrar_dialogo("Canal repetido", "Ese canal ya está en la lista.")
            return
        canales.append(texto)
        if not guardar_canales(canales):
            mostrar_dialogo("Error", "No se pudo guardar la lista de canales.")
            return
        campo.text = ""
        self.actualizar_canales()

    def borrar_canal(self, canal):
        canales = cargar_canales()
        if canal in canales:
            canales.remove(canal)
            guardar_canales(canales)
        self.actualizar_canales()

    def restaurar_backup(self):
        backups = listar_backups()
        if not backups:
            mostrar_dialogo("Sin backups", "Todavía no hay copias de seguridad de tus canales.")
            return
        ultimo = backups[-1]

        def hacer():
            try:
                with open(ultimo, "r", encoding="utf-8") as f:
                    canales = json.load(f)
                if not isinstance(canales, list):
                    raise ValueError("Formato inválido")
                guardar_canales([str(c) for c in canales])
                self.actualizar_canales()
                mostrar_dialogo("Listo", f"Se restauraron {len(canales)} canal(es).")
            except Exception as e:
                mostrar_dialogo("Error", f"No se pudo restaurar: {e}")

        mostrar_dialogo(
            "Restaurar backup",
            "Se volverá a la lista de canales anterior al último cambio. "
            "La lista actual también queda guardada como backup.",
            on_aceptar=hacer,
            texto_aceptar="Restaurar",
        )

    # ---------- descarga ----------
    def iniciar_descarga(self):
        if self.descargando:
            return
        canales = cargar_canales()
        if not canales:
            mostrar_dialogo("Sin canales", "Agrega al menos un canal para poder descargar.")
            return
        self.descargando = True
        self.texto_progreso = "Comprobando conexión..."
        threading.Thread(target=self._hilo_descarga, args=(canales,), daemon=True).start()

    def _hilo_descarga(self, canales):
        # Corre en un hilo aparte: toda actualización de la UI va por Clock.
        if not hay_internet():
            self._terminado_seguro(False, "Sin conexión a internet. Conéctate e inténtalo de nuevo.")
            return
        try:
            descargar_canales(canales, self._progreso_seguro, self._terminado_seguro)
        except Exception as e:
            self._terminado_seguro(False, f"Error inesperado: {e}")

    def _progreso_seguro(self, mensaje):
        Clock.schedule_once(lambda dt: setattr(self, "texto_progreso", mensaje))

    def _terminado_seguro(self, ok, resumen):
        Clock.schedule_once(lambda dt: self._fin_descarga(ok, resumen))

    def _fin_descarga(self, ok, resumen):
        self.descargando = False
        self.texto_progreso = ""
        self.actualizar_storage()
        mostrar_dialogo(
            "Descarga terminada" if ok else "Descarga con problemas",
            resumen or "Sin novedades.",
        )

    # ---------- feeds y mantenimiento ----------
    def abrir_feed(self, tipo):
        carpeta = TEMP_DIR if tipo == "temp" else PERMANENT_DIR
        if not listar_videos(carpeta):
            mostrar_dialogo(
                "Sin videos",
                "No hay videos en el feed temporal. Descarga algunos primero."
                if tipo == "temp"
                else "Aún no has guardado favoritos. Dale doble toque a un video para guardarlo.",
            )
            return
        feed = self.manager.get_screen("feed")
        feed.cargar(tipo)
        self.manager.transition = SlideTransition(direction="left")
        self.manager.current = "feed"

    def vaciar_temporales(self):
        def hacer():
            borrados = 0
            for f in os.listdir(TEMP_DIR):
                try:
                    os.remove(os.path.join(TEMP_DIR, f))
                    borrados += 1
                except Exception:
                    pass
            self.actualizar_storage()
            mostrar_dialogo("Listo", f"Se borraron {borrados} archivo(s) temporales.")

        mostrar_dialogo(
            "Vaciar temporales",
            "Se borrarán todos los videos del feed temporal (los Favoritos no se tocan). "
            "No se volverán a descargar.",
            on_aceptar=hacer,
            texto_aceptar="Vaciar",
        )


# ==========================================
# PANTALLA: FEED (estilo TikTok)
# ==========================================
class FeedScreen(Screen):
    texto_contador = StringProperty("")
    texto_boton_guardar = StringProperty("Guardar")
    guardado_ahora = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fuente = "temp"
        self.videos = []
        self.indice = 0
        self.video = None

    # ---------- carga ----------
    def cargar(self, tipo):
        self.fuente = tipo
        carpeta = TEMP_DIR if tipo == "temp" else PERMANENT_DIR
        self.videos = listar_videos(carpeta)
        random.shuffle(self.videos)
        self.indice = 0

    def on_pre_enter(self, *args):
        self.mostrar_actual()

    def on_leave(self, *args):
        self._quitar_video()

    def _quitar_video(self):
        if self.video is not None:
            try:
                self.video.state = "stop"
                self.video.unload()
            except Exception:
                pass
            try:
                self.ids.video_holder.remove_widget(self.video)
            except Exception:
                pass
            self.video = None

    def mostrar_actual(self):
        self._quitar_video()
        if not self.videos:
            self.texto_contador = "0/0"
            return
        self.indice %= len(self.videos)
        ruta = self.videos[self.indice]
        self.video = Video(
            source=ruta,
            state="play",
            options={"eos": "loop"},
            fit_mode="contain",
            size_hint=(1, 1),
            pos_hint={"x": 0, "y": 0},
        )
        holder = self.ids.video_holder
        # index alto = queda al fondo, para que los botones sigan encima
        holder.add_widget(self.video, index=len(holder.children))
        self.texto_contador = f"{self.indice + 1}/{len(self.videos)}"
        self.guardado_ahora = os.path.dirname(ruta) == PERMANENT_DIR
        self.texto_boton_guardar = "Guardado" if self.guardado_ahora else "Guardar"

    # ---------- navegación ----------
    def siguiente(self):
        if len(self.videos) > 1:
            self.indice = (self.indice + 1) % len(self.videos)
            self.mostrar_actual()

    def anterior(self):
        if len(self.videos) > 1:
            self.indice = (self.indice - 1) % len(self.videos)
            self.mostrar_actual()

    def volver(self):
        self._quitar_video()
        if self.manager:
            self.manager.transition = SlideTransition(direction="right")
            self.manager.current = "home"

    # ---------- acciones ----------
    def _alternar_pausa(self):
        if self.video is not None:
            self.video.state = "pause" if self.video.state == "play" else "play"

    def guardar_favorito(self):
        if not self.videos:
            return
        ruta = self.videos[self.indice]
        if os.path.dirname(ruta) == PERMANENT_DIR:
            return  # ya está en Favoritos
        self._quitar_video()  # soltar el archivo antes de moverlo
        try:
            destino = os.path.join(PERMANENT_DIR, os.path.basename(ruta))
            shutil.move(ruta, destino)
            self.videos[self.indice] = destino
        except Exception as e:
            mostrar_dialogo("No se pudo guardar", str(e))
        # se vuelve a cargar el mismo video (ya desde Favoritos, botón en verde)
        self.mostrar_actual()

    def eliminar_actual(self):
        if not self.videos:
            return
        ruta = self.videos[self.indice]
        self._quitar_video()
        try:
            os.remove(ruta)
        except Exception as e:
            mostrar_dialogo("No se pudo eliminar", str(e))
            self.mostrar_actual()
            return
        self.videos.pop(self.indice)
        if not self.videos:
            self.volver()
            mostrar_dialogo("Sin videos", "Ya no quedan videos en este feed.")
            return
        self.mostrar_actual()  # mismo índice = el video que seguía

    # ---------- gestos ----------
    def on_touch_down(self, touch):
        if super().on_touch_down(touch):
            return True  # lo atendió un botón
        touch.ud["feed_inicio"] = (touch.x, touch.y)
        return True

    def on_touch_up(self, touch):
        inicio = touch.ud.get("feed_inicio")
        if inicio is None:
            return super().on_touch_up(touch)
        dx = touch.x - inicio[0]
        dy = touch.y - inicio[1]
        if abs(dy) > dp(60) and abs(dy) > abs(dx):
            # deslizar hacia arriba = siguiente, hacia abajo = anterior
            if dy > 0:
                self.siguiente()
            else:
                self.anterior()
        elif abs(dx) < dp(15) and abs(dy) < dp(15):
            # un toque alterna pausa; el doble toque guarda (y deshace la pausa
            # que provocó el primer toque)
            self._alternar_pausa()
            if touch.is_double_tap:
                self.guardar_favorito()
        return True


# ==========================================
# APP
# ==========================================
class ShortiesApp(MDApp):
    fab_image = StringProperty(FAB_IMAGE)

    def build(self):
        self.title = "Shorties"
        self.theme_cls.theme_style = "Dark"
        Window.clearcolor = (0, 0, 0, 1)
        Builder.load_string(KV)
        sm = ScreenManager(transition=SlideTransition())
        sm.add_widget(HomeScreen(name="home"))
        sm.add_widget(FeedScreen(name="feed"))
        Window.bind(on_keyboard=self.on_tecla)
        return sm

    def on_tecla(self, window, key, *args):
        # 27 = Esc / botón "atrás" de Android
        if key == 27 and self.root.current == "feed":
            self.root.get_screen("feed").volver()
            return True
        return False

    def on_pause(self):
        # Sin esto, Android cierra la app al salir a otra app
        return True

    def on_resume(self):
        pass


if __name__ == "__main__":
    ShortiesApp().run()
