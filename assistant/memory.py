"""Local, explicit personal memory. Never writes anything until an approved action."""
from __future__ import annotations
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
NAME_RE = re.compile(r'^[\w\- ]{1,40}$', re.UNICODE)


def language_for(text: str, fallback: str = 'en') -> str:
    if re.search(r'[\u0600-\u06ff]', text):
        return 'ar'
    if re.search(r'[A-Za-z]', text):
        return 'en'
    return fallback if fallback in {'ar', 'en'} else 'en'


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {'users': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('users'), dict):
                raise ValueError('bad format')
            return data
        except (ValueError, OSError) as exc:
            raise ValueError('Memory file cannot be read; it was not overwritten.') from exc

    def get(self, user_id: int) -> dict[str, Any]:
        with _LOCK:
            data = self._read()['users'].get(str(user_id), {})
            if not isinstance(data, dict):
                raise ValueError('Memory data is invalid.')
            # JSON round trip gives the caller an independent snapshot.
            return json.loads(json.dumps(data, ensure_ascii=False))

    def mutate(self, user_id: int, op: str, value: str = '', name: str = '', steps: str = '') -> str:
        with _LOCK:
            data = self._read()
            user = data['users'].setdefault(str(user_id), {})
            notes = user.setdefault('notes', [])
            macros = user.setdefault('macros', {})
            if op == 'add_note':
                if not value.strip() or len(value) > 1000:
                    raise ValueError('Note must contain 1–1000 characters.')
                if len(notes) >= 100:
                    raise ValueError('Note limit reached (100).')
                notes.append(value.strip())
                result = f'Note saved as #{len(notes)}.'
            elif op == 'forget_note':
                if not value.isdigit() or not 1 <= int(value) <= len(notes):
                    raise ValueError('Note number does not exist.')
                if notes[int(value) - 1] != steps:
                    raise ValueError('That note changed since the plan was proposed; retry /forget.')
                notes.pop(int(value) - 1)
                result = f'Note #{value} deleted; remaining note numbers may change.'
            elif op == 'forget_language':
                user.pop('language', None)
                result = 'Language preference removed.'
            elif op == 'forget_all':
                data['users'][str(user_id)] = {}
                result = 'All notes, preferences and macros were removed; scheduled jobs are unchanged.'
            elif op == 'set_language':
                if value not in {'ar', 'en'}:
                    raise ValueError('Language must be ar or en.')
                user['language'] = value
                result = 'Language preference saved.' if value == 'en' else 'تم حفظ اللغة المفضلة.'
            elif op == 'save_macro':
                if not NAME_RE.fullmatch(name) or not steps:
                    raise ValueError('Invalid macro name or empty steps.')
                parsed = json.loads(steps)
                if not isinstance(parsed, list) or not parsed:
                    raise ValueError('Macro steps must be a non-empty list.')
                if len(macros) >= 30 and name not in macros:
                    raise ValueError('Macro limit reached (30).')
                macros[name] = parsed
                result = f'Macro {name} saved. Running it always requires a new approval.'
            elif op == 'delete_macro':
                if name not in macros:
                    raise ValueError('Macro not found.')
                del macros[name]
                result = f'Macro {name} deleted.'
            else:
                raise ValueError('Unknown memory operation.')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.json.tmp')
            try:
                with temp.open('w', encoding='utf-8') as file:
                    json.dump(data, file, ensure_ascii=False, indent=2)
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
                os.replace(temp, self.path)
            finally:
                temp.unlink(missing_ok=True)
            return result
