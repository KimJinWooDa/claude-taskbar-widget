# -*- coding: utf-8 -*-
"""
Claude 사용량 트레이 아이콘 v2 — 시계 옆에 사용률(%)을 항상 표시.

데이터 (우선순위):
 1. 사용량 API — 대화하지 않아도 60초마다 갱신 (계정 정책이 허용할 때만)
 2. ~/.claude/usage-widget.json — Stop 훅이 답변 직후 남기는 값 (API가 막혔을 때)

트레이 아이콘 = 가장 한도에 가까운 항목의 %.
아이콘 클릭 → 항목별 수치와 재설정까지 남은 시간이 메뉴에 표시된다.
"""
import ctypes
import ctypes.wintypes
import json
import os
import sys
import time
import socket
import threading
import queue
import logging
import datetime
import urllib.request
import urllib.error

APP_NAME = "ClaudeUsageWidget"
HOME = os.path.expanduser("~")
CRED_PATH = os.path.join(HOME, ".claude", ".credentials.json")
USAGE_FILE = os.path.join(HOME, ".claude", "usage-widget.json")
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLI_UA = "claude-cli/2.1.207 (external, cli)"
API_URL = "https://api.anthropic.com/api/oauth/usage"
API_HEADERS = {"User-Agent": CLI_UA, "anthropic-beta": "oauth-2025-04-20"}

POLL_SEC = 5
API_INTERVAL_OK = 60
API_INTERVAL_DENIED = 30 * 60
SINGLETON_PORT = 53917

APPDATA_DIR = os.path.join(os.environ.get("APPDATA", HOME), APP_NAME)
LOG_PATH = os.path.join(APPDATA_DIR, "widget.log")
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_VBS = os.path.join(STARTUP_DIR, "ClaudeUsageWidget.vbs")

WINDOW_LABELS = [
    ("five_hour", "현재 세션"),
    ("seven_day", "주간 (모든 모델)"),
    ("seven_day_opus", "주간 Opus"),
    ("seven_day_sonnet", "주간 Sonnet"),
    ("seven_day_oauth_apps", "주간 앱"),
]


def severity_color(pct):
    if pct is None:
        return "#6e7681"
    if pct >= 90:
        return "#da3633"
    if pct >= 70:
        return "#bb8009"
    return "#2ea043"


os.makedirs(APPDATA_DIR, exist_ok=True)
try:
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
        os.remove(LOG_PATH)
except OSError:
    pass
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------- 시간 표기
def reset_phrase(val):
    """'4시간 46분 후' 처럼 남은 시간으로 표기."""
    if val in (None, ""):
        return ""
    try:
        if isinstance(val, (int, float)):
            dt = datetime.datetime.fromtimestamp(float(val)).astimezone()
        else:
            s = str(val)
            if s.replace(".", "", 1).isdigit():
                dt = datetime.datetime.fromtimestamp(float(s)).astimezone()
            else:
                dt = datetime.datetime.fromisoformat(
                    s.replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError, OverflowError):
        return ""
    secs = (dt - datetime.datetime.now().astimezone()).total_seconds()
    if secs <= 0:
        return "곧 재설정"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}일 {hours}시간 후 재설정"
    if hours:
        return f"{hours}시간 {mins}분 후 재설정"
    return f"{mins}분 후 재설정"


def short_reset(val):
    """플로팅 바용 짧은 표기: '3시간 59분 후' / '곧 리셋'."""
    return reset_phrase(val).replace(" 재설정", "").replace("곧", "곧 리셋")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


def rows_from_limits(limits):
    """API의 limits 배열 → [(라벨, %, 리셋원본)]. 모델 스코프(Fable 등) 포함."""
    rows = []
    for it in limits or []:
        if not isinstance(it, dict) or it.get("percent") is None:
            continue
        kind = it.get("kind")
        model = ((it.get("scope") or {}).get("model") or {}).get("display_name")
        if kind == "session":
            label = "현재 세션"
        elif kind == "weekly_all":
            label = "주간 (모든 모델)"
        elif model:
            label = f"주간 {model}"
        else:
            label = str(kind or "?")
        try:
            rows.append((label, float(it["percent"]), it.get("resets_at")))
        except (TypeError, ValueError):
            continue
    return rows


