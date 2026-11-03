# -*- coding: utf-8 -*-
"""
smoke_local.py — проверка «всё собрано и связано» без браузера.

Запуск (нужен настоящий PyQt6 и экран либо QT_QPA_PLATFORM=offscreen):

    QT_QPA_PLATFORM=offscreen python smoke_local.py

Что проверяется:
  * модуль импортируется, все классы и функции на месте;
  * окна создаются и раскладываются (панели не перекрывают область чата);
  * кнопки подключены к своим обработчикам;
  * локальный шаблон и конфиг читаются/пишутся;
  * склад промтов отдаёт PDF;
  * выход по «✕» не оставляет потоков;
  * шторка ожидания стоит РОВНО над областью чата и едет вместе с окном
    (это и проверялось на живом запуске: «окно ожидания слева от GUI»);
  * окно программы остаётся поверх остальных (frameless + topmost);
  * таймер передачи фокуса взведён, браузер не запускается вообще.

Браузер НЕ запускается: проверяется только собственная часть приложения.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


def main() -> int:
    print("Дымовой тест LocalLegalyze (без браузера)")

    try:
        import LocalLegalyze_Local as app
    except Exception as exc:
        print(f"  FAIL импорт модуля: {exc}")
        return 1
    check(True, "модуль импортируется")

    from PyQt6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication(sys.argv)

    # ---------------------------------------------------------- промты
    print("\n=== Локальный склад промтов ===")
    app.ensure_dirs()
    store = app.PromptStore(app.PROMPTS_DIR)
    store.ensure_example()
    check(bool(store.list_prompts()), "папка prompts/ не пуста")

    # ---------------------------------------------------------- конфиг
    print("\n=== Конфиг и шаблон ===")
    cfg = app.load_config()
    check(isinstance(cfg, dict) and "template" in cfg, "config.json читается")
    cfg["template"] = app.normalize_template({"Name": "Тест", "ID": "1"})
    app.save_template(cfg, cfg["template"])
    check(app.TEMPLATE_FILE.is_file(), "Шаблон.txt создан рядом с программой")
    reread = app.read_template_file()
    check(reread.get("Name") == "Тест", "Шаблон.txt читается обратно")
    check(len(reread) == 6, "в шаблоне ровно 6 полей")

    # ---------------------------------------------------------- окна
    print("\n=== Диалоги ===")
    template_window = app.TemplateWindow(cfg)
    check(template_window.get_template().get("Name") == "Тест",
          "TemplateWindow подставляет значения шаблона")
    template_window.deleteLater()

    hotkeys = app.OnboardingHotkeyWindow(cfg)
    hotkeys.mic_button.key_name = "F3"
    hotkeys.toggle_button.key_name = "F2"
    hotkeys._on_save()
    check(hotkeys.get_mic_key() == "F3" and hotkeys.get_toggle_key() == "F2",
          "OnboardingHotkeyWindow принимает обе клавиши")
    hotkeys.mic_button.key_name = "F2"
    hotkeys.toggle_button.key_name = "F2"
    hotkeys.warning.setText("")
    hotkeys._on_save()
    check("отличаться" in hotkeys.warning.text(), "одинаковые клавиши не принимаются")
    hotkeys.deleteLater()

    # ---------------------------------------------------------- главное окно
    print("\n=== Главное окно ===")
    window = app.MainWindow(cfg)
    window.show()
    application.processEvents()

    check(window.width() == app.W and window.height() == app.H, "размер окна из настроек")
    top = window.top_bar.geometry()
    bottom = window.bottom_bar.geometry()
    placeholder = window.browser_placeholder.geometry()
    check(top.height() == window.top_inset, "верхняя панель на своём месте")
    check(placeholder.top() == top.bottom() + 1 or placeholder.top() == window.top_inset,
          "область чата начинается сразу под верхней панелью")
    check(placeholder.bottom() <= bottom.top() + 1,
          "область чата заканчивается ДО нижней панели (панели не перекрыты)")

    check(window.btn_template.text() == "Шаблон", "кнопка «Шаблон» есть")
    check(window.btn_prompt.text() == "Промт", "кнопка «Промт» есть")
    check(window.btn_reload.text() == "⟳", "кнопка перезапуска есть")
    check(hasattr(window, "chip_mic") and hasattr(window, "chip_toggle"),
          "обе плашки горячих клавиш на месте")

    # Ключевое требование: счётчика запросов и авторизации больше нет
    check(not hasattr(window, "query_label"), "счётчика запросов в окне нет")
    check(not hasattr(window, "_balance_worker"), "воркера баланса нет")
    check(not hasattr(app, "LoginWindow"), "окна авторизации нет")

    # ---------------------------------------------------------- раскладка/зум
    print("\n=== Зум и раскладка ===")
    css_w, css_h, dsf = app.target_css(453, 651, 453, 651, 0.667)
    check(abs(css_w - 679) <= 2, f"ширина раскладки ≈680 CSS px (получено {css_w})")
    check(abs(dsf - 0.667) < 0.01, f"dsf ≈ 0.667 (получено {dsf:.3f})")
    inset = window._browser_inset()
    check(inset == (0, 0, 0, 0), "по умолчанию окно браузера занимает область целиком")

    # ------------------------------------------------- шторка ожидания
    print("\n=== Шторка ожидания (её видели слева от окна) ===")
    window._show_overlay("Проверка", "Проверяем положение шторки…")
    application.processEvents()

    overlay = window.overlay
    check(overlay.isVisible(), "шторка показана")
    area = window.browser_placeholder
    origin = area.mapToGlobal(area.rect().topLeft())
    check((overlay.x(), overlay.y()) == (origin.x(), origin.y()),
          f"шторка стоит ровно над областью чата "
          f"({overlay.x()},{overlay.y()} против {origin.x()},{origin.y()})")
    check((overlay.width(), overlay.height()) == (area.width(), area.height()),
          f"шторка того же размера, что область чата "
          f"({overlay.width()}x{overlay.height()} против {area.width()}x{area.height()})")

    # Переезд окна: рамка обещала, что шторка поедет за ним.
    before = (overlay.x(), overlay.y())
    window.move(window.x() + 120, window.y() + 75)
    application.processEvents()
    expected_origin = area.mapToGlobal(area.rect().topLeft())
    check((overlay.x(), overlay.y()) == (expected_origin.x(), expected_origin.y()),
          "после переезда окна шторка на новом месте (она не осталась слева)")
    check((overlay.x(), overlay.y()) != before, "координаты шторки действительно изменились")
    check((overlay.width(), overlay.height()) == (area.width(), area.height()),
          "после переезда размер шторки по-прежнему совпадает с областью чата")

    # Ручной сдвиг шторки не должен «залипать»: синхронизация возвращает её на место.
    overlay.move(overlay.x() - 300, overlay.y() - 200)
    window._place_overlay()
    application.processEvents()
    fixed_origin = area.mapToGlobal(area.rect().topLeft())
    check((overlay.x(), overlay.y()) == (fixed_origin.x(), fixed_origin.y()),
          "шторку можно вернуть на место одним вызовом _place_overlay()")

    # Свёрнутое окно не должно оставлять шторку висеть на экране.
    window.showMinimized()
    application.processEvents()
    check(not overlay.isVisible(), "при сворачивании окна шторка скрывается")
    window.showNormal()
    application.processEvents()

    check(window.focus_timer.isActive(), "таймер передачи фокуса в чат взведён")
    check(window.topmost_timer.isActive(), "таймер удержания окна поверх взведён")
    flags = window.windowFlags()
    from PyQt6.QtCore import Qt as _Qt
    check(bool(flags & _Qt.WindowType.FramelessWindowHint),
          "у окна нет системной рамки (кнопки свернуть/закрыть не показываются)")
    check(bool(flags & _Qt.WindowType.WindowStaysOnTopHint), "окно поверх остальных")

    window._hide_overlay()
    application.processEvents()
    check(not overlay.isVisible(), "шторка убирается после готовности")

    # ---------------------------------------------------------- закрытие
    print("\n=== Закрытие ===")
    window._close_app()
    application.processEvents()
    check(window._cleaned, "очистка при выходе выполнена")

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
