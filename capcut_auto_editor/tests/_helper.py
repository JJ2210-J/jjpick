"""테스트 공통 준비 — 프로젝트 루트를 import 경로에 넣습니다."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
