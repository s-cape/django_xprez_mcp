"""Tools that take files sent to one-time upload URLs."""

from typing import Any, Optional

from xprez_mcp import MCPServer, ModelProvider, Provider, ToolError, auth, tool
from xprez_mcp.contrib.oauth.auth import access_token
from xprez_mcp.contrib.uploads import get_batch, prepare_uploads
from xprez_mcp.core.files import file_argument
from xprez_mcp.permissions import allow_authenticated

from .models import Document, Tag

UploadedFile = file_argument(("data", "upload"))


class UploadProvider(Provider):
    namespace = "files"

    @tool
    def prepare(
        self,
        ctx,
        files: list[dict[str, Any]],
        batch: Optional[str] = None,
        content_types: Optional[list[str]] = None,
    ):
        return prepare_uploads(ctx, files, batch=batch, content_types=content_types)

    @tool
    def store(self, ctx, file: UploadedFile):
        Tag.objects.create(name=file.name)
        return {
            "name": file.name,
            "size": file.size,
            "sha256": file.sha256,
            "content_type": file.content_type,
            "content": file.read().decode("utf-8", "replace"),
            "streamed": file.value.read().decode() if file.source == "upload" else None,
        }

    @tool
    def store_and_fail(self, ctx, file: UploadedFile):
        Tag.objects.create(name=file.name)
        file.read()
        raise ToolError("Failed after reading.")

    @tool
    def store_two(self, ctx, file: UploadedFile, other: UploadedFile):
        Tag.objects.create(name=file.name)
        return {"same": file.read() == other.read()}

    @tool
    def store_raced(self, ctx, file: UploadedFile):
        """Another call uses the upload while this one runs."""
        Tag.objects.create(name=file.name)
        type(file.upload).objects.filter(pk=file.upload.pk).update(
            consumed_at=file.upload.received_at
        )
        return "stored"

    @tool(read_only=True)
    def batch(self, ctx, batch_id: str):
        batch = get_batch(ctx, batch_id)
        return {
            state: [upload.path for upload in getattr(batch, state)]
            for state in ("received", "missing", "expired", "failed", "consumed")
        }

    @tool
    def import_batch(self, ctx, batch_id: str):
        batch = get_batch(ctx, batch_id)
        contents = {
            upload.path: upload.open().read().decode() for upload in batch.received
        }
        return {"consumed": batch.consume(), "contents": contents}


class DocumentProvider(ModelProvider):
    fields = ["id", "title", "file"]
    actions = ["get", "create"]
    file_sources = ["data", "upload"]


uploads_mcp = MCPServer(
    name="uploads",
    authentication=[access_token, auth.bearer_token],
    permission=allow_authenticated,
    atomic_writes=True,
)
uploads_mcp.register(UploadProvider)
uploads_mcp.register(Document, DocumentProvider)
