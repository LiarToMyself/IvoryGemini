#!/usr/bin/env python3
"""IvoryGemini — Gemini Desktop Bridge MCP Server v2.5.0 (Windows UIA & macOS Accessibility)."""

import base64
import ctypes
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVER_NAME = "gemini-desktop-bridge"
SERVER_VERSION = "2.5.0"
APP_NAME = os.environ.get("GEMINI_APP_NAME", "Gemini")
CUSTOM_EXE_PATH = os.environ.get("GEMINI_DESKTOP_PATH", "").strip()
DEFAULT_WAIT_TIMEOUT_SEC = int(os.environ.get("GEMINI_DEFAULT_TIMEOUT_SEC", "180"))
DEFAULT_POLL_INTERVAL_SEC = float(os.environ.get("GEMINI_POLL_INTERVAL_SEC", "1.5"))
DEFAULT_STABLE_SEC = float(os.environ.get("GEMINI_STABLE_SEC", "4.0"))
DEFAULT_MAX_RESPONSE_CHARS = int(os.environ.get("GEMINI_MAX_RESPONSE_CHARS", "0"))
MASK_EMAILS_DEFAULT = os.environ.get("GEMINI_MASK_EMAILS", "1").strip().lower() not in ("0", "false", "no")
EXPOSE_RAW_EMAILS = os.environ.get("GEMINI_EXPOSE_RAW_EMAILS", "0").strip().lower() in ("1", "true", "yes")

ENV_ACCOUNT_SLOTS: Dict[int, str] = {
    i: os.environ.get(f"GEMINI_ACCOUNT_SLOT_{i}", "").strip()
    for i in range(10)
    if os.environ.get(f"GEMINI_ACCOUNT_SLOT_{i}", "").strip()
}

_STATE_LOCK = threading.Lock()
_WINDOW_ACTION_LOCK = threading.RLock()
_TASK_STATE: Dict[str, Any] = {
    "task_id": 0, "prompt": "", "sent_at": 0.0, "baseline_response": "", "baseline_hash": "",
    "last_partial_response": "", "last_completed_response": "", "status": "idle",
    "is_generating": False, "window_hwnd": 0, "active_account": None, "active_account_slot": None, "updated_at": 0.0,
}

_EMAIL_RE = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
_FOOTER_NOISE_PATTERNS = [
    re.compile(r"(?im)^\s*(?:Gemini\s*[—\-–:]?\s*это\s+ИИ|—\s*это\s+ИИ)\b.*?(?:ошибаться|проверяйте).*$"),
    re.compile(r"(?im)^\s*Gemini\s+может\s+(?:выдавать\s+неточную\s+информацию|ошибаться).*$"),
    re.compile(r"(?im)^\s*Gemini\s+(?:is\s+AI\s+and\s+)?can\s+make\s+mistakes.*$"),
    re.compile(r"(?im)^\s*Выбор\s+режима,\s*сейчас\s+используется\b.*$"),
    re.compile(r"(?im)^\s*(?:Проверяйте\s+важную\s+информацию|Double-check\s+important\s+info)\.?$"),
]


