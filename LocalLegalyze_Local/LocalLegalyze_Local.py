#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 LocalLegalyze — ПОЛНОСТЬЮ ЛОКАЛЬНАЯ версия клиента Legalyze (v25 → local)
================================================================================

Один файл. Никаких обращений к серверу: ни авторизации, ни HWID, ни счётчика
запросов, ни проверки версии, ни скачивания промтов.

Что осталось (то, без чего программа не работает):
  * окно программы и встроенное окно настоящего Chrome с чатом ИИ;
  * автовложение двух файлов в чат: промт (.pdf) + «Шаблон.txt»;
  * выбор промта из локальной папки `prompts/`;
  * окно «Шаблон» с шестью полями;
  * речевой ввод (штатный микрофон страницы, два нажатия);
  * глобальные горячие клавиши «Микрофон» и «Окно»;
  * зум 67 % и точная посадка окна браузера в окно программы.

Что убрано по сравнению с TestLegalUniversal_v25/main.py:
  * LoginWindow, AuthWorker, HWIDWorker            — окна авторизации больше нет;
  * BalanceWorker, DecrementWorker                 — счётчика запросов больше нет;
  * PromptLoaderWorker (GET /api/client/prompts)   — промты берутся из `prompts/`;
  * TemplateSyncWorker, POST /api/client/template  — шаблон только локальный;
  * updater.check_for_update                       — проверки версии больше нет;
  * browser_fetch (скачивание Chrome for Testing)  — используется установленный;
  * diagnostics.py + browser_trace.py              — вместо них один текстовый лог.

ИСПРАВЛЕН ДЕФЕКТ «в чат приезжают лишние PDF и Шаблон.txt».
  В серверной версии `UploadThread` решал «файла нет» по одному замеру сразу
  после впрыска. Чип в чате появляется позже (0.5–3 с), поэтому поток успевал
  впрыснуть тот же файл ещё раз, а дедупликация срабатывала только при
  `present > 4` и один раз за жизнь потока. Подробный разбор — НАВИГАЦИЯ.md §6.
  Здесь вместо него `PageController` + `plan_attachments()`:
    1) один механизм впрыска (input[type=file] + change; drop — только если
       поля файла на странице нет) → один вызов = максимум один чип;
    2) пауза ПОСЛЕ впрыска и до следующего замера (`HOLD_AFTER_INJECT`);
    3) «пусто» подтверждается двумя замерами подряд (`EMPTY_CONFIRM`);
    4) точное сопоставление «файл ↔ чип» по нормализованному имени;
    5) безымянный чип засчитывается как «возможно, уже этот файл»;
    6) лишние чипы удаляются по одному, с проверкой после каждого;
    7) после подтверждения — «запор»: без исчезновения файла впрыска нет.

