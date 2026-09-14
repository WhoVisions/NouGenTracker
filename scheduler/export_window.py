"""Export a date window into a SIDE directory, never the published dailies/.

Usage (from the tracker root): python scheduler/export_window.py OUT_DIR START END
Patches fleet_dailies.DAILIES_DIR before token_tracker runs, so export_days
cannot rewrite archived days even if the window filter misbehaves.
"""
import runpy, sys
from pathlib import Path

out, start, end = sys.argv[1:4]
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
import fleet_dailies
fleet_dailies.DAILIES_DIR = Path(out)
sys.argv = [str(root / "token_tracker.py"), "--start", start, "--end", end, "--export"]
runpy.run_path(sys.argv[0], run_name="__main__")
