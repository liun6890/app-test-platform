import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from core.stall_monitor import StallDetector, fingerprint_png


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _gradient(reverse: bool = False) -> Image.Image:
    image = Image.new("RGB", (160, 240), "white")
    draw = ImageDraw.Draw(image)
    for x in range(image.width):
        value = int(255 * x / max(1, image.width - 1))
        if reverse:
            value = 255 - value
        draw.line((x, 0, x, image.height), fill=(value, value, value))
    return image


class StallMonitorTests(unittest.TestCase):
    def test_identical_images_have_identical_fingerprints(self):
        content = _png_bytes(_gradient())
        first = fingerprint_png(content)
        second = fingerprint_png(content)
        self.assertEqual(first, second)
        self.assertEqual(first.distance(second), 0)
        self.assertEqual(len(first.as_hex()), 64)

    def test_visually_different_images_have_distant_fingerprints(self):
        forward = fingerprint_png(_png_bytes(_gradient()))
        reverse = fingerprint_png(_png_bytes(_gradient(reverse=True)))
        self.assertGreater(forward.distance(reverse), 100)

    def test_status_and_navigation_bars_are_excluded(self):
        first = _gradient()
        second = first.copy()
        draw = ImageDraw.Draw(second)
        draw.rectangle((0, 0, second.width, 10), fill="red")
        draw.rectangle(
            (0, second.height - 12, second.width, second.height), fill="blue"
        )
        self.assertEqual(
            fingerprint_png(_png_bytes(first)),
            fingerprint_png(_png_bytes(second)),
        )

    def test_static_episode_is_reported_once(self):
        fingerprint = fingerprint_png(_png_bytes(_gradient()))
        detector = StallDetector(unchanged_seconds=30, max_hash_distance=4)
        self.assertFalse(detector.observe("pkg/.Main", fingerprint, 0).detected)
        self.assertFalse(detector.observe("pkg/.Main", fingerprint, 20).detected)
        event = detector.observe("pkg/.Main", fingerprint, 30)
        self.assertTrue(event.detected)
        self.assertEqual(event.unchanged_seconds, 30)
        self.assertFalse(detector.observe("pkg/.Main", fingerprint, 60).detected)

    def test_component_or_page_change_restarts_timer(self):
        first = fingerprint_png(_png_bytes(_gradient()))
        second = fingerprint_png(_png_bytes(_gradient(reverse=True)))
        detector = StallDetector(unchanged_seconds=30, max_hash_distance=4)
        detector.observe("pkg/.Main", first, 0)
        changed = detector.observe("pkg/.Other", first, 20)
        self.assertTrue(changed.state_changed)
        self.assertFalse(detector.observe("pkg/.Other", first, 40).detected)
        changed = detector.observe("pkg/.Other", second, 45)
        self.assertTrue(changed.state_changed)
        self.assertFalse(detector.observe("pkg/.Other", second, 70).detected)
        self.assertTrue(detector.observe("pkg/.Other", second, 75).detected)


if __name__ == "__main__":
    unittest.main()
