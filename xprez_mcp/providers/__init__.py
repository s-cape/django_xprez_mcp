"""Providers: ``Provider`` (base), ``ModelProvider``, generic mode, and
``AdminProvider`` (in ``admin``, which needs ``django.contrib.admin``).

``FileArgumentsMixin`` lets any provider write model file fields from file
arguments."""

from .files import FileArgumentsMixin

__all__ = ["FileArgumentsMixin"]
