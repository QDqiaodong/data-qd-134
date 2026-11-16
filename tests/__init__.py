"""Make ``python -m unittest discover -s tests`` work without installing.

The package is importable from ``src`` in editable layouts; installed layouts
find it on ``sys.path`` already, so this is a harmless no-op there.
"""
import sys
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]/'src'
if SOURCE.is_dir() and str(SOURCE) not in sys.path:sys.path.insert(0,str(SOURCE))
