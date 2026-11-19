SECRET_KEY = "test-secret"
DEBUG = False
ALLOWED_HOSTS = ["testserver"]
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "xprez_mcp",
    "xprez_mcp.tests.testapp",
]
ROOT_URLCONF = "xprez_mcp.tests.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
XPREZ_MCP = {
    "NAME": "test",
    "PERMISSION": "authenticated",
    "TOKEN_RESOLVER": "xprez_mcp.tests.testapp.tokens.resolve",
    "EXCLUDE": ["hidden_*"],
}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"xprez_mcp": {"handlers": ["null"], "propagate": False}},
}
