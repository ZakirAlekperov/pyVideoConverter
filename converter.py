# Требования: Python 3.8+, tkinter (встроен в stdlib), бинарный файл ffmpeg
"""
Canon EOS R50V → Apple ProRes 422 HQ Конвертер
================================================
Использование:
    python converter.py

Приложение предоставляет графический интерфейс для конвертации видеофайлов
с камеры Canon EOS R50V в формат Apple ProRes 422 HQ с сохранением
максимального качества и точности цветопередачи.

Требования:
    - Python 3.8+
    - tkinter (входит в стандартную библиотеку Python)
    - ffmpeg и ffprobe (установленные в системе или указанные вручную)
      Скачать: https://ffmpeg.org/download.html
"""

from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
APP_NAME = "Canon → ProRes Конвертер"
APP_VERSION = "1.0.0"
SUPPORTED_EXTENSIONS = {".mp4", ".MP4", ".mov", ".MOV", ".mxf", ".MXF"}

PRORES_PROFILES = {
    "ProRes 422 Standard (профиль 2)": 2,
    "ProRes 422 HQ (профиль 3)": 3,
}

COLOR_MODES = [
    "Автоопределение",
    "Принудительно Rec.709",
    "Принудительно HDR→SDR (тонмэппинг)",
]

# Примерный битрейт ProRes 422 HQ при 1080p25 ~ 220 Мбит/с
# Формула: bits_per_mb * mb_per_frame * fps
# Используем упрощённую оценку: 0.36 байт на пиксель на кадр для HQ
PRORES_BYTES_PER_PIXEL_PER_FRAME = {
    2: 0.27,   # 422 Standard
    3: 0.36,   # 422 HQ
}


