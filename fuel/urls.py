from django.urls import path

from fuel.views import RouteView, SmartRouteView, route_map

urlpatterns = [
    path('route/', RouteView.as_view(), name='route'),
    path('route/smart/', SmartRouteView.as_view(), name='route-smart'),
    path('route/map/', route_map, name='route-map'),
]
