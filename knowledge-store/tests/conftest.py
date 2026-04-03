"""Add knowledge-store to sys.path for test imports."""

import os
import sys

# Add knowledge-store/ to Python path so `import store` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
