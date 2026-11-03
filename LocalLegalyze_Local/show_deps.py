"""Показать в консоли все пакеты, которые нужны локальной версии.

Запуск (из папки LocalLegalyze_Local):

    python show_deps.py            # таблица: пакет, нужно / не нужно, версия
    python show_deps.py --pip      # только строка для pip install
    python show_deps.py --check    # код выхода 1, если чего-то не хватает

Файл ничего не меняет и ничего не ставит — только читает.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys

APP_NAME = "LocalLegalyze (локальная версия)"

# (имя пакета для pip, модуль для проверки, имена в метаданных, зачем нужен)
# Имя модуля и имя пакета в pip не всегда совпадают: `import websocket` —
# это пакет `websocket-client`, `import fpdf` — пакет `fpdf2`.
PACKAGES = (
    ("PyQt6", "PyQt6", ("PyQt6",),
     "окно программы, поля, кнопки"),
    ("websocket-client", "websocket", ("websocket-client", "websocket"),
     "управление страницей чата через DevTools"),
    # НЕ блокирует запуск: без него программа работает, но промты
    # .txt/.md/.json не превращаются в PDF (только .pdf).
    ("fpdf2", "fpdf", ("fpdf2", "pyfpdf2", "fpdf"),
     "необязательно: промты .txt/.md -> PDF (шрифт data/DejaVuSans.ttf)"),
)

OPTIONAL = (
    ("Nuitka", "nuitka", ("Nuitka",),
     "только для сборки .exe (см. СБОРКА_локальная.txt)"),
    ("ordered-set", "ordered_set", ("ordered-set", "ordered_set"),
     "зависимость Nuitka при сборке"),
    ("zstandard", "zstandard", ("zstandard",),
     "зависимость Nuitka при сборке (сжатие onefile)"),
)

PYTHON_MIN = (3, 9)


def version_of(dists: tuple[str, ...]) -> str:
    """Версия пакета по его именам в метаданных (иначе пустая строка)."""
    # importlib.metadata есть только с Python 3.8 — на старом интерпретаторе
    # колонка с версией просто останется пустой, таблица не сломается.
    try:
        from importlib.metadata import PackageNotFoundError, version
    except Exception:
        return ""
    for dist in dists:
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
        except Exception:
            return ""
    return ""


def installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def python_ok() -> bool:
    return sys.version_info[:2] >= PYTHON_MIN


def print_table() -> list[str]:
    """Напечатать таблицу. Вернуть список НЕустановленных обязательных пакетов."""
    print(f"{APP_NAME}: что нужно установить")
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    if not python_ok():
        print(f"  ВНИМАНИЕ: нужен Python "
              f"{PYTHON_MIN[0]}.{PYTHON_MIN[1]} или новее")
    print()

    missing: list[str] = []
    print(f"{'ПАКЕТ':<20} {'СОСТОЯНИЕ':<12} {'ВЕРСИЯ':<10} ЗАЧЕМ")
    print("-" * 78)
    for name, module, dists, why in PACKAGES:
        there = installed(module)
        if not there:
            missing.append(name)
        state = "есть" if there else "НЕТ"
        print(f"{name:<20} {state:<12} {version_of(dists) or '-':<10} {why}")

    print()
    print("Нужны только при сборке .exe (для работы программы не требуются):")
    for name, module, dists, why in OPTIONAL:
        there = installed(module)
        state = "есть" if there else "нет"
        print(f"  {name:<18} {state:<8} {version_of(dists) or '-':<10} {why}")

    print()
    print("Поставляется вместе с программой (pip не нужен):")
    print("  data/DejaVuSans.ttf      шрифт для PDF с кириллицей")
    print("  icon.ico                 значок окна и .exe")
    print("  browser (Chrome/Edge)    отдельно: обычная установка в системе")
    print()
    return missing


def pip_line() -> str:
    return "pip install " + " ".join(name for name, *_rest in PACKAGES)


def main() -> int:
    args = set(sys.argv[1:])
    if "--pip" in args:
        print(pip_line())
        return 0

    missing = print_table()

    if shutil.which("pip") is None and shutil.which("pip3") is None:
        print("ВНИМАНИЕ: pip не найден в PATH. Используйте: "
              "python -m pip install ...")

    if missing:
        print(f"Чего не хватает: {', '.join(missing)}")
        print()
        print("Установить одной командой:")
        print("    " + pip_line())
        print("или из файла зависимостей:")
        print("    pip install -r requirements-local.txt")
        if "--check" in args:
            return 1
    else:
        print("Всё обязательное установлено — можно запускать "
              "LocalLegalyze_Local.py")
        if "--check" in args:
            return 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
