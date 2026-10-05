"""Look and feel for the Claude-Teams-Bot desktop app: colour tokens, fonts, ttk styles
and the status pill. Presentation only - no meeting logic lives here."""
import os
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

try:
    from PIL import Image, ImageDraw, ImageTk
    LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
except Exception:                                    # pill falls back to plain canvas shapes
    Image = ImageDraw = ImageTk = LANCZOS = None

# Colour tokens. Text colours keep at least 4.5:1 contrast on the surface they sit on.
BG = "#f3f4f6"               # window
SURFACE = "#ffffff"          # header, cards, activity feed
BORDER = "#e2e5e9"           # card outlines, dividers
BORDER_STRONG = "#c6ccd4"    # inputs and buttons
TEXT = "#1b1f24"
MUTED = "#5b6470"            # field labels, helper text (6.0:1 on white)
SUBTLE = "#6b7480"           # timestamps, technical details (4.7:1 on white)
DISABLED = "#9aa1ab"
ACCENT = "#1f4e9c"           # brand blue from the app icon (8.0:1 on white)
ACCENT_HOVER = "#1a4387"
ACCENT_PRESSED = "#153670"
ACCENT_SOFT = "#eaf0fa"      # Claude answer cards, selected segment
SUCCESS = "#107c41"
WARN_BG, WARN_FG, WARN_MARK = "#fff4e5", "#8a5300", "#e8890c"
DANGER = "#b42318"
DANGER_SOFT = "#fdecea"
VIEW_BG = "#1e2228"          # Meeting view backdrop

# Status pill tones: (fill, text, dot)
TONES = {
    "idle": ("#eef0f3", "#3d4550", "#8a929c"),
    "busy": (ACCENT_SOFT, ACCENT, ACCENT),
    "live": ("#e6f4ea", "#0f6b37", "#16a34a"),
    "action": (WARN_BG, WARN_FG, WARN_MARK),
    "error": (DANGER_SOFT, "#a4161a", "#d92d20"),
    "done": ("#eef0f3", "#3d4550", "#5b6470"),
}


