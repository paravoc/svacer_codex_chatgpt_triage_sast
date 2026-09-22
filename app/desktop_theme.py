"""Neutral desktop skin. Only ttk rendering changes; native widget behavior stays intact."""
from __future__ import annotations

import math
import sys
import struct
import tkinter as tk
import zlib
from tkinter import ttk

BG = "#0c0e11"
SURFACE = "#15191e"
SURFACE_2 = "#1b2229"
TEXT = "#edf0f3"
MUTED = "#9faab5"
BLUE = "#9bc5ed"
GREEN = "#8dd8ae"
YELLOW = "#e2c182"
RED = "#f0a1a1"
PURPLE = "#c8b4eb"
BORDER = "#2d353e"
SELECTION = "#304052"


def style_window(window: tk.Misc) -> None:
    """Dark title bar, with the native resize/minimize/close controls retained."""
    if sys.platform != "win32":
        return

    def mapped(event):
        if event.widget is not window:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(window.winfo_id())
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            dark = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        except (OSError, AttributeError, tk.TclError):
            pass  # Unsupported systems retain their native title bar.
    window.bind("<Map>", mapped, add="+")


def rounded_png(fill: str, outline: str, radius: int = 9) -> bytes:
    """Small antialiased nine-slice background, generated without extra dependencies."""
    # A wide center matters: Tk tiles nine-slice centers instead of scaling them.
    # Tiny 2–4px centers cause tens of thousands of draws for every large panel.
    size = 96
    colors = [tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) for color in (fill, outline)]
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG scanline filter
        for x in range(size):
            if radius <= x < size - radius or radius <= y < size - radius:
                rgb = colors[1 if x in (0, size - 1) or y in (0, size - 1) else 0]
                raw.extend((*rgb, 255))
                continue
            totals = [0, 0, 0]
            hits = 0
            for dy in (0.125, 0.375, 0.625, 0.875):
                for dx in (0.125, 0.375, 0.625, 0.875):
                    # Signed distance to a rounded rectangle.
                    qx = abs(x + dx - size / 2) - (size / 2 - radius)
                    qy = abs(y + dy - size / 2) - (size / 2 - radius)
                    distance = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius
                    if distance <= 0:
                        rgb = colors[1 if distance > -1 else 0]
                        hits += 1
                        for channel in range(3):
                            totals[channel] += rgb[channel]
            raw.extend([*(round(total / hits) if hits else 0 for total in totals), round(255 * hits / 16)])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    if "triage-desktop" in style.theme_names():
        style.theme_use("triage-desktop")
        return style
    style.theme_create("triage-desktop", parent="clam")
    style.theme_use("triage-desktop")
    style_window(root)
    # Tk images must live as long as the interpreter, not just this function.
    images: list[tk.PhotoImage] = []
    root._triage_theme_images = images

    def tile(fill: str, outline: str | None = None, radius: int = 9) -> tk.PhotoImage:
        image = tk.PhotoImage(master=root, data=rounded_png(fill, outline or fill, radius), format="png")
        images.append(image)
        return image

    root.option_add("*Text.highlightThickness", 0)
    root.option_add("*Text.borderWidth", 0)
    root.option_add("*Text.selectBackground", SELECTION)
    root.option_add("*Text.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.background", SURFACE_2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECTION)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    style.configure(".", background=BG, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, troughcolor=BG, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG, borderwidth=0)
    for prefix, color in (("Surface", SURFACE), ("Surface2", SURFACE_2)):
        style.configure(f"{prefix}.TFrame", background=color, borderwidth=0)
        name = f"{prefix}.rounded"
        style.element_create(name, "image", tile(color, BORDER, 12), border=13, padding=0,
                             width=30, height=30, sticky="nsew")
        style.layout(f"{prefix}.Card.TFrame", [(name, {"sticky": "nsew"})])
        style.configure(f"{prefix}.Card.TFrame", background=color)
        style.configure(f"{prefix}.TLabel", background=color, foreground=TEXT)
    style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
    style.configure("Section.TLabel", font=("Segoe UI Semibold", 11))
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("CardTitle.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Surface2Card.TLabel", background=SURFACE_2, foreground=MUTED, font=("Segoe UI", 9))

    buttons = {
        "TButton": ("#232b33", "#303c48", "#1b232b", TEXT, BORDER),
        "Neutral.TButton": ("#232b33", "#303c48", "#1b232b", TEXT, BORDER),
        "Accent.TButton": ("#a9c7e3", "#c6ddf0", "#88aecf", "#111a24", "#a9c7e3"),
        "Success.TButton": ("#1e352b", "#294837", "#192b22", GREEN, "#31523e"),
        "Warning.TButton": ("#373022", "#493c2b", "#2d271e", YELLOW, "#544634"),
        "Danger.TButton": ("#392626", "#4b3030", "#302020", RED, "#593939"),
    }
    for name, (fill, hover, pressed, foreground, edge) in buttons.items():
        element = name + ".rounded"
        style.element_create(element, "image", tile(fill, edge),
                             ("disabled", tile("#1b2025", BORDER)),
                             ("pressed", tile(pressed, edge)),
                             ("focus", tile(fill, BLUE)),
                             ("active", tile(hover, edge)), border=10, padding=0,
                             width=24, height=24, sticky="nsew")
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])
        style.configure(name, foreground=foreground, background=BG, padding=(12, 7),
                        font=("Segoe UI Semibold", 9), borderwidth=0, anchor="center")
        style.map(name, foreground=[("disabled", "#7d8994")])

    style.configure("Treeview", background=SURFACE, foreground=TEXT, fieldbackground=SURFACE,
                    rowheight=30, borderwidth=0, relief="flat")
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
    style.configure("Treeview.Heading", background=SURFACE_2, foreground=MUTED,
                    font=("Segoe UI Semibold", 9), padding=(8, 7), relief="flat", borderwidth=0)
    style.map("Treeview.Heading", background=[("active", "#26323e"), ("pressed", "#26323e")],
              relief=[("pressed", "flat")])
    style.map("Treeview", background=[("selected", SELECTION)], foreground=[("selected", TEXT)])

    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 5))
    style.element_create("Desktop.notebook", "image", tile(BG), border=0, padding=0,
                         width=1, height=1, sticky="nsew")
    style.layout("TNotebook", [("Desktop.notebook", {"sticky": "nsew"})])
    style.element_create("Desktop.tab", "image", tile(BG),
                         ("selected", tile(SURFACE_2, BORDER)), ("active", tile("#202932")),
                         border=10, padding=0, width=24, height=24, sticky="nsew")
    style.layout("TNotebook.Tab", [("Desktop.tab", {"sticky": "nsew", "children": [
        ("Notebook.padding", {"sticky": "nsew", "children": [
            ("Notebook.focus", {"sticky": "nsew", "children": [
                ("Notebook.label", {"sticky": "nsew"})]})]})]})])
    style.configure("TNotebook.Tab", foreground=MUTED, padding=(17, 8), font=("Segoe UI Semibold", 10))
    style.map("TNotebook.Tab", foreground=[("selected", TEXT), ("active", TEXT)])

    for name in ("TCombobox", "TSpinbox", "TEntry"):
        style.configure(name, fieldbackground=SURFACE_2, background=SURFACE_2, foreground=TEXT,
                        insertcolor=TEXT, arrowcolor=MUTED, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, padding=6, selectbackground=SELECTION, selectforeground=TEXT)
        style.map(name, fieldbackground=[("disabled", SURFACE), ("readonly", SURFACE_2)],
                  foreground=[("disabled", "#7d8994"), ("readonly", TEXT)],
                  background=[("active", "#26323e"), ("readonly", SURFACE_2)],
                  bordercolor=[("focus", BLUE)], arrowcolor=[("disabled", "#7d8994")])
    style.configure("TRadiobutton", background=SURFACE, foreground=TEXT, indicatorbackground=SURFACE_2,
                    indicatorforeground=TEXT, font=("Segoe UI", 10), padding=(0, 3))
    style.map("TRadiobutton", background=[("active", SURFACE)],
              indicatorbackground=[("selected", "#d8e0e8"), ("active", "#304052")],
              foreground=[("disabled", "#7d8994"), ("active", TEXT)])
    style.configure("TPanedwindow", background=BG)
    style.configure("Sash", sashthickness=7, background=BG, gripcount=0)
    style.configure("Horizontal.TProgressbar", troughcolor=SURFACE_2, background=BLUE,
                    borderwidth=0, lightcolor=BLUE, darkcolor=BLUE, thickness=5)
    style.element_create("Desktop.progress.trough", "image", tile(SURFACE_2, radius=2),
                         border=3, padding=0, width=10, height=6, sticky="nsew")
    style.element_create("Desktop.progress.pbar", "image", tile(BLUE, radius=2),
                         border=3, padding=0, width=10, height=6, sticky="nsew")
    style.layout("Horizontal.TProgressbar", [("Desktop.progress.trough", {"sticky": "nsew", "children": [
        ("Desktop.progress.pbar", {"side": "left", "sticky": "ns"})]})])
    for direction in ("Vertical", "Horizontal"):
        name = direction + ".TScrollbar"
        style.configure(name, background="#35414c", troughcolor=SURFACE, borderwidth=0,
                        arrowcolor=MUTED, arrowsize=11, width=11, relief="flat")
        style.map(name, background=[("active", "#4c5c6b"), ("pressed", "#65798b")],
                  lightcolor=[("active", "#4c5c6b")], darkcolor=[("active", "#4c5c6b")])
    return style
