"""Built-in runtime adapters. Importing registers them on RuntimeAdapter._registry."""
from . import custom_example, goose, kagent  # noqa: F401  (side-effect: register)
from .goose import GooseAdapter
from .kagent import KagentAdapter

__all__ = ["KagentAdapter", "GooseAdapter"]