def _log_stderr(msg: str) -> None:
    try:
        sys.stderr.write(f"[{SERVER_NAME}] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def mask_email_address(text: Optional[str]) -> str:
    if not text:
        return ""
    if EXPOSE_RAW_EMAILS or not MASK_EMAILS_DEFAULT:
        return text
    def _repl(m: re.Match) -> str:
        addr = m.group(1)
        local, domain = addr.split("@", 1) if "@" in addr else (addr, "")
        masked = (local[0] + "***") if len(local) <= 2 else f"{local[0]}***{local[-1]}"
        return f"{masked}@{domain}" if domain else masked
    return _EMAIL_RE.sub(_repl, text)


def clean_extracted_response(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    for pat in _FOOTER_NOISE_PATTERNS:
        cleaned = pat.sub("", cleaned)
    cleaned = re.sub(
        r"(?is)\s*(?:Gemini\s*[—\-–]\s*это\s+ИИ\.?\s*Он\s+может\s+ошибаться\.?|—\s*это\s+ИИ\.?\s*Он\s+может\s+ошибаться\.?|Gemini\s+can\s+make\s+mistakes,\s*so\s+double-check\s+it\.?)\s*$",
        "", cleaned,
    )
    return cleaned.strip()


def apply_text_pagination(text: str, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS, offset: int = 0) -> Tuple[str, Dict[str, Any]]:
    total_len = len(text)
    safe_offset = max(0, min(offset, total_len))
    sliced = text[safe_offset:]
    truncated = bool(max_chars and max_chars > 0 and len(sliced) > max_chars)
    if truncated:
        sliced = sliced[:max_chars]
    meta: Dict[str, Any] = {"total_char_count": total_len, "offset": safe_offset, "returned_char_count": len(sliced), "truncated": truncated}
    if truncated:
        meta["next_offset"] = safe_offset + len(sliced)
    return sliced, meta


def _update_task_state(**kwargs: Any) -> Dict[str, Any]:
    with _STATE_LOCK:
        if isinstance(kwargs.get("active_account"), str):
            kwargs["active_account"] = mask_email_address(kwargs["active_account"])
        _TASK_STATE.update(kwargs)
        _TASK_STATE["updated_at"] = time.time()
        return dict(_TASK_STATE)


def _get_task_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        st = dict(_TASK_STATE)
        st["elapsed_sec"] = round(time.time() - st["sent_at"], 1) if st["sent_at"] > 0 else 0.0
        return st


def _hash_text(text: str) -> str:
    norm = re.sub(r"\s+", " ", clean_extracted_response(text or "").strip())
    return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()[:16]


def find_gemini_desktop_executable() -> Optional[str]:
    if CUSTOM_EXE_PATH and os.path.exists(CUSTOM_EXE_PATH):
        return CUSTOM_EXE_PATH
    system = platform.system()
    if system == "Windows":
        la = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
        ad = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        matches = sorted(glob.glob(os.path.join(la, "Google", "Gemini", "app-*", "Gemini.exe")), reverse=True)
        if matches:
            return matches[0]
        for p in (
            os.path.join(la, "Google", "Gemini", "Gemini.exe"),
            os.path.join(la, "Programs", "Gemini", "Gemini.exe"),
            os.path.join(la, "gemini-desktop", "Gemini.exe"),
            os.path.join(pf, "Google", "Gemini", "Gemini.exe"),
            os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs", "Gemini.lnk"),
            os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs", "Google Gemini.lnk"),
        ):
            if os.path.exists(p):
                return p
    elif system == "Darwin":
        for p in ("/Applications/Gemini.app", os.path.expanduser("~/Applications/Gemini.app")):
            if os.path.exists(p):
                return p
    return None


def bind_thread_to_default_desktop_win32() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDPIAware"):
            user32.SetProcessDPIAware()
        h_desk = user32.OpenDesktopW("default", 0, False, 0x10000000 | 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
            return True
    except Exception:
        pass
    return False


def find_gemini_windows_win32() -> List[Dict[str, Any]]:
    if platform.system() != "Windows":
        return []
    bind_thread_to_default_desktop_win32()
    user32, kernel32, psapi = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.psapi
    found: List[Dict[str, Any]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    SKIP_EXES = {
        "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "explorer.exe",
        "antigravity.exe", "cursor.exe", "code.exe", "windsurf.exe", "pycharm64.exe", "devenv.exe", "claude.exe", "chatgpt.exe",
    }

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def _proc_name(pid: int) -> str:
        h = kernel32.OpenProcess(0x1010, False, pid)
        if not h:
            return ""
        try:
            buf, sz = ctypes.create_unicode_buffer(512), ctypes.c_ulong(512)
            if hasattr(kernel32, "QueryFullProcessImageNameW") and kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
                return os.path.basename(buf.value).lower()
            if psapi.GetModuleBaseNameW(h, None, buf, 512):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            kernel32.CloseHandle(h)

    def _cb(hwnd: int, _lp: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        ln = user32.GetWindowTextLengthW(hwnd)
        if ln <= 0:
            return True
        buf = ctypes.create_unicode_buffer(ln + 1)
        user32.GetWindowTextW(hwnd, buf, ln + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = _proc_name(pid.value)
        if pname in SKIP_EXES:
            return True
        if "gemini" in pname or APP_NAME.lower() in title.lower():
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            w, h = max(0, r.right - r.left), max(0, r.bottom - r.top)
            found.append({
                "hwnd": int(hwnd), "title": title, "pid": int(pid.value), "process": pname,
                "class_name": cbuf.value, "is_minimized": bool(user32.IsIconic(hwnd)),
                "rect": {"left": int(r.left), "top": int(r.top), "right": int(r.right), "bottom": int(r.bottom), "width": int(w), "height": int(h)},
            })
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    found.sort(key=lambda w: (0 if w.get("process") == "gemini.exe" else 1, 0 if not w.get("is_minimized") else 1, -(w["rect"]["width"] * w["rect"]["height"])))
    return found


def check_macos_gemini_window() -> Dict[str, Any]:
    script = f'tell application "System Events"\nif not (exists process "{APP_NAME}") then return "NO_PROCESS"\ntell process "{APP_NAME}"\nif (count of windows) is 0 then return "NO_WINDOW"\nreturn "HAS_WINDOW:" & ((count of windows) as string) & ":" & (name of first window)\nend tell\nend tell'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    out = res.stdout.strip()
    return {"process_running": out != "NO_PROCESS" and res.returncode == 0, "window_exists": out.startswith("HAS_WINDOW"), "raw_status": out or res.stderr.strip()}


def focus_gemini_window_win32(hwnd: int) -> bool:
    bind_thread_to_default_desktop_win32()
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)
    if user32.IsIconic(hwnd):
        time.sleep(0.18)
    fg = user32.GetForegroundWindow()
    if int(fg) == int(hwnd):
        return True
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_tid = kernel32.GetCurrentThreadId()
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    att_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True)) if (fg_tid and fg_tid != cur_tid) else False
    att_tgt = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True)) if (tgt_tid and tgt_tid != cur_tid) else False
    try:
        user32.keybd_event(0x12, 0, 0, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
    finally:
        if att_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        if att_tgt:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)
    time.sleep(0.25)
    return int(user32.GetForegroundWindow()) == int(hwnd)


def activate_chromium_accessibility_win32(hwnd: int) -> None:
    if platform.system() != "Windows":
        return
    try:
        bind_thread_to_default_desktop_win32()
        user32 = ctypes.windll.user32
        objid = ctypes.c_ssize_t(-4).value
        res = ctypes.c_size_t()
        user32.SendMessageTimeoutW(hwnd, 0x003D, 0, objid, 2, 500, ctypes.byref(res))
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(ch: int, _lp: int) -> bool:
            user32.SendMessageTimeoutW(ch, 0x003D, 0, objid, 2, 300, ctypes.byref(res))
            return True
        user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
    except Exception:
        pass


def ensure_gemini_desktop_window(timeout_sec: float = 10.0, target_hwnd: Optional[int] = None) -> Dict[str, Any]:
    system = platform.system()
    exe_path = find_gemini_desktop_executable()
    if system == "Windows":
        windows = find_gemini_windows_win32()
        if windows:
            chosen = next((w for w in windows if target_hwnd and int(w["hwnd"]) == int(target_hwnd)), windows[0])
            activate_chromium_accessibility_win32(int(chosen["hwnd"]))
            return {"ok": True, "window": chosen, "windows_count": len(windows), "launched_now": False}
        if exe_path:
            if exe_path.lower().endswith(".lnk"):
                os.startfile(exe_path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen([exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                time.sleep(0.5)
                windows = find_gemini_windows_win32()
                if windows:
                    activate_chromium_accessibility_win32(int(windows[0]["hwnd"]))
                    return {"ok": True, "window": windows[0], "launched_now": True, "exe_path": exe_path}
        user32 = ctypes.windll.user32
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x20, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x20, 0, 2, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        for _ in range(8):
            time.sleep(0.4)
            windows = find_gemini_windows_win32()
            if windows:
                activate_chromium_accessibility_win32(int(windows[0]["hwnd"]))
                return {"ok": True, "window": windows[0], "launched_now": True, "summoned_via": "Alt+Space"}
        return {"ok": False, "error": f"Окно Gemini Desktop не найдено. Путь: {exe_path or 'не найден'}."}
    if system == "Darwin":
        state = check_macos_gemini_window()
        if state["window_exists"]:
            return {"ok": True, "macos_state": state, "launched_now": False}
        subprocess.run(["open", "-a", exe_path or APP_NAME], capture_output=True, text=True)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(0.5)
            state = check_macos_gemini_window()
            if state["window_exists"]:
                return {"ok": True, "macos_state": state, "launched_now": True}
        return {"ok": False, "error": f"Окно '{APP_NAME}' на macOS не найдено ({state['raw_status']})."}
    return {"ok": False, "error": f"ОС {system} не поддерживается."}


def set_clipboard_text(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    if platform.system() == "Windows":
        p = subprocess.Popen(["powershell", "-NoProfile", "-STA", "-Command", "$input | Set-Clipboard"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.communicate(input=text.encode("utf-8"))
        return p.returncode == 0
    if platform.system() == "Darwin":
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(input=text.encode("utf-8"))
        return p.returncode == 0
    return False


def get_clipboard_text() -> str:
    try:
        import pyperclip
        val = pyperclip.paste()
        if isinstance(val, str):
            return val
    except Exception:
        pass
    if platform.system() == "Windows":
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw"], capture_output=True)
        return r.stdout.decode("utf-8", errors="replace").strip("\r\n") if r.returncode == 0 else ""
    if platform.system() == "Darwin":
        r = subprocess.run(["pbpaste"], capture_output=True)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""
    return ""


_POWERSHELL_UIA_SCRIPT = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$dllPath = Join-Path ([System.IO.Path]::GetTempPath()) "ivory_gemini_win32_bridge_v25.dll"
if (-not ("Win32Bridge" -as [type])) {
    if (Test-Path $dllPath) { try { [Reflection.Assembly]::LoadFrom($dllPath) | Out-Null } catch {} }
}
if (-not ("Win32Bridge" -as [type])) {
Add-Type -OutputAssembly $dllPath -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class Win32Bridge {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr OpenDesktopW(string lpszDesktop, uint dwFlags, bool fInherit, uint dwDesiredAccess);
    [DllImport("user32.dll")] public static extern bool SetThreadDesktop(IntPtr hDesktop);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeoutW(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam, uint fuFlags, uint uTimeout, out IntPtr lpdwResult);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, IntPtr dwExtraInfo);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public static void BindDefaultDesktop() {
        try { SetProcessDPIAware(); } catch {}
        IntPtr hDesk = OpenDesktopW("default", 0, false, 0x10000000 | 0x01FF);
        if (hDesk != IntPtr.Zero) { SetThreadDesktop(hDesk); }
    }
    public static void WakeAccessibility(IntPtr hwnd) { IntPtr res; SendMessageTimeoutW(hwnd, 0x003D, IntPtr.Zero, new IntPtr(-4), 2, 400, out res); }
    public static void ClickAt(int x, int y) { SetCursorPos(x, y); System.Threading.Thread.Sleep(50); mouse_event(2, 0, 0, 0, IntPtr.Zero); System.Threading.Thread.Sleep(35); mouse_event(4, 0, 0, 0, IntPtr.Zero); }
    public static void PressEscape() { keybd_event(0x1B, 0, 0, UIntPtr.Zero); System.Threading.Thread.Sleep(35); keybd_event(0x1B, 0, 2, UIntPtr.Zero); }
}
"@
    try { [Reflection.Assembly]::LoadFrom($dllPath) | Out-Null } catch {}
}
[Win32Bridge]::BindDefaultDesktop()
$hwndVal = [int64]$env:GEMINI_TARGET_HWND
$action = $env:GEMINI_UIA_ACTION
$filterText = $env:GEMINI_UIA_FILTER
$maxElements = if ($env:GEMINI_UIA_MAX_ELEMENTS) { [int]$env:GEMINI_UIA_MAX_ELEMENTS } else { 120 }
$maxDepth = if ($env:GEMINI_UIA_MAX_DEPTH) { [int]$env:GEMINI_UIA_MAX_DEPTH } else { 8 }
$interactiveOnly = ($env:GEMINI_UIA_INTERACTIVE_ONLY -eq "1")
$allowMouseFallback = ($env:GEMINI_ALLOW_MOUSE_FALLBACK -ne "0")
$accountQuery = $env:GEMINI_ACCOUNT_QUERY
$accountIndex = if ($env:GEMINI_ACCOUNT_INDEX) { [int]$env:GEMINI_ACCOUNT_INDEX } else { -1 }
$modelQuery = $env:GEMINI_MODEL_QUERY
$hwndPtr = [IntPtr]$hwndVal
[Win32Bridge]::WakeAccessibility($hwndPtr)
$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwndPtr)
if ($null -eq $root) { @{ ok = $false; error = "AutomationElement.FromHandle null for HWND=$hwndVal" } | ConvertTo-Json -Compress; exit 0 }
$winRect = $root.Current.BoundingRectangle
$allNodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
if ($null -eq $allNodes -or $allNodes.Count -lt 5) {
    Start-Sleep -Milliseconds 350
    [Win32Bridge]::WakeAccessibility($hwndPtr)
    $allNodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
}
function Invoke-OrClick($el) {
    if ($null -eq $el) { return $false }
    try { $sc = $el.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern); if ($null -ne $sc) { $sc.ScrollIntoView() } } catch {}
    try { $p = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern); if ($null -ne $p) { $p.Invoke(); return $true } } catch {}
    try { $lp = $el.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern); if ($null -ne $lp) { $lp.DoDefaultAction(); return $true } } catch {}
    try { $sp = $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern); if ($null -ne $sp) { $sp.Select(); return $true } } catch {}
    try { $ep = $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern); if ($null -ne $ep) { $ep.Expand(); return $true } } catch {}
    if (-not $allowMouseFallback) { return $false }
    try {
        $r = $el.Current.BoundingRectangle
        if (-not $r.IsEmpty -and $r.Width -gt 2 -and $r.Height -gt 2) {
            [Win32Bridge]::SetForegroundWindow($hwndPtr) | Out-Null
            [Win32Bridge]::ClickAt([int]($r.Left + $r.Width / 2), [int]($r.Top + $r.Height / 2))
            return $true
        }
    } catch {}
    return $false
}
function Focus-InputElement($el) {
    if ($null -eq $el) { return $false }
    try { $el.SetFocus() } catch {}
    try {
        $r = $el.Current.BoundingRectangle
        if (-not $r.IsEmpty -and $r.Width -gt 4 -and $r.Height -gt 4) {
            [Win32Bridge]::SetForegroundWindow($hwndPtr) | Out-Null
            [Win32Bridge]::ClickAt([int]($r.Left + $r.Width / 2), [int]($r.Top + $r.Height / 2))
            return $true
        }
    } catch {}
    return (Invoke-OrClick $el)
}
function Get-ElementText($el) {
    try { $n = $el.Current.Name; if (-not [string]::IsNullOrWhiteSpace($n)) { return $n.Trim() } } catch {}
    try { $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern); if ($null -ne $vp -and -not [string]::IsNullOrWhiteSpace($vp.Current.Value)) { return $vp.Current.Value.Trim() } } catch {}
    return ""
}
if ($action -eq "inspect_tree") {
    $items = New-Object System.Collections.Generic.List[object]
    $idx = 0
    foreach ($el in $allNodes) {
        try {
            $cType = $el.Current.ControlType.ProgrammaticName -replace "^ControlType\.", ""
            if ($interactiveOnly -and ($cType -notin @("Button", "Edit", "Hyperlink", "MenuItem", "ComboBox", "ListItem", "RadioButton", "CheckBox", "TabItem"))) { continue }
            $name = Get-ElementText $el
            $help = ""; try { $help = $el.Current.HelpText } catch {}
            $aid = $el.Current.AutomationId; $cls = $el.Current.ClassName; $r = $el.Current.BoundingRectangle
            if ([string]::IsNullOrWhiteSpace($name) -and [string]::IsNullOrWhiteSpace($aid) -and [string]::IsNullOrWhiteSpace($help)) { continue }
            if (-not [string]::IsNullOrWhiteSpace($filterText) -and ("$name $help $aid $cType" -notmatch [regex]::Escape($filterText))) { continue }
            $rectObj = if (-not $r.IsEmpty -and $r.Width -gt 0 -and $r.Height -gt 0) { @{ x = [int]$r.Left; y = [int]$r.Top; w = [int]$r.Width; h = [int]$r.Height; cx = [int]($r.Left + $r.Width / 2); cy = [int]($r.Top + $r.Height / 2) } } else { $null }
            $items.Add(@{ index = $idx; type = $cType; name = if ($name.Length -gt 300) { $name.Substring(0, 300) + "..." } else { $name }; help = if ($help.Length -gt 200) { $help.Substring(0, 200) + "..." } else { $help }; automation_id = $aid; class_name = $cls; rect = $rectObj })
            $idx++; if ($items.Count -ge $maxElements) { break }
        } catch {}
    }
    @{ ok = $true; total_nodes = if ($allNodes) { $allNodes.Count } else { 0 }; returned_count = $items.Count; matched_items = $items } | ConvertTo-Json -Depth 5 -Compress
    exit 0
}
$stopButton = $null; $copyResponseButtons = New-Object System.Collections.Generic.List[object]; $accountButton = $null; $activeAccountLabel = ""
$newChatButton = $null; $modelPickerButton = $null; $activeModelLabel = ""; $inputEditElement = $null
$bottomRightActionButtons = New-Object System.Collections.Generic.List[object]; $isThinkingIndicator = $false
$chatRecords = New-Object System.Collections.Generic.List[object]
$noiseExact = @("Gemini", "Google Gemini", "Спросите Gemini", "Ask Gemini", "Введите запрос", "Enter a prompt here", "Отправить", "Send message", "Микрофон", "Use microphone", "Новый чат", "New chat", "Настройки и справка", "Settings and help", "Gemini может выдавать неточную информацию, в том числе о людях, поэтому проверяйте его ответы.", "Gemini — это ИИ. Он может ошибаться.", "— это ИИ. Он может ошибаться", "Gemini can make mistakes, so double-check it", "Gemini is AI and can make mistakes.")
$noiseRegex = "(?i)^(?:Gemini\s*[—\-–:]?\s*это\s+ИИ|—\s*это\s+ИИ|Gemini\s+может\s+ошибаться|Gemini\s+is\s+AI\s+and\s+can\s+make\s+mistakes|Проверяйте\s+важную\s+информацию|Выбор\s+режима,\s*сейчас\s+используется)"
$stopRegex = "(?i)(^(Остановить|Остановить ответ|Остановить генерацию|Остановить создание ответа|Стоп|Прервать|Отменить генерацию|Stop|Stop generating|Stop response|Cancel generation)\b|\b(Остановить ответ|Остановить генерацию|Stop generating|Stop response)\b)"
$copyBtnRegex = "(?i)^(Копировать|Скопировать|Копировать ответ|Скопировать ответ|Copy|Copy response)$"
$modelPickerRegex = "(?i)(Выбор режима|сейчас используется|Mode selector|Choose model|\b(2\.5|3\.0|Flash|Pro|Deep Research|Thinking|Reasoning|Advanced)\b)"
$seq = 0
foreach ($el in $allNodes) {
    $seq++
    try {
        $cType = $el.Current.ControlType.ProgrammaticName -replace "^ControlType\.", ""
        $name = Get-ElementText $el; $help = ""; try { $help = ($el.Current.HelpText).Trim() } catch {}
        $aid = ""; try { $aid = ($el.Current.AutomationId).Trim() } catch {}
        $r = $el.Current.BoundingRectangle; $combined = ("$name $help").Trim()
        $hasRect = (-not $r.IsEmpty -and $r.Width -gt 0 -and $r.Height -gt 0)
        if ($cType -eq "Edit" -and $hasRect) {
            if ($null -eq $inputEditElement -or $r.Top -gt $inputEditElement.Current.BoundingRectangle.Top) { $inputEditElement = $el }
        } elseif ($null -eq $inputEditElement -and $hasRect -and ($combined -match "(?i)(Спросите Gemini|Ask Gemini|Введите запрос|Enter a prompt)") -and $r.Top -gt ($winRect.Top + $winRect.Height * 0.45)) {
            $inputEditElement = $el
        }
        if ([string]::IsNullOrWhiteSpace($combined) -and [string]::IsNullOrWhiteSpace($aid)) { continue }
        if ($cType -in @("Button", "Hyperlink", "MenuItem", "Custom", "Image") -and $null -eq $accountButton) {
            if ($combined -match "(?i)(Аккаунт Google|Google Account|Учетная запись Google)" -or ($combined -match "[\w\.\-]+@[\w\.\-]+\.\w+" -and $hasRect -and $r.Top -lt ($winRect.Top + 150))) {
                $accountButton = $el; $activeAccountLabel = if ($name) { $name } else { $combined }
            }
        }
        if ($cType -in @("Button", "Custom", "Hyperlink", "MenuItem", "ComboBox")) {
            if ((($name -match $stopRegex) -or ($help -match $stopRegex) -or ($combined -match $stopRegex) -or ($aid -match "(?i)(stop[-_]?button|cancel[-_]?gen|stop[-_]?generation)")) -and $hasRect) { $stopButton = $el }
            if ((($name -match $copyBtnRegex) -or ($help -match $copyBtnRegex) -or ($combined -match $copyBtnRegex)) -and ($combined -notmatch "(?i)(код|code|ссылк|link|snippet)")) {
                $copyResponseButtons.Add(@{ el = $el; seq = $seq; y = if ($hasRect) { [int]$r.Top } else { 0 }; x = if ($hasRect) { [int]$r.Left } else { 0 }; label = if ($name) { $name } else { $help } })
            }
            if (($name -match "(?i)^(Новый чат|New chat)\b" -or $help -match "(?i)^(Новый чат|New chat)\b") -and $hasRect -and $null -eq $newChatButton) { $newChatButton = $el }
            if (($name -match $modelPickerRegex -or $help -match $modelPickerRegex) -and $hasRect) {
                if ($null -eq $modelPickerButton -or $combined -match "(?i)(Выбор режима|сейчас используется|Mode selector)") {
                    $modelPickerButton = $el; $activeModelLabel = if ($name) { $name } else { $combined }
                }
            }
            if ($hasRect -and $r.Top -gt ($winRect.Bottom - 220) -and $r.Left -gt ($winRect.Left + $winRect.Width * 0.55)) {
                $bottomRightActionButtons.Add(@{ el = $el; name = $name; help = $help; rect = $r })
            }
        }
        if ($combined -match "(?i)^(Размышляет|Думает|Thinking|Show thinking|Показать размышления|Поиск в|Анализирую|Analyzing|Deep Research in progress|Создаю|Ищу)\b") { $isThinkingIndicator = $true }
        $inSidebar = ($hasRect -and $winRect.Width -gt 750 -and $r.Right -lt ($winRect.Left + 250))
        $inTopHeader = ($hasRect -and $r.Bottom -lt ($winRect.Top + 55))
        if (-not $inSidebar -and -not $inTopHeader -and ($cType -in @("Text", "Document", "Group", "Heading", "ListItem", "Button", "Edit", "Code"))) {
            $chatRecords.Add(@{ seq = $seq; type = $cType; name = $name; help = $help; y = if ($hasRect) { [int]$r.Top } else { -1 }; x = if ($hasRect) { [int]$r.Left } else { -1 } })
        }
    } catch {}
}
if ($action -eq "focus_input") {
    if ($null -ne $inputEditElement) {
        $focused = Focus-InputElement $inputEditElement; $r = $inputEditElement.Current.BoundingRectangle
        @{ ok = [bool]$focused; method = "uia_input_edit"; rect = if (-not $r.IsEmpty) { @{ x = [int]$r.Left; y = [int]$r.Top; w = [int]$r.Width; h = [int]$r.Height } } else { $null } } | ConvertTo-Json -Depth 4 -Compress
    } elseif (-not $winRect.IsEmpty -and $winRect.Width -gt 200) {
        $cx = [int]($winRect.Left + $winRect.Width * 0.52); $cy = [int]($winRect.Bottom - 95)
        [Win32Bridge]::SetForegroundWindow($hwndPtr) | Out-Null; [Win32Bridge]::ClickAt($cx, $cy)
        @{ ok = $true; method = "coord_bottom_input_fallback"; cx = $cx; cy = $cy } | ConvertTo-Json -Compress
    } else { @{ ok = $false; error = "Поле ввода не найдено" } | ConvertTo-Json -Compress }
    exit 0
}
if ($action -eq "stop_generation") {
    if ($null -ne $stopButton) { @{ ok = $true; stopped = [bool](Invoke-OrClick $stopButton); method = "uia_stop_button" } | ConvertTo-Json -Compress; exit 0 }
    if ($isThinkingIndicator -and $bottomRightActionButtons.Count -gt 0) {
        $cand = $bottomRightActionButtons[$bottomRightActionButtons.Count - 1]
        if ($cand.name -notmatch "(?i)(Микрофон|Use microphone|Выбор режима)") { @{ ok = $true; stopped = [bool](Invoke-OrClick $cand.el); method = "thinking_bottom_right_stop_button" } | ConvertTo-Json -Compress; exit 0 }
    }
    if ($isThinkingIndicator) { [Win32Bridge]::SetForegroundWindow($hwndPtr) | Out-Null; [Win32Bridge]::PressEscape(); @{ ok = $true; stopped = $true; method = "thinking_escape_hotkey" } | ConvertTo-Json -Compress; exit 0 }
    @{ ok = $true; stopped = $false; info = "Генерация уже завершена." } | ConvertTo-Json -Compress; exit 0
}
if ($action -eq "new_chat") {
    if ($null -ne $newChatButton) {
        $clicked = Invoke-OrClick $newChatButton; Start-Sleep -Milliseconds 350
        $inputFocused = if ($null -ne $inputEditElement) { Focus-InputElement $inputEditElement } else { $false }
        @{ ok = $true; clicked = [bool]$clicked; input_focused = [bool]$inputFocused; method = "uia_new_chat_button" } | ConvertTo-Json -Compress
    } else { @{ ok = $false; error = "Кнопка 'Новый чат' не найдена" } | ConvertTo-Json -Compress }
    exit 0
}
if ($action -eq "select_model") {
    $q = ($modelQuery).Trim().ToLower()
    if ([string]::IsNullOrWhiteSpace($q)) { @{ ok = $false; error = "Не указан model_query." } | ConvertTo-Json -Compress; exit 0 }
    if ($null -ne $modelPickerButton) { Invoke-OrClick $modelPickerButton | Out-Null; Start-Sleep -Milliseconds 750; $allNodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) }
    $matchedEl = $null; $matchedName = ""
    foreach ($el in $allNodes) {
        try {
            $cType = $el.Current.ControlType.ProgrammaticName -replace "^ControlType\.", ""
            $nm = Get-ElementText $el; $hlp = ""; try { $hlp = ($el.Current.HelpText).Trim() } catch {}
            $full = ("$nm $hlp").Trim(); $r = $el.Current.BoundingRectangle
            if ($cType -in @("MenuItem", "ListItem", "Button", "RadioButton", "Custom", "Text") -and -not [string]::IsNullOrWhiteSpace($full) -and -not $r.IsEmpty) {
                if ($full -match "(?i)^Выбор режима,\s*сейчас используется") { continue }
                if ($full.ToLower().Contains($q)) { $matchedEl = $el; $matchedName = if ($nm) { $nm } else { $full }; if ($cType -in @("MenuItem", "ListItem", "RadioButton", "Button")) { break } }
            }
        } catch {}
    }
    if ($null -ne $matchedEl) {
        $clicked = Invoke-OrClick $matchedEl; Start-Sleep -Milliseconds 300
        if ($null -ne $inputEditElement) { Focus-InputElement $inputEditElement | Out-Null }
        @{ ok = [bool]$clicked; selected_model = $matchedName; previous_picker_label = $activeModelLabel } | ConvertTo-Json -Compress
    } else { [Win32Bridge]::PressEscape(); @{ ok = $false; error = "Модель '$modelQuery' не найдена."; current_picker_label = $activeModelLabel } | ConvertTo-Json -Compress }
    exit 0
}
if ($action -eq "click_last_copy") {
    if ($copyResponseButtons.Count -gt 0) {
        $lastCopy = $copyResponseButtons[$copyResponseButtons.Count - 1]
        @{ ok = [bool](Invoke-OrClick $lastCopy.el); copy_buttons_count = $copyResponseButtons.Count; label = $lastCopy.label; seq = $lastCopy.seq } | ConvertTo-Json -Compress
    } else { @{ ok = $false; copy_buttons_count = 0; error = "Кнопка копирования не найдена" } | ConvertTo-Json -Compress }
    exit 0
}
if ($action -eq "list_accounts" -or $action -eq "switch_account") {
    if ($null -eq $accountButton) {
        foreach ($el in $allNodes) {
            try {
                $cType = $el.Current.ControlType.ProgrammaticName -replace "^ControlType\.", ""; $r = $el.Current.BoundingRectangle
                if ($cType -in @("Button", "Hyperlink", "Image", "Custom") -and -not $r.IsEmpty -and $r.Top -ge $winRect.Top -and $r.Top -lt ($winRect.Top + 125) -and $r.Right -gt ($winRect.Right - 220) -and $r.Width -ge 20 -and $r.Width -le 72) {
                    $nm = Get-ElementText $el
                    if ($nm -notmatch "(?i)(Свернуть|Развернуть|Закрыть|Minimize|Maximize|Close)") { $accountButton = $el; $activeAccountLabel = $nm; break }
                }
            } catch {}
        }
    }
    $openedMenu = $false
    if ($null -ne $accountButton -and ($env:GEMINI_OPEN_ACCOUNT_MENU -eq "1" -or $action -eq "switch_account")) {
        $openedMenu = Invoke-OrClick $accountButton; Start-Sleep -Milliseconds 1200
        $allNodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($el in $allNodes) {
            try {
                if ((Get-ElementText $el) -match "(?i)^(Сменить аккаунт|Другие аккаунты|Показать другие аккаунты|Switch account|Show more accounts)$") {
                    Invoke-OrClick $el | Out-Null; Start-Sleep -Milliseconds 800
                    $allNodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition); break
                }
            } catch {}
        }
    }
    $accounts = New-Object System.Collections.Generic.List[object]; $seenEmails = @{}; $emailRegex = "([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"
    $currentEmail = if ($activeAccountLabel -match $emailRegex) { $Matches[1].ToLower() } else { "" }
    foreach ($el in $allNodes) {
        try {
            $cType = $el.Current.ControlType.ProgrammaticName -replace "^ControlType\.", ""
            $nm = Get-ElementText $el; $hlp = ""; try { $hlp = $el.Current.HelpText } catch {}
            $full = ("$nm $hlp").Trim()
            if ($full -match $emailRegex) {
                $em = $Matches[1].ToLower()
                if (-not $seenEmails.ContainsKey($em)) {
                    $r = $el.Current.BoundingRectangle
                    if (-not $r.IsEmpty -and $r.Width -gt 5 -and $r.Height -gt 5) {
                        $isActive = ($em -eq $currentEmail) -or ($full -match "(?i)(Аккаунт Google:|Google Account:|Используется|Default)")
                        if ([string]::IsNullOrWhiteSpace($currentEmail) -and $accounts.Count -eq 0) { $currentEmail = $em; $isActive = $true }
                        $seenEmails[$em] = $true; $slotIdx = $accounts.Count
                        $accounts.Add(@{ index = $slotIdx; slot_id = "slot_$slotIdx"; email = $em; label = $full; type = $cType; is_active = [bool]$isActive; el = $el; rect = @{ x = [int]$r.Left; y = [int]$r.Top; w = [int]$r.Width; h = [int]$r.Height } })
                    }
                } elseif ($cType -in @("Button", "Hyperlink", "ListItem", "MenuItem")) {
                    foreach ($acc in $accounts) { if ($acc.email -eq $em -and $acc.type -eq "Text") { $acc.el = $el; $acc.type = $cType } }
                }
            }
        } catch {}
    }
    if ($action -eq "list_accounts") {
        $clean = New-Object System.Collections.Generic.List[object]
        foreach ($a in $accounts) { $clean.Add(@{ index = $a.index; slot_id = $a.slot_id; email = $a.email; label = $a.label; is_active = $a.is_active; rect = $a.rect }) }
        @{ ok = $true; active_account_badge = $activeAccountLabel; active_email = $currentEmail; accounts_count = $clean.Count; accounts = $clean; menu_opened = $openedMenu } | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }
    if ($accounts.Count -eq 0) { @{ ok = $false; error = "Список аккаунтов не найден."; active_account_badge = $activeAccountLabel } | ConvertTo-Json -Compress; exit 0 }
    $targetAcc = $null
    if ($accountIndex -ge 0 -and $accountIndex -lt $accounts.Count) { $targetAcc = $accounts[$accountIndex] }
    elseif (-not [string]::IsNullOrWhiteSpace($accountQuery)) {
        $q = $accountQuery.ToLower().Trim()
        if ($q -match "^slot[_-]?(\d+)$" -and [int]$Matches[1] -lt $accounts.Count) { $targetAcc = $accounts[[int]$Matches[1]] }
        if ($null -eq $targetAcc) { foreach ($a in $accounts) { if ($a.email.ToLower().Contains($q) -or $a.label.ToLower().Contains($q)) { $targetAcc = $a; break } } }
    } else {
        foreach ($a in $accounts) { if (-not $a.is_active -and $a.email -ne $currentEmail) { $targetAcc = $a; break } }
        if ($null -eq $targetAcc) { $targetAcc = if ($accounts.Count -gt 1) { $accounts[1] } else { $accounts[0] } }
    }
    if ($null -eq $targetAcc) { @{ ok = $false; error = "Слот аккаунта не найден." } | ConvertTo-Json -Compress; exit 0 }
    @{ ok = [bool](Invoke-OrClick $targetAcc.el); status = if ($targetAcc.email -eq $currentEmail -and $accounts.Count -eq 1) { "single_account_active" } else { "switched" }; previous_email = $currentEmail; target_account = @{ index = $targetAcc.index; slot_id = $targetAcc.slot_id; email = $targetAcc.email; label = $targetAcc.label }; available_accounts_count = $accounts.Count } | ConvertTo-Json -Depth 5 -Compress
    exit 0
}
$userTurnMarkers = "(?i)^(Вы сказали|You said|Ваш запрос|User)\b"
$modelTurnMarkers = "(?i)^(Ответ Gemini|Gemini сказал|Gemini said|Gemini)\b"
$actionToolbarMarkers = "(?i)^(Нравится|Не нравится|Хороший ответ|Плохой ответ|Good response|Bad response|Копировать|Скопировать|Копировать ответ|Copy|Copy response|Поделиться|Share|Прослушать|Listen|Ещё|More)$"
$turns = New-Object System.Collections.Generic.List[object]; $currentChunks = New-Object System.Collections.Generic.List[string]; $currentRole = "unknown"; $lastAddedText = ""
foreach ($rec in $chatRecords) {
    $t = $rec.type; $nm = ($rec.name).Trim()
    if ([string]::IsNullOrWhiteSpace($nm) -or ($nm -in $noiseExact) -or ($nm -match $noiseRegex) -or ($t -eq "Edit")) { continue }
    if ($nm -match $userTurnMarkers) {
        if ($currentChunks.Count -gt 0) { $turns.Add(@{ role = $currentRole; text = ($currentChunks -join "`n").Trim() }); $currentChunks = New-Object System.Collections.Generic.List[string] }
        $currentRole = "user"; $cleaned = ($nm -replace $userTurnMarkers, "").Trim()
        if ($cleaned -and ($cleaned -notin $noiseExact) -and ($cleaned -notmatch $noiseRegex)) { $currentChunks.Add($cleaned); $lastAddedText = $cleaned }
        continue
    }
    if ($nm -match $modelTurnMarkers) {
        if ($currentChunks.Count -gt 0) { $turns.Add(@{ role = $currentRole; text = ($currentChunks -join "`n").Trim() }); $currentChunks = New-Object System.Collections.Generic.List[string] }
        $currentRole = "assistant"; $cleaned = ($nm -replace $modelTurnMarkers, "").Trim()
        if ($cleaned -and ($cleaned -notin $noiseExact) -and ($cleaned -notmatch $noiseRegex)) { $currentChunks.Add($cleaned); $lastAddedText = $cleaned }
        continue
    }
    if ($t -eq "Button" -and $nm -match $actionToolbarMarkers) {
        if ($currentChunks.Count -gt 0) { $turns.Add(@{ role = "assistant"; text = ($currentChunks -join "`n").Trim() }); $currentChunks = New-Object System.Collections.Generic.List[string]; $currentRole = "unknown"; $lastAddedText = "" }
        continue
    }
    if ($t -eq "Button") { continue }
    if ($t -in @("Text", "Document", "Heading", "ListItem", "Code", "Group") -and $nm -ne $lastAddedText) {
        if ($lastAddedText -and $lastAddedText.Contains($nm)) { continue }
        if ($lastAddedText -and $nm.Contains($lastAddedText) -and $currentChunks.Count -gt 0) { $currentChunks[$currentChunks.Count - 1] = $nm } else { $currentChunks.Add($nm) }
        $lastAddedText = $nm
    }
}
if ($currentChunks.Count -gt 0) { $turns.Add(@{ role = if ($currentRole -eq "unknown") { "assistant" } else { $currentRole }; text = ($currentChunks -join "`n").Trim() }) }
$lastAssistantText = ""
for ($i = $turns.Count - 1; $i -ge 0; $i--) { if ($turns[$i].role -eq "assistant" -and -not [string]::IsNullOrWhiteSpace($turns[$i].text)) { $lastAssistantText = $turns[$i].text; break } }
if ([string]::IsNullOrWhiteSpace($lastAssistantText) -and $turns.Count -gt 0) { $lastAssistantText = $turns[$turns.Count - 1].text }
@{ ok = $true; is_generating = ($null -ne $stopButton) -or [bool]$isThinkingIndicator; is_thinking = [bool]$isThinkingIndicator; has_stop_button = ($null -ne $stopButton); copy_buttons_count = $copyResponseButtons.Count; active_account_badge = $activeAccountLabel; active_model_badge = $activeModelLabel; turns_count = $turns.Count; turns = $turns; last_response = $lastAssistantText } | ConvertTo-Json -Depth 6 -Compress
"""

_SCRIPT_HASH = hashlib.sha256(_POWERSHELL_UIA_SCRIPT.encode("utf-8")).hexdigest()[:12]
_CACHED_PS1_PATH: Optional[str] = None
_PS1_INIT_LOCK = threading.Lock()


def _ensure_uia_ps1_script() -> str:
    global _CACHED_PS1_PATH
    with _PS1_INIT_LOCK:
        if _CACHED_PS1_PATH and os.path.exists(_CACHED_PS1_PATH):
            return _CACHED_PS1_PATH
        target_path = os.path.join(tempfile.gettempdir(), f"gemini_desktop_uia_v25_{_SCRIPT_HASH}.ps1")
        if not os.path.exists(target_path):
            tmp_path = f"{target_path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp_path, "w", encoding="utf-8-sig") as f:
                f.write(_POWERSHELL_UIA_SCRIPT)
            try:
                os.replace(tmp_path, target_path)
            except OSError:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        _CACHED_PS1_PATH = target_path
        return target_path


def _sanitize_account_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return data
    for k in ("active_account_badge", "active_email", "previous_email"):
        if isinstance(data.get(k), str):
            data[k] = mask_email_address(data[k])
    if isinstance(data.get("target_account"), dict):
        ta = dict(data["target_account"])
        for k in ("email", "label"):
            if k in ta:
                ta[k] = mask_email_address(str(ta[k]))
        data["target_account"] = ta
    for lk in ("accounts", "available_accounts"):
        if isinstance(data.get(lk), list):
            out = []
            for idx, item in enumerate(data[lk]):
                if isinstance(item, dict):
                    e = dict(item)
                    e["slot_id"] = e.get("slot_id") or f"slot_{int(e.get('index', idx))}"
                    for k in ("email", "label"):
                        if k in e:
                            e[k] = mask_email_address(str(e[k]))
                    out.append(e)
                else:
                    out.append(item)
            data[lk] = out
    return data


def _run_windows_uia_action(
    hwnd: int, action: str = "read_chat", filter_text: str = "", max_depth: int = 8,
    max_elements: int = 120, interactive_only: bool = False, account_query: str = "",
    account_index: int = -1, open_account_menu: bool = False, model_query: str = "", timeout_sec: float = 15.0,
) -> Dict[str, Any]:
    if platform.system() != "Windows":
        return {"ok": False, "error": "Windows UIA доступен только на ОС Windows."}
    activate_chromium_accessibility_win32(hwnd)
    resolved_query = account_query.strip() or ENV_ACCOUNT_SLOTS.get(account_index, "")
    env = os.environ.copy()
    env.update({
        "GEMINI_TARGET_HWND": str(int(hwnd)), "GEMINI_UIA_ACTION": action, "GEMINI_UIA_FILTER": filter_text,
        "GEMINI_UIA_MAX_DEPTH": str(max(1, int(max_depth))), "GEMINI_UIA_MAX_ELEMENTS": str(max(1, int(max_elements))),
        "GEMINI_UIA_INTERACTIVE_ONLY": "1" if interactive_only else "0",
        "GEMINI_ALLOW_MOUSE_FALLBACK": "0" if action == "click_last_copy" else "1",
        "GEMINI_ACCOUNT_QUERY": resolved_query, "GEMINI_ACCOUNT_INDEX": str(int(account_index)),
        "GEMINI_OPEN_ACCOUNT_MENU": "1" if open_account_menu else "0", "GEMINI_MODEL_QUERY": model_query.strip(),
    })
    try:
        proc = subprocess.Popen(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", _ensure_uia_ps1_script()], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        stdout_b, stderr_b = proc.communicate(timeout=timeout_sec)
        raw_out = stdout_b.decode("utf-8", errors="replace").strip()
        if not raw_out:
            return {"ok": False, "error": f"Пустой ответ UIA (код {proc.returncode}): {stderr_b.decode('utf-8', errors='replace').strip()}"}
        lines = [ln.strip() for ln in raw_out.splitlines() if ln.strip().startswith("{")]
        parsed = json.loads(lines[-1] if lines else raw_out)
        if isinstance(parsed.get("last_response"), str):
            parsed["last_response"] = clean_extracted_response(parsed["last_response"])
        if isinstance(parsed.get("turns"), list):
            cleaned_turns = [{"role": t.get("role", "assistant"), "text": clean_extracted_response(str(t.get("text", "")))} for t in parsed["turns"] if isinstance(t, dict) and clean_extracted_response(str(t.get("text", "")))]
            parsed["turns"] = cleaned_turns
            parsed["turns_count"] = len(cleaned_turns)
        return _sanitize_account_payload(parsed)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": f"Таймаут запроса UIA ({timeout_sec} сек)."}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка Windows UIA: {exc}"}


def _read_macos_uia_chat() -> Dict[str, Any]:
    script = f'tell application "System Events"\nif not (exists process "{APP_NAME}") then return "ERR:NO_PROCESS"\ntell process "{APP_NAME}"\nif (count of windows) is 0 then return "ERR:NO_WINDOW"\nset allTexts to value of every static text of entire contents of first window\nset outStr to ""\nrepeat with t in allTexts\nif t is not missing value and (length of (t as string)) > 0 then set outStr to outStr & (t as string) & "\\n---CHUNK---\\n"\nend repeat\nreturn outStr\nend tell\nend tell'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=12)
    out = res.stdout.strip()
    if out.startswith("ERR:") or res.returncode != 0:
        return {"ok": False, "error": out or res.stderr.strip()}
    chunks = [clean_extracted_response(c) for c in out.split("---CHUNK---") if clean_extracted_response(c)]
    is_think = any(re.search(r"^(Размышляет|Думает|Thinking|Analyzing|Deep Research)", c, re.I) for c in chunks)
    is_gen = is_think or any(re.search(r"^(Остановить|Остановить ответ|Стоп|Stop|Stop generating)$", c, re.I) for c in chunks)
    return {"ok": True, "is_generating": is_gen, "is_thinking": is_think, "turns_count": len(chunks), "turns": [{"role": "assistant", "text": c} for c in chunks[-15:]], "last_response": chunks[-1] if chunks else ""}


def extract_clean_response_via_copy_button_win32(hwnd: int, fallback_text: str = "") -> Tuple[str, str]:
    with _WINDOW_ACTION_LOCK:
        sentinel = f"__GEMINI_MCP_CLIP_SENTINEL_{int(time.time() * 1000)}__"
        prev_clip = get_clipboard_text()
        set_clipboard_text(sentinel)
        res = _run_windows_uia_action(hwnd=hwnd, action="click_last_copy", timeout_sec=10.0)
        if res.get("ok"):
            for _ in range(8):
                time.sleep(0.15)
                clip_now = get_clipboard_text()
                if clip_now and clip_now != sentinel:
                    cleaned = clean_extracted_response(clip_now)
                    if prev_clip and prev_clip != sentinel:
                        set_clipboard_text(prev_clip)
                    return cleaned, "uia_copy_button_clipboard"
        if prev_clip and prev_clip != sentinel:
            set_clipboard_text(prev_clip)
        return clean_extracted_response(fallback_text), "uia_tree_text"


def poll_and_read_gemini_response(
    wait_until_complete: bool = True, timeout_sec: int = DEFAULT_WAIT_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC, stable_sec: float = DEFAULT_STABLE_SEC,
    prefer_clipboard_copy: bool = True, baseline_text: Optional[str] = None, prompt_sent: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_RESPONSE_CHARS, offset: int = 0,
    progress_cb: Optional[Callable[[float, float, str], None]] = None,
) -> Dict[str, Any]:
    ensured = ensure_gemini_desktop_window()
    if not ensured.get("ok"):
        return ensured
    system = platform.system()
    hwnd = int(ensured["window"]["hwnd"]) if system == "Windows" else 0
    state = _get_task_state()
    baseline_text = state.get("baseline_response", "") if baseline_text is None else baseline_text
    prompt_sent = state.get("prompt", "") if prompt_sent is None else prompt_sent
    baseline_hash = _hash_text(baseline_text) if baseline_text else ""
    prompt_hash = _hash_text(prompt_sent) if prompt_sent else ""

    def _snap() -> Dict[str, Any]:
        return _run_windows_uia_action(hwnd=hwnd, action="read_chat", timeout_sec=12.0) if system == "Windows" else (_read_macos_uia_chat() if system == "Darwin" else {"ok": False, "error": f"ОС {system} не поддерживается"})

    if not wait_until_complete:
        s = _snap()
        if not s.get("ok"):
            return s
        raw = clean_extracted_response(s.get("last_response") or "")
        is_gen, is_think = bool(s.get("is_generating")), bool(s.get("is_thinking"))
        src = "uia_tree_text"
        if not is_gen and prefer_clipboard_copy and system == "Windows" and s.get("copy_buttons_count", 0) > 0:
            raw, src = extract_clean_response_via_copy_button_win32(hwnd, fallback_text=raw)
        st = "thinking" if is_think else ("streaming" if is_gen else "completed")
        _update_task_state(status=st, is_generating=is_gen, last_partial_response=raw, last_completed_response=raw if st == "completed" else state.get("last_completed_response", ""), active_account=s.get("active_account_badge") or state.get("active_account"))
        paged, meta = apply_text_pagination(raw, max_chars=max_chars, offset=offset)
        return {"ok": True, "status": st, "is_generating": is_gen, "is_thinking": is_think, "source": src, "response": paged, "char_count": len(raw), "active_account": s.get("active_account_badge"), "active_model": s.get("active_model_badge"), **meta}

    start_ts = time.time()
    deadline = start_ts + max(1, int(timeout_sec))
    last_text, last_change_ts = "", start_ts
    saw_activity = False
    last_copy_cnt = -1
    init_copy_cnt: Optional[int] = None
    init_turns_cnt: Optional[int] = None

    while time.time() < deadline:
        s = _snap()
        now = time.time()
        if not s.get("ok"):
            time.sleep(poll_interval_sec)
            continue
        cur_text = clean_extracted_response(s.get("last_response") or "")
        cur_hash = _hash_text(cur_text) if cur_text else ""
        is_gen, is_think = bool(s.get("is_generating")), bool(s.get("is_thinking"))
        copy_cnt, turns_cnt = int(s.get("copy_buttons_count", 0)), int(s.get("turns_count", 0))
        if init_copy_cnt is None:
            init_copy_cnt = copy_cnt
        if init_turns_cnt is None:
            init_turns_cnt = turns_cnt
        is_new_text = bool(cur_text and cur_hash != baseline_hash and cur_hash != prompt_hash)
        if is_gen or is_think or is_new_text:
            saw_activity = True
        if cur_text != last_text or copy_cnt != last_copy_cnt:
            last_text, last_copy_cnt, last_change_ts = cur_text, copy_cnt, now
        cur_st = "thinking" if is_think else ("streaming" if is_gen else ("waiting_for_start" if not saw_activity else "stabilizing"))
        _update_task_state(status=cur_st, is_generating=is_gen or is_think, last_partial_response=cur_text, active_account=s.get("active_account_badge") or state.get("active_account"))
        if progress_cb:
            try:
                progress_cb(min(round(now - start_ts, 1), float(timeout_sec)), float(timeout_sec), f"{cur_st}: {len(cur_text)} chars")
            except Exception:
                pass
        has_candidate = is_new_text or (not baseline_hash and bool(cur_text)) or (init_copy_cnt is not None and copy_cnt > init_copy_cnt) or (init_turns_cnt is not None and turns_cnt > init_turns_cnt and bool(cur_text))
        if not is_gen and not is_think and has_candidate:
            req_stable = 2.0 if (init_copy_cnt is not None and copy_cnt > init_copy_cnt) else stable_sec
            if (now - last_change_ts) >= req_stable:
                final_txt, src = cur_text, "uia_tree_text"
                if prefer_clipboard_copy and system == "Windows" and copy_cnt > 0:
                    copied, c_src = extract_clean_response_via_copy_button_win32(hwnd, fallback_text=cur_text)
                    if copied and _hash_text(copied) != baseline_hash:
                        final_txt, src = copied, c_src
                _update_task_state(status="completed", is_generating=False, last_partial_response=final_txt, last_completed_response=final_txt, active_account=s.get("active_account_badge") or state.get("active_account"))
                paged, meta = apply_text_pagination(final_txt, max_chars=max_chars, offset=offset)
                return {"ok": True, "status": "completed", "is_generating": False, "source": src, "elapsed_sec": round(now - start_ts, 1), "char_count": len(final_txt), "active_account": s.get("active_account_badge"), "active_model": s.get("active_model_badge"), "response": paged, **meta}
        time.sleep(poll_interval_sec)

    elapsed = round(time.time() - start_ts, 1)
    still_gen = bool(_get_task_state().get("is_generating"))
    paged, meta = apply_text_pagination(last_text, max_chars=max_chars, offset=offset)
    return {
        "ok": True, "status": "streaming" if still_gen else "timeout_partial", "is_generating": still_gen,
        "elapsed_sec": elapsed, "char_count": len(last_text), "partial_response": paged, "response": paged,
        "next_action_hint": "Вызовите wait_for_gemini_response(timeout_sec=180) для продолжения ожидания.", **meta,
    }


def _send_to_verified_window_windows(prompt: str, new_chat: bool = False) -> Dict[str, Any]:
    with _WINDOW_ACTION_LOCK:
        ensured = ensure_gemini_desktop_window()
        if not ensured.get("ok"):
            return ensured
        win_info = ensured["window"]
        hwnd = int(win_info["hwnd"])
        pre_snap = _run_windows_uia_action(hwnd=hwnd, action="read_chat", timeout_sec=8.0)
        baseline_resp = (pre_snap.get("last_response") or "").strip() if pre_snap.get("ok") else ""
        if not focus_gemini_window_win32(hwnd):
            return {"ok": False, "error": f"Windows заблокировала перевод фокуса на HWND={hwnd}.", "window": win_info}
        user32 = ctypes.windll.user32
        if new_chat:
            nc_res = _run_windows_uia_action(hwnd=hwnd, action="new_chat", timeout_sec=8.0)
            if not nc_res.get("ok"):
                for vk, fl in ((0x11, 0), (0x10, 0), (0x4F, 0), (0x4F, 2), (0x10, 2), (0x11, 2)):
                    user32.keybd_event(vk, 0, fl, 0)
                    time.sleep(0.02)
            time.sleep(0.45)
            baseline_resp = ""
        input_focus_res = _run_windows_uia_action(hwnd=hwnd, action="focus_input", timeout_sec=6.0)
        time.sleep(0.12)
        prev_user_clip = get_clipboard_text()
        if not set_clipboard_text(prompt):
            return {"ok": False, "error": "Не удалось скопировать промпт в буфер обмена."}
        if int(user32.GetForegroundWindow()) != hwnd and not focus_gemini_window_win32(hwnd):
            if prev_user_clip:
                set_clipboard_text(prev_user_clip)
            return {"ok": False, "error": "Окно потеряло фокус перед вставкой текста."}
        for vk, fl in ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)):
            user32.keybd_event(vk, 0, fl, 0)
            time.sleep(0.03)
        time.sleep(0.35)
        user32.keybd_event(0x0D, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x0D, 0, 2, 0)
        if prev_user_clip and prev_user_clip != prompt:
            time.sleep(0.12)
            set_clipboard_text(prev_user_clip)
        cur_state = _get_task_state()
        _update_task_state(task_id=int(cur_state.get("task_id", 0)) + 1, prompt=prompt, sent_at=time.time(), baseline_response=baseline_resp, baseline_hash=_hash_text(baseline_resp) if baseline_resp else "", last_partial_response="", status="waiting_for_start", is_generating=True, window_hwnd=hwnd, active_account=pre_snap.get("active_account_badge") if pre_snap.get("ok") else None)
        return {"ok": True, "mode": "gemini_desktop_win32_uia", "status": "sent", "window": win_info, "input_focused": bool(input_focus_res.get("ok")), "input_focus_method": input_focus_res.get("method"), "baseline_captured": bool(baseline_resp), "launched_now": ensured.get("launched_now", False)}


def _send_to_verified_window_macos(prompt: str, new_chat: bool = False) -> Dict[str, Any]:
    with _WINDOW_ACTION_LOCK:
        ensured = ensure_gemini_desktop_window()
        if not ensured.get("ok"):
            return ensured
        pre_snap = _read_macos_uia_chat()
        baseline_resp = (pre_snap.get("last_response") or "").strip() if pre_snap.get("ok") else ""
        prev_clip = get_clipboard_text()
        set_clipboard_text(prompt)
        nc = 'keystroke "o" using {command down, shift down}\ndelay 0.5' if new_chat else ""
        script = f'tell application "{APP_NAME}" to activate\ndelay 0.4\ntell application "System Events"\ntell process "{APP_NAME}"\nset frontmost to true\ndelay 0.2\n{nc}\nkeystroke "v" using {{command down}}\ndelay 0.3\nkey code 36\nend tell\nend tell'
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if prev_clip and prev_clip != prompt:
            set_clipboard_text(prev_clip)
        if res.returncode != 0:
            return {"ok": False, "mode": "gemini_desktop_macos", "error": res.stderr.strip()}
        cur_state = _get_task_state()
        _update_task_state(task_id=int(cur_state.get("task_id", 0)) + 1, prompt=prompt, sent_at=time.time(), baseline_response="" if new_chat else baseline_resp, baseline_hash="" if new_chat else (_hash_text(baseline_resp) if baseline_resp else ""), last_partial_response="", status="waiting_for_start", is_generating=True)
        return {"ok": True, "mode": "gemini_desktop_macos", "status": "sent", "launched_now": ensured.get("launched_now", False)}


def sync_check_status() -> Dict[str, Any]:
    system, exe_path, task_state = platform.system(), find_gemini_desktop_executable(), _get_task_state()
    if system == "Windows":
        windows = find_gemini_windows_win32()
        snap = _run_windows_uia_action(hwnd=int(windows[0]["hwnd"]), action="read_chat", timeout_sec=6.0) if windows else {}
        return {
            "ok": True, "platform": "Windows", "server_version": SERVER_VERSION, "gemini_desktop_exe": exe_path,
            "window_exists": len(windows) > 0, "windows_found": windows,
            "active_account": snap.get("active_account_badge") if snap.get("ok") else None,
            "active_account_slot": task_state.get("active_account_slot"),
            "active_model": snap.get("active_model_badge") if snap.get("ok") else None,
            "email_privacy_masking": MASK_EMAILS_DEFAULT and not EXPOSE_RAW_EMAILS,
            "configured_env_slots": list(ENV_ACCOUNT_SLOTS.keys()),
            "is_generating": bool(snap.get("is_generating")) if snap.get("ok") else False,
            "last_task_state": {"task_id": task_state["task_id"], "status": task_state["status"], "elapsed_sec": task_state["elapsed_sec"]},
        }
    if system == "Darwin":
        ms = check_macos_gemini_window()
        return {"ok": True, "platform": "macOS", "server_version": SERVER_VERSION, "gemini_desktop_app": exe_path, "window_exists": ms["window_exists"], "process_running": ms["process_running"], "email_privacy_masking": MASK_EMAILS_DEFAULT and not EXPOSE_RAW_EMAILS, "last_task_state": {"task_id": task_state["task_id"], "status": task_state["status"], "elapsed_sec": task_state["elapsed_sec"]}}
    return {"ok": True, "platform": system, "server_version": SERVER_VERSION, "gemini_desktop_exe": exe_path, "window_exists": False, "email_privacy_masking": MASK_EMAILS_DEFAULT and not EXPOSE_RAW_EMAILS}


def sync_launch_gemini() -> Dict[str, Any]:
    return ensure_gemini_desktop_window(timeout_sec=10.0)


def sync_focus_window(hwnd: Optional[int] = None, window_index: int = 0) -> Dict[str, Any]:
    ensured = ensure_gemini_desktop_window(target_hwnd=hwnd)
    if not ensured.get("ok"):
        return ensured
    if platform.system() == "Windows":
        windows = find_gemini_windows_win32()
        if not windows:
            return {"ok": False, "error": "Окна Gemini Desktop не найдены."}
        target = next((w for w in windows if hwnd and int(w["hwnd"]) == int(hwnd)), windows[window_index] if 0 <= window_index < len(windows) else windows[0])
        focused = focus_gemini_window_win32(int(target["hwnd"]))
        return {"ok": focused, "focused": focused, "window": target, "all_windows": windows}
    if platform.system() == "Darwin":
        subprocess.run(["osascript", "-e", f'tell application "{APP_NAME}" to activate'], capture_output=True)
        return {"ok": True, "focused": True}
    return {"ok": False, "error": "ОС не поддерживается."}


def sync_send_task(
    prompt: str, wait_for_response: bool = True, timeout_sec: int = DEFAULT_WAIT_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC, stable_sec: float = DEFAULT_STABLE_SEC,
    new_chat: bool = False, prefer_clipboard_copy: bool = True, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
    progress_cb: Optional[Callable[[float, float, str], None]] = None,
) -> Dict[str, Any]:
    system = platform.system()
    send_res = _send_to_verified_window_windows(prompt=prompt, new_chat=new_chat) if system == "Windows" else (_send_to_verified_window_macos(prompt=prompt, new_chat=new_chat) if system == "Darwin" else {"ok": False, "error": f"ОС {system} не поддерживается"})
    if not send_res.get("ok") or not wait_for_response:
        return send_res
    time.sleep(1.0)
    state = _get_task_state()
    wait_res = poll_and_read_gemini_response(wait_until_complete=True, timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec, stable_sec=stable_sec, prefer_clipboard_copy=prefer_clipboard_copy, baseline_text=state.get("baseline_response", ""), prompt_sent=prompt, max_chars=max_chars, progress_cb=progress_cb)
    wait_res["window"] = send_res.get("window")
    wait_res["prompt_sent"] = prompt
    return wait_res


def sync_read_last_response(wait_until_complete: bool = True, timeout_sec: int = 120, stable_sec: float = 3.5, prefer_clipboard_copy: bool = True, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS, offset: int = 0) -> Dict[str, Any]:
    return poll_and_read_gemini_response(wait_until_complete=wait_until_complete, timeout_sec=timeout_sec, poll_interval_sec=DEFAULT_POLL_INTERVAL_SEC, stable_sec=stable_sec, prefer_clipboard_copy=prefer_clipboard_copy, max_chars=max_chars, offset=offset)


def sync_wait_for_response(timeout_sec: int = 300, poll_interval_sec: float = 2.0, stable_sec: float = 4.0, prefer_clipboard_copy: bool = True, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS, progress_cb: Optional[Callable[[float, float, str], None]] = None) -> Dict[str, Any]:
    state = _get_task_state()
    return poll_and_read_gemini_response(wait_until_complete=True, timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec, stable_sec=stable_sec, prefer_clipboard_copy=prefer_clipboard_copy, baseline_text=state.get("baseline_response", ""), prompt_sent=state.get("prompt", ""), max_chars=max_chars, progress_cb=progress_cb)


def sync_get_generation_status() -> Dict[str, Any]:
    snap_res = poll_and_read_gemini_response(wait_until_complete=False, prefer_clipboard_copy=False)
    state = _get_task_state()
    snap_res["task_id"] = state.get("task_id")
    snap_res["elapsed_sec"] = state.get("elapsed_sec")
    return snap_res


def sync_stop_generation() -> Dict[str, Any]:
    ensured = ensure_gemini_desktop_window()
    if not ensured.get("ok"):
        return ensured
    if platform.system() == "Windows":
        res = _run_windows_uia_action(hwnd=int(ensured["window"]["hwnd"]), action="stop_generation", timeout_sec=8.0)
        if res.get("stopped"):
            _update_task_state(status="stopped", is_generating=False)
        return res
    if platform.system() == "Darwin":
        subprocess.run(["osascript", "-e", f'tell application "{APP_NAME}" to activate\ndelay 0.15\ntell application "System Events" to key code 53'], capture_output=True)
        _update_task_state(status="stopped", is_generating=False)
        return {"ok": True, "stopped": True, "method": "macos_escape_key"}
    return {"ok": False, "error": "ОС не поддерживается."}


def sync_read_chat_history(max_turns: int = 20, limit: Optional[int] = None, max_chars_per_turn: int = 0) -> Dict[str, Any]:
    ensured = ensure_gemini_desktop_window()
    if not ensured.get("ok"):
        return ensured
    snap = _run_windows_uia_action(hwnd=int(ensured["window"]["hwnd"]), action="read_chat", timeout_sec=12.0) if platform.system() == "Windows" else (_read_macos_uia_chat() if platform.system() == "Darwin" else {"ok": False, "error": "ОС не поддерживается"})
    if not snap.get("ok"):
        return snap
    eff = int(limit) if (limit is not None and int(limit) > 0) else int(max_turns)
    turns = (snap.get("turns") or [])[-eff:] if eff > 0 else (snap.get("turns") or [])
    if max_chars_per_turn and max_chars_per_turn > 0:
        turns = [{"role": t.get("role", "assistant"), "text": (str(t.get("text", ""))[:max_chars_per_turn] + "... [truncated]") if len(str(t.get("text", ""))) > max_chars_per_turn else str(t.get("text", ""))} for t in turns]
    return {"ok": True, "turns_count": len(turns), "is_generating": bool(snap.get("is_generating")), "active_account": snap.get("active_account_badge"), "turns": turns, "last_response": snap.get("last_response", "")}


def sync_start_new_chat() -> Dict[str, Any]:
    with _WINDOW_ACTION_LOCK:
        ensured = ensure_gemini_desktop_window()
        if not ensured.get("ok"):
            return ensured
        if platform.system() == "Windows":
            hwnd = int(ensured["window"]["hwnd"])
            focus_gemini_window_win32(hwnd)
            res = _run_windows_uia_action(hwnd=hwnd, action="new_chat", timeout_sec=8.0)
            if not res.get("ok"):
                user32 = ctypes.windll.user32
                for vk, fl in ((0x11, 0), (0x10, 0), (0x4F, 0), (0x4F, 2), (0x10, 2), (0x11, 2)):
                    user32.keybd_event(vk, 0, fl, 0)
                    time.sleep(0.02)
                time.sleep(0.35)
                f_res = _run_windows_uia_action(hwnd=hwnd, action="focus_input", timeout_sec=6.0)
                res = {"ok": True, "method": "hotkey_ctrl_shift_o", "input_focused": bool(f_res.get("ok"))}
            _update_task_state(baseline_response="", baseline_hash="", last_partial_response="", status="idle", is_generating=False)
            return res
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e", f'tell application "{APP_NAME}" to activate\ndelay 0.2\ntell application "System Events" to keystroke "o" using {{command down, shift down}}'], capture_output=True)
            _update_task_state(baseline_response="", baseline_hash="", last_partial_response="", status="idle", is_generating=False)
            return {"ok": True, "method": "hotkey_cmd_shift_o"}
        return {"ok": False, "error": "ОС не поддерживается."}


def sync_list_accounts(open_menu: bool = True) -> Dict[str, Any]:
    with _WINDOW_ACTION_LOCK:
        ensured = ensure_gemini_desktop_window()
        if not ensured.get("ok"):
            return ensured
        if platform.system() != "Windows":
            return {"ok": False, "error": "Список аккаунтов через UIA поддерживается на Windows."}
        hwnd = int(ensured["window"]["hwnd"])
        focus_gemini_window_win32(hwnd)
        res = _run_windows_uia_action(hwnd=hwnd, action="list_accounts", open_account_menu=open_menu, timeout_sec=15.0)
        if open_menu and res.get("menu_opened"):
            try:
                ctypes.windll.user32.keybd_event(0x1B, 0, 0, 0)
                time.sleep(0.05)
                ctypes.windll.user32.keybd_event(0x1B, 0, 2, 0)
            except Exception:
                pass
        return res


def sync_switch_account(account_query: str = "", account_index: int = -1) -> Dict[str, Any]:
    with _WINDOW_ACTION_LOCK:
        ensured = ensure_gemini_desktop_window()
        if not ensured.get("ok"):
            return ensured
        if platform.system() != "Windows":
            return {"ok": False, "error": "Переключение аккаунтов через UIA поддерживается на Windows."}
        hwnd = int(ensured["window"]["hwnd"])
        focus_gemini_window_win32(hwnd)
        res = _run_windows_uia_action(hwnd=hwnd, action="switch_account", account_query=account_query, account_index=account_index, open_account_menu=True, timeout_sec=18.0)
        if not res.get("ok"):
            return res
        time.sleep(2.5)
        windows = find_gemini_windows_win32()
        new_hwnd = int(windows[0]["hwnd"]) if windows else hwnd
        focus_gemini_window_win32(new_hwnd)
        v_snap = _run_windows_uia_action(hwnd=new_hwnd, action="read_chat", timeout_sec=10.0)
        new_badge = v_snap.get("active_account_badge", "") if v_snap.get("ok") else ""
        slot_id = res.get("target_account", {}).get("slot_id")
        _update_task_state(baseline_response="", baseline_hash="", last_partial_response="", status="idle", is_generating=False, window_hwnd=new_hwnd, active_account=new_badge or res.get("target_account", {}).get("email"), active_account_slot=slot_id)
        res["verified_active_account_badge"] = new_badge
        res["window_hwnd"] = new_hwnd
        return res


def sync_select_model(model_query: str) -> Dict[str, Any]:
    with _WINDOW_ACTION_LOCK:
        ensured = ensure_gemini_desktop_window()
        if not ensured.get("ok"):
            return ensured
        if platform.system() != "Windows":
            return {"ok": False, "error": "Переключение модели через UIA поддерживается на Windows."}
        hwnd = int(ensured["window"]["hwnd"])
        focus_gemini_window_win32(hwnd)
        return _run_windows_uia_action(hwnd=hwnd, action="select_model", model_query=model_query, timeout_sec=12.0)


def sync_inspect_ui_tree(filter_text: str = "", max_depth: int = 8, max_elements: int = 120, interactive_only: bool = False) -> Dict[str, Any]:
    ensured = ensure_gemini_desktop_window()
    if not ensured.get("ok"):
        return ensured
    if platform.system() != "Windows":
        return {"ok": False, "error": "Инспекция UIA доступна только на Windows."}
    res = _run_windows_uia_action(hwnd=int(ensured["window"]["hwnd"]), action="inspect_tree", filter_text=filter_text, max_depth=max_depth, max_elements=max_elements, interactive_only=interactive_only, timeout_sec=12.0)
    res["requested_max_depth"] = max_depth
    return res


def sync_capture_window_screenshot(output_path: str = "", include_base64: bool = False) -> Dict[str, Any]:
    ensured = ensure_gemini_desktop_window()
    if not ensured.get("ok"):
        return ensured
    target_file = output_path.strip() or os.path.join(tempfile.gettempdir(), f"gemini_desktop_window_{int(time.time())}.png")
    system = platform.system()
    if system == "Windows":
        win = ensured["window"]
        focus_gemini_window_win32(int(win["hwnd"]))
        r = win.get("rect") or {}
        left, top, width, height = int(r.get("left", 0)), int(r.get("top", 0)), max(100, int(r.get("width", 1024))), max(100, int(r.get("height", 768)))
        ps_cmd = f"Add-Type -AssemblyName System.Drawing; $b = New-Object System.Drawing.Bitmap({width}, {height}); $g = [System.Drawing.Graphics]::FromImage($b); $g.CopyFromScreen({left}, {top}, 0, 0, $b.Size); $b.Save('{target_file.replace(chr(39), chr(39)*2)}', [System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $b.Dispose();"
        res = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps_cmd], capture_output=True, text=True, timeout=10)
        if res.returncode != 0 or not os.path.exists(target_file):
            return {"ok": False, "error": f"Не удалось сохранить скриншот: {res.stderr.strip()}"}
        out: Dict[str, Any] = {"ok": True, "file_path": target_file, "window_rect": {"left": left, "top": top, "width": width, "height": height}}
        if include_base64:
            with open(target_file, "rb") as f:
                out["base64_png"] = base64.b64encode(f.read()).decode("ascii")
        return out
    if system == "Darwin":
        res = subprocess.run(["screencapture", "-x", target_file], capture_output=True, text=True, timeout=10)
        if res.returncode != 0 or not os.path.exists(target_file):
            return {"ok": False, "error": f"Ошибка screencapture: {res.stderr.strip()}"}
        out = {"ok": True, "file_path": target_file}
        if include_base64:
            with open(target_file, "rb") as f:
                out["base64_png"] = base64.b64encode(f.read()).decode("ascii")
        return out
    return {"ok": False, "error": f"ОС {system} не поддерживается."}


def _load_tools_schema() -> List[Dict[str, Any]]:
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools_schema.json")
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                return data["tools"]
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}} for n in (
        "check_gemini_bridge_status", "launch_gemini_desktop", "focus_gemini_window", "send_task_to_gemini",
        "read_last_gemini_response", "wait_for_gemini_response", "get_gemini_generation_status",
        "stop_gemini_generation", "read_gemini_chat_history", "start_new_gemini_chat", "list_gemini_accounts",
        "switch_gemini_account", "select_gemini_model", "inspect_gemini_ui_tree", "capture_gemini_window_screenshot",
    )]


TOOLS_SCHEMA: List[Dict[str, Any]] = _load_tools_schema()


def _dispatch_tool_call(t_name: str, t_args: Dict[str, Any], progress_cb: Optional[Callable[[float, float, str], None]] = None) -> Dict[str, Any]:
    if t_name == "check_gemini_bridge_status":
        return sync_check_status()
    if t_name == "launch_gemini_desktop":
        return sync_launch_gemini()
    if t_name == "focus_gemini_window":
        h = int(t_args.get("hwnd", 0) or 0)
        return sync_focus_window(hwnd=h if h > 0 else None, window_index=int(t_args.get("window_index", 0) or 0))
    if t_name == "send_task_to_gemini":
        return sync_send_task(
            prompt=str(t_args.get("prompt", "")), wait_for_response=bool(t_args.get("wait_for_response", True)),
            timeout_sec=int(t_args.get("timeout_sec", DEFAULT_WAIT_TIMEOUT_SEC)), poll_interval_sec=float(t_args.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC)),
            stable_sec=float(t_args.get("stable_sec", DEFAULT_STABLE_SEC)), new_chat=bool(t_args.get("new_chat", False)),
            prefer_clipboard_copy=bool(t_args.get("prefer_clipboard_copy", True)), max_chars=int(t_args.get("max_chars", DEFAULT_MAX_RESPONSE_CHARS) or 0),
            progress_cb=progress_cb,
        )
    if t_name == "read_last_gemini_response":
        return sync_read_last_response(
            wait_until_complete=bool(t_args.get("wait_until_complete", True)), timeout_sec=int(t_args.get("timeout_sec", 120)),
            stable_sec=float(t_args.get("stable_sec", 3.5)), prefer_clipboard_copy=bool(t_args.get("prefer_clipboard_copy", True)),
            max_chars=int(t_args.get("max_chars", DEFAULT_MAX_RESPONSE_CHARS) or 0), offset=int(t_args.get("offset", 0) or 0),
        )
    if t_name == "wait_for_gemini_response":
        return sync_wait_for_response(
            timeout_sec=int(t_args.get("timeout_sec", 300)), poll_interval_sec=float(t_args.get("poll_interval_sec", 2.0)),
            stable_sec=float(t_args.get("stable_sec", 4.0)), prefer_clipboard_copy=bool(t_args.get("prefer_clipboard_copy", True)),
            max_chars=int(t_args.get("max_chars", DEFAULT_MAX_RESPONSE_CHARS) or 0), progress_cb=progress_cb,
        )
    if t_name == "get_gemini_generation_status":
        return sync_get_generation_status()
    if t_name == "stop_gemini_generation":
        return sync_stop_generation()
    if t_name == "read_gemini_chat_history":
        lim = t_args.get("limit")
        return sync_read_chat_history(max_turns=int(t_args.get("max_turns", 20) or 20), limit=int(lim) if lim is not None else None, max_chars_per_turn=int(t_args.get("max_chars_per_turn", 0) or 0))
    if t_name == "start_new_gemini_chat":
        return sync_start_new_chat()
    if t_name == "list_gemini_accounts":
        return sync_list_accounts(open_menu=bool(t_args.get("open_menu", True)))
    if t_name == "switch_gemini_account":
        return sync_switch_account(account_query=str(t_args.get("account_query", "")), account_index=int(t_args.get("account_index", -1)))
    if t_name == "select_gemini_model":
        return sync_select_model(model_query=str(t_args.get("model_query", "")))
    if t_name == "inspect_gemini_ui_tree":
        return sync_inspect_ui_tree(filter_text=str(t_args.get("filter_text", "")), max_depth=int(t_args.get("max_depth", 8) or 8), max_elements=int(t_args.get("max_elements", 120) or 120), interactive_only=bool(t_args.get("interactive_only", False)))
    if t_name == "capture_gemini_window_screenshot":
        return sync_capture_window_screenshot(output_path=str(t_args.get("output_path", "")), include_base64=bool(t_args.get("include_base64", False)))
    return {"ok": False, "error": f"Неизвестный инструмент: {t_name}"}


try:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP(SERVER_NAME)

    @mcp.tool()
    def check_gemini_bridge_status() -> str:
        return json.dumps(sync_check_status(), ensure_ascii=False, indent=2)

    @mcp.tool()
    def launch_gemini_desktop() -> str:
        return json.dumps(sync_launch_gemini(), ensure_ascii=False, indent=2)

    @mcp.tool()
    def focus_gemini_window(hwnd: int = 0, window_index: int = 0) -> str:
        return json.dumps(sync_focus_window(hwnd=hwnd if hwnd > 0 else None, window_index=window_index), ensure_ascii=False, indent=2)

    @mcp.tool()
    def send_task_to_gemini(prompt: str, wait_for_response: bool = True, timeout_sec: int = DEFAULT_WAIT_TIMEOUT_SEC, poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC, stable_sec: float = DEFAULT_STABLE_SEC, new_chat: bool = False, prefer_clipboard_copy: bool = True, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS) -> str:
        return json.dumps(sync_send_task(prompt=prompt, wait_for_response=wait_for_response, timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec, stable_sec=stable_sec, new_chat=new_chat, prefer_clipboard_copy=prefer_clipboard_copy, max_chars=max_chars), ensure_ascii=False, indent=2)

    @mcp.tool()
    def read_last_gemini_response(wait_until_complete: bool = True, timeout_sec: int = 120, stable_sec: float = 3.5, prefer_clipboard_copy: bool = True, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS, offset: int = 0) -> str:
        return json.dumps(sync_read_last_response(wait_until_complete=wait_until_complete, timeout_sec=timeout_sec, stable_sec=stable_sec, prefer_clipboard_copy=prefer_clipboard_copy, max_chars=max_chars, offset=offset), ensure_ascii=False, indent=2)

    @mcp.tool()
    def wait_for_gemini_response(timeout_sec: int = 300, poll_interval_sec: float = 2.0, stable_sec: float = 4.0, prefer_clipboard_copy: bool = True, max_chars: int = DEFAULT_MAX_RESPONSE_CHARS) -> str:
        return json.dumps(sync_wait_for_response(timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec, stable_sec=stable_sec, prefer_clipboard_copy=prefer_clipboard_copy, max_chars=max_chars), ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_gemini_generation_status() -> str:
        return json.dumps(sync_get_generation_status(), ensure_ascii=False, indent=2)

    @mcp.tool()
    def stop_gemini_generation() -> str:
        return json.dumps(sync_stop_generation(), ensure_ascii=False, indent=2)

    @mcp.tool()
    def read_gemini_chat_history(max_turns: int = 20, limit: Optional[int] = None, max_chars_per_turn: int = 0) -> str:
        return json.dumps(sync_read_chat_history(max_turns=max_turns, limit=limit, max_chars_per_turn=max_chars_per_turn), ensure_ascii=False, indent=2)

    @mcp.tool()
    def start_new_gemini_chat() -> str:
        return json.dumps(sync_start_new_chat(), ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_gemini_accounts(open_menu: bool = True) -> str:
        return json.dumps(sync_list_accounts(open_menu=open_menu), ensure_ascii=False, indent=2)

    @mcp.tool()
    def switch_gemini_account(account_query: str = "", account_index: int = -1) -> str:
        return json.dumps(sync_switch_account(account_query=account_query, account_index=account_index), ensure_ascii=False, indent=2)

    @mcp.tool()
    def select_gemini_model(model_query: str) -> str:
        return json.dumps(sync_select_model(model_query=model_query), ensure_ascii=False, indent=2)

    @mcp.tool()
    def inspect_gemini_ui_tree(filter_text: str = "", max_depth: int = 8, max_elements: int = 120, interactive_only: bool = False) -> str:
        return json.dumps(sync_inspect_ui_tree(filter_text=filter_text, max_depth=max_depth, max_elements=max_elements, interactive_only=interactive_only), ensure_ascii=False, indent=2)

    @mcp.tool()
    def capture_gemini_window_screenshot(output_path: str = "", include_base64: bool = False) -> str:
        return json.dumps(sync_capture_window_screenshot(output_path=output_path, include_base64=include_base64), ensure_ascii=False, indent=2)

    _HAS_FASTMCP = True
except ImportError:
    _HAS_FASTMCP = False


def _run_stdlib_mcp_loop() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as exc:
            _log_stderr(f"JSON parse error: {exc}")
            continue

        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if msg_id is None:
            continue

        if method in ("initialize", "server/discover"):
            resp = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS_SCHEMA}}
        elif method == "tools/call":
            t_name = str(params.get("name") or "")
            raw_args = params.get("arguments") or {}
            t_args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args if isinstance(raw_args, dict) else {})
            meta = params.get("_meta") or {}
            prog_token = meta.get("progressToken")

            def _emit_progress(progress: float, total: float, message: str) -> None:
                if prog_token is not None:
                    notif = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": prog_token, "progress": progress, "total": total, "message": message}}
                    sys.stdout.write(json.dumps(notif, ensure_ascii=False) + "\n")
                    sys.stdout.flush()

            try:
                data = _dispatch_tool_call(t_name, t_args, progress_cb=_emit_progress if prog_token is not None else None)
            except Exception as exc:
                data = {"ok": False, "error": f"Внутренняя ошибка {t_name}: {exc}"}

            resp = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                    "isError": not bool(data.get("ok", False)),
                },
            }
        elif method in ("resources/list", "prompts/list"):
            key = "resources" if "resources" in method else "prompts"
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}
        else:
            resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if _HAS_FASTMCP:
        mcp.run(transport="stdio")
    else:
        _run_stdlib_mcp_loop()