Запуск:   python LocalLegalyze_Local.py
Сборка:   см. «СБОРКА_локальная.txt»
Зависимости: PyQt6, websocket-client, fpdf2 (только для .txt/.md промтов).
================================================================================
"""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# =============================================================================
# 1. ПУТИ. Всё лежит РЯДОМ С ПРОГРАММОЙ — сервера больше нет.
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent


def _writable_root() -> Path:
    """Папка программы, если в неё можно писать; иначе %APPDATA%\\LegalyzeLocal."""
    try:
        probe = SCRIPT_DIR / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return SCRIPT_DIR
    except Exception:
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "LegalyzeLocal"


ROOT_DIR = _writable_root()
DATA_DIR = ROOT_DIR / "data"
WORK_DIR = DATA_DIR / "work"
PROMPTS_DIR = ROOT_DIR / "prompts"
LOGS_DIR = ROOT_DIR / "logs"
CONFIG_FILE = ROOT_DIR / "config.json"
TEMPLATE_FILE = ROOT_DIR / "Шаблон.txt"
BROWSER_PROFILE = ROOT_DIR / "browser-profile"
LOG_FILE = LOGS_DIR / "locallegalyze.log"

APP_NAME = "Legalyze Local"
APP_VERSION = "1.0.7-local"

#: Страница чата. Никаких серверов Legalyze — это адрес самого Google.
CHAT_URL = "https://google.com/ai"
#: Рабочая поверхность чата, если сервис отдал «режим ИИ в поиске» без вложений.
CHAT_FALLBACK_URL = "https://gemini.google.com/app"
CONSENT_URL_HINT = "consent.google.com"

# =============================================================================
# 2. НАСТРОЙКИ РАСКЛАДКИ — правьте числа ТОЛЬКО здесь
# =============================================================================

X, Y, W, H = 1426, 200, 453, 735      # главное окно, DIP

# Сдвиг окна браузера внутри области чата (между панелями), в DIP.
# 0 = окно занимает область целиком: панели и страница НЕ перекрываются
# ни на одном масштабе Windows. В серверной версии здесь стояли -10 / -24 —
# это подгонка под панели, которые лежали ПОВЕРХ страницы; в локальной версии
# область чата вырезана из раскладки, поэтому подгонка не нужна.
BROWSER_DX = 0       # сдвиг окна браузера по X (DIP)
BROWSER_DY = 0       # сдвиг окна браузера по Y (DIP)
BROWSER_DW = 0       # изменить ширину окна браузера
BROWSER_DH = 0       # изменить высоту окна браузера

PAGE_ZOOM = 0.667    # нативный зум страницы 67 %
SPEECH_LANG = "ru-RU"
FORCE_LIGHT_THEME = True

#: Сколько ждать появления чипа файла после впрыска, прежде чем считать,
#: что впрыск не сработал. Именно эта пауза убирает дубли.
HOLD_AFTER_INJECT = 1.6
#: Сколько замеров подряд «в чате ноль файлов» нужно, чтобы начать новый цикл.
EMPTY_CONFIRM = 2
#: Период опроса состояния чата, с.
POLL_INTERVAL = 0.40
#: После «запора» перепроверяем реже — GUI не грузим.
SEALED_INTERVAL = 0.90
#: Сколько впрысков одного файла допустимо за цикл, прежде чем сдаться.
MAX_INJECTS_PER_FILE = 4
#: Пауза между удалениями лишних чипов, с.
PRUNE_PAUSE = 0.8
#: Через сколько секунд без единого чипа сообщить «вложение не работает».
BLOCK_AFTER_SECONDS = 25.0

THEME = {
    "bg": "#0e1020", "bg1": "#151830", "bg2": "#151830",
    "panel": "rgba(14, 16, 32, 0.96)", "line": "#262a4a",
    "text": "#e6e8f5", "muted": "#8f96bf", "accent": "#7c83ff",
    "accent2": "#5b61e8", "danger": "#ef4444", "ok": "#34d399",
}

BASE_QSS = f"""
QDialog, QWidget#Card {{
    background: {THEME['bg']};
    border: 1px solid {THEME['line']};
    border-radius: 14px;
}}
QLabel {{ color: {THEME['text']}; font-size: 13px; }}
QLabel#Caption {{ color: {THEME['muted']}; font-size: 11px; }}
QLineEdit {{
    background: {THEME['bg2']}; border: 1px solid {THEME['line']};
    border-radius: 8px; padding: 10px 12px; color: {THEME['text']};
    font-size: 14px; selection-background-color: {THEME['accent2']};
}}
QLineEdit:focus {{ border: 1px solid {THEME['accent']}; background: #171b38; }}
QComboBox {{
    background: {THEME['bg2']}; border: 1px solid {THEME['line']};
    border-radius: 8px; padding: 8px 10px; color: {THEME['text']}; font-size: 13px;
}}
QComboBox:hover {{ border: 1px solid {THEME['accent']}; }}
QComboBox QAbstractItemView {{
    background: {THEME['bg2']}; color: {THEME['text']};
    selection-background-color: {THEME['accent2']};
    border: 1px solid {THEME['line']}; outline: none;
}}
QCheckBox {{ color: {THEME['muted']}; font-size: 12px; }}
QPushButton {{
    background: {THEME['bg2']}; color: {THEME['text']};
    border: 1px solid {THEME['line']}; border-radius: 9px;
    padding: 9px 16px; font-size: 13px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {THEME['accent']}; background: #1b1f40; }}
QPushButton:pressed {{ background: #222750; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {THEME['accent']}, stop:1 {THEME['accent2']});
    border: none; color: #ffffff;
}}
QPushButton#Primary:hover {{ background: {THEME['accent2']}; }}
QPushButton#Ghost {{ background: transparent; color: {THEME['muted']}; }}
QPushButton#Ghost:hover {{ color: {THEME['text']}; border-color: {THEME['accent']}; }}
"""

# =============================================================================
# 3. ЛОГ. Один текстовый файл, запись пачками, без нагрузки на GUI.
# =============================================================================

#: Позиция главного окна на экране. По журналу сразу видно, сдвигали ли окно:
#: если оно стоит на месте, а шторка «уехала» — дело не в moveEvent, а если
#: обе координаты менялись вместе — шторка просто не успевает за окном.
_WINDOW_POSITION: dict[str, int] = {}


class Log:
    """Потокобезопасный лог с буфером: диск не тормозит ни GUI, ни страницу.

    В серверной версии «максимальное логирование» писалось сразу на диск из
    нескольких потоков, а `browser_trace` ещё и выгружал HTML страницы — это
    одна из причин, по которой окно начинало подлагивать. Здесь буфер на
    несколько строк, сброс раз в 2 секунды и жёсткий предел размера файла.
    """

    _lock = threading.Lock()
    _buffer: list[str] = []
    _last_flush = 0.0
    _max_bytes = 2 * 1024 * 1024

    @classmethod
    def write(cls, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{stamp} {level:5s} {message}"
        with cls._lock:
            cls._buffer.append(line)
            if len(cls._buffer) > 500:
                del cls._buffer[:250]
            now = time.monotonic()
            if now - cls._last_flush < 2.0 and len(cls._buffer) < 40:
                return
            cls._last_flush = now
            chunk = "\n".join(cls._buffer) + "\n"
            cls._buffer.clear()
        cls._flush(chunk)

    @classmethod
    def _flush(cls, chunk: str) -> None:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                if LOG_FILE.stat().st_size > cls._max_bytes:
                    old = LOG_FILE.with_suffix(".old.log")
                    if old.exists():
                        old.unlink()
                    LOG_FILE.rename(old)
            except FileNotFoundError:
                pass
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(chunk)
        except Exception:
            pass

    @classmethod
    def info(cls, message: str) -> None:
        cls.write("INFO", message)

    @classmethod
    def warn(cls, message: str) -> None:
        cls.write("WARN", message)

    @classmethod
    def error(cls, message: str) -> None:
        cls.write("ERROR", message)

    @classmethod
    def exc(cls, where: str) -> None:
        try:
            cls.write("ERROR", f"{where}: {traceback.format_exc(limit=4).strip()}")
        except Exception:
            pass


# =============================================================================
# 4. Win32: типы и мелкие помощники
# =============================================================================

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_DLGFRAME = 0x00400000
WS_BORDER = 0x00800000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_ASYNCWINDOWPOS = 0x4000
SW_SHOW = 5
SW_HIDE = 0

TH32CS_SNAPPROCESS = 0x00000002
#: HWND_TOPMOST — не «минус единица», а указатель на -1: ctypes требует
#: c_void_p(-1). Раньше здесь стояло `int(hwnd)` в SetWindowPos, то есть
#: вместо HWND в функцию уезжало 0 — окно не поднималось.
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_TOP = 0

user32 = ctypes.WinDLL("user32", use_last_error=True) if IS_WINDOWS else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None

WNDENUMPROC = None
if IS_WINDOWS:
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _declare_win32() -> None:  # noqa: C901 - один список прототипов, так виднее
    """Явные прототипы Win32: HWND — это указатель, а не int.

    Без `argtypes` ctypes выводит тип из значения и на 64-битном Python
    падает с «int too long to convert» на окнах с большим хэндлом, а
    `LoadImageW` без `restype` обрезает HICON до 32 бит — иконка пропадает.
    """
    if not user32:
        return
    ptr, uint, long_ = ctypes.c_void_p, wintypes.UINT, ctypes.c_long
    decl = {
        "SetWindowPos": ([ptr, ptr, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, uint], wintypes.BOOL),
        "SetParent": ([ptr, ptr], ptr),
        "GetParent": ([ptr], ptr),
        "GetWindow": ([ptr, uint], ptr),
        "ShowWindow": ([ptr, ctypes.c_int], wintypes.BOOL),
        "GetWindowRect": ([ptr, ctypes.POINTER(RECT)], wintypes.BOOL),
        "GetClientRect": ([ptr, ctypes.POINTER(RECT)], wintypes.BOOL),
        "GetWindowLongPtrW": ([ptr, ctypes.c_int], ctypes.c_void_p),
        "SetWindowLongPtrW": ([ptr, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
        "GetWindowLongW": ([ptr, ctypes.c_int], long_),
        "SetWindowLongW": ([ptr, ctypes.c_int, long_], long_),
        # Нужно для focus-перехода между процессами (см. browser_focus).
        "GetForegroundWindow": ([], ptr),
        "SetFocus": ([ptr], ptr),
        "GetFocus": ([], ptr),
        "IsChild": ([ptr, ptr], wintypes.BOOL),
        "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        "WindowFromPoint": ([wintypes.POINT], ptr),
        "AttachThreadInput": ([wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
        "GetGUIThreadInfo": ([wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)], wintypes.BOOL),
        "GetAncestor": ([ptr, uint], ptr),
        "EnumChildWindows": ([ptr, ctypes.c_void_p, wintypes.LPARAM], wintypes.BOOL),
        "GetWindowThreadProcessId": ([ptr, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "IsWindow": ([ptr], wintypes.BOOL),
        "IsWindowVisible": ([ptr], wintypes.BOOL),
        "GetClassNameW": ([ptr, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
        "RegisterHotKey": ([ptr, ctypes.c_int, uint, uint], wintypes.BOOL),
        "UnregisterHotKey": ([ptr, ctypes.c_int], wintypes.BOOL),
        "SendMessageW": ([ptr, uint, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_void_p),
        "PostMessageW": ([ptr, uint, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        "LoadImageW": ([ptr, wintypes.LPCWSTR, uint, ctypes.c_int, ctypes.c_int, uint], ptr),
        "GetConsoleWindow": ([], ptr),
    }
    for name, (args, restype) in decl.items():
        try:
            function = getattr(user32, name, None)
            if function is None:
                continue
            function.argtypes = args
            function.restype = restype
        except Exception:
            continue
    try:
        user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
    except Exception:
        pass


def handle_value(value) -> int:
    """HWND/HANDLE → обычное число.

    Здесь была КОРНЕВАЯ ошибка всей вживляющей части: `int(c_void_p(1234))`
    в Python НЕ даёт 1234 — ctypes пытается разобрать сырые байты как текст и
    падает с ValueError. `int()` внутри `try/except` это глотал, и ЛЮБОЙ
    хэндл, вернувшийся из Win32 как c_void_p, превращался в 0.
    Читать надо `.value` — только он и есть адрес.
    """
    if value is None:
        return 0
    raw = getattr(value, "value", value)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def hwnd_of(value) -> ctypes.c_void_p:
    """Преобразовать HWND (int/c_void_p/sip.voidptr) в указатель для ctypes."""
    return ctypes.c_void_p(handle_value(value))


def _clear_error() -> None:
    """Сбросить последнюю ошибку Win32 (на не-Windows функции нет)."""
    setter = getattr(ctypes, "set_last_error", None)
    if setter is not None:
        try:
            setter(0)
        except Exception:
            pass


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        return 0
    try:
        return int(getter())
    except Exception:
        return 0


def get_window_long(hwnd: int, index: int) -> int:
    """Прочитать стиль окна. Возвращает ЗНАКОВОЕ 32-битное число, как Win32."""
    if not user32 or not hwnd:
        return 0
    getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    _clear_error()
    try:
        value = getter(hwnd_of(hwnd), index)
    except Exception:
        Log.exc("win.get_window_long")
        return 0
    raw = handle_value(value)
    # 0xFFFFFFFF для младших 32 бит: стили — это LONG, и старшие биты после
    # GetWindowLongPtrW приходят нулями только на 64-битном Python.
    raw &= 0xFFFFFFFF
    return raw - 0x100000000 if raw & 0x80000000 else raw


def set_window_long(hwnd: int, index: int, value: int) -> int:
    """Записать стиль окна.

    Для GWL_STYLE/GWL_EXSTYLE у Win32 есть ровно `SetWindowLongW` с аргументом
    LONG (4 байта). `SetWindowLongPtrW` с `c_void_p` здесь НЕЛЬЗЯ: для этих
    индексов функция копирует только младшие 32 бита, а передача указателя
    на некоторых сборках Windows возвращает ERROR_INVALID_PARAMETER.
    """
    if not user32 or not hwnd:
        return 0
    signed = value & 0xFFFFFFFF
    if signed & 0x80000000:
        signed -= 0x100000000
    _clear_error()
    try:
        previous = user32.SetWindowLongW(hwnd_of(hwnd), index, signed)
    except Exception:
        Log.exc("win.set_window_long")
        return 0
    if not previous and _last_error():
        raise OSError(_last_error(), "SetWindowLongW failed")
    raw = handle_value(previous) & 0xFFFFFFFF
    return raw - 0x100000000 if raw & 0x80000000 else raw


def raise_window(hwnd: int) -> bool:
    """Поднять окно наверх ВНУТРИ родителя (HWND_TOP).

    Дочернее окно браузера после SetParent встаёт поверх соседей, поэтому
    одной шторки Qt недостаточно — её нужно поднимать настоящим HWND_TOP.
    """
    if not user32 or not hwnd:
        return False
    try:
        return bool(user32.SetWindowPos(hwnd_of(hwnd), hwnd_of(HWND_TOP), 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
    except Exception:
        Log.exc("win.raise_window")
        return False


def client_size(hwnd: int) -> tuple[int, int]:
    """Размер КЛИЕНТСКОЙ части окна (без рамки) — в нём живут дочерние окна."""
    if not user32 or not hwnd:
        return (0, 0)
    rect = RECT()
    try:
        if not user32.GetClientRect(hwnd_of(hwnd), ctypes.byref(rect)):
            return (0, 0)
    except Exception:
        Log.exc("win.client_size")
        return (0, 0)
    return (int(rect.right - rect.left), int(rect.bottom - rect.top))


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    if not user32 or not hwnd:
        return (0, 0, 0, 0)
    rect = RECT()
    try:
        if not user32.GetWindowRect(hwnd_of(hwnd), ctypes.byref(rect)):
            return (0, 0, 0, 0)
    except Exception:
        Log.exc("win.window_rect")
        return (0, 0, 0, 0)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def force_topmost(hwnd: int) -> bool:
    """Поднять окно поверх остальных (у приложения нет рамки).

    ЗДЕСЬ БЫЛА ВТОРАЯ ОШИБКА: `SetWindowPos(int(hwnd), HWND_TOPMOST, ...)`.
    Первый аргумент объявлен как c_void_p — проходил, а вот HWND_TOPMOST был
    числом -1 вместо `c_void_p(-1)`: ctypes не мог его сконвертировать, вызов
    не выполнялся, и окно никогда не поднималось наверх.
    """
    if not user32 or not hwnd:
        return False
    try:
        return bool(user32.SetWindowPos(hwnd_of(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
    except Exception:
        Log.exc("win.force_topmost")
        return False


def apply_taskbar_style(hwnd: int) -> None:
    """Убрать окно из панели задач и из Alt+Tab (WS_EX_TOOLWINDOW)."""
    if not user32 or not hwnd:
        return
    try:
        style = get_window_long(hwnd, GWL_EXSTYLE)
        set_window_long(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        user32.SetWindowPos(hwnd_of(hwnd), None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                            SWP_NOACTIVATE | SWP_FRAMECHANGED)
    except Exception:
        Log.exc("win.taskbar_style")


# ---------------------------------------------------------------------------
# ПАНЕЛЬ ЗАДАЧ. Стиля WS_EX_TOOLWINDOW НЕДОСТАТОЧНО: Chromium сам возвращает
# свою кнопку через ITaskbarList::AddTab, поэтому нужен DeleteTab.
#
# Вызов идёт через COM, а COM-объект можно прочитать «не туда» — и тогда
# процесс падает с access violation, который НЕ перехватывается (SEH уже не
# работает). Поэтому: сначала БЕЗОПАСНОЕ чтение таблицы методов через
# ReadProcessMemory своего же процесса, при первом сомнении COM выключается
# до конца работы программы.
# ---------------------------------------------------------------------------

CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
IID_ITASKBAR_LIST = "{56FDF342-FD6D-11D0-958A-006097C9A090}"
CLSCTX_ALL = 0x0017
_COM_SLOT_HR_INIT = 3
_COM_SLOT_DELETE_TAB = 5
_COM_SLOT_RELEASE = 2
_COM_SLOTS = 8

#: True после первой же осечки — дальше COM в этом процессе не трогаем.
_COM_DISABLED = False
_OLE32 = None


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    raw = str(text).strip("{}").split("-")
    value = GUID()
    value.Data1 = int(raw[0], 16)
    value.Data2 = int(raw[1], 16)
    value.Data3 = int(raw[2], 16)
    value.Data4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(raw[3] + raw[4]))
    return value


def ole32():
    """ole32 с прототипами (без comtypes и pywin32)."""
    global _OLE32, _COM_DISABLED
    if _COM_DISABLED or not IS_WINDOWS:
        return None
    if _OLE32 is not None:
        return _OLE32
    try:
        dll = ctypes.WinDLL("ole32", use_last_error=True)
        dll.CoInitialize.argtypes = [ctypes.c_void_p]
        dll.CoInitialize.restype = ctypes.c_long
        dll.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(GUID),
                                         ctypes.POINTER(ctypes.c_void_p)]
        dll.CoCreateInstance.restype = ctypes.c_long
        _OLE32 = dll
        return dll
    except Exception:
        _COM_DISABLED = True
        Log.exc("win.ole32")
        return None


def read_pointer(address: int) -> int | None:
    """Прочитать указатель БЕЗ разыменования из Python.

    `table[slot]` — это обращение к чужой памяти из Python: если адрес плохой,
    процесс просто умирает. `ReadProcessMemory` по своему же процессу вместо
    этого честно возвращает «не удалось».
    """
    if not kernel32 or not address:
        return None
    buffer = ctypes.c_void_p()
    read = ctypes.c_size_t(0)
    size = ctypes.sizeof(ctypes.c_void_p)
    try:
        ok = kernel32.ReadProcessMemory(kernel32.GetCurrentProcess(),
                                        ctypes.c_void_p(int(address)),
                                        ctypes.byref(buffer), size,
                                        ctypes.byref(read))
    except Exception:
        return None
    if not ok or int(read.value or 0) != size:
        return None
    return int(buffer.value or 0)


def com_vtable(interface: int, slots: int = _COM_SLOTS) -> list[int] | None:
    """Первые `slots` адресов таблицы методов. None — читать нельзя."""
    if not interface:
        return None
    table = read_pointer(interface)
    if not table:
        return None
    size = ctypes.sizeof(ctypes.c_void_p)
    result = []
    for index in range(int(slots)):
        address = read_pointer(table + index * size)
        if not address:
            return None
        result.append(address)
    # У живого COM-объекта слоты разные: совпадение означает мусор.
    if len(set(result)) != len(result):
        return None
    return result


def com_call(interface: int, slot: int, *args) -> int | None:
    """Вызвать метод COM-объекта. Вне Windows и при сомнениях — None."""
    global _COM_DISABLED
    if _COM_DISABLED or not interface or not IS_WINDOWS:
        return None
    slots = com_vtable(interface)
    if not slots or int(slot) >= len(slots):
        return None
    address = int(slots[int(slot)] or 0)
    if not address:
        return None
    try:
        proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p,
                                   *([wintypes.HWND] * len(args)))
        hr = int(proto(address)(ctypes.c_void_p(int(interface)), *args))
        return hr
    except OSError:
        _COM_DISABLED = True
        Log.exc("win.com_call")
        return None
    except Exception:
        return None


def hide_from_taskbar(hwnd: int) -> bool:
    """Убрать кнопку окна из панели задач: стиль + ITaskbarList::DeleteTab."""
    global _COM_DISABLED
    if not user32 or not hwnd:
        return False
    apply_taskbar_style(hwnd)
    if _COM_DISABLED:
        return True
    dll = ole32()
    if dll is None:
        return True
    pointer = ctypes.c_void_p()
    try:
        try:
            dll.CoInitialize(None)
        except Exception:
            pass
        hr = int(dll.CoCreateInstance(ctypes.byref(_guid(CLSID_TASKBAR_LIST)), None,
                                      CLSCTX_ALL,
                                      ctypes.byref(_guid(IID_ITASKBAR_LIST)),
                                      ctypes.byref(pointer)))
        if hr < 0 or not pointer.value:
            return True
        interface = int(pointer.value)
        if com_vtable(interface) is None:
            _COM_DISABLED = True
            Log.warn("COM панели задач недоступен — окно убираем только стилем")
            return True
        init = com_call(interface, _COM_SLOT_HR_INIT)
        if init is None or init < 0:
            return True
        deleted = com_call(interface, _COM_SLOT_DELETE_TAB, hwnd_of(hwnd))
        return bool(deleted is not None and int(deleted) >= 0)
    except Exception:
        Log.exc("win.taskbar")
        return False
    finally:
        if pointer.value:
            try:
                com_call(int(pointer.value), _COM_SLOT_RELEASE)
            except Exception:
                pass


def process_names() -> dict[int, str]:
    """{pid: 'chrome.exe'} через Toolhelp32 — без wmic и без подпроцессов."""
    if not IS_WINDOWS or not kernel32:
        return {}
    names: dict[int, str] = {}
    handle = None
    try:
        handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not handle:
            return {}
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(handle, ctypes.byref(entry)):
            while True:
                names[int(entry.th32ProcessID)] = str(entry.szExeFile or "")
                if not kernel32.Process32NextW(handle, ctypes.byref(entry)):
                    break
    except Exception:
        Log.exc("win.process_names")
    finally:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    return names


def process_tree(roots) -> set[int]:
    """Все потомки переданных pid (Chrome плодит рендереры и утилиты).

    Раньше здесь на КАЖДЫЙ процесс делался свой снимок Toolhelp32 — при
    двух десятках процессов это сотни миллисекунд и заметный подлаг GUI.
    Теперь один снимок и одно дерево.
    """
    if not IS_WINDOWS or not kernel32:
        return set(int(p) for p in roots if p)
    parents: dict[int, int] = {}
    handle = None
    try:
        handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if handle:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if kernel32.Process32FirstW(handle, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not kernel32.Process32NextW(handle, ctypes.byref(entry)):
                        break
    except Exception:
        Log.exc("win.process_tree")
    finally:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    found: set[int] = set()
    queue_ = [int(p) for p in roots if p]
    while queue_:
        current = queue_.pop()
        for child in children.get(current, []):
            if child in found or child in (int(p) for p in roots if p):
                continue
            found.add(child)
            queue_.append(child)
    return found


def kill_tree(roots) -> None:
    """Снять процессы браузера вместе с деревом одним вызовом.

    `proc.terminate()` убивает только родителя: рендереры и GPU выживают, их
    окна остаются висеть — именно из-за этого «плодятся браузеры».
    `taskkill /T` снимает всё дерево, не трогая чужие процессы.
    """
    if not IS_WINDOWS:
        return
    for pid in [int(p) for p in roots if p]:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, creationflags=0x08000000, timeout=8)
        except Exception:
            continue


def _declare_kernel32() -> None:
    """Прототипы для снимков процессов и безопасного чтения указателей."""
    if not kernel32:
        return
    decl = {
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], ctypes.c_void_p),
        "CloseHandle": ([ctypes.c_void_p], wintypes.BOOL),
        "GetCurrentProcess": ([], ctypes.c_void_p),
        "GetCurrentThreadId": ([], wintypes.DWORD),
        "ReadProcessMemory": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)],
                              wintypes.BOOL),
    }
    for name, (args, restype) in decl.items():
        try:
            function = getattr(kernel32, name, None)
            if function is None:
                continue
            function.argtypes = args
            function.restype = restype
        except Exception:
            continue


_declare_win32()
_declare_kernel32()


# =============================================================================
# 5. Горячие клавиши: опрос GetAsyncKeyState из таймера главного потока
# =============================================================================

VK_MAP = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "F13": 0x7C, "F14": 0x7D, "F15": 0x7E, "F16": 0x7F, "F17": 0x80, "F18": 0x81,
    "F19": 0x82, "F20": 0x83, "F21": 0x84, "F22": 0x85, "F23": 0x86, "F24": 0x87,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46, "G": 0x47,
    "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C, "M": 0x4D, "N": 0x4E,
    "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52, "S": 0x53, "T": 0x54, "U": 0x55,
    "V": 0x56, "W": 0x57, "X": 0x58, "Y": 0x59, "Z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "Num0": 0x60, "Num1": 0x61, "Num2": 0x62, "Num3": 0x63, "Num4": 0x64,
    "Num5": 0x65, "Num6": 0x66, "Num7": 0x67, "Num8": 0x68, "Num9": 0x69,
    "Num*": 0x6A, "Num+": 0x6B, "Num-": 0x6D, "Num.": 0x6E, "Num/": 0x6F,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "Pause": 0x13, "ScrollLock": 0x91,
    "Space": 0x20, "Tab": 0x09, "Backspace": 0x08, "Enter": 0x0D,
    "Esc": 0x1B, "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}

HOTKEY_TOGGLE_ID = 1
HOTKEY_MIC_ID = 2
_MOD_NOREPEAT = 0x4000


def normalize_key(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for name in VK_MAP:
        if name.lower() == text.lower():
            return name
    return text


def resolve_key_event(event) -> str | None:
    """Qt-событие клавиши → имя клавиши из VK_MAP (для диалога настройки)."""
    try:
        key = event.key()
        mapping = {
            0x01000000: "Esc", 0x01000001: "Tab", 0x01000004: "Enter",
            0x01000005: "Enter", 0x01000013: "Up",
            0x01000015: "Down", 0x01000012: "Left", 0x01000014: "Right",
            0x01000016: "Home", 0x01000017: "End", 0x01000006: "Insert",
            0x01000007: "Delete", 0x01000021: "PageUp", 0x01000022: "PageDown",
            0x20: "Space",
        }
        if key in mapping:
            return mapping[key]
        text = (event.text() or "").upper()
        if text in VK_MAP:
            return text
        for name, code in VK_MAP.items():
            if name.startswith("F") and name[1:].isdigit() and key == code:
                return name
        if 0x30 <= key <= 0x39:
            return chr(key)
        if 0x41 <= key <= 0x5A:
            return chr(key)
    except Exception:
        Log.exc("hotkey.resolve_key")
    return None


class HotkeyMonitor:
    """Опрос физического состояния клавиш: работает при любом фокусе.

    `RegisterHotKey` — только «лучше-попытка» подавления проброса в другие
    приложения; его отказ никогда не блокирует срабатывание.
    """

    _DEBOUNCE = 0.28

    def __init__(self, on_event):
        self._on_event = on_event
        self._keys: dict[int, int] = {}
        self._down: dict[int, bool] = {}
        self._last_fire: dict[int, float] = {}
        self._registered: dict[int, int] = {}
        self._hwnd = 0
        self._get_state = self._make_state()

    @staticmethod
    def _make_state():
        if not IS_WINDOWS or not user32:
            return lambda _vk: False
        try:
            getter = user32.GetAsyncKeyState
            getter.argtypes = [ctypes.c_int]
            getter.restype = ctypes.c_short
            return lambda vk: bool(getter(int(vk)) & 0x8000)
        except Exception:
            Log.exc("hotkey.get_state")
            return lambda _vk: False

    def set_keys(self, toggle_vk: int, mic_vk: int) -> bool:
        self._keys = {HOTKEY_TOGGLE_ID: int(toggle_vk or 0),
                      HOTKEY_MIC_ID: int(mic_vk or 0)}
        self._down = {key_id: False for key_id in self._keys}
        self._resuppress()
        return True

    def clear(self) -> None:
        self.set_keys(0, 0)

    def tick(self, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else now
        for key_id, vk in self._keys.items():
            if not vk:
                continue
            try:
                down = bool(self._get_state(vk))
            except Exception:
                down = False
            was_down = self._down.get(key_id, False)
            last = self._last_fire.get(key_id)
            if down and not was_down and (last is None or (ts - last) >= self._DEBOUNCE):
                self._last_fire[key_id] = ts
                try:
                    self._on_event(key_id)
                except Exception:
                    Log.exc("hotkey.callback")
            self._down[key_id] = down

    def try_suppress(self, hwnd: int) -> bool:
        self._hwnd = int(hwnd or 0)
        return self._register()

    def _register(self) -> bool:
        self._release()
        if not IS_WINDOWS or not user32 or not self._hwnd:
            return False
        ok_any = False
        for key_id, vk in self._keys.items():
            if not vk:
                continue
            try:
                if user32.RegisterHotKey(ctypes.c_void_p(self._hwnd), int(key_id),
                                         _MOD_NOREPEAT, int(vk)):
                    self._registered[key_id] = self._hwnd
                    ok_any = True
            except Exception:
                continue
        return ok_any

    def _release(self) -> None:
        if not IS_WINDOWS or not user32:
            self._registered.clear()
            return
        for key_id, hwnd in list(self._registered.items()):
            try:
                user32.UnregisterHotKey(ctypes.c_void_p(hwnd), int(key_id))
            except Exception:
                pass
        self._registered.clear()

    def _resuppress(self) -> None:
        if self._hwnd:
            self._register()
        else:
            self._release()

    def release(self) -> None:
        self._release()

# =============================================================================
# 6. КОНФИГ И ШАБЛОН — оба файла лежат рядом с программой
# =============================================================================

TEMPLATE_FIELDS = ("Name", "ID", "Fraction", "Rang", "Department", "JobTitle")

DEFAULT_TEMPLATE = {field: "" for field in TEMPLATE_FIELDS}

DEFAULT_CONFIG = {
    "selected_prompt": "",
    "hotkey_toggle": "",
    "hotkey_mic": "",
    "hotkeys_configured": False,
    "template": DEFAULT_TEMPLATE.copy(),
    "speech_lang": SPEECH_LANG,
    "light_theme": FORCE_LIGHT_THEME,
    "browser_path": "",
    # Сдвиг окна браузера внутри области чата, в DIP. Ноль — окно занимает
    # область целиком; серверные значения (-10 / -24) оставлены в BROWSER_DX /
    # BROWSER_DY как справка, если понадобится точная подгонка.
    "browser_dx": 0,
    "browser_dy": 0,
    "browser_dw": 0,
    "browser_dh": 0,
}


def ensure_dirs() -> None:
    for folder in (DATA_DIR, WORK_DIR, PROMPTS_DIR, LOGS_DIR, BROWSER_PROFILE):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            Log.exc(f"dirs.{folder.name}")


def normalize_template(template: dict | None) -> dict:
    template = template or {}
    return {field: str(template.get(field, "") or "") for field in TEMPLATE_FIELDS}


def load_config() -> dict:
    ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    cfg["template"] = DEFAULT_TEMPLATE.copy()
    if CONFIG_FILE.is_file():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in DEFAULT_CONFIG.items():
                    if key in raw:
                        cfg[key] = raw[key]
        except Exception:
            Log.exc("config.read")
    cfg["template"] = normalize_template(cfg.get("template"))
    if not cfg.get("hotkeys_configured"):
        # Первичная настройка не пройдена — поля строго пустые.
        cfg["hotkey_toggle"] = ""
        cfg["hotkey_mic"] = ""
        cfg["hotkeys_configured"] = False
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    try:
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        Log.exc("config.write")


def read_template_file() -> dict:
    try:
        if TEMPLATE_FILE.is_file():
            data = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return normalize_template(data)
    except Exception:
        Log.exc("template.read")
    return normalize_template(DEFAULT_TEMPLATE)


def write_template_file(template: dict | None) -> Path:
    ensure_dirs()
    data = normalize_template(template)
    try:
        tmp = TEMPLATE_FILE.with_name(TEMPLATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, TEMPLATE_FILE)
    except Exception:
        Log.exc("template.write")
    return TEMPLATE_FILE


def ensure_template_file(template: dict | None = None) -> Path:
    if not TEMPLATE_FILE.is_file():
        return write_template_file(template or DEFAULT_TEMPLATE)
    return TEMPLATE_FILE


def save_template(cfg: dict, template: dict | None) -> dict:
    data = normalize_template(template)
    cfg["template"] = data
    save_config(cfg)
    write_template_file(data)
    return cfg


def template_filled(cfg: dict) -> bool:
    return any(str(v).strip() for v in (cfg.get("template") or {}).values())


# =============================================================================
# 7. ПРОМТЫ — ЛОКАЛЬНАЯ ПАПКА вместо сервера
# =============================================================================

#: Ключ, которым администратор шифрует промт (`encrypt_tool.py` из v25).
#: Он остаётся в коде, чтобы уже подготовленные `.enc`-файлы открывались.
LEGACY_PROMPT_KEY = b"legalyze-fernet-key-32bytes-long!"
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

PROMPT_EXTENSIONS = (".pdf", ".enc", ".bin", ".dat", ".txt", ".md", ".json", ".prompt")


def prompt_display_name(filename: str) -> str:
    """Имя так, как его видно в списке: без пути и без последнего расширения.

    Снимается любое расширение, а не только известные: «Мой промт.docx» и
    «Промт.2024.md» тоже должны выглядеть человеко-читаемо.
    """
    name = (filename or "").strip().replace("\\", "/").split("/")[-1]
    if "." in name[1:]:
        head, tail = name.rsplit(".", 1)
        # Служебные двойные расширения вида «.enc.bak» снимаем по одному шагу.
        if tail and len(tail) <= 8:
            name = head
    return name.strip(" .") or "prompt"


def pdf_file_name(filename: str) -> str:
    """Итоговое имя PDF, которое увидит чат (и человек, и ИИ)."""
    safe = _ILLEGAL_FS.sub("_", prompt_display_name(filename)).strip(" .")
    return f"{safe or 'prompt'}.pdf"


def _legacy_decrypt(blob: bytes) -> bytes:
    """Расшифровка `.enc` тем же ключом, что у серверной версии (Fernet)."""
    try:
        import base64 as _b64
        import hashlib as _hash

        from cryptography.fernet import Fernet

        key = _b64.urlsafe_b64encode(_hash.sha256(LEGACY_PROMPT_KEY).digest())
        return Fernet(key).decrypt(blob)
    except Exception:
        return b""


def text_to_pdf_bytes(text: bytes, title: str = "") -> bytes:
    """Текстовый промт → PDF. Нужен, если промт лежит как `.txt`/`.md`."""
    try:
        from fpdf import FPDF
    except Exception as exc:      # noqa: BLE001
        raise RuntimeError(
            "Для промтов .txt/.md нужен пакет fpdf2: pip install fpdf2. "
            "Либо положите промт готовым PDF."
        ) from exc

    pdf = FPDF()
    if title:
        try:
            pdf.set_title(title)
        except Exception:
            pass
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    font_path = DATA_DIR / "DejaVuSans.ttf"
    used_unicode_font = False
    if font_path.is_file():
        try:
            pdf.add_font("DejaVu", "", str(font_path))
            pdf.set_font("DejaVu", size=10)
            used_unicode_font = True
        except Exception:
            Log.exc("pdf.font")
    if not used_unicode_font:
        pdf.set_font("Helvetica", size=10)
    decoded = text.decode("utf-8", errors="replace")
    for line in decoded.splitlines():
        line = line.rstrip()
        if not line.strip():
            pdf.ln(6)
            continue
        try:
            pdf.multi_cell(0, 6, line)
        except Exception:
            try:
                pdf.multi_cell(0, 6, line.encode("latin-1", "replace").decode("latin-1"))
            except Exception:
                pass
    return bytes(pdf.output())


class PromptStore:
    """Локальный «склад промтов»: читает папку `prompts/` рядом с программой.

    Заменяет `GET /api/client/prompts` + `GET /api/client/prompts?filename=…`
    из серверной версии. Ничего не скачивает и не отправляет.
    """

    def __init__(self, folder: Path = PROMPTS_DIR):
        self.folder = Path(folder)

    def list_prompts(self) -> list[str]:
        """Имена файлов промтов в папке (отсортированные по имени)."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            found = [
                item.name for item in self.folder.iterdir()
                if item.is_file()
                and not item.name.startswith((".", "_"))
                and item.name.lower().endswith(PROMPT_EXTENSIONS)
            ]
        except Exception:
            Log.exc("prompts.list")
            return []
        return sorted(found, key=str.lower)

    def ensure_example(self) -> None:
        """Положить пример промта, если папка пуста: программа сразу рабочая."""
        if self.list_prompts():
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            body = (
                "ПРИМЕР ЛОКАЛЬНОГО ПРОМТА\n"
                "========================\n\n"
                "Этот файл создан автоматически, чтобы программа работала сразу\n"
                "после запуска. Замените его своим промтом:\n\n"
                "  * положите в папку «prompts» файл .pdf, .txt или .md;\n"
                "  * нажмите кнопку «Промт» и выберите его.\n\n"
                "Имя файла станет именем вложения в чате (например, Memphis.pdf).\n"
            )
            (self.folder / "Пример_промта.txt").write_text(body, encoding="utf-8")
            Log.info("prompts: создан пример промта")
        except Exception:
            Log.exc("prompts.example")

    def build_pdf(self, filename: str) -> tuple[bytes, str]:
        """Вернуть (байты PDF, имя PDF для чата). Никаких сетевых вызовов.

        Поддерживаются:
          * `.pdf`            — берётся как есть;
          * `.enc/.bin/.dat`  — расшифровывается ключом серверной версии,
                                внутри ожидается PDF (или текст — тогда PDF
                                собирается на месте);
          * `.txt/.md/.json`  — собирается PDF шрифтом DejaVu (кириллица).
        """
        source = self.folder / filename
        if not source.is_file():
            raise FileNotFoundError(f"Промт не найден: {source}")
        raw = source.read_bytes()
        suffix = source.suffix.lower()
        target_name = pdf_file_name(filename)

        if suffix == ".pdf" or raw[:4] == b"%PDF":
            return raw, target_name

        if suffix in (".enc", ".bin", ".dat"):
            plain = _legacy_decrypt(raw)
            if not plain:
                raise ValueError(
                    f"Не удалось расшифровать «{filename}». Это промт старого формата — "
                    "положите в папку prompts обычный PDF."
                )
            if plain[:4] == b"%PDF":
                return plain, target_name
            return text_to_pdf_bytes(plain, title=prompt_display_name(filename)), target_name

        return text_to_pdf_bytes(raw, title=prompt_display_name(filename)), target_name


# =============================================================================
# 8. CDP — единственный способ управлять страницей
# =============================================================================

class CDP:
    """Мини-клиент DevTools: send() и eval(). Ровно как в серверной версии."""

    def __init__(self, ws_url: str):
        from websocket import create_connection

        self.ws = create_connection(ws_url, suppress_origin=True, max_size=None,
                                    timeout=5,
                                    http_no_proxy=["127.0.0.1", "localhost"])
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError(f"CDP busy: {method}")
        try:
            self._id += 1
            mid = self._id
            payload = json.dumps({"id": mid, "method": method, "params": params or {}})
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            self.ws.settimeout(left)
            self.ws.send(payload)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(method)
                self.ws.settimeout(left)
                raw = self.ws.recv()
                if not raw:
                    raise ConnectionError("CDP socket closed")
                message = json.loads(raw)
                if message.get("id") != mid:
                    continue          # события страницы не наш ответ
                if "error" in message:
                    error = message.get("error") or {}
                    raise RuntimeError(f"CDP {method}: {error.get('message')}")
                return message.get("result", {}) or {}
        finally:
            self._lock.release()

    def eval(self, expression: str, timeout: float = 10, await_promise: bool = False) -> dict:
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
            "userGesture": True,
            "generatePreview": False,
        }, timeout=timeout)
        if isinstance(result, dict) and "exceptionDetails" in result:
            details = result.get("exceptionDetails") or {}
            raise RuntimeError(str(details.get("text", "JS exception")))
        return result.get("result", {}) if isinstance(result, dict) else {}

    def value(self, expression: str, timeout: float = 10, default=None):
        """Результат JS-выражения или `default`, если страница недоступна."""
        try:
            return self.eval(expression, timeout=timeout).get("value", default)
        except Exception:
            return default

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def http_json(port: int, path: str, timeout: float = 1.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.load(response)


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def is_wall(url: str) -> bool:
    """Страница согласия/входа/ошибки — автоматизацию на неё не вешаем."""
    raw = (url or "").lower()
    host = host_of(url)
    if host in ("consent.google.com", "accounts.google.com"):
        return True
    return raw.startswith(("chrome-error:", "about:blank"))


def choose_target(targets: list) -> dict | None:
    """Выбрать страницу чата, а не стену согласия.

    Серверная версия когда-то цеплялась к `consent.google.com` и «висела» —
    здесь порядок жёсткий: чат → любая обычная → стена (последняя).
    """
    pages = [t for t in (targets or [])
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    real = [p for p in pages if (p.get("url") or "").lower().startswith(("http://", "https://"))]
    chat = [p for p in real if "gemini.google.com" in (p.get("url") or "")
            or "aistudio." in (p.get("url") or "")
            or "/ai" in (p.get("url") or "")]
    neutral = [p for p in real if not is_wall(p.get("url") or "")]
    walls = [p for p in real if is_wall(p.get("url") or "")]
    for group in (chat, neutral, walls, real):
        if group:
            return group[0]
    return None


def attach(port: int, timeout: float = 30.0):
    """Дождаться DevTools и вернуть (browser, page)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            version = http_json(port, "/json/version")
            targets = http_json(port, "/json/list")
            chosen = choose_target(targets)
            browser_ws = version.get("webSocketDebuggerUrl")
            if browser_ws and chosen:
                browser = CDP(browser_ws)
                try:
                    return browser, CDP(chosen["webSocketDebuggerUrl"])
                except Exception:
                    browser.close()
                    raise
        except Exception:
            pass
        time.sleep(0.15)
    raise TimeoutError("DevTools не отвечает")


def grant_microphone(browser: CDP, page: CDP) -> None:
    """Разрешение на микрофон через CDP (плюс оно записано в профиль)."""
    origins = {"https://google.com", "https://www.google.com",
               "https://gemini.google.com", "https://accounts.google.com"}
    try:
        origin = page.value("location.origin", timeout=3)
        if origin and origin != "null":
            origins.add(origin)
    except Exception:
        pass
    context = None
    try:
        ids = browser.send("Target.getBrowserContexts", timeout=3).get("browserContextIds") or []
        if ids:
            context = ids[0]
    except Exception:
        pass
    for origin in origins:
        params = {"origin": origin, "permissions": ["audioCapture", "videoCapture"]}
        if context:
            params["browserContextId"] = context
        try:
            browser.send("Browser.grantPermissions", params, timeout=3)
        except Exception:
            continue

# =============================================================================
# 9. JS ДЛЯ СТРАНИЦЫ
# =============================================================================
#
#  Все скрипты возвращают в Qt типы, которые JSON может отдать: числа, строки,
#  массивы строк, объекты без вложенных функций и без DOM-узлов.
# -----------------------------------------------------------------------------

READY_SCRIPT = r"""(() => {
    if (location.protocol !== 'https:') return false;
    const host = location.hostname;
    if (!['google.com','www.google.com','gemini.google.com','aistudio.google.com']
            .includes(host)) return false;
    if (!['interactive','complete'].includes(document.readyState)) return false;
    return Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]'))
        .some(e => e.getClientRects().length > 0 && !e.disabled && !e.readOnly);
})()"""

#: Состояние чата: сколько чипов, имена, идёт ли ещё загрузка, есть ли поле ввода.
#: Возвращается одним вызовом — это единственный «тяжёлый» опрос страницы.
JS_CHECK_FILES = r"""(() => {
const chips = Array.from(document.querySelectorAll('div.ArblTe'));
let done = 0, uploading = false, named = 0;
const names = [];
for (const chip of chips) {
    let fname = '';
    try {
        const host = chip.closest('div[role="button"]');
        if (host) {
            fname = (host.getAttribute('title') || host.getAttribute('aria-label') || '').trim();
            if (!fname) {
                const label = host.querySelector('div.zrI2ad');
                const txt = (label ? label.innerText : host.innerText) || '';
                fname = txt.replace(/\s+/g, ' ').trim();
            }
        }
        if (!fname) fname = ((chip.innerText || '').replace(/\s+/g, ' ').trim());
        fname = fname.slice(0, 160);
    } catch (e) { fname = ''; }
    if (chip.querySelector('div.zXgSbc')) { done += 1; if (fname) named += 1; names.push(fname); continue; }
    const bar = chip.querySelector('div[data-progressvalue]');
    if (bar) {
        const val = parseFloat(bar.getAttribute('data-progressvalue') || '0');
        if (val < 0.99) { uploading = true; continue; }
    }
    done += 1; if (fname) named += 1; names.push(fname);
}
let composer = 0;
try {
    composer = Array.from(document.querySelectorAll(
        'textarea, div[contenteditable="true"], [role="textbox"]'))
        .filter(e => e.getClientRects().length > 0 && !e.disabled && !e.readOnly).length;
} catch (e) { composer = 0; }
return {present: done, named: named, uploading: uploading, names: names,
        chips: chips.length, composer: composer};
})()"""

#: Удалить ОДИН последний чип: лишние уходят по одному, с проверкой после каждого.
JS_REMOVE_LAST = r"""(() => {
const chips = Array.from(document.querySelectorAll('div.ArblTe'));
if (!chips.length) return 0;
const chip = chips[chips.length - 1];
const fire = (el) => {
    try {
        const r = el.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const opts = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y};
        try { el.dispatchEvent(new PointerEvent('pointerdown', {...opts, pointerId: 1, pointerType: 'mouse', isPrimary: true})); } catch (e) {}
        el.dispatchEvent(new MouseEvent('mousedown', opts));
        try { el.dispatchEvent(new PointerEvent('pointerup', {...opts, pointerId: 1, pointerType: 'mouse', isPrimary: true})); } catch (e) {}
        el.dispatchEvent(new MouseEvent('mouseup', opts));
        el.dispatchEvent(new MouseEvent('click', opts));
        return true;
    } catch (e) { return false; }
};
let target = chip.querySelector('div.zXgSbc') || chip.querySelector('button[aria-label]');
if (target && fire(target)) return 1;
const parent = chip.closest('div[role="button"]');
if (parent && fire(parent)) return 1;
return 0;
})()"""

JS_REMOVE_ALL_FILES = r"""(() => {
const chips = Array.from(document.querySelectorAll('div.ArblTe'));
let clicked = 0;
for (const chip of chips) {
    const x = chip.querySelector('div.zXgSbc');
    if (!x) continue;
    try {
        const r = x.getBoundingClientRect();
        const opts = {bubbles: true, cancelable: true, view: window,
                      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
        x.dispatchEvent(new MouseEvent('pointerdown', opts));
        x.dispatchEvent(new MouseEvent('mousedown', opts));
        x.dispatchEvent(new MouseEvent('mouseup', opts));
        x.dispatchEvent(new MouseEvent('click', opts));
        clicked += 1;
    } catch (e) {}
}
return clicked;
})()"""

#: Кнопки страницы: состояние «пишет ли страница прямо сейчас».
JS_CHECK_RECORDING = r"""(() => {
try {
    if (document.querySelector('.tFTltc, .S7I6ve, .S34Aff, .r5nqdd')) return {recording: true};
    if (document.querySelector('button[data-xid="input-plate-voice-send-button"]')) return {recording: true};
    const nodes = document.querySelectorAll('.pRjbAe, [aria-live="polite"]');
    for (const el of nodes) {
        if (el.innerText && el.innerText.indexOf('Преобразование речи') !== -1) return {recording: true};
    }
    const ta = document.querySelector('textarea.ITIRGe, textarea[aria-label="Задайте вопрос"]');
    if (ta && (ta.hidden || ta.style.display === 'none')) return {recording: true};
} catch (e) {}
return {recording: false};
})()"""

#: Общий «нажиматель»: один набор событий мыши для любой кнопки страницы.
_JS_CLICK = r"""
const fire = (el) => {
    try { el.disabled = false; } catch (e) {}
    try { el.removeAttribute('disabled'); } catch (e) {}
    try { el.setAttribute('aria-disabled', 'false'); } catch (e) {}
    try { el.focus(); } catch (e) {}
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const opts = {bubbles: true, cancelable: true, view: window,
                  clientX: x, clientY: y, button: 0, buttons: 1};
    try { el.dispatchEvent(new PointerEvent('pointerdown', {...opts, pointerId: 1, pointerType: 'mouse', isPrimary: true, pressure: 0.5})); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent('mousedown', opts)); } catch (e) {}
    try { el.dispatchEvent(new PointerEvent('pointerup', {...opts, buttons: 0, pressure: 0})); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent('mouseup', {...opts, buttons: 0})); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent('click', {...opts, buttons: 0})); } catch (e) {}
    try { el.click(); } catch (e) {}
    return {ok: true, x: x, y: y};
};
"""

#: Поиск кнопки микрофона на странице (наборы из серверной версии, проверенные
#: на живой странице: data-xid, aria-label на нескольких языках, SVG-иконка).
_JS_FIND_MIC = r"""
const findMic = () => {
    const selectors = [
        'button[data-xid="input-plate-voice-button"]',
        'button[data-xid="h4gG8"]',
        'button.uMMzHc.vpw7Fc',
        'button.vpw7Fc',
        'button[aria-label="Микрофон"]',
        'button[aria-label="Использовать микрофон"]',
        'button[aria-label*="икрофон" i]',
        'button[aria-label*="дикт" i]',
        'button[aria-label*="Microphone" i]',
        'button[aria-label*="voice" i]:not([aria-label*="send" i])',
        'div[role="button"][aria-label*="икрофон" i]'
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.getClientRects().length) return el;
    }
    const area = document.querySelector('.esoFne, .Txyg0d, footer, form') || document.body;
    for (const btn of Array.from(area.querySelectorAll('button, div[role="button"]'))) {
        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
        const xid = (btn.getAttribute('data-xid') || '').toLowerCase();
        if ((label.includes('микрофон') || label.includes('mic') || xid.includes('voice'))
                && !xid.includes('send')) return btn;
    }
    return null;
};
"""

#: Поиск кнопки отправки (голосовую отправку, обычную, Enter как резерв).
_JS_FIND_SEND = r"""
const findSend = () => {
    const selectors = [
        'button[data-xid="input-plate-voice-send-button"]',
        'button.wdK4Nc',
        'button[aria-label="Отправить"]',
        'button[data-xid*="voice-send"]',
        'button[data-xid*="send"]',
        'button[aria-label*="тправить" i]',
        'button[aria-label*="end" i]',
        'button[aria-label*="ubmit" i]',
        'div[role="button"][aria-label*="тправить" i]'
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.getClientRects().length) return el;
    }
    const area = document.querySelector('.esoFne, .Txyg0d, footer, form') || document.body;
    for (const btn of Array.from(area.querySelectorAll('button, div[role="button"]'))) {
        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
        const xid = (btn.getAttribute('data-xid') || '').toLowerCase();
        if (label.includes('отправ') || label.includes('send') || xid.includes('send')) return btn;
    }
    return null;
};
"""

_JS_PRESS_ENTER = r"""
const inputEl = document.querySelector('textarea, div[contenteditable="true"], div[role="textbox"]');
if (inputEl) {
    try { inputEl.focus(); } catch (e) {}
    const opts = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true};
    inputEl.dispatchEvent(new KeyboardEvent('keydown', opts));
    inputEl.dispatchEvent(new KeyboardEvent('keypress', opts));
    inputEl.dispatchEvent(new KeyboardEvent('keyup', opts));
    return {ok: true, fallback: 'enter'};
}
return {ok: false, reason: 'not_found'};
"""

#: ВАЖНО: `fire` объявляется ПЕРВЫМ. Раньше вспомогательные функции шли после
#: `return fire(el)` — `const` в temporal dead zone давал ReferenceError, и
#: кнопки на странице «не нажимались» без внятной причины.
JS_START_RECORDING = ("(() => {" + _JS_CLICK + _JS_FIND_MIC + """
const el = findMic();
if (!el) return {ok: false, reason: 'not_found'};
return fire(el);
})()""")

JS_STOP_AND_SEND = ("(() => {" + _JS_CLICK + _JS_FIND_SEND + """
const el = findSend();
if (el) return fire(el);
""" + _JS_PRESS_ENTER + "})()")

#: Светлая тема: на Windows с тёмной темой Chrome рисует чат тёмным.
JS_LIGHT_THEME = r"""(() => {
let changed = 0;
try {
    const root = document.documentElement;
    if (root) {
        root.style.setProperty('color-scheme', 'light', 'important');
        root.setAttribute('data-legalyze-theme', 'light');
        changed += 1;
    }
} catch (e) {}
try {
    let meta = document.querySelector('meta[name="color-scheme"]');
    if (!meta) {
        meta = document.createElement('meta');
        meta.setAttribute('name', 'color-scheme');
        (document.head || document.documentElement).appendChild(meta);
    }
    meta.setAttribute('content', 'light');
    changed += 1;
} catch (e) {}
return changed;
})()"""

#: Язык распознавания речи: настоящий SpeechRecognition не подменяем (иначе
#: перестанет работать сервис Google), принудительно держим только `lang`.
JS_SPEECH_LANG = r"""(() => {
const LANG = '%s';
try { document.documentElement.setAttribute('lang', LANG); } catch (e) {}
const Native = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!Native) return 'no-native';
if (Native.__legalyzeLang) return 'already';
try {
    const desc = Object.getOwnPropertyDescriptor(Native.prototype, 'lang');
    const Patched = function () {
        const rec = new Native();
        try { if (desc && desc.set) desc.set.call(rec, LANG); } catch (e) {}
        try { rec.lang = LANG; } catch (e) {}
        try {
            Object.defineProperty(rec, 'lang', {
                configurable: true,
                get() { try { return desc && desc.get ? desc.get.call(rec) : LANG; }
                        catch (e) { return LANG; } },
                set(_v) { try { if (desc && desc.set) desc.set.call(rec, LANG); } catch (e) {} }
            });
        } catch (e) {}
        return rec;
    };
    Patched.prototype = Native.prototype;
    Patched.__legalyzeLang = true;
    window.SpeechRecognition = Patched;
    window.webkitSpeechRecognition = Patched;
    return 'patched';
} catch (e) {
    return 'error';
}
})()"""

#: Авто-клик согласия Google (EU/новый профиль): без него чат не открывается.
JS_CONSENT = r"""(() => {
const norm = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
const ACCEPT = /(accept all|allow all|принять все|принять всё|aceptar todo|alle akzeptieren|tout accepter|accetta tutto|aceitar tudo|tümünü kabul|zaakceptuj wszystko|alles accepteren|прийняти всі)/i;
const REJECT = /(reject|decline|отклон|настро|manage|more option|learn more|подробн)/i;
let clicked = 0;
const nodes = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"]'));
for (const el of nodes) {
    const label = norm(el.getAttribute && (el.getAttribute('aria-label') || el.innerText));
    if (!label || label.length > 64) continue;
    if (REJECT.test(label)) continue;
    if (!ACCEPT.test(label)) continue;
    try { el.click(); clicked += 1; } catch (e) {}
}
if (!clicked) {
    for (const sel of ['button#L2AGLb', 'button[jsname="tWT92d"]', 'form[action*="consent"] button']) {
        try {
            const el = document.querySelector(sel);
            if (el) { el.click(); clicked += 1; break; }
        } catch (e) {}
    }
}
return clicked;
})()"""

#: Убрать со страницы лишние элементы (шапка Google, меню аккаунта, снекбары).
#: Панель ввода, микрофон, отправка и чипы файлов защищены явно.
JS_PURGE = r"""(() => {
    const SELECTORS = [
        'div.qEn1od[jsname="NlVIob"]', 'div.qEn1od', 'div.P3mIxe.Hw60ud',
        'div.FSUH7d[jsname="xcvsnc"]', 'div.GG4mbd[role="navigation"]',
        'div.eT9Cje', 'span.gb', 'header#gb', 'div#gbwa',
        'g-snackbar[jsname="PWj1Zb"]'
    ];
    const S = SELECTORS.join(',');
    const INPUT_AREA = 'form, footer, .esoFne, .Txyg0d, .CEpIFc, [role="region"], [data-xid*="input-plate"]';
    const PROTECTED = /икрофон|микрофон|mic|voice|голос|отправ|send|dictat|дикт|вопрос|attach|прикреп|файл|file/i;
    const isProtected = (el) => {
        try {
            if (!el || el.nodeType !== 1) return true;
            if (el.closest && el.closest(INPUT_AREA)) return true;
            const label = (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('data-xid')
                || el.getAttribute('title'))) || '';
            if (PROTECTED.test(label)) return true;
            return PROTECTED.test((el.textContent || '').slice(0, 120));
        } catch (e) { return true; }
    };
    const hide = (el) => {
        if (!el || isProtected(el)) return;
        try { el.style.setProperty('display', 'none', 'important'); } catch (e) {}
        try { el.remove(); } catch (e) {}
    };
    const kill = (node) => {
        if (!node || node.nodeType !== 1) return;
        if (node.matches && node.matches(S)) { hide(node); return; }
        if (node.querySelectorAll) {
            const list = node.querySelectorAll(S);
            for (let i = list.length - 1; i >= 0; i--) hide(list[i]);
        }
    };
    if (window.__purgeInstalled) { kill(document.documentElement); return 'again'; }
    window.__purgeInstalled = true;
    kill(document.documentElement);
    try {
        const obs = new MutationObserver((mutations) => {
            for (const m of mutations) {
                if (m.type === 'childList') { for (const n of m.addedNodes) kill(n); }
                else if (m.type === 'attributes') { kill(m.target); }
            }
        });
        obs.observe(document.documentElement, {childList: true, subtree: true,
                                               attributes: true,
                                               attributeFilter: ['class', 'id', 'jsname']});
    } catch (e) {}
    return 'installed';
})()"""

#: После показа/переезда окна: заставить страницу перерисоваться.
JS_REVIVE = r"""(() => {
try { window.dispatchEvent(new Event('resize')); } catch (e) {}
try {
    document.documentElement && document.documentElement.style &&
        document.documentElement.style.setProperty('color-scheme', 'light', 'important');
} catch (e) {}
try { const b = document.body; if (b) { b.offsetHeight; } } catch (e) {}
return true;
})()"""


def speech_lang_js(lang: str = SPEECH_LANG) -> str:
    return JS_SPEECH_LANG % str(lang or SPEECH_LANG)


#: Размер куска base64 в одном CDP-вызове. PDF на 2–3 МБ уходил одним
#: сообщением ~4 МБ и растягивал страницу; частями — ровно и предсказуемо.
INJECT_CHUNK = 1_200_000

#: Впрыск ОДНОГО файла. Механизм ровно один:
#:   1) `input[type=file]` + `change` — если поле файла на странице есть;
#:   2) `dragenter/dragover/drop` — ТОЛЬКО если поля файла нет;
#:   3) `DOM.setFileInputFiles` — последний резерв (в Python).
#: Именно «оба механизма сразу», как было в серверной версии, и создавало
#: второй чип на тот же файл.
INJECT_FILE_JS = r"""(() => {
    const store = window.__llz || (window.__llz = {});
    const name = "%(name)s";
    const mime = "%(mime)s";
    let file = null;
    try {
        if (store.pending && store.pending.name === name) {
            file = store.pending.file;
            store.pending = null;
        }
        // Повторная попытка того же файла: он уже лежит в странице, повторно
        // мегабайты base64 не гоняем.
        if (!file && store.file && store.name === name) {
            file = store.file;
        }
    } catch (e) {}
    if (!file) {
        const b64 = (store.parts || []).join('');
        store.parts = [];
        if (!b64) return {ok: false, reason: 'no-data'};
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        file = new File([bytes], name, {type: mime});
        store.file = file;
    }
    const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
    let matched = false;
    for (const input of inputs) {
        try {
            const dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.dispatchEvent(new Event('input', {bubbles: true}));
            matched = true;
        } catch (e) {}
    }
    if (!matched) {
        const zone = document.querySelector('.esoFne') || document.querySelector('textarea')
                  || document.querySelector('[role="textbox"]');
        if (zone) {
            try {
                const dt = new DataTransfer();
                dt.items.add(file);
                zone.dispatchEvent(new DragEvent('dragenter', {bubbles: true, cancelable: true, dataTransfer: dt}));
                zone.dispatchEvent(new DragEvent('dragover', {bubbles: true, cancelable: true, dataTransfer: dt}));
                zone.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt}));
                matched = true;
            } catch (e) {}
        }
    }
    return {ok: matched, name: name, bytes: file.size, inputs: inputs.length,
            mode: matched ? (inputs.length ? 'input' : 'drop') : 'none'};
})()"""

#: Кладём File в страницу ОДИН раз, кусками; сам впрыск потом берёт его из
#: `window.__llz.file`. Так 3 МБ PDF не превращаются в одно гигантское
#: JS-выражение, а повторный впрыск не пересылает данные заново.
STASH_INIT_JS = r"""(() => {
    const store = window.__llz || (window.__llz = {});
    store.parts = [];
    store.pending = null;
    store.file = null;
    store.name = "%(name)s";
    return true;
})()"""

STASH_PUSH_JS = r"""(() => {
    const store = window.__llz || (window.__llz = {});
    store.parts = store.parts || [];
    store.parts.push("%(chunk)s");
    return store.parts.length;
})()"""

STASH_COMMIT_JS = r"""(() => {
    const store = window.__llz || (window.__llz = {});
    const name = "%(name)s";
    const mime = "%(mime)s";
    const b64 = (store.parts || []).join('');
    store.parts = [];
    if (!b64) return {ok: false, reason: 'no-data'};
    try {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const file = new File([bytes], name, {type: mime});
        store.pending = {name: name, file: file};
        store.file = file;
        store.size = bytes.length;
        return {ok: true, size: bytes.length};
    } catch (e) {
        return {ok: false, reason: String(e)};
    }
})()"""


def mime_for(path: Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in (".txt", ".md", ".json", ".csv"):
        return "text/plain"
    return "application/octet-stream"


def js_name(value: str) -> str:
    """Безопасная подстановка имени файла в JS-строку."""
    return (str(value).replace("\\", "").replace('"', "").replace("\n", "")
            .replace("\r", "").replace("'", ""))


def stash_chunks(data: bytes) -> list[str]:
    encoded = base64.b64encode(data).decode("ascii")
    return [encoded[i:i + INJECT_CHUNK] for i in range(0, len(encoded), INJECT_CHUNK)]

# =============================================================================
# 10. БРАУЗЕР: поиск, профиль, запуск
# =============================================================================

#: Зум страницы. Chrome хранит его как «zoom level»: factor = 1.2 ** level.
ZOOM = 2.0 / 3.0
ZOOM_LEVEL = math.log(ZOOM) / math.log(1.2)
ZOOM_HOSTS = ("google.com", "www.google.com", "gemini.google.com",
              "aistudio.google.com", "accounts.google.com")

#: Флаги против «лишнего хрома» интерфейса. Один `--disable-features`: второй
#: такой же ключ перезаписал бы список целиком.
DISABLED_FEATURES = ("SystemTitlebar", "CaptionButtons", "Translate",
                     "OptimizationHints", "MediaRouter", "GlobalMediaControls",
                     "DialMediaRouteProvider", "InProductHelp", "MenuCommands",
                     "AcceptCHFrame")
#: Авто-тёмная тема содержимого (из-за неё чат был тёмным на тёмной Windows).
FORCE_LIGHT_FEATURES = ("WebContentsForceDark", "DarkMode")

BROWSER_WINDOW_CLASS = "Chrome_WidgetWin_1"


class Candidate:
    __slots__ = ("name", "kind", "path", "rank")

    def __init__(self, name: str, kind: str, path: str, rank: int):
        self.name = name
        self.kind = kind
        self.path = str(path)
        self.rank = rank

    @property
    def branded(self) -> bool:
        return self.kind in ("chrome", "portable", "chrome-beta", "chrome-dev", "chrome-canary")


def _registry_app_path(exe_name: str) -> str:
    """Путь из `App Paths\\<exe>` (Chrome/Edge регистрируются там)."""
    if not IS_WINDOWS:
        return ""
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
                                        0, winreg.KEY_READ | view) as key:
                        value = winreg.QueryValueEx(key, "")[0]
                        if value and Path(str(value)).is_file():
                            return str(value)
                except Exception:
                    continue
    except Exception:
        Log.exc("browser.registry")
    return ""


def discover_browsers(app_dir: Path | None = None, configured: str | None = None) -> list[Candidate]:
    """Настоящие Chromium-браузеры в порядке предпочтения.

    Микрофон страницы (Web Speech API) есть только в брендированных сборках,
    поэтому порядок такой: закреплённая сборка рядом с программой → Chrome →
    каналы Chrome → Edge → прочие Chromium.
    """
    app_dir = Path(app_dir or ROOT_DIR)
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    pf = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
    pf86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")

    out: list[Candidate] = []

    def add(name: str, kind: str, path, rank: int) -> None:
        if not path:
            return
        try:
            if Path(path).is_file():
                out.append(Candidate(name, kind, path, rank))
        except Exception:
            pass

    add("configured", "configured", configured, 0)
    for sub in ("browser", "chromium"):
        for exe in ("chrome.exe", "chromium.exe"):
            add("portable", "portable", app_dir / sub / exe, 1)
    add("portable", "portable", app_dir / "chrome.exe", 1)

    add("chrome", "chrome", _registry_app_path("chrome.exe"), 2)
    for root in (pf, pf86, local):
        add("chrome", "chrome", root / "Google" / "Chrome" / "Application" / "chrome.exe", 2)
    for sub_dir, label in (("Chrome Beta", "chrome-beta"), ("Chrome Dev", "chrome-dev"),
                           ("Chrome SxS", "chrome-canary")):
        add(label, "chrome", pf / "Google" / sub_dir / "Application" / "chrome.exe", 2)
        add(label, "chrome", local / "Google" / sub_dir / "Application" / "chrome.exe", 2)

    add("edge", "edge", _registry_app_path("msedge.exe"), 3)
    for root in (pf86, pf):
        add("edge", "edge", root / "Microsoft" / "Edge" / "Application" / "msedge.exe", 3)

    for root in (local, pf):
        add("yandex", "chromium", root / "Yandex" / "YandexBrowser" / "Application" / "browser.exe", 4)
        add("opera", "chromium", root / "Programs" / "Opera" / "launcher.exe", 4)
        add("opera", "chromium", root / "Opera" / "launcher.exe", 4)
        add("brave", "chromium", root / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe", 4)
        add("vivaldi", "chromium", root / "Vivaldi" / "Application" / "vivaldi.exe", 5)
    add("chromium", "chromium", local / "Chromium" / "Application" / "chrome.exe", 5)

    best: dict[str, Candidate] = {}
    for cand in out:
        try:
            key = str(Path(cand.path).resolve()).lower()
        except Exception:
            key = cand.path.lower()
        if key not in best or cand.rank < best[key].rank:
            best[key] = cand
    ordered = sorted(best.values(), key=lambda c: (c.rank, c.name))
    Log.info("браузеры найдены: " + ", ".join(f"{c.kind}:{c.path}" for c in ordered[:6]))
    return ordered


def _chrome_timestamp() -> str:
    """Время в системе Chrome: микросекунды с 1601 года."""
    try:
        import datetime as _dt

        epoch = _dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc)
        now = _dt.datetime.now(_dt.timezone.utc)
        return str(int((now - epoch).total_seconds() * 1_000_000))
    except Exception:
        return "0"


def write_profile_prefs(profile_dir: Path, lang: str, light_theme: bool) -> None:
    """Один проход по `Preferences`: зум 67 %, язык и право микрофона.

    Это ровно то, что Chrome делает сам после `Ctrl+-` до 67 % и кнопки
    «Разрешить» для микрофона — поэтому ни флага про небезопасный запуск
    (`--use-fake-ui-for-media-stream`), ни жёлтой полосы не появляется.
    Без зума страница верстается шире окна и текст уезжает за правый край.
    """
    stamp = _chrome_timestamp()
    accepted = f"{lang},{lang.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    for sub in ("Default", ""):
        base = Path(profile_dir) / sub if sub else Path(profile_dir)
        try:
            base.mkdir(parents=True, exist_ok=True)
            path = base / "Preferences"
            prefs: dict = {}
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        prefs = loaded
                except Exception:
                    prefs = {}

            partition = prefs.setdefault("partition", {})
            if not isinstance(partition, dict):
                partition = {}
                prefs["partition"] = partition
            partition["default_zoom_level"] = {"x": ZOOM_LEVEL}
            per_host = partition.setdefault("per_host_zoom_levels", {})
            if not isinstance(per_host, dict):
                per_host = {}
                partition["per_host_zoom_levels"] = per_host
            bucket = per_host.setdefault("x", {})
            if not isinstance(bucket, dict):
                bucket = {}
                per_host["x"] = bucket
            for host in ZOOM_HOSTS:
                bucket[host] = {"zoom_level": ZOOM_LEVEL, "last_modified": stamp}

            intl = prefs.setdefault("intl", {})
            if isinstance(intl, dict):
                intl["accept_languages"] = accepted
                intl["selected_languages"] = accepted

            profile = prefs.setdefault("profile", {})
            if isinstance(profile, dict):
                settings = profile.setdefault("content_settings", {})
                if isinstance(settings, dict):
                    exceptions = settings.setdefault("exceptions", {})
                    if isinstance(exceptions, dict):
                        for kind in ("media_stream_mic", "media_stream_camera"):
                            kind_bucket = exceptions.setdefault(kind, {})
                            if not isinstance(kind_bucket, dict):
                                kind_bucket = {}
                                exceptions[kind] = kind_bucket
                            for host in ZOOM_HOSTS:
                                for pattern in (f"https://{host}:443,*",
                                                f"https://[*.]{host}:443,*"):
                                    kind_bucket[pattern] = {"last_modified": stamp,
                                                            "setting": 1,
                                                            "secondary_pattern": "*"}

            if light_theme:
                # Предпочтение темы: без него Chromium может включить тёмный UI.
                browser = prefs.setdefault("browser", {})
                if isinstance(browser, dict):
                    browser["theme"] = "light"

            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            Log.exc("browser.prefs")


def build_args(exe: str, port: int, profile: Path, url: str, width: int, height: int,
               gpu: bool, staging: tuple[int, int], lang: str, light_theme: bool) -> list[str]:
    """Ключи запуска: проверенные в рабочей сборке + стабильность."""
    args = [
        exe,
        f"--remote-debugging-port={int(port)}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        f"--app={url}",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-window",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--noerrdialogs",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=" + ",".join(
            tuple(DISABLED_FEATURES) + (tuple(FORCE_LIGHT_FEATURES) if light_theme else ())),
        f"--window-position={int(staging[0])},{int(staging[1])}",
        f"--window-size={int(width)},{int(height)}",
    ]
    if lang:
        args.append(f"--lang={lang}")
    if not gpu:
        args.extend(["--disable-gpu", "--disable-gpu-compositing", "--disable-software-rasterizer"])
    return args


#: В этих сборках штатного микрофона страницы может не быть: Web Speech API
#: там либо отсутствует, либо не возвращает результат. Программа работает,
#: но голосовой ввод недоступен — об этом честно сообщается человеку.
WEAK_SPEECH_KINDS = ("edge", "chromium", "opera", "vivaldi", "brave", "yandex")


class BrowserProcess:
    """Процесс браузера и его DevTools-порт. Ничего не знает про Qt."""

    def __init__(self, candidate: Candidate, url: str, profile: Path,
                 lang: str, light_theme: bool, gpu: bool = True, attempt: int = 0):
        self.candidate = candidate
        self.url = url
        self.lang = lang
        self.light_theme = light_theme
        self.gpu = gpu
        self.attempt = max(0, int(attempt))
        self.profile_base = Path(profile)
        self.profile_dir = self.profile_base if self.attempt == 0 else \
            Path(f"{self.profile_base}-retry{self.attempt}")
        self.port = 0
        self.proc: subprocess.Popen | None = None

    def launch(self, width: int, height: int, staging: tuple[int, int]) -> subprocess.Popen:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = int(sock.getsockname()[1])
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        write_profile_prefs(self.profile_dir, self.lang, self.light_theme)
        args = build_args(self.candidate.path, self.port, self.profile_dir, self.url,
                          width, height, self.gpu, staging, self.lang, self.light_theme)
        startupinfo = None
        creationflags = 0
        if IS_WINDOWS:
            creationflags = 0x08000000  # CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE: окно есть, фокус не крадёт
        Log.info(f"запуск браузера: {self.candidate.kind} порт={self.port} "
                 f"попытка={self.attempt} gpu={self.gpu}")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=creationflags, startupinfo=startupinfo)
        return self.proc

    def probe(self) -> tuple[str, dict | None, list | None]:
        """Неблокирующий опрос: ('waiting'|'ready'|'exited', version, targets)."""
        if self.proc is not None and self.proc.poll() is not None:
            return "exited", None, None
        try:
            return "ready", http_json(self.port, "/json/version", 0.8), \
                http_json(self.port, "/json/list", 0.8)
        except Exception:
            return "waiting", None, None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def pid(self) -> int:
        return int(self.proc.pid) if self.proc else 0

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                kill_tree({proc.pid})
        except Exception:
            Log.exc("browser.close")
        Log.info(f"браузер остановлен (порт {self.port})")


# =============================================================================
# 11. WIN32: вживление окна браузера в окно программы
# =============================================================================

def _windows_of(pids: set[int]) -> list[dict]:
    """Все окна указанных процессов с их прямоугольниками."""
    found: list[dict] = []
    if not user32 or not pids or WNDENUMPROC is None:
        return found

    def callback(hwnd, _lparam):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_of(hwnd), ctypes.byref(pid))
            if pid.value not in pids:
                return True
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd_of(hwnd), buffer, 256)
            name = buffer.value
            if not name.startswith("Chrome_WidgetWin"):
                return True
            left, top, right, bottom = window_rect(hwnd)
            handle = handle_value(hwnd)
            found.append({
                "hwnd": handle,
                "class": name,
                "visible": bool(user32.IsWindowVisible(hwnd_of(hwnd))),
                "parent": handle_value(user32.GetParent(hwnd_of(hwnd))),
                "owner": handle_value(user32.GetWindow(hwnd_of(hwnd), GW_OWNER)),
                "self_root": handle_value(user32.GetAncestor(hwnd_of(hwnd),
                                                             GA_ROOTOWNER)) == handle,
                "rect": (left, top, right, bottom),
                "area": max(0, right - left) * max(0, bottom - top),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def find_browser_window(pids: set[int], timeout: float = 30.0) -> int:
    """Самое большое видимое окно браузера верхнего уровня.

    Служебные окна Chromium (`Chrome_WidgetWin_0`) отбрасываются: вживить их
    значит показать пустое окно без страницы. Если окон класса `_1` нет вовсе,
    берём любое большое — лучше хоть какое-то окно, чем ничего.
    """
    deadline = time.monotonic() + max(1.0, timeout)
    wanted = set(pids) | process_tree(pids)
    while time.monotonic() < deadline:
        windows = [w for w in _windows_of(wanted)
                   if w["visible"] and not w["parent"]
                   and (w["rect"][2] - w["rect"][0]) >= 120
                   and (w["rect"][3] - w["rect"][1]) >= 120]
        eligible = [w for w in windows if w["class"] == BROWSER_WINDOW_CLASS] or windows
        if eligible:
            best = max(eligible, key=lambda w: w["area"])
            Log.info(f"окно браузера найдено: hwnd={best['hwnd']} class={best['class']}")
            return best["hwnd"]
        wanted |= process_tree(wanted)
        time.sleep(0.2)
    Log.warn("окно браузера не найдено за отведённое время")
    return 0


def is_child_of(child: int, parent: int) -> bool:
    """Действительно ли окно встроено в нужного родителя (проверка, не надежда)."""
    if not user32 or not child or not parent:
        return False
    try:
        return handle_value(user32.GetParent(hwnd_of(child))) == int(parent)
    except Exception:
        return False


def embed_window(hwnd: int, parent: int, inset: tuple[int, int, int, int],
                 cover: int = 0, timeout: float = 4.0) -> bool:
    """Сделать окно браузера ДОЧЕРНИМ окном плейсхолдера.

    Что здесь важно и чего раньше НЕ было (из-за этого браузер оставался
    отдельным окном со своими кнопками свернуть/закрыть):

    1. `GetParent` проверяется ПОСЛЕ `SetParent`. Chromium на `--app=` создаёт
       окно позже собственной инициализации; если `SetParent` пришёл слишком
       рано, он молча ничего не делает. Поэтому попытка повторяется, пока не
       выйдет время, — а не «один раз и как получится».
    2. Стиль ставится ДО `SetParent` и проверяется ПОСЛЕ: без `WS_CHILD`
       окно с `WS_POPUP` остаётся самостоятельным окном поверх родителя.
    3. Порядок «поставить на место → поднять шторку → показать → поднять
       шторку снова»: иначе браузер на мгновение виден поверх шторки.
    4. При любой ошибке — полный откат: полувстроенное окно хуже обычного.
    """
    if not user32 or not hwnd or not parent:
        return False
    style = get_window_long(hwnd, GWL_STYLE)
    exstyle = get_window_long(hwnd, GWL_EXSTYLE)
    try:
        set_window_long(hwnd, GWL_STYLE,
                        (style & ~(WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME |
                                   WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_DLGFRAME | WS_BORDER))
                        | WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)
        set_window_long(hwnd, GWL_EXSTYLE,
                        (exstyle & ~(WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE |
                                     WS_EX_CLIENTEDGE | WS_EX_STATICEDGE |
                                     WS_EX_APPWINDOW | WS_EX_NOACTIVATE))
                        | WS_EX_TOOLWINDOW)
        # `--app` окна Chromium иногда создаются уже ПОСЛЕ того, как мы их
        # нашли: SetParent по такому окну просто не срабатывает. Повторяем.
        deadline = time.monotonic() + max(0.5, timeout)
        attempt = 0
        while True:
            attempt += 1
            _clear_error()
            try:
                user32.SetParent(hwnd_of(hwnd), hwnd_of(parent))
            except Exception:
                Log.exc("win.embed_setparent")
            error = _last_error()
            if is_child_of(hwnd, parent):
                Log.info(f"окно браузера встроено: hwnd={hwnd} parent={parent} "
                         f"попыток={attempt}")
                break
            if time.monotonic() >= deadline:
                raise OSError(error or -1, "SetParent не прикрепил окно браузера")
            time.sleep(0.12)
        sync_window(hwnd, parent, inset)
        if cover:
            raise_window(cover)
        user32.ShowWindow(hwnd_of(hwnd), SW_SHOW)
        if cover:
            raise_window(cover)
        # После показа Chromium может вернуть себе часть стиля — проверяем.
        current = get_window_long(hwnd, GWL_STYLE)
        if not current & WS_CHILD:
            Log.warn("окно браузера потеряло WS_CHILD — ставлю стиль заново")
            set_window_long(hwnd, GWL_STYLE,
                            (current & ~(WS_POPUP | WS_CAPTION | WS_SYSMENU |
                                         WS_THICKFRAME | WS_MINIMIZEBOX |
                                         WS_MAXIMIZEBOX | WS_DLGFRAME | WS_BORDER))
                            | WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)
            sync_window(hwnd, parent, inset)
        return True
    except Exception:
        Log.exc("win.embed")
        detach_window(hwnd, style, exstyle)
        return False


def detach_window(hwnd: int, style: int = 0, exstyle: int = 0) -> None:
    """Отпустить окно браузера обратно в самостоятельные окна (откат)."""
    if not user32 or not hwnd:
        return
    try:
        user32.SetParent(hwnd_of(hwnd), None)
        if style:
            set_window_long(hwnd, GWL_STYLE, style)
        if exstyle:
            set_window_long(hwnd, GWL_EXSTYLE, exstyle)
        user32.ShowWindow(hwnd_of(hwnd), SW_SHOW)
    except Exception:
        Log.exc("win.detach")


def sync_window(hwnd: int, parent: int, inset: tuple[int, int, int, int],
                show: bool = True) -> bool:
    """Держать окно браузера ровно над плейсхолдером (в его клиентских px).

    Если окно перестало быть дочерним (Chromium пересоздал его после
    перезагрузки страницы), возвращается False — хост по этому признаку
    вживляет окно заново, а не «двигает» чужое окно по экрану.
    """
    if not user32 or not hwnd or not parent:
        return False
    if not is_child_of(hwnd, parent):
        return False
    width, height = client_size(parent)
    if width <= 0 or height <= 0:
        return False
    left, top, right, bottom = (int(v or 0) for v in (tuple(inset) + (0, 0, 0, 0))[:4])
    box_w = width - left - right
    box_h = height - top - bottom
    if box_w <= 0 or box_h <= 0:
        return False
    flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS
    if show:
        flags |= SWP_SHOWWINDOW
    try:
        return bool(user32.SetWindowPos(hwnd_of(hwnd), None, left, top,
                                        box_w, box_h, flags))
    except Exception:
        Log.exc("win.sync")
        return False


def hide_window(hwnd: int) -> None:
    if user32 and hwnd:
        try:
            user32.ShowWindow(hwnd_of(hwnd), SW_HIDE)
        except Exception:
            pass


WM_CLOSE = 0x0010
#: GWL-индексы и флаг «окно — самостоятельный корень». Нужны, чтобы отличить
#: настоящее лишнее окно Chromium от его же служебного окна.
GW_OWNER = 4
GA_ROOTOWNER = 3


def hide_stray_windows(pids: set[int], keep: int = 0) -> list[int]:
    """Скрыть лишние окна браузера; закрыть — только настоящие лишние.

    Chromium поднимает служебные окна уже после старта: без этого они висят в
    Alt+Tab, а иногда и поверх программы. Два правила, оба важные:

    * Встроенное окно (`keep`) не трогаем никогда.
    * Закрывать `WM_CLOSE` можно ТОЛЬКО окно класса `Chrome_WidgetWin_1`.
      Служебные окна (`Chrome_WidgetWin_0`) — это опорное окно браузера:
      его закрытие убивает весь процесс вместе со страницей.
    """
    if not user32:
        return []
    hidden: list[int] = []
    for window in _windows_of(set(pids) | process_tree(pids)):
        hwnd = int(window.get("hwnd") or 0)
        if not hwnd or hwnd == int(keep or 0) or not window.get("visible"):
            continue
        if keep and is_child_of(hwnd, keep):
            # Дочернее окно ВСТРОЕННОГО окна — это его же поверхность.
            continue
        # Настоящее лишнее окно: либо без родителя, либо самостоятельный
        # корень своего окна-владельца. Служебные окна Chromium вложены в
        # основное — их не трогаем вовсе (иначе Chromium умирает).
        if window.get("parent") and not window.get("self_root"):
            continue
        try:
            user32.ShowWindow(hwnd_of(hwnd), SW_HIDE)
            hide_from_taskbar(hwnd)
            hidden.append(hwnd)
            if window.get("class") == BROWSER_WINDOW_CLASS:
                user32.PostMessageW(hwnd_of(hwnd), WM_CLOSE, 0, 0)
        except Exception:
            Log.exc("win.hide_stray")
    if hidden:
        Log.info(f"скрыто лишних окон браузера: {len(hidden)}")
    return hidden


# ---------------------------------------------------------------------------
# ФОКУС КЛАВИАТУРЫ. Окно браузера — ДРУГОЙ процесс со своей очередью ввода:
# клик внутри него не передаёт ему клавиатуру. Без этого «при выделении чата
# ничего не пишется» — буквально: клавиши уходят в окно программы.
#
# Ограничения (сознательные, как в исходной версии): перехвата клавиш нет,
# текст не читается, ничего не печатается за пользователя. Читается только
# состояние ЛЕВОЙ КНОПКИ МЫШИ и хэндл окна под курсором; очереди ввода
# соединяются ровно на время одного `SetFocus`.
# ---------------------------------------------------------------------------

def belongs_to(root: int, hwnd: int) -> bool:
    """Окно `hwnd` — это `root` или его потомок."""
    if not user32 or not root or not hwnd:
        return False
    try:
        return handle_value(root) == handle_value(hwnd) or \
            bool(user32.IsChild(hwnd_of(root), hwnd_of(hwnd)))
    except Exception:
        return False


def thread_focus(tid: int) -> dict:
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if not user32.GetGUIThreadInfo(int(tid), ctypes.byref(info)):
            return {}
    except Exception:
        return {}
    return {"active": handle_value(info.hwndActive),
            "focus": handle_value(info.hwndFocus)}


def handoff_focus(host: int, browser: int, target: int) -> bool:
    """Отдать клавиатурный фокус окну внутри НАШЕГО встроенного браузера.

    Только если наше окно (или сам браузер) — активное окно системы, и только
    если цель действительно принадлежит нашему браузеру. Чужое окно не
    трогаем никогда.
    """
    if not user32 or not kernel32:
        return False
    try:
        foreground = handle_value(user32.GetForegroundWindow())
        if not (belongs_to(host, foreground) or belongs_to(browser, foreground)):
            return False
        if not belongs_to(browser, target):
            return False
        current_tid = int(kernel32.GetCurrentThreadId())
        target_tid = int(user32.GetWindowThreadProcessId(hwnd_of(target), None) or 0)
        if not target_tid:
            return False
        if thread_focus(target_tid).get("focus") == handle_value(target):
            return True
        attached = False
        try:
            if target_tid != current_tid:
                _clear_error()
                attached = bool(user32.AttachThreadInput(current_tid, target_tid, True))
                if not attached:
                    return False
            user32.SetFocus(hwnd_of(target))
            return belongs_to(browser, thread_focus(target_tid).get("focus", 0))
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(current_tid, target_tid, False)
                except Exception:
                    pass
    except Exception:
        Log.exc("win.handoff_focus")
        return False


class FocusBridge:
    """Отдаёт фокус в чат по клику мышью внутри встроенного браузера."""

    def __init__(self) -> None:
        self.host = 0
        self.browser = 0

    def poll(self, host: int, browser: int, enabled: bool) -> None:
        if not user32 or not browser or not enabled:
            return
        self.host, self.browser = int(host or 0), int(browser or 0)
        try:
            down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
            if not down:
                return
            point = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return
            target = handle_value(user32.WindowFromPoint(point))
            if not target or not belongs_to(self.browser, target):
                return
            handoff_focus(self.host, self.browser, target)
        except Exception:
            Log.exc("win.focus_poll")


# =============================================================================
# 12. ЗУМ: раскладка страницы ровно по окну (измерением, не на веру)
# =============================================================================

def target_css(logical_w: int, logical_h: int, physical_w: int, physical_h: int,
               zoom: float = PAGE_ZOOM) -> tuple[int, int, float]:
    """(css_w, css_h, dsf) — раскладка, которая ТОЧНО заполняет окно браузера.

    * ширина = логический размер (DIP) / зум → 453 / 0.667 ≈ 680 CSS px,
      одинаково при масштабе Windows 100 %, 125 % и 150 %;
    * dsf = физическая ширина / css-ширина → поверхность ровно как окно;
    * высота = физическая высота / dsf → поле ввода не срезано снизу.
    """
    css_w = max(320, int(round(float(logical_w) / float(zoom or PAGE_ZOOM))))
    dsf = (float(physical_w) / css_w) if css_w else 1.0
    css_h = max(320, int(round(float(physical_h) / dsf)))
    return css_w, css_h, dsf


def page_metrics(page: CDP) -> dict:
    probe = ("JSON.stringify({w: innerWidth, h: innerHeight, dpr: devicePixelRatio,"
             " sw: document.documentElement ? document.documentElement.scrollWidth : 0})")
    try:
        raw = page.value(probe, timeout=3)
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {"innerWidth": int(data.get("w") or 0), "innerHeight": int(data.get("h") or 0),
            "dpr": float(data.get("dpr") or 0.0), "scrollWidth": int(data.get("sw") or 0)}


def zoom_ok(page: CDP, logical_w: int, logical_h: int, physical_w: int, physical_h: int,
            tol_w: int = 28, tol_h: int = 48) -> tuple[bool, dict]:
    """Проверка «зум действительно 67 % и раскладка действительно вписалась».

    Одной ширины недостаточно: v8 объявлял «ок», хотя страница была шире окна и
    текст уезжал за правый край. Поэтому проверяются и ширина, и высота, и
    произведение `innerWidth * dpr` против физической ширины окна.
    """
    css_w, css_h, _dsf = target_css(logical_w, logical_h, physical_w, physical_h)
    metrics = page_metrics(page)
    if not metrics or metrics["innerWidth"] <= 0:
        return False, metrics
    if abs(metrics["innerWidth"] - css_w) > tol_w:
        return False, metrics
    if abs(metrics["innerHeight"] - css_h) > tol_h:
        return False, metrics
    if physical_w:
        rendered = metrics["innerWidth"] * metrics["dpr"]
        if abs(rendered - physical_w) > max(6.0, 0.03 * physical_w):
            return False, metrics
    return True, metrics


def apply_viewport_raw(page: CDP, width: int, height: int, dsf: float,
                       fit_window: bool = False) -> bool:
    try:
        page.send("Emulation.setDeviceMetricsOverride", {
            "width": int(width), "height": int(height),
            "deviceScaleFactor": float(dsf), "mobile": False,
            "fitWindow": bool(fit_window)}, timeout=5)
        return True
    except Exception:
        Log.exc("zoom.viewport")
        return False


def apply_zoom(page: CDP, logical_w: int, logical_h: int, physical_w: int,
               physical_h: int, settle: float = 0.25) -> dict:
    """Гарантировать раскладку 67 % — замером, а не предположением.

    1. Если зум профиля уже работает — не трогаем ничего (`source=native`).
    2. Иначе объявляем «форму окна»: логические px при `dsf=1.0` — тогда
       множители профиля и CDP не складываются (именно складывание давало
       `dpr=0.445` и «съехавший» текст на несколько секунд).
    """
    ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h)
    if ok:
        return {"ok": True, "source": "native", "metrics": metrics}
    css_w, css_h, dsf_base = target_css(logical_w, logical_h, physical_w, physical_h)
    attempts = (
        ("window-dsf1", max(320, logical_w), max(320, logical_h), 1.0, False),
        ("emulation-dsf", css_w, css_h, dsf_base, False),
        ("emulation-fit", css_w, css_h, dsf_base, True),
    )
    for name, width, height, dsf, fit in attempts:
        if not apply_viewport_raw(page, width, height, dsf, fit):
            continue
        time.sleep(settle)
        ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h)
        if ok:
            Log.info(f"зум выставлен: {name} css={width}x{height}")
            return {"ok": True, "source": name, "metrics": metrics}
    Log.warn(f"зум не подтверждён, метрики={metrics}")
    return {"ok": False, "source": "failed", "metrics": metrics}


def settle_layout(page: CDP, logical_w: int, logical_h: int, physical_w: int,
                  physical_h: int, budget_ms: int = 1200, step_ms: int = 120,
                  need: int = 2) -> dict:
    """Держать раскладку ЗАМЕРОМ, пока окно возмущено (переезд/показ).

    Chrome переприменяет зум уже ПОСЛЕ показа окна. Одна проверка такую
    поломку не видит по определению — её в этот момент ещё нет.
    """
    started = time.monotonic()
    deadline = started + max(0.0, budget_ms / 1000.0)
    good = 0
    fixed = 0
    ok = False
    metrics: dict = {}
    while True:
        ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h)
        if ok:
            good += 1
            if good >= max(1, need):
                break
        else:
            good = 0
            fixed += 1
            apply_viewport_raw(page, max(320, logical_w), max(320, logical_h), 1.0)
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.02, step_ms / 1000.0))
    return {"ok": bool(ok and good >= max(1, need)), "fixed": fixed,
            "ms": int((time.monotonic() - started) * 1000),
            "innerWidth": int(metrics.get("innerWidth") or 0),
            "dpr": round(float(metrics.get("dpr") or 0.0), 3)}

# =============================================================================
# 13. Qt: импорт ДО классов ниже (QThread/pyqtSignal нужны на этапе объявления)
# =============================================================================

from PyQt6.QtCore import (QEvent, QLockFile, QObject, QPoint, QRectF, QThread,
                          QTimer, Qt, pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QBrush
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog,
                             QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QMainWindow, QMessageBox, QPushButton, QVBoxLayout,
                             QWidget)

# =============================================================================
# 14. СОСТОЯНИЕ ЧАТА И ПРАВИЛО ВЛОЖЕНИЯ — здесь исправлен дефект «дублей»
# =============================================================================
#
#  В серверной версии (`UploadThread.run`) решение принималось по ОДНОМУ замеру
#  сразу после впрыска, и критериев было два разных (`names_ok` / `not names_ok`).
#  Чип файла появляется в чате через 0.5–3 с, поэтому поток успевал впрыснуть
#  тот же файл второй раз, а «дедупликация» срабатывала только при `present > 4`
#  и один раз за жизнь потока. Отсюда `Memphis.pdf Шаблон.txt Memphis.pdf
#  Шаблон.txt` — и чем медленнее машина, тем чаще.
#
#  Здесь правило ОДНО и проверяемое: `plan_attachments()` ниже. Оно не трогает
#  страницу само — только отвечает на вопрос «что делать в этом круге»: ждать,
#  впрыснуть конкретный файл, удалить один лишний чип или ничего не делать.
# -----------------------------------------------------------------------------

_NAME_CLEAN = re.compile(r"[\s\u00a0]+")


def norm_name(value: str) -> str:
    """Имя файла к сравнимому виду: без пути, регистронезависимо, без «(1)»."""
    text = str(value or "").strip().replace("\\", "/").split("/")[-1]
    text = _NAME_CLEAN.sub(" ", text).strip().lower()
    dot = text.rfind(".")
    stem, ext = (text[:dot], text[dot:]) if dot > 0 else (text, "")
    # Chrome/браузер дописывает «(1)», «(2)» перед расширением: «Memphis (1).pdf».
    stem = re.sub(r"\s*\(\d+\)$", "", stem).strip()
    return f"{stem}{ext}"


def stem_of(value: str) -> str:
    text = norm_name(value)
    dot = text.rfind(".")
    return text[:dot] if dot > 0 else text


def name_match(wanted: str, have: str) -> str:
    """'exact' | 'stem' | 'partial' | '' — насколько чип соответствует файлу.

    Сравнение НЕ «подстрока в любую сторону» (как было в `_chat_has_file`):
    иначе чип `Memphis.pdf` «удовлетворял» любой промт, чьё имя является его
    подстрокой, и чат считался готовым при `Memphis.pdf Memphis.pdf`.
    """
    want, got = norm_name(wanted), norm_name(have)
    if not want or not got:
        return ""
    if want == got:
        return "exact"
    if stem_of(want) and stem_of(want) == stem_of(got):
        return "stem"
    if len(want) >= 6 and (want in got or got in want):
        return "partial"
    return ""


def plan_attachments(expected: list[str], state: dict, attempts: dict[str, int],
                     empty_rounds: int, injected_since_check: bool = False,
                     now: float | None = None,
                     hold_until: float = 0.0) -> dict:
    """Что делать в этом круге. Чистая функция — без страницы и без таймеров.

    Возвращает `{action, targets, reason, present, names, matched}`:
      * `sealed`  — оба файла в чате, впрыскивать НЕЛЬЗЯ (это и есть «запор»);
      * `wait`    — сейчас ничего не трогаем (идёт загрузка / ждём подтверждения);
      * `inject`  — впрыснуть `targets` (только недостающие файлы);
      * `prune`   — удалить ОДИН лишний чип (дубликат или чужой файл);
      * `blocked` — на этой странице вложение не работает, сообщить человеку.
    """
    now = time.monotonic() if now is None else now
    expected = [str(item) for item in (expected or []) if item]
    names = [str(n or "") for n in (state.get("names") or [])]
    present = int(state.get("present") or 0)
    uploading = bool(state.get("uploading"))
    wanted = len(expected)

    # --- 1. Сопоставление «файл ↔ чип»: сначала точные имена, потом остальное.
    used: set[int] = set()
    matched: dict[str, int] = {}
    for level in ("exact", "stem", "partial"):
        for path in expected:
            if path in matched:
                continue
            for index, chip in enumerate(names):
                if index in used or not chip:
                    continue
                if name_match(path, chip) == level:
                    matched[path] = index
                    used.add(index)
                    break
    unmatched_named = [i for i, chip in enumerate(names) if chip and i not in used]
    unnamed = max(0, present - len([chip for chip in names if chip]))

    info = {"present": present, "names": names, "matched": sorted(matched),
            "unnamed": unnamed, "missing": []}

    # --- 2. Запор: ровно столько чипов, сколько файлов, и все опознаны.
    if present == wanted and len(matched) == wanted:
        return dict(info, action="sealed", targets=[], reason="both-files-present")

    # --- 3. Чипов ровно столько, сколько нужно, и каждый опознан или безымянный.
    #        Безымянный чип — «возможно, уже этот файл»: так он НЕ вызывает
    #        повторный впрыск. Серверная версия в этой ситуации уходила в ветку
    #        `not names_ok`, теряла привязку к файлам и впрыскивала всё заново —
    #        это второй источник дублей. Здесь безымянный чип считается
    #        «покрывающим» недостающий файл (иначе был бы вечный цикл ожидания).
    if present == wanted and len(matched) + unnamed >= wanted:
        reason = ("files-present-unnamed" if not matched
                  else "files-present-partly-unnamed")
        return dict(info, action="sealed", targets=[], reason=reason)

    # --- 4. Идёт загрузка: любой впрыск сейчас = гарантированный дубликат.
    if uploading:
        return dict(info, action="wait", targets=[], reason="uploading")

    # --- 5. Только что впрыснули: дать странице время создать чип.
    if injected_since_check and now < hold_until:
        return dict(info, action="wait", targets=[], reason="hold-after-inject")

    # --- 6. Лишние чипы: больше, чем файлов, либо есть чужой/дубликат.
    if present > wanted:
        return dict(info, action="prune", targets=[], reason="too-many-chips")
    if unmatched_named:
        return dict(info, action="prune", targets=[], reason="unexpected-file")

    # --- 7. Недостающие файлы. «Пусто» подтверждается двумя замерами подряд.
    missing = [path for path in expected if path not in matched]
    info["missing"] = missing
    if not missing:
        # Чипов меньше, чем нужно, но все опознаны: ждём остальные.
        return dict(info, action="wait", targets=[], reason="waiting-for-rest")

    # «Подтвердить пусто» нужно ТОЛЬКО когда в чате нет ни одного чипа. Если
    # один файл уже прикреплён, второй можно вкладывать сразу: он не может
    # «задвоиться», потому что вкладываются лишь неопознанные имена.
    # (Без этого условия второй файл не появлялся никогда: счётчик пустых
    #  замеров растёт только при present == 0.)
    if present == 0 and not injected_since_check and empty_rounds < EMPTY_CONFIRM:
        return dict(info, action="wait", targets=[], reason="confirm-empty")

    ready = [path for path in missing if int(attempts.get(path, 0)) < MAX_INJECTS_PER_FILE]
    if not ready:
        return dict(info, action="blocked", targets=[],
                    reason="no-file-appeared-after-injects")
    return dict(info, action="inject", targets=ready, reason="missing-files")


class ChatState:
    """Последний замер чата: сколько чипов, с какими именами."""

    def __init__(self) -> None:
        self.present = 0
        self.names: list[str] = []
        self.uploading = False
        self.composer = 0
        self.ok = False

    def update(self, payload: dict | None) -> None:
        if not isinstance(payload, dict):
            self.ok = False
            return
        try:
            self.present = int(payload.get("present") or 0)
        except Exception:
            self.present = 0
        names = payload.get("names") or []
        self.names = [str(name) for name in names] if isinstance(names, list) else []
        self.uploading = bool(payload.get("uploading"))
        self.composer = int(payload.get("composer") or 0)
        self.ok = True

    def as_dict(self) -> dict:
        return {"present": self.present, "names": list(self.names),
                "uploading": self.uploading, "composer": self.composer,
                "probe_ok": self.ok}


def read_chat_state(page: CDP) -> dict:
    """Один вызов страницы: и чипы, и имена, и поле ввода."""
    payload = page.value(JS_CHECK_FILES, timeout=4, default=None)
    return payload if isinstance(payload, dict) else {}


# =============================================================================
# 15. КОНТРОЛЛЕР СТРАНИЦЫ — один поток, один владелец DevTools
# =============================================================================

#: Сколько максимум ждать настоящей готовности чата (поля ввода и файлов).
SURFACE_TIMEOUT = 90.0
#: Период опроса готовности, с.
SURFACE_POLL = 0.5


class PageController(QThread):
    """Держит CDP-соединение и приводит чат к нужному виду.

    Один поток — один владелец websocket. Всё «тяжёлое» (замер состояния,
    впрыск, удаление лишнего) происходит здесь, а GUI только получает короткие
    сигналы. Никакого сбора HTML и подсчёта SHA-256 (этим серверная версия
    грузила процесс на каждом впрыске, отсюда и «подлагивания»).
    """

    #: Готовность: страница — настоящий чат, раскладка выставлена.
    ready = pyqtSignal()
    #: Строка состояния для GUI: (текст, цвет-роль).
    status = pyqtSignal(str, str)
    #: Вложение подтверждено / снято.
    attachments_changed = pyqtSignal(bool)
    #: На этой странице вложение не работает (вердикт).
    blocked = pyqtSignal(str)
    #: Фатальная ошибка страницы.
    failed = pyqtSignal(str)

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self.port = int(port)
        self.browser: CDP | None = None
        self.page: CDP | None = None
        self.chat = ChatState()
        #: Ожидаемые файлы: [(путь источника, байты PDF, имя PDF в чате)].
        self.expected: list[tuple[str, bytes, str]] = []
        self._expected_lock = threading.Lock()
        #: (логическая ширина, высота, физическая ширина, высота) — обычные int.
        self.sizes: tuple[int, int, int, int] = (W, H, W, H)
        self.lang = SPEECH_LANG
        self.light_theme = FORCE_LIGHT_THEME
        self._stop = False
        self._ready = False
        self._sealed = False
        self._injected_at = 0.0
        self._attempts: dict[str, int] = {}
        self._last_state = ChatState()
        self._empty_rounds = 0
        self._zero_since = 0.0
        self._blocked_reported = False
        self._last_signature = None
        self._last_status = ""
        self._prompt_install = 0.0
        # Кэш «файл уже лежит в странице»: base64 не пересылается повторно.
        self._stash_chunks: list[str] = []
        self._stash_name = ""
        self._replacing = False

    # ------------------------------------------------------------------ API
    def set_expected(self, items: list[tuple[str, bytes, str]]) -> None:
        """Задать файлы, которые обязаны быть в чате (вызывается из GUI)."""
        with self._expected_lock:
            self.expected = list(items or [])
            self._attempts = {}
            self._sealed = False
            self._injected_at = 0.0
            self._empty_rounds = 0
            self._blocked_reported = False
        Log.info("ожидаемые вложения: " + ", ".join(name for _p, _b, name in (items or [])))

    def replace(self, items: list) -> None:
        """Заменить файлы в чате: убрать старые чипы, затем вложить новые.

        Без этого смена шаблона ничего не меняла: имя файла то же
        («Шаблон.txt»), и правило вложения справедливо считало, что файл уже
        на месте. Здесь старые чипы сначала снимаются, и только потом
        начинается новый цикл.
        """
        # Флаг ставится СИНХРОННО: иначе главный цикл успевает сделать один
        # замер между вызовом и запуском потока и впрыснет файл в старые чипы.
        self._replacing = True

        def task():
            try:
                self._drop_all_chips()
                time.sleep(0.8)
            except Exception:
                Log.exc("controller.replace")
            finally:
                try:
                    self.set_expected(items)
                    self.arm()
                except Exception:
                    Log.exc("controller.replace.reset")
                finally:
                    # Флаг снимается в любом случае, иначе главный цикл
                    # перестал бы следить за чатом до перезапуска программы.
                    self._replacing = False

        threading.Thread(target=task, name="AttachReplace", daemon=True).start()

    def _drop_all_chips(self) -> int:
        """Снять все вложения из чата. Возвращает число запрошенных удалений."""
        page = self.page
        if page is None:
            return 0
        try:
            removed = page.value(JS_REMOVE_ALL_FILES, timeout=6, default=0)
            Log.info(f"снятие вложений: запрошено {removed}")
            return int(removed or 0)
        except Exception:
            Log.exc("controller.drop_chips")
            return 0

    def arm(self) -> None:
        """Новый цикл вложения: «пусто» ещё нужно подтвердить замерами.

        Без этого первый же замер после сброса (например, сразу после
        пересборки файлов) увидел бы «в чате ничего нет» и впрыснул бы файлы
        немедленно — ровно тот сценарий, из которого рождались дубли.
        """
        with self._expected_lock:
            self._attempts = {}
            self._sealed = False
            self._injected_at = 0.0
            self._empty_rounds = 0
            self._zero_since = 0.0
            self._blocked_reported = False
            self._stash_chunks = []
            self._stash_name = ""
            self._last_signature = None

    def expected_names(self) -> list[str]:
        with self._expected_lock:
            return [name for _p, _b, name in self.expected]

    def request_stop(self) -> None:
        self._stop = True

    def eval_async(self, expression: str, callback=None, timeout: float = 4) -> None:
        """Короткий вызов страницы из GUI-потока (микрофон, кнопки)."""
        page = self.page

        def task():
            value = None
            try:
                if page is not None:
                    value = page.value(expression, timeout=timeout)
            except Exception:
                Log.exc("page.eval_async")
            if callback is not None:
                try:
                    callback(value)
                except Exception:
                    Log.exc("page.eval_async.callback")

        threading.Thread(target=task, name="PageEval", daemon=True).start()

    # ------------------------------------------------------------- lifecycle
    def run(self) -> None:
        try:
            self.browser, self.page = attach(self.port, timeout=40)
            Log.info(f"DevTools подключён (порт {self.port})")
        except Exception as exc:
            Log.error(f"DevTools: {exc}")
            self.failed.emit("no-devtools")
            return
        try:
            try:
                grant_microphone(self.browser, self.page)
            except Exception:
                Log.exc("page.mic_permission")
            self._install_scripts()
            if not self._wait_ready():
                self.failed.emit("no-chat-surface")
                return
            self._apply_zoom()
            self._ready = True
            self.ready.emit()
            self._loop()
        except Exception as exc:
            Log.error(f"контроллер страницы: {exc}")
            self.failed.emit(str(exc))
        finally:
            for connection in (self.page, self.browser):
                if connection is not None:
                    connection.close()
            self.page = self.browser = None

    def _install_scripts(self) -> None:
        """Скрипты, которые должны работать при КАЖДОЙ загрузке документа."""
        sources = [JS_CONSENT, JS_PURGE]
        if self.light_theme:
            sources.append(JS_LIGHT_THEME)
        if self.lang:
            sources.append(speech_lang_js(self.lang))
        for source in sources:
            try:
                self.page.send("Page.addScriptToEvaluateOnNewDocument",
                               {"source": source}, timeout=5)
            except Exception:
                Log.exc("page.add_script")
        try:
            self.page.send("Page.enable", timeout=5)
        except Exception:
            pass
        self._prompt_install = time.monotonic()

    def _prime_page(self) -> None:
        """Разово применить тему, язык и убрать лишние элементы."""
        for source in (JS_CONSENT, JS_LIGHT_THEME, JS_PURGE):
            try:
                self.page.eval(source, timeout=4)
            except Exception:
                pass

    def _wait_ready(self) -> bool:
        """Ждать настоящий чат, а не «страница загрузилась»."""
        deadline = time.monotonic() + SURFACE_TIMEOUT
        while time.monotonic() < deadline and not self._stop:
            try:
                ready = bool(self.page.value(READY_SCRIPT, timeout=3, default=False))
            except Exception:
                ready = False
            if ready:
                self._prime_page()
                time.sleep(0.4)
                state = read_chat_state(self.page)
                if int(state.get("composer") or 0) > 0 or ready:
                    Log.info("страница готова: чат найден")
                    self._prime_page()
                    return True
            else:
                # Стена согласия/входа: один клик и сообщение человеку.
                try:
                    self.page.eval(JS_CONSENT, timeout=3)
                    self.page.eval(JS_PURGE, timeout=3)
                except Exception:
                    pass
            self.status.emit("Ожидание страницы чата…", "muted")
            time.sleep(SURFACE_POLL)
        Log.error("страница чата не появилась")
        return False

    def _apply_zoom(self) -> None:
        logical_w, logical_h, physical_w, physical_h = self.sizes
        result = apply_zoom(self.page, int(logical_w), int(logical_h),
                            int(physical_w), int(physical_h))
        if not result.get("ok"):
            self.status.emit("Раскладка страницы не подтверждена", "muted")

    def refresh_zoom(self) -> None:
        """Пересчитать раскладку (после показа окна, смены DPI и т.п.)."""
        if not self._ready or self.page is None:
            return
        threading.Thread(target=self._settle_worker, name="ZoomSettle", daemon=True).start()

    def _settle_worker(self) -> None:
        page = self.page
        if page is None:
            return
        logical_w, logical_h, physical_w, physical_h = self.sizes
        try:
            page.eval(JS_REVIVE, timeout=3)
        except Exception:
            pass
        result = settle_layout(page, int(logical_w), int(logical_h),
                               int(physical_w), int(physical_h))
        Log.info(f"раскладка после возмущения: ok={result.get('ok')} "
                 f"правок={result.get('fixed')} innerWidth={result.get('innerWidth')}")

    # ------------------------------------------------------------- main loop
    def _loop(self) -> None:
        while not self._stop:
            try:
                self._tick()
            except Exception:
                # Ошибка одного круга не имеет права убить поток: страница
                # может быть на навигации, CDP-соединение — переподключиться.
                Log.exc("controller.tick")
            time.sleep(SEALED_INTERVAL if self._sealed else POLL_INTERVAL)
            if time.monotonic() - self._prompt_install > 30 and not self._sealed:
                self._prompt_install = time.monotonic()
                for source in (JS_LIGHT_THEME, JS_PURGE):
                    try:
                        self.page.eval(source, timeout=3)
                    except Exception:
                        pass

    def _tick(self) -> None:
        page = self.page
        if page is None or self._replacing:
            return
        state = ChatState()
        state.update(read_chat_state(page))
        self.chat = state
        if not state.ok:
            return

        expected = self.expected_names()
        if not expected:
            # Файлы ещё не заданы (промт не выбран) — страницу не трогаем.
            self._sealed = False
            return
        payload = state.as_dict()
        injected_recently = (
            self._injected_at > 0 and
            time.monotonic() - self._injected_at < HOLD_AFTER_INJECT
        )
        hold_until = self._injected_at + HOLD_AFTER_INJECT if self._injected_at else 0.0

        if state.present == 0:
            if self._zero_since == 0.0:
                self._zero_since = time.monotonic()
            self._empty_rounds += 1
        else:
            self._zero_since = 0.0
            self._empty_rounds = 0

        plan = plan_attachments(expected, payload, self._attempts,
                               self._empty_rounds, injected_recently,
                               hold_until=hold_until)
        action = plan["action"]
        # Пишем в лог только ИЗМЕНЕНИЯ: иначе файл лога растёт на несколько
        # строк в секунду, и разбирать его становится невозможно.
        signature = (state.present, tuple(state.names[:6]), action, plan["reason"])
        if signature != self._last_signature:
            self._last_signature = signature
            Log.info(f"чат: present={state.present} names={state.names[:4]} "
                     f"→ {action} ({plan['reason']})")

        if action == "sealed":
            if not self._sealed:
                self._sealed = True
                self._injected_at = 0.0
                Log.info("вложение подтверждено — впрыски заблокированы")
                self.status.emit("Готов к работе", "ok")
                self.attachments_changed.emit(True)
            return

        if self._sealed:
            # Запор снят: в чате что-то изменилось (ответ ИИ, новый чат и т.п.).
            self._sealed = False
            self.attachments_changed.emit(False)
            Log.warn("вложения в чате больше нет — снимаю запор")

        if action == "wait":
            if plan["reason"] == "uploading":
                self.status.emit("Файл загружается…", "muted")
            return

        if action == "prune":
            self._prune_once()

        if action == "blocked":
            if not self._blocked_reported:
                self._blocked_reported = True
                Log.warn(f"вложение не работает: {plan['reason']} "
                         f"(попыток: {dict(self._attempts)})")
                self.blocked.emit(plan["reason"])
                self.status.emit("Не удалось вложить файлы — нажмите «⟳»", "danger")
            return

        if action == "inject":
            self._inject(plan["targets"])

        # Диагностика «файлов нет слишком долго» — один раз.
        if (state.present == 0 and not self._blocked_reported
                and self._zero_since and time.monotonic() - self._zero_since > BLOCK_AFTER_SECONDS):
            self._blocked_reported = True
            Log.warn("файлов нет дольше отведённого времени")
            self.blocked.emit("no-file-input")

    # ---------------------------------------------------------------- actions
    def _prune_once(self) -> None:
        """Удалить РОВНО ОДИН лишний чип и подождать результат.

        Именно по одному: серверная версия удаляла всё сразу и не проверяла,
        что получилось, поэтому «лишний» чип мог остаться, а нужный — уйти.
        """
        page = self.page
        if page is None:
            return
        before = read_chat_state(page)
        removed = page.value(JS_REMOVE_LAST, timeout=4, default=0)
        Log.info(f"удаление лишнего чипа: запрошено={removed} было={before.get('present')}")
        time.sleep(PRUNE_PAUSE)
        after = read_chat_state(page)
        if int(after.get("present") or 0) >= int(before.get("present") or 0):
            Log.warn("чип не удалился по кнопке — снимаю все вложения")
            try:
                page.eval(JS_REMOVE_ALL_FILES, timeout=4)
            except Exception:
                Log.exc("controller.remove_all")
            time.sleep(PRUNE_PAUSE)
        self._injected_at = 0.0
        self._attempts = {}

    def _stash(self, data: bytes, name: str) -> bool:
        """Положить файл в страницу один раз, кусками (без гигантских вызовов)."""
        page = self.page
        if page is None:
            return False
        mime = mime_for(Path(name))
        if self._stash_name == name:
            # Тот же файл уже лежит в странице: только повторная попытка
            # впрыска, без пересылки мегабайтов base64 (иначе GUI «подлагивает»).
            fresh = page.value(
                "(() => { const s = window.__llz; if (!s) return false;"
                " if (s.name !== " + json.dumps(js_name(name)) + ") return false;"
                " return !!(s.file || (s.pending && s.pending.file)); })()",
                timeout=4, default=False)
            if fresh:
                return True
        self._stash_chunks = stash_chunks(data)
        try:
            page.eval(STASH_INIT_JS % {"name": js_name(name)}, timeout=5)
            for chunk in self._stash_chunks:
                page.eval(STASH_PUSH_JS % {"chunk": chunk}, timeout=10)
            self._stash_name = name
            result = page.value(STASH_COMMIT_JS % {"name": js_name(name), "mime": mime},
                                timeout=15, default=None)
        except Exception:
            Log.exc("controller.stash")
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            Log.warn(f"файл не подготовлен в странице: {name} ({result})")
            return False
        return True

    def _inject(self, targets: list[str]) -> None:
        """Впрыснуть ТОЛЬКО недостающие файлы. Один файл = максимум один чип."""
        page = self.page
        if page is None:
            return
        with self._expected_lock:
            table = {name: (data, name) for _src, data, name in self.expected}
        for name in targets:
            item = table.get(name)
            if item is None:
                continue
            data, file_name = item
            self._attempts[name] = int(self._attempts.get(name, 0)) + 1
            ok = self._stash(data, file_name)
            if ok:
                mime = mime_for(Path(file_name))
                try:
                    result = page.value(
                        INJECT_FILE_JS % {"name": js_name(file_name), "mime": mime},
                        timeout=20, default=None)
                except Exception:
                    Log.exc("controller.inject")
                    result = None
                if not isinstance(result, dict) or not result.get("ok"):
                    ok = self._inject_via_dom(file_name)
                    mode = "dom"
                else:
                    mode = str(result.get("mode") or "input")
            else:
                ok = self._inject_via_dom(file_name)
                mode = "dom"
            Log.info(f"впрыск «{file_name}»: ok={ok} способ={mode} "
                     f"попытка={self._attempts[name]} байт={len(data)}")
            # ГЛАВНОЕ: запомнить момент впрыска. До следующего замера не
            # впрыскиваем ничего — странице нужно время создать чип.
            self._injected_at = time.monotonic()
            time.sleep(0.25)

    def _inject_via_dom(self, file_name: str) -> bool:
        """Последний резерв: отдать файл прямо в поле выбора (DOM.setFileInputFiles).

        Нужен потому, что часть сборок не принимает `DataTransfer` из JS, но
        штатно принимает файл, выбранный «руками». Требует файл на диске.
        """
        page = self.page
        if page is None:
            return False
        with self._expected_lock:
            entry = next(((src, data) for src, data, name in self.expected if name == file_name), None)
        if entry is None:
            return False
        source, data = entry
        try:
            path = Path(source)
            if not path.is_file() or path.read_bytes() != data:
                path = WORK_DIR / f"{js_name(file_name)}"
                path.write_bytes(data)
            document = page.send("DOM.getDocument", {"depth": 1}, timeout=5)
            root_id = (document.get("root") or {}).get("nodeId")
            query = page.send("DOM.querySelector",
                              {"nodeId": root_id, "selector": 'input[type="file"]'},
                              timeout=5)
            node_id = int(query.get("nodeId") or 0)
            if not node_id:
                return False
            page.send("DOM.setFileInputFiles",
                      {"files": [str(path)], "nodeId": node_id}, timeout=15)
            return True
        except Exception:
            Log.exc("controller.inject_dom")
            return False

    # -------------------------------------------------------------- микрофон
    def mic_start(self, callback=None) -> None:
        self.eval_async(JS_START_RECORDING, callback=callback, timeout=6)

    def mic_check(self, callback) -> None:
        self.eval_async(JS_CHECK_RECORDING, callback=callback, timeout=4)

    def mic_send(self, callback=None) -> None:
        self.eval_async(JS_STOP_AND_SEND, callback=callback, timeout=6)


# =============================================================================
# 16. ЗАПУСК БРАУЗЕРА (фоновый поток): процесс → порт → окно
# =============================================================================

class _Launcher(QThread):
    """Запускает браузер и ищет его окно. Всё в фоне: GUI не замирает."""

    window_found = pyqtSignal(int)      # hwnd
    devtools_found = pyqtSignal(int)    # порт
    failed = pyqtSignal(str)

    def __init__(self, candidates: list[Candidate], url: str, profile: Path,
                 lang: str, light_theme: bool, width: int, height: int,
                 staging: tuple[int, int], configured: str, parent=None):
        super().__init__(parent)
        self.candidates = list(candidates or [])
        self.url = url
        self.profile = Path(profile)
        self.lang = lang
        self.light_theme = light_theme
        self.width = int(width)
        self.height = int(height)
        self.staging = staging
        self.configured = configured
        self.process: BrowserProcess | None = None
        self.hwnd = 0
        self.devtools_ok = False
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        attempts = [(0, True), (1, True), (0, False), (2, True)]  # (номер, gpu)
        for attempt, gpu in attempts:
            if self._stop:
                return
            for candidate in self.candidates:
                if self._stop:
                    return
                try:
                    self._spawn(candidate, attempt, gpu)
                except Exception:
                    Log.exc("launcher.spawn")
                    continue
                if self.process is None:
                    continue
                self._supervise()
                if self._stop:
                    return
                if self.hwnd and self.devtools_ok:
                    return
                # Неудача: снять процесс и попробовать следующий вариант.
                process, self.process = self.process, None
                try:
                    process.close()
                except Exception:
                    pass
        self.failed.emit("no-browser")

    def _spawn(self, candidate: Candidate, attempt: int, gpu: bool) -> None:
        Log.info(f"пробую браузер: {candidate.kind} ({candidate.path})")
        process = BrowserProcess(candidate, self.url, self.profile, self.lang,
                                 self.light_theme, gpu=gpu, attempt=attempt)
        try:
            process.launch(self.width, self.height, self.staging)
        except Exception as exc:
            Log.error(f"браузер не запустился: {exc}")
            self.process = None
            return
        self.process = process

    #: Сколько ждать, пока ссылка `--app=` откроется как настоящая страница.
    #: Сразу после старта Chromium показывает служебное окно-«заглушку»
    #: (`Chrome_WidgetWin_0`, около 500x300) — если вживить ЕГО, программа
    #: получит пустое окно, а настоящая страница останется отдельным окном
    #: со своими кнопками свернуть/закрыть. Именно это и было видно.
    PAGE_TIMEOUT = 20.0

    def _wait_for_page(self, process: BrowserProcess) -> bool:
        """Дождаться, пока `--app=` станет настоящей страницей с целью DevTools."""
        deadline = time.monotonic() + self.PAGE_TIMEOUT
        while time.monotonic() < deadline and not self._stop:
            kind, _version, targets = process.probe()
            if kind == "exited":
                return False
            if kind == "ready" and choose_target(targets or []) is not None:
                return True
            time.sleep(0.2)
        Log.warn("страница чата не открылась — вживляю окно как есть")
        return True

    def _find_page_window(self, process: BrowserProcess, timeout: float = 12.0) -> int:
        """Настоящее окно страницы: класс `Chrome_WidgetWin_1`, размером с окно.

        `Chrome_WidgetWin_0` — служебное окно браузера: вживлять его нельзя,
        оно не содержит страницы.
        """
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline and not self._stop:
            found = _windows_of({process.pid()} | process_tree({process.pid()}))
            for window in found:
                if not window["visible"] or window["parent"]:
                    continue
                if window["class"] != BROWSER_WINDOW_CLASS:
                    continue
                left, top, right, bottom = window["rect"]
                if (right - left) >= 300 and (bottom - top) >= 300:
                    return int(window["hwnd"])
            time.sleep(0.2)
        return 0

    def _supervise(self) -> None:
        process = self.process
        if process is None:
            return
        deadline = time.monotonic() + 45
        window_sent = False
        devtools_sent = False
        page_seen = False
        while time.monotonic() < deadline and not self._stop:
            kind, _version, _targets = process.probe()
            if kind == "exited":
                Log.warn(f"браузер завершился сам (код {process.proc.returncode if process.proc else '?'})")
                return
            if kind == "ready" and not devtools_sent:
                devtools_sent = True
                self.devtools_ok = True
                self.devtools_found.emit(process.port)
            if kind == "ready" and not page_seen:
                # Страница уже есть — можно искать её окно.
                page_seen = self._wait_for_page(process)
            if not window_sent and page_seen:
                found = self._find_page_window(process)
                if not found:
                    # Запасной вариант: любое большое окно Chromium.
                    found = find_browser_window({process.pid()}, timeout=0.1)
                if found:
                    window_sent = True
                    self.hwnd = found
                    self.window_found.emit(found)
            if window_sent and devtools_sent:
                return
            time.sleep(0.25)
        if not window_sent:
            Log.warn("окно браузера не найдено")
        elif not devtools_sent:
            Log.warn("окно есть, но DevTools не отвечает — пробую другой вариант")

# =============================================================================
# 17. ХОСТ БРАУЗЕРА: сводит процесс, окно и Qt-плейсхолдер вместе
# =============================================================================

class BrowserHost(QObject):
    """Запускает браузер, вживляет его окно и держит его поверх плейсхолдера.

    Заменяет `NativeHost` серверной версии. Отличие принципиальное: там было
    шесть попыток с перебором флагов и профилей и внешний `NativeBrowser`; здесь
    то же самое, но без приборной панели — только то, что нужно для работы.
    """

    ready = pyqtSignal(int)          # порт DevTools
    failed = pyqtSignal(str)         # причина
    weak_speech = pyqtSignal(str)    # найден браузер со слабым речевым вводом

    def __init__(self, parent=None, url: str = CHAT_URL, profile: Path = BROWSER_PROFILE,
                 lang: str = SPEECH_LANG, light_theme: bool = FORCE_LIGHT_THEME,
                 width: int = W, height: int = H, configured: str = "",
                 inset_provider=None):
        super().__init__(parent)
        self.url = url
        self.profile = Path(profile)
        self.lang = lang
        self.light_theme = light_theme
        self.width = int(width)
        self.height = int(height)
        self.configured = configured or ""
        self.inset_provider = inset_provider or (lambda: (0, 0, 0, 0))
        self.placeholder = None
        self.cover = 0
        self.hwnd = 0
        self.port = 0
        self.inset = (0, 0, 0, 0)
        self.pids: set[int] = set()
        self.launcher: _Launcher | None = None
        self._announced = False
        self._stopping = False
        self._tick = 0
        self._embed_failures = 0
        self.focus = FocusBridge()
        # Один таймер на всё: держать окно на месте и убирать лишние окна.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_timer)

    # ------------------------------------------------------------------ start
    def start(self, placeholder: QWidget) -> bool:
        self.placeholder = placeholder
        try:
            placeholder.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            self.cover = handle_value(placeholder.window().winId())
        except Exception:
            self.cover = 0
        candidates = discover_browsers(ROOT_DIR, self.configured or None)
        if not candidates:
            Log.error("ни одного Chromium-браузера не найдено")
            self.failed.emit("no-browser")
            return False
        self.launcher = _Launcher(candidates, self.url, self.profile, self.lang,
                                  self.light_theme, self.width, self.height,
                                  (X + 80, Y + 80), self.configured, self)
        self.launcher.window_found.connect(self._on_window)
        self.launcher.devtools_found.connect(self._on_devtools)
        self.launcher.failed.connect(self._on_failed)
        self.launcher.start()
        self.timer.start(800)
        return True

    # -------------------------------------------------------------- callbacks
    def _on_window(self, hwnd: int) -> None:
        if self._stopping or not self.placeholder:
            return
        try:
            parent = handle_value(self.placeholder.winId())
        except Exception:
            Log.exc("host.placeholder_hwnd")
            return
        self.hwnd = int(hwnd)
        try:
            if self.launcher and self.launcher.process:
                self.pids = {self.launcher.process.pid()}
        except Exception:
            self.pids = set()
        self.inset = tuple(int(v) for v in self.inset_provider())
        if not embed_window(self.hwnd, parent, self.inset, self.cover):
            self.failed.emit("embed-failed")
            return
        self._embed_failures = 0
        hide_from_taskbar(self.hwnd)
        self._maybe_ready()

    def _on_devtools(self, port: int) -> None:
        self.port = int(port)
        self._maybe_ready()

    def _on_failed(self, reason: str) -> None:
        if self._stopping:
            return
        Log.error(f"запуск браузера не удался: {reason}")
        self.failed.emit(reason)

    def _maybe_ready(self) -> None:
        if not self._announced and self.hwnd and self.port:
            self._announced = True
            kind = ""
            try:
                if self.launcher and self.launcher.process:
                    kind = self.launcher.process.candidate.kind
            except Exception:
                kind = ""
            if kind in WEAK_SPEECH_KINDS:
                Log.warn(f"браузер «{kind}»: штатный микрофон страницы может не работать")
                self.weak_speech.emit(kind)
            Log.info(f"браузер готов: hwnd={self.hwnd} порт={self.port} вид={kind}")
            self.ready.emit(self.port)

    # ------------------------------------------------------------------ upkeep
    def _on_timer(self) -> None:
        """Держать окно на месте и присматривать за ним.

        Три задачи, и все три — из-за того, что браузер живёт отдельно:
        * пересчитать положение (окно переехало, изменился масштаб, размер);
        * заметить ОТРЫВ окна (Chromium пересоздал его) и вживить заново —
          иначе браузер снова становится отдельным окном со своими кнопками;
        * убрать из панели задач и закрыть лишние окна Chromium.
        """
        if self._stopping or not self.hwnd or not self.placeholder:
            return
        self._tick += 1
        try:
            parent = handle_value(self.placeholder.winId())
        except Exception:
            Log.exc("host.placeholder_hwnd")
            return
        self.inset = tuple(int(v) for v in self.inset_provider())
        if not is_child_of(self.hwnd, parent):
            self._embed_failures += 1
            Log.warn(f"окно браузера выпало из встраивания (попытка "
                     f"{self._embed_failures}) — вживляю заново")
            if not embed_window(self.hwnd, parent, self.inset, self.cover):
                if self._embed_failures >= 5:
                    self.failed.emit("embed-lost")
                return
        else:
            sync_window(self.hwnd, parent, self.inset)
            # Шторку поднимаем ПОВЕРХ браузера, пока страница не готова.
            if self.cover and not self._announced:
                raise_window(self.cover)
        if self._tick % 5 == 0:          # раз в ~4 секунды
            try:
                hide_from_taskbar(self.hwnd)
                self.refresh_pids()
                hide_stray_windows(self.pids, keep=self.hwnd)
            except Exception:
                Log.exc("host.strays")

    def poll_focus(self, host_hwnd: int, enabled: bool) -> None:
        """Отдать клавиатуру в чат по клику внутри окна браузера."""
        if self._stopping or not self.hwnd:
            return
        try:
            self.focus.poll(host_hwnd, self.hwnd, bool(enabled))
        except Exception:
            Log.exc("host.focus")

    def sync(self) -> None:
        """Сразу пересчитать сдвиг и поставить окно на место."""
        if not self.hwnd or not self.placeholder:
            return
        try:
            self.inset = tuple(int(v) for v in self.inset_provider())
            sync_window(self.hwnd, handle_value(self.placeholder.winId()), self.inset)
        except Exception:
            Log.exc("host.sync_now")

    def refresh_pids(self) -> None:
        if not self.pids:
            return
        try:
            self.pids |= process_tree(self.pids)
        except Exception:
            Log.exc("host.refresh_pids")

    def stop(self) -> None:
        """Погасить браузер ЦЕЛИКОМ (с деревом процессов) и отпустить окно."""
        self._stopping = True
        self.timer.stop()
        if self.launcher is not None:
            self.launcher.stop()
        if self.launcher is not None and self.launcher.process is not None:
            try:
                # close() снимает всё дерево процессов (см. kill_tree):
                # `terminate()` убил бы только родителя, а окна рендереров
                # остались бы висеть — из-за этого «плодятся браузеры».
                self.launcher.process.close()
            except Exception:
                Log.exc("host.stop")
        if self.hwnd:
            detach_window(self.hwnd)
            hide_window(self.hwnd)


# =============================================================================
# 18. ДИАЛОГИ
# =============================================================================

def icon_path() -> Path:
    for candidate in (ROOT_DIR / "icon.ico", SCRIPT_DIR / "icon.ico"):
        if candidate.is_file():
            return candidate
    return Path()


def apply_win32_icon(hwnd: int) -> None:
    """Иконка приложения на HWND (WM_SETICON) — Qt её ставит не всегда."""
    path = icon_path()
    if not path or not user32:
        return
    try:
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        hicon = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(hwnd_of(hwnd), 0x0080, 1, hicon)   # WM_SETICON ICON_BIG
            user32.SendMessageW(hwnd_of(hwnd), 0x0080, 0, hicon)   # WM_SETICON ICON_SMALL
    except Exception:
        Log.exc("ui.icon")


class DraggableDialog(QDialog):
    """Диалог, который можно тянуть мышью за любую свободную область."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        path = icon_path()
        if path:
            self.setWindowIcon(QIcon(str(path)))

    def showEvent(self, event):
        super().showEvent(event)
        try:
            apply_win32_icon(handle_value(self.winId()))
        except Exception:
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not isinstance(child, (QAbstractButton, QLineEdit, QComboBox)):
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class TemplateWindow(DraggableDialog):
    """«Шаблон» — шесть полей, которые уходят в чат файлом Шаблон.txt."""

    FIELDS = [
        ("Name", "Имя и фамилия", "Mike Macmillan"),
        ("ID", "ID", "85028"),
        ("Fraction", "Фракция", "SANG"),
        ("Rang", "Ранг", "11"),
        ("Department", "Отдел", "MP"),
        ("JobTitle", "Должность", "Заместитель командующего MP, сенатор NG"),
    ]

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Шаблон данных")
        self.setFixedSize(560, 480)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Dialog)
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(6)

        title = QLabel("Шаблон данных")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 19px; font-weight: 700; color: {THEME['accent']};")
        root.addWidget(title)

        hint = QLabel("Поля уходят в чат файлом «Шаблон.txt» рядом с промтом")
        hint.setObjectName("Caption")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)
        root.addSpacing(14)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.fields: dict[str, QLineEdit] = {}
        template = cfg.get("template") or {}
        for key, caption, placeholder in self.FIELDS:
            label = QLabel(caption)
            label.setMinimumWidth(130)
            label.setStyleSheet(f"color: {THEME['muted']}; font-size: 13px;")
            field = QLineEdit()
            field.setPlaceholderText(placeholder)
            field.setText(str(template.get(key, "") or ""))
            field.setMinimumHeight(40)
            field.setMinimumWidth(300)
            self.fields[key] = field
            form.addRow(label, field)
        root.addLayout(form)
        root.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("Ghost")
        cancel.setMinimumHeight(46)
        cancel.setMinimumWidth(180)
        cancel.clicked.connect(lambda _c=False: self.reject())
        buttons.addWidget(cancel)
        save = QPushButton("Сохранить")
        save.setObjectName("Primary")
        save.setMinimumHeight(46)
        save.setMinimumWidth(180)
        save.clicked.connect(lambda _c=False: self.accept())
        buttons.addWidget(save)
        buttons.addStretch()
        root.addLayout(buttons)

    def get_template(self) -> dict:
        return {key: field.text().strip() for key, field in self.fields.items()}


