"""Оформление K.O. Gateway для Windows — палитра сайта и Telegram Mini App.

Цвета и радиусы взяты из static/site.css и static/style.css, чтобы
десктопное приложение выглядело так же, как веб-версия.
"""

import tkinter as tk

# ── Палитра (совпадает с site.css / style.css) ──────────────────────────────
BG = "#0a0a0c"          # фон окна
CARD = "#141418"        # фон карточек
CARD_HI = "#1b1b21"     # приподнятая карточка (hover)
TEXT = "#f2f2f4"        # основной текст
MUTED = "#9b9ba6"       # второстепенный текст
ACCENT = "#e01e2b"      # брендовый красный (кнопки, акценты)
ACCENT_HOVER = "#ff2d3c"
ACCENT_DARK = "#7f0d16"  # тёмный оттенок акцента (градиент)
LINK = "#ff3b47"        # ссылки
OK = "#22c55e"          # успех / активна
ERR = "#ff4d5a"         # ошибка
WARN = "#f59e0b"        # предупреждение
BORDER = "#26262e"      # границы карточек
BORDER_SOFT = "#1f1f26"
INPUT_BG = "#101014"    # поля ввода
NAV_BG = "#101014"      # нижняя навигация
RADIUS = 16

FONT = "Segoe UI"

# ── Готовые виджеты ─────────────────────────────────────────────────────────


def card(parent, **kw):
    """Карточка в стиле сайта: тёмный фон + тонкая рамка."""
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                    highlightthickness=1, bd=0, **kw)


def brand_mark(parent, text="K.O"):
    """Квадратный логотип с градиентом, как .brand-mark в Mini App."""
    holder = tk.Frame(parent, bg=ACCENT, width=44, height=44)
    holder.pack_propagate(False)
    tk.Label(holder, text=text, bg=ACCENT, fg="#ffffff",
             font=(FONT, 14, "bold")).pack(expand=True)
    return holder


def accent_button(parent, text, command, **kw):
    """Кнопка акцентного цвета (--btn)."""
    base_bg = kw.pop("bg", ACCENT)
    opts = dict(bg=base_bg, fg="#ffffff", activebackground=ACCENT_HOVER,
                activeforeground="#ffffff", font=(FONT, 10, "bold"),
                relief="flat", bd=0, cursor="hand2", highlightthickness=0)
    opts.update(kw)
    btn = tk.Button(parent, text=text, command=command, **opts)
    btn.bind("<Enter>", lambda e, b=btn: b.config(bg=ACCENT_HOVER))
    btn.bind("<Leave>", lambda e, b=btn: b.config(bg=base_bg))
    return btn


def ghost_button(parent, text, command, **kw):
    """Вторичная кнопка: тёмный фон + рамка."""
    base_bg = kw.pop("bg", CARD)
    opts = dict(bg=base_bg, fg=TEXT, activebackground=CARD_HI,
                activeforeground=TEXT, font=(FONT, 10),
                relief="flat", bd=0, cursor="hand2", highlightthickness=1,
                highlightbackground=BORDER, highlightcolor=BORDER)
    opts.update(kw)
    btn = tk.Button(parent, text=text, command=command, **opts)
    btn.bind("<Enter>", lambda e, b=btn: b.config(bg=CARD_HI))
    btn.bind("<Leave>", lambda e, b=btn: b.config(bg=base_bg))
    return btn


def pill(parent, text, color=OK, **kw):
    """Небольшой «пилюля»-бейдж (как .badge в Mini App)."""
    return tk.Label(parent, text=text, bg=BG, fg=color,
                    font=(FONT, 9, "bold"), padx=10, pady=3, **kw)


def title_label(parent, text, size=15, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", CARD), fg=TEXT,
                    font=(FONT, size, "bold"), anchor="w", **kw)


def hint_label(parent, text, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", CARD), fg=MUTED,
                    font=(FONT, 9), anchor="w", **kw)


def kv_row(parent, key, value_var, key_width=26, value_color=TEXT):
    """Строка «параметр: значение» как .row в Mini App."""
    tk.Label(parent, text=key, bg=CARD, fg=MUTED, font=(FONT, 10),
             width=key_width, anchor="w").pack(side="left")
    val = tk.Label(parent, textvariable=value_var, bg=CARD, fg=value_color,
                   font=(FONT, 10, "bold"), anchor="w")
    val.pack(side="left", fill="x", expand=True)
    return val


