from django.urls import path

from xprez_mcp import mcp
from xprez_mcp.tests.testapp.servers import open_mcp, staff_mcp

urlpatterns = [
    path("mcp/", mcp.urls),
    path("staff/mcp/", staff_mcp.urls),
    path("open/mcp/", open_mcp.urls),
]
