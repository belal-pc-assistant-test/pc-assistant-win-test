"""Visible on-screen capture indicator (separate process, never hidden)."""
from __future__ import annotations
import sys


def main() -> int:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title('SCREEN SHARING ACTIVE')
        root.configure(background='#b00020')
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        width, height = 320, 70
        x = max(0, root.winfo_screenwidth() - width - 15)
        root.geometry(f'{width}x{height}+{x}+15')
        tk.Label(root, text='●  SCREEN SHARING ACTIVE', bg='#b00020', fg='white',
                 font=('Segoe UI', 12, 'bold')).pack(pady=(5, 2))
        tk.Button(root, text='Stop screen sharing', command=root.destroy,
                  bg='white', fg='black').pack(pady=(0, 5))
        root.update_idletasks()
        root.update()

        def heartbeat() -> None:
            try:
                if not root.winfo_viewable() or not root.attributes('-topmost'):
                    print('HIDDEN', flush=True)
                    root.destroy()
                    return
                print('VISIBLE', flush=True)
                root.after(500, heartbeat)
            except tk.TclError:
                pass

        if not root.winfo_viewable() or not root.attributes('-topmost'):
            root.destroy()
            return 1
        print('READY', flush=True)
        root.after(500, heartbeat)
        root.mainloop()
        return 0
    except Exception:
        # Never report sensitive screen content or exception details.
        return 1


if __name__ == '__main__':
    sys.exit(main())
