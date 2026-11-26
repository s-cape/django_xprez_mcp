"""One-time upload URLs: clients send files straight from disk with ``PUT``.

``prepare_uploads`` and ``get_batch`` live in ``batches``; they are importable
from here too.
"""


def __getattr__(name):
    # lazy: the models need the app registry
    if name in ("prepare_uploads", "get_batch"):
        from . import batches

        return getattr(batches, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
