name: Build APK

on:
  push:
    branches: [ main ]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-22.04
    steps:
      - name: Descargar el código del repo
        uses: actions/checkout@v4

      - name: Instalar Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Instalar dependencias del sistema
        run: |
          sudo apt-get update
          sudo apt-get install -y git zip unzip openjdk-17-jdk autoconf libtool pkg-config \
            zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev

      - name: Cachear buildozer
        uses: actions/cache@v4
        with:
          path: |
            .buildozer
            ~/.buildozer
          key: buildozer-${{ hashFiles('buildozer.spec') }}
          restore-keys: |
            buildozer-

      - name: Instalar Buildozer
        run: |
          pip install --upgrade pip
          pip install buildozer "cython<3"

      - name: Compilar APK
        run: buildozer -v android debug

      - name: Subir el APK
        uses: actions/upload-artifact@v4
        with:
          name: shorties-apk
          path: bin/*.apk
