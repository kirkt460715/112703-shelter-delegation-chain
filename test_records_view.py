"""验证实际登记材料的读取和分批取用。"""
import json
from pathlib import Path
import tempfile
import unittest

from records_view import describe, load_sources, read_page


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).with_name("records")
        self.sources = load_sources(self.directory)

    def test_inventory_covers_actual_sources(self):
        files = {path.stem for path in self.directory.glob("*.json")}
        self.assertEqual(files, {row["source"] for row in describe(self.sources)})

    def test_pages_preserve_records(self):
        for name in self.sources:
            with self.subTest(source=name):
                gathered = []
                offset = 0
                while offset is not None:
                    page = read_page(self.sources, name, offset, 2)
                    gathered.extend(page["records"])
                    offset = page["next_offset"]
                original = json.loads((self.directory / f"{name}.json").read_text())
                self.assertEqual(gathered, original)

    def test_duplicate_record_within_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "same.json").write_text('[{"id":"same"},{"id":"same"}]')
            with self.assertRaises(ValueError):
                load_sources(path)
