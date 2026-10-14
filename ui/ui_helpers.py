import math
import numpy as np
import tkinter as tk
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


def plot_samples(time, values, bucket_count=500):
    """Reduce rendering work while retaining extrema and jumps in each bucket."""
    if len(time) <= 4 * bucket_count:
        return time, values
    indices = {0, len(time) - 1}
    for bucket in np.array_split(np.arange(len(time)), bucket_count):
        indices.update((int(bucket[0]), int(bucket[-1]),
                        int(bucket[np.argmin(values[bucket])]),
                        int(bucket[np.argmax(values[bucket])])))
    selected = np.array(sorted(indices))
    return time[selected], values[selected]


ICON_PATTERNS = {
    "disturbances": (
        "................",
        "................",
        "..........##....",
        ".........#..#...",
        ".........#..#...",
        "........#....#..",
        "..#.....#....#..",
        "..#....#......#.",
        ".#.#....#......#",
        ".#..#..#........",
        "#...#..#........",
        "#....##.........",
        "................",
        "................",
        "................",
        "................",
    ),
    "dynamics": (
        ".....######.....",
        "...##......##...",
        "..#..........#..",
        ".#.....##.....#.",
        ".#.....##.....#.",
        "#......##......#",
        "#......##......#",
        "#......#####...#",
        "#..........#...#",
        "#..............#",
        ".#............#.",
        ".#............#.",
        "..#..........#..",
        "...##......##...",
        ".....######.....",
        "................",
    ),
    "results": (
        "................",
        "...........###..",
        "...........###..",
        "...........###..",
        ".......###.###..",
        ".......###.###..",
        ".......###.###..",
        "...###.###.###..",
        "...###.###.###..",
        "...###.###.###..",
        "...###.###.###..",
        ".##############.",
        ".##############.",
        "................",
        "................",
        "................",
    ),
    "controller": (
        "................",
        "...##...........",
        ".######.........",
        "...##...........",
        "...##...........",
        "...##......##...",
        "..........######",
        "............##..",
        "............##..",
        "......##....##..",
        "....######......",
        "......##........",
        "......##........",
        "......##........",
        "................",
        "................",
    ),
    "scenarios": (
        "................",
        "....########....",
        "....#......#....",
        ".##.#......#....",
        ".##.#.####.#....",
        "....#......#....",
        "....#.####.#....",
        ".##.#......#....",
        ".##.#.####.#....",
        "....#......#....",
        "....#.####.#....",
        "....#......#....",
        "....########....",
        "................",
        "................",
        "................",
    ),
    "comparison": (
        "................",
        "............##..",
        "...........#..#.",
        "..........#....#",
        "..##.....#......",
        ".#..#....#......",
        "#....#..#.......",
        "......##........",
        ".....#..#.......",
        "....#....#......",
        "...#......#.....",
        "..#........#....",
        ".#..........#...",
        "................",
        "................",
        "................",
    ),
    "sensitivity": (
        "................",
        "................",
        "...#............",
        "...#............",
        "...#......#.....",
        "...#.....#.#....",
        "...#....#...#...",
        "...#...#.....#..",
        "...#..#.......#.",
        "...#.#.........#",
        "...##...........",
        ".###............",
        "................",
        "................",
        "................",
        "................",
    ),
    "tuning_map": (
        "................",
        "..###.###.###...",
        "..#.#.#.#.#.#...",
        "..###.###.###...",
        "................",
        "..###.###.###...",
        "..#.#.#.#.#.#...",
        "..###.###.###...",
        "................",
        "..###.###.###...",
        "..#.#.#.#.#.#...",
        "..###.###.###...",
        "................",
        "................",
        "................",
        "................",
    ),
    "export": (
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        "..##...##...##..",
        "...##..##..##...",
        "....##.##.##....",
        ".....######.....",
        "......####......",
        ".......##.......",
        "................",
        ".##############.",
        ".#............#.",
        ".##############.",
        "................",
    ),
}


DISTURBANCE_HELP = {
    "Ступенчатое": (
        "Ступенчатое воздействие",
        "В момент t₀ сигнал скачком достигает заданной величины и сохраняется до конца моделирования.",
        "step",
    ),
    "Импульсное": (
        "Импульсное воздействие",
        "После t₀ возникает кратковременный полусинусоидальный импульс заданной длительности.",
        "impulse",
    ),
    "Временное прямоугольное": (
        "Временное прямоугольное воздействие",
        "Сигнал включается в t₀, остаётся постоянным заданное время, затем возвращается к нулю.",
        "rectangle",
    ),
    "Плавно нарастающее": (
        "Плавно нарастающее воздействие",
        "Начиная с t₀ сигнал линейно растёт и достигает полной величины за указанное время.",
        "ramp",
    ),
}