def rows_from_windows(d):
    """[(라벨, %, 리셋원본)] — used_percentage / utilization(0~1) 모두 지원."""
    rows, seen = [], set()

    def pct_of(v):
        p = v.get("used_percentage")
        if p is None and v.get("utilization") is not None:
            u = float(v["utilization"])
            p = u * 100 if u <= 1 else u
        return None if p is None else float(p)

    for key, label in WINDOW_LABELS:
        seen.add(key)
        v = d.get(key)
        if isinstance(v, dict):
            try:
                p = pct_of(v)
            except (TypeError, ValueError):
                continue
            if p is not None:
                rows.append((label, p, v.get("resets_at")))
    for key, v in d.items():
        if key in seen or not isinstance(v, dict):
            continue
        try:
            p = pct_of(v)
        except (TypeError, ValueError):
            continue
        if p is not None:
            rows.append((key, p, v.get("resets_at")))
    return rows


# ---------------------------------------------------------------- API
class ApiDenied(Exception):
    pass


_refresh_lock = threading.Lock()


def get_access_token(force_refresh=False):
    with _refresh_lock:
        try:
            with open(CRED_PATH, encoding="utf-8") as f:
                creds = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ApiDenied(f"인증 파일 없음: {e}")
        oauth = creds.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not force_refresh and token and \
                oauth.get("expiresAt", 0) > time.time() * 1000 + 120_000:
            return token
        rt = oauth.get("refreshToken")
        if not rt:
            raise ApiDenied("리프레시 토큰 없음")
        req = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps({"grant_type": "refresh_token", "refresh_token": rt,
                             "client_id": CLIENT_ID}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": CLI_UA},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                t = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            log.info("token refresh body: %s", body)
            raise ApiDenied(f"토큰 갱신 실패 HTTP {e.code}")
        except OSError as e:
            raise RuntimeError(f"네트워크: {e}")
        try:
            with open(CRED_PATH, encoding="utf-8") as f:
                creds = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        o = creds.setdefault("claudeAiOauth", {})
        o["accessToken"] = t["access_token"]
        if t.get("refresh_token"):
            o["refreshToken"] = t["refresh_token"]
        o["expiresAt"] = int(time.time() * 1000) + int(t.get("expires_in", 3600)) * 1000
        tmp = CRED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(creds, f)
        os.replace(tmp, CRED_PATH)
        log.info("token refreshed")
        return t["access_token"]


def fetch_usage_api():
    for attempt in (0, 1):
        token = get_access_token(force_refresh=(attempt == 1))
        req = urllib.request.Request(
            API_URL, headers={"Authorization": f"Bearer {token}", **API_HEADERS})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            if e.code in (401, 403):
                raise ApiDenied(f"HTTP {e.code} {body[:120]}")
            raise RuntimeError(f"HTTP {e.code}")
        except OSError as e:
            raise RuntimeError(f"네트워크: {e}")
    raise ApiDenied("인증 실패")


# ---------------------------------------------------------------- 자동 실행
def pythonw_exe():
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def startup_installed():
    return os.path.exists(STARTUP_VBS)


def install_startup():
    content = ('CreateObject("Wscript.Shell").Run '
               f'"""{pythonw_exe()}"" ""{os.path.abspath(__file__)}""", 0, False')
    with open(STARTUP_VBS, "w", encoding="utf-16") as f:
        f.write(content)
    log.info("startup registered")


def uninstall_startup():
    try:
        os.remove(STARTUP_VBS)
    except OSError:
        pass


def demote_tray_icon():
    """아이콘을 시계 옆이 아니라 오버플로(^) 패널 안에 두기 — 바가 숫자를 대신 보여준다."""
    import winreg
    exe = pythonw_exe().lower()
    changed = 0
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Control Panel\NotifyIconSettings")
    except OSError:
        return 0
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub, 0,
                                    winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                    if str(winreg.QueryValueEx(k, "ExecutablePath")[0]).lower() == exe:
                        if winreg.QueryValueEx(k, "IsPromoted")[0] != 0:
                            winreg.SetValueEx(k, "IsPromoted", 0,
                                              winreg.REG_DWORD, 0)
                            changed += 1
            except OSError:
                continue
    if changed:
        log.info("tray demoted: %d", changed)
    return changed


# ---------------------------------------------------------------- 아이콘
def make_icon_image(pct):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=severity_color(pct))
    text = "?" if pct is None else str(min(round(pct), 999))
    size = 38 if len(text) <= 2 else 27
    try:
        font = ImageFont.truetype("arialbd.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    b = d.textbbox((0, 0), text, font=font)
    d.text(((64 - b[2] - b[0]) / 2, (64 - b[3] - b[1]) / 2), text,
           font=font, fill="#ffffff")
    return img


# ---------------------------------------------------------------- Claude 감시
class _PE32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


def claude_running():
    """claude.exe(데스크톱 앱 또는 CLI)가 하나라도 실행 중인가."""
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(2, 0)
    if snap in (0, -1):
        return True     # 조회 실패 시엔 종료하지 않는 쪽으로
    try:
        e = _PE32W()
        e.dwSize = ctypes.sizeof(_PE32W)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == "claude.exe":
                return True
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return False
    finally:
        k.CloseHandle(snap)


# ---------------------------------------------------------------- 플로팅 바
def _taskbar_covered():
    """작업표시줄이 전체화면 앱에 가려졌으면 True — 바를 잠시 숨긴다.

    전면 창 좌표 비교는 테두리 없는 최대화 창(Electron 앱 등)을 오탐하므로,
    작업표시줄 중앙 픽셀을 실제로 차지한 창이 무엇인지로 판정한다.
    """
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        u.GetAncestor.restype = ctypes.c_void_p
        tray = u.FindWindowW("Shell_TrayWnd", None)
        if not tray or not u.IsWindowVisible(ctypes.c_void_p(tray)):
            return True
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(tray), ctypes.byref(r))
        pt = ctypes.wintypes.POINT((r.left + r.right) // 2,
                                   (r.top + r.bottom) // 2)
        h = u.WindowFromPoint(pt)
        h = u.GetAncestor(ctypes.c_void_p(h), 2) if h else None
        if not h or h == tray:
            return False
        hr = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(hr))
        if (hr.right - hr.left) < u.GetSystemMetrics(0) * 3 // 5:
            return False        # 툴팁·플라이아웃 같은 작은 창은 가림으로 안 침
        return hr.top < r.top - 4
    except Exception:
        return False


