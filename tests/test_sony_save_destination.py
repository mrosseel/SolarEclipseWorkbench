"""Unit tests for Sony save-destination downloader decision logic."""

import unittest

from src.solareclipseworkbench.camera import sony_save_destination_needs_downloader


class TestSonySaveDestinationNeedsDownloader(unittest.TestCase):
    def test_none_or_empty_is_not_pc_only(self):
        self.assertFalse(sony_save_destination_needs_downloader(None))
        self.assertFalse(sony_save_destination_needs_downloader(""))
        self.assertFalse(sony_save_destination_needs_downloader("   "))

    def test_pc_only_variants_enable_downloader(self):
        self.assertTrue(sony_save_destination_needs_downloader("PC Only"))
        self.assertTrue(sony_save_destination_needs_downloader("pc-only"))
        self.assertTrue(sony_save_destination_needs_downloader("Computer only"))
        self.assertTrue(sony_save_destination_needs_downloader("Nur PC"))

    def test_mixed_or_camera_destinations_disable_downloader(self):
        self.assertFalse(sony_save_destination_needs_downloader("PC+Camera"))
        self.assertFalse(sony_save_destination_needs_downloader("PC/Camera"))
        self.assertFalse(sony_save_destination_needs_downloader("Camera Only"))
        self.assertFalse(sony_save_destination_needs_downloader("PC+Camara"))


if __name__ == '__main__':
    unittest.main()
