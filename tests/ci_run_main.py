"""CI only: the GitHub runner account is an administrator, which the assistant refuses.
Pretend it is not, so the rest of the program can be exercised. Not shipped."""
import os, runpy, sys
sys.path.insert(0, os.getcwd())
from assistant import elevation
elevation.is_administrator = lambda *a, **k: False
sys.argv = ["main.py"] + sys.argv[1:]
runpy.run_path("main.py", run_name="__main__")
