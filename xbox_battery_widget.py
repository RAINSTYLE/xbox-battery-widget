"""
Виджет рабочего стола Windows 11: заряд батареи беспроводного геймпада Xbox.

Источники данных (по приоритету):
  1. Windows.Gaming.Input (WinRT)  — точные проценты, если контроллер их отдаёт.
  2. Bluetooth-свойство устройства — точные проценты (геймпад по Bluetooth).
  3. XInput                         — 4 уровня: пустой / низкий / средний / полный.

Управление: перетаскивание левой кнопкой мыши, меню — правой кнопкой.
Запуск с ключом --diag выводит в консоль, что видит каждый источник.
"""

import ctypes
import json
import os
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional

APP_NAME = "XboxBatteryWidget"
REFRESH_MS = 30_000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


# ───────────────────────────── Результат ─────────────────────────────

@dataclass
class BatteryState:
    connected: bool = False
    percent: Optional[int] = None      # точные проценты, если известны
    level: Optional[int] = None        # 0..3 (XInput), если процентов нет
    wired: bool = False                # питание по USB-кабелю
    charging: bool = False
    source: str = ""


# ───────────────────────────── XInput ─────────────────────────────

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_BATTERY_INFORMATION(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


BATTERY_TYPE_DISCONNECTED = 0x00
BATTERY_TYPE_WIRED = 0x01
BATTERY_TYPE_UNKNOWN = 0xFF
BATTERY_DEVTYPE_GAMEPAD = 0x00

_xinput = None
for _dll in ("xinput1_4", "xinput1_3"):
    try:
        _xinput = ctypes.WinDLL(_dll)
        _xinput.XInputGetBatteryInformation  # noqa: B018 — проверка наличия функции
        break
    except (OSError, AttributeError):
        _xinput = None


def xinput_read() -> List[tuple]:
    """Список (слот, тип_батареи, уровень 0..3) для подключённых геймпадов."""
    result = []
    if _xinput is None:
        return result
    for slot in range(4):
        state = XINPUT_STATE()
        if _xinput.XInputGetState(slot, ctypes.byref(state)) != 0:
            continue
        # Геймпад подключён. По Bluetooth XInput часто не знает заряд —
        # тогда тип батареи будет DISCONNECTED/UNKNOWN, но слот всё равно учитываем.
        info = XINPUT_BATTERY_INFORMATION()
        if _xinput.XInputGetBatteryInformation(slot, BATTERY_DEVTYPE_GAMEPAD, ctypes.byref(info)) != 0:
            info.BatteryType = BATTERY_TYPE_UNKNOWN
        result.append((slot, info.BatteryType, info.BatteryLevel))
    return result


# ───────────────────────── Windows.Gaming.Input ─────────────────────────

try:
    from winrt.windows.gaming.input import Gamepad
    import winrt.windows.devices.power  # noqa: F401 — проекция BatteryReport
    _WGI_OK = True
except Exception:  # noqa: BLE001
    Gamepad = None
    _WGI_OK = False


def wgi_init() -> None:
    """Подписка на события «прогревает» список геймпадов WinRT."""
    if not _WGI_OK:
        return
    try:
        Gamepad.add_gamepad_added(lambda *_: None)
        Gamepad.add_gamepad_removed(lambda *_: None)
        _ = Gamepad.gamepads
    except Exception:  # noqa: BLE001
        pass


def wgi_read() -> List[tuple]:
    """Список (проценты или None, зарядка: bool) для геймпадов WinRT."""
    result = []
    if not _WGI_OK:
        return result
    try:
        pads = list(Gamepad.gamepads)
    except Exception:  # noqa: BLE001
        return result
    for pad in pads:
        try:
            report = pad.try_get_battery_report()
        except Exception:  # noqa: BLE001
            report = None
        percent, charging = None, False
        if report is not None:
            try:
                remaining = report.remaining_capacity_in_milliwatt_hours
                full = report.full_charge_capacity_in_milliwatt_hours
                if remaining is not None and full:
                    percent = max(0, min(100, round(remaining * 100 / full)))
            except Exception:  # noqa: BLE001
                pass
            try:
                charging = int(report.status) == 3  # BatteryStatus.Charging
            except Exception:  # noqa: BLE001
                pass
        result.append((percent, charging))
    return result


# ───────────────────────── Bluetooth (cfgmgr32) ─────────────────────────

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        h = text.strip("{}").replace("-", "")
        g = cls(int(h[0:8], 16), int(h[8:12], 16), int(h[12:16], 16))
        for i in range(8):
            g.Data4[i] = int(h[16 + i * 2:18 + i * 2], 16)
        return g


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.ULONG)]


