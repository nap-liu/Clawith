"""Read-image builtin tool — dispatch images to a configured vision model."""

from app.services.tools.read_image.handler import handle_read_image

__all__ = ["handle_read_image"]
