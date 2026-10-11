"""
Django settings for spotter project.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/6.1/ref/settings/
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name):
    return [v.strip() for v in os.environ.get(name, '').split(',') if v.strip()]


# Development by default. Production sets DEBUG=False, SECRET_KEY and DOMAIN in .env
# (see .env.production.example and docker-compose.prod.yml).
# https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/

DEBUG = os.environ.get('DEBUG', 'True').lower() in ('1', 'true', 'yes')

SECRET_KEY = os.environ.get('SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('SECRET_KEY must be set when DEBUG is False.')
    SECRET_KEY = 'django-insecure-spotter-dev-only-change-me'

# The public domain served by Caddy, e.g. fuelroute.duckdns.org.
DOMAIN = os.environ.get('DOMAIN', '')
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS') or ([DOMAIN] if DOMAIN else [])
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS') or (
    [f'https://{DOMAIN}'] if DOMAIN else [])
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured('Set DOMAIN (or ALLOWED_HOSTS) when DEBUG is False.')
if not DEBUG:
    placeholders = [name for name in ('SECRET_KEY', 'DOMAIN', 'POSTGRES_PASSWORD')
                    if 'change-me' in os.environ.get(name, '')]
    if placeholders:
        raise ImproperlyConfigured(f"Replace 'change-me' in .env: {', '.join(placeholders)}")

# Send our origin (not the full URL) as Referer to other sites. OpenStreetMap's tile servers
# block browser requests without a Referer; Django's default 'same-origin' sends none.
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'

if not DEBUG:
    # Caddy terminates HTTPS and tells Django the original scheme.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.gis',
    'rest_framework',
    'fuel',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'spotter.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'spotter.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': os.environ['POSTGRES_DB'],
        'USER': os.environ['POSTGRES_USER'],
        'PASSWORD': os.environ['POSTGRES_PASSWORD'],
        'HOST': os.environ['POSTGRES_HOST'],
        'PORT': os.environ['POSTGRES_PORT'],
    }
}


REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'fuel.exceptions.api_exception_handler',
    # Public API with no logins: without this, a browser logged in to /admin/ would
    # need a CSRF token to POST from the index page.
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    # Per-IP limits (fuel.throttles), counted in the cache. "smart" calls a paid LLM;
    # "route" protects the free public OSRM server.
    'DEFAULT_THROTTLE_RATES': {
        'smart': os.environ.get('SMART_RATE_LIMIT', '20/hour'),
        'route': os.environ.get('ROUTE_RATE_LIMIT', '120/hour'),
    },
    # Behind Caddy, the client IP is the last X-Forwarded-For entry. Set NUM_PROXIES=1
    # only behind a proxy (docker-compose.prod.yml does); otherwise the header could be faked.
    'NUM_PROXIES': int(os.environ['NUM_PROXIES']) if os.environ.get('NUM_PROXIES') else None,
}


# Cache: stores routed trips so repeat requests and the map page skip OSRM.
# Redis when REDIS_URL is set; in-memory otherwise, and always for `manage.py test`
# (tests call cache.clear(), which must never flush a real Redis).

REDIS_URL = os.environ.get('REDIS_URL')
TESTING = sys.argv[1:2] == ['test']

CACHE_KEYS = {
    'KEY_PREFIX': 'fuelroute',
    'VERSION': 1,  # bump to invalidate every cached route
}

if REDIS_URL and not TESTING:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                # Fail fast when Redis is down; the app treats that as a cache miss.
                'socket_connect_timeout': 1,
                'socket_timeout': 1,
            },
            **CACHE_KEYS,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'routes',
            **CACHE_KEYS,
        }
    }


# Strategy router (POST /api/route/smart/): one LLM call picks a fuel strategy.
# Provider: 'openrouter' (default) or 'anthropic'. No key -> the endpoint still works
# and falls back to lowest_cost. Keys are never set under `manage.py test`, so tests
# can never call a real (paid) API.

STRATEGY_ROUTER_PROVIDER = os.environ.get('STRATEGY_ROUTER_PROVIDER', 'openrouter')
if STRATEGY_ROUTER_PROVIDER not in ('openrouter', 'anthropic'):
    raise ImproperlyConfigured("STRATEGY_ROUTER_PROVIDER must be 'openrouter' or 'anthropic'.")
OPENROUTER_API_KEY = '' if TESTING else os.environ.get('OPENROUTER_API_KEY', '')
ANTHROPIC_API_KEY = '' if TESTING else os.environ.get('ANTHROPIC_API_KEY', '')
STRATEGY_ROUTER_MODEL = os.environ.get('STRATEGY_ROUTER_MODEL') or (
    'claude-haiku-5-5' if STRATEGY_ROUTER_PROVIDER == 'anthropic'
    else 'anthropic/claude-haiku-5.5'
)
STRATEGY_ROUTER_TIMEOUT = 5  # seconds


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'  # filled by collectstatic, served by WhiteNoise
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}


# Logging: warnings (cache down, OSRM or LLM failures) go to the console, so they show
# in `docker compose logs web`.

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
}


# Email
# https://docs.djangoproject.com/en/6.1/topics/email/#topic-email-configuration

MAILERS = {
    'default': {
        'BACKEND': 'django.core.mail.backends.console.EmailBackend',
    },
}
