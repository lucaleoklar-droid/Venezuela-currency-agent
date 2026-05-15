"""Make the project root importable so tests can use the same module paths as
production code (`from scrapers.parallel_scraper import parse_rate`)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