class KeyCaptureButton(QPushButton):
    """Кнопка «нажмите клавишу»: ловит нажатие и показывает его имя."""

    captured = pyqtSignal(str)

    def __init__(self, text: str = "[ Нажмите клавишу ]", parent=None):
        super().__init__(text, parent)
        self.key_name = ""
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(42)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda _c=False: self.setFocus())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt,
                           Qt.Key.Key_Meta):
            return
        name = resolve_key_event(event)
        if name:
            self.key_name = name
            self.setText(name)
            self.captured.emit(name)
            event.accept()
            return
        super().keyPressEvent(event)


class HotkeyDialog(DraggableDialog):
    """Смена одной горячей клавиши прямым нажатием на клавиатуре."""

    def __init__(self, current_key: str, title: str = "Горячая клавиша",
                 taken: set | None = None, parent=None):
        super().__init__(parent)
        self.selected = current_key
        self.taken = {key for key in (taken or set()) if key}
        self.setWindowTitle(title)
        self.setFixedSize(380, 240)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Dialog)
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        caption = QLabel(title)
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {THEME['accent']};")
        root.addWidget(caption)

        self.warning = QLabel("")
        self.warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning.setStyleSheet(f"color: {THEME['danger']}; font-size: 11px;")
        root.addWidget(self.warning)

        self.capture = KeyCaptureButton(current_key or "[ Нажмите клавишу ]")
        self.capture.captured.connect(self._on_captured)
        root.addWidget(self.capture)
        root.addStretch()

        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(lambda _c=False: self.reject())
        buttons.addWidget(cancel)
        save = QPushButton("Сохранить")
        save.setObjectName("Primary")
        save.clicked.connect(lambda _c=False: self._on_save())
        buttons.addWidget(save)
        root.addLayout(buttons)

    def _on_captured(self, name: str) -> None:
        if name in self.taken:
            self.warning.setText("Эта клавиша уже занята другой функцией")
            return
        self.warning.setText("")
        self.selected = name

    def _on_save(self) -> None:
        if not self.selected:
            self.warning.setText("Сначала нажмите клавишу")
            return
        if self.selected in self.taken:
            self.warning.setText("Эта клавиша уже занята другой функцией")
            return
        self.accept()


