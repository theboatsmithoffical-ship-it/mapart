"""Capture real per-tab screenshots from a live GUI instance (temporary tool)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tkinter as tk
from firefly.mapart.gui import GuiApp

root = tk.Tk()
app = GuiApp(root)
root.geometry("1400x860+40+20")
root.update_idletasks(); root.update()
time.sleep(1.2)
# find the notebook tab list
nb = None
for w in root.winfo_children():
    if isinstance(w, __import__("tkinter").ttk.Notebook):
        nb = w
names = [nb.tab(i, "text") for i in range(nb.index("end"))]
def snap(idx, name):
    nb.select(idx)
    root.update_idletasks(); root.update()
    time.sleep(0.7)
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    safe = "".join(c if c.isalnum() else "_" for c in name)[:24]
    p = Path("screenshots") / f"tab_{idx:02d}_{safe}.png"
    p.parent.mkdir(exist_ok=True)
    img.save(p)
    print("saved", p, img.size)
for i, n in enumerate(names):
    snap(i, n)
root.destroy()
