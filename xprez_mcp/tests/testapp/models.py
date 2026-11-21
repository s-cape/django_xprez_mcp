from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Industry(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        verbose_name_plural = "industries"


class Tag(models.Model):
    name = models.CharField(max_length=50)


class Hue:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class HueField(models.CharField):
    """Loads a value class JSON cannot encode, like timezone or money fields."""

    def from_db_value(self, value, expression, connection):
        return None if value is None else Hue(value)


class ColoredTag(Tag):
    """Multi-table inheritance: an auto-created, concrete ``tag_ptr``."""

    color = HueField(max_length=20)


class Swatch(models.Model):
    tag = models.ForeignKey(
        ColoredTag, related_name="swatches", on_delete=models.CASCADE
    )
    hex = models.CharField(max_length=7)

    class Meta:
        verbose_name_plural = "swatches"


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


class Note(models.Model):
    text = models.CharField(max_length=100)
    content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.CASCADE
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    target = GenericForeignKey()


class Document(models.Model):
    title = models.CharField(max_length=100)
    file = models.FileField(upload_to="docs/")
    attachment = models.FileField(upload_to="attachments/", blank=True)

    def __str__(self):
        return self.title


class Page(models.Model):
    document = models.ForeignKey(
        Document, related_name="pages", on_delete=models.CASCADE
    )
    number = models.PositiveIntegerField()
    scan = models.FileField(upload_to="scans/", blank=True)

    class Meta:
        ordering = ["number"]
