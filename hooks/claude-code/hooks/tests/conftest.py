import sys
from pathlib import Path

hooks_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(hooks_dir))

langstash_deliver_dir = hooks_dir.parent.parent / "langstash-deliver" / "python"
if langstash_deliver_dir.exists():
    sys.path.insert(0, str(langstash_deliver_dir))