FORMULAS = {
    "P": r"$v(t)=K\,e(t)$",
    "PI": r"$v(t)=K\left(e(t)+\frac{1}{T_i}\int_0^t e(\tau)\,d\tau\right)$",
    "PD": r"$v(t)=K\left(e(t)-T_d\frac{dy(t)}{dt}\right)$",
    "PID": r"$v(t)=K\left(e(t)+\frac{1}{T_i}\int_0^t e(\tau)\,d\tau-T_d\frac{dy(t)}{dt}\right)$",
}


def create_icon(master, name, color="#E5ECF5"):
    pattern = ICON_PATTERNS[name]
    image = tk.PhotoImage(master=master, width=len(pattern[0]), height=len(pattern))
    for y, row in enumerate(pattern):
        for x, pixel in enumerate(row):
            if pixel == "#":
                image.put(color, (x, y))
    return image


class DisturbanceTooltip:
    def __init__(self, widget, value_getter, delay=450):
        self.widget = widget
        self.value_getter = value_getter
        self.delay = delay
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<FocusIn>", self._schedule, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        self._after_id = None
        help_content = DISTURBANCE_HELP.get(self.value_getter())
        if help_content is None or self._window is not None:
            return
        title, description, shape = help_content
        window = tk.Toplevel(self.widget)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        frame = ttk.Frame(window, style="Card.TFrame", padding=(12, 10))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        canvas = tk.Canvas(
            frame,
            width=190,
            height=52,
            background="#FFFFFF",
            highlightthickness=0,
        )
        canvas.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        self._draw_profile(canvas, shape)
        ttk.Label(
            frame,
            text=description,
            style="Body.TLabel",
            wraplength=260,
            justify="left",
        ).grid(row=2, column=0, sticky="w")
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window.geometry(f"+{x}+{y}")
        self._window = window

    def _hide(self, _event=None):
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    @staticmethod
    def _draw_profile(canvas, shape):
        width = 190
        baseline = 42
        top = 10
        start = 52
        canvas.create_line(8, baseline, width - 8, baseline, fill="#98A2B3", width=1)
        canvas.create_line(start, 5, start, 47, fill="#D0D5DD", dash=(3, 3))
        if shape == "step":
            points = (8, baseline, start, baseline, start, top, width - 8, top)
        elif shape == "rectangle":
            end = 135
            points = (8, baseline, start, baseline, start, top, end, top, end, baseline, width - 8, baseline)
        elif shape == "ramp":
            end = 135
            points = (8, baseline, start, baseline, end, top, width - 8, top)
        else:
            points = [(8, baseline), (start, baseline)]
            for index in range(31):
                fraction = index / 30.0
                points.append(
                    (
                        start + fraction * 90,
                        baseline - math.sin(math.pi * fraction) * (baseline - top),
                    )
                )
            points.append((width - 8, baseline))
            points = tuple(coordinate for point in points for coordinate in point)
        canvas.create_line(*points, fill="#2563EB", width=2.5)
        canvas.create_text(start + 2, 48, text="t₀", fill="#667085", anchor="nw")


class TextTooltip:
    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<FocusIn>", self._schedule, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        self._after_id = None
        if self._window is not None:
            return
        window = tk.Toplevel(self.widget)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        frame = ttk.Frame(window, style="Card.TFrame", padding=(10, 8))
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=self.text,
            style="Body.TLabel",
            wraplength=280,
            justify="left",
        ).pack()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        window.geometry(f"+{x}+{y}")
        self._window = window

    def _hide(self, _event=None):
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None


