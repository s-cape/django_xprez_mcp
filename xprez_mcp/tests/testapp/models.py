from django.conf import settings
from django.db import models


class Industry(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        verbose_name_plural = "industries"


class Tag(models.Model):
    name = models.CharField(max_length=50)


class ColoredTag(Tag):
    """Multi-table inheritance: an auto-created, concrete ``tag_ptr``."""

    color = models.CharField(max_length=20)


class Client(models.Model):
    SIZES = [("s", "Small"), ("m", "Medium"), ("l", "Large")]

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    group = models.CharField(max_length=100, blank=True)
    industry = models.ForeignKey(Industry, null=True, on_delete=models.SET_NULL)
    tags = models.ManyToManyField(Tag, blank=True)
    size = models.CharField(max_length=1, choices=SIZES, default="s")
    founded = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def label(self):
        return f"{self.name} ({self.slug})"


class Contact(models.Model):
    client = models.ForeignKey(
        Client, related_name="contacts", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
