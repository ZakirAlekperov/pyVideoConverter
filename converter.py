#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pyVideoConverter v1.0.0
=======================
Конвертер видео Canon EOS R50V -> Apple ProRes 422 HQ

Требования:
  - Python 3.8+
  - tkinter (входит в стандартную библиотеку Python)
  - ffmpeg и ffprobe (https://ffmpeg.org/download.html)

Запуск:
  python converter.py

Приложение автоматически определяет формат источника через ffprobe,
строит оптимальную команду FFmpeg с правильными параметрами
цветовой науки и выполняет пакетное конвертирование.
"""

import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional, Tuple

# ===========================================================================
# КОНСТАНТЫ
# ===========================================================================
APP_NAME = "pyVideoConverter — Canon EOS R50V → ProRes 422 HQ"
VERSION = "1.0.0"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v"}
FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"
# Битрейт ProRes 422 HQ при 1080p25 ≈ 220 Мбит/с -> коэффициент для расчёта
PRORes_MBPS_PER_PIXEL_PER_FPS = 220.0 / (1920 * 1080 * 25)


# ===========================================================================
# ПЕРЕЧИСЛЕНИЯ
# ===========================================================================
class ColorMode(Enum):
    """Режим обработки цвета."""
    AUTO = auto()               # Автоопределение
    FORCE_REC709 = auto()       # Принудительно Rec.709
    HDR_TO_SDR = auto()         # Тональное отображение HDR → SDR


class ProResProfile(Enum):
    """Профиль ProRes."""
    PRORES_422 = 2       # Стандартный ProRes 422
    PRORES_422_HQ = 3    # ProRes 422 HQ (максимальное качество)


class JobStatus(Enum):
    """Статус задачи конвертирования."""
    PENDING = "Ожидание"
    RUNNING = "Конвертирование"
    DONE = "Завершено"
    ERROR = "Ошибка"
    CANCELLED = "Отменено"


# ===========================================================================
# ДАТАКЛАССЫ
# ===========================================================================
@dataclass
class StreamMetadata:
    """Метаданные видеопотока, полученные через ffprobe."""
    codec: str = ""
    pix_fmt: str = ""
    colorspace: str = ""
    color_primaries: str = ""
    color_trc: str = ""
    width: int = 0
    height: int = 0
    fps: str = ""
    bitrate: int = 0
    profile: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    audio_codec: str = ""
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    duration: float = 0.0
    nb_frames: int = 0


@dataclass
class ConversionJob:
    """Одна задача конвертирования: источник → выход."""
    src: Path
    dst: Path
    meta: StreamMetadata = field(default_factory=StreamMetadata)
    ffmpeg_args: List[str] = field(default_factory=list)
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    error_msg: str = ""
    src_size_mb: float = 0.0
    est_size_mb: float = 0.0
    t_start: float = 0.0
    t_end: float = 0.0


# ===========================================================================
# АНАЛИЗАТОР ЦВЕТА
# ===========================================================================
class ColorAnalyzer:
    """
    Анализирует метаданные ffprobe и возвращает стратегию обработки цвета
    вместе с флагами FFmpeg.
    """

    CLOG3_MARKERS = {"canon log 3", "c-log3", "clog3", "canon log3"}

    @staticmethod
    def analyze(
        meta: StreamMetadata,
        mode: ColorMode
    ) -> Tuple[str, List[str], List[str]]:
        """
        Возвращает (описание, vf_фильтры, доп_флаги).
        vf_фильтры — список для -vf (объединяются через запятую).
        доп_флаги — дополнительные аргументы ffmpeg (цветовые флаги).
        """
        if mode == ColorMode.HDR_TO_SDR:
            return ColorAnalyzer._hdr_to_sdr()
        if mode == ColorMode.FORCE_REC709:
            return ColorAnalyzer._rec709()

        # Режим AUTO
        if ColorAnalyzer._detect_clog3(meta):
            return ColorAnalyzer._clog3()

        trc = meta.color_trc.lower()
        primaries = meta.color_primaries.lower()
        if "smpte2084" in trc or "bt2020" in primaries or "2020" in primaries:
            return ColorAnalyzer._hdr_to_sdr()

        return ColorAnalyzer._rec709()

    @staticmethod
    def _detect_clog3(meta: StreamMetadata) -> bool:
        """Проверяет наличие Canon C-Log 3 в тегах и профиле."""
        for v in meta.tags.values():
            for marker in ColorAnalyzer.CLOG3_MARKERS:
                if marker in str(v).lower():
                    return True
        if meta.profile:
            for marker in ColorAnalyzer.CLOG3_MARKERS:
                if marker in meta.profile.lower():
                    return True
        return False

    @staticmethod
    def _rec709() -> Tuple[str, List[str], List[str]]:
        """Стратегия Rec.709."""
        desc = "Rec.709 → ProRes 422 HQ (прямое копирование цветового пространства)"
        vf: List[str] = []
        flags = [
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
        ]
        return desc, vf, flags

    @staticmethod
    def _hdr_to_sdr() -> Tuple[str, List[str], List[str]]:
        """Стратегия HDR PQ / Rec.2020 → SDR Rec.709."""
        desc = "HDR PQ / Rec.2020 → SDR Rec.709 (тональное отображение Hable)"
        vf = [
            "zscale=transfer=linear:npl=100",
            "tonemap=hable:desat=0",
            "zscale=transfer=bt709:matrix=bt709:primaries=bt709:range=tv",
            "format=yuv422p10le",
        ]
        flags = [
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
        ]
        return desc, vf, flags

    @staticmethod
    def _clog3() -> Tuple[str, List[str], List[str]]:
        """Стратегия Canon C-Log 3 → Rec.709."""
        desc = "Canon C-Log 3 → Rec.709 (расширение гаммы + преобразование цветового пространства)"
        vf = [
            "zscale=transfer=linear:matrixin=bt709:primariesin=bt709",
            "tonemap=clip:desat=0",
            "zscale=transfer=bt709:matrix=bt709:primaries=bt709:range=tv",
            "format=yuv422p10le",
        ]
        flags = [
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
        ]
        return desc, vf, flags


# ===========================================================================
# ОБЁРТКА FFMPEG
# ===========================================================================
class FFmpegWrapper:
    """
    Управляет бинарными файлами ffmpeg/ffprobe, разбором метаданных
    и построением команд конвертирования.
    """

    DEFAULT_PATHS = {
        "Windows": [
            Path("C:/ffmpeg/bin"),
            Path("C:/Program Files/ffmpeg/bin"),
            Path(os.path.expanduser("~")) / "ffmpeg" / "bin",
        ],
        "Darwin": [
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            Path("/usr/bin"),
        ],
        "Linux": [
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/snap/bin"),
        ],
    }

    def __init__(self):
        self.ffmpeg_bin: Optional[Path] = None
        self.ffprobe_bin: Optional[Path] = None
        self._find_binaries()

    def _find_binaries(self):
        """Ищет ffmpeg и ffprobe в PATH и стандартных директориях."""
        system = platform.system()
        suffix = ".exe" if system == "Windows" else ""

        # Проверяем системный PATH
        ffmpeg_in_path = shutil.which("ffmpeg")
        ffprobe_in_path = shutil.which("ffprobe")

        if ffmpeg_in_path:
            self.ffmpeg_bin = Path(ffmpeg_in_path)
        if ffprobe_in_path:
            self.ffprobe_bin = Path(ffprobe_in_path)

        if self.ffmpeg_bin and self.ffprobe_bin:
            return

        # Проверяем стандартные директории
        dirs = self.DEFAULT_PATHS.get(system, [])
        for d in dirs:
            ff = d / f"ffmpeg{suffix}"
            fp = d / f"ffprobe{suffix}"
            if not self.ffmpeg_bin and ff.is_file():
                self.ffmpeg_bin = ff
            if not self.ffprobe_bin and fp.is_file():
                self.ffprobe_bin = fp
            if self.ffmpeg_bin and self.ffprobe_bin:
                break

    def available(self) -> bool:
        """Возвращает True, если оба бинарных файла найдены."""
        return bool(self.ffmpeg_bin and self.ffprobe_bin)

    def probe(self, path: Path) -> StreamMetadata:
        """Запускает ffprobe и разбирает JSON-метаданные файла."""
        if not self.ffprobe_bin:
            raise RuntimeError("Ошибка: ffprobe не найден")

        cmd = [
            str(self.ffprobe_bin),
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Ошибка ffprobe: {result.stderr}")

        data = json.loads(result.stdout)
        meta = StreamMetadata()

        # Видеопоток
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                meta.codec = stream.get("codec_name", "")
                meta.pix_fmt = stream.get("pix_fmt", "")
                meta.colorspace = stream.get("color_space", "")
                meta.color_primaries = stream.get("color_primaries", "")
                meta.color_trc = stream.get("color_transfer", "")
                meta.width = int(stream.get("width", 0))
                meta.height = int(stream.get("height", 0))
                meta.profile = stream.get("profile", "")
                meta.tags = stream.get("tags", {})
                # FPS
                r = stream.get("r_frame_rate", "25/1")
                try:
                    num, denom = r.split("/")
                    fps_val = float(num) / float(denom)
                    meta.fps = f"{fps_val:.2f}"
                except:
                    meta.fps = "25.00"
                # Число кадров
                nb_str = stream.get("nb_frames", "0")
                try:
                    meta.nb_frames = int(nb_str)
                except:
                    meta.nb_frames = 0
                break

        # Аудиопоток
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                meta.audio_codec = stream.get("codec_name", "")
                meta.audio_sample_rate = int(stream.get("sample_rate", 48000))
                meta.audio_channels = int(stream.get("channels", 2))
                break

        # Длительность
        fmt = data.get("format", {})
        try:
            meta.duration = float(fmt.get("duration", 0.0))
        except:
            meta.duration = 0.0
        try:
            meta.bitrate = int(fmt.get("bit_rate", 0))
        except:
            meta.bitrate = 0

        return meta

    def build_cmd(
        self,
        job: ConversionJob,
        profile: ProResProfile,
        color_mode: ColorMode
    ) -> List[str]:
        """Построение команды ffmpeg для конвертирования."""
        if not self.ffmpeg_bin:
            raise RuntimeError("Ошибка: ffmpeg не найден")

        desc, vf_filters, color_flags = ColorAnalyzer.analyze(job.meta, color_mode)

        cmd: List[str] = [
            str(self.ffmpeg_bin),
            "-y",  # Перезаписывать без запроса
            "-i", str(job.src),
        ]

        # Видео
        cmd += ["-c:v", "prores_ks"]
        cmd += ["-profile:v", str(profile.value)]
        cmd += ["-vendor", "apl0"]
        cmd += ["-bits_per_mb", "8000"]
        cmd += ["-pix_fmt", "yuv422p10le"]

        # Фильтры
        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]

        # Цветовые флаги
        cmd += color_flags

        # Аудио
        cmd += ["-c:a", "pcm_s24le"]
        cmd += ["-ar", str(job.meta.audio_sample_rate)]

        # Выход
        cmd.append(str(job.dst))

        return cmd

    def estimate_output_size(self, job: ConversionJob) -> float:
        """Оценивает размер выходного файла в МБ."""
        if job.meta.duration == 0:
            return 0.0
        fps = float(job.meta.fps) if job.meta.fps else 25.0
        pixels = job.meta.width * job.meta.height
        mbps = pixels * fps * PRORES_MBPS_PER_PIXEL_PER_FPS
        mb_total = (mbps / 8) * job.meta.duration
        return mb_total


# ===========================================================================
# МЕНЕДЖЕР ПАКЕТНОЙ ОБРАБОТКИ
# ===========================================================================
class BatchManager:
    """Управляет очередью задач конвертирования."""

    def __init__(self, ffmpeg: FFmpegWrapper, profile: ProResProfile, color_mode: ColorMode):
        self.ffmpeg = ffmpeg
        self.profile = profile
        self.color_mode = color_mode
        self.jobs: List[ConversionJob] = []
        self.current_job: Optional[ConversionJob] = None
        self.process: Optional[subprocess.Popen] = None
        self.cancelled = False

    def add_job(self, job: ConversionJob):
        """Добавляет задачу в очередь."""
        self.jobs.append(job)

    def cancel(self):
        """Отменяет текущую задачу."""
        self.cancelled = True
        if self.process:
            if platform.system() == "Windows":
                self.process.terminate()
            else:
                self.process.send_signal(signal.SIGTERM)

    def run(
        self,
        progress_callback: callable,
        log_callback: callable,
        done_callback: callable
    ):
        """
        Запускает последовательное выполнение всех задач.
        progress_callback(job, percent) — вызывается при обновлении прогресса
        log_callback(message) — для вывода логов
        done_callback(success) — вызывается по завершении
        """
        self.cancelled = False
        success = True

        for job in self.jobs:
            if self.cancelled:
                job.status = JobStatus.CANCELLED
                log_callback(f"✘ {job.src.name}: Отменено")
                continue

            self.current_job = job
            job.status = JobStatus.RUNNING
            job.t_start = time.time()
            log_callback(f"\n▶ Конвертирование: {job.src.name}")

            try:
                cmd = job.ffmpeg_args
                log_callback(f"  Команда: {' '.join(cmd)}")

                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )

                # Парсинг прогресса ffmpeg
                for line in self.process.stdout:
                    if self.cancelled:
                        break
                    # ffmpeg пишет "frame=  123 fps=..." в stderr
                    match = re.search(r'frame=\s*(\d+)', line)
                    if match and job.meta.nb_frames > 0:
                        frame = int(match.group(1))
                        percent = min(100.0, 100.0 * frame / job.meta.nb_frames)
                        job.progress = percent
                        progress_callback(job, percent)

                self.process.wait()
                job.t_end = time.time()

                if self.cancelled:
                    job.status = JobStatus.CANCELLED
                    log_callback(f"✘ {job.src.name}: Отменено")
                elif self.process.returncode == 0:
                    job.status = JobStatus.DONE
                    job.progress = 100.0
                    elapsed = job.t_end - job.t_start
                    log_callback(f"✔ {job.src.name}: Завершено за {elapsed:.1f} с")
                    progress_callback(job, 100.0)
                else:
                    job.status = JobStatus.ERROR
                    job.error_msg = f"ffmpeg завершился с кодом {self.process.returncode}"
                    log_callback(f"✘ {job.src.name}: Ошибка — {job.error_msg}")
                    success = False

            except Exception as e:
                job.status = JobStatus.ERROR
                job.error_msg = str(e)
                job.t_end = time.time()
                log_callback(f"✘ {job.src.name}: Исключение — {e}")
                success = False

        self.current_job = None
        done_callback(success and not self.cancelled)


# ===========================================================================
# GUI ПРИЛОЖЕНИЯ
# ===========================================================================
class ConverterGUI:
    """Главное окно приложения."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1000x750")

        self.ffmpeg = FFmpegWrapper()
        self.batch: Optional[BatchManager] = None
        self.input_files: List[Path] = []
        self.output_dir: Optional[Path] = None

        self._setup_ui()
        self._check_ffmpeg()

    def _setup_ui(self):
        """Создание всех элементов интерфейса."""
        # === ВЕРХНЯЯ ПАНЕЛЬ: ввод файлов ===
        frame_input = ttk.LabelFrame(self.root, text="1. Выберите видеофайлы", padding=10)
        frame_input.pack(fill="x", padx=10, pady=5)

        ttk.Button(
            frame_input, text="Добавить файлы", command=self._select_files
        ).pack(side="left", padx=5)
        ttk.Button(
            frame_input, text="Добавить папку", command=self._select_folder
        ).pack(side="left", padx=5)
        ttk.Button(
            frame_input, text="Очистить список", command=self._clear_input
        ).pack(side="left", padx=5)

        self.lbl_input = ttk.Label(frame_input, text="Ни одного файла не выбрано", foreground="gray")
        self.lbl_input.pack(side="left", padx=10)

        # === ПАНЕЛЬ: выбор выходной папки ===
        frame_output = ttk.LabelFrame(self.root, text="2. Выберите папку для сохранения", padding=10)
        frame_output.pack(fill="x", padx=10, pady=5)

        ttk.Button(
            frame_output, text="Выбрать папку", command=self._select_output_dir
        ).pack(side="left", padx=5)

        self.lbl_output = ttk.Label(frame_output, text="Не выбрана", foreground="gray")
        self.lbl_output.pack(side="left", padx=10)

        # === НАСТРОЙКИ ===
        frame_settings = ttk.LabelFrame(self.root, text="3. Настройки", padding=10)
        frame_settings.pack(fill="x", padx=10, pady=5)

        # Профиль ProRes
        ttk.Label(frame_settings, text="Профиль ProRes:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.var_profile = tk.StringVar(value="ProRes 422 HQ")
        combo_profile = ttk.Combobox(
            frame_settings,
            textvariable=self.var_profile,
            values=["ProRes 422", "ProRes 422 HQ"],
            state="readonly",
            width=20
        )
        combo_profile.grid(row=0, column=1, sticky="w", padx=5, pady=5)

        # Режим обработки цвета
        ttk.Label(frame_settings, text="Цветовое пространство:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.var_color = tk.StringVar(value="Автоопределение")
        combo_color = ttk.Combobox(
            frame_settings,
            textvariable=self.var_color,
            values=["Автоопределение", "Принудительно Rec.709", "HDR → SDR"],
            state="readonly",
            width=30
        )
        combo_color.grid(row=1, column=1, sticky="w", padx=5, pady=5)

        # === КНОПКИ УПРАВЛЕНИЯ ===
        frame_controls = ttk.Frame(self.root)
        frame_controls.pack(fill="x", padx=10, pady=10)

        self.btn_start = ttk.Button(
            frame_controls, text="▶ Начать конвертирование", command=self._start_conversion
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_cancel = ttk.Button(
            frame_controls, text="■ Отменить", command=self._cancel, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=5)

        self.btn_open_output = ttk.Button(
            frame_controls, text="📂 Открыть папку с результатами", command=self._open_output_folder, state="disabled"
        )
        self.btn_open_output.pack(side="right", padx=5)

        # === ПРОГРЕСС-БАР ===
        frame_progress = ttk.LabelFrame(self.root, text="Прогресс", padding=10)
        frame_progress.pack(fill="x", padx=10, pady=5)

        self.progressbar = ttk.Progressbar(frame_progress, mode="determinate", maximum=100)
        self.progressbar.pack(fill="x", pady=5)

        self.lbl_progress = ttk.Label(frame_progress, text="Готов к работе")
        self.lbl_progress.pack()

        # === ЛОГ ===
        frame_log = ttk.LabelFrame(self.root, text="Журнал событий", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(frame_log, height=15, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _check_ffmpeg(self):
        """Проверка наличия ffmpeg/ffprobe."""
        if not self.ffmpeg.available():
            self._log("✘ Ошибка: ffmpeg или ffprobe не найдены!")
            self._log(f"Скачайте с {FFMPEG_DOWNLOAD_URL}")
            messagebox.showerror(
                "Ошибка",
                f"ffmpeg/ffprobe не найдены.\n\n"
                f"Скачайте и установите ffmpeg:\n{FFMPEG_DOWNLOAD_URL}"
            )
        else:
            self._log(f"✔ ffmpeg найден: {self.ffmpeg.ffmpeg_bin}")
            self._log(f"✔ ffprobe найден: {self.ffmpeg.ffprobe_bin}")

    def _log(self, msg: str):
        """Добавляет сообщение в лог."""
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _select_files(self):
        """Открывает диалог выбора файлов."""
        files = filedialog.askopenfilenames(
            title="Выберите видеофайлы",
            filetypes=[("Video files", "*" + " *".join(VIDEO_EXTENSIONS))]
        )
        for f in files:
            p = Path(f)
            if p.suffix.lower() in VIDEO_EXTENSIONS and p not in self.input_files:
                self.input_files.append(p)
        self._update_input_label()

    def _select_folder(self):
        """Открывает диалог выбора папки с видео."""
        folder = filedialog.askdirectory(title="Выберите папку с видео")
        if not folder:
            return
        for item in Path(folder).rglob("*"):
            if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                if item not in self.input_files:
                    self.input_files.append(item)
        self._update_input_label()

    def _clear_input(self):
        """Очищает список входных файлов."""
        self.input_files.clear()
        self._update_input_label()

    def _update_input_label(self):
        """Обновляет надпись с количеством файлов."""
        if not self.input_files:
            self.lbl_input.config(text="Ни одного файла не выбрано", foreground="gray")
        else:
            self.lbl_input.config(text=f"Выбрано файлов: {len(self.input_files)}", foreground="black")

    def _select_output_dir(self):
        """Открывает диалог выбора выходной папки."""
        folder = filedialog.askdirectory(title="Выберите папку для сохранения")
        if folder:
            self.output_dir = Path(folder)
            self.lbl_output.config(text=str(self.output_dir), foreground="black")

    def _open_output_folder(self):
        """Открывает выходную папку в проводнике."""
        if not self.output_dir:
            return
        if platform.system() == "Windows":
            os.startfile(self.output_dir)
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(self.output_dir)])
        else:
            subprocess.run(["xdg-open", str(self.output_dir)])

    def _cancel(self):
        """Отменяет конвертирование."""
        if self.batch:
                    self._log("\nПолучен запрос на отмену...")
            self.batch.cancel()
            self.btn_cancel.config(state="disabled")

    def _start_conversion(self):
        """Запускает процесс конвертирования."""
        # Проверка входных данных
        if not self.ffmpeg.available():
            messagebox.showerror("Ошибка", "ffmpeg/ffprobe не найдены")
            return

        if not self.input_files:
            messagebox.showwarning("Предупреждение", "Не выбрано ни одного файла для конвертирования")
            return

        if not self.output_dir:
            messagebox.showwarning("Предупреждение", "Не выбрана папка для сохранения")
            return

        # Определение параметров
        profile_str = self.var_profile.get()
        if profile_str == "ProRes 422":
            profile = ProResProfile.PRORES_422
        else:
            profile = ProResProfile.PRORES_422_HQ

        color_str = self.var_color.get()
        if color_str == "Принудительно Rec.709":
            color_mode = ColorMode.FORCE_REC709
        elif color_str == "HDR → SDR":
            color_mode = ColorMode.HDR_TO_SDR
        else:
            color_mode = ColorMode.AUTO
          
        self._log("\n" + "="*70)
        self._log("НАЧАЛО КОНВЕРТИРОВАНИЯ")
        self._log(f"Профиль: {profile_str}")
        self._log(f"Цвет: {color_str}")
        self._log(f"Файлов: {len(self.input_files)}")
        self._log("="*70 + "\n")

        # Создание задач
        jobs: List[ConversionJob] = []
        for src in self.input_files:
            dst = self.output_dir / f"{src.stem}_prores.mov"
            job = ConversionJob(src=src, dst=dst)

            # Анализ метаданных
            try:
                self._log(f"  Анализ {src.name}...")
                job.meta = self.ffmpeg.probe(src)
                self._log(f"  ✔ Кодек: {job.meta.codec}, {job.meta.width}x{job.meta.height}, {job.meta.fps} fps")
                self._log(f"  ✔ Цвет: {job.meta.colorspace}/{job.meta.color_primaries}/{job.meta.color_trc}")
            except Exception as e:
                self._log(f"  ✖ Ошибка анализа {src.name}: {e}")
                messagebox.showerror("Ошибка", f"Не удалось проанализировать {src.name}: {e}")
                return

            jobs.append(job)

        # Обновление UI с прогрессом
              self.progressbar["value"] = 0
        self.lbl_progress.config(text=f"Готов: 0/{len(jobs)}", fg="blue")

        # Создание и запуск пакета
        self.batch = ConversionBatch(
            ffmpeg_wrapper=self.ffmpeg,
            jobs=jobs,
            profile=profile,
            color_mode=color_mode,
            progress_callback=self._on_progress,
            done_callback=self._on_done,
            finish_callback=self._finish
        )
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.btn_open_output.config(state="disabled")
        self.batch.start()

    def _on_progress(self, percent: float):
        """Обновление UI с прогрессом."""  
        self.progressbar["value"] = percent
        self.lbl_progress.config(text=f"Прогресс: {percent:.1f}%")
    def _on_done(self, success: bool):
        """Вызывается после завершения всех задач."""
        self.root.after(0, lambda: self._finish(suПрогресс

    def _finish(self, success: bool):
        """Разблокировка UI после конвертирования."""
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.btn_open_output.config(state="normal")

        if success:
            self._log("\n" + "="*70)
            self._log("✔ ВСЕ ЗАДАЧИ УСПЕШНО ЗАВЕРШЕНЫ!")
            self._log("="*70)
            self.lbl_progress.config(text="Завершено")
            messagebox.showinfo("Готово", "Конвертирование успешно завершено!")
        else:
            self._log("\n" + "="*70)
            self._log("✖ Конвертирование отменено или завершено с ошибками")
            self._log("="*70)
            self.lbl_progress.config(text="Отменено/Ошибка")


# ========================================================================
# ТОЧКА ВХОДА
# ========================================================================

def main():
    """Точка входа приложения."""
    root = tk.Tk()
    app = ConverterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

    