def enable_dpi_awareness():
    """Draw at the display's real resolution. Without this, Windows bitmap-stretches the
    window on scaled displays (125%, 150%) and every letter is blurry. Call before tk.Tk()."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _first(available, *names):
    return next((n for n in names if n in available), None)


class Fonts:
    """Segoe UI Variable on Windows 11, Segoe UI on Windows 10. Sizes are points, so Tk
    scales them with the display DPI."""

    def __init__(self, root):
        fams = set(tkfont.families(root))
        ui = _first(fams, "Segoe UI Variable Text", "Segoe UI") or "Helvetica"
        semi = _first(fams, "Segoe UI Variable Text Semibold", "Segoe UI Semibold")
        title = _first(fams, "Segoe UI Variable Display Semib", "Segoe UI Semibold") or semi
        mono = _first(fams, "Cascadia Mono", "Consolas") or "Courier"
        icons = _first(fams, "Segoe Fluent Icons", "Segoe MDL2 Assets")     # Windows 11, Windows 10

        def font(size, family=ui, strong=False):
            if strong:                               # semibold face, else bold weight
                return tkfont.Font(root, family=semi or ui, size=size,
                                   weight="normal" if semi else "bold")
            return tkfont.Font(root, family=family, size=size)

        self.family = ui
        self.body = font(10)
        self.small = font(9)
        self.strong = font(10, strong=True)
        self.strong_small = font(9, strong=True)
        self.section = font(11, strong=True)
        self.title = (tkfont.Font(root, family=title, size=13) if title
                      else tkfont.Font(root, family=ui, size=13, weight="bold"))
        self.mono = font(9, family=mono)
        self.tiny = font(2)
        # The settings gear: Windows' icon font, or the Unicode gear where it is missing.
        self.icon = font(12, family=icons) if icons else font(12)
        self.gear = "\ue713" if icons else "\u2699"


class Theme:
    """Fonts and ttk styles for one Tk root. px() turns design pixels (at 96 DPI) into screen
    pixels, so spacing grows with the fonts on high-DPI displays."""

    def __init__(self, root):
        self.root = root
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.f = Fonts(root)
        root.configure(bg=BG)
        # Default fonts for entries and plain tk widgets. (Not option_add("*Font"): that sets
        # every ttk label's own -font and silently overrides the styles' fonts.)
        for name in ("TkDefaultFont", "TkTextFont"):
            tkfont.nametofont(name, root).configure(family=self.f.family, size=10)
        self._styles()

    def px(self, v):
        return int(round(v * self.scale))

    def _styles(self):
        st, px, f = ttk.Style(self.root), self.px, self.f
        st.theme_use("clam")                         # flat, fully colourable base theme
        st.configure(".", background=BG, foreground=TEXT, font=f.body, bordercolor=BORDER_STRONG,
                     lightcolor=BG, darkcolor=BG, troughcolor=BG, focuscolor=ACCENT,
                     selectbackground=ACCENT_SOFT, selectforeground=TEXT, insertcolor=TEXT)
        st.configure("Card.TFrame", background=SURFACE)
        st.configure("Header.TFrame", background=SURFACE)
        labels = {
            "Card.TLabel": dict(background=SURFACE),
            "Field.Card.TLabel": dict(foreground=MUTED),
            "Help.Card.TLabel": dict(foreground=MUTED, font=f.small),
            "Error.Card.TLabel": dict(foreground=DANGER, font=f.small),
            "Section.Card.TLabel": dict(font=f.section),
            "Value.Card.TLabel": dict(font=f.strong),
            "Header.TLabel": dict(background=SURFACE),
            "Title.Header.TLabel": dict(font=f.title),
            "Hint.TLabel": dict(foreground=MUTED),
            "Banner.TLabel": dict(background=WARN_BG, foreground=TEXT),
            "Title.Banner.TLabel": dict(foreground=WARN_FG, font=f.strong),
        }
        for name, opts in labels.items():
            st.configure(name, **opts)

        def button(style, bg, fg, border, hover, pressed, off_bg, off_fg, off_border, **kw):
            st.configure(style, background=bg, foreground=fg, bordercolor=border,
                         lightcolor=bg, darkcolor=bg, **kw)
            fill = [("disabled", off_bg), ("pressed", pressed), ("active", hover)]
            st.map(style, background=fill, lightcolor=fill, darkcolor=fill,
                   foreground=[("disabled", off_fg)], bordercolor=[("disabled", off_border)])

        pad = (px(16), px(6))
        button("TButton", SURFACE, TEXT, BORDER_STRONG, "#f2f4f7", "#e6e9ee",
               BG, DISABLED, BORDER, padding=pad, focuscolor=ACCENT)
        button("Accent.TButton", ACCENT, "#ffffff", ACCENT, ACCENT_HOVER, ACCENT_PRESSED,
               "#b9c7e1", "#f4f6fa", "#b9c7e1", padding=(px(20), px(6)), font=f.strong,
               focuscolor="#ffffff")
        button("Danger.TButton", SURFACE, DANGER, "#e7b9b4", DANGER_SOFT, "#f9d7d2",
               BG, DISABLED, BORDER, padding=(px(20), px(6)), font=f.strong, focuscolor=DANGER)
        st.configure("Key.TButton", padding=(px(12), px(6)), width=-5)   # clam's default is -11
        button("Icon.TButton", SURFACE, MUTED, SURFACE, "#f2f4f7", "#e6e9ee",     # header gear
               SURFACE, DISABLED, SURFACE, padding=(px(8), px(6)), font=f.icon, width=0,
               focuscolor=ACCENT)
        st.map("Icon.TButton", foreground=[("disabled", DISABLED), ("active", TEXT)])

        st.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT, bordercolor=BORDER_STRONG,
                     lightcolor=SURFACE, darkcolor=SURFACE, padding=(px(8), px(6)))
        st.map("TEntry", bordercolor=[("disabled", BORDER), ("focus", ACCENT), ("hover", "#9aa3ae")],
               lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
               fieldbackground=[("disabled", BG)], foreground=[("disabled", MUTED)])
        st.configure("Invalid.TEntry", bordercolor=DANGER, lightcolor=DANGER_SOFT, darkcolor=DANGER_SOFT)
        st.map("Invalid.TEntry", bordercolor=[("focus", DANGER)], lightcolor=[("focus", DANGER)],
               darkcolor=[("focus", DANGER)])

        st.configure("Card.TCheckbutton", background=SURFACE, foreground=TEXT, focuscolor=ACCENT,
                     indicatorbackground=SURFACE, indicatorforeground=SURFACE,
                     upperbordercolor=BORDER_STRONG, lowerbordercolor=BORDER_STRONG,
                     indicatorsize=px(14), indicatormargin=(0, 0, px(8), 0))
        st.map("Card.TCheckbutton", background=[("active", SURFACE)],
               foreground=[("disabled", DISABLED)],
               indicatorbackground=[("disabled", BG), ("selected", ACCENT)],
               indicatorforeground=[("selected", "#ffffff")],
               upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        if Image is not None:                        # rounded box with a real tick, not clam's X
            try:
                off, on, off_dis, on_dis = self._check_images = self._checkbox_images()
                st.element_create("Modern.Checkbutton.indicator", "image", off,
                                  ("disabled", "selected", on_dis), ("disabled", off_dis),
                                  ("selected", on))
                st.layout("Card.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("Modern.Checkbutton.indicator", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})]})]})])
            except tk.TclError:                      # element already exists in this interpreter
                pass

        # Segmented control (speech mode): borderless radio buttons on a strip whose colour
        # shows through 1px gaps as the outline and dividers (see app.SettingsDialog).
        st.layout("Segment.TRadiobutton", [("Button.border", {"sticky": "nswe", "children": [
            ("Radiobutton.focus", {"sticky": "nswe", "children": [
                ("Radiobutton.padding", {"sticky": "nswe", "children": [
                    ("Radiobutton.label", {"sticky": "nswe"})]})]})]})])
        st.configure("Segment.TRadiobutton", background=SURFACE, foreground=TEXT, relief="flat",
                     bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE, borderwidth=0,
                     padding=(px(16), px(6)), anchor="center")
        seg = [("disabled", BG), ("selected", ACCENT_SOFT), ("active", "#f2f4f7")]
        st.map("Segment.TRadiobutton", background=seg, lightcolor=seg, darkcolor=seg, bordercolor=seg,
               foreground=[("disabled", DISABLED), ("selected", ACCENT)])

        # Header tabs: radio buttons with no indicator; the app draws the underline.
        st.layout("Tab.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Radiobutton.focus", {"sticky": "nswe", "children": [
                ("Radiobutton.label", {"sticky": "nswe"})]})]})])
        st.configure("Tab.TRadiobutton", background=SURFACE, foreground=MUTED, font=f.strong,
                     padding=(px(10), px(14), px(10), px(11)))
        st.map("Tab.TRadiobutton", background=[("active", SURFACE)],
               foreground=[("selected", TEXT), ("active", TEXT)])

        # The page switcher is a notebook whose own tabs are hidden (the header shows them).
        st.layout("Tabless.TNotebook.Tab", [])
        st.configure("Tabless.TNotebook", background=BG, borderwidth=0, padding=0, tabmargins=0,
                     bordercolor=BG, lightcolor=BG, darkcolor=BG)

        thin = [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
        thumb = [("pressed", "#9aa3ae"), ("active", "#b3bac4")]
        for style, colour in (("Thin.Vertical.TScrollbar", "#cdd2d9"),
                              ("Idle.Thin.Vertical.TScrollbar", SURFACE)):    # nothing to scroll
            st.layout(style, thin)
            st.configure(style, troughcolor=SURFACE, background=colour, bordercolor=SURFACE,
                         lightcolor=colour, darkcolor=colour, gripcount=0, arrowsize=px(7))
        st.map("Thin.Vertical.TScrollbar", background=thumb, lightcolor=thumb, darkcolor=thumb)

    def _checkbox_images(self):
        """Checkbox indicator images (off, on, disabled off, disabled on). Each includes
        the gap before the label, since an image element has no margin of its own."""
        box, gap, s = self.px(16), self.px(8), 4

        def draw(fill, edge, tick):
            im = Image.new("RGBA", ((box + gap) * s, box * s), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle((s // 2, s // 2, box * s - s // 2 - 1, box * s - s // 2 - 1),
                                radius=self.px(3) * s, fill=fill, outline=edge, width=s)
            if tick:
                pts = [(0.27, 0.53), (0.43, 0.69), (0.74, 0.34)]
                d.line([(x * box * s, y * box * s) for x, y in pts], fill=tick,
                       width=max(2 * s, int(1.7 * self.scale * s)), joint="curve")
            return ImageTk.PhotoImage(im.resize((box + gap, box), LANCZOS), master=self.root)

        return (draw(SURFACE, BORDER_STRONG, None), draw(ACCENT, ACCENT, "#ffffff"),
                draw(BG, BORDER, None), draw("#b9c7e1", "#b9c7e1", "#ffffff"))


class StatusPill(tk.Canvas):
    """Rounded status badge (coloured dot + text). The shape is drawn with Pillow so the
    ends are anti-aliased; without Pillow it falls back to plain canvas shapes."""

    def __init__(self, parent, theme, bg=SURFACE):
        self.t, self.h = theme, theme.px(30)
        super().__init__(parent, height=self.h, width=1, bg=bg, highlightthickness=0, bd=0)
        self.text, self.tone, self._img = None, None, None

    def set(self, text, tone):
        if (text, tone) != (self.text, self.tone):
            self.text, self.tone = text, tone
            self._draw()

    def _draw(self):
        px, font = self.t.px, self.t.f.strong
        fill, fg, dot = TONES.get(self.tone, TONES["idle"])
        pad, d, gap, h = px(12), px(8), px(8), self.h
        w = pad + d + gap + font.measure(self.text) + pad
        self.delete("all")
        self.configure(width=w)
        if Image is not None:
            s = 4                                    # supersample, then shrink = smooth edges
            im = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            dr.rounded_rectangle((0, 0, w * s - 1, h * s - 1), radius=h * s // 2, fill=fill)
            cy = h * s // 2
            dr.ellipse((pad * s, cy - d * s // 2, (pad + d) * s, cy + d * s // 2), fill=dot)
            self._img = ImageTk.PhotoImage(im.resize((w, h), LANCZOS), master=self)
            self.create_image(0, 0, anchor="nw", image=self._img)
        else:
            r = h // 2
            self.create_oval(0, 0, h, h, fill=fill, outline=fill)
            self.create_oval(w - h, 0, w, h, fill=fill, outline=fill)
            self.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
            self.create_oval(pad, r - d / 2, pad + d, r + d / 2, fill=dot, outline=dot)
        self.create_text(pad + d + gap, h / 2, text=self.text, anchor="w", fill=fg, font=font)


def icon_image(path, size):
    """The app icon as a PhotoImage for the header, or None without Pillow."""
    if Image is None or not os.path.exists(path):
        return None
    try:
        im = Image.open(path)
        sizes = im.info.get("sizes")
        if sizes:
            im.size = max(sizes)                     # .ico: use the largest frame
        return ImageTk.PhotoImage(im.convert("RGBA").resize((size, size), LANCZOS))
    except Exception:
        return None
