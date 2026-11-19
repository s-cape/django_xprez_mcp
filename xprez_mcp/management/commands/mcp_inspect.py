from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from xprez_mcp.server import KINDS


class Command(BaseCommand):
    help = "List the tools, resources and prompts an MCP server exposes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--server", default="xprez_mcp.mcp", help="Dotted path to an MCPServer."
        )
        parser.add_argument(
            "--user", help="Username; marks the items this user cannot see."
        )

    def handle(self, *args, **options):
        try:
            server = import_string(options["server"])
        except ImportError as error:
            raise CommandError(error) from None
        ctx = None
        if options["user"]:
            User = get_user_model()
            try:
                user = User._default_manager.get_by_natural_key(options["user"])
            except User.DoesNotExist:
                raise CommandError(f"No user {options['user']!r}.") from None
            ctx = server.get_context(None, user)
        for kind in KINDS:
            for identifier, item in server.catalog[kind].items():
                hidden = ctx is not None and not item.is_allowed(ctx)
                line = f"{kind:<9} {identifier:<40} {item.source}"
                self.stdout.write(line + ("  (hidden)" if hidden else ""))
        for item in server.withheld_writes:
            line = f"{item.kind:<9} {item.identifier:<40} {item.source}"
            self.stdout.write(line + "  (withheld: ALLOW_WRITES)")
        for provider in server.get_providers():
            for entry in getattr(provider, "unsupported", ()):
                label = type(provider).__qualname__
                self.stdout.write(f"note      {label}: unsupported {entry}")
