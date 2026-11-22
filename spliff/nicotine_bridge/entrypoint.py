"""Installed as the Nicotine+ plugin's __init__.py."""
import sys
from types import ModuleType

# Nicotine+ 3.3.11 executes user plugins without registering their package.
if __name__ not in sys.modules:
    package = ModuleType(__name__)
    package.__path__ = __path__
    package.__file__ = __file__
    sys.modules[__name__] = package

from .plugin import Plugin

__all__ = ["Plugin"]
