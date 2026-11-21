from .items import PromptSpec, ResourceSpec, ToolSpec

SPEC_ATTRIBUTE = "_xprez_mcp_spec"


def get_spec(value):
    return getattr(value, SPEC_ATTRIBUTE, None)


def tool(func=None, **options):
    """Expose a provider method (or, through ``MCPServer.tool``, a function) as a tool.

    Options: name, title, description, permission, read_only, destructive,
    idempotent, open_world, input_schema, output_schema.
    """

    def decorator(func):
        return _attach(func, ToolSpec(func, **options))

    return decorator(func) if func is not None else decorator


def resource(uri, **options):
    """Expose a method as a resource. ``{param}`` in the URI makes it a template.

    Options: name, title, description, permission, mime_type.
    """

    def decorator(func):
        return _attach(func, ResourceSpec(func, uri, **options))

    return decorator


def prompt(func=None, **options):
    """Expose a method as a prompt. Options: name, title, description, permission."""

    def decorator(func):
        return _attach(func, PromptSpec(func, **options))

    return decorator(func) if func is not None else decorator


def _attach(func, spec):
    setattr(func, SPEC_ATTRIBUTE, spec)
    if hasattr(spec, "complete"):
        func.complete = spec.complete
    return func
