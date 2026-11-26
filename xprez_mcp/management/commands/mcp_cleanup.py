"""``mcp_cleanup``: one scheduled command for every app's leftovers.

An app takes part by giving its ``AppConfig`` a method
``mcp_cleanup(self, now) -> {"what": count}``, e.g.
``{"access tokens": 12, "codes": 3}``.
"""

import logging

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger("xprez_mcp")


class Command(BaseCommand):
    help = (
        "Deletes what the installed MCP apps no longer need: expired OAuth codes "
        "and tokens, unused clients and so on. Schedule it hourly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            nargs="+",
            metavar="APP_LABEL",
            help="Clean up only these apps.",
        )

    def handle(self, *args, **options):
        configs = [
            config
            for config in apps.get_app_configs()
            if callable(getattr(config, "mcp_cleanup", None))
        ]
        if options["only"]:
            known = {config.label: config for config in configs}
            labels = list(dict.fromkeys(options["only"]))
            unknown = [label for label in labels if label not in known]
            if unknown:
                raise CommandError(
                    f"No cleanup for {', '.join(unknown)}; apps with one: "
                    f"{', '.join(sorted(known)) or 'none'}."
                )
            configs = [known[label] for label in labels]
        if not configs:
            self.stdout.write("No installed app has anything to clean up.")
            return
        now = timezone.now()
        failed = []
        for config in configs:
            # one app failing (a storage error, say) does not stop the others
            try:
                counts = config.mcp_cleanup(now)
            except Exception as error:
                failed.append(config.label)
                logger.exception("Cleanup of %s failed", config.label)
                self.stderr.write(f"{config.label}: failed: {error!r}")
                continue
            summary = ", ".join(f"{count} {name}" for name, count in counts.items())
            self.stdout.write(f"{config.label}: {summary or 'nothing'} deleted.")
        if failed:
            raise CommandError(f"Cleanup failed for {', '.join(failed)}.")
