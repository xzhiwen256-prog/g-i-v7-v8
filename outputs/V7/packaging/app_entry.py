"""Application entry for the isolated bundled Python search path."""
from pathlib import Path
import os
import runpy
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'app'))
os.environ['TCL_LIBRARY'] = str(ROOT/'runtime/tcl/tcl8.6')
os.environ['TK_LIBRARY'] = str(ROOT/'runtime/tcl/tk8.6')
runpy.run_path(str(ROOT/'app/preview.py'), run_name='__main__')
