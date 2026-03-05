from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ''):
    # Allow running as: python apps/web/run_live_site.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

import uvicorn


def main() -> int:
    uvicorn.run('apps.web.live_site:app', host='127.0.0.1', port=8780, reload=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
