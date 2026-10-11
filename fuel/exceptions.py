from math import ceil

from rest_framework.exceptions import Throttled
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Wrap DRF's own errors (bad JSON, wrong method, ...) in the API's error shape."""
    response = exception_handler(exc, context)
    if response is not None:
        detail = getattr(exc, 'detail', None)
        response.data = {
            'error': {
                'code': getattr(exc, 'default_code', 'error'),
                'message': str(detail) if isinstance(detail, str) else 'Request error.',
                'details': {} if isinstance(detail, str) else (detail or {}),
            }
        }
        if isinstance(exc, Throttled) and exc.wait is not None:
            response.data['error']['details'] = {'retry_after_seconds': ceil(exc.wait)}
    return response
