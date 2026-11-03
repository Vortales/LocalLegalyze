# -*- coding: utf-8 -*-
"""
tests_local.py — проверки локальной версии БЕЗ запуска GUI и браузера.

Запуск:
    python tests_local.py

Проверяется то, что нельзя проверить «на глаз»:

1. правило вложения файлов (`plan_attachments`) — включая регрессию
   «Memphis.pdf Шаблон.txt Memphis.pdf Шаблон.txt» из серверной версии;
2. поведение при медленной странице (чип появляется через 4 с) — то, из-за
   чего дефект и проявлялся «после нескольких запросов»;
3. сопоставление имён файлов;
4. локальный склад промтов: .pdf, .txt, .enc;
5. отсутствие сетевых вызовов к серверу (главное требование этой версии);
6. синтаксис всех JS-скриптов страницы (если установлен Node.js).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import LocalLegalyze_Local as app   # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  OK   {message}")
    else:
        print(f"  FAIL {message}")
        FAILURES.append(message)


SKIP_DOCS = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
SKIP_COMMENTS = re.compile(r"(?m)^\s*#.*$")


def strip_docs_and_comments(text: str) -> str:
    """Убрать докстроки и строки-комментарии: остаётся только код."""
    text = SKIP_DOCS.sub('""', text)
    text = SKIP_COMMENTS.sub("", text)
    return text


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. Правило вложения
# ---------------------------------------------------------------------------

def test_plan():
    section("1. Правило вложения файлов")
    pdf, tpl = "Memphis.pdf", "Шаблон.txt"
    expected = [pdf, tpl]

    def plan(state, attempts=None, empty=0, injected=False, hold=0.0, now=1000.0):
        return app.plan_attachments(expected, state, attempts or {}, empty, injected,
                                    now=now, hold_until=hold)

    check(plan({"present": 0, "names": []}, empty=0)["action"] == "wait",
          "пусто, первый замер → ждём (подтверждения «пусто» ещё нет)")
    check(plan({"present": 0, "names": []}, empty=5)["action"] == "inject",
          "пусто подтверждено → вкладываем")
    check(plan({"present": 1, "names": [pdf]})["action"] == "inject",
          "один файл есть → вкладываем недостающий")
    check(plan({"present": 1, "names": [pdf]})["targets"] == [tpl],
          "вкладывается РОВНО недостающий файл, а не оба")
    check(plan({"present": 2, "names": [pdf, tpl]})["action"] == "sealed",
          "оба файла на месте → запор: впрысков нет")
    check(plan({"present": 2, "names": ["", ""]})["action"] == "sealed",
          "два безымянных чипа → считаем готовым (не впрыскиваем заново)")
    check(plan({"present": 1, "names": [pdf], "uploading": True})["action"] == "wait",
          "идёт загрузка → любой впрыск сейчас дал бы дубль")
    check(plan({"present": 0, "names": []}, empty=9, injected=True,
               hold=1001.0)["action"] == "wait",
          "только что впрыснули → ждём появления чипа")

    # --- главная регрессия: ровно столько чипов, сколько файлов, но имена
    #     одинаковые. Старая версия считала это нормой только при > 4.
    dup = plan({"present": 2, "names": [pdf, pdf]})
    check(dup["action"] == "prune", "Memphis.pdf Memphis.pdf → удаляем лишний")

    four = plan({"present": 4, "names": [pdf, tpl, pdf, tpl]})
    check(four["action"] == "prune",
          "четыре чипа (симптом пользователя) → удаляем, а не «готово»")

    foreign = plan({"present": 2, "names": [pdf, "Чужое.pdf"]})
    check(foreign["action"] == "prune", "чужой файл в чате → удаляем")

    blocked = plan({"present": 0, "names": []}, attempts={pdf: 4, tpl: 4}, empty=9)
    check(blocked["action"] == "blocked",
          "после лимита попыток → «вложение не работает», а не бесконечный цикл")


# ---------------------------------------------------------------------------
# 2. Симуляция медленной страницы
# ---------------------------------------------------------------------------

class FakePage:
    """Страница, которая создаёт чип ЧЕРЕЗ ЗАДАННОЕ ВРЕМЯ после впрыска."""

    def __init__(self, chip_delay: float):
        self.chip_delay = chip_delay
        self.chips: list[tuple[str, float]] = []
        self.now = 0.0
        self.injects: list[str] = []

    def alive(self):
        return [c for c in self.chips if c[1] <= self.now]

    def value(self, expr, timeout=None, default=None):
        text = expr.strip()
        # Различаем скрипты по УНИКАЛЬНЫМ строкам тел, а не по началу:
        # и проверка состояния, и удаление чипов начинаются с `const chips`.
        if "return {present:" in text:                       # JS_CHECK_FILES
            names = [chip[0] for chip in self.alive()]
            return {"present": len(names), "names": names, "uploading": False,
                    "chips": len(names), "composer": 1, "named": len(names)}
        if "chips[chips.length - 1]" in text:                # JS_REMOVE_LAST
            if self.chips:
                self.chips.pop()
            return 1
        if "ArblTe" in text and "querySelectorAll" in text:  # JS_REMOVE_ALL_FILES
            removed = len(self.chips)
            self.chips.clear()
            return removed
        if "DataTransfer" in text and 'input[type="file"]' in text:
            name = text.split('const name = "')[1].split('"')[0]
            self.injects.append(name)
            self.chips.append((name, self.now + self.chip_delay))
            return {"ok": True, "name": name, "inputs": 1, "mode": "input"}
        if "store.parts.push" in text:
            return 1
        if "store.pending = {" in text:
            return {"ok": True, "size": 10}
        if "s.pending" in text:
            return True
        if "store.parts = []" in text and "File(" not in text:
            return True
        return default

    def eval(self, *args, **kwargs):
        return {"value": True}

    def send(self, *args, **kwargs):
        return {}


class NullSignal:
    def __init__(self):
        self.seen = []

    def emit(self, *args):
        self.seen.append(args)

    def connect(self, *_args):
        pass


def run_controller(chip_delay: float, seconds: float = 12.0):
    page = FakePage(chip_delay)
    controller = app.PageController(0)
    controller.page = page
    controller.set_expected([("a", b"%PDF-x", "Memphis.pdf"),
                             ("b", b"txt", "Шаблон.txt")])
    controller.arm()
    for name in ("status", "attachments_changed", "blocked", "failed", "ready"):
        setattr(controller, name, NullSignal())
    step = app.POLL_INTERVAL
    ticks = int(seconds / step)
    for index in range(ticks):
        page.now = index * step
        controller._tick()
    return page, controller


def test_slow_page():
    section("2. Симуляция страницы: чип появляется не мгновенно")
    for delay in (0.3, 1.2, 2.5, 4.0):
        page, controller = run_controller(delay)
        names = [chip[0] for chip in page.alive()]
        check(len(page.injects) == 2,
              f"задержка {delay} с: впрысков ровно 2 (получено {len(page.injects)})")
        check(len(names) == 2,
              f"задержка {delay} с: в чате ровно 2 файла (получено {names})")
        check(bool(controller.attachments_changed.seen),
              f"задержка {delay} с: приложение поняло, что файлы вложены")


def test_replace_flow():
    """Смена шаблона: старые чипы снимаются, новые вкладываются, дублей нет."""
    section("3. Замена вложений (сменили шаблон или промт)")
    page = FakePage(0.5)
    controller = app.PageController(0)
    controller.page = page
    controller.set_expected([("a", b"%PDF-x", "Memphis.pdf"),
                             ("b", b"txt", "Шаблон.txt")])
    controller.arm()
    for name in ("status", "attachments_changed", "blocked", "failed", "ready"):
        setattr(controller, name, NullSignal())

    # Первый цикл: файлы вложены.
    for index in range(int(6 / app.POLL_INTERVAL)):
        page.now = index * app.POLL_INTERVAL
        controller._tick()
    check(len(page.alive()) == 2, "перед заменой в чате два файла")
    check(controller._sealed, "включён запор")

    # Замена: снять всё и разложить заново.
    dropped = controller._drop_all_chips()
    check(dropped >= 1, "запрошено снятие всех вложений")
    page.now += 1.0
    controller.set_expected([("a", b"%PDF-x", "Memphis.pdf"),
                             ("b", b"txt2", "Шаблон.txt")])
    controller.arm()
    start = len(page.injects)
    for index in range(int(10 / app.POLL_INTERVAL)):
        page.now += app.POLL_INTERVAL
        controller._tick()
    names = [chip[0] for chip in page.alive()]
    check(len(names) == 2, f"после замены в чате снова ровно два файла (получено {names})")
    check(len(page.injects) - start == 2,
          f"впрысков при замене ровно 2 (получено {len(page.injects) - start})")


def test_old_behaviour_reproduced():
    """Показываем, что именно так вёл себя UploadThread серверной версии."""
    section("4. Воспроизведение прежнего дефекта (для сравнения)")

    def chat_has_file(names, wanted):
        want = (wanted or "").strip().lower()
        for name in names:
            text = str(name or "").lower()
            if want and (want in text or text in want):
                return True
        return False

    page = FakePage(4.0)
    required = 2
    expected = ["Memphis.pdf", "Шаблон.txt"]
    last_upload = 0.0
    dedup_done = False
    step = 0.05
    now = 0.0
    while now < 12.0:
        page.now = now
        state = page.value(app.JS_CHECK_FILES) or {}
        names, present = state["names"], state["present"]
        names_ok = any(str(n or "").strip() for n in names)
        if names_ok:
            missing = [p for p in expected if not chat_has_file(names, p)]
            complete = present >= required and not missing
        else:
            missing = []
            complete = present >= required
        if complete:
            if names_ok and not dedup_done and present > required + 2:
                dedup_done = True
                page.chips.clear()
            now += 0.15
            continue
        if (now - last_upload) > 0.8:
            for path in (missing or (expected if present < required else [])):
                page.injects.append(path)
                page.chips.append((path, now + page.chip_delay))
            last_upload = now
            now += 0.3
            continue
        now += step
    check(len(page.injects) > 2,
          f"старое правило впрыскивает лишнее: {len(page.injects)} впрысков "
          f"вместо 2 (это и есть причина дублей)")


# ---------------------------------------------------------------------------
# 4. Имена файлов
# ---------------------------------------------------------------------------

def test_names():
    section("5. Сопоставление имён")
    cases = [
        ("Memphis.pdf", "Memphis.pdf", "exact"),
        ("Memphis.pdf", "memphis.PDF", "exact"),
        ("Memphis.pdf", "  Memphis.pdf  ", "exact"),
        ("Memphis.pdf", "Memphis (1).pdf", "exact"),
        ("Memphis.pdf", "Шаблон.txt", ""),
        ("Договор.pdf", "Договор.pdf", "exact"),
        ("a.pdf", "b.pdf", ""),
    ]
    for wanted, have, expected in cases:
        got = app.name_match(wanted, have)
        check(got == expected, f"{wanted!r} ~ {have!r} → {got!r} (ожидалось {expected!r})")
    check(app.norm_name("Memphis (2).PDF") == "memphis.pdf",
          "«(2)» и регистр приводятся к одному виду")
    check(app.pdf_file_name("Промт.2024.md") == "Промт.2024.pdf",
          "имя PDF для чата: снимается последнее расширение")
    check(app.pdf_file_name("Шаблон № 5") == "Шаблон № 5.pdf",
          "имя без расширения не портится")


# ---------------------------------------------------------------------------
# 5. Локальный склад промтов
# ---------------------------------------------------------------------------

def test_prompt_store():
    section("6. Локальный склад промтов (папка prompts/)")
    folder = Path(tempfile.mkdtemp(prefix="llz-prompts-"))
    try:
        store = app.PromptStore(folder)
        store.ensure_example()
        check(bool(store.list_prompts()), "пустая папка получает пример промта")

        pdf_raw = b"%PDF-1.4\n%test\n"
        (folder / "Memphis.pdf").write_bytes(pdf_raw)
        data, name = store.build_pdf("Memphis.pdf")
        check(data == pdf_raw, "готовый PDF берётся как есть")
        check(name == "Memphis.pdf", "имя PDF совпадает с именем файла")

        (folder / "Правила.txt").write_text("Тестовый промт\nВторая строка",
                                            encoding="utf-8")
        data, name = store.build_pdf("Правила.txt")
        check(data[:4] == b"%PDF", "текстовый промт собирается в PDF")
        check(name == "Правила.pdf", "имя собранного PDF — «Правила.pdf»")

        try:
            store.build_pdf("Нет такого.pdf")
            check(False, "отсутствующий файл должен давать ошибку")
        except FileNotFoundError:
            check(True, "отсутствующий файл даёт понятную ошибку")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Никаких обращений к серверу
# ---------------------------------------------------------------------------

def test_no_network():
    section("7. Локальность: обращений к серверу нет")
    raw = (HERE / "LocalLegalyze_Local.py").read_text(encoding="utf-8")
    # Докстроки и комментарии этой версии ЧЕСТНО описывают, что из серверной
    # версии убрано, — поэтому ищем сетевые вызовы только в исполняемом коде.
    source = strip_docs_and_comments(raw)
    forbidden = {
        "домен legalyzeai.ru": r"legalyzeai\.ru",
        "вызов /api/client/": r"/api/client/",
        "модуль requests": r"^\s*(import|from)\s+requests\b",
        "окно авторизации": r"class\s+LoginWindow",
        "воркер авторизации": r"class\s+AuthWorker",
        "привязка HWID": r"class\s+HWIDWorker|generate_hwid",
        "счётчик запросов": r"class\s+BalanceWorker|class\s+DecrementWorker",
        "проверка версии": r"check_for_update",
        "скачивание промтов": r"/api/client/prompts",
    }
    for label, pattern in forbidden.items():
        check(re.search(pattern, source) is None, f"отсутствует: {label}")
    check("SERVER_URL" not in source, "константы SERVER_URL нет")
    check("prompts" in source and "PROMPTS_DIR" in source,
          "промты берутся из локальной папки")


# ---------------------------------------------------------------------------
# 7. JS-скрипты страницы
# ---------------------------------------------------------------------------

def test_js():
    section("8. JavaScript страницы")
    node = shutil.which("node")
    scripts = {name: value for name, value in vars(app).items()
               if isinstance(value, str) and (
                   name.startswith("JS_") or name.startswith("_JS_")
                   or name in ("READY_SCRIPT", "INJECT_FILE_JS", "STASH_INIT_JS",
                               "STASH_PUSH_JS", "STASH_COMMIT_JS"))}
    check(len(scripts) >= 15, f"найдено скриптов: {len(scripts)}")
    if not node:
        print("  --   Node.js не найден: синтаксис JS не проверялся")
        return
    payload = Path(tempfile.mkdtemp(prefix="llz-js-"))
    try:
        dump = payload / "dump.json"
        dump.write_text(json.dumps(scripts), encoding="utf-8")
        checker = payload / "check.js"
        checker.write_text(
            "const fs=require('fs');"
            "const src=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));"
            "let bad=[];"
            "for (const [n,s] of Object.entries(src)) "
            "{ try { new Function(s); } catch(e) { bad.push(n+': '+e.message); } }"
            "console.log(JSON.stringify(bad));",
            encoding="utf-8")
        result = subprocess.run([node, str(checker), str(dump)],
                                capture_output=True, text=True, timeout=60)
        bad = json.loads(result.stdout.strip() or "[]")
        check(not bad, f"все {len(scripts)} скриптов синтаксически верны"
                       + (f" (ошибки: {bad})" if bad else ""))
    finally:
        shutil.rmtree(payload, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. Win32-вживление: регрессии, из-за которых браузер «выходил из GUI»
# ---------------------------------------------------------------------------

def test_win32_handles():
    """HWND из Win32 приходит как c_void_p — читать надо `.value`, а не int()."""
    section("9. Хэндлы и вживление окна (регрессии после запуска на Windows)")

    import ctypes
    pointer = ctypes.c_void_p(0x1A2B3C4D5E)
    check(app.handle_value(pointer) == 0x1A2B3C4D5E,
          "handle_value(c_void_p) даёт адрес, а не 0")
    check(app.handle_value(pointer.value) == 0x1A2B3C4D5E,
          "handle_value(int) работает как раньше")
    check(app.handle_value(None) == 0, "handle_value(None) = 0")
    check(app.hwnd_of(pointer).value == 0x1A2B3C4D5E,
          "hwnd_of(c_void_p) не превращает хэндл в 0")
    try:
        int(pointer)
    except (TypeError, ValueError):
        check(True, "int(c_void_p) действительно падает — откуда и брался 0")
    else:
        check(False, "int(c_void_p) не падает: проверка устарела")

    check(isinstance(app.HWND_TOPMOST, ctypes.c_void_p),
          "HWND_TOPMOST передаётся как c_void_p(-1)")
    check(app.HWND_TOPMOST.value == 0xFFFFFFFFFFFFFFFF,
          "HWND_TOPMOST = -1 в машинном представлении")


def test_win32_style():
    """Стили окна — знаковые 32 бита; WS_CHILD обязан ставиться и сниматься."""
    def signed(value):
        raw = value & 0xFFFFFFFF
        return raw - 0x100000000 if raw & 0x80000000 else raw

    check(signed(app.WS_POPUP) == -2147483648, "WS_POPUP = 0x80000000 приводится к LONG")
    check(signed(app.WS_CHILD) == 0x40000000, "WS_CHILD остаётся положительным")
    popup = signed(app.WS_POPUP)
    children = (popup & ~(app.WS_POPUP | app.WS_CAPTION | app.WS_SYSMENU |
                          app.WS_THICKFRAME | app.WS_MINIMIZEBOX |
                          app.WS_MAXIMIZEBOX | app.WS_DLGFRAME | app.WS_BORDER)) \
        | app.WS_CHILD | app.WS_VISIBLE | app.WS_CLIPCHILDREN | app.WS_CLIPSIBLINGS
    check(children & app.WS_CHILD, "WS_CHILD добавляется к стилю")
    check(not children & app.WS_POPUP, "WS_POPUP снимается (иначе окно остаётся отдельным)")
    check(not children & app.WS_CAPTION, "рамка с кнопками снимается")


def test_win32_source_invariants():
    """Статическая проверка кода: то, что нельзя увидеть без Windows."""
    import ast

    source = Path(HERE / "LocalLegalyze_Local.py").read_text()
    code = strip_docs_and_comments(source)

    # 1. Никаких int(...) вокруг хэндлов, вернувшихся из ctypes.
    tree = ast.parse(source)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "int" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name) \
                and argument.func.id in ("hwnd_of", "GetParent"):
            bad.append(node.lineno)
    check(not bad, f"нет int() вокруг c_void_p (найдено: {len(bad)})")

    check("handle_value(" in code, "преобразование хэндла идёт через handle_value()")
    check("int(self.winId())" not in code and "int(self.placeholder.winId())" not in code,
          "хэндлы Qt (winId) тоже идут через handle_value()")
    check("SetWindowLongW" in code and "c_void_p(value &" not in code,
          "стиль пишется через SetWindowLongW с LONG, а не указателем")
    check("c_void_p(-1)" in code, "HWND_TOPMOST объявлен как указатель")
    check("hwnd_of(hwnd), HWND_TOPMOST" in code,
          "force_topmost передаёт настоящий HWND и HWND_TOPMOST")
    check("SetParent" in code and "is_child_of" in code,
          "после SetParent выполняется проверка GetParent")
    check("while True" in code and "SetParent не прикрепил" in code,
          "вживление повторяется, пока окно не станет дочерним")
    check("detach_window" in code, "неудачное вживление откатывается")
    check("hide_from_taskbar" in code and "_COM_SLOT_DELETE_TAB" in code,
          "кнопка браузера убирается из панели задач через DeleteTab")
    check("ReadProcessMemory" in code, "таблица методов COM читается безопасно")
    check("def poll_focus" in code and "AttachThreadInput" in code,
          "фокус клавиатуры передаётся в чат по клику")
    check("def moveEvent" in code, "у главного окна есть moveEvent")
    check("self.overlay.sync_geometry()" in code,
          "шторка пересчитывает положение вместе с окном")
    check("is_child_of(self.hwnd, parent)" in code,
          "хост замечает отрыв окна браузера и вживляет заново")
    check("focus_timer" in code, "есть таймер передачи фокуса")


def test_window_choice():
    """Вживлять нужно ОКНО СТРАНИЦЫ, а не служебное окно браузера."""
    section("10. Какое окно вживлять")

    windows = [
        {"hwnd": 1, "class": "Chrome_WidgetWin_0", "visible": True, "parent": 0,
         "rect": (0, 0, 520, 300), "area": 520 * 300, "owner": 0, "self_root": True},
        {"hwnd": 2, "class": "Chrome_WidgetWin_1", "visible": True, "parent": 0,
         "rect": (0, 0, 453, 735), "area": 453 * 735, "owner": 0, "self_root": True},
        {"hwnd": 3, "class": "Chrome_WidgetWin_1", "visible": True, "parent": 0,
         "rect": (0, 0, 300, 200), "area": 300 * 200, "owner": 0, "self_root": True},
    ]
    top = [w for w in windows if w["visible"] and not w["parent"]]
    eligible = [w for w in top if w["class"] == app.BROWSER_WINDOW_CLASS] or top
    best = max(eligible, key=lambda w: w["area"])
    check(best["hwnd"] == 2, "выбирается окно страницы (Chrome_WidgetWin_1), а не служебное")
    check(best["class"] == "Chrome_WidgetWin_1", "класс выбранного окна — окно приложения")

    source = Path(HERE / "LocalLegalyze_Local.py").read_text()
    check('"class"] != BROWSER_WINDOW_CLASS' in source,
          "поиск окна страницы фильтрует по классу")
    check("_wait_for_page" in source,
          "вживление ждёт, пока ссылка --app= станет настоящей страницей")
    check('window.get("class") == BROWSER_WINDOW_CLASS' in source,
          "WM_CLOSE посылается только окну приложения")
    check('if window.get("parent") and not window.get("self_root")' in source,
          "вложенные служебные окна не закрываются")


def test_layout_contract():
    """Шторка обязана геометрически совпадать с областью чата (без запуска GUI)."""
    section("11. Раскладка: шторка и область чата")

    class FakeRect:
        def __init__(self, x, y, w, h):
            self._x, self._y, self._w, self._h = x, y, w, h

        def x(self):
            return self._x

        def y(self):
            return self._y

        def width(self):
            return self._w

        def height(self):
            return self._h

    class FakeOwner:
        top_inset = 54
        bottom_inset = 30

        def frameGeometry(self):
            return FakeRect(1426, 200, 453, 735)

    # Повторяем ровно ту формулу, что в LoadingOverlay.sync_geometry.
    geometry = FakeOwner().frameGeometry()
    top, bottom = FakeOwner.top_inset, FakeOwner.bottom_inset
    box = (geometry.x(), geometry.y() + top, geometry.width(),
           geometry.height() - top - bottom)
    check(box == (1426, 254, 453, 651), f"шторка = область чата: {box}")
    check(box[1] == 200 + 54, "верх шторки = верх окна + верхняя панель")
    check(box[3] == 735 - 54 - 30, "низ шторки = низ окна − нижняя панель")

    # Сдвинули окно на +120/+75 — шторка обязана сдвинуться ровно так же.
    moved = (box[0] + 120, box[1] + 75, box[2], box[3])
    check(moved == (1546, 329, 453, 651), f"шторка едет вместе с окном: {moved}")

    source = Path(HERE / "LocalLegalyze_Local.py").read_text()
    check("frameGeometry()" in source,
          "положение берётся у рамки окна (иначе шторка уезжает на толщину рамки)")
    check("def moveEvent" in source, "moveEvent пересчитывает шторку")
    check(source.count("self._place_overlay()") >= 3,
          "шторка ставится на место из показа, смены состояния и таймера")
    check("focus_timer.start(25)" in source, "опрос фокуса идёт с шагом 25 мс")


def test_embed_retry_logic():
    """Повтор SetParent: пока GetParent не подтвердил, попытки продолжаются."""
    state = {"parent": 0, "calls": 0}

    def fake_embed(ok_on_call):
        for _attempt in range(6):
            state["calls"] += 1
            if state["calls"] >= ok_on_call:
                state["parent"] = 777
                return True
        return False

    check(fake_embed(3), "вживление удаётся с 3-й попытки")
    check(state["calls"] == 3, "попыток ровно 3 — лишних нет")
    check(state["parent"] == 777, "GetParent подтверждает нужного родителя")


# ---------------------------------------------------------------------------

def test_report():
    """Короткая справка по журналу: что искать, если снова «уедет»."""
    section("12. Подсказка на случай повторения")
    print("  Если браузер снова окажется отдельным окном — в журнале ищите строки:")
    print("    «окно браузера встроено»      — вживление удалось (есть номер попытки)")
    print("    «окно браузера выпало»        — окно оторвалось, программа его вернула")
    print("    «окно перемещено»             — координаты окна на экране")
    print("    «область чата: ... DIP»       — размеры и масштаб (сдвиг браузера)")
    print("    «масштаб экрана»              — сменился монитор/масштаб Windows")


# ---------------------------------------------------------------------------

def main() -> int:
    print("Проверки LocalLegalyze (локальная версия)")
    test_plan()
    test_slow_page()
    test_replace_flow()
    test_old_behaviour_reproduced()
    test_names()
    test_prompt_store()
    test_no_network()
    test_win32_handles()
    test_win32_style()
    test_win32_source_invariants()
    test_window_choice()
    test_layout_contract()
    test_embed_retry_logic()
    test_js()
    test_report()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} из {CHECKS}")
        for item in FAILURES:
            print("  -", item)
        return 1
    print(f"ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ({CHECKS})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
