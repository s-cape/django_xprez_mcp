from django.urls import include, path

from xprez_mcp import mcp
from xprez_mcp.contrib.oauth.views import ConnectionsView
from xprez_mcp.tests.testapp.admin_servers import admin_generic_mcp, admin_mcp
from xprez_mcp.tests.testapp.domain_servers import domain_mcp, plain_mcp
from xprez_mcp.tests.testapp.file_servers import (
    files_admin_mcp,
    files_form_mcp,
    files_generic_mcp,
    files_mcp,
)
from xprez_mcp.tests.testapp.model_servers import generic_mcp, model_mcp
from xprez_mcp.tests.testapp.servers import oauth_mcp, open_mcp, staff_mcp
from xprez_mcp.tests.testapp.token_admin import site as token_site
from xprez_mcp.tests.testapp.token_admin import token_admin_mcp

urlpatterns = [
    path("", include("xprez_mcp.contrib.oauth.well_known")),
    path("account/connections/", ConnectionsView.as_view()),
    path("mcp/", mcp.urls),
    path("oauth/mcp/", oauth_mcp.urls),
    path("staff/mcp/", staff_mcp.urls),
    path("open/mcp/", open_mcp.urls),
    path("token-admin/mcp/", token_admin_mcp.urls),
    path("token-admin/", token_site.urls),
    path("models/mcp/", model_mcp.urls),
    path("generic/mcp/", generic_mcp.urls),
    path("admin/mcp/", admin_mcp.urls),
    path("admin-generic/mcp/", admin_generic_mcp.urls),
    path("files/mcp/", files_mcp.urls),
    path("files-generic/mcp/", files_generic_mcp.urls),
    path("files-admin/mcp/", files_admin_mcp.urls),
    path("files-form/mcp/", files_form_mcp.urls),
    path("domain/mcp/", domain_mcp.urls),
    path("plain/mcp/", plain_mcp.urls),
]

try:
    from xprez_mcp.tests.testapp.drf_servers import drf_mcp
except ImportError:  # DRF is optional
    pass
else:
    urlpatterns.append(path("drf/mcp/", drf_mcp.urls))
