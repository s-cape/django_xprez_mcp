from django.urls import path

from xprez_mcp import mcp
from xprez_mcp.tests.testapp.admin_servers import admin_generic_mcp, admin_mcp
from xprez_mcp.tests.testapp.model_servers import generic_mcp, model_mcp
from xprez_mcp.tests.testapp.servers import open_mcp, staff_mcp

urlpatterns = [
    path("mcp/", mcp.urls),
    path("staff/mcp/", staff_mcp.urls),
    path("open/mcp/", open_mcp.urls),
    path("models/mcp/", model_mcp.urls),
    path("generic/mcp/", generic_mcp.urls),
    path("admin/mcp/", admin_mcp.urls),
    path("admin-generic/mcp/", admin_generic_mcp.urls),
]

try:
    from xprez_mcp.tests.testapp.drf_servers import drf_mcp
except ImportError:  # DRF is optional
    pass
else:
    urlpatterns.append(path("drf/mcp/", drf_mcp.urls))
