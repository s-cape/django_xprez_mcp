SECRET_KEY = "test-secret"
DEBUG = False
ALLOWED_HOSTS = ["testserver"]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
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
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
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
