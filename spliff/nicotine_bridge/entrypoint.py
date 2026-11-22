"""Installed as the Nicotine+ plugin's __init__.py."""
import sys
from types import ModuleType

# Nicotine+ 3.3.11 loads this entrypoint outside the import registry.
# Clear our owned submodules so a plugin toggle also loads upgraded source.
for child in ("plugin", "downloads", "wire"):
    sys.modules.pop(f"{__name__}.{child}", None)
package = ModuleType(__name__)
package.__path__ = __path__
package.__file__ = __file__
sys.modules[__name__] = package

from .plugin import Plugin

__all__ = ["Plugin"]
