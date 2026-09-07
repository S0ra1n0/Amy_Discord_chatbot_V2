"""Live DuckDuckGo check. Thin wrapper so the runner's network tier picks it up."""
import os
import runpy
import sys

sys.argv = [sys.argv[0], "--network"]
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_websearch.py"),
               run_name="__main__")
