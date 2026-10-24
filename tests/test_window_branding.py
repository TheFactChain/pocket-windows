import ast
import base64
import os
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import window_branding


class WindowBrandingTests(unittest.TestCase):
    def test_every_icon_frame_is_a_png_of_its_own_size(self):
        self.assertEqual(sorted(window_branding.FRAMES), [16, 32, 64, 256])
        for size, encoded in window_branding.FRAMES.items():
            data = base64.b64decode(encoded)
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', data[16:24]), (size, size))

    def test_window_icon_differs_from_the_panel_icon(self):
        panel = (ROOT / 'ui/AppIcon.ico').read_bytes()
        for encoded in window_branding.FRAMES.values():
            self.assertNotIn(base64.b64decode(encoded), panel)

    def test_branding_and_pointer_release_are_wired_into_the_controller(self):
        text = (ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8')
        tree = ast.parse(text)
        names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertLessEqual({'brand_work_window', 'release_pointer_in_fullscreen'}, names)
        self.assertIn("WINDOW_TITLE = 'Pocket Windows'", text)
        self.assertIn("env['SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS']='0'", text)
        branding = ast.get_source_segment(text, next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'brand_work_window'))
        self.assertIn('SendMessageTimeoutW', branding)
        self.assertNotIn('SetWindowTextW', branding)  # a plain call would wait for QEMU's UI thread


if __name__ == '__main__':
    unittest.main()