def _key(guid: str, pid: int) -> DEVPROPKEY:
    return DEVPROPKEY(GUID.parse(guid), pid)


DEVPKEY_NAME = _key("b725f130-47ef-101a-a5f1-02608c9eebac", 10)
DEVPKEY_FRIENDLY = _key("a45c254e-df1c-4efd-8020-67d146a850e0", 14)
DEVPKEY_BT_BATTERY = _key("104ea319-6ee2-4701-bd47-8ddbf425bbe5", 2)
DEVPKEY_BT_CONNECTED = _key("83da6326-97a6-4088-9453-a1923f573b29", 15)

DEVPROP_TYPE_BYTE = 0x03
DEVPROP_TYPE_STRING = 0x12
DEVPROP_TYPE_BOOLEAN = 0x11
CM_GETIDLIST_FILTER_ENUMERATOR = 0x1
CR_SUCCESS = 0

try:
    _cfg = ctypes.WinDLL("cfgmgr32")
except OSError:
    _cfg = None


def _devnode_prop(devinst: int, key: DEVPROPKEY):
    ptype = wintypes.ULONG()
    size = wintypes.ULONG(0)
    _cfg.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(ptype),
                                  None, ctypes.byref(size), 0)
    if size.value == 0:
        return None, None
    buf = (ctypes.c_ubyte * size.value)()
    if _cfg.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(ptype),
                                     buf, ctypes.byref(size), 0) != CR_SUCCESS:
        return None, None
    if ptype.value == DEVPROP_TYPE_STRING:
        return ptype.value, ctypes.wstring_at(ctypes.addressof(buf))
    return ptype.value, bytes(buf)


def bluetooth_read() -> List[int]:
    """Проценты заряда Xbox-геймпадов, подключённых по Bluetooth."""
    result = []
    if _cfg is None:
        return result
    for enumerator in ("BTHLE", "BTHENUM"):
        size = wintypes.ULONG()
        if _cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), enumerator,
                                            CM_GETIDLIST_FILTER_ENUMERATOR) != CR_SUCCESS:
            continue
        buf = ctypes.create_unicode_buffer(size.value + 1)
        if _cfg.CM_Get_Device_ID_ListW(enumerator, buf, size.value,
                                       CM_GETIDLIST_FILTER_ENUMERATOR) != CR_SUCCESS:
            continue
        ids = [s for s in buf[:size.value].split("\0") if s]
        for dev_id in ids:
            devinst = wintypes.DWORD()
            # Флаг 0 (NORMAL) находит только присутствующие устройства.
            if _cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), dev_id, 0) != CR_SUCCESS:
                continue
            _, name = _devnode_prop(devinst.value, DEVPKEY_FRIENDLY)
            if not name:
                _, name = _devnode_prop(devinst.value, DEVPKEY_NAME)
            if not name or "xbox" not in name.lower():
                continue
            ctype, cval = _devnode_prop(devinst.value, DEVPKEY_BT_CONNECTED)
            if ctype == DEVPROP_TYPE_BOOLEAN and cval and cval[0] == 0:
                continue  # сопряжён, но сейчас не подключён
            btype, bval = _devnode_prop(devinst.value, DEVPKEY_BT_BATTERY)
            if btype == DEVPROP_TYPE_BYTE and bval:
                result.append(max(0, min(100, bval[0])))
    return result


