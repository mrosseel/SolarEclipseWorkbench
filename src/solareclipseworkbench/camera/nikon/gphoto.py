from ..gphoto import GPhotoCamera


class NikonGPhoto(GPhotoCamera):
    """Adapter specialized for Nikon cameras. Future Nikon-specific
    helpers can be added here."""

    def __init__(self, gp_camera, name: str):
        super().__init__(gp_camera, name)
        self.vendor = 'Nikon'


# Backward-compatible alias
NikonCamera = NikonGPhoto
