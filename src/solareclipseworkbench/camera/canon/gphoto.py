from ..gphoto import GPhotoCamera


class CanonGPhoto(GPhotoCamera):
    """Adapter specialized for Canon cameras. Future Canon-specific
    helpers can be added here."""

    def __init__(self, gp_camera, name: str):
        super().__init__(gp_camera, name)
        self.vendor = 'Canon'


# Backward-compatible alias
CanonCamera = CanonGPhoto
