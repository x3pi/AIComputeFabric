import sys, json
sys.path.insert(0, r"C:\wagent\src")
from wagent.web import list_models
for url, key in (("http://10.42.0.1:18090/v1", ""), ("http://10.42.0.1:18080/v1", "sai"), ("file:///C:/Windows/win.ini", "")):
    try: print(url, "->", list_models(url, key))
    except Exception as e: print(url, "-> LOI:", type(e).__name__, str(e)[:90])
