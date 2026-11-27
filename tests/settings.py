SECRET_KEY = "tests"
DEBUG = False
USE_TZ = True
TIME_ZONE = "UTC"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "tests.testapp",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "tests.urls"
DRF_FAST_OUTPUT = {"VERIFY": False}
