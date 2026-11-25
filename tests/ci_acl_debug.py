"""CI only: show why the ACL script fails on a runner."""
import base64, os, subprocess, sys, tempfile, pathlib
sys.path.insert(0, os.getcwd())
from assistant import token_store as ts
print("python", sys.version)
sid = ts._current_user_sid()
print("sid", sid)
d = pathlib.Path(tempfile.mkdtemp(prefix="acl dbg "))
f = d / "t.txt"; f.write_text("x")
env = dict(os.environ, TPA_ACL_PATH=str(f), TPA_ACL_SID=sid or "", TPA_ACL_DIR="0")
enc = base64.b64encode(ts._ACL_APPLY_SCRIPT.encode("utf-16-le")).decode()
p = subprocess.run(["powershell","-NoProfile","-NonInteractive","-EncodedCommand",enc],capture_output=True,text=True,encoding="utf-8",errors="replace",env=env,timeout=60)
print("rc", p.returncode); print("stdout", p.stdout[:800]); import re
print("errors", re.findall(r'<S S="Error">(.*?)</S>', p.stderr)[:12])
print("lock_down", ts._lock_down(f))
