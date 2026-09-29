[app]

title = Shorties
package.name = shorties
package.domain = org.personal

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 1.0

# Dependencias del proyecto. ffpyplayer permite a Kivy reproducir vídeo
# sin necesitar ffmpeg del sistema (no disponible en Android).
requirements = python3,kivy==2.3.0,kivymd==1.2.0,yt-dlp,certifi,requests,ffpyplayer,pyjnius,android,urllib3,websockets,mutagen

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/fab_gradient.png

# Permisos: solo Internet, ya que guardamos los vídeos en el
# almacenamiento privado de la app (no requiere permisos de storage).
android.permissions = INTERNET

android.api = 33
android.minapi = 24
android.ndk = 25b
android.accept_sdk_license = True
android.archs = arm64-v8a
p4a.branch = v2024.01.21

# targetSdk alto + almacenamiento privado de la app evita todos los líos
# de "scoped storage" / FileProvider de Android 10+.
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