def dark_entry(parent, textvariable=None, **kw):
    """Поле ввода в тёмной теме."""
    opts = dict(bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                relief="flat", bd=0, highlightthickness=1,
                highlightbackground=BORDER, highlightcolor=ACCENT,
                font=(FONT, 10))
    opts.update(kw)
    return tk.Entry(parent, textvariable=textvariable, **opts)


def setup_ttk_styles(root):
    """Настраивает ttk-стили (combobox, notebook, scrollbar) под тёмную тему.

    ttk-виджеты не поддаются config() — их цвета берутся из темы,
    поэтому переключаем тему на clam и задаём цвета через Style.
    """
    try:
        from tkinter import ttk
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox",
                        fieldbackground=INPUT_BG, background=INPUT_BG,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        lightcolor=INPUT_BG, darkcolor=INPUT_BG,
                        selectbackground=ACCENT, selectforeground="#ffffff",
                        borderwidth=0, relief="flat", padding=(6, 5))
        style.map("TCombobox",
                  fieldbackground=[("readonly", INPUT_BG), ("disabled", CARD)],
                  foreground=[("readonly", TEXT), ("disabled", MUTED)],
                  bordercolor=[("focus", ACCENT), ("!focus", BORDER)],
                  arrowcolor=[("!disabled", TEXT)],
                  selectbackground=[("readonly", ACCENT)],
                  selectforeground=[("readonly", "#ffffff")])
        style.configure("TNotebook", background=BG, borderwidth=0,
                        bordercolor=BG, lightcolor=BG, darkcolor=BG,
                        troughcolor=BG, tabmargins=(0, 0, 0, 0), focuscolor=BG)
        style.map("TNotebook",
                  background=[("selected", BG), ("!selected", BG)],
                  bordercolor=[("selected", BG), ("!selected", BG)],
                  lightcolor=[("selected", BG), ("!selected", BG)],
                  darkcolor=[("selected", BG), ("!selected", BG)],
                  troughcolor=[("selected", BG), ("!selected", BG)])
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED,
                        borderwidth=0, bordercolor=BORDER, lightcolor=CARD,
                        darkcolor=CARD, padding=(18, 9), focuscolor=CARD)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT), ("!selected", CARD)],
                  foreground=[("selected", "#ffffff"), ("!selected", MUTED)],
                  bordercolor=[("selected", ACCENT), ("!selected", BORDER)],
                  lightcolor=[("selected", ACCENT), ("!selected", CARD)],
                  darkcolor=[("selected", ACCENT), ("!selected", CARD)])
        # рамка вокруг содержимого вкладки
        style.layout("TNotebook.Tab", [
            ("Notebook.tab", {"sticky": "nswe", "children": [
                ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                    ("Notebook.label", {"side": "top", "sticky": ""}),
                ]}),
            ]}),
        ])
        style.configure("Vertical.TScrollbar", background=CARD_HI,
                        troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                        borderwidth=0, relief="flat", width=12)
        style.map("Vertical.TScrollbar",
                  background=[("active", ACCENT), ("!active", CARD_HI)],
                  arrowcolor=[("active", "#ffffff"), ("!active", MUTED)])
    except Exception:
        pass
    try:
        root.option_add("*TCombobox*Listbox.background", INPUT_BG)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
        root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    except Exception:
        pass


def style_notebook(nb):
    """Тёмное оформление вкладок ttk.Notebook."""
    try:
        setup_ttk_styles(nb)
    except Exception:
        pass
    return nb


def style_combobox(combo):
    """Тёмное оформление выпадающих списков."""
    try:
        setup_ttk_styles(combo.root)
        combo.configure(background=INPUT_BG, foreground=TEXT,
                        fieldbackground=INPUT_BG, borderwidth=0,
                        arrowcolor=TEXT, lightcolor=INPUT_BG,
                        darkcolor=INPUT_BG, selectbackground=ACCENT,
                        selectforeground="#ffffff", relief="flat")
    except Exception:
        pass
    return combo


def style_checkbox(cb, bg=CARD):
    cb.configure(bg=bg, fg=TEXT, activebackground=bg,
                 selectcolor=INPUT_BG, highlightthickness=0, bd=0,
                 font=(FONT, 10))
    return cb


def set_dark_titlebar(root):
    """Тёмный заголовок окна в Windows 10/11 (DWM)."""
    try:
        import ctypes
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        val = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def apply_root(root):
    """Общие настройки окна: фон, тёмный заголовок, шрифт по умолчанию."""
    root.configure(bg=BG)
    set_dark_titlebar(root)
    try:
        root.option_add("*Font", (FONT, 10))
        root.option_add("*TCombobox*Listbox.background", INPUT_BG)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    except Exception:
        pass

