import sys, tempfile
from pathlib import Path
sys.path.insert(0, 'tests'); sys.path.insert(0, '.')
from test_assistant import make_config
from assistant.actions import execute_step
from assistant.planner import Step
root = Path(tempfile.mkdtemp())
cfg = make_config(root)
for name, params in [('system_info', {}), ('list_processes', {'sort': 'cpu'}), ('list_processes', {'sort': 'memory'}), ('volume', {'action': 'get'}), ('clipboard_get', {}), ('screenshot', {})]:
    try:
        r = execute_step(Step(name, params), cfg)
        print(name, params, 'ok=', r.ok, '|', (r.output or '')[:400].replace('\n', ' / '))
    except Exception as e:
        print(name, 'EXC', type(e).__name__, str(e)[:300])
