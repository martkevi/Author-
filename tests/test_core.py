"""Offline tests for Telegram Book Studio."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import storage
import exporter

from engine import _extract_json, WritingEngineError


class BookStudioTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.data_dir = self.root / "data"
        self.output_dir = self.root / "exports"
        self.database_path = self.data_dir / "test.sqlite3"

        self.patches = [
            patch.object(config, "DATA_DIR", self.data_dir),
            patch.object(config, "OUTPUT_DIR", self.output_dir),
            patch.object(config, "DATABASE_PATH", self.database_path),
        ]

        for item in self.patches:
            item.start()

        storage.initialize_storage()

        self.user_id = 10001

        self.book = storage.create_book(
            user_id=self.user_id,
            title="The Silent City",
            genre="Mystery",
            audience="Adult readers",
            premise="A journalist investigates a mysterious disappearance.",
        )

        self.book_id = self.book["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

        self.temp_dir.cleanup()

    def test_book_creation_and_retrieval(self):
        book = storage.get_book(
            self.book_id,
            self.user_id,
        )

        self.assertIsNotNone(book)
        self.assertEqual(book["title"], "The Silent City")
        self.assertEqual(book["genre"], "Mystery")

    def test_users_cannot_access_other_users_books(self):
        book = storage.get_book(
            self.book_id,
            user_id=99999,
        )

        self.assertIsNone(book)

        chapters = storage.list_chapters(
            self.book_id,
            user_id=99999,
        )

        self.assertIsNone(chapters)

    def test_outline_persistence(self):
        outline = {
            "title": "The Silent City",
            "synopsis": "A journalist investigates a disappearance.",
            "chapters": [
                {
                    "number": 1,
                    "title": "The Missing Letter",
                    "summary": "The journalist receives a letter.",
                }
            ],
        }

        saved = storage.save_outline(
            self.book_id,
            self.user_id,
            outline,
        )

        self.assertTrue(saved)

        retrieved = storage.get_outline(
            self.book_id,
            self.user_id,
        )

        self.assertEqual(retrieved, outline)

    def test_chapter_persistence(self):
        chapter = storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="The Missing Letter",
            content="The journalist opened the letter and read it carefully.",
            summary="The journalist receives a mysterious letter.",
        )

        self.assertEqual(
            chapter["title"],
            "The Missing Letter",
        )

        retrieved = storage.get_chapter(
            self.book_id,
            self.user_id,
            1,
        )

        self.assertIsNotNone(retrieved)
        self.assertEqual(
            retrieved["content"],
            "The journalist opened the letter and read it carefully.",
        )

    def test_chapter_revision_history(self):
        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="Original Chapter",
            content="This is the original chapter text.",
            summary="Original version.",
        )

        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="Revised Chapter",
            content="This is the improved chapter text.",
            summary="Revised version.",
            revision_note="Improve the opening.",
        )

        current = storage.get_chapter(
            self.book_id,
            self.user_id,
            1,
        )

        revisions = storage.get_chapter_revisions(
            self.book_id,
            self.user_id,
            1,
        )

        self.assertEqual(
            current["content"],
            "This is the improved chapter text.",
        )

        self.assertEqual(len(revisions), 1)

        self.assertEqual(
            revisions[0]["content"],
            "This is the original chapter text.",
        )

    def test_book_statistics(self):
        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="Chapter One",
            content="One two three four five.",
            summary="A short chapter.",
            status="approved",
        )

        stats = storage.get_book_statistics(
            self.book_id,
            self.user_id,
        )

        self.assertEqual(stats["chapter_count"], 1)
        self.assertEqual(stats["total_words"], 5)
        self.assertEqual(stats["approved_chapters"], 1)

    def test_json_extraction(self):
        result = _extract_json(
            '{"title": "Test Book", "chapters": []}'
        )

        self.assertEqual(result["title"], "Test Book")

    def test_json_extraction_from_markdown(self):
        result = _extract_json(
            '```json\n{"title": "Test Book"}\n```'
        )

        self.assertEqual(result["title"], "Test Book")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(WritingEngineError):
            _extract_json("This is not valid JSON.")

    def test_txt_export(self):
        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="The Beginning",
            content="The rain fell over the silent city.",
            summary="The story begins.",
        )

        path = exporter.export_book(
            self.book_id,
            self.user_id,
            "txt",
        )

        self.assertTrue(path.exists())

        text = path.read_text(encoding="utf-8")

        self.assertIn("The Silent City", text)
        self.assertIn("The Beginning", text)
        self.assertIn(
            "The rain fell over the silent city.",
            text,
        )

    def test_docx_export(self):
        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="The Beginning",
            content="The rain fell over the silent city.",
            summary="The story begins.",
        )

        path = exporter.export_book(
            self.book_id,
            self.user_id,
            "docx",
        )

        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)

        from docx import Document

        document = Document(str(path))

        document_text = "\n".join(
            paragraph.text
            for paragraph in document.paragraphs
        )

        self.assertIn("The Silent City", document_text)
        self.assertIn("The Beginning", document_text)

    def test_epub_export(self):
        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="The Beginning",
            content="The rain fell over the silent city.",
            summary="The story begins.",
        )

        path = exporter.export_book(
            self.book_id,
            self.user_id,
            "epub",
        )

        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)

        import zipfile

        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()

            self.assertIn("mimetype", names)
            self.assertTrue(
                any(name.endswith(".xhtml") for name in names)
            )

    def test_pdf_export(self):
        storage.save_chapter(
            book_id=self.book_id,
            user_id=self.user_id,
            chapter_number=1,
            title="The Beginning",
            content="The rain fell over the silent city.",
            summary="The story begins.",
        )

        path = exporter.export_book(
            self.book_id,
            self.user_id,
            "pdf",
        )

        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)

        with path.open("rb") as file:
            self.assertEqual(
                file.read(5),
                b"%PDF-",
            )

    def test_export_rejects_unsupported_format(self):
        with self.assertRaises(exporter.ExportError):
            exporter.export_book(
                self.book_id,
                self.user_id,
                "unsupported",
            )

    def test_export_rejects_book_without_chapters(self):
        with self.assertRaises(exporter.ExportError):
            exporter.export_book(
                self.book_id,
                self.user_id,
                "txt",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
