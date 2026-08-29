"""Read-image builtin tool — dispatch images to a configured vision model."""

from app.services.tools.read_image.handler import (
    get_effective_read_image_max_bytes,
    handle_read_image,
)

__all__ = ["get_effective_read_image_max_bytes", "handle_read_image"]
