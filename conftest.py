import sys
from pathlib import Path

# Ensure the project root is on sys.path so sibling packages
# (cache, tools, auth, …) are importable from within sub-packages
# like actions/ without relying on installation or PYTHONPATH.
sys.path.insert(0, str(Path(__file__).parent))
