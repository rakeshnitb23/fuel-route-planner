from django.contrib.gis.db import models


class TruckStop(models.Model):
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=6, decimal_places=3)
    location = models.PointField(geography=True, srid=4326)

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'
