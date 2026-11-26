from django.core import checks
from django.core.exceptions import ImproperlyConfigured

from . import conf


@checks.register()
def check_uploads(app_configs=None, **kwargs):
    try:
        conf.get_storage()
    except (ImproperlyConfigured, ImportError) as error:
        return [
            checks.Error(
                str(error),
                hint="Point it at storage that is not public, such as a "
                "FileSystemStorage outside MEDIA_ROOT or a private bucket.",
                id="xprez_mcp_uploads.E001",
            )
        ]
    return []
