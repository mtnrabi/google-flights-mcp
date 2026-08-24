import sys
from pathlib import Path

# Tests import `src.*`, so the package root has to be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Unlike the free server, load_settings() has no required variables: this
# process holds no upstream credential, because the caller supplies their own
# key per request. So there is nothing to seed here, and that absence is
# itself worth asserting -- see test_settings.py.
