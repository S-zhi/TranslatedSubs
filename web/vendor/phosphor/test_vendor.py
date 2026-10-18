"""Offline assertions for the pinned, unmodified Phosphor regular assets."""

import hashlib
import json
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


VENDOR = Path(__file__).resolve().parent
WEB = VENDOR.parent.parent
CSS = (VENDOR / "regular/style.css").read_text(encoding="utf-8")
PROVENANCE = json.loads((VENDOR / "provenance.json").read_text())
ICONS = dict(re.findall(
    r'\.ph\.(ph-[a-z0-9-]+):before\s*\{\s*content:\s*"\\([0-9a-f]+)";', CSS
))


class VendorTests(unittest.TestCase):
    def test_pinned_provenance_and_bytes(self):
        self.assertEqual(PROVENANCE["package"], "@phosphor-icons/web")
        self.assertEqual(PROVENANCE["version"], "2.1.1")
        self.assertEqual(PROVENANCE["license"], "MIT")
        self.assertEqual(PROVENANCE["package_url"],
                         "https://registry.npmjs.org/@phosphor-icons/web/-/web-2.1.1.tgz")
        self.assertEqual(PROVENANCE["archive_sha256"],
                         "c74ab1decb33d7cefa22a7c412345e30d28ac39941b3be6837bc956cbef654d0")
        self.assertEqual(PROVENANCE["integrity"],
                         "sha512-QjrfbItu5Rb2i37GzsKxmrRHfZPTVk3oXSPBnQ2+oACDbQRWGAeB0AsvZw263n1nFouQuff+khOCtRbrc6+k+A==")
        expected = {"LICENSE", "regular/style.css"} | {
            f"regular/Phosphor.{suffix}" for suffix in ("svg", "ttf", "woff", "woff2")
        }
        self.assertEqual({item["path"] for item in PROVENANCE["files"]}, expected)
        for item in PROVENANCE["files"]:
            with self.subTest(path=item["path"]):
                path = VENDOR / item["path"]
                self.assertFalse(path.is_symlink())
                data = path.read_bytes()
                self.assertEqual(len(data), item["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
        self.assertIn("Copyright (c) 2020-2021 Phosphor Icons",
                      (VENDOR / "LICENSE").read_text())

    def test_all_css_urls_are_local_and_exist(self):
        urls = re.findall(r'url\([\s\"\']*([^\)\"\'\s]+)', CSS)
        self.assertEqual(len(urls), 4)
        self.assertNotIn("@import", CSS)
        for url in urls:
            with self.subTest(url=url):
                parts = urlsplit(url)
                self.assertFalse(parts.scheme or parts.netloc or parts.query)
                path = (VENDOR / "regular" / parts.path).resolve()
                self.assertEqual(path.parent, VENDOR / "regular")
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

    def test_font_formats(self):
        for suffix, magic in (("woff2", b"wOF2"), ("woff", b"wOFF"),
                              ("ttf", b"\x00\x01\x00\x00")):
            with self.subTest(suffix=suffix):
                self.assertEqual((VENDOR / f"regular/Phosphor.{suffix}").read_bytes()[:4], magic)
        svg = ET.parse(VENDOR / "regular/Phosphor.svg")
        self.assertIsNotNone(svg.find(".//{http://www.w3.org/2000/svg}font[@id='Phosphor']"))

    def test_css_codepoints_exist_in_upstream_svg_font(self):
        svg = ET.parse(VENDOR / "regular/Phosphor.svg")
        glyphs = {glyph.get("unicode") for glyph in svg.iter()
                  if glyph.tag.endswith("}glyph") and glyph.get("d")}
        self.assertGreater(len(ICONS), 1000)
        missing = {name for name, code in ICONS.items() if chr(int(code, 16)) not in glyphs}
        self.assertEqual(missing, set())

    def test_application_icons_and_stylesheet_entry(self):
        paths = list(WEB.glob("*.html"))
        paths += list((WEB / "js").rglob("*.js"))
        paths += list((WEB / "styles").rglob("*.css"))
        used = set()
        for path in paths:
            names = set(re.findall(r"\bph-[a-z0-9]+(?:-[a-z0-9]+)*\b",
                                   path.read_text(encoding="utf-8")))
            # Existing UI uses this animation helper alongside ph-spinner-gap;
            # it is not an icon name or a selector provided by upstream.
            names.discard("ph-spin")
            used.update(names)
            with self.subTest(path=str(path.relative_to(WEB))):
                self.assertEqual(names - ICONS.keys(), set())
        self.assertGreater(len(used), 0)
        self.assertIn('href="vendor/phosphor/regular/style.css"',
                      (WEB / "index.html").read_text(encoding="utf-8"))
        print(f"Validated {len(used)} application icon names across {len(paths)} source files.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
