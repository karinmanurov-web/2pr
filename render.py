"""
Скрипт покадрового рендеринга HTML-анимации в видеофайл (.mp4) 1920x1080.
Использует Playwright (headless Chromium) для отрисовки каждого кадра через window.renderAt(t)
и FFmpeg для прямого кодирования потока кадров в видео без промежуточных файлов на диске.

Установка зависимостей:
    pip install playwright
    playwright install chromium
    (также в системе должен быть установлен ffmpeg и добавлен в PATH)

Пример запуска:
    python render.py --episode episodes/ep01_apelsin.json --out out/apelsin.mp4 --fps 30
    python render.py --episode episodes/ep02_test.json --audio voice/ep02.mp3 --out out/ep02.mp4 --fps 30
"""

import argparse
import base64
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from playwright.sync_api import sync_playwright


def render_video(
    html_path: Path,
    episode_path: Path,
    output_path: Path,
    fps: int = 30,
    crf: int = 18,
    audio_path: Optional[Path] = None,
) -> None:
    """
    Выполняет покадровый рендер HTML-страницы с загруженным JSON-эпизодом в MP4-файл.

    Args:
        html_path: Путь к исходному HTML-файлу движка.
        episode_path: Путь к JSON-файлу конкретного выпуска.
        output_path: Путь для сохранения итогового видеофайла.
        fps: Частота кадров (30 или 60).
        crf: Качество H.264 (16-20 — визуально без потерь, меньше число = выше качество).
        audio_path: Необязательный путь к аудиофайлу озвучки для подмешивания в итоговый MP4.
    """
    if not html_path.exists():
        raise FileNotFoundError(f"HTML-файл не найден: {html_path.resolve()}")
    if not episode_path.exists():
        raise FileNotFoundError(f"Файл эпизода не найден: {episode_path.resolve()}")
    if audio_path is not None and not audio_path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {audio_path.resolve()}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Читаем JSON эпизода и кодируем в base64 для безопасной передачи через URL без CORS-ограничений file://
    episode_raw = episode_path.read_text(encoding="utf-8")
    json.loads(episode_raw)  # Проверяем валидность JSON перед запуском браузера
    episode_b64 = base64.urlsafe_b64encode(episode_raw.encode("utf-8")).decode("ascii")

    # Формируем URL с параметром ?export=1 и данными эпизода
    file_url = f"{html_path.resolve().as_uri()}?export=1&data={episode_b64}"

    print(f"🎬 Запуск рендера: {episode_path.name} -> {output_path.name} ({fps} FPS)")
    if audio_path is not None:
        print(f"🔊 Подмешиваем аудио: {audio_path.name}")

    with sync_playwright() as p:
        # Запускаем Chromium в безголовом режиме с фиксированным окном 1920x1080
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
        )
        page = context.new_page()

        try:
            page.goto(file_url, wait_until="networkidle")

            # Получаем общую длительность ролика из JS-движка
            duration_ms = page.evaluate("() => window.getDurationMs()")
            if not duration_ms or duration_ms <= 0:
                raise ValueError("Функция window.getDurationMs() вернула некорректную длительность.")

            total_frames = math.ceil((duration_ms / 1000.0) * fps)
            frame_step_ms = 1000.0 / fps

            print(f"⏱ Длительность: {duration_ms / 1000:.2f} сек | Всего кадров: {total_frames}")

            # Команда FFmpeg для приёма потока JPEG-кадров через stdin и сборки H.264 MP4
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",                        # Перезаписывать выходной файл без подтверждения
                "-loglevel", "error",
                "-f", "image2pipe",          # Чтение последовательности изображений из потока
                "-vcodec", "mjpeg",          # Формат входных кадров (быстрее PNG при сохранении качества)
                "-framerate", str(fps),      # Частота кадров входного потока
                "-i", "-",                   # Вход из stdin
            ]

            if audio_path is not None:
                ffmpeg_cmd.extend(["-i", str(audio_path.resolve())])

            ffmpeg_cmd.extend([
                "-c:v", "libx264",           # Кодек H.264 (совместим с YouTube и любыми монтажными программами)
                "-pix_fmt", "yuv420p",       # Стандартное цветовое пространство для YouTube/плееров
                "-crf", str(crf),            # Постоянный фактор качества
                "-preset", "medium",         # Баланс скорости кодирования и сжатия
            ])

            if audio_path is not None:
                ffmpeg_cmd.extend([
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                ])

            ffmpeg_cmd.extend([
                "-movflags", "+faststart",   # Оптимизация метаданных для быстрого старта воспроизведения
                str(output_path.resolve()),
            ])

            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            start_time = time.time()

            try:
                for frame_idx in range(total_frames):
                    current_ms = frame_idx * frame_step_ms

                    # Переводим сцену в точное состояние для текущего кадра
                    page.evaluate(f"window.renderAt({current_ms})")

                    # Снимаем кадр в буфер памяти (quality=95 даёт отличную чёткость текста и вектора)
                    img_bytes = page.screenshot(type="jpeg", quality=95)
                    ffmpeg_proc.stdin.write(img_bytes)

                    # Выводим прогресс каждые 15 кадров или на последнем кадре
                    if (frame_idx + 1) % 15 == 0 or (frame_idx + 1) == total_frames:
                        percent = ((frame_idx + 1) / total_frames) * 100
                        elapsed = time.time() - start_time
                        render_fps = (frame_idx + 1) / max(elapsed, 0.001)
                        sys.stdout.write(
                            f"\r⏳ Кадр {frame_idx + 1}/{total_frames} ({percent:5.1f}%) | "
                            f"Скорость: {render_fps:4.1f} кадр/с"
                        )
                        sys.stdout.flush()

                print()  # Перевод строки после завершения прогресс-бара

            finally:
                # Корректно закрываем поток ввода FFmpeg и дожидаемся окончания кодирования
                if ffmpeg_proc.stdin:
                    ffmpeg_proc.stdin.close()
                stderr_output = ffmpeg_proc.stderr.read().decode("utf-8", errors="replace")
                return_code = ffmpeg_proc.wait()

                if return_code != 0:
                    raise RuntimeError(f"FFmpeg завершился с ошибкой (код {return_code}):\n{stderr_output}")

            total_elapsed = time.time() - start_time
            print(f"✅ Готово! Видео сохранено в {output_path.resolve()} за {total_elapsed:.1f} сек.")

        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Рендер HTML-анимации в MP4 (1920x1080)")
    parser.add_argument("--html", type=Path, default=Path("index.html"), help="Путь к HTML-файлу движка (по умолчанию index.html)")
    parser.add_argument("--episode", type=Path, default=Path("episodes/ep01_apelsin.json"), help="Путь к JSON-файлу эпизода")
    parser.add_argument("--audio", type=Path, default=None, help="Необязательный аудиофайл озвучки для подмешивания")
    parser.add_argument("--out", type=Path, default=Path("out/output.mp4"), help="Выходной MP4-файл (по умолчанию out/output.mp4)")
    parser.add_argument("--fps", type=int, default=30, help="Частота кадров (по умолчанию 30)")
    parser.add_argument("--crf", type=int, default=18, help="Качество H.264 CRF (по умолчанию 18)")

    args = parser.parse_args()

    try:
        render_video(
            html_path=args.html,
            episode_path=args.episode,
            output_path=args.out,
            fps=args.fps,
            crf=args.crf,
            audio_path=args.audio,
        )
    except Exception as exc:
        print(f"\n❌ Ошибка рендера: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
