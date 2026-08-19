"""
Django settings for bookmyseat project.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent


# ============================================================
# CORE SETTINGS
# ============================================================

DEBUG = os.environ.get("DEBUG", "True").lower() == "true"

SECRET_KEY = os.environ.get("SECRET_KEY")

if not SECRET_KEY:
    if DEBUG:
        # Local development fallback only.
        SECRET_KEY = "django-insecure-local-dev-only-change-this-before-production"
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable is required when DEBUG=False."
        )


ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "ALLOWED_HOSTS",
        ".vercel.app,django-book-two.vercel.app,localhost,127.0.0.1",
    ).split(",")
    if host.strip()
]


CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "https://django-book-two.vercel.app",
    ).split(",")
    if origin.strip()
]


# Local HTTP needs non-secure cookies.
# Production HTTPS should use secure cookies.
IS_PRODUCTION = (
    os.environ.get("IS_PRODUCTION", "False").lower() == "true"
    or not DEBUG
)

SESSION_COOKIE_SECURE = IS_PRODUCTION
CSRF_COOKIE_SECURE = IS_PRODUCTION

SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "users",
    "movies",
]


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


AUTH_USER_MODEL = "auth.User"

ROOT_URLCONF = "bookmyseat.urls"

WSGI_APPLICATION = "bookmyseat.wsgi.application"


# ============================================================
# TEMPLATES
# ============================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
        )
    }
else:
    if DEBUG:
        # Local fallback.
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }
    else:
        raise ImproperlyConfigured(
            "DATABASE_URL environment variable is required when DEBUG=False."
        )


# ============================================================
# PASSWORD VALIDATION
# ============================================================

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


# ============================================================
# INTERNATIONALIZATION
# ============================================================

LANGUAGE_CODE = "en-us"

TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")

USE_I18N = True
USE_TZ = True


# ============================================================
# STATIC / MEDIA
# ============================================================

STATIC_URL = "static/"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# ============================================================
# LOGIN
# ============================================================

LOGIN_URL = "login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ============================================================
# TASK 4 — RAZORPAY
# ============================================================

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get(
    "RAZORPAY_WEBHOOK_SECRET",
    "",
)


# ============================================================
# TASK 2 — PUBLIC URL FOR QR VERIFICATION
# ============================================================

SITE_BASE_URL = os.environ.get(
    "SITE_BASE_URL",
    "http://127.0.0.1:8000",
)


# ============================================================
# TASK 2 — CELERY / REDIS
# ============================================================

CELERY_BROKER_URL = os.environ.get(
    "CELERY_BROKER_URL",
    "redis://localhost:6379/0",
)

CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND",
    "redis://localhost:6379/0",
)

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE


# ============================================================
# TASK 2 — EMAIL
# ============================================================

# During local development, if SMTP isn't configured,
# emails are printed in the terminal instead.
if DEBUG and not os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"


EMAIL_HOST = os.environ.get("EMAIL_HOST", "")

EMAIL_PORT = int(
    os.environ.get(
        "EMAIL_PORT",
        "587",
    )
)

EMAIL_HOST_USER = os.environ.get(
    "EMAIL_HOST_USER",
    "",
)

EMAIL_HOST_PASSWORD = os.environ.get(
    "EMAIL_HOST_PASSWORD",
    "",
)

EMAIL_USE_TLS = (
    os.environ.get(
        "EMAIL_USE_TLS",
        "True",
    ).lower()
    == "true"
)

DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL",
    "noreply@bookmyseat.local",
)