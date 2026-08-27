import os
import sys

# make project root importable (so `import analyze`, `import make_mock_data` work)
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