# ---------------------------------------------------------------------------
# FFmpegWrapper
# ---------------------------------------------------------------------------
class FFmpegWrapper:
    """Обнаружение бинарных файлов ffmpeg/ffprobe, разбор метаданных,
    построение команд и выполнение подпроцессов."""

    COMMON_PATHS: Dict[str, List[str]] = {
        "Windows": [
            r"C:\ffmpeg\bin",
            r"C:\Program Files\ffmpeg\bin",
            r"C:\Tools\ffmpeg\bin",
        ],
        "Darwin": [
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "/opt/local/bin",
        ],
        "Linux": [
            "/usr/bin",
            "/usr/local/bin",
            "/snap/bin",
        ],
    }

    def __init__(self) -> None:
        self.ffmpeg_path: Optional[Path] = None
        self.ffprobe_path: Optional[Path] = None
        self._find_binaries()

    def _find_binaries(self) -> None:
        system = platform.system()
        exe_suffix = ".exe" if system == "Windows" else ""

        # Сначала проверяем PATH
        ffmpeg_in_path = shutil.which("ffmpeg")
        ffprobe_in_path = shutil.which("ffprobe")

        if ffmpeg_in_path:
            self.ffmpeg_path = Path(ffmpeg_in_path)
        if ffprobe_in_path:
            self.ffprobe_path = Path(ffprobe_in_path)

        if self.ffmpeg_path and self.ffprobe_path:
            return

        # Проверяем стандартные пути
        candidates = self.COMMON_PATHS.get(system, [])
        for dir_str in candidates:
            dir_path = Path(dir_str)
            ff = dir_path / f"ffmpeg{exe_suffix}"
            fp = dir_path / f"ffprobe{exe_suffix}"
            if not self.ffmpeg_path and ff.is_file():
                self.ffmpeg_path = ff
            if not self.ffprobe_path and fp.is_file():
                self.ffprobe_path = fp
            if self.ffmpeg_path and self.ffprobe_path:
                break

    def is_available(self) -> bool:
        return self.ffmpeg_path is not None and self.ffprobe_path is not None

    def get_missing_info(self) -> str:
        missing = []
        if not self.ffmpeg_path:
            missing.append("ffmpeg")
        if not self.ffprobe_path:
            missing.append("ffprobe")
        return (
            f"Не найдены бинарные файлы: {', '.join(missing)}.\n"
            "Скачайте ffmpeg с https://ffmpeg.org/download.html\n"
            "и добавьте путь к бинарным файлам в системную переменную PATH."
        )

    def probe(self, file_path: Path) -> Optional[Dict]:
        """Запускает ffprobe и возвращает словарь с метаданными в формате JSON."""
        if not self.ffprobe_path:
            return None
        cmd = [
            str(self.ffprobe_path),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None

    def build_ffmpeg_command(
        self,
        source: Path,
        output: Path,
        prores_profile: int,
        color_flags: List[str],
        vf_filter: Optional[str],
        audio_sample_rate: int,
        audio_channels: int,
    ) -> List[str]:
        """Формирует список аргументов команды ffmpeg."""
        assert self.ffmpeg_path is not None
        cmd: List[str] = [
            str(self.ffmpeg_path),
            "-y",                 # перезапись без вопросов
            "-i", str(source),
            "-c:v", "prores_ks",
            "-profile:v", str(prores_profile),
            "-vendor", "apl0",
            "-bits_per_mb", "8000",
        ]

        if vf_filter:
            cmd += ["-vf", vf_filter]

        cmd += ["-pix_fmt", "yuv422p10le"]
        cmd += color_flags

        # Сохраняем частоту кадров, разрешение и SAR точно
        cmd += ["-fps_mode", "passthrough"]
        cmd += ["-sws_flags", "lanczos"]

        # Аудио: PCM 24-bit
        cmd += [
            "-c:a", "pcm_s24le",
            "-ar", str(audio_sample_rate),
            "-ac", str(audio_channels),
        ]

        cmd += [str(output)]
        return cmd

    def run_conversion(
        self,
        cmd: List[str],
        duration_seconds: float,
        progress_callback: Callable[[float], None],
        log_callback: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> bool:
        """Запускает ffmpeg, разбирает прогресс из stderr, возвращает успех."""
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log_callback(f"Ошибка запуска ffmpeg: {exc}")
            return False

        stderr_lines: List[str] = []

        def read_stderr():
            assert process.stderr is not None
            for line in process.stderr:
                line = line.rstrip()
                stderr_lines.append(line)
                # Разбор прогресса: ffmpeg пишет "time=HH:MM:SS.ms"
                if "time=" in line:
                    time_str = _extract_ffmpeg_time(line)
                    if time_str is not None and duration_seconds > 0:
                        elapsed = _time_str_to_seconds(time_str)
                        pct = min(elapsed / duration_seconds * 100, 99.9)
                        progress_callback(pct)
                elif line.strip():
                    log_callback(line)

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()

        # Ожидаем завершения или отмены
        while process.poll() is None:
            if cancel_event.is_set():
                _terminate_process(process)
                reader.join(timeout=3)
                log_callback("Конвертация отменена пользователем.")
                return False
            time.sleep(0.1)

        reader.join(timeout=5)
        retcode = process.returncode

        if retcode != 0 and not cancel_event.is_set():
            log_callback(f"ffmpeg завершился с кодом {retcode}.")
            # Выводим последние строки stderr для диагностики
            for ln in stderr_lines[-10:]:
                log_callback(ln)
            return False

        progress_callback(100.0)
        return retcode == 0


# ---------------------------------------------------------------------------
# Вспомогательные функции для разбора ffmpeg
# ---------------------------------------------------------------------------
def _extract_ffmpeg_time(line: str) -> Optional[str]:
    idx = line.find("time=")
    if idx == -1:
        return None
    part = line[idx + 5:].split()[0]
    return part


def _time_str_to_seconds(time_str: str) -> float:
    """Преобразует HH:MM:SS.ms в секунды."""
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        return float(time_str)
    except (ValueError, IndexError):
        return 0.0


def _terminate_process(process: subprocess.Popen) -> None:
    try:
        if platform.system() == "Windows":
            process.terminate()
        else:
            process.send_signal(signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


# ---------------------------------------------------------------------------
# ColorMetadataAnalyzer
# ---------------------------------------------------------------------------
@dataclass
class ColorStrategy:
    description: str
    color_flags: List[str]
    vf_filter: Optional[str]


class ColorMetadataAnalyzer:
    """Анализирует вывод ffprobe и определяет стратегию цветовой обработки."""

    # Известные идентификаторы Canon C-Log3 в тегах потока
    CLOG3_TAGS = {"Canon Log 3", "canon-log-3", "clog3", "Canon Log3"}

    def analyze(
        self,
        probe_data: Dict,
        force_mode: str,
    ) -> ColorStrategy:
        video_stream = self._get_video_stream(probe_data)
        if video_stream is None:
            return self._rec709_strategy()

        if force_mode == "Принудительно Rec.709":
            return self._rec709_strategy()

        if force_mode == "Принудительно HDR→SDR (тонмэппинг)":
            return self._hdr_tonemap_strategy()

        # Автоопределение
        color_trc = video_stream.get("color_trc", "")
        color_primaries = video_stream.get("color_primaries", "")
        colorspace = video_stream.get("color_space", "")
        tags = video_stream.get("tags", {})

        # Проверка C-Log3
        for tag_val in tags.values():
            if isinstance(tag_val, str) and tag_val in self.CLOG3_TAGS:
                return self._clog3_strategy()

        # Проверка HDR PQ
        if color_trc in ("smpte2084", "arib-std-b67", "smpte428") or \
           color_primaries in ("bt2020"):
            return self._hdr_tonemap_strategy()

        # По умолчанию Rec.709
        return self._rec709_strategy()

    def _get_video_stream(self, probe_data: Dict) -> Optional[Dict]:
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                return stream
        return None

    def _rec709_strategy(self) -> ColorStrategy:
        return ColorStrategy(
            description="Rec.709 — передача цветовых метаданных без изменений",
            color_flags=[
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
            ],
            vf_filter=None,
        )

    def _hdr_tonemap_strategy(self) -> ColorStrategy:
        vf = (
            "zscale=transfer=linear,"
            "tonemap=hable,"
            "zscale=transfer=bt709:matrix=bt709:primaries=bt709,"
            "format=yuv422p10le"
        )
        return ColorStrategy(
            description="HDR PQ (Rec.2020) → SDR (Rec.709) тонмэппинг через zscale+hable",
            color_flags=[
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
            ],
            vf_filter=vf,
        )

    def _clog3_strategy(self) -> ColorStrategy:
        # Canon C-Log3: применяем colorspace с гамма-расширением
        vf = (
            "colorspace=iall=bt709:itrc=log316:iprimaries=bt709:"
            "all=bt709:trc=bt709:primaries=bt709,"
            "format=yuv422p10le"
        )
        return ColorStrategy(
            description="Canon C-Log3 → Rec.709: применяется гамма-расширение colorspace",
            color_flags=[
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
            ],
            vf_filter=vf,
        )


# ---------------------------------------------------------------------------
# ConversionJob
# ---------------------------------------------------------------------------
@dataclass
class ConversionJob:
    source: Path
    output: Path
    probe_data: Optional[Dict]
    color_strategy: Optional[ColorStrategy]
    ffmpeg_cmd: List[str] = field(default_factory=list)
    status: str = "ожидание"  # ожидание / обработка / готово / ошибка / отменено
    error_message: str = ""
    duration_seconds: float = 0.0
    estimated_size_mb: float = 0.0

    def format_metadata_summary(self) -> str:
        if not self.probe_data:
            return "Метаданные недоступны"
        lines = [f"Источник: {self.source.name}"]
        for stream in self.probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                lines.append(
                    f"  Видео: {stream.get('codec_name','?')} "
                    f"{stream.get('width','?')}x{stream.get('height','?')} "
                    f"{stream.get('r_frame_rate','?')} fps"
                )
                lines.append(
                    f"  Цветовое пространство: {stream.get('color_space','?')}, "
                    f"TRC: {stream.get('color_trc','?')}, "
                    f"Примари: {stream.get('color_primaries','?')}"
                )
                lines.append(
                    f"  Формат пикселей: {stream.get('pix_fmt','?')}, "
                    f"Битрейт: {_format_bitrate(stream.get('bit_rate'))}"
                )
            elif stream.get("codec_type") == "audio":
                lines.append(
                    f"  Аудио: {stream.get('codec_name','?')} "
                    f"{stream.get('sample_rate','?')} Гц, "
                    f"{stream.get('channels','?')} кан."
                )
        if self.color_strategy:
            lines.append(f"  Стратегия цвета: {self.color_strategy.description}")
        lines.append(f"  Оценочный размер вывода: {self.estimated_size_mb:.1f} МБ")
        return "\n".join(lines)


def _format_bitrate(br_str: Optional[str]) -> str:
    if not br_str:
        return "неизвестно"
    try:
        br = int(br_str)
        return f"{br // 1_000_000} Мбит/с"
    except ValueError:
        return br_str


# ---------------------------------------------------------------------------
# BatchManager
# ---------------------------------------------------------------------------
class BatchManager:
    """Управляет очередью задач конвертации и выполняет их последовательно."""

    def __init__(
        self,
        ffmpeg: FFmpegWrapper,
        analyzer: ColorMetadataAnalyzer,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.analyzer = analyzer
        self.jobs: List[ConversionJob] = []
        self._cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def prepare_jobs(
        self,
        sources: List[Path],
        output_dir: Path,
        prores_profile: int,
        color_mode: str,
    ) -> List[str]:
        """Создаёт задания, запускает ffprobe, возвращает список предупреждений."""
        self.jobs.clear()
        warnings: List[str] = []

        for src in sources:
            probe = self.ffmpeg.probe(src)
            if probe is None:
                warnings.append(f"Пропущен (ошибка ffprobe): {src.name}")
                continue

            video_stream = _get_video_stream(probe)
            if video_stream is None:
                warnings.append(f"Пропущен (видеопоток не найден): {src.name}")
                continue

            audio_stream = _get_audio_stream(probe)
            sample_rate = int(audio_stream.get("sample_rate", 48000)) if audio_stream else 48000
            channels = int(audio_stream.get("channels", 2)) if audio_stream else 2

            duration = _get_duration(probe)
            color_strategy = self.analyzer.analyze(probe, color_mode)

            output_path = output_dir / (src.stem + ".mov")

            est_size = _estimate_output_size(
                video_stream, duration, prores_profile
            )

            cmd = self.ffmpeg.build_ffmpeg_command(
                source=src,
                output=output_path,
                prores_profile=prores_profile,
                color_flags=color_strategy.color_flags,
                vf_filter=color_strategy.vf_filter,
                audio_sample_rate=sample_rate,
                audio_channels=channels,
            )

            job = ConversionJob(
                source=src,
                output=output_path,
                probe_data=probe,
                color_strategy=color_strategy,
                ffmpeg_cmd=cmd,
                duration_seconds=duration,
                estimated_size_mb=est_size,
            )
            self.jobs.append(job)

        return warnings

    def check_disk_space(self, output_dir: Path) -> Optional[str]:
        total_needed = sum(j.estimated_size_mb for j in self.jobs) * 1024 * 1024
        try:
            free = shutil.disk_usage(output_dir).free
            if total_needed > free * 0.95:
                needed_gb = total_needed / 1024**3
                free_gb = free / 1024**3
                return (
                    f"Недостаточно места на диске!\n"
                    f"Требуется: ~{needed_gb:.1f} ГБ, "
                    f"Свободно: {free_gb:.1f} ГБ"
                )
        except OSError:
            pass
        return None

    def start(
        self,
        job_progress_cb: Callable[[int, float], None],
        job_done_cb: Callable[[int, bool], None],
        batch_done_cb: Callable[[int, int], None],
        log_cb: Callable[[str], None],
    ) -> None:
        self._cancel_event.clear()
        self._thread = threading.Thread(
            target=self._run_all,
            args=(job_progress_cb, job_done_cb, batch_done_cb, log_cb),
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _run_all(
        self,
        job_progress_cb: Callable[[int, float], None],
        job_done_cb: Callable[[int, bool], None],
        batch_done_cb: Callable[[int, int], None],
        log_cb: Callable[[str], None],
    ) -> None:
        success_count = 0
        for idx, job in enumerate(self.jobs):
            if self._cancel_event.is_set():
                job.status = "отменено"
                continue

            job.status = "обработка"
            log_cb(f"\n{'='*50}")
            log_cb(f"Конвертация [{idx+1}/{len(self.jobs)}]: {job.source.name}")
            log_cb(job.format_metadata_summary())
            log_cb(f"Команда: {' '.join(job.ffmpeg_cmd)}")
            log_cb(f"{'='*50}")

            ok = self.ffmpeg.run_conversion(
                cmd=job.ffmpeg_cmd,
                duration_seconds=job.duration_seconds,
                progress_callback=lambda pct, i=idx: job_progress_cb(i, pct),
                log_callback=log_cb,
                cancel_event=self._cancel_event,
            )

            if ok:
                job.status = "готово"
                success_count += 1
                log_cb(f"✓ Готово: {job.output.name}")
            elif self._cancel_event.is_set():
                job.status = "отменено"
            else:
                job.status = "ошибка"
                log_cb(f"✗ Ошибка: {job.source.name}")

            job_done_cb(idx, ok)

        batch_done_cb(success_count, len(self.jobs))


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _get_video_stream(probe: Dict) -> Optional[Dict]:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def _get_audio_stream(probe: Dict) -> Optional[Dict]:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def _get_duration(probe: Dict) -> float:
    fmt = probe.get("format", {})
    try:
        return float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        return 0.0


def _estimate_output_size(
    video_stream: Dict,
    duration_seconds: float,
    prores_profile: int,
) -> float:
    """Возвращает оценочный размер файла в МБ."""
    width = int(video_stream.get("width", 1920))
    height = int(video_stream.get("height", 1080))
    fps_str = video_stream.get("r_frame_rate", "25/1")
    fps = _parse_fps(fps_str)
    bpp = PRORES_BYTES_PER_PIXEL_PER_FRAME.get(prores_profile, 0.36)
    total_bytes = width * height * bpp * fps * duration_seconds
    # Добавляем 10% на аудио и метаданные
    total_bytes *= 1.10
    return total_bytes / (1024 * 1024)


def _parse_fps(fps_str: str) -> float:
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return float(num) / float(den)
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 25.0


# ---------------------------------------------------------------------------
# ConverterGUI
# ---------------------------------------------------------------------------
class ConverterGUI:
    """Главное окно приложения на tkinter."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.resizable(True, True)
        self.root.minsize(780, 600)

        self.ffmpeg = FFmpegWrapper()
        self.analyzer = ColorMetadataAnalyzer()
        self.batch_manager = BatchManager(self.ffmpeg, self.analyzer)

        self._gui_queue: queue.Queue = queue.Queue()
        self._is_running = False

        self._build_ui()
        self._check_ffmpeg()
        self._poll_gui_queue()

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=8)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        self._build_input_section(main)
        self._build_settings_section(main)
        self._build_progress_section(main)
        self._build_log_section(main)
        self._build_action_bar(main)

    def _build_input_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Файлы источника", padding=6)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        frame.columnconfigure(1, weight=1)

        ttk.Button(frame, text="Добавить файлы…", command=self._add_files).grid(
            row=0, column=0, padx=(0, 4)
        )
        ttk.Button(frame, text="Добавить папку…", command=self._add_folder).grid(
            row=0, column=1, sticky="w", padx=(0, 4)
        )
        ttk.Button(frame, text="Очистить список", command=self._clear_files).grid(
            row=0, column=2
        )

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        list_frame.columnconfigure(0, weight=1)

        self.file_listbox = tk.Listbox(
            list_frame, height=5, selectmode=tk.EXTENDED,
            font=("Courier", 10), bg="#1e1e1e", fg="#d4d4d4",
            selectbackground="#264f78"
        )
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_listbox.yview)
        self.file_listbox.config(yscrollcommand=sb.set)
        self.file_listbox.grid(row=0, column=0, sticky="ew")
        sb.grid(row=0, column=1, sticky="ns")

        # Вывод папки
        out_frame = ttk.Frame(frame)
        out_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        out_frame.columnconfigure(1, weight=1)

        ttk.Label(out_frame, text="Папка вывода:").grid(row=0, column=0, padx=(0, 4))
        self.output_var = tk.StringVar()
        ttk.Entry(out_frame, textvariable=self.output_var).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(out_frame, text="Обзор…", command=self._choose_output).grid(
            row=0, column=2, padx=(4, 0)
        )

    def _build_settings_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Настройки конвертации", padding=6)
        frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="Профиль ProRes:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.profile_var = tk.StringVar(value="ProRes 422 HQ (профиль 3)")
        profile_cb = ttk.Combobox(
            frame, textvariable=self.profile_var,
            values=list(PRORES_PROFILES.keys()), state="readonly", width=36
        )
        profile_cb.grid(row=0, column=1, sticky="w")

        ttk.Label(frame, text="Режим цвета:").grid(
            row=0, column=2, sticky="w", padx=(16, 8)
        )
        self.color_mode_var = tk.StringVar(value="Автоопределение")
        color_cb = ttk.Combobox(
            frame, textvariable=self.color_mode_var,
            values=COLOR_MODES, state="readonly", width=36
        )
        color_cb.grid(row=0, column=3, sticky="w")

    def _build_progress_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Прогресс", padding=6)
        frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Текущий файл:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.file_progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.file_progress.grid(row=0, column=1, sticky="ew")
        self.file_progress_label = ttk.Label(frame, text="0%", width=6)
        self.file_progress_label.grid(row=0, column=2, padx=(4, 0))

        ttk.Label(frame, text="Всего:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(4, 0))
        self.batch_progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.batch_progress.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        self.batch_progress_label = ttk.Label(frame, text="0%", width=6)
        self.batch_progress_label.grid(row=1, column=2, padx=(4, 0), pady=(4, 0))

        self.status_label = ttk.Label(frame, text="Готов к работе", foreground="#888")
        self.status_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_log_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Журнал", padding=6)
        frame.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            frame, height=12, state=tk.DISABLED,
            font=("Courier", 9), bg="#1e1e1e", fg="#d4d4d4",
            wrap=tk.WORD
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _build_action_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.grid(row=4, column=0, sticky="ew")

        self.start_btn = ttk.Button(
            bar, text="▶  Начать конвертацию", command=self._start_conversion
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 6))

        self.cancel_btn = ttk.Button(
            bar, text="✖  Отменить", command=self._cancel_conversion, state=tk.DISABLED
        )
        self.cancel_btn.grid(row=0, column=1, padx=(0, 6))

        self.open_btn = ttk.Button(
            bar, text="📂  Открыть папку вывода", command=self._open_output_folder,
            state=tk.DISABLED
        )
        self.open_btn.grid(row=0, column=2)

        # Информация о ffmpeg
        self.ffmpeg_label = ttk.Label(bar, text="", foreground="#888", font=("", 8))
        self.ffmpeg_label.grid(row=0, column=3, padx=(16, 0))

    # ------------------------------------------------------------------
    # Проверка ffmpeg
    # ------------------------------------------------------------------
    def _check_ffmpeg(self) -> None:
        if self.ffmpeg.is_available():
            self.ffmpeg_label.config(
                text=f"ffmpeg: {self.ffmpeg.ffmpeg_path}",
                foreground="#4ec9b0"
            )
            self._log("ffmpeg обнаружен: " + str(self.ffmpeg.ffmpeg_path))
            self._log("ffprobe обнаружен: " + str(self.ffmpeg.ffprobe_path))
        else:
            self.ffmpeg_label.config(text="ffmpeg не найден!", foreground="#f44747")
            self._log("ВНИМАНИЕ: " + self.ffmpeg.get_missing_info())
            self.start_btn.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Обработчики файлов
    # ------------------------------------------------------------------
    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Выберите видеофайлы",
            filetypes=[
                ("Видеофайлы", "*.mp4 *.MP4 *.mov *.MOV *.mxf *.MXF"),
                ("Все файлы", "*.*"),
            ],
        )
        for p in paths:
            self._add_to_list(Path(p))

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку с видеофайлами")
        if not folder:
            return
        folder_path = Path(folder)
        added = 0
        for ext in SUPPORTED_EXTENSIONS:
            for f in folder_path.rglob(f"*{ext}"):
                self._add_to_list(f)
                added += 1
        self._log(f"Добавлено файлов из папки: {added}")

    def _add_to_list(self, path: Path) -> None:
        current = self.file_listbox.get(0, tk.END)
        if str(path) not in current:
            self.file_listbox.insert(tk.END, str(path))

    def _clear_files(self) -> None:
        self.file_listbox.delete(0, tk.END)

    def _choose_output(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку для сохранения файлов")
        if folder:
            self.output_var.set(folder)

    # ------------------------------------------------------------------
    # Конвертация
    # ------------------------------------------------------------------
    def _start_conversion(self) -> None:
        if not self.ffmpeg.is_available():
            messagebox.showerror("Ошибка", self.ffmpeg.get_missing_info())
            return

        sources_str = self.file_listbox.get(0, tk.END)
        if not sources_str:
            messagebox.showwarning("Предупреждение", "Не выбрано ни одного файла.")
            return

        output_str = self.output_var.get().strip()
        if not output_str:
            messagebox.showwarning("Предупреждение", "Не указана папка для вывода файлов.")
            return

        output_dir = Path(output_str)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            messagebox.showerror("Ошибка", f"Нет прав на запись в папку:\n{output_dir}")
            return

        sources = [Path(s) for s in sources_str]
        profile_name = self.profile_var.get()
        prores_profile = PRORES_PROFILES.get(profile_name, 3)
        color_mode = self.color_mode_var.get()

        self._log("\n" + "="*50)
        self._log("Анализ файлов источника через ffprobe…")
        warnings = self.batch_manager.prepare_jobs(
            sources, output_dir, prores_profile, color_mode
        )
        for w in warnings:
            self._log(f"⚠ {w}")

        if not self.batch_manager.jobs:
            messagebox.showerror("Ошибка", "Ни один файл не прошёл валидацию ffprobe.")
            return

        # Вывод метаданных
        for job in self.batch_manager.jobs:
            self._log(job.format_metadata_summary())

        # Проверка места на диске
        disk_warn = self.batch_manager.check_disk_space(output_dir)
        if disk_warn:
            if not messagebox.askyesno("Предупреждение о месте", disk_warn + "\n\nПродолжить?"):
                return

        total = len(self.batch_manager.jobs)
        self.batch_progress.config(maximum=total * 100)
        self.batch_progress["value"] = 0

        self._is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.open_btn.config(state=tk.DISABLED)
        self.status_label.config(text=f"Конвертация 0/{total}…", foreground="#9cdcfe")

        self.batch_manager.start(
            job_progress_cb=self._on_job_progress,
            job_done_cb=self._on_job_done,
            batch_done_cb=self._on_batch_done,
            log_cb=self._log_thread_safe,
        )

    def _cancel_conversion(self) -> None:
        self.batch_manager.cancel()
        self.status_label.config(text="Отмена…", foreground="#f44747")

    # ------------------------------------------------------------------
    # Callbacks из рабочего потока (через очередь)
    # ------------------------------------------------------------------
    def _on_job_progress(self, job_idx: int, pct: float) -> None:
        self._gui_queue.put(("job_progress", job_idx, pct))

    def _on_job_done(self, job_idx: int, success: bool) -> None:
        self._gui_queue.put(("job_done", job_idx, success))

    def _on_batch_done(self, success_count: int, total: int) -> None:
        self._gui_queue.put(("batch_done", success_count, total))

    def _log_thread_safe(self, msg: str) -> None:
        self._gui_queue.put(("log", msg))

    def _poll_gui_queue(self) -> None:
        """Опрашивает очередь GUI каждые 80 мс."""
        try:
            while True:
                item = self._gui_queue.get_nowait()
                self._handle_gui_event(item)
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll_gui_queue)

    def _handle_gui_event(self, item: tuple) -> None:
        event_type = item[0]

        if event_type == "log":
            self._log(item[1])

        elif event_type == "job_progress":
            _, job_idx, pct = item
            total = len(self.batch_manager.jobs)
            self.file_progress["value"] = pct
            self.file_progress_label.config(text=f"{pct:.0f}%")
            overall = job_idx * 100 + pct
            self.batch_progress["value"] = overall
            overall_pct = overall / (total * 100) * 100 if total else 0
            self.batch_progress_label.config(text=f"{overall_pct:.0f}%")

        elif event_type == "job_done":
            _, job_idx, success = item
            total = len(self.batch_manager.jobs)
            done = job_idx + 1
            self.status_label.config(
                text=f"Конвертация {done}/{total}…",
                foreground="#9cdcfe"
            )

        elif event_type == "batch_done":
            _, success_count, total = item
            self._is_running = False
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.open_btn.config(state=tk.NORMAL)
            if success_count == total:
                self.status_label.config(
                    text=f"Завершено! Успешно: {success_count}/{total}",
                    foreground="#4ec9b0"
                )
            else:
                self.status_label.config(
                    text=f"Завершено с ошибками: {success_count}/{total} успешно",
                    foreground="#f44747"
                )
            self.file_progress["value"] = 100
            self.file_progress_label.config(text="100%")
            self.batch_progress["value"] = total * 100
            self.batch_progress_label.config(text="100%")

    # ------------------------------------------------------------------
    # Вспомогательные методы GUI
    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _open_output_folder(self) -> None:
        output_str = self.output_var.get().strip()
        if not output_str:
            return
        path = Path(output_str)
        if not path.exists():
            messagebox.showwarning("Предупреждение", "Папка вывода не существует.")
            return
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(str(path))
            elif system == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{exc}")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def main() -> None:
    root = tk.Tk()

    # Тёмная тема
    style = ttk.Style(root)
    available = style.theme_names()
    if "clam" in available:
        style.theme_use("clam")

    style.configure(".", background="#252526", foreground="#d4d4d4")
    style.configure("TFrame", background="#252526")
    style.configure("TLabelframe", background="#252526", foreground="#d4d4d4")
    style.configure("TLabelframe.Label", background="#252526", foreground="#d4d4d4")
    style.configure("TLabel", background="#252526", foreground="#d4d4d4")
    style.configure(
        "TButton",
        background="#3c3c3c", foreground="#d4d4d4",
        focusthickness=1, relief="flat"
    )
    style.map("TButton", background=[("active", "#505050")])
    style.configure("TEntry", fieldbackground="#3c3c3c", foreground="#d4d4d4")
    style.configure("TCombobox", fieldbackground="#3c3c3c", foreground="#d4d4d4")
    style.configure("Horizontal.TProgressbar", troughcolor="#3c3c3c", background="#007acc")

    app = ConverterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