class FloatingBar(threading.Thread):
    """작업표시줄에 얹히는 투명 두 줄 바 (시안 B) — 1줄 세션, 2줄 Fable(없으면 주간).

    평상시 무채색, 70%↑ 주황·90%↑ 빨강만 색 표시. 드래그로 이동(위치 저장),
    우클릭 숨김, 트레이 메뉴에서 표시·잠금 토글. 잠금 시 클릭이 통과한다.
    """

    BG = "#1f1f1f"          # 첫 픽셀 샘플링 전까지의 임시 배경
    PAL_DARK = {"label": "#a6a6a6", "value": "#dcdcdc", "time": "#7a7a7a"}
    PAL_LIGHT = {"label": "#5f5f5f", "value": "#1f1f1f", "time": "#909090"}

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app

    def run(self):
        while not self.app.stop_evt.is_set():
            try:
                self._run()
            except Exception:
                log.exception("floating bar crashed")
            if self.app.stop_evt.is_set():
                break
            time.sleep(5)   # 탐색기 재시작 등으로 창이 죽으면 새로 만든다

    def _run(self):
        import tkinter as tk
        import tkinter.font as tkfont
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=self.BG)
        f = self._font = tkfont.Font(family="맑은 고딕", size=8)
        self._fix_w = f.measure("주간 (모든 모델) 100%  · 16시간 59분 후") + 24
        self._fix_h = 2 * f.metrics("linespace") + 8
        self._shown = False
        self._covered = 0
        self._ticks = 0
        self._rgb = None
        self._pal = self.PAL_DARK
        self._bgimg = None
        self._last = [None, None]
        cv = self.cv = tk.Canvas(root, width=self._fix_w, height=self._fix_h,
                                 highlightthickness=0, bd=0, bg=self.BG)
        cv.pack()
        self._img_item = cv.create_image(0, 0, anchor="nw")
        self._ys = (self._fix_h // 4 + 1, self._fix_h * 3 // 4 - 1)
        self.items = [tuple(cv.create_text(8, y, anchor="w", font=f, text="",
                                           fill=self._pal[k])
                            for k in ("label", "value", "time"))
                      for y in self._ys]
        self._lock_applied = None
        for w in (root, cv):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save_pos)
            w.bind("<Button-3>", self._hide_click)
        self._place_initial()
        self._adopt_by_taskbar()
        self._tick()
        root.mainloop()

    def _adopt_by_taskbar(self):
        """작업표시줄을 소유자(owner)로 지정 — 그 바로 위 z에 상시 고정.

        작업표시줄이 z순서를 되찾을 때 소유 창은 같은 순간 함께 올라오므로
        타이머 lift로 쫓아갈 필요가 없고(깜빡임 소멸), 전체화면 앱이
        작업표시줄을 덮으면 같이 덮여 자연스럽게 가려진다.
        """
        try:
            u = ctypes.windll.user32
            u.FindWindowW.restype = ctypes.c_void_p
            u.GetWindow.restype = ctypes.c_void_p
            tray = u.FindWindowW("Shell_TrayWnd", None)
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            if not tray or not hwnd:
                return
            if u.GetWindow(ctypes.c_void_p(hwnd), 4) == tray:
                return              # 이미 걸려 있음 — Tk가 지웠을 때만 다시 건다
            u.SetWindowLongPtrW.restype = ctypes.c_void_p
            u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_void_p]
            u.SetWindowLongPtrW(ctypes.c_void_p(hwnd), -8,
                                ctypes.c_void_p(tray))
            log.info("bar owned by taskbar")
        except Exception:
            log.exception("adopt failed")

    def _probe(self):
        """바 왼쪽 옆 작업표시줄 픽셀 하나 — 배경이 바뀌었는지 감지용."""
        try:
            self.root.update_idletasks()    # geometry 반영 전 winfo_x()=0 방지
            u, g = ctypes.windll.user32, ctypes.windll.gdi32
            dc = u.GetDC(0)
            c = g.GetPixel(dc, self.root.winfo_x() - 6,
                           self.root.winfo_y() + self._fix_h // 2)
            u.ReleaseDC(0, dc)
            if c < 0:
                return None
            return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
        except Exception:
            return None

    def _match_background(self, force=False):
        """바 자리의 작업표시줄 픽셀을 통째로 캡처해 배경 이미지로 — 완전 위장.

        단색이 아니라 실제 조각(미카 그라데이션 포함)을 입히므로 경계가 없다.
        캡처하려면 바를 잠깐 숨겨야 해서, 옆 픽셀이 실제로 달라졌을 때만 다시 찍는다.
        """
        rgb = self._probe()
        if rgb is None:
            return
        if not force and self._rgb and \
                max(abs(a - b) for a, b in zip(rgb, self._rgb)) <= 3:
            return
        self._rgb = rgb
        was_shown = self._shown
        try:
            from PIL import ImageGrab, ImageTk
            if was_shown:
                self.root.withdraw()
                self.root.update()
                time.sleep(0.06)        # 컴포지터가 창을 지울 시간
            x, y = self.root.winfo_x(), self.root.winfo_y()
            img = ImageGrab.grab(bbox=(x, y, x + self._fix_w,
                                       y + self._fix_h), all_screens=True)
            self._bgimg = ImageTk.PhotoImage(img)
            self.cv.itemconfigure(self._img_item, image=self._bgimg)
            r, gr, b = img.resize((1, 1)).getpixel((0, 0))[:3]
            lum = 0.299 * r + 0.587 * gr + 0.114 * b
            self._pal = self.PAL_LIGHT if lum >= 128 else self.PAL_DARK
            self._last = [None, None]   # 새 팔레트로 텍스트 다시 그리기
            log.info("bar camo #%02x%02x%02x", r, gr, b)
        except Exception:
            log.exception("bg capture failed")
        finally:
            if was_shown:
                self.root.deiconify()

    def _value_color(self, pct):
        if pct >= 90:
            return "#da3633"
        if pct >= 70:
            return "#bb8009"
        return self._pal["value"]

    def _apply_lock(self):
        """잠금이면 클릭 통과(WS_EX_TRANSPARENT), 아니면 해제. Alt-Tab 제외는 항상."""
        locked = bool(self.app.cfg.get("bar_locked"))
        if locked == self._lock_applied:
            return
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            style = u.GetWindowLongW(hwnd, -20) | 0x80
            if locked:
                style |= 0x20
            else:
                style &= ~0x20
            u.SetWindowLongW(hwnd, -20, style)
            self._lock_applied = locked
        except Exception:
            log.exception("lock apply failed")

    def _place_initial(self):
        w, h = self._fix_w, self._fix_h
        x, y = self.app.cfg.get("bar_x"), self.app.cfg.get("bar_y")
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            r = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                       ctypes.byref(r), 0)
            x = sw - w - 330                       # 트레이 아이콘 왼쪽
            if sh > r.bottom:                      # 작업표시줄이 아래쪽
                y = r.bottom + max((sh - r.bottom - h) // 2, 0)
            else:
                y = sh - h - 8
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def _press(self, e):
        if self.app.cfg.get("bar_locked"):
            return
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag(self, e):
        if self.app.cfg.get("bar_locked") or not hasattr(self, "_dx"):
            return
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _save_pos(self, e):
        if self.app.cfg.get("bar_locked") or not hasattr(self, "_dx"):
            return
        self.app.cfg["bar_x"] = self.root.winfo_x()
        self.app.cfg["bar_y"] = self.root.winfo_y()
        save_config(self.app.cfg)
        self._match_background(force=True)  # 옮긴 자리의 배경으로 다시 위장

    def _hide_click(self, e):
        self.app.cfg["bar_visible"] = False
        save_config(self.app.cfg)
        self.root.withdraw()

    def _pick(self):
        """rows에서 (세션, 둘째 줄, 둘째줄라벨) 고르기 — Fable 우선, 없으면 주간."""
        sess = fable = week = None
        for label, pct, reset in self.app.rows:
            if label == "현재 세션" and sess is None:
                sess = (pct, reset)
            elif "Fable" in label and fable is None:
                fable = (pct, reset)
            elif label.startswith("주간") and week is None:
                week = (pct, reset)
        if fable:
            return sess, fable, "Fable"
        return sess, week, "주간"

    def _tick(self):
        if self.app.stop_evt.is_set():
            self.root.destroy()
            return
        try:
            self._update()
        except Exception:
            log.exception("bar update failed")
        self.root.after(2000, self._tick)

    def _update(self):
        self._ticks += 1
        if not self.app.cfg.get("bar_visible", True):
            self._show(False)
            return
        self._covered = self._covered + 1 if _taskbar_covered() else 0
        if self._covered >= 2:      # 순간 오탐으로 깜빡이지 않게 2회 연속일 때만
            self._show(False)
            return
        if self._ticks % 15 == 0:
            self._match_background()
        sess, second, name2 = self._pick()
        for idx, (title, row) in enumerate((("세션", sess), (name2, second))):
            if row:
                pct, reset = row
                t = short_reset(reset)
                self._set_line(idx, f"{title} ", f"{round(pct)}%",
                               f" · {t}" if t else "", self._value_color(pct))
            else:
                self._set_line(idx, "", "", "", self._pal["value"])
        self._show(True)
        self._apply_lock()

    def _set_line(self, idx, label, value, when, vcolor):
        """실제로 달라졌을 때만 캔버스 텍스트를 다시 그린다."""
        key = (label, value, when, vcolor, id(self._pal))
        if self._last[idx] == key:
            return
        self._last[idx] = key
        l, v, w = self.items[idx]
        self.cv.itemconfigure(l, text=label, fill=self._pal["label"])
        self.cv.itemconfigure(v, text=value, fill=vcolor)
        self.cv.itemconfigure(w, text=when, fill=self._pal["time"])
        y = self._ys[idx]
        x = 8 + self._font.measure(label)
        self.cv.coords(v, x, y)
        self.cv.coords(w, x + self._font.measure(value), y)

    def _show(self, on):
        """상태가 바뀔 때만 표시/숨김 — 매 틱 재표시로 인한 깜빡임 방지."""
        if on and not self._shown:
            if self._bgimg is None:
                self._match_background(force=True)  # 첫 표시 전에 위장 준비
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
            self._adopt_by_taskbar()    # 표시 후에 걸어야 Tk가 안 지운다
        elif not on and self._shown:
            self.root.withdraw()
        elif on and self._ticks % 30 == 0:
            self.root.lift()
            self._adopt_by_taskbar()    # 연결이 풀린 경우를 위한 드문 보험
        self._shown = on


# ---------------------------------------------------------------- 앱
class TrayApp:
    def __init__(self):
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.wake = threading.Event()
        self.force_api = threading.Event()
        self.rows = []
        self.source = None      # "api" | "hook"
        self.updated_at = None
        self.status = "불러오는 중…"
        self.icon = None
        self.cfg = load_config()

        self._load_file(initial=True)   # 켜자마자 마지막 값 표시

    # ---------------- 데이터
    def _load_file(self, initial=False):
        try:
            mtime = os.path.getmtime(USAGE_FILE)
        except OSError:
            return None
        try:
            with open(USAGE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        rows = rows_from_windows(d.get("rate_limits") or {})
        if not rows:
            return None
        ts = d.get("written_at", mtime)
        if initial:
            self.rows, self.source, self.updated_at = rows, "hook", ts
            self.status = None
        return (rows, ts)

    def _poll_loop(self):
        last_mtime = None
        last_cred = None
        next_api = 0.0
        api_denied_reason = None
        api_ok = False
        claude_gone_at = None
        while not self.stop_evt.is_set():
            now = time.time()
            try:
                if claude_running():
                    claude_gone_at = None
                elif claude_gone_at is None:
                    claude_gone_at = now
                elif now - claude_gone_at >= 10:
                    log.info("claude not running - exiting with it")
                    self.q.put(("quit",))
                    break
                if self.force_api.is_set():
                    self.force_api.clear()
                    next_api = 0.0
                try:
                    cred = os.path.getmtime(CRED_PATH)
                except OSError:
                    cred = None
                if last_cred is None:
                    last_cred = cred
                elif cred != last_cred:
                    last_cred = cred
                    next_api = 0.0
                    log.info("credentials changed - retry api now")
                if now >= next_api:
                    try:
                        data = fetch_usage_api()
                        rows = rows_from_limits(data.get("limits")) \
                            or rows_from_windows(data)
                        if rows:
                            self.q.put(("data", rows, "api", now))
                            api_denied_reason = None
                            if not api_ok:
                                api_ok = True
                                log.info("api ok: %d rows", len(rows))
                        next_api = now + API_INTERVAL_OK
                    except ApiDenied as e:
                        api_denied_reason = str(e)
                        api_ok = False
                        next_api = now + API_INTERVAL_DENIED
                        log.info("api denied: %s", e)
                    except Exception as e:
                        api_ok = False
                        next_api = now + API_INTERVAL_OK
                        log.warning("api error: %s", e)

                try:
                    mtime = os.path.getmtime(USAGE_FILE)
                except OSError:
                    mtime = None
                if mtime and mtime != last_mtime:
                    last_mtime = mtime
                    got = self._load_file()
                    if got:
                        self.q.put(("data", got[0], "hook", got[1]))

                if not self.rows:
                    self.q.put(("status", "대기 중 — 훅 설정 확인 필요"
                                if api_denied_reason else "불러오는 중…"))
            except Exception:
                log.exception("poll error")
            self.wake.wait(POLL_SEC)
            self.wake.clear()

    # ---------------- 트레이
    def _menu_lines(self):
        import pystray
        items = []
        if self.rows:
            for label, pct, reset in self.rows:
                phrase = reset_phrase(reset)
                text = f"{label}   {round(pct)}%"
                if phrase:
                    text += f"   ·  {phrase}"
                items.append(pystray.MenuItem(text, None, enabled=False))
            if self.updated_at:
                age = time.time() - self.updated_at
                when = time.strftime("%H:%M", time.localtime(self.updated_at))
                src = "실시간" if self.source == "api" else "마지막 대화 시점"
                stale = "" if age < 90 else f" ({int(age // 60)}분 전)"
                items.append(pystray.MenuItem(f"— {src} · {when}{stale}",
                                              None, enabled=False))
        else:
            items.append(pystray.MenuItem(self.status or "데이터 없음",
                                          None, enabled=False))
        return items

    def _build_menu(self):
        import pystray
        return pystray.Menu(
            *self._menu_lines(),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("지금 새로고침", lambda i, it: self.q.put(("refresh",))),
            pystray.MenuItem("플로팅 바 표시",
                             lambda i, it: self.q.put(("bar",)),
                             checked=lambda it: self.cfg.get("bar_visible", True)),
            pystray.MenuItem("바 위치 잠금 (클릭 통과)",
                             lambda i, it: self.q.put(("lock",)),
                             checked=lambda it: bool(self.cfg.get("bar_locked"))),
            pystray.MenuItem("Windows 시작 시 자동 실행",
                             lambda i, it: self.q.put(("startup",)),
                             checked=lambda it: startup_installed()),
            pystray.MenuItem("로그 폴더 열기", lambda i, it: self.q.put(("log",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda i, it: self.q.put(("quit",))),
        )

    def _worst(self):
        p = [x[1] for x in self.rows if x[1] is not None]
        return max(p) if p else None

    def _refresh_tray(self):
        if not self.icon:
            return
        try:
            self.icon.icon = make_icon_image(self._worst())
            tip = ["Claude 사용량"]
            for label, pct, reset in self.rows:
                phrase = reset_phrase(reset)
                tip.append(f"{label} {round(pct)}%" + (f" · {phrase}" if phrase else ""))
            if not self.rows:
                tip.append(self.status or "")
            self.icon.title = "\n".join(tip)[:127]
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
        except Exception as e:
            log.error("tray refresh failed: %s", e)

    def _pump(self, icon):
        icon.visible = True
        self._refresh_tray()
        for delay in (6, 60):
            t = threading.Timer(delay, lambda: demote_tray_icon())
            t.daemon = True
            t.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._singleton_listener, daemon=True).start()
        FloatingBar(self).start()

        last_tray = 0.0
        while not self.stop_evt.is_set():
            try:
                msg = self.q.get(timeout=1.0)
            except queue.Empty:
                if self.rows and time.time() - last_tray >= 30:
                    last_tray = time.time()
                    self._refresh_tray()   # 남은 시간 표시 갱신 (분 단위면 충분)
                continue
            kind = msg[0]
            if kind == "data":
                self.rows, self.source, self.updated_at = msg[1], msg[2], msg[3]
                self.status = None
                self._refresh_tray()
            elif kind == "status":
                if not self.rows:
                    self.status = msg[1]
                    self._refresh_tray()
            elif kind == "refresh":
                self.force_api.set()
                self.wake.set()
            elif kind == "bar":
                self.cfg["bar_visible"] = not self.cfg.get("bar_visible", True)
                save_config(self.cfg)
                self._refresh_tray()
            elif kind == "lock":
                self.cfg["bar_locked"] = not self.cfg.get("bar_locked")
                save_config(self.cfg)
                self._refresh_tray()
            elif kind == "startup":
                uninstall_startup() if startup_installed() else install_startup()
                self._refresh_tray()
            elif kind == "log":
                os.startfile(APPDATA_DIR)
            elif kind == "quit":
                self.stop_evt.set()
                icon.stop()
                return

    def _singleton_listener(self):
        if _singleton_sock is None:
            return
        _singleton_sock.settimeout(1.0)
        while not self.stop_evt.is_set():
            try:
                conn, _ = _singleton_sock.accept()
                conn.close()
                self.force_api.set()
                self.wake.set()
            except socket.timeout:
                continue
            except OSError:
                break

    def run(self):
        import pystray
        self.icon = pystray.Icon(APP_NAME, make_icon_image(self._worst()),
                                 "Claude 사용량", self._build_menu())
        self.icon.run(setup=self._pump)


_singleton_sock = None


def acquire_singleton():
    global _singleton_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(2)
        _singleton_sock = s
    except OSError:
        s.close()
        try:
            c = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=2)
            c.close()
            log.info("already running — signalled existing instance")
            sys.exit(0)
        except OSError:
            _singleton_sock = None


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    threading.excepthook = lambda a: log.error(
        "thread crashed", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    acquire_singleton()
    log.info("---- tray v2 start (python %s) ----", sys.version.split()[0])
    TrayApp().run()


if __name__ == "__main__":
    main()
