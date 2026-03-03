# pyVideoConverter — Конвертер Canon EOS R50V → Apple ProRes 422 HQ

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![FFmpeg](https://img.shields.io/badge/FFmpeg-Required-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

**Кроссплатформенное Python-приложение** для конвертирования видео с Canon EOS R50V в Apple ProRes 422 HQ с **максимальным качеством и сохранением цветности**.

---

## 🎯 Возможности

- ✅ **Поддержка всех форматов Canon EOS R50V:**
  - XF-HEVC S YCC 4:2:2 10-bit (H.265/HEVC, .mp4) — до 250 Мбит/с
  - XF-HEVC S YCC 4:2:0 8-bit (H.265/HEVC, .mp4)
  - XF-AVC S YCC 4:2:2 10-bit (H.264, .mp4) — до 150 Мбит/с
  - XF-AVC S YCC 4:2:0 8-bit (H.264, .mp4)
  - HDR PQ (H.265, YCbCr 4:2:2, Rec.2020, .mp4)
  - Standard MP4 (H.264, YCbCr 4:2:0, Rec.709, .mp4)

- 🎨 **Автоматическое определение цветового пространства:**
  - Rec.709 → ProRes 422 HQ (прямое копирование)
  - HDR PQ / Rec.2020 → SDR Rec.709 (тональное отображение Hable)
  - Canon C-Log 3 → Rec.709 (расширение гаммы)

- 🖥️ **Удобный графический интерфейс:**
  - Выбор файлов или целой папки
  - Пакетная обработка
  - Real-time прогресс-бар
  - Подробный лог событий

- 🚀 **Кроссплатформенность:** Windows, macOS, Linux

---

## 📦 Требования

- **Python 3.8+**
- **tkinter** (входит в стандартную библиотеку Python)
- **FFmpeg** и **ffprobe** (внешние бинарные файлы)

### Установка FFmpeg

#### Windows
```bash
# Скачайте с https://ffmpeg.org/download.html
# Добавьте путь к ffmpeg/bin в переменную окружения PATH
```

#### macOS (Homebrew)
```bash
brew install ffmpeg
```

#### Linux (Debian/Ubuntu)
```bash
sudo apt update
sudo apt install ffmpeg
```

---

## 🚀 Запуск

```bash
python converter.py
```

---

## 📖 Использование

1. **Добавьте видеофайлы:**
   - Нажмите «Добавить файлы» или «Добавить папку»
   - Приложение автоматически найдёт все видеофайлы

2. **Выберите папку для сохранения:**
   - Нажмите «Выбрать папку»

3. **Настройте параметры:**
   - **Профиль ProRes:** ProRes 422 или ProRes 422 HQ
   - **Цветовое пространство:**
     - Автоопределение (рекомендуется)
     - Принудительно Rec.709
     - HDR → SDR

4. **Нажмите «Начать конвертирование»**

5. **Отслеживайте прогресс** в реальном времени

6. **Откройте папку с результатами** по завершении

---

## 🔬 Технические детали

### Выходной формат

- **Кодек:** Apple ProRes 422 HQ (ffmpeg profile:v 3)
- **Пиксельный формат:** yuv422p10le (10-bit)
- **Контейнер:** .mov (QuickTime)
- **Аудио:** PCM 24-bit (pcm_s24le), сохранение оригинальной частоты дискретизации и каналов

### Обработка цвета

#### Rec.709 (стандарт)
```bash
-color_primaries bt709 -color_trc bt709 -colorspace bt709
```

#### HDR PQ / Rec.2020 → SDR Rec.709
```bash
-vf "zscale=transfer=linear:npl=100,tonemap=hable:desat=0,zscale=transfer=bt709:matrix=bt709:primaries=bt709:range=tv,format=yuv422p10le"
```

#### Canon C-Log 3 → Rec.709
```bash
-vf "zscale=transfer=linear:matrixin=bt709:primariesin=bt709,tonemap=clip:desat=0,zscale=transfer=bt709:matrix=bt709:primaries=bt709:range=tv,format=yuv422p10le"
```

---

## 📁 Структура проекта

```
pyVideoConverter/
├── converter.py    # Основной файл приложения
└── README.md       # Документация
```

### Архитектура кода

- **FFmpegWrapper** — обнаружение бинарных файлов, парсинг метаданных, построение команд
- **ColorAnalyzer** — анализ цветовых метаданных и выбор стратегии обработки
- **ConversionJob** — dataclass для хранения информации о задаче
- **BatchManager** — управление очередью задач, последовательное выполнение
- **ConverterGUI** — графический интерфейс (tkinter)

---

## ⚙️ Параметры командной строки FFmpeg

```bash
ffmpeg -y -i <input> \
  -c:v prores_ks \
  -profile:v 3 \
  -vendor apl0 \
  -bits_per_mb 8000 \
  -pix_fmt yuv422p10le \
  [-vf <color_filters>] \
  -color_primaries bt709 \
  -color_trc bt709 \
  -colorspace bt709 \
  -c:a pcm_s24le \
  -ar <original_rate> \
  <output>.mov
```

---

## 🐞 Отладка

### Проверка установки FFmpeg

```bash
ffmpeg -version
ffprobe -version
```

### Проверка метаданных файла

```bash
ffprobe -v quiet -print_format json -show_streams -show_format <input>.mp4
```

---

## 📝 Лицензия

MIT License — свободное использование, модификация и распространение.

---

## 👤 Автор

**Zakir Alekperov**

GitHub: [@ZakirAlekperov](https://github.com/ZakirAlekperov)

---

## 🙏 Благодарности

- FFmpeg Team — за мощный инструмент обработки видео
- Canon — за отличную камеру EOS R50V
- Python Community — за tkinter и pathlib

---

## 📧 Поддержка

Если у вас возникли вопросы или предложения — создайте [Issue](https://github.com/ZakirAlekperov/pyVideoConverter/issues).