# ───────────────────────────── Сборка результата ─────────────────────────────

def read_battery() -> BatteryState:
    try:
        xi = xinput_read()
    except Exception:  # noqa: BLE001
        xi = []
    try:
        wgi = wgi_read()
    except Exception:  # noqa: BLE001
        wgi = []

    if not xi and not wgi:
        return BatteryState(connected=False)

    all_wired = bool(xi) and all(t == BATTERY_TYPE_WIRED for _, t, _ in xi)
    charging = any(c for _, c in wgi)

    # 1. Точные проценты от Bluetooth — то же значение, что в «Параметрах» Windows.
    if not all_wired:
        try:
            bt = bluetooth_read()
        except Exception:  # noqa: BLE001
            bt = []
        if bt:
            return BatteryState(True, percent=bt[0], charging=charging,
                                source="Bluetooth (точные %)")

    # 2. Проценты от Windows.Gaming.Input. Для Bluetooth-геймпадов он бывает
    #    неверным (остаток 100 из «ёмкости» 1000), поэтому идёт вторым.
    for percent, chg in wgi:
        if percent is not None:
            return BatteryState(True, percent=percent, charging=chg,
                                source="Windows.Gaming.Input (точные %)")

    # 3. Четыре уровня от XInput
    for _, btype, level in xi:
        if btype not in (BATTERY_TYPE_WIRED, BATTERY_TYPE_DISCONNECTED, BATTERY_TYPE_UNKNOWN):
            return BatteryState(True, level=min(3, level), charging=charging,
                                source="XInput (4 уровня)")
    if xi and all_wired:
        return BatteryState(True, wired=True, source="XInput (USB-кабель)")
    if xi and not wgi:
        return BatteryState(True, source="XInput (заряд неизвестен)")
    return BatteryState(True, charging=charging, source="Windows.Gaming.Input (нет данных)")


# ───────────────────────────── Настройки и автозапуск ─────────────────────────────

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_settings(data: dict) -> None:
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:  # noqa: BLE001
        pass


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_get() -> Optional[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0]
    except OSError:
        return None


def autostart_set(enabled: bool) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


# ───────────────────────────── Окно виджета ─────────────────────────────

BG = "#202020"
FG = "#F2F2F2"
FG_DIM = "#A0A0A0"
GREEN = "#4CC24A"
YELLOW = "#E8B931"
ORANGE = "#F0883E"
RED = "#E5484D"
GREY = "#6E6E6E"

LEVEL_NAMES = ["Пустой", "Низкий", "Средний", "Полный"]
LEVEL_COLORS = [RED, ORANGE, YELLOW, GREEN]


