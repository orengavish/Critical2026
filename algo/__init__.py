import sys
from pathlib import Path

# Ensure project root (parent of algo/) is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
