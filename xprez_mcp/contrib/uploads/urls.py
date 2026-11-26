from django.urls import path

from . import views


def server_urls(server):
    return [
        path(
            "uploads/<str:key_id>/",
            views.UploadView.as_view(server=server),
            name="upload",
        )
    ]
