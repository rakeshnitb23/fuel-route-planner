import logging

from rest_framework.throttling import ScopedRateThrottle

from fuel.services import CACHE_ERRORS

logger = logging.getLogger(__name__)


class SafeScopedRateThrottle(ScopedRateThrottle):
    """Per-IP rate limit for the view's throttle_scope. If the cache is down, allow the request."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except CACHE_ERRORS as exc:
            logger.warning('Rate limit check skipped, cache unavailable: %s', exc)
            return True
