"""Moi nhiem vu web chay tren luong rieng. Thu vien uiautomation giu _AutomationClient la singleton TOAN TIEN TRINH,
tao o luong dau (gan voi COM apartment cua luong do). Luong do thoat -> CoUninitialize -> con tro chet; nhiem vu sau dung lai
=> access violation 0xc0000005 trong _ctypes.pyd (do that: server crash luc bat dau nhiem vu thu 2).
Giai phap: moi luong tu co client rieng, va bo di khi luong ket thuc."""
import contextlib, gc


@contextlib.contextmanager
def uia_thread(ua):
    """ua: module uiautomation. Dung trong CHINH luong chay nhiem vu."""
    mod = getattr(ua, "uiautomation", ua)
    client = getattr(mod, "_AutomationClient", None)
    if client is not None:
        client._instance = None               # luong nay se tao client moi trong apartment cua no
    init = ua.UIAutomationInitializerInThread()
    try:
        yield
    finally:
        try:
            if client is not None:
                client._instance = None       # tha con tro IUIAutomation cua luong nay TRUOC khi dong COM
            gc.collect()                      # (do that: Release sau CoUninitialize => access violation, stack faulthandler)
        finally:
            init.Uninitialize()