class FormulaPanel(ttk.Frame):
    def __init__(self, parent, background="#FFFFFF"):
        super().__init__(parent, style="CardBody.TFrame")
        self.figure = Figure(
            figsize=(3.25, 1.72),
            dpi=110,
            facecolor=background,
            constrained_layout=True,
        )
        self.axis = self.figure.add_subplot(111)
        self.axis.set_axis_off()
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        # Matplotlib stores each resized width as the widget request; a fixed
        # frame height without propagation lets the panel shrink again.
        self.configure(height=self.canvas.get_tk_widget().winfo_reqheight())
        self.pack_propagate(False)

    def set_controller_type(self, controller_type):
        self.axis.clear()
        self.axis.set_axis_off()
        self.axis.text(
            0.02,
            0.84,
            r"$T\,\frac{dy(t)}{dt}+y(t)=y_{\mathrm{уст}}(t)$",
            fontsize=13,
            color="#1F2937",
            va="center",
        )
        formula_size = 10.5 if controller_type == "PID" else 12.5
        self.axis.text(
            0.02,
            0.57,
            FORMULAS[controller_type],
            fontsize=formula_size,
            color="#1F2937",
            va="center",
        )
        self.axis.text(
            0.02,
            0.29,
            r"$\lambda=\max(0.5T,L),\qquad K=\frac{T}{|k_\eta|(\lambda+L)}$",
            fontsize=10.5,
            color="#1F2937",
            va="center",
        )
        self.axis.text(
            0.02,
            0.08,
            r"$T_i=\min\!\left(T,4(\lambda+L)\right),\qquad T_d=\frac{L}{3}$",
            fontsize=10.5,
            color="#1F2937",
            va="center",
        )
        self.canvas.draw_idle()


def relax_field_widths(container, minimum=10):
    """Lower the minimum width of stretching fields so narrow panels do not clip them.

    Entries and lists that fill a weighted grid column keep their full width when
    space allows; their character width only sets how far the panel can shrink.
    """
    for widget in container.winfo_children():
        relax_field_widths(widget, minimum)
        if widget.winfo_class() not in ("TEntry", "TCombobox") or widget.winfo_manager() != "grid":
            continue
        info = widget.grid_info()
        column = int(info["column"])
        if "e" in info["sticky"] and "w" in info["sticky"] and int(widget.master.grid_columnconfigure(column)["weight"]) > 0:
            if int(widget.cget("width")) > minimum:
                widget.configure(width=minimum)


class ScrollablePage(ttk.Frame):
    def __init__(self, parent, background="#F4F6F8"):
        super().__init__(parent, style="App.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            background=background,
            borderwidth=0,
            highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.horizontal_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=self.horizontal_scrollbar.set)
        self.content = ttk.Frame(self.canvas, style="App.TFrame")
        self.content.columnconfigure(0, weight=1)
        self._window_id = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        self.content.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._fit_content_width)
        self._scrolling_enabled = False
        self._wheel_remainder = 0
        self._content_width = None
        self._scroll_region = None
        self._horizontal_scrolling_enabled = None

    def bind_mousewheel(self):
        self.canvas.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self._bind_descendants(self.content)

    def scroll_to_top(self):
        self.canvas.yview_moveto(0.0)

    def _update_scroll_region(self, _event=None):
        bounds = self.canvas.bbox("all")
        if bounds != self._scroll_region:
            self._scroll_region = bounds
            self.canvas.configure(scrollregion=bounds)
        self._update_scroll_state()

    def _fit_content_width(self, event):
        width = max(event.width, self.content.winfo_reqwidth())
        if width != self._content_width:
            self._content_width = width
            self.canvas.itemconfigure(self._window_id, width=width)
        self._update_scroll_state()

    def _update_scroll_state(self):
        bounds = self.canvas.bbox("all")
        horizontal = bounds is not None and bounds[2] - bounds[0] > self.canvas.winfo_width()
        if horizontal != self._horizontal_scrolling_enabled:
            self._horizontal_scrolling_enabled = horizontal
            if horizontal:
                self.horizontal_scrollbar.grid()
            else:
                self.horizontal_scrollbar.grid_remove()
                self.canvas.xview_moveto(0.0)
        content_height = 0 if bounds is None else bounds[3] - bounds[1]
        should_scroll = content_height > self.canvas.winfo_height()
        if should_scroll == self._scrolling_enabled:
            return

        self._scrolling_enabled = should_scroll
        if should_scroll:
            self.scrollbar.grid()
        else:
            self.canvas.yview_moveto(0.0)
            self.scrollbar.grid_remove()

    def _on_mousewheel(self, event):
        if not self._scrolling_enabled:
            return
        self._wheel_remainder -= event.delta
        units = int(self._wheel_remainder / 120)
        self._wheel_remainder -= units * 120
        if units:
            self.canvas.yview_scroll(units, "units")
        return "break"

    def _bind_descendants(self, widget):
        if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview, ttk.Combobox)):
            return
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_descendants(child)