class Widget:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.state = BatteryState()
        self.updated_at = ""

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Батарея геймпада Xbox")
        self.root.overrideredirect(True)      # без рамки и без кнопки на панели задач
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", 0.93)

        self.s = self.root.winfo_fpixels("1i") / 96.0   # масштаб для высокого DPI
        self.font_main = tkfont.Font(family="Segoe UI Semibold", size=13)
        self.font_sub = tkfont.Font(family="Segoe UI", size=8)
        self.font_msg = tkfont.Font(family="Segoe UI", size=10)
        self.font_bolt = tkfont.Font(family="Segoe UI Symbol", size=9, weight="bold")

        self.height = int(56 * self.s)
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0,
                                height=self.height, width=int(200 * self.s), cursor="fleur")
        self.canvas.pack(fill="both", expand=True)

        self.topmost = tk.BooleanVar(value=bool(self.settings.get("topmost", False)))
        self.autostart = tk.BooleanVar(value=autostart_get() is not None)
        self.root.attributes("-topmost", self.topmost.get())

        self._build_menu()
        self._bind_drag()
        self._first_run_autostart()

        self.draw()
        self._place_initial()
        self.root.deiconify()
        self.root.after(50, self._round_corners)

        wgi_init()
        self.refresh()
        # WinRT заполняет список геймпадов не мгновенно — уточняем через 3 с.
        self.root.after(3000, self.refresh, False)

    # ── автозапуск ──
    def _first_run_autostart(self) -> None:
        if not self.settings.get("autostart_initialized"):
            autostart_set(True)
            self.settings["autostart_initialized"] = True
            save_settings(self.settings)
        elif self.autostart.get() and autostart_get() != autostart_command():
            autostart_set(True)   # exe перенесли в другую папку — обновляем путь
        self.autostart.set(autostart_get() is not None)

    def _toggle_autostart(self) -> None:
        autostart_set(self.autostart.get())
        self.autostart.set(autostart_get() is not None)

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", self.topmost.get())
        self.settings["topmost"] = self.topmost.get()
        save_settings(self.settings)

    # ── меню ──
    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="", state="disabled")
        self.menu.add_command(label="", state="disabled")
        self.menu.add_separator()
        self.menu.add_command(label="Обновить сейчас", command=self.refresh_now)
        self.menu.add_checkbutton(label="Поверх всех окон", variable=self.topmost,
                                  command=self._toggle_topmost)
        self.menu.add_checkbutton(label="Запускать вместе с Windows", variable=self.autostart,
                                  command=self._toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="Закрыть виджет", command=self.root.destroy)
        self.canvas.bind("<Button-3>", self._show_menu)

    def _show_menu(self, event) -> None:
        self.autostart.set(autostart_get() is not None)
        src = self.state.source or "—"
        self.menu.entryconfigure(0, label=f"Источник: {src}")
        self.menu.entryconfigure(1, label=f"Обновлено: {self.updated_at or '—'}")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ── перетаскивание ──
    def _bind_drag(self) -> None:
        self._drag = None
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _e) -> None:
        self._drag = None
        self.settings["x"] = self.root.winfo_x()
        self.settings["y"] = self.root.winfo_y()
        save_settings(self.settings)

    # ── позиция ──
    def _place_initial(self) -> None:
        self.root.update_idletasks()
        w, h = self.canvas.winfo_reqwidth(), self.height
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        x, y = self.settings.get("x"), self.settings.get("y")
        if x is None or y is None:
            x = vx + user32.GetSystemMetrics(0) - w - int(24 * self.s)
            y = vy + int(24 * self.s)
        # Не даём окну потеряться за пределами экранов (например, отключили монитор).
        x = max(vx, min(int(x), vx + vw - w))
        y = max(vy, min(int(y), vy + vh - h))
        self.root.geometry(f"+{x}+{y}")

    def _round_corners(self) -> None:
        try:
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref),
                                                       ctypes.sizeof(pref))
        except Exception:  # noqa: BLE001
            pass

    # ── обновление ──
    def refresh_now(self) -> None:
        self.refresh(reschedule=False)

    def refresh(self, reschedule: bool = True) -> None:
        try:
            self.state = read_battery()
        except Exception:  # noqa: BLE001
            self.state = BatteryState(connected=False)
        self.updated_at = time.strftime("%H:%M:%S")
        self.draw()
        if reschedule:
            self.root.after(REFRESH_MS, self.refresh)

    # ── рисование ──
    def draw(self) -> None:
        c, s, st = self.canvas, self.s, self.state
        c.delete("all")

        bx, bw, bh = 14 * s, 40 * s, 21 * s
        by = (self.height - bh) / 2
        pad, line = 3 * s, max(1, round(2 * s))
        inner_x0, inner_x1 = bx + pad, bx + bw - pad
        inner_y0, inner_y1 = by + pad, by + bh - pad

        if not st.connected:
            outline = GREY
        elif st.percent is not None:
            outline = FG if st.percent >= 20 else RED
        elif st.level is not None:
            outline = FG if st.level > 0 else RED
        else:
            outline = FG

        # Корпус и «плюсовой» контакт батареи
        c.create_rectangle(bx, by, bx + bw, by + bh, outline=outline, width=line)
        c.create_rectangle(bx + bw + line / 2, by + bh * 0.32, bx + bw + 4 * s, by + bh * 0.68,
                           fill=outline, outline=outline)

        if st.connected and st.percent is not None:
            p = st.percent
            color = GREEN if p >= 50 else YELLOW if p >= 20 else RED
            fill_w = (inner_x1 - inner_x0) * p / 100
            if fill_w > 0:
                c.create_rectangle(inner_x0, inner_y0, inner_x0 + max(fill_w, 2 * s), inner_y1,
                                   fill=color, outline="")
            main, sub = f"{p} %", "Геймпад Xbox"
        elif st.connected and st.level is not None:
            lvl = st.level
            color = LEVEL_COLORS[lvl]
            gap = 2 * s
            seg_w = (inner_x1 - inner_x0 - 2 * gap) / 3
            for i in range(3):
                x0 = inner_x0 + i * (seg_w + gap)
                if i < lvl:
                    c.create_rectangle(x0, inner_y0, x0 + seg_w, inner_y1, fill=color, outline="")
            main, sub = LEVEL_NAMES[lvl], f"Геймпад Xbox · уровень {lvl + 1} из 4"
        elif st.connected and st.wired:
            c.create_rectangle(inner_x0, inner_y0, inner_x1, inner_y1, fill=GREY, outline="")
            main, sub = "По проводу", "Геймпад Xbox · USB"
        elif st.connected:
            main, sub = "Нет данных", "Геймпад Xbox"
        else:
            c.create_line(inner_x0 + 6 * s, inner_y0, inner_x1 - 6 * s, inner_y1,
                          fill=GREY, width=line)
            main, sub = None, None

        if st.connected and st.charging:
            c.create_text(bx + bw / 2, by + bh / 2, text="⚡", fill=FG, font=self.font_bolt)
            sub = (sub or "Геймпад Xbox") + " · заряжается"

        tx = bx + bw + 14 * s
        if main is None:
            msg = "Геймпад не подключён"
            c.create_text(tx, self.height / 2, text=msg, anchor="w", fill=FG_DIM,
                          font=self.font_msg)
            text_w = self.font_msg.measure(msg)
        else:
            c.create_text(tx, self.height / 2 + 3 * s, text=main, anchor="sw", fill=FG,
                          font=self.font_main)
            c.create_text(tx, self.height / 2 + 2 * s, text=sub, anchor="nw", fill=FG_DIM,
                          font=self.font_sub)
            text_w = max(self.font_main.measure(main), self.font_sub.measure(sub))

        width = int(tx + text_w + 16 * s)
        if int(c.cget("width")) != width:
            c.configure(width=width)
            self.root.geometry(f"{width}x{self.height}")

    def run(self) -> None:
        self.root.mainloop()


# ───────────────────────────── Запуск ─────────────────────────────

def single_instance() -> bool:
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    single_instance.handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_Mutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def diag() -> None:
    wgi_init()
    t_end = time.time() + 3
    while time.time() < t_end:   # даём WinRT время найти геймпады
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)
    print("XInput    :", xinput_read(), "(слот, тип, уровень 0..3)")
    print("WinRT     :", wgi_read() if _WGI_OK else "недоступен", "(проценты, зарядка)")
    print("Bluetooth :", bluetooth_read(), "(проценты)")
    print("Итог      :", read_battery())
    print("Автозапуск:", autostart_get())


def main() -> None:
    if "--diag" in sys.argv:
        if sys.stdout is None:   # exe без консоли — пишем отчёт в файл
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            sys.stdout = open(os.path.join(SETTINGS_DIR, "diag.txt"), "w", encoding="utf-8")
        print("WinRT импорт:", "OK" if _WGI_OK else "ошибка")
        diag()
        sys.stdout.flush()
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    if not single_instance():
        return
    Widget().run()


if __name__ == "__main__":
    main()
