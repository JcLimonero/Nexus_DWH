import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(CLIENT_DIR)
for p in (CLIENT_DIR, os.path.join(ROOT, "dwh_back", "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)
