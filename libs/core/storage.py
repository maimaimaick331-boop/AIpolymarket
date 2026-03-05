from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any



def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)



def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')



def append_jsonl(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    with path.open('a', encoding='utf-8') as fp:
        fp.write(json.dumps(payload, ensure_ascii=False))
        fp.write('\n')



def utc_now_slug() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
