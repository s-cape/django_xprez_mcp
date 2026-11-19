from .decorators import get_spec
from .items import Item


class Provider:
    """A class whose ``@tool``, ``@resource`` and ``@prompt`` methods are exposed.

    ``namespace`` prefixes tool and prompt names (``<namespace>_<name>``).
    Override ``has_permission`` to gate every item of the provider.
    """

    namespace = None

    def __init__(self, server, model=None):
        self.server = server
        self.model = model

    def has_permission(self, ctx):
        return True

    @classmethod
    def get_specs(cls):
        """Specs through the MRO, in definition order; an override without a
        decorator (or ``name = None``) removes the inherited item."""
        found = {}
        for klass in reversed(cls.__mro__):
            for attribute, value in vars(klass).items():
                found[attribute] = get_spec(value)
        return [spec for spec in found.values() if spec is not None]

    def get_items(self):
        return [Item(spec, self) for spec in self.get_specs()]
