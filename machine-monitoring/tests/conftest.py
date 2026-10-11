import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "services" / "collector", ROOT / "services" / "simulator", ROOT / "services" / "api"):
    sys.path.insert(0, str(p))

CONFIG_DIR = ROOT / "machine-config"
SCHEMA = CONFIG_DIR / "schema" / "machine.schema.json"
