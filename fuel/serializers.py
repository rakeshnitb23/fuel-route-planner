from rest_framework import serializers

from fuel.optimizer import STRATEGIES


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    strategy = serializers.ChoiceField(choices=list(STRATEGIES), default='lowest_cost')


class SmartRouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    preference = serializers.CharField(max_length=300, trim_whitespace=True, required=False,
                                       allow_blank=True, default='')