class OnboardingHotkeyWindow(DraggableDialog):
    """Первичная настройка двух клавиш: «Микрофон» и «Окно»."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Горячие клавиши")
        self.setFixedSize(560, 480)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Dialog)
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(10)

        title = QLabel("Горячие клавиши")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 19px; font-weight: 700; color: {THEME['accent']};")
        root.addWidget(title)

        hint = QLabel("Нажмите на поле и нажмите нужную клавишу на клавиатуре")
        hint.setObjectName("Caption")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)
        root.addSpacing(16)

        self.mic_button = KeyCaptureButton(cfg.get("hotkey_mic") or "[ Нажмите клавишу ]")
        self.toggle_button = KeyCaptureButton(cfg.get("hotkey_toggle") or "[ Нажмите клавишу ]")
        self.mic_button.captured.connect(self._clear_warning)
        self.toggle_button.captured.connect(self._clear_warning)

        for caption, description, button in (
            ("Микрофон", "запись голоса и отправка вопроса в нейросеть", self.mic_button),
            ("Окно", "мгновенно свернуть или развернуть Legalyze", self.toggle_button),
        ):
            card = QFrame()
            card.setObjectName("Card")
            card.setStyleSheet(
                f"QFrame#Card {{ background: {THEME['bg2']}; border: 1px solid {THEME['line']};"
                f" border-radius: 10px; }}")
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 12, 14, 12)
            texts = QVBoxLayout()
            texts.setSpacing(2)
            name = QLabel(caption)
            name.setStyleSheet(f"color: {THEME['text']}; font-size: 14px; font-weight: 700;")
            texts.addWidget(name)
            desc = QLabel(description)
            desc.setObjectName("Caption")
            desc.setWordWrap(True)
            texts.addWidget(desc)
            row.addLayout(texts)
            row.addStretch()
            button.setMinimumWidth(190)
            row.addWidget(button)
            root.addWidget(card)

        self.warning = QLabel("")
        self.warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning.setStyleSheet(f"color: {THEME['danger']}; font-size: 11px;")
        root.addWidget(self.warning)
        root.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("Ghost")
        cancel.setMinimumHeight(46)
        cancel.setMinimumWidth(180)
        cancel.clicked.connect(lambda _c=False: self.reject())
        buttons.addWidget(cancel)
        save = QPushButton("Сохранить")
        save.setObjectName("Primary")
        save.setMinimumHeight(46)
        save.setMinimumWidth(180)
        save.clicked.connect(lambda _c=False: self._on_save())
        buttons.addWidget(save)
        buttons.addStretch()
        root.addLayout(buttons)

    def _clear_warning(self, _name: str = "") -> None:
        self.warning.setText("")

    def _on_save(self) -> None:
        mic = self.mic_button.key_name
        toggle = self.toggle_button.key_name
        if not mic or not toggle:
            self.warning.setText("Назначьте обе клавиши")
            return
        if mic == toggle:
            self.warning.setText("Клавиши должны отличаться")
            return
        self.accept()

    def get_toggle_key(self) -> str:
        return self.toggle_button.key_name

    def get_mic_key(self) -> str:
        return self.mic_button.key_name


class HotkeyChip(QWidget):
    """Плашка в шапке окна: подпись + назначенная клавиша."""

    clicked = pyqtSignal()

    def __init__(self, caption: str, key: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.caption = QLabel(caption)
        self.caption.setStyleSheet(f"color: {THEME['muted']}; font-size: 10px; border: none;")
        layout.addWidget(self.caption)

        self.button = QPushButton(key or "—")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setToolTip("Нажмите, чтобы сменить клавишу")
        self.button.setStyleSheet(
            f"QPushButton {{ background: rgba(255,255,255,0.05); color: {THEME['text']};"
            f" border: 1px solid {THEME['line']}; border-radius: 6px; padding: 4px 8px;"
            f" font-size: 11px; font-weight: 700; }}"
            f"QPushButton:hover {{ border-color: {THEME['accent']}; }}")
        self.button.clicked.connect(lambda _c=False: self.clicked.emit())
        layout.addWidget(self.button)

    def set_key(self, key: str) -> None:
        self.button.setText(key or "—")

    def set_active(self, active: bool) -> None:
        color = THEME["ok"] if active else THEME["line"]
        text_color = "#04120c" if active else THEME["text"]
        self.button.setStyleSheet(
            f"QPushButton {{ background: {color}; color: {text_color};"
            f" border: 1px solid {color}; border-radius: 6px; padding: 4px 8px;"
            f" font-size: 11px; font-weight: 700; }}")


class PromptSelectionWindow(DraggableDialog):
    """Выбор промта из ЛОКАЛЬНОЙ папки `prompts/`."""

    def __init__(self, items: list[str], display_names: list[str],
                 current_index: int, folder: Path, parent=None):
        super().__init__(parent)
        self.items = list(items)
        self.choice: str | None = None
        self.folder = Path(folder)

        self.setWindowTitle("Выбор промта")
        self.setFixedSize(340, 210)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Dialog)
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Выберите рабочий промт")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {THEME['accent']};")
        layout.addWidget(title)

        self.combo = QComboBox()
        self.combo.addItems(display_names)
        if 0 <= current_index < len(display_names):
            self.combo.setCurrentIndex(current_index)
        self.combo.setMinimumHeight(38)
        layout.addWidget(self.combo)

        hint = QLabel(f"Файлы берутся из папки: {self.folder}")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()

        buttons = QHBoxLayout()
        open_folder = QPushButton("Папка")
        open_folder.setObjectName("Ghost")
        open_folder.setToolTip("Открыть папку с промтами")
        open_folder.clicked.connect(lambda _c=False: self._open_folder())
        buttons.addWidget(open_folder)

        cancel = QPushButton("Отмена")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(lambda _c=False: self.reject())
        buttons.addWidget(cancel)

        ok = QPushButton("Выбрать")
        ok.setObjectName("Primary")
        ok.clicked.connect(lambda _c=False: self._on_accept())
        buttons.addWidget(ok)
        layout.addLayout(buttons)

        parent_widget = self.parentWidget()
        if parent_widget:
            geometry = parent_widget.geometry()
            self.move(geometry.x() - self.width() - 8, geometry.y())

    def _open_folder(self) -> None:
        try:
            if IS_WINDOWS:
                os.startfile(str(self.folder))       # noqa: S606 - это GUI-действие
            else:
                subprocess.Popen(["xdg-open", str(self.folder)])
        except Exception:
            Log.exc("ui.open_prompts_folder")

    def _on_accept(self) -> None:
        index = self.combo.currentIndex()
        if 0 <= index < len(self.items):
            self.choice = self.items[index]
        self.accept()


class LoadingOverlay(QWidget):
    """Шторка поверх окна браузера: спиннер и сообщение.

    Пока браузер не готов и раскладка не устоялась, пользователь не видит
    «сырую» страницу — только аккуратную карточку ожидания.
    """

    def __init__(self, owner: QWidget):
        super().__init__(None)
        self._owner = owner
        self._angle = 0.0
        self._pulse = 0.0
        self._title = "Подготовка"
        self._message = "Пожалуйста, подождите…"
        self._error = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.BusyCursor)

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.setInterval(16)

    # ------------------------------------------------------------------- API
    def show_state(self, title: str, message: str, error: bool = False) -> None:
        if (self.isVisible() and self._title == title
                and self._message == message and self._error == error):
            if not self._anim.isActive():
                self._anim.start()
            return
        self._title, self._message, self._error = title, message, error
        self.sync_geometry()
        if not self.isVisible():
            self.show()
        if not self._anim.isActive():
            self._anim.start()
        force_topmost(handle_value(self.winId()))
        self.update()

    def set_message(self, message: str) -> None:
        self._message = message
        self.update()

    def finish(self) -> None:
        self._anim.stop()
        self.hide()

    def sync_geometry(self) -> None:
        """Поставить шторку ровно над областью чата внутри окна программы.

        Шторка — отдельное окно верхнего уровня (у неё нет родителя: иначе она
        не поднимется над встроенным браузером). Значит её положение НЕ
        меняется само — его надо пересчитывать каждый раз, когда тронулось
        главное окно. Именно поэтому шторка раньше «оставалась слева».
        """
        try:
            owner = self._owner
            if not owner:
                return
            geometry = owner.frameGeometry()
            top = int(getattr(owner, "top_inset", 54))
            bottom = int(getattr(owner, "bottom_inset", 30))
            # Запас по рамке: у окна без рамки он нулевой, но на другой
            # версии Windows рамка может появиться после смены стиля.
            left = geometry.x()
            upper = geometry.y() + top
            width = max(120, geometry.width())
            height = max(120, geometry.height() - top - bottom)
            current = self.geometry()
            if (current.x(), current.y(), current.width(), current.height()) != \
                    (left, upper, width, height):
                self.setGeometry(left, upper, width, height)
        except Exception:
            Log.exc("ui.overlay_geometry")

    # ------------------------------------------------------------------ paint
    def _tick(self) -> None:
        self._angle = (self._angle + 4.2) % 360.0
        self._pulse = (self._pulse + 0.035) % (2 * math.pi)
        self.update()

    def mousePressEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        painter.fillRect(rect, QColor(9, 11, 24, 238))

        cx, cy = rect.width() / 2, rect.height() / 2
        radius = 90 + 12 * math.sin(self._pulse)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(124, 131, 255, 26)))
        painter.drawEllipse(QRectF(cx - radius, cy - radius - 40, radius * 2, radius * 2))

        card_w = min(rect.width() - 40, 340)
        card_h = 210
        card = QRectF(cx - card_w / 2, cy - card_h / 2, card_w, card_h)
        path = QPainterPath()
        path.addRoundedRect(card, 16, 16)
        painter.setBrush(QBrush(QColor(21, 24, 48, 245)))
        painter.setPen(QPen(QColor(38, 42, 74), 1))
        painter.drawPath(path)

        spin_r = 26.0
        spin_rect = QRectF(cx - spin_r, card.top() + 34, spin_r * 2, spin_r * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(42, 47, 84), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(spin_rect, 0, 360 * 16)

        accent = QColor(239, 68, 68) if self._error else QColor(124, 131, 255)
        painter.setPen(QPen(accent, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span = int((110 + 40 * math.sin(self._pulse)) * 16)
        painter.drawArc(spin_rect, int(-self._angle * 16), span)

        painter.setPen(QColor(230, 232, 245))
        painter.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        painter.drawText(QRectF(card.left() + 16, card.top() + 100, card.width() - 32, 26),
                         int(Qt.AlignmentFlag.AlignCenter), self._title)

        painter.setPen(QColor(143, 150, 191))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(QRectF(card.left() + 18, card.top() + 128, card.width() - 36, 52),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop |
                             Qt.TextFlag.TextWordWrap), self._message)

        bar_w = card.width() - 44
        bar = QRectF(card.left() + 22, card.bottom() - 26, bar_w, 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(38, 42, 74))
        painter.drawRoundedRect(bar, 2, 2)
        seg_w = bar_w * 0.32
        pos = (self._angle / 360.0) * (bar_w + seg_w) - seg_w
        seg = QRectF(bar.left() + max(0.0, pos), bar.top(),
                     min(seg_w, bar_w - max(0.0, pos)), bar.height())
        painter.setBrush(accent)
        painter.drawRoundedRect(seg, 2, 2)
        painter.end()

# =============================================================================
# 19. ПОДГОТОВКА ВЛОЖЕНИЙ (локально: файл → байты PDF, без сервера)
# =============================================================================

TEMPLATE_EXPORT_NAME = "Шаблон.txt"


class AttachmentBuilder(QThread):
    """Собирает ровно два вложения: PDF промта и Шаблон.txt.

    Заменяет `PromptLoaderWorker` серверной версии. Там были: запрос HEAD за
    контрольной суммой, скачивание `.enc`, расшифровка, кэш и манифест. Здесь
    всё локально и без сети — осталась только сборка PDF из файла.
    """

    done = pyqtSignal(bool, object)     # (успех, {"items": [...], "pdf_name": ...} | текст ошибки)

    def __init__(self, store: PromptStore, cfg: dict, prompt_name: str, parent=None):
        super().__init__(parent)
        self.store = store
        self.cfg = dict(cfg or {})
        self.prompt_name = prompt_name

    def run(self) -> None:
        try:
            if not self.prompt_name:
                self.done.emit(False, "Промт не выбран — нажмите «Промт» и выберите файл")
                return
            source = self.store.folder / self.prompt_name
            pdf_bytes, pdf_name = self.store.build_pdf(self.prompt_name)
            if not pdf_bytes[:4] == b"%PDF":
                self.done.emit(False, "Не удалось собрать PDF из промта")
                return
            # Шаблон.txt пишем прямо перед вложением: он всегда актуален.
            template_path = write_template_file(self.cfg.get("template") or {})
            template_bytes = template_path.read_bytes()
            items = [
                (str(source), pdf_bytes, pdf_name),
                (str(template_path), template_bytes, TEMPLATE_EXPORT_NAME),
            ]
            self.done.emit(True, {"items": items, "pdf_name": pdf_name,
                                  "pdf_bytes": len(pdf_bytes)})
        except Exception as exc:
            Log.error(f"подготовка вложений: {exc}")
            self.done.emit(False, str(exc))


# =============================================================================
# 20. ГЛАВНОЕ ОКНО
# =============================================================================

class MainWindow(QMainWindow):
    """Окно программы: панели + встроенный чат. Только то, что нужно для работы.

    По сравнению с серверной версией убраны: строка «Запросы: N», кнопки
    обновления доступа, обработчики баланса и списаний, синхронизация шаблона
    с сервером и вся ветка авторизации. Логика страницы, вложения и микрофона
    сохранена.
    """

    TOP_INSET = 54
    BOTTOM_INSET = 30

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.store = PromptStore(PROMPTS_DIR)
        self.store.ensure_example()

        self.top_inset = self.TOP_INSET
        self.bottom_inset = self.BOTTOM_INSET

        self.host: BrowserHost | None = None
        self.controller: PageController | None = None
        self.builder: AttachmentBuilder | None = None

        self.speech_lang = str(cfg.get("speech_lang") or SPEECH_LANG)
        self.light_theme = bool(cfg.get("light_theme", FORCE_LIGHT_THEME))

        self.mic_active = False
        self._mic_got_text = False
        self._mic_busy = False
        self._mic_last_ts = 0.0
        self._mic_retried = False

        self._closing = False
        self._cleaned = False
        self._chat_ready = False
        self._attached = False
        self._prompt_picker_shown = False
        self._crash_recoveries = 0
        self._drag_pos: QPoint | None = None

        self.hotkeys = HotkeyMonitor(self._on_hotkey)
        self._hotkey_timer = QTimer(self)
        self._hotkey_timer.setInterval(25)
        self._hotkey_timer.timeout.connect(self._hotkey_tick)
        self._hotkey_timer.start()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Window)
        self.setWindowTitle(APP_NAME)
        path = icon_path()
        if path:
            self.setWindowIcon(QIcon(str(path)))
        self.setFixedSize(W, H)
        self.setGeometry(X, Y, W, H)
        self.setStyleSheet(BASE_QSS)

        self.central_widget = QWidget()
        self.central_widget.setStyleSheet(f"background: {THEME['bg']};")
        self.setCentralWidget(self.central_widget)

        #: Плейсхолдер — ровно та область, где живёт браузер (между панелями).
        self.browser_placeholder = QWidget(self.central_widget)
        self.browser_placeholder.setStyleSheet(f"background: {THEME['bg']};")
        self.browser_placeholder.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._build_ui()
        self._layout_children()

        self.overlay = LoadingOverlay(self)
        self._show_overlay("Запуск", "Поиск браузера и открытие чата…")

        self._register_hotkeys()

        self.topmost_timer = QTimer(self)
        self.topmost_timer.timeout.connect(self._enforce_topmost)
        self.topmost_timer.start(1200)

        # Окно браузера — другой процесс со своей очередью ввода: без этого
        # таймера клик по чату не отдаёт ему клавиатуру и «ничего не пишется».
        self.focus_timer = QTimer(self)
        self.focus_timer.timeout.connect(self._poll_browser_focus)
        self.focus_timer.start(25)
        self._last_dpr = 0.0

        # Смена масштаба/монитора: пересчитать сдвиг и раскладку сразу.
        try:
            window_handle = self.windowHandle()
            if window_handle is not None:
                window_handle.screenChanged.connect(lambda _s: self._on_screen_changed())
        except Exception:
            Log.exc("ui.screen_hook")

        QTimer.singleShot(120, self._start_browser)
        QTimer.singleShot(600, self._prepare_attachments_if_possible)

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        panel = f"background-color: {THEME['panel']};"

        self.top_bar = QWidget(self.central_widget)
        self.top_bar.setStyleSheet(panel + f"border-bottom: 1px solid {THEME['line']};")

        row = QHBoxLayout(self.top_bar)
        row.setContentsMargins(12, 6, 10, 6)
        row.setSpacing(6)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel("Legalyze")
        title.setStyleSheet(f"color: {THEME['accent']}; font-weight: 800; font-size: 15px;"
                            " letter-spacing: 0.3px; border: none;")
        brand.addWidget(title)
        self.status_label = QLabel("Запуск…")
        self.status_label.setStyleSheet(f"color: {THEME['muted']}; font-size: 9px; border: none;")
        brand.addWidget(self.status_label)
        row.addLayout(brand)
        row.addStretch()

        button_style = f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.04);
                color: {THEME['text']};
                border: 1px solid {THEME['line']};
                border-radius: 7px;
                font-size: 11px;
                font-weight: 600;
                padding: 5px 9px;
            }}
            QPushButton:hover {{
                background: rgba(124, 131, 255, 0.24);
                border-color: {THEME['accent']};
                color: #ffffff;
            }}
            QPushButton:pressed {{ background: rgba(124, 131, 255, 0.4); }}
        """

        self.chip_mic = HotkeyChip("Микрофон", self.cfg.get("hotkey_mic") or "—")
        self.chip_mic.clicked.connect(lambda: self._choose_hotkey("mic"))
        row.addWidget(self.chip_mic)

        self.chip_toggle = HotkeyChip("Окно", self.cfg.get("hotkey_toggle") or "—")
        self.chip_toggle.clicked.connect(lambda: self._choose_hotkey("toggle"))
        row.addWidget(self.chip_toggle)

        separator = QLabel("")
        separator.setFixedWidth(1)
        separator.setStyleSheet(f"background: {THEME['line']}; border: none;")
        row.addWidget(separator)

        self.btn_template = QPushButton("Шаблон")
        self.btn_template.setToolTip("Данные шаблона (уходят в чат файлом Шаблон.txt)")
        self.btn_template.setStyleSheet(button_style)
        self.btn_template.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_template.clicked.connect(lambda _c=False: self._open_template())
        row.addWidget(self.btn_template)

        self.btn_prompt = QPushButton("Промт")
        self.btn_prompt.setToolTip("Выбор промта из локальной папки")
        self.btn_prompt.setStyleSheet(button_style)
        self.btn_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_prompt.clicked.connect(lambda _c=False: self._choose_prompt())
        row.addWidget(self.btn_prompt)

        self.btn_reload = QPushButton("⟳")
        self.btn_reload.setToolTip("Перезапустить браузер и заново вложить файлы")
        self.btn_reload.setStyleSheet(button_style)
        self.btn_reload.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reload.clicked.connect(lambda _c=False: self._restart_browser())
        row.addWidget(self.btn_reload)

        btn_close = QPushButton("✕")
        btn_close.setToolTip("Закрыть")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet("""
            QPushButton {
                background: rgba(239, 68, 68, 0.16);
                color: #fca5a5;
                border: 1px solid rgba(239, 68, 68, 0.5);
                border-radius: 7px;
                font-size: 12px;
                font-weight: 700;
                padding: 5px 10px;
            }
            QPushButton:hover { background: rgba(239, 68, 68, 0.55); color: #fff; }
        """)
        btn_close.clicked.connect(lambda _c=False: self._close_app())
        row.addWidget(btn_close)

        self.bottom_bar = QWidget(self.central_widget)
        self.bottom_bar.setStyleSheet(panel + f"border-top: 1px solid {THEME['line']};")
        bottom = QHBoxLayout(self.bottom_bar)
        bottom.setContentsMargins(12, 4, 12, 4)
        bottom.setSpacing(8)

        self.files_label = QLabel("файлы: —")
        self.files_label.setStyleSheet(f"color: {THEME['text']}; font-size: 11px;"
                                       " font-weight: 600; border: none;")
        bottom.addWidget(self.files_label)
        bottom.addStretch()

        self.prompt_label = QLabel("промт: —")
        self.prompt_label.setStyleSheet(f"color: {THEME['muted']}; font-size: 10px; border: none;")
        bottom.addWidget(self.prompt_label)

        self.dot_label = QLabel("●")
        self.dot_label.setStyleSheet("color: #f59e0b; font-size: 11px; border: none;")
        bottom.addWidget(self.dot_label)

        self._update_prompt_label()

    def _layout_children(self) -> None:
        """Ручная раскладка: браузер занимает ровно область между панелями."""
        # resizeEvent может прийти раньше, чем панели созданы (setFixedSize в
        # конструкторе) — без этой проверки окно падало на старте.
        if not all(hasattr(self, name) for name in
                   ("central_widget", "top_bar", "bottom_bar", "browser_placeholder")):
            return
        width = self.central_widget.width() or W
        height = self.central_widget.height() or H
        self.top_bar.setGeometry(0, 0, width, self.top_inset)
        self.bottom_bar.setGeometry(0, height - self.bottom_inset, width, self.bottom_inset)
        self.browser_placeholder.setGeometry(0, self.top_inset, width,
                                             height - self.top_inset - self.bottom_inset)
        # Окно браузера встаёт по КЛИЕНТСКОЙ части плейсхолдера, а она равна
        # ему только пока окно программы не имеет рамки. Если Windows всё же
        # добавила рамку, браузер вылезал бы за область чата — поэтому размер
        # запоминается здесь и используется при вживлении.
        self.top_bar.raise_()
        self.bottom_bar.raise_()

    def moveEvent(self, event):
        """Окно переехало — шторка ожидания обязана переехать с ним.

        Это отдельное окно верхнего уровня (у него нет родителя, иначе оно не
        поднимется над встроенным браузером). Пока `moveEvent` не было, шторка
        оставалась на старом месте: сдвинули программу — «Ожидание» осталось
        висеть слева от неё.
        """
        super().moveEvent(event)
        position = (self.x(), self.y())
        if (_WINDOW_POSITION.get("x"), _WINDOW_POSITION.get("y")) != position:
            _WINDOW_POSITION["x"], _WINDOW_POSITION["y"] = position
            Log.info(f"окно перемещено: {position[0]},{position[1]}; "
                     f"масштаб={self.devicePixelRatioF():.2f}")
        try:
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
                force_topmost(handle_value(self.overlay.winId()))
        except Exception:
            Log.exc("ui.move_event")
        self.sync_browser()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()

    def showEvent(self, event):
        super().showEvent(event)
        self._layout_children()
        QTimer.singleShot(0, lambda: apply_win32_icon(handle_value(self.winId())))
        QTimer.singleShot(0, self._place_overlay)
        QTimer.singleShot(150, self._log_metrics_once)

    # ----------------------------------------------------------- window drag
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not isinstance(child, (QAbstractButton, QLineEdit, QComboBox)):
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            self.sync_browser()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str, color: str | None = None) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {color or THEME['muted']}; font-size: 9px; border: none;")

    def _set_dot(self, color: str) -> None:
        self.dot_label.setStyleSheet(f"color: {color}; font-size: 11px; border: none;")

    def _update_prompt_label(self) -> None:
        selected = self.cfg.get("selected_prompt") or ""
        name = prompt_display_name(selected) if selected else "—"
        self.prompt_label.setText(f"промт: {name}")

    def _update_files_label(self) -> None:
        names = self.controller.expected_names() if self.controller else []
        if not names:
            self.files_label.setText("файлы: —")
            return
        self.files_label.setText("файлы: " + " + ".join(names))

    def _show_overlay(self, title: str, message: str, error: bool = False) -> None:
        """Показать шторку и СРАЗУ поставить её над областью чата.

        `show_state` синхронизирует геометрию до `show()`, но у окна верхнего
        уровня первая отрисовка может прийти с задержкой — поэтому позиция
        выставляется повторно, уже после показа.
        """
        if self._closing:
            return
        self.overlay.show_state(title, message, error)
        self._place_overlay()

    def _hide_overlay(self) -> None:
        self.overlay.finish()

    def _enforce_topmost(self) -> None:
        """Держать окно программы (и шторку) поверх остальных окон.

        Окно без рамки легко «проваливается» под чужие окна. Заодно здесь же
        выравнивается шторка: размер/позиция окна могли измениться без
        событий Qt (переезд на монитор с другим масштабом).
        """
        if self._closing or not self.isVisible():
            return
        try:
            force_topmost(handle_value(self.winId()))
        except Exception:
            Log.exc("ui.topmost")
        try:
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
                force_topmost(handle_value(self.overlay.winId()))
        except Exception:
            Log.exc("ui.topmost_overlay")

    def _poll_browser_focus(self) -> None:
        """Отдать клавиатуру в чат, если пользователь щёлкнул внутри него."""
        if self._closing or self.host is None:
            return
        try:
            self.host.poll_focus(handle_value(self.winId()), self.isActiveWindow())
        except Exception:
            Log.exc("ui.focus_timer")

    def _place_overlay(self) -> None:
        """Поставить шторку ровно над областью чата (после показа окна)."""
        try:
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
                force_topmost(handle_value(self.overlay.winId()))
        except Exception:
            Log.exc("ui.place_overlay")

    def _on_screen_changed(self) -> None:
        """Монитор или масштаб сменились: пересчитать всё, не перезапуская браузер."""
        if self._closing:
            return
        try:
            ratio = float(self.devicePixelRatioF()) or 1.0
        except Exception:
            ratio = 1.0
        if abs(ratio - float(getattr(self, "_last_dpr", 0.0) or 0.0)) < 0.001:
            return
        self._last_dpr = ratio
        Log.info(f"масштаб экрана: {ratio}")
        self._layout_children()
        self.sync_browser()
        self._place_overlay()
        self._refresh_layout()

    def sync_browser(self) -> None:
        if self.host is not None:
            self.host.sync()

    def _browser_inset(self) -> tuple[int, int, int, int]:
        """Сдвиг окна браузера внутри плейсхолдера, в ФИЗИЧЕСКИХ пикселях.

        По умолчанию 0: браузер занимает ровно область между панелями, поэтому
        панели и страница не перекрываются ни на одной машине. Значения
        `browser_dx/dy/dw/dh` из config.json оставлены для точной подгонки.
        """
        try:
            dx = int(self.cfg.get("browser_dx", BROWSER_DX))
            dy = int(self.cfg.get("browser_dy", BROWSER_DY))
            dw = int(self.cfg.get("browser_dw", BROWSER_DW))
            dh = int(self.cfg.get("browser_dh", BROWSER_DH))
        except (TypeError, ValueError):
            dx, dy, dw, dh = BROWSER_DX, BROWSER_DY, BROWSER_DW, BROWSER_DH
        logical_w, logical_h = self._placeholder_logical_size()
        physical_w, physical_h = self._placeholder_physical_size()
        scale = (float(physical_w) / float(logical_w)) if logical_w else 1.0
        return (int(round(dx * scale)), int(round(dy * scale)),
                -int(round((dx + dw) * scale)), -int(round((dy + dh) * scale)))

    def _placeholder_logical_size(self) -> tuple[int, int]:
        placeholder = getattr(self, "browser_placeholder", None)
        if placeholder is not None:
            width, height = int(placeholder.width()), int(placeholder.height())
            if width > 0 and height > 0:
                return width, height
        return int(W), int(H - self.top_inset - self.bottom_inset)

    def _log_metrics_once(self) -> None:
        """Один раз записать в журнал размеры — по ним видно, где «уехало»."""
        try:
            logical = self._placeholder_logical_size()
            physical = self._placeholder_physical_size()
            Log.info(f"область чата: {logical[0]}x{logical[1]} DIP, "
                     f"{physical[0]}x{physical[1]} px, "
                     f"масштаб={self.devicePixelRatioF():.2f}, "
                     f"сдвиг браузера={self._browser_inset()}")
        except Exception:
            Log.exc("ui.log_metrics")

    def _placeholder_physical_size(self) -> tuple[int, int]:
        placeholder = getattr(self, "browser_placeholder", None)
        if placeholder is not None and user32:
            try:
                width, height = client_size(handle_value(placeholder.winId()))
                if width > 0 and height > 0:
                    return int(width), int(height)
            except Exception:
                Log.exc("ui.placeholder_physical")
        logical_w, logical_h = self._placeholder_logical_size()
        try:
            ratio = float(self.devicePixelRatioF()) or 1.0
        except Exception:
            ratio = 1.0
        return int(logical_w * ratio), int(logical_h * ratio)

    def _zoom_sizes(self) -> tuple[int, int, int, int]:
        logical_w, logical_h = self._placeholder_logical_size()
        physical_w, physical_h = self._placeholder_physical_size()
        return logical_w, logical_h, physical_w, physical_h

    # ------------------------------------------------------------- браузер
    def _start_browser(self) -> None:
        if self._closing or self.host is not None:
            return
        self._set_status("Ожидание страницы ИИ…", THEME["muted"])
        self._show_overlay("Запуск", "Открываем браузер и чат…")
        host = BrowserHost(self, url=CHAT_URL, profile=BROWSER_PROFILE,
                           lang=self.speech_lang, light_theme=self.light_theme,
                           width=W, height=H - self.top_inset - self.bottom_inset,
                           configured=str(self.cfg.get("browser_path") or ""),
                           inset_provider=self._browser_inset)
        self.host = host
        host.ready.connect(self._on_browser_ready)
        host.failed.connect(self._on_browser_failed)
        host.weak_speech.connect(self._on_weak_speech)
        if not host.start(self.browser_placeholder):
            self._on_browser_failed("no-browser")

    def _on_browser_ready(self, port: int) -> None:
        if self._closing:
            return
        self._set_status("Подключение к странице ИИ…", THEME["muted"])
        self._show_overlay("Подготовка страницы", "Проверяем готовность чата…")
        controller = PageController(port, self)
        controller.lang = self.speech_lang
        controller.light_theme = self.light_theme
        controller.sizes = self._zoom_sizes()
        controller.status.connect(self._on_page_status)
        controller.ready.connect(self._on_page_ready)
        controller.attachments_changed.connect(self._on_attachments_changed)
        controller.blocked.connect(self._on_upload_blocked)
        controller.failed.connect(self._on_page_failed)
        self.controller = controller
        controller.start()

    def _on_weak_speech(self, kind: str) -> None:
        """Найден браузер без штатного микрофона — предупредить, но работать."""
        self._set_status("Голосовой ввод: браузер без микрофона страницы", "#f59e0b")
        Log.warn(f"микрофон страницы недоступен в браузере «{kind}»")

    def _on_browser_failed(self, reason: str) -> None:
        if self._closing:
            return
        Log.error(f"браузер не запустился: {reason}")
        self._set_status("Браузер не запущен", THEME["danger"])
        if reason in ("embed-failed", "embed-lost"):
            # Окно браузера не прикрепилось: оставить его висеть отдельным
            # окном хуже, чем начать заново — иначе оно перекроет программу.
            self._stop_browser()
            self._show_overlay(
                "Браузер не встроился",
                "Закрываю и открываю окно заново…",
                error=True)
            QTimer.singleShot(700, self._restart_browser)
            return
        if reason in ("no-browser", "attempts-exhausted"):
            self._show_overlay(
                "Нужен Google Chrome",
                "Не найден ни один браузер на Chromium.\n"
                "Установите Google Chrome и нажмите «⟳».",
                error=True)
            QTimer.singleShot(0, self._explain_no_browser)
            return
        self._show_overlay("Браузер не запустился", "Нажмите «⟳», чтобы повторить.", error=True)

    def _explain_no_browser(self) -> None:
        QMessageBox.warning(
            self, "Нужен Google Chrome",
            "Для работы нужен установленный Google Chrome (или совместимый Chromium).\n\n"
            "Он нужен ради штатного микрофона страницы и стабильной отрисовки чата.\n"
            "После установки нажмите кнопку «⟳» в окне программы.")

    def _on_page_status(self, text: str, color: str) -> None:
        self._set_status(text, THEME.get(color, THEME["muted"]))

    def _on_page_ready(self) -> None:
        if self._closing:
            return
        self._chat_ready = True
        self._set_status("Готов к работе", THEME["ok"])
        self._set_dot(THEME["ok"])
        self._hide_overlay()
        self.sync_browser()
        self._update_files_label()
        # Окно браузера только что встало на место: раскладку перепроверяем
        # замером (Chrome переприменяет зум уже после показа окна).
        QTimer.singleShot(700, self._refresh_layout)
        self._prepare_attachments_if_possible()

    def _refresh_layout(self) -> None:
        """Пересчитать зум/раскладку после переезда окна или смены масштаба."""
        if self._closing or self.controller is None:
            return
        try:
            self.controller.sizes = self._zoom_sizes()
            self.controller.refresh_zoom()
        except Exception:
            Log.exc("ui.refresh_layout")

    def _on_page_failed(self, reason: str) -> None:
        if self._closing:
            return
        Log.error(f"страница: {reason}")
        self._show_overlay(
            "Страница не готова",
            "Войдите в аккаунт Google в открытом окне и нажмите «⟳».\n"
            f"Причина: {reason}",
            error=True)
        self._set_status("Войдите в Google, затем «⟳»", THEME["danger"])

    # -------------------------------------------------------------- промты
    def _choose_prompt(self) -> None:
        items = self.store.list_prompts()
        if not items:
            QMessageBox.information(
                self, "Промты",
                f"В папке промтов пусто:\n{self.store.folder}\n\n"
                "Положите туда файл .pdf, .txt или .md и нажмите «Промт» ещё раз.")
            self._open_prompts_folder()
            return
        display = [prompt_display_name(name) for name in items]
        current = self.cfg.get("selected_prompt") or ""
        index = items.index(current) if current in items else 0

        self.overlay.hide()
        dialog = PromptSelectionWindow(items, display, index, self.store.folder, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted or not dialog.choice:
            # Отмена: если чат уже работает — шторку не возвращаем, иначе
            # возвращаем то, что было (человек ждёт загрузку).
            if not self._attached and not self._chat_ready:
                self._show_overlay("Выберите промт",
                                   "Без промта экспорт в чат невозможен.")
            return

        self.cfg["selected_prompt"] = dialog.choice
        save_config(self.cfg)
        self._update_prompt_label()
        Log.info(f"выбран промт: {dialog.choice}")
        self._prepare_attachments_if_possible(force=True)

    def _open_prompts_folder(self) -> None:
        try:
            PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
            if IS_WINDOWS:
                os.startfile(str(PROMPTS_DIR))       # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(PROMPTS_DIR)])
        except Exception:
            Log.exc("ui.open_prompts")

    def _open_template(self) -> None:
        overlay_was_visible = self.overlay.isVisible()
        if overlay_was_visible:
            self.overlay.hide()
        dialog = TemplateWindow(self.cfg, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if overlay_was_visible and not self._attached:
            self.overlay.show()
            self.overlay.sync_geometry()
        if not accepted:
            return
        save_template(self.cfg, dialog.get_template())
        Log.info("шаблон сохранён")
        # Шаблон изменился — переложить оба файла в чат заново.
        self._prepare_attachments_if_possible(force=True)

    # ----------------------------------------------------------- вложения
    def _prepare_attachments_if_possible(self, force: bool = False) -> None:
        if self._closing or self.controller is None:
            return
        selected = self.cfg.get("selected_prompt") or ""
        if not selected:
            if not self._prompt_picker_shown:
                self._prompt_picker_shown = True
                self._show_overlay(
                    "Выберите промт",
                    "Сейчас откроется список промтов из локальной папки.")
                QTimer.singleShot(0, self._choose_prompt)
            return
        if not (self.store.folder / selected).is_file():
            Log.warn(f"файл промта пропал: {selected}")
            self.cfg["selected_prompt"] = ""
            save_config(self.cfg)
            self._update_prompt_label()
            QTimer.singleShot(0, self._choose_prompt)
            return
        if self.builder is not None and self.builder.isRunning():
            return
        if self._attached and not force:
            return

        # Если чат уже работает, шторка только мешает: меняем строку статуса.
        if not self._chat_ready or self._attached:
            self._show_overlay("Подготовка файлов", "Собираем PDF промта и Шаблон.txt…")
        self._set_status("Подготовка файлов…", THEME["muted"])
        self.builder = AttachmentBuilder(self.store, self.cfg, selected, self)
        self.builder.done.connect(self._on_attachments_built)
        self.builder.start()

    def _on_attachments_built(self, ok: bool, payload) -> None:
        if self._closing:
            return
        if not ok or not isinstance(payload, dict):
            Log.error(f"вложения не собраны: {payload}")
            self._show_overlay("Файлы не собраны", str(payload), error=True)
            self._set_status("Файлы не собраны", THEME["danger"])
            return
        if self.controller is None:
            return
        was_attached = self._attached
        self._attached = False
        items = payload.get("items") or []
        if was_attached:
            # Файлы уже лежали в чате (сменили шаблон или промт): их нужно
            # СНЯТЬ и вложить заново, иначе с тем же именем останется старый.
            self.controller.replace(items)
        else:
            # «Пусто в чате» подтверждается двумя замерами подряд: свежий цикл
            # начинается с двух ожиданий, а не с мгновенного впрыска.
            self.controller.set_expected(items)
            self.controller.arm()
        self._update_files_label()
        self._set_dot("#f59e0b")
        self._set_status("Вкладываем файлы в чат…", THEME["muted"])
        self._show_overlay(
            "Экспорт в чат",
            f"«{payload.get('pdf_name')}» + «{TEMPLATE_EXPORT_NAME}»")

    def _on_attachments_changed(self, attached: bool) -> None:
        if self._closing:
            return
        attached = bool(attached)
        if attached == self._attached:
            return
        self._attached = attached
        if attached:
            Log.info("оба файла в чате — готово")
            self._set_dot(THEME["ok"])
            self._set_status("Готов к работе", THEME["ok"])
            self._hide_overlay()
        else:
            self._set_dot("#f59e0b")
            self._set_status("Файлы исчезли из чата — вкладываем заново", "#f59e0b")

    def _on_upload_blocked(self, verdict: str) -> None:
        if self._closing:
            return
        Log.warn(f"вложение не удалось: {verdict}")
        self._set_dot(THEME["danger"])
        self._set_status("Файлы не вложились — нажмите «⟳»", THEME["danger"])
        self._show_overlay(
            "Файлы не вложились",
            "Страница не принимает вложения. Нажмите «⟳»: программа откроет\n"
            "рабочую страницу чата заново.",
            error=True)

    # ---------------------------------------------------------- микрофон
    def _on_hotkey(self, key_id: int) -> None:
        if self._closing:
            return
        if key_id == HOTKEY_TOGGLE_ID:
            self._toggle_visibility()
        elif key_id == HOTKEY_MIC_ID:
            self._toggle_mic()

    def _hotkey_tick(self) -> None:
        try:
            self.hotkeys.tick()
        except Exception:
            Log.exc("hotkey.tick")

    def _toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
            if self.overlay.isVisible():
                self.overlay.hide()
            if self.host is not None:
                self.host.sync()
        else:
            self.show()
            self.activateWindow()
            force_topmost(handle_value(self.winId()))
            if self.host is not None:
                self.host.sync()
            if self.controller is not None and self._chat_ready:
                self.controller.refresh_zoom()
            if not self._attached and not self._closing:
                self._show_overlay("Подготовка", "Пожалуйста, подождите…")

    def _toggle_mic(self) -> None:
        """Первое нажатие — запись, второе — отправка вопроса в чат."""
        if self.controller is None:
            return
        if not self._chat_ready:
            self._set_status("Страница ещё загружается — подождите…", THEME["muted"])
            return
        now = time.monotonic()
        if self._mic_busy or (now - self._mic_last_ts) < 0.35:
            return
        self._mic_last_ts = now
        self._mic_busy = True
        QTimer.singleShot(350, lambda: setattr(self, "_mic_busy", False))

        if not self.mic_active:
            self.mic_active = True
            self._mic_got_text = True
            self._mic_retried = False
            self.chip_mic.set_active(True)
            self._set_status("Запись голоса… (повтор: отправить)", THEME["ok"])
            self.controller.mic_start(callback=self._on_mic_started)
            return

        if not self._mic_got_text:
            self._mic_cancel("Запись не началась — нажмите «Микрофон» ещё раз")
            return

        self.mic_active = False
        self._mic_got_text = False
        self.chip_mic.set_active(False)
        self._set_status("Отправка запроса…", THEME["accent"])
        self.controller.mic_send(callback=self._on_mic_sent)
        QTimer.singleShot(2500, lambda: self._set_status("Готов к работе", THEME["ok"]))

    def _on_mic_started(self, value) -> None:
        if not isinstance(value, dict) or not value.get("ok"):
            reason = (value or {}).get("reason") if isinstance(value, dict) else "no-result"
            Log.warn(f"кнопка микрофона не найдена: {reason}")
            self._mic_reset("Кнопка микрофона не найдена — обновите страницу (⟳)",
                            THEME["danger"])
            return
        # Убедиться, что запись ДЕЙСТВИТЕЛЬНО началась (иначе второе нажатие
        # отправит пустой запрос — это была реальная жалоба в серверной версии).
        QTimer.singleShot(700, self._verify_recording)

    def _verify_recording(self) -> None:
        if self.controller is None or not self.mic_active:
            return

        def on_check(value):
            recording = bool(isinstance(value, dict) and value.get("recording"))
            if recording:
                self._mic_got_text = True
                self._set_status("Идёт запись… (повтор: отправить)", THEME["ok"])
                return
            if not self._mic_retried:
                self._mic_retried = True
                self.controller.mic_start()
                QTimer.singleShot(900, self._verify_recording)
                return
            self._mic_reset("Запись не началась — разрешите микрофон в Windows",
                            THEME["danger"])

        self.controller.mic_check(on_check)

    def _on_mic_sent(self, value) -> None:
        ok = isinstance(value, dict) and (value.get("ok") is True or value.get("fallback"))
        if not ok:
            Log.warn("кнопка отправки не найдена")
            self._set_status("Кнопка отправки не найдена — проверьте страницу",
                             THEME["danger"])

    def _mic_cancel(self, message: str) -> None:
        self._mic_reset(message, THEME["danger"])

    def _mic_reset(self, message: str, color: str) -> None:
        self.mic_active = False
        self._mic_got_text = False
        self._mic_retried = False
        self.chip_mic.set_active(False)
        self._set_status(message, color)

    # -------------------------------------------------------------- хоткеи
    def _register_hotkeys(self) -> None:
        toggle_key = normalize_key(self.cfg.get("hotkey_toggle"))
        mic_key = normalize_key(self.cfg.get("hotkey_mic"))
        self.hotkeys.set_keys(VK_MAP.get(toggle_key, 0), VK_MAP.get(mic_key, 0))
        try:
            self.hotkeys.try_suppress(handle_value(self.winId()))
        except Exception:
            Log.exc("hotkey.suppress")
        Log.info(f"горячие клавиши: микрофон={mic_key or '—'} окно={toggle_key or '—'}")

    def _choose_hotkey(self, which: str) -> None:
        if which == "mic":
            cfg_key, other_key = "hotkey_mic", "hotkey_toggle"
            caption = "Клавиша микрофона"
            chip = self.chip_mic
        else:
            cfg_key, other_key = "hotkey_toggle", "hotkey_mic"
            caption = "Клавиша окна"
            chip = self.chip_toggle
        current = self.cfg.get(cfg_key) or ""
        taken = {self.cfg.get(other_key) or ""} - {current}
        dialog = HotkeyDialog(current or "[ Нажмите клавишу ]", caption, taken, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.cfg[cfg_key] = dialog.selected
        self.cfg["hotkeys_configured"] = bool(self.cfg.get("hotkey_toggle")
                                             and self.cfg.get("hotkey_mic"))
        save_config(self.cfg)
        chip.set_key(dialog.selected)
        self._register_hotkeys()
        Log.info(f"горячая клавиша {cfg_key} = {dialog.selected}")

    # ------------------------------------------------------------ сервис
    def _restart_browser(self) -> None:
        """Полный перезапуск браузера и автоматизации (кнопка «⟳»)."""
        if self._closing:
            return
        Log.info("перезапуск по кнопке")
        self._chat_ready = False
        self._attached = False
        self._set_dot("#f59e0b")
        self._set_status("Перезапуск…", THEME["muted"])
        self._show_overlay("Перезапуск", "Закрываем и открываем чат заново…")
        self._stop_browser()
        QTimer.singleShot(400, self._start_browser)

    def _stop_browser(self) -> None:
        controller, self.controller = self.controller, None
        if controller is not None:
            try:
                controller.request_stop()
            except Exception:
                pass
        host, self.host = self.host, None
        if host is not None:
            try:
                host.stop()
            except Exception:
                Log.exc("ui.stop_host")

    def _close_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cleanup()
        QApplication.instance().quit()

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self._hotkey_timer.stop()
            self.topmost_timer.stop()
            self.focus_timer.stop()
        except Exception:
            pass
        try:
            self.hotkeys.release()
        except Exception:
            pass
        self.overlay.finish()
        self._stop_browser()
        Log.info("приложение закрыто")

    def closeEvent(self, event):
        self._close_app()
        event.accept()

    def changeEvent(self, event):
        """Свернули окно — убрать шторку, развернули — вернуть, если нужно."""
        super().changeEvent(event)
        try:
            if event.type() == QEvent.Type.WindowStateChange:
                if self.isMinimized():
                    # Свёрнутое окно не должно оставлять шторку висеть на
                    # экране: она — отдельное окно верхнего уровня.
                    self.overlay.hide()
                elif self.overlay._anim.isActive() and not self._attached:
                    self.overlay.show()
                    self._place_overlay()
        except Exception:
            Log.exc("ui.change_event")

# =============================================================================
# 21. ЗАПУСК
# =============================================================================

def suppress_console() -> None:
    """Скрыть и отсоединить консольное окно (Windows) — приложение без консоли."""
    if not IS_WINDOWS:
        return
    try:
        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, SW_HIDE)
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def copy_bundled_font() -> None:
    """Положить DejaVuSans.ttf рядом с данными: он нужен для кириллицы в PDF."""
    target = DATA_DIR / "DejaVuSans.ttf"
    if target.is_file():
        return
    for source in (SCRIPT_DIR / "data" / "DejaVuSans.ttf",
                   getattr(sys, "_MEIPASS", "") and Path(getattr(sys, "_MEIPASS")) / "data" / "DejaVuSans.ttf",
                   ROOT_DIR / "data" / "DejaVuSans.ttf"):
        try:
            if source and Path(source).is_file():
                ensure_dirs()
                shutil.copy2(source, target)
                return
        except Exception:
            continue
    Log.warn("шрифт DejaVuSans.ttf не найден — текстовые промты будут собираться "
             "системным шрифтом (кириллица может не отобразиться)")


def check_dependencies() -> bool:
    """Проверить, что установлено всё нужное, и объяснить, чего нет."""
    import importlib.util

    missing = [name for module, name in (("PyQt6", "PyQt6"),
                                         ("websocket", "websocket-client"))
               if importlib.util.find_spec(module) is None]
    if missing:
        message = ("Не установлены зависимости: " + ", ".join(missing) + "\n\n"
                   "Установите их командой:\n"
                   "    pip install -r requirements-local.txt")
        try:
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        except Exception:
            print(message)
        return False
    return True


def check_pdf_support() -> bool:
    """fpdf2 нужен ТОЛЬКО для промтов .txt/.md/.json — без него программа жива.

    Серверная версия требовала его на старте и не запускалась вовсе; здесь
    достаточно предупредить: промты в PDF работают без конвертации.
    """
    import importlib.util

    if importlib.util.find_spec("fpdf") is not None:
        return True
    message = ("Пакет fpdf2 не установлен.\n\n"
               "Промты в формате .pdf это не затрагивает. Но промты .txt, .md и "
               ".json преобразуются в PDF через fpdf2 и работать не будут.\n\n"
               "Установить:  pip install fpdf2")
    Log.warn("fpdf2 не установлен — текстовые промты недоступны")
    try:
        # Один раз, на старте: без консоли это единственный способ увидеть.
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
    except Exception:
        print(message)
    return False


def main() -> int:
    suppress_console()
    if not check_dependencies():
        return 1
    check_pdf_support()
    ensure_dirs()
    copy_bundled_font()
    Log.info(f"=== {APP_NAME} {APP_VERSION} ===")
    Log.info(f"папка программы: {ROOT_DIR}")
    Log.info(f"промты: {PROMPTS_DIR}")

    cfg = load_config()
    ensure_template_file(cfg.get("template") or {})

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    path = icon_path()
    if path:
        app.setWindowIcon(QIcon(str(path)))

    # Один экземпляр: второй процесс молча выходит (иначе два браузера делят
    # один профиль и окно «залипает»).
    lock = QLockFile(str(ROOT_DIR / "locallegalyze.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        Log.warn("уже запущен другой экземпляр — выход")
        return 0

    # Первичная настройка: шаблон и горячие клавиши.
    try:
        if not template_filled(cfg):
            dialog = TemplateWindow(cfg)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                save_template(cfg, dialog.get_template())
    except Exception:
        Log.exc("main.template")

    try:
        if not (cfg.get("hotkey_toggle") and cfg.get("hotkey_mic")
                and cfg.get("hotkeys_configured")):
            dialog = OnboardingHotkeyWindow(cfg)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                cfg["hotkey_toggle"] = dialog.get_toggle_key()
                cfg["hotkey_mic"] = dialog.get_mic_key()
                cfg["hotkeys_configured"] = bool(cfg["hotkey_toggle"] and cfg["hotkey_mic"])
                save_config(cfg)
    except Exception:
        Log.exc("main.hotkeys")

    window = MainWindow(cfg)
    window.show()
    window.raise_()
    window.activateWindow()
    force_topmost(handle_value(window.winId()))

    exit_code = app.exec()
    try:
        Log.info("=== завершение ===")
        Log._flush("\n".join(Log._buffer) + "\n")
        Log._buffer.clear()
    except Exception:
        pass
    return int(exit_code)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        try:
            Log.exc("main.crash")
            ctypes.windll.user32.MessageBoxW(
                None, "Приложение остановлено из-за ошибки.\n\n"
                      f"Подробности в файле:\n{LOG_FILE}", APP_NAME, 0x10)
        except Exception:
            traceback.print_exc()
        sys.exit(1)
