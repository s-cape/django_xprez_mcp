import atexit
import shutil
import tempfile

from django.core.files.storage import FileSystemStorage

# private and per test run; tests empty it as they go
location = tempfile.mkdtemp(prefix="xprez-mcp-uploads-")
atexit.register(shutil.rmtree, location, ignore_errors=True)
upload_storage = FileSystemStorage(location=location)
